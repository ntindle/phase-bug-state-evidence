#!/usr/bin/env python3
"""Issue #6862 revalidation on v0.98.0 (protocol 94): Esper's to Magicite.

Reported (Discord): "AI just exiled [[Sylvan Reclamation]] with
[[Esper's to Magicite]]. It is sitting on its battlefield."

Oracle: "Exile each opponent's graveyard. When you do, choose up to one
target creature card exiled this way. Create a token that's a copy of that
card, except it's an artifact and it loses all other card types."

The 2026-09-11 run (20260911-6862f, v0.80.0, protocol 69, evidence commit
234bfaed884cff5197adfe5c1e6543743fcf35df) reproduced a clearly identified
related failure in the same clause: Esper's resolved correctly (exiled P1's
graveyard) but the reflexive "choose up to one target creature card exiled
this way" trigger never fired across two independent games -- no stack
entry, no TargetSelection prompt (411 waiting_for transitions: only
MulliganDecision / Priority / DeclareAttackers / DeclareBlockers /
DiscardToHandSize / GameOver), zero interaction submissions, no token.

This run re-validates on the pinned v0.98.0 release (protocol 94) per the
playbook staleness rule.

Plan (two human seats, native engine):
  SETUP  - land drops; P1 casts Grizzly Bears, attacks with them; P0 casts
           Grave Titan and blocks Bears -> 2+ Bear cards in P1's graveyard.
  LOAD   - P1 holds cards (no land drops / casts / attacks) until a cleanup
           discard puts Sylvan Reclamation into P1's graveyard.
  READY  - P0 main phase priority, Esper's to Magicite in hand, >=4 untapped
           lands (incl. a Swamp) -> export pre.json -> cast Esper's.
  RESOLVE- let Esper's resolve (P1 graveyard exiled), then its reflexive
           "choose up to one target creature card exiled this way" trigger
           must prompt P0 with a TargetSelection.
  TARGET - record the advertised candidates (names, zones, types).
           A2: Bear creature cards are offered.
           A3: Sylvan Reclamation (instant) is NOT offered. If it IS offered
               -> illegal branch: submit it; acceptance = full bug, rejection
               = offered-but-enforced (still a filter defect at the offer).
           Control (legal branch): choose a Bear -> token must be an
           artifact-only copy of the Bear (A5).

Behavioral contract:
  A1 setup_ok            pre.json: P1 gy has >=1 Bear card and >=1 Sylvan
                         Reclamation; P0 cast Esper's from hand w/ mana
  A2 creature_offered    target prompt lists Bear card(s) as candidates
  A3 noncreature_excluded Sylvan Reclamation NOT among candidates
  A4 illegal_enforcement (only if A3 failed) submit Reclamation: rejected
                         => offered-but-enforced; accepted => full bug
  A5 control_token       choosing a Bear yields an artifact-only Bear token
                         on P0's battlefield
  A6 cleanup             game proceeds; stack empty; no stuck prompt

Verdict = reproduced iff A3 fails (noncreature offered), A4 shows the
illegal choice accepted, or A2 fails because the reflexive trigger never
prompted (the clearly identified related failure from the v0.80.0 run);
not-reproduced iff A2, A3, A5, A6 pass.
"""
import asyncio
import copy
import hashlib
import json
import logging
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client as _client
_client.URL = "ws://127.0.0.1:9374/ws"
from client import PhaseClient, deck  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario6862")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
EVID_RUN_ID = "20261001-6862"
EVDIR = f"{BACKFILL}/evidence/6862/{EVID_RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.path.exists(EVDIR) or not os.listdir(EVDIR), "EVDIR not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


SERVER_IDENTITY = {
    "validated_version": "v0.98.0",
    "build_commit": "61e8550",
    "protocol_version": 94,
    "server_binary_sha256": "15c50bbd3e90b9af49c851d9a56a775f4d74f5874f19e5ea231520816c8170a9",
    "card_data_sha256": "1a5919f2a50754c7f5e48922390816150a20b703821114adfe08427ff0b11960",
    "draft_pools_sha256": "bf3316202d84068ac38bcec48c5fc57d38d7834aef5f18c6b410ba9f64afd594",
    "signature_verified": True,
}

for _f, _k in (("server/releases/v0.98.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.98.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.98.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"

ESPER = "Espers to Magicite"   # exact card-data name (no apostrophe)
RECLAM = "Sylvan Reclamation"
BEAR = "Grizzly Bears"
TITAN = "Grave Titan"
SWAMP = "Swamp"
FOREST = "Forest"
PLAINS = "Plains"

ST = {"stage": "SETUP", "stop": False, "retry": False,
      "cast_turn": None, "target_wait_t0": None}
CAST = {}
P1MODE = {"hold": False}
MULLS = {}
SHAPES = set()
WF_SEEN = []
SUBMITTED = set()
LAST_SUBMIT = {"iid": None}
TARGET = {"prompt_seen": False, "candidates": None, "shape": None,
          "answered": False, "illegal_tried": False,
          "illegal_accepted": None, "control_tried": False,
          "token_oid": None, "seen_at": None, "control_at": None}
TOKEN = {"seen": False, "oid": None, "obj": None}
_WATCH = {}


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


say("server identity hashes verified against on-disk pinned artifacts")


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(),
                               "event": event + "_unserializable",
                               "error": str(e)}) + "\n")
    WIRE.flush()


def oname(o):
    return o.get("base_name") or o.get("card_name") or o.get("name") or ""


def owner_of(o, pid):
    return o.get("owner") == pid or o.get("controller") == pid


def hand_oids(state, pid):
    return [str(oid) for oid, o in state["objects"].items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if oname(state["objects"][oid]) == name:
            return oid
    return None


def bf(state, pid):
    return [(oid, o) for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def zone_oids(state, pid, zone, name=None):
    out = []
    for oid, o in state["objects"].items():
        if o.get("zone") == zone and owner_of(o, pid):
            if name is None or oname(o) == name:
                out.append(str(oid))
    return out


def n_untapped_lands(state, pid):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) in (SWAMP, FOREST, PLAINS) and not o.get("tapped"))


def untapped_swamp(state, pid):
    return any(oname(o) == SWAMP and not o.get("tapped")
               for _, o in bf(state, pid))


def my_main(state, pid):
    return (state.get("active_player") == pid
            and state.get("priority_player") == pid
            and state.get("phase") in ("PreCombatMain", "PostCombatMain",
                                       "Main"))


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_player(state):
    return (wf_of(state).get("data") or {}).get("player")


def merged_actions(st):
    return st.get("legal_actions") or []


def vi_ops(c):
    st = c.latest
    if not st:
        return []
    return (st.get("viewer_interaction") or {}).get("opportunities", []) or []


def pending_for(state, pid):
    data = wf_of(state).get("data") or {}
    for p in data.get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            return p
    return None


def record_wf(state):
    wf = wf_of(state).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf, "stage": ST["stage"]})


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": ST["stage"]})
    await c.send_action(action)


async def send_interaction(c, sub):
    iid = (sub or {}).get("interactionId")
    if iid:
        LAST_SUBMIT["iid"] = iid
    wire("interaction_submit", {"who": c.name, "submission": sub,
                                "stage": ST["stage"]})
    await c.send_interaction(sub)


def drain_rejections(c):
    found = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            found.append({"type": t, "data": data})
            wire("rejected", {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {json.dumps(data, default=str)[:300]}")
    return found


async def export_now(path):
    s = await C0.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return s


def castspell_advertised(acts, oid):
    if not oid:
        return None
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


def watch(c):
    now = time.time()
    last = _WATCH.get(c.name)
    if last and last["rev"] == c.revision and now - last["t"] > 45:
        st = c.latest
        view = "no-state"
        if st:
            s = st["state"]
            view = (f"turn={s.get('turn')} phase={s.get('phase')} "
                    f"active={s.get('active_player')} wf={json.dumps(wf_of(s), default=str)[:160]}")
        say(f"WATCHDOG [{c.name}] revision {c.revision} stale 45s+: {view}")
        last["t"] = now
    elif not last or last["rev"] != c.revision:
        _WATCH[c.name] = {"rev": c.revision, "t": now}


def p0_rank(state):
    def rank(oid):
        nm = oname(state["objects"][oid])
        if nm == ESPER:
            return 2
        if nm == SWAMP:
            return 0
        return 1
    return rank


def p1_rank(state):
    def rank(oid):
        nm = oname(state["objects"][oid])
        if nm == RECLAM:
            return 0
        if nm == BEAR:
            return 2
        return 1
    return rank


# ------------------------------------------------------------------ shared p94 ticks

async def do_mulligan(c, pid, tag, keep_fn):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "MulliganDecision":
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    if MULLS.get((tag, "kept")):
        return False
    mull_count = MULLS.get((tag, "mulls"), 0)
    choice = "Keep" if (keep_fn(state) or mull_count >= 2) else "Mulligan"
    say(f"[{tag}] mulligan -> {choice} (prior mulligans: {mull_count})")
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": choice}}})
    if choice == "Mulligan":
        MULLS[(tag, "mulls")] = mull_count + 1
    else:
        MULLS[(tag, "kept")] = True
    wire("mulligan", {"who": tag, "decision": choice})
    return True


async def do_bottom(c, pid, tag, rank_fn):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    pend = pending_for(state, pid)
    if not pend:
        return False
    if (pend.get("phase") or {}).get("type") != "BottomCards":
        return False
    if (tag, "bottomed") in MULLS:
        return False
    n = (pend.get("phase") or {}).get("count") or 1
    picks = [int(x) for x in sorted(hand_oids(state, pid),
                                    key=rank_fn(state))[:n]]
    say(f"[{tag}] bottoming {n}: "
        f"{[oname(state['objects'][str(x)]) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    MULLS[(tag, "bottomed")] = True
    wire("bottom", {"who": tag, "count": n})
    return True


_DISCARD_REV = {}


async def do_discard(c, pid, tag, rank_fn):
    st = c.latest
    if not st:
        return False
    rev = st.get("state_revision", -1)
    if _DISCARD_REV.get((c.name, rev)):
        return False
    state = st["state"]
    if wf_of(state).get("type") != "DiscardToHandSize":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    n = len(hand_oids(state, pid)) - 7
    if n <= 0:
        return False
    picks = [int(x) for x in sorted(hand_oids(state, pid),
                                    key=rank_fn(state))[:n]]
    say(f"[{tag}] discarding to hand size: "
        f"{[oname(state['objects'][str(x)]) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    _DISCARD_REV[(c.name, rev)] = True
    return True


async def pay_tick(c):
    st = c.latest
    if not st:
        return False
    for a in merged_actions(st):
        if a.get("type") in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    return False


async def answer_assign_damage(c, pid, tag):
    """Answer AssignCombatDamage (the attacker's damage-assignment decision).
    Prefer the engine-advertised legacy action submitted verbatim after a
    sanity check (the 1362 precedent); fall back to a viewer-interaction
    opportunity if the server offers one instead."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "AssignCombatDamage":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    acts = merged_actions(st)
    for a in acts:
        if a.get("type") == "AssignCombatDamage":
            d = copy.deepcopy(a.get("data", {}))
            assigns = d.get("assignments", []) or []
            tram = int(d.get("trample_damage", 0) or 0)
            ctl = int(d.get("controller_damage", 0) or 0)
            total = sum(int(x[1]) for x in assigns) + tram + ctl
            # the attacker must assign exactly its power (CR 510.1c/d)
            atk_id = (wf_of(state).get("data") or {}).get("attacker_id")
            atk = state["objects"].get(str(atk_id), {})
            try:
                pwr = int(atk.get("power") or 0)
            except (TypeError, ValueError):
                pwr = 0
            ok = all(isinstance(x, (list, tuple)) and len(x) == 2
                     and int(x[1]) >= 0 for x in assigns) \
                and pwr > 0 and total == pwr
            if ("assign_dmg_shape",) not in SHAPES:
                SHAPES.add(("assign_dmg_shape",))
                wire("assign_damage_advertised",
                     {"who": tag, "data": d, "attacker_power": pwr})
            if ok:
                await submit_as_is(c, {"type": "AssignCombatDamage",
                                       "data": d})
                say(f"[{tag}] assigns combat damage (advertised default): "
                    f"{assigns} tram={tram} ctl={ctl} (power {pwr})")
                wire("assign_damage_submitted",
                     {"who": tag, "assignments": assigns})
                return True
            say(f"[{tag}] REFUSES advertised damage assignment: {d} "
                f"(attacker power {pwr})")
            wire("assign_damage_refused",
                 {"who": tag, "data": d, "attacker_power": pwr})
            return False
    # viewer-interaction fallback
    for op in vi_ops(c):
        iid = op.get("interactionId") or op.get("id")
        if not iid or iid in SUBMITTED:
            continue
        resp = op.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        if ("assign_dmg_vi",) not in SHAPES:
            SHAPES.add(("assign_dmg_vi",))
            wire("assign_damage_vi_shape", {"who": tag, "opportunity": op})
            say(f"[{tag}] AssignCombatDamage via viewer interaction; "
                f"shape logged, deferring")
    return False


def find_relations_op(c):
    for op in vi_ops(c):
        resp = op.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        spec = data.get("spec") or {}
        if resp.get("type") == "schema" and isinstance(spec, dict) \
                and spec.get("type") == "relations":
            return op
    return None


def relations_from_edges(edges, want):
    """Build a relations response from engine edges: each edge's sourceId
    paired with its first targetId (want filters which edges to include)."""
    rels = []
    for e in edges:
        src = e.get("sourceId")
        tids = e.get("targetIds") or []
        if src and tids and want(src, e):
            rels.append({"sourceId": src, "targetId": tids[0],
                         "group": None})
    return rels


async def answer_declare(c, pid, tag, attack_all=False, block_greedy=False):
    """Declare attackers/blockers via the relations-schema interaction
    (protocol 94 has no legacy DeclareAttackers/DeclareBlockers action).
    attack_all: include every engine-advertised attack edge (P1's Bears).
    block_greedy: pair each advertised attacker with one untapped Titan."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    wf = wf_of(state)
    wtype = wf.get("type")
    if wtype not in ("DeclareAttackers", "DeclareBlockers"):
        return False
    if str(wf_player(state)) != str(pid):
        return False
    op = find_relations_op(c)
    if not op:
        return False
    iid = op.get("interactionId") or op.get("id")
    data = (op.get("response") or {}).get("data", {}) or {}
    spec = data.get("spec", {}) or {}
    edges = (spec.get("data") or {}).get("edges", [])
    cands = {ch.get("id"): ch for ch in data.get("candidates", [])}

    def cand_ref(cid):
        ch = cands.get(cid, {})
        for s in ch.get("surfaces", []) or []:
            if s.get("type") == "object":
                return str((s.get("data") or {}).get("reference"))
        return None

    def cand_name(cid):
        ref = cand_ref(cid)
        o = state["objects"].get(ref, {}) if ref else {}
        return oname(o)

    rels = []
    if wtype == "DeclareAttackers" and attack_all:
        # all advertised edges: P1's Bears -> P0
        rels = relations_from_edges(edges, lambda src, e: True)
    elif wtype == "DeclareBlockers" and block_greedy:
        # greedy 1:1 Titan -> attacker pairing
        used_titans = set()
        for e in edges:
            src, tids = e.get("sourceId"), e.get("targetIds") or []
            if not src or not tids:
                continue
            if cand_name(src) != TITAN:
                continue
            ref = cand_ref(src)
            o = state["objects"].get(ref, {}) if ref else {}
            if o.get("tapped") or ref in used_titans:
                continue
            rels.append({"sourceId": src, "targetId": tids[0],
                         "group": None})
            used_titans.add(ref)
    # InteractionResponse::Relations { relations: Vec<InteractionRelation> }
    sub = {"interactionId": iid,
           "response": {"type": "relations", "data": {"relations": rels}}}
    wire("declare", {"who": tag, "wf": wtype, "n_edges": len(edges),
                     "n_rels": len(rels), "submission": sub})
    say(f"[{tag}] declares {wtype}: {len(rels)} relations "
        f"({len(edges)} edges)")
    await c.send_interaction(sub)
    SUBMITTED.add(iid)
    return True

# ------------------------------------------------- Esper's target prompt

def candidate_info(op, state):
    """Resolve opportunity candidates to (choiceId, ref_oid, name, zone)."""
    data = (op.get("response") or {}).get("data", {}) or {}
    out = []
    for ch in data.get("choices") or data.get("candidates") or []:
        ref = None
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict) and "reference" in d:
                ref = str(d["reference"])
                break
        o = state["objects"].get(str(ref), {}) if ref else {}
        out.append({"choice_id": ch.get("id"), "ref": ref,
                    "name": oname(o), "zone": o.get("zone"),
                    "owner": o.get("owner")})
    return out


async def answer_target_prompt(c, state, st):
    """Handle P0's TargetSelection for the Esper's reflexive trigger."""
    if wf_of(state).get("type") != "TargetSelection":
        return False
    if str(wf_player(state)) != "0":
        return False
    for op in vi_ops(c):
        iid = op.get("interactionId") or op.get("id")
        if not iid or iid in SUBMITTED:
            continue
        resp = op.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        cands = candidate_info(op, state)
        zones = {c["zone"] for c in cands}
        # This is the Esper's trigger prompt iff candidates come from Exile.
        if "Exile" not in zones:
            continue
        # Illegal submission still pending on a re-offered prompt: the
        # engine did not accept it (rejected or ignored).
        if TARGET["illegal_tried"] and TARGET["illegal_accepted"] is None \
                and iid not in SUBMITTED:
            TARGET["illegal_accepted"] = False
            say("[P0] illegal submission not accepted (prompt re-offered)")
            wire("illegal_rejected_by_reoffer", {})
        spec = data.get("spec") or {}
        spec_type = spec.get("type") if isinstance(spec, dict) else None
        key = (rtype, spec_type, len(chs))
        if key not in SHAPES:
            SHAPES.add(key)
            wire("target_prompt", {"rtype": rtype, "spec": spec_type,
                                   "candidates": cands,
                                   "opportunity": op})
            say(f"[P0] Esper's target prompt: rtype={rtype} spec={spec_type}")
            for cc in cands:
                say(f"    candidate: {cc['name']} (zone={cc['zone']}, "
                    f"choice={cc['choice_id']})")
        if TARGET["candidates"] is None:
            TARGET["candidates"] = cands
            TARGET["shape"] = key
            TARGET["prompt_seen"] = True
            TARGET["seen_at"] = time.time()
            await export_now("mid_target.json")
            say("[P0] recorded target candidates; exported mid_target.json")

        reclam_ch = next((x for x in cands if x["name"] == RECLAM), None)
        bear_ch = next((x for x in cands if x["name"] == BEAR), None)

        # Illegal branch first (only when the bug offers it).
        if reclam_ch and not TARGET["illegal_tried"]:
            if rtype == "schema" and spec_type in ("sequence", "select"):
                resp_out = {"type": spec_type,
                            "data": {"choiceIds": [reclam_ch["choice_id"]]}}
            elif rtype == "exactChoices":
                resp_out = {"type": "choose",
                            "data": {"choiceId": reclam_ch["choice_id"]}}
            else:
                say(f"[P0] illegal branch: unexpected shape {key}; skipping")
                TARGET["illegal_tried"] = True
                continue
            await send_interaction(c, {"interactionId": iid,
                                       "response": resp_out})
            SUBMITTED.add(iid)
            TARGET["illegal_tried"] = True
            say("[P0] ILLEGAL branch: submitted Sylvan Reclamation as target")
            return True
        # Legal control branch.
        if bear_ch and not TARGET["control_tried"] and \
                (not reclam_ch or TARGET["illegal_tried"]):
            if rtype == "schema" and spec_type in ("sequence", "select"):
                resp_out = {"type": spec_type,
                            "data": {"choiceIds": [bear_ch["choice_id"]]}}
            elif rtype == "exactChoices":
                resp_out = {"type": "choose",
                            "data": {"choiceId": bear_ch["choice_id"]}}
            else:
                say(f"[P0] control branch: unexpected shape {key}; skipping")
                TARGET["control_tried"] = True
                continue
            await send_interaction(c, {"interactionId": iid,
                                       "response": resp_out})
            SUBMITTED.add(iid)
            TARGET["control_tried"] = True
            TARGET["control_at"] = time.time()
            TARGET["answered"] = True
            say("[P0] CONTROL branch: submitted Grizzly Bears as target")
            return True
    return False


# ------------------------------------------------------------------ tick

async def tick(c, pid, is_p0):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), merged_actions(st)
    tag = "P0" if is_p0 else "P1"
    if is_p0:
        keep = any(oname(state["objects"][o]) == TITAN
                   for o in hand_oids(state, pid)) and \
            sum(1 for o in hand_oids(state, pid)
                if oname(state["objects"][o]) == SWAMP) >= 2
        if await do_mulligan(c, pid, tag, lambda s: keep):
            return True
    else:
        if await do_mulligan(c, pid, tag, lambda s:
                sum(1 for o in hand_oids(s, pid)
                    if oname(s["objects"][o]) in (FOREST, PLAINS)) >= 2
                and any(oname(s["objects"][o]) in (BEAR, RECLAM)
                        for o in hand_oids(s, pid))):
            return True
    if await do_bottom(c, pid, tag, p0_rank if is_p0 else p1_rank):
        return True
    if await do_discard(c, pid, tag, p0_rank if is_p0 else p1_rank):
        return True
    if await pay_tick(c):
        return True
    # combat: P0 never attacks, blocks Bears with Titans; P1 attacks with
    # all Bears unless holding (LOAD phase).
    if is_p0:
        if await answer_declare(c, pid, tag, block_greedy=True):
            return True
    else:
        if await answer_declare(c, pid, tag,
                                attack_all=not P1MODE["hold"]):
            return True
    # combat damage assignment (attacker's decision; P1's blocked Bears)
    if await answer_assign_damage(c, pid, tag):
        return True
    # legend rule: keep the first via the advertised action
    for a in acts:
        if a.get("type") == "ChooseLegend":
            await submit_as_is(c, copy.deepcopy(a))
            say(f"{c.name} answers ChooseLegend (keep first)")
            wire("legend_answered", {"player": pid})
            return True
    # P0's Esper's target prompt
    if is_p0 and await answer_target_prompt(c, state, st):
        return True
    wt0 = wf_of(state).get("type")
    wplayer = wf_player(state)
    # never pass while P0 has a decision pending
    if is_p0 and wt0 in ("OptionalCostChoice", "TargetSelection",
                         "ManaPayment", "ChooseXValue", "DiscardChoice") \
            and str(wplayer) == "0":
        return False
    # Esper's cast in flight: take no main-phase actions, but still pass
    # priority so the spell can resolve (holding here deadlocks: after
    # casting, the caster gets priority first).
    esper_in_flight = False
    if is_p0 and CAST.get("in_flight"):
        oid = CAST.get("oid")
        o = state["objects"].get(str(oid), {})
        on_stack = o.get("zone") == "Stack"
        if on_stack:
            CAST["stack_seen"] = True
        resolved = CAST.get("stack_seen") and not on_stack
        esper_in_flight = not resolved and not CAST.get("rejected")
    if is_p0 and my_main(state, pid) and not esper_in_flight:
        if ST["stage"] == "SETUP":
            if await p0_setup(c, pid, state, acts):
                return True
        elif ST["stage"] == "CAST":
            if await p0_cast_step(c, pid, state, acts):
                return True
    if not is_p0:
        if await p1_step(c, pid, state, acts):
            return True
    # default: pass priority only when genuinely holding it
    if wt0 == "Priority" and str(wplayer) == str(pid):
        for a in acts:
            if a.get("type") == "PassPriority":
                await submit_as_is(c, a)
                return True
    return False


async def p0_setup(c, pid, state, acts):
    # land drop
    lid = find_hand(state, pid, SWAMP)
    for a in acts:
        if a["type"] == "PlayLand" and lid and str(
                a.get("data", {}).get("object_id")) == lid:
            await submit_as_is(c, a)
            return True
    # cast Grave Titan once 6 untapped lands are available
    if n_untapped_lands(state, 0) >= 6:
        oid = find_hand(state, pid, TITAN)
        a = castspell_advertised(acts, oid)
        if a:
            await submit_as_is(c, a)
            say("P0 casts Grave Titan")
            return True
    return False


async def p0_cast_step(c, pid, state, acts):
    # READY gate checked in the main loop; cast here.
    if CAST.get("in_flight") or CAST.get("done"):
        return False
    if not my_main(state, pid):
        return False
    oid = find_hand(state, pid, ESPER)
    a = castspell_advertised(acts, oid)
    if not oid or not a:
        return False
    if n_untapped_lands(state, 0) < 4 or not untapped_swamp(state, 0):
        return False
    await export_now("pre.json")
    CAST.update({"oid": str(oid), "in_flight": True, "stack_seen": False,
                 "rejected": False, "done": False,
                 "rev_at_submit": c.revision, "wall_at_submit": time.time()})
    await submit_as_is(c, a)
    say(f"P0 casts Esper's to Magicite (oid={oid})")
    wire("esper_cast", {"oid": oid})
    return True


async def p1_step(c, pid, state, acts):
    hold = P1MODE["hold"]
    # land drop (not while holding)
    if not hold:
        lid = find_hand(state, pid, FOREST) or find_hand(state, pid, PLAINS)
        for a in acts:
            if a["type"] == "PlayLand" and lid and str(
                    a.get("data", {}).get("object_id")) == lid:
                await submit_as_is(c, a)
                return True
    # cast Bears (not while holding); one per tick is fine
    if not hold and my_main(state, pid):
        oid = find_hand(state, pid, BEAR)
        a = castspell_advertised(acts, oid)
        if a and n_untapped_lands(state, 1) >= 2:
            await submit_as_is(c, a)
            say("P1 casts Grizzly Bears")
            return True
    return False

# ------------------------------------------------------------------ main

def reset_globals():
    """Reinitialize all per-game state before a new attempt."""
    global C0
    ST.clear()
    ST.update({"stage": "SETUP", "stop": False, "retry": False,
               "cast_turn": None, "target_wait_t0": None})
    CAST.clear()
    P1MODE.clear()
    P1MODE.update({"hold": False})
    MULLS.clear()
    SHAPES.clear()
    WF_SEEN.clear()
    SUBMITTED.clear()
    LAST_SUBMIT.clear()
    LAST_SUBMIT.update({"iid": None})
    TARGET.clear()
    TARGET.update({"prompt_seen": False, "candidates": None, "shape": None,
                   "answered": False, "illegal_tried": False,
                   "illegal_accepted": None, "control_tried": False,
                   "token_oid": None, "seen_at": None, "control_at": None})
    TOKEN.clear()
    TOKEN.update({"seen": False, "oid": None, "obj": None})
    _WATCH.clear()
    _DISCARD_REV.clear()
    C0 = None


async def attempt():
    reset_globals()
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    A = obs["assert"]

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck((ESPER, 4), (TITAN, 12), (SWAMP, 44)))
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck((BEAR, 8), (RECLAM, 8),
                                    (FOREST, 22), (PLAINS, 22)))
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    wire("game", {"code": p0.game_code})

    global C0
    C0 = p0

    last_rev = {}
    force_tick = {}
    last_tick_wall = {}
    TIMEOUT = 2400
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        for c, pid, is_p0 in ((p0, p0.player_id, True),
                              (p1, p1.player_id, False)):
            watch(c)
            rej = drain_rejections(c)
            if rej:
                if is_p0 and CAST.get("in_flight") and \
                        not CAST.get("stack_seen"):
                    CAST["rejected"] = True
                    say("[P0] Esper's cast rejected?!")
                if LAST_SUBMIT["iid"] in SUBMITTED:
                    SUBMITTED.discard(LAST_SUBMIT["iid"])
                    LAST_SUBMIT["iid"] = None
                force_tick[c.name] = True
            if now - last_tick_wall.get(c.name, 0) >= 5:
                force_tick[c.name] = True
            if c.revision == last_rev.get(c.name) \
                    and not force_tick.get(c.name):
                continue
            force_tick[c.name] = False
            last_tick_wall[c.name] = now
            try:
                if await tick(c, pid, is_p0):
                    last_rev[c.name] = c.revision
            except Exception as e:
                say(f"tick error {c.name}: {e}")
        st = p0.latest
        if not st:
            continue
        record_wf(st["state"])
        state = st["state"]

        # periodic setup diagnostics
        _dbg_last = globals().get("_DBG_LAST", 0)
        if ST["stage"] == "SETUP" and now - _dbg_last > 60:
            globals()["_DBG_LAST"] = now
            p1hand = {}
            for oid in hand_oids(state, 1):
                n = oname(state["objects"][oid])
                p1hand[n] = p1hand.get(n, 0) + 1
            p1gy = {}
            for oid in zone_oids(state, 1, "Graveyard"):
                n = oname(state["objects"][oid])
                p1gy[n] = p1gy.get(n, 0) + 1
            p1bf = {}
            for _, o in bf(state, 1):
                n = oname(o)
                p1bf[n] = p1bf.get(n, 0) + 1
            say(f"DBG SETUP turn={state.get('turn')} P1 hand={p1hand} "
                f"gy={p1gy} bf={p1bf} hold={P1MODE['hold']} "
                f"p0bf_titans={sum(1 for _, o in bf(state, 0) if oname(o) == TITAN)}")
            wire("setup_dbg", {"turn": state.get("turn"), "p1hand": p1hand,
                               "p1gy": p1gy, "p1bf": p1bf,
                               "hold": P1MODE["hold"]})

        # --- game ended: finalize if the trigger sequence completed,
        # otherwise flag a retry (e.g. P0 died during setup)
        if wf_of(state).get("type") == "GameOver" and not ST["stop"]:
            if TOKEN["seen"] or TARGET["illegal_accepted"] is not None \
                    or TARGET["control_tried"] or TARGET["prompt_seen"] \
                    or ST["stage"] == "TARGET":
                try:
                    await export_now("post.json")
                except Exception as e:
                    say(f"post export failed: {e}")
                ST["stop"] = True
            else:
                ST["retry"] = True
                ST["stop"] = True
                obs["notes"].append(
                    "game ended before Esper's trigger sequence; retry")
                say("game over before trigger -> retrying with new game")

        # --- SETUP bookkeeping: bears dead -> P1 hold mode
        if ST["stage"] == "SETUP":
            n_bears_gy = len(zone_oids(state, 1, "Graveyard", BEAR))
            n_reclam_gy = len(zone_oids(state, 1, "Graveyard", RECLAM))
            if n_bears_gy >= 2 and not P1MODE["hold"]:
                P1MODE["hold"] = True
                say(f"=== P1 hold mode (bears in gy={n_bears_gy}) ===")
                wire("p1_hold", {"bears_gy": n_bears_gy})
            if n_bears_gy >= 2 and n_reclam_gy >= 1:
                ST["stage"] = "CAST"
                SUBMITTED.clear()
                say(f"=== stage -> CAST (bears={n_bears_gy} "
                    f"reclam={n_reclam_gy}) ===")

        # --- Esper's resolution watch
        if CAST.get("in_flight") and CAST.get("stack_seen"):
            o = state["objects"].get(str(CAST["oid"]), {})
            if o.get("zone") != "Stack":
                CAST["in_flight"] = False
                CAST["done"] = True
                gy = zone_oids(state, 1, "Graveyard")
                ex = zone_oids(state, 1, "Exile")
                wire("esper_resolved", {"p1_gy_remaining": len(gy),
                                       "p1_exile": len(ex),
                                       "exile_names": sorted(
                                           {oname(state["objects"][x])
                                            for x in ex})})
                say(f"Esper's resolved: P1 gy={len(gy)} exile={len(ex)}")
                await export_now("post_exile.json")
                ST["stage"] = "TARGET"
                ST["cast_turn"] = state.get("turn")
                ST["target_wait_t0"] = time.time()
                SUBMITTED.clear()

        # --- token watch (control branch)
        if TARGET["control_tried"] and not TOKEN["seen"]:
            for oid, o in state["objects"].items():
                if o.get("zone") == "Battlefield" and \
                        o.get("controller") == 0 and \
                        oname(o) == BEAR and o.get("is_token"):
                    TOKEN["seen"] = True
                    TOKEN["oid"] = oid
                    TOKEN["obj"] = {k: o.get(k) for k in
                                    ("card_name", "base_name", "name",
                                     "is_token", "card_type", "types",
                                     "subtypes", "power", "toughness")}
                    wire("control_token", TOKEN["obj"])
                    say(f"control token seen: {TOKEN['obj']}")
                    break

        # --- illegal acceptance watch: Reclamation token on P0's BF
        if TARGET["illegal_tried"] and \
                TARGET["illegal_accepted"] is None:
            for oid, o in state["objects"].items():
                if o.get("zone") == "Battlefield" and \
                        o.get("controller") == 0 and \
                        oname(o) == RECLAM and o.get("is_token"):
                    TARGET["illegal_accepted"] = True
                    say("ILLEGAL branch ACCEPTED: Reclamation token on BF!")
                    wire("illegal_token", {"oid": oid})
                    break

        # --- finish: token observed (control) or illegal accepted
        if (TOKEN["seen"] or TARGET["illegal_accepted"]) and \
                not ST["stop"]:
            stack_empty = not any(o.get("zone") == "Stack"
                                  for o in state["objects"].values())
            if stack_empty:
                await asyncio.sleep(2)
                await export_now("post.json")
                ST["stop"] = True
                say("=== DONE ===")

        # --- TARGET stage, trigger never fired: bounded wait, then stop.
        # (v0.80.0 behavior: no TargetSelection ever appears.)
        if ST["stage"] == "TARGET" and not TARGET["prompt_seen"] \
                and not ST["stop"]:
            waited = time.time() - (ST["target_wait_t0"] or time.time())
            turn = state.get("turn")
            cast_turn = ST["cast_turn"]
            turns_later = (turn is not None and cast_turn is not None
                           and turn - cast_turn >= 3)
            if waited > 180 or turns_later:
                say(f"TARGET: no reflexive-trigger prompt after "
                    f"{waited:.0f}s (turn {turn} vs cast {cast_turn}); "
                    f"exporting post and stopping")
                wire("trigger_never_fired",
                     {"waited_s": round(waited, 1), "turn": turn,
                      "cast_turn": cast_turn})
                try:
                    await export_now("post.json")
                except Exception as e:
                    say(f"post export failed: {e}")
                ST["stop"] = True

        # --- target prompt seen but never answerable: give up gracefully
        if TARGET["prompt_seen"] and not TARGET["answered"] \
                and not TARGET["illegal_tried"] and not TARGET["control_tried"] \
                and not ST["stop"] and TARGET.get("seen_at") \
                and time.time() - TARGET["seen_at"] > 120:
            say("target prompt unanswerable after 120s; stopping")
            wire("target_unanswerable", {"candidates": TARGET["candidates"]})
            try:
                await export_now("post.json")
            except Exception as e:
                say(f"post export failed: {e}")
            ST["stop"] = True
        if TARGET.get("control_at") and not TOKEN["seen"] \
                and not ST["stop"] \
                and time.time() - TARGET["control_at"] > 90:
            say("control branch: no token after 90s; exporting post anyway")
            wire("control_timeout", {})
            try:
                await export_now("post.json")
            except Exception as e:
                say(f"post export failed: {e}")
            ST["stop"] = True

    # ------------------------------------------------------- evaluate
    if ST.get("retry"):
        await p0.close()
        await p1.close()
        return obs, False

    def load_state(path):
        env = json.load(open(f"{EVDIR}/{path}"))
        return env["state"]

    try:
        pre = load_state("pre.json")
        A["A1_setup_ok"] = ("passed"
                            if len(zone_oids(pre, 1, "Graveyard", BEAR)) >= 1
                            and len(zone_oids(pre, 1, "Graveyard", RECLAM)) >= 1
                            else "failed")
    except Exception as e:
        A["A1_setup_ok"] = "not-run"
        obs["notes"].append(f"pre.json missing: {e}")
    cands = TARGET["candidates"] or []
    cnames = [c["name"] for c in cands]
    A["A2_creature_offered"] = ("passed" if BEAR in cnames
                                else ("failed" if TARGET["prompt_seen"]
                                      else "not-run"))
    # trigger never fired: the clearly identified related failure (same
    # Oracle clause) — record explicitly for the verdict rule below.
    A["A2b_trigger_prompted"] = ("passed" if TARGET["prompt_seen"]
                                 else "failed")
    A["A3_noncreature_excluded"] = ("passed" if TARGET["prompt_seen"]
                                    and RECLAM not in cnames
                                    else ("failed" if TARGET["prompt_seen"]
                                          and RECLAM in cnames
                                          else "not-run"))
    if RECLAM in cnames:
        A["A4_illegal_enforcement"] = (
            "passed" if TARGET["illegal_accepted"] is False
            else ("failed" if TARGET["illegal_accepted"] is True
                  else "not-run"))
    else:
        A["A4_illegal_enforcement"] = "not-run"
    tok = TOKEN.get("obj") or {}
    tok_types = tok.get("card_type") or {}
    core = tok_types.get("core_types") if isinstance(tok_types, dict) else None
    A["A5_control_token"] = ("passed" if TOKEN["seen"] and core == ["Artifact"]
                             else ("failed" if TARGET["control_tried"]
                                   and not TOKEN["seen"]
                                   else "not-run"))
    try:
        post = load_state("post.json")
        stack_empty = not any(o.get("zone") == "Stack"
                              for o in post["objects"].values())
        A["A6_cleanup"] = ("passed" if stack_empty else "failed")
    except Exception:
        A["A6_cleanup"] = "not-run"

    obs["notes"].append(f"WF sequence: {WF_SEEN}")
    obs["notes"].append(f"candidates: {cnames}")
    obs["notes"].append(f"illegal branch: tried={TARGET['illegal_tried']} "
                        f"accepted={TARGET['illegal_accepted']}")
    obs["notes"].append(f"control token: {tok}")
    obs["notes"].append("protocol-94 driver (v0.98.0): MulliganDecision as "
                        "{choice:{type:Keep}} gated on pending Declare; "
                        "bottom/discard-to-hand-size via single "
                        "SelectCards(data.cards); DeclareAttackers/Blockers "
                        "via relations-schema interaction "
                        "(edges -> sourceId/targetId relations); "
                        "CastSpell/ActivateAbility via advertised actions; "
                        "PayMana* via pay_tick; passes gated on Priority; "
                        "stale-client watchdog.")
    for k in sorted(A):
        say(f"{k}: {A[k]}")

    if A.get("A3_noncreature_excluded") == "failed" \
            or TARGET["illegal_accepted"] is True:
        verdict = "reproduced"
    elif A.get("A2b_trigger_prompted") == "failed":
        # same-clause related failure: Esper's resolves but the reflexive
        # trigger never prompts (matches the published v0.80.0 run).
        verdict = "reproduced"
    elif (A.get("A2_creature_offered") == "passed"
          and A.get("A3_noncreature_excluded") == "passed"
          and A.get("A5_control_token") == "passed"
          and A.get("A6_cleanup") == "passed"):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    run_doc = {
        "issue": 6862,
        "run_id": EVID_RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(time.time() - t0, 1),
        "server_identity": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 94, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(
            f"{BACKFILL}/driver/scenario_6862_0980.py"),
        "decks": {
            "P0": [[ESPER, 4], [TITAN, 12], [SWAMP, 44]],
            "P1": [[BEAR, 8], [RECLAM, 8], [FOREST, 22], [PLAINS, 22]],
        },
        "assertions": A,
        "notes": obs["notes"],
        "wf_sequence": WF_SEEN,
        "target_shape": TARGET["shape"],
        "candidates": cands,
        "illegal_branch": {"tried": TARGET["illegal_tried"],
                           "accepted": TARGET["illegal_accepted"]},
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts >4-of for custom games).",
            "Native AI seats not used; the report's AI involvement concerns the same engine-level target prompt exercised here with human seats.",
            "States are authoritative exports, restorable only via full game replay (scenario_6862_0980.py).",
        ],
        "setup_line": "P0: 4x Esper's to Magicite, 12x Grave Titan, 44x Swamp; P1: 8x Grizzly Bears, 8x Sylvan Reclamation, 22x Forest, 22x Plains; P1 Bears killed by Titan blocks, Reclamation discarded to hand size",
        "contract_line": "Esper's resolves exiling P1 gy (Bears + Reclamation); reflexive trigger must prompt P0 offering exiled creature cards only",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run_doc, f, indent=1)
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "wf_sequence": WF_SEEN,
                   "target_shape": TARGET["shape"],
                   "candidates": cands}, f, indent=2)
    await render_summary(run_doc, f"{EVDIR}/summary.png")
    WIRE.close()
    RUNLOG.close()
    await write_manifest()
    say(f"verdict: {verdict}")
    await p0.close()
    await p1.close()
    return obs, True


async def main():
    """Run attempts until one completes the trigger sequence (or attempts
    are exhausted)."""
    obs = {"assert": {}, "notes": ["no completed attempt"]}
    for n in range(1, 7):
        say(f"===== ATTEMPT {n} =====")
        try:
            obs, done = await attempt()
        except Exception as e:
            say(f"attempt {n} crashed: {e!r}")
            obs, done = {"assert": {},
                         "notes": [f"attempt {n} crash: {e!r}"]}, False
        if done:
            return obs
        say(f"attempt {n} did not complete; starting a new game")
    obs["notes"].append("all attempts exhausted without completing")
    return obs


async def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 760
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #6862 - Esper's to Magicite reflexive trigger "
                    "(revalidation)", fill=(235, 240, 250))
    y += 30
    d.text((24, y),
           f"server v{si['validated_version']} ({si['build_commit']}) "
           f"protocol {si['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180))
    y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced"
                 else (230, 200, 90)))
    y += 34
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "A1 setup: P1 gy has Bear + Reclamation; P0 cast Esper's",
        "A2b_trigger_prompted": "A2b reflexive trigger prompted P0 (TargetSelection)",
        "A2_creature_offered": "A2 exiled Bears offered as token sources",
        "A3_noncreature_excluded": "A3 Sylvan Reclamation NOT offered",
        "A4_illegal_enforcement": "A4 (if offered) Reclamation submission rejected",
        "A5_control_token": "A5 control: Bear choice -> artifact-only Bear token",
        "A6_cleanup": "A6 stack empty after sequence; game proceeds",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y),
               f"{'pass' if v == 'passed' else ('FAIL' if v == 'failed' else 'n/a')} {lab}",
               fill=col)
        y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in run["notes"][:8]:
        d.text((40, y), n[:120], fill=(150, 165, 185))
        y += 20
    d.text((24, H - 30),
           "Evidence: ntindle/phase-bug-state-evidence 6862/" + run["run_id"],
           fill=(120, 130, 150))
    img.save(out_path)


async def write_manifest():
    lines = []
    for name in sorted(os.listdir(EVDIR)):
        if name == "manifest.sha256":
            continue
        p = os.path.join(EVDIR, name)
        if os.path.isfile(p):
            lines.append(f"{sha256_of_file(p)}  {name}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    obs = asyncio.run(main())
    print(json.dumps(obs["assert"], indent=2))
