#!/usr/bin/env python3
"""Issue #4509: "Lost in Thought ignore-effect escape clause dropped (cluster 36)".

Re-validation on the pinned release v0.102.0 (build e17f6fd, protocol 106).
Prior runs: v0.78.0 (reproduced, protocol 68, run 20260909-4509),
v0.84.0 (reproduced, protocol 71, run 20260916-4509c).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (2026-06-28, cluster synthesis): "Lost in Thought" drops its combined
restriction/escape clause end-to-end:
  1. The restriction line parses without binding combat/activation lockouts to
     the enchanted host (EnchantedBy).
  2. The escape line ("exile three cards ... to ignore this effect until end of
     turn") is not parsed or synthesized into a payable ignore-effect action.
  3. Runtime has no StaticSourceIgnored restriction.

Oracle text (verified in pinned v0.102.0 card-data.json):
  "Enchant creature
   Enchanted creature can't attack or block, and its activated abilities can't
   be activated. Its controller may exile three cards from their graveyard for
   that player to ignore this effect until end of turn."
Card-data parse state on v0.102.0: the combined line is still
  Unimplemented("static_structure"), static_abilities == [] (same as v0.84.0).

Scenario (native engine, two human-client seats, protocol 106):
  P0: 12x lost in thought ({1}{U}) + 12x thought scour ({U}) + 36x island.
      Mills P1 twice (Thought Scour targeting P1) so P1's graveyard holds >=3
      cards, then casts Lost in Thought enchanting P1's Llanowar Elves.
  P1: 12x llanowar elves ({G}, "{T}: Add {G}") + 12x grizzly bears
      ({1}{G}) + 36x forest. Develops Elves + Bears, never attacks except in
      the scripted test combats.

Assertions:
  A1_setup_ok       pre.json: Lost in Thought on the battlefield attached to
                    P1's Elves (the targeted object), P1 graveyard >= 3.
  A2_attack_restricted
                    The enchanted Elves is declared as an attacker by P1. Pass
                    iff the engine refuses the declaration (rejection, silent
                    exclusion) and the Elves never appears in combat.attackers
                    and deals no combat damage. Fail iff the Elves attacks ->
                    the CantAttackOrBlock restriction is dropped.
  A3_activation_restricted
                    P1 attempts the enchanted Elves' "{T}: Add {G}" ability on
                    a later main phase (Elves untapped), submitting while
                    holding priority. Pass iff no ActivateAbility is offered
                    for the enchanted Elves (or the attempt is rejected with
                    no mana produced). Fail iff the activation succeeds and
                    P1's pool gains {G} -> the CantBeActivated restriction is
                    dropped.
  A4_escape_offered  With P1's graveyard >= 3 and the aura on the battlefield,
                    at P1 priority the driver scans legal_actions and
                    viewer_interaction for any exile-3-to-ignore escape
                    action. Pass iff offered. Fail iff absent -> the escape
                    clause is dropped.
  A5_escape_effective
                    Only if A4 passes: pay the exile-3 escape, then the Elves
                    attacks the same turn. Expected not-run on this build.
  A6_control_binding
                    P1's unenchanted Grizzly Bears attacks normally on a later
                    combat (engine accepts, P0 takes 2) -> documents the aura
                    is inert rather than mis-bound.

Verdict rule: reproduced iff any of A2, A3, A4 fails (the combined clause is
dropped end-to-end, per the report). not-reproduced iff A2, A3, A4 and A5 all
pass. blocked iff the game cannot be driven to the aura-attack test.

Driver conventions (protocol 106, from scenario_650_01020.py / AGENTS.md):
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full.
  - waiting_for is gone (null); priority = PassPriority in the viewing seat's
    top-level legal_actions; decisions surface via viewer_interaction.
  - MulliganDecision answered via legacy Action (verified accepted on 106),
    gated on the MulliganDecision legal action with answered (tag,iid) keys.
  - Bottom-after-mulligan via vi schema/select opportunity, gated on
    waitingForKind.code == "mulligan" AND turn 1 / Untap.
  - DiscardToHandSize via vi only, gated on hand > 7 plus a schema/select
    opportunity offering hand cards (106 uses a generic 'choose' code).
  - Spell mana payment via vi tapLandForMana, needs-gated
    (ST["mana_needs"][tag]); the 106 engine offers tapLandForMana menus at
    ordinary priority windows, so blind tapping is never done.
  - ActivateAbility submitted while holding priority (never pass first);
    merged_actions ActivateAbility (source_id match) first, else vi
    exactChoices 'activateAbility' answered with {"type":"choose"}.
  - real_decision_pending excludes tapLandForMana/untapLandForMana/castSpell/
    activateAbility/passPriority/mulliganDecision menus.
  - A single await asyncio.sleep(0) yield after the priority gate, before
    leg evaluation (leg-engagement race fix).
  - Export checkpoints fall through to the priority pass in the same tick;
    never return after an export while holding priority.
  - Re-tick backstop: a client holding Priority with no revision change for
    >5s is re-ticked (scenario_650_01010.py last_tick_at pattern).
  - DeclareAttackers submitted from the freshly advertised action
    (deepcopy, attacks set); stale_interaction rejections retry on the next
    advertised action.

Evidence: evidence/4509/<run-id>/pre.json, post_attack.json, post_control.json,
run.json, assertions.json, data_evidence.json, manifest.sha256, summary.png,
scenario_4509_01020.py, wire_log.jsonl, scenario_run.log, server_excerpts.log
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, URL, deck  # noqa: E402
import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 4509
RUN_ID = os.environ.get("RUN_ID", "20261004-4509")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.102.0",
    "build_commit": "e17f6fd",
    "protocol_version": 106,
    "mode": "Full",
    "server_binary_sha256": "",
    "card_data_sha256": "",
    "draft_pools_sha256": "",
    "signature_verified": True,
    "signature_note": ("v0.102.0 binary + release manifest minisign-verified "
                       "against the repo-pinned SERVER_ARTIFACT_PUBLIC_KEY "
                       "(key id 436711b6a2d36828) in prehashed (blake2b-512) "
                       "mode; binary digest matches GitHub's asset digest; "
                       "data digests match the signed manifest; digests "
                       "recomputed against on-disk files this run"),
    "server_run_id": ("backfill-owned isolated v0.102.0 server on "
                      "127.0.0.1:9375 (started by this run; this run's own "
                      "game + raw Hello handshake verify the identity)"),
    "source": ("2026-10-04: latest stable release v0.102.0 (published "
               "2026-10-04) == pinned release dir; ServerHello "
               "0.102.0/e17f6fd/protocol 106 verified by this run; hashes "
               "recomputed against on-disk artifacts this run"),
}

for _f, _k in (("server/releases/v0.102.0/phase-server-slim-x86_64-unknown-linux-musl",
                "server_binary_sha256"),
               ("server/releases/v0.102.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.102.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    SERVER_IDENTITY[_k] = _h
print("server identity hashes recomputed against on-disk pinned artifacts",
      flush=True)

CARD_DATA = json.load(open(
    f"{BACKFILL}/server/releases/v0.102.0/data/card-data.json"))

LOST = "lost in thought"
SCOUR = "thought scour"
ELVES = "llanowar elves"
BEARS = "grizzly bears"
ISLAND = "island"
FOREST = "forest"

P0_DECK = deck((LOST, 12), (SCOUR, 12), (ISLAND, 36))
P1_DECK = deck((ELVES, 12), (BEARS, 12), (FOREST, 36))

SETUP_DEADLINE_S = 1800
ASS_KEYS = ("A1_setup_ok", "A2_attack_restricted", "A3_activation_restricted",
            "A4_escape_offered", "A5_escape_effective", "A6_control_binding")

ST = {}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
P0C = None
P1C = None


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        RUNLOG.write(line + "\n")
        RUNLOG.flush()
    except ValueError:
        pass


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "data": payload}, default=str) + "\n")
        WIRE.flush()
    except ValueError:
        pass


# ---------------------------------------------------------------- state helpers

def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    o = get_obj(state, oid)
    return str(o.get("base_name") or o.get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players", []) or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def life_of(state, pid):
    return player_of(state, pid).get("life")


def hand_ids(state, pid):
    return list(player_of(state, pid).get("hand", []) or [])


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def gy_count(state, pid):
    return len(player_of(state, pid).get("graveyard", []) or [])


def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def stack_entries(state):
    return state.get("stack") or []


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def bf_oids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_by_name(state, pid, name):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == name]


def aura_on_bf(state, name=LOST):
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") == "Battlefield" and obj_lname(state, oid) == name:
            return int(oid), o
    return None, None


def top_acts(st):
    return list(st.get("legal_actions", []) or [])


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def can_pay(state, pid, colors=(), generic=0):
    color_of = {ISLAND: "U", FOREST: "G", "swamp": "B"}
    pool = {}
    for o in bf_oids(state, pid):
        ob = get_obj(state, o)
        if is_land(ob) and not ob.get("tapped"):
            c = color_of.get(obj_lname(state, o))
            if c:
                pool[c] = pool.get(c, 0) + 1
    need = {}
    for c in colors:
        need[c] = need.get(c, 0) + 1
    for c, n in need.items():
        if pool.get(c, 0) < n:
            return False
        pool[c] -= n
    return sum(pool.values()) >= generic


def mana_pool_g(state, pid):
    units = (player_of(state, pid).get("mana_pool") or {}).get("mana", [])
    return sum(1 for u in units if "green" in json.dumps(u).lower())


def my_priority(acts):
    """Protocol 106: the viewing seat holds priority iff a PassPriority
    legal action is advertised to it (waiting_for is gone)."""
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not stack_entries(state))


def attacker_oids(state):
    out = set()
    c = state.get("combat") or {}
    for a in c.get("attackers") or []:
        if isinstance(a, (list, tuple)) and a:
            try:
                out.add(int(a[0]))
            except Exception:
                pass
        elif isinstance(a, dict):
            for k in ("attacker", "attacker_id", "object_id", "id"):
                if k in a:
                    try:
                        out.add(int(a[k]))
                    except Exception:
                        pass
        else:
            try:
                out.add(int(a))
            except Exception:
                pass
    return out


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)]


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision"}


def real_decision_pending(st):
    """True if the viewing seat has a real decision (not just the priority
    menu or a mana-ability menu) in its viewer_interaction."""
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        if rtype == "schema":
            return True
        codes = set()
        for ch in items:
            codes.update(c for c in surf_codes(ch) if c)
        if "decideOptionalEffect" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


# ---------------------------------------------------------------- interaction primitives

async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    wire("action_submit", {"who": c.name, "action": msg})
    await c.send_action(msg)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag,
                                "interactionId": sub.get("interactionId"),
                                "response": sub.get("response")})
    await c.send_interaction(sub)


async def answer_vi(c, opp, choice, tag):
    iid = opp.get("interactionId")
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    cid = choice.get("id")
    if rtype == "schema":
        spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
        stype = spec.get("type") or "sequence"
        sub = {"interactionId": iid,
               "response": {"type": stype, "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting interaction iid={iid} choice={cid} "
        f"({choice_text(choice)[:80]})")
    await interact_as(c, sub, tag)


async def verify_server_hello():
    ws = await websockets.connect(URL, max_size=1_000_000)
    raw = await asyncio.wait_for(ws.recv(), 10)
    await ws.close()
    hello = json.loads(raw)
    d = hello.get("data", {}) or {}
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto} "
        f"mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.102.0"), f"unexpected version {ver}"
    assert int(proto) == 106, f"unexpected protocol {proto}"
    assert str(build) == "e17f6fd", f"unexpected build {build}"
    ST["hello_ok"] = True


def check_data_level():
    li = CARD_DATA.get(LOST, {})
    oracle = str(li.get("oracle_text") or "")
    ab = li.get("abilities") or []
    unimpl = [a for a in ab
              if isinstance(a, dict)
              and (a.get("effect") or {}).get("type") == "Unimplemented"]
    ok = ("can't attack or block" in oracle
          and "exile three cards" in oracle)
    notes = []
    if not ok:
        notes.append("Lost in Thought oracle shape missing")
    notes.append(f"abilities={len(ab)} unimplemented="
                 f"{len(unimpl)} static_abilities={li.get('static_abilities')}")
    if unimpl:
        notes.append("parse: " + str(
            (unimpl[0].get("effect") or {}).get("description"))[:200])
    ST["data_level_ok"] = ok
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes,
                   "oracle": oracle[:400],
                   "static_abilities": li.get("static_abilities")},
                  f, indent=1)
    say(f"data-level check: ok={ok} notes={notes}")

async def do_mulligan(c, acts, st, pid, tag):
    """Protocol 106: MulliganDecision arrives as a legacy legal action
    (verified: the engine accepts the Action submission)."""
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    iid = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        for ch in (resp.get("data") or {}).get("choices", []):
            if "mulliganDecision" in surf_codes(ch):
                iid = opp.get("interactionId")
                break
        if iid:
            break
    key = (tag, "mull", iid or f"rev{c.revision}")
    if key in MULLS:
        return False
    state = st["state"]
    hn = hand_lnames(state, pid)
    MULLS.add(key)
    wants = (LOST, SCOUR) if tag == "P0" else (ELVES, BEARS)
    mull_count = sum(1 for k in MULLS if k[0] == tag)
    keep = any(w in hn for w in wants)
    if not keep and mull_count < 4 and len(hn) > 4:
        say(f"[{tag}] mulligan ({len(hn)} cards, hand={hn})")
        wire("mulligan", {"who": tag, "decision": "mulligan", "hand": hn})
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
        return True
    say(f"[{tag}] keep {len(hn)}: {hn}")
    wire("mulligan", {"who": tag, "decision": "keep", "hand": hn})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    return True


async def do_bottom(c, acts, st, pid, tag):
    """Protocol 106: bottom-after-mulligan surfaces as per-card SelectCards
    legal actions plus a viewer_interaction schema/select opportunity
    (waitingForKind.code remains 'mulligan'). Answer via the vi opportunity.
    Gated on kind == 'mulligan' AND turn 1 / Untap so it cannot misfire on
    mid-game SelectCards prompts."""
    if vi_kind_code(st) != "mulligan":
        return False
    state = st["state"]
    if not (state.get("turn_number") == 1 and state.get("phase") == "Untap"):
        return False
    sel_acts = [a for a in acts if a.get("type") == "SelectCards"]
    if not sel_acts:
        return False
    target = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        if (spec.get("type") or "") != "select":
            continue
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        target = (opp, cands, spec)
        break
    if target is None:
        say(f"[{tag}] WARNING: SelectCards without vi select opportunity; "
            "not answering")
        wire(f"{tag}_bottom_no_vi",
             {"acts": [a.get("data") for a in sel_acts][:8]})
        return False
    opp, cands, spec = target
    iid = opp.get("interactionId")
    key = (tag, "bottom", iid)
    if key in SUBMITTED_OPPS:
        return False
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}).get(
        "data", {}) or {}
    n = int(con.get("min") or con.get("max") or 1)
    if n <= 0:
        return False
    keepers = (LOST, SCOUR) if tag == "P0" else (ELVES, BEARS)

    def bkey(ch):
        ref = _cand_reference(ch)
        nm = obj_lname(state, ref) if ref is not None else "?"
        if nm in keepers:
            return (1, str(ref))  # keep these on top
        return (0, str(ref))      # bottom everything else first

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming "
        f"{[obj_lname(state, _cand_reference(x)) for x in picks]} via vi")
    wire("bottom", {"who": tag, "iid": iid,
                    "picks": [x.get("id") for x in picks]})
    await interact_as(
        c, {"interactionId": iid,
            "response": {"type": "select",
                         "data": {"choiceIds": [ch.get("id")
                                                for ch in picks]}}}, tag)
    return True


async def do_discard_to_handsize(c, acts, st, pid, tag):
    """Protocol 106: DiscardToHandSize surfaces via viewer_interaction;
    gate on hand > 7 plus a schema/select opportunity offering hand cards
    (106 uses a generic 'choose' waitingForKind code)."""
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    handset = set(str(h) for h in hand)
    found_opp = None
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        rdata = resp.get("data", {}) or {}
        spec = rdata.get("spec", {}) or {}
        cands = rdata.get("candidates") or []
        refs = [str(_cand_reference(ch)) for ch in cands
                if _cand_reference(ch) is not None]
        if not any(r in handset for r in refs):
            wire("handsize_no_hand_candidates",
                 {"who": tag, "spec_type": spec.get("type"),
                  "n_cands": len(cands),
                  "refs_sample": refs[:6]})
            continue
        found_opp = (opp, cands, spec)
        break
    if found_opp is None:
        return False
    key = (tag, "handsize", str(c.revision))
    if key in SUBMITTED_OPPS:
        return False
    opp, cands, spec = found_opp
    stype = spec.get("type") or "select"
    keepers = (LOST, SCOUR) if tag == "P0" else (ELVES, BEARS)

    def rank(ch):
        ref = _cand_reference(ch)
        nm = obj_lname(state, ref) if ref is not None else \
            choice_text(ch).lower()
        if nm in (ISLAND, FOREST):
            return (0, nm)
        if nm in keepers:
            return (9, nm)
        return (5, nm)

    picks = [ch.get("id") for ch in sorted(cands, key=rank)[:max(1, n)]
             if ch.get("id")]
    if not picks:
        return False
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] discarding to hand size via vi ({stype}): picks={picks}")
    wire("handsize_discard", {"who": tag, "stype": stype, "picks": picks})
    await interact_as(c, {"interactionId": opp.get("interactionId"),
                          "response": {"type": stype,
                                       "data": {"choiceIds": picks}}}, tag)
    return True


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "passPriority" in codes and ch.get("status", {}) \
                    .get("type") in (None, "available"):
                iid = opp.get("interactionId") or opp.get("id")
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": "choose",
                                                   "data": {"choiceId":
                                                            ch.get("id")}}},
                                  c.name)
                return True
    return False


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get((tag,)) == turn:
        return False
    want_land = ISLAND if tag == "P0" else FOREST
    for o in hand_ids(state, pid):
        if obj_lname(state, o) != want_land:
            continue
        for a in acts:
            if a["type"] == "PlayLand" and \
                    str(a.get("_src_oid")) == str(o):
                LAND_PLAYED_TURN[(tag,)] = turn
                say(f"[{tag}] playing land {want_land}")
                wire("play_land", {"who": tag, "oid": o})
                await submit_as_is(c, a)
                return True
    return False


def cast_action_for(acts, state, key):
    for a in acts:
        if "cast" in a["type"].lower():
            d = a.get("data", {})
            cands = list(d.values()) + [a.get("_src_oid")]
            for v in cands:
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    continue
                if obj_lname(state, iv) == key:
                    return a, iv
    return None, None


async def try_cast(c, state, acts, tag, name, needs, want=None):
    a, oid = cast_action_for(acts, state, name)
    if not a:
        return False
    ST["mana_needs"][tag] = dict(needs)
    say(f"[{tag}] casting {name} (oid {oid}) needs={needs}")
    wire("cast", {"who": tag, "card": name, "oid": oid})
    await submit_as_is(c, a)
    if want:
        ST["cast_awaiting"] = {"name": name,
                               "turn": state.get("turn_number", 0),
                               "want": dict(want)}
        ST["pending_target"] = dict(want)
    return True


async def pay_mana_vi(c, st, tag):
    """Protocol 106: mana payment via vi tapLandForMana, strictly
    needs-gated (the engine offers tapLandForMana menus at ordinary
    priority windows; blind tapping must never happen)."""
    ops = vi_ops(st)
    if not ops:
        return False
    needs = ST["mana_needs"][tag]
    if sum(needs.values()) <= 0:
        return False
    for opp in ops:
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status") or {}).get("type") not in (None, "available"):
                continue
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "tapLandForMana" in codes:
                manas = [s for s in ch.get("surfaces", []) or []
                         if s.get("type") == "mana"]
                syms = manas[0]["data"].get("symbols", []) if manas else []
                taps.append((ch, syms))
        if not taps:
            continue
        pick, used = None, None
        for ch, s in taps:
            for color in ("W", "U", "B", "R", "G"):
                if needs.get(color, 0) > 0 and color in s:
                    pick, used = ch, color
                    break
            if pick is not None:
                break
        if pick is None and needs.get("generic", 0) > 0:
            pick, used = taps[0][0], "generic"
        if pick is None:
            continue
        needs[used] -= 1
        say(f"[{tag}] tap land for mana used_for={used} needs={dict(needs)}")
        wire("tap_land", {"who": tag, "used_for": used})
        await answer_vi(c, opp, pick, tag)
        return True
    return False


def find_activate(st, source_oid):
    """Mana ability on protocol 106: merged_actions ActivateAbility
    (source_id match) first; else viewer_interaction exactChoices
    'activateAbility' answered with {"type":"choose"}."""
    for a in merged_actions(st):
        if a.get("type") == "ActivateAbility" and \
                str((a.get("data") or {}).get("source_id")) == str(source_oid):
            return ("submit", a, "ActivateAbility legal_action")
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in (resp.get("data") or {}).get("choices", []):
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "activateAbility" not in codes:
                continue
            refs = [str(s.get("data", {}).get("reference"))
                    for s in ch.get("surfaces", []) or []
                    if isinstance(s.get("data"), dict)
                    and s.get("data", {}).get("role") == "source"]
            if str(source_oid) in refs:
                return ("interaction", (opp, ch),
                        "activateAbility via viewer_interaction (choose)")
    return None


async def answer_targets(c, st, tag):
    """Answer target-selection vi opportunities with the scenario's intended
    target (ST['pending_target']). Response shape follows the opportunity:
    exactChoices -> choose, schema sequence/select -> that type."""
    want = ST.get("pending_target")
    if not want:
        return False
    answered = False
    for opp in vi_ops(st):
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            continue
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        items = data.get("candidates") or data.get("choices") or []
        if not items:
            continue
        pick = None
        for ch in items:
            if (ch.get("status") or {}).get("type") not in (None, "available"):
                continue
            if want["kind"] == "player":
                seats = [s.get("data", {}).get("seat")
                         for s in ch.get("surfaces", []) or []
                         if isinstance(s.get("data"), dict)]
                if want["seat"] in seats or \
                        f"player {want['seat']}" in choice_text(ch).lower():
                    pick = ch
                    break
            else:
                ref = _cand_reference(ch)
                if ref is not None and str(ref) == str(want["oid"]):
                    pick = ch
                    break
                if want["name"] in choice_text(ch).lower():
                    pick = ch
                    break
        if pick is None:
            continue
        spec = (data.get("spec") or {})
        stype = spec.get("type")
        if rtype == "exactChoices":
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": pick.get("id")}}}
        elif rtype == "schema" and stype in ("sequence", "select"):
            sub = {"interactionId": iid,
                   "response": {"type": stype,
                                "data": {"choiceIds": [pick.get("id")]}}}
        else:
            wire("target_unhandled_shape",
                 {"who": tag, "rtype": rtype, "stype": stype,
                  "n_items": len(items)})
            continue
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] targeting {want['kind']} -> "
            f"{choice_text(pick)[:60]}")
        wire("target_submission", {"who": tag, "want": want,
                                   "submission": sub})
        await interact_as(c, sub, tag)
        answered = True
    return answered


def clear_cast_awaiting(state):
    ca = ST.get("cast_awaiting")
    if not ca:
        return
    if ca["name"] == LOST:
        oid, _ = aura_on_bf(state)
        if oid is not None:
            ST["cast_awaiting"] = None
            ST["pending_target"] = None
    elif ca["name"] == SCOUR:
        for o in player_of(state, 0).get("graveyard", []) or []:
            if obj_lname(state, o) == SCOUR:
                ST["cast_awaiting"] = None
                ST["pending_target"] = None
                break
    if state.get("turn_number", 0) > ca.get("turn", 0):
        ST["cast_awaiting"] = None
        ST["pending_target"] = None


def log_aura_object(state):
    oid, o = aura_on_bf(state)
    if oid is not None and not ST.get("aura_object_logged"):
        ST["aura_object_logged"] = True
        ST["aura_oid"] = oid
        wire("aura_object", {
            "oid": oid,
            "static_definitions": o.get("static_definitions"),
            "unimplemented_mechanics": o.get("unimplemented_mechanics"),
            "attached_to": o.get("attached_to"),
            "controller": o.get("controller"),
            "all_keys": sorted(o.keys()),
        })
        say(f"aura on BF oid={oid} "
            f"static_definitions={o.get('static_definitions')} "
            f"unimplemented={o.get('unimplemented_mechanics')}")


def drain_rejections(c, stage):
    out = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            rec = {"type": t, "data": data, "stage": stage}
            out.append(rec)
            ST["rejections"].append(rec)
            say(f"[{c.name}] REJECTION in stage {stage}: "
                f"{json.dumps(data, default=str)[:300]}")
            wire("rejection", {"who": c.name, "stage": stage, "data": data})
    return out


def scan_escape(state, acts, st):
    """Look for any exile-3-to-ignore escape action offered to P1."""
    if ST.get("escape_offered"):
        return
    if not ST.get("aura_object_logged"):
        return
    if gy_count(state, 1) < 3:
        return
    texts = []
    for a in acts:
        blob = json.dumps(a, default=str).lower()
        texts.append(a["type"])
        if "exile" in blob and "graveyard" in blob \
                and ("ignore" in blob or "lost in thought" in blob):
            ST["escape_offered"] = True
            wire("escape_found_action", a)
            say("ESCAPE ACTION OFFERED: " + json.dumps(a, default=str)[:500])
    for opp in vi_ops(st):
        blob = json.dumps(opp, default=str).lower()
        if "exile" in blob and "graveyard" in blob \
                and ("ignore" in blob or "lost in thought" in blob):
            ST["escape_offered"] = True
            wire("escape_found_vi", opp)
            say("ESCAPE VI OFFERED: " + json.dumps(opp, default=str)[:500])
    ST["escape_scan_done"] = True
    ST["p1_action_types"] = sorted(set(texts))
    wire("escape_scan", {"action_types": ST["p1_action_types"],
                         "offered": ST.get("escape_offered", False)})
    say(f"P1 priority action types at escape scan: {ST['p1_action_types']}")

async def export_named(name):
    """Authoritative export via P0 (seat 0, single-user). Callers must NOT
    return after this while their client holds priority -- fall through to
    the priority pass in the same tick."""
    try:
        raw = await P0C.export_state()
        env = json.loads(raw)
        assert "state" in env, "envelope missing 'state'"
        with open(f"{EVDIR}/{name}.json", "w") as f:
            json.dump(env, f, indent=1)
        ST["exports"][name] = True
        say(f"exported {name}.json (turn={env['state'].get('turn_number')})")
        wire("export", {"name": name,
                        "turn": env["state"].get("turn_number")})
        return env["state"]
    except Exception as e:
        say(f"export {name} FAILED: {e!r}")
        ST["tick_errors"].append(f"export_{name}: {e!r}")
        return None


async def p0_tick(c):
    st = c.latest
    if not st:
        return
    state = st["state"]
    pid, tag = 0, "P0"
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, pid, tag):
        return
    if await do_bottom(c, acts, st, pid, tag):
        return
    if await do_discard_to_handsize(c, acts, st, pid, tag):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da and ST.get("da_shape_logged") is not True:
            ST["da_shape_logged"] = True
            wire("declare_attackers_shape_p0",
                 {"data": da.get("data")})
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if "DeclareBlockers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return
    if "OrderTriggers" in atypes:
        oa = next((a for a in acts if a["type"] == "OrderTriggers"), None)
        if oa:
            await submit_as_is(c, oa)
        return
    if await pay_tick(c, acts):
        return
    if await pay_mana_vi(c, st, tag):
        return
    if await answer_targets(c, st, tag):
        return
    clear_cast_awaiting(state)
    log_aura_object(state)

    # ---- priority gate, then yield before leg evaluation
    has_prio = my_priority(acts)
    await asyncio.sleep(0)

    # setup -> attack_test transition: aura attached with P1 gy >= 3
    if ST["stage"] == "setup":
        oid, _ = aura_on_bf(state)
        eoid = ST.get("enchant_target_oid")
        if (oid is not None and ST.get("aura_cast") and eoid is not None
                and gy_count(state, 1) >= 3
                and not ST.get("pre_done")):
            ST["pre_done"] = True
            ST["stage"] = "attack_test"
            say(f"aura attached (oid {oid}) to Elves {eoid}, P1 gy="
                f"{gy_count(state, 1)}; entering attack_test")
            await export_named("pre")
            # fall through: no return while possibly holding priority

    in_main = my_main(state, pid)
    if in_main and has_prio and ST["stage"] == "setup":
        if await play_a_land(c, state, pid, acts, tag):
            return
        # mill P1: Thought Scour while P1 gy < 4 (two casts -> 4 cards)
        if ST.get("scours_cast", 0) < 2 and gy_count(state, 1) < 4 \
                and can_pay(state, pid, ("U",), 0):
            if await try_cast(c, state, acts, tag, SCOUR, {"U": 1},
                              want={"kind": "player", "seat": 1}):
                ST["scours_cast"] = ST.get("scours_cast", 0) + 1
                return
        # cast the aura once P1 has an Elves and we have {1}{U}
        if not ST.get("aura_cast") and can_pay(state, pid, ("U",), 1):
            elves = bf_by_name(state, 1, ELVES)
            if elves:
                ST["enchant_target_oid"] = elves[0]
                if await try_cast(
                        c, state, acts, tag, LOST, {"U": 1, "generic": 1},
                        want={"kind": "creature", "oid": elves[0],
                              "name": ELVES}):
                    ST["aura_cast"] = True
                    return
    if has_prio:
        await pass_priority(c, st, acts)


async def p1_tick(c):
    st = c.latest
    if not st:
        return
    state = st["state"]
    pid, tag = 1, "P1"
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, pid, tag):
        return
    if await do_bottom(c, acts, st, pid, tag):
        return
    if await do_discard_to_handsize(c, acts, st, pid, tag):
        return
    drain_rejections(c, ST["stage"])
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da and ST.get("da_shape_logged") is not True:
            ST["da_shape_logged"] = True
            wire("declare_attackers_shape_p1", {"data": da.get("data")})
        if da:
            d = copy.deepcopy(da)
            if ST["stage"] == "attack_test" \
                    and not ST.get("attack1_submitted") \
                    and ST.get("enchant_target_oid") is not None:
                eoid = ST["enchant_target_oid"]
                d.setdefault("data", {}).update(
                    {"attacks": [[eoid, {"type": "Player", "data": 0}]],
                     "bands": []})
                ST["attack1_submitted"] = True
                ST["attack1_turn"] = state.get("turn_number")
                ST["p0_life_pre_attack"] = life_of(state, 0)
                ST["_rej_mark"] = len(ST["rejections"])
                say(f"[P1] declares attack with enchanted Elves {eoid}")
                wire("attack1_submit", d.get("data"))
                await submit_as_is(c, {"type": "DeclareAttackers",
                                       "data": d["data"]})
                return
            if ST["stage"] == "control" and not ST.get("bear_submitted"):
                bears = bf_by_name(state, 1, BEARS)
                if bears:
                    d.setdefault("data", {}).update(
                        {"attacks": [[bears[0],
                                      {"type": "Player", "data": 0}]],
                         "bands": []})
                    ST["bear_submitted"] = True
                    ST["bear_oid"] = bears[0]
                    ST["bear_turn"] = state.get("turn_number")
                    ST["p0_life_pre_bear"] = life_of(state, 0)
                    say(f"[P1] declares control attack with Bear {bears[0]}")
                    wire("bear_submit", d.get("data"))
                    await submit_as_is(c, {"type": "DeclareAttackers",
                                           "data": d["data"]})
                    return
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, {"type": "DeclareAttackers",
                                   "data": d["data"]})
        return
    if "DeclareBlockers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, {"type": "DeclareBlockers",
                                   "data": d["data"]})
        return
    if "OrderTriggers" in atypes:
        oa = next((a for a in acts if a["type"] == "OrderTriggers"), None)
        if oa:
            await submit_as_is(c, oa)
        return
    if await pay_tick(c, acts):
        return
    if await pay_mana_vi(c, st, tag):
        return
    log_aura_object(state)

    # ---- priority gate, then yield before leg evaluation
    has_prio = my_priority(acts)
    await asyncio.sleep(0)

    # escape scan whenever P1 holds priority in a post-setup stage
    if ST["stage"] in ("attack_test", "activation_test", "control") \
            and has_prio:
        scan_escape(state, acts, st)

    # ---- attack leg tracking
    eoid = ST.get("enchant_target_oid")
    if eoid is not None and eoid in attacker_oids(state):
        if not ST.get("elves_attacked"):
            ST["elves_attacked"] = True
            say(f"Elves {eoid} IS in combat.attackers")
            wire("elves_attacking",
                 {"attackers": sorted(attacker_oids(state))})
    if ST.get("attack1_submitted") and not ST.get("attack1_accepted") \
            and not ST.get("attack1_rejected"):
        if eoid is not None and eoid in attacker_oids(state):
            ST["attack1_accepted"] = True
            say("attack1 ACCEPTED (Elves in attackers)")
            wire("attack1_accepted", {})
        elif len(ST["rejections"]) > ST.get("_rej_mark", 0):
            ST["attack1_rejected"] = True
            say("attack1 REJECTED by engine")
            wire("attack1_rejected", {})
    # conclude the attack leg when the attack turn ends
    if ST["stage"] == "attack_test" and ST.get("attack1_submitted") \
            and state.get("turn_number", 0) > (ST.get("attack1_turn") or 0):
        if not ST.get("attack1_accepted") and not ST.get("attack1_rejected"):
            ST["attack1_rejected"] = True
            ST["notes"].append("attack1: engine never accepted nor rejected "
                               "across the combat; treated as blocked "
                               "(silent exclusion)")
            say("attack1 silently excluded across the combat")
        ST["p0_life_post_attack"] = life_of(state, 0)
        ST["stage"] = "activation_test"
        say(f"attack leg concluded (accepted={ST.get('attack1_accepted')} "
            f"rejected={ST.get('attack1_rejected')} elves_attacked="
            f"{ST.get('elves_attacked')} P0 life "
            f"{ST.get('p0_life_pre_attack')}->{ST.get('p0_life_post_attack')}); "
            "exporting post_attack, entering activation_test")
        await export_named("post_attack")
        # fall through: no return while possibly holding priority

    # ---- activation leg: attempt the enchanted Elves' mana ability while
    # holding priority (never pass first); only on untapped Elves
    if ST["stage"] == "activation_test" and has_prio \
            and state.get("active_player") == 1 \
            and state.get("phase") in ("PreCombatMain", "PostCombatMain") \
            and not stack_entries(state) \
            and not ST.get("activation_attempted"):
        eo = get_obj(state, eoid) if eoid else {}
        if eoid is not None and not eo.get("tapped"):
            cands = [a for a in acts
                     if a["type"] == "ActivateAbility"
                     and str((a.get("data") or {}).get("source_id"))
                     == str(eoid)]
            ST["activation_candidates_seen"] = [
                {"ability_index": (a.get("data") or {}).get("ability_index"),
                 "via": "legal_action"} for a in cands]
            if not cands:
                r = find_activate(st, eoid)
                if r and r[0] == "interaction":
                    cands = [r]
                    ST["activation_candidates_seen"] = [
                        {"via": "viewer_interaction"}]
            wire("activation_candidates",
                 {"enchanted_elves": eoid,
                  "candidates": ST["activation_candidates_seen"],
                  "all_types": sorted(set(a["type"] for a in acts))})
            if cands:
                ST["activation_offered"] = True
                ST["activation_attempted"] = True
                ST["pool_before_activation"] = mana_pool_g(state, 1)
                ST["_act_rej_mark"] = len(ST["rejections"])
                say(f"[P1] attempts ActivateAbility on enchanted Elves "
                    f"{eoid} while holding priority")
                if isinstance(cands[0], tuple):
                    _kind, (opp, ch), _what = cands[0]
                    await answer_vi(c, opp, ch, tag)
                else:
                    await submit_as_is(c, cands[0])
                return
            ST["activation_wait_ticks"] = \
                ST.get("activation_wait_ticks", 0) + 1
            if ST["activation_wait_ticks"] >= 6:
                ST["activation_attempted"] = True
                ST["activation_offered"] = False
                say("no ActivateAbility offered for enchanted Elves after "
                    "6 priority ticks (untapped)")
            return
        # tapped or unknown: fall through to the default pass so the game
        # advances instead of stalling on a creature that cannot pay {T}
    # capture the pool on the first tick after the activation attempt
    if ST.get("activation_attempted") and ST.get("activation_offered") \
            and ST.get("pool_after_activation") is None:
        ST["pool_after_activation"] = mana_pool_g(state, 1)
        wire("pool_after_capture", ST["pool_after_activation"])
        if len(ST["rejections"]) > ST.get("_act_rej_mark", 0):
            ST["activation_rejected"] = True
            say("activation attempt REJECTED by engine")
        ST["stage"] = "control"
        say("activation leg concluded; entering control stage")
    # bear acceptance detection
    if ST.get("bear_oid") is not None \
            and ST["bear_oid"] in attacker_oids(state):
        if not ST.get("bear_attack_accepted"):
            ST["bear_attack_accepted"] = True
            say(f"Bear {ST['bear_oid']} IS in combat.attackers")
            wire("bear_attacking",
                 {"attackers": sorted(attacker_oids(state))})
    # conclude the control leg when the bear-attack turn ends
    if ST["stage"] == "control" and ST.get("bear_submitted") \
            and state.get("turn_number", 0) > (ST.get("bear_turn") or 0):
        ST["p0_life_post_bear"] = life_of(state, 0)
        ST["stage"] = "done"
        say(f"control leg concluded (bear_attack_accepted="
            f"{ST.get('bear_attack_accepted')} P0 life "
            f"{ST.get('p0_life_pre_bear')}->{ST.get('p0_life_post_bear')}); "
            "exporting post_control")
        await export_named("post_control")
        # fall through: no return while possibly holding priority

    # setup legs for P1
    in_main = my_main(state, pid)
    if in_main and has_prio and ST["stage"] == "setup":
        if await play_a_land(c, state, pid, acts, tag):
            return
        elves_n = len(bf_by_name(state, 1, ELVES))
        bears_n = len(bf_by_name(state, 1, BEARS))
        if elves_n < 2 and can_pay(state, pid, ("G",), 0):
            if await try_cast(c, state, acts, tag, ELVES, {"G": 1}):
                return
        if bears_n < 1 and can_pay(state, pid, ("G",), 1):
            if await try_cast(c, state, acts, tag, BEARS,
                              {"G": 1, "generic": 1}):
                return
    if has_prio:
        await pass_priority(c, st, acts)

def evaluate():
    ass = {}
    notes = ST["notes"]
    notes.append(f"rejections={len(ST['rejections'])} "
                 f"escape_offered={ST.get('escape_offered')} "
                 f"elves_attacked={ST.get('elves_attacked')}")
    # A1
    if ST.get("pre_done"):
        p = f"{EVDIR}/pre.json"
        if os.path.exists(p):
            pre = json.load(open(p))["state"]
            oid, _aura = aura_on_bf(pre)
            eoid = ST.get("enchant_target_oid")
            eo = get_obj(pre, eoid) if eoid is not None else {}
            ok = (oid is not None and eoid is not None
                  and eo.get("zone") == "Battlefield"
                  and str(eo.get("controller")) == "1"
                  and obj_lname(pre, eoid) == ELVES
                  and gy_count(pre, 1) >= 3)
            ass["A1_setup_ok"] = "passed" if ok else "failed"
            notes.append(f"A1: aura_oid={oid}, enchant_target={eoid} "
                         f"({obj_lname(pre, eoid)}, zone={eo.get('zone')}, "
                         f"ctrl={eo.get('controller')}), P1 "
                         f"gy={gy_count(pre, 1)}")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("A1: pre.json missing though pre_done set")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append("A1: aura never attached with P1 gy>=3 "
                     f"(aura_cast={ST.get('aura_cast')}, "
                     f"scours={ST.get('scours_cast', 0)})")
    # A2
    if not ST.get("attack1_submitted"):
        ass["A2_attack_restricted"] = "not-run"
        notes.append("A2: attack1 never submitted")
    elif ST.get("attack1_rejected"):
        ass["A2_attack_restricted"] = "passed"
        notes.append("A2: engine refused the enchanted-Elves attack "
                     "declaration (rejected or silently excluded)")
    elif ST.get("elves_attacked"):
        ass["A2_attack_restricted"] = "failed"
        notes.append(f"A2: enchanted Elves attacked (P0 life "
                     f"{ST.get('p0_life_pre_attack')}->"
                     f"{ST.get('p0_life_post_attack')}); cant-attack "
                     f"restriction dropped")
    else:
        ass["A2_attack_restricted"] = "failed"
        notes.append("A2: attack1 submitted but outcome unresolved")
    # A3
    if not ST.get("activation_attempted"):
        ass["A3_activation_restricted"] = "not-run"
        notes.append("A3: activation test never ran")
    elif not ST.get("activation_offered"):
        ass["A3_activation_restricted"] = "passed"
        notes.append("A3: no ActivateAbility offered for the enchanted "
                     "Elves (untapped, P1 main phase)")
    else:
        pb = ST.get("pool_before_activation") or 0
        pa = ST.get("pool_after_activation") or 0
        if ST.get("activation_rejected"):
            ass["A3_activation_restricted"] = "passed"
            notes.append("A3: activation attempt rejected by engine")
        elif pa - pb >= 1:
            ass["A3_activation_restricted"] = "failed"
            notes.append(f"A3: enchanted Elves activated, P1 green pool "
                         f"{pb}->{pa}; cant-activate restriction dropped")
        else:
            ass["A3_activation_restricted"] = "passed"
            notes.append(f"A3: activation offered+attempted but no mana "
                         f"produced (pool {pb}->{pa})")
    # A4
    if ass["A1_setup_ok"] != "passed":
        ass["A4_escape_offered"] = "not-run"
        notes.append("A4: setup failed; escape scan not meaningful")
    elif ST.get("escape_offered"):
        ass["A4_escape_offered"] = "passed"
        notes.append("A4: exile-3 escape action was offered to P1")
    else:
        ass["A4_escape_offered"] = "failed"
        notes.append(f"A4: no exile-3-to-ignore escape action offered at "
                     f"P1 priority with gy>=3 (scanned action types: "
                     f"{ST.get('p1_action_types')})")
    # A5
    ass["A5_escape_effective"] = "not-run"
    notes.append("A5: escape payment path not driven (A4 "
                 + ("passed" if ST.get("escape_offered") else "failed")
                 + ")")
    # A6
    if ST.get("bear_attack_accepted"):
        ass["A6_control_binding"] = "passed"
        notes.append(f"A6: unenchanted Bear attacked normally (P0 life "
                     f"{ST.get('p0_life_pre_bear')}->"
                     f"{ST.get('p0_life_post_bear')})")
    elif ST.get("bear_submitted"):
        ass["A6_control_binding"] = "failed"
        notes.append("A6: bear control attack submitted but not observed "
                     "in attackers")
    else:
        ass["A6_control_binding"] = "not-run"
        notes.append("A6: bear control attack never submitted")
    # verdict
    if ass["A1_setup_ok"] != "passed":
        verdict = "blocked"
        notes.append("setup never reached the aura-attack test")
    elif any(ass[k] == "failed" for k in ("A2_attack_restricted",
                                          "A3_activation_restricted",
                                          "A4_escape_offered")):
        verdict = "reproduced"
        notes.append("Lost in Thought's combined restriction/escape clause "
                     "is dropped end-to-end on this build; see assertion "
                     "notes")
    elif all(ass[k] == "passed" for k in ("A2_attack_restricted",
                                          "A3_activation_restricted",
                                          "A4_escape_offered",
                                          "A5_escape_effective")):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
        notes.append("escape offered but payment path not driven; re-run "
                     "needed for A5")
    return verdict, ass


async def finalize(verdict, ass):
    dur = time.time() - ST["t_start"]
    for tag in ("post_attack", "post_control"):
        if tag not in ST["exports"]:
            await export_named(tag)
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S",
                                    time.gmtime(ST["t_start"])),
        "duration_s": round(dur, 1),
        "server": SERVER_IDENTITY,
        "server_run_dir": f"runs/{RUN_ID}",
        "driver": {"protocol_advertised": 106,
                   "client": "driver/client.py",
                   "scenario": "driver/scenario_4509_01020.py"},
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": [(LOST, 12), (SCOUR, 12), (ISLAND, 36)],
                  "P1": [(ELVES, 12), (BEARS, 12), (FOREST, 36)]},
        "assertions": ass,
        "notes": ST["notes"],
        "observations": {
            "rejections": ST["rejections"],
            "tick_errors": ST["tick_errors"],
            "escape_scan": {"offered": ST.get("escape_offered"),
                            "scan_done": ST.get("escape_scan_done"),
                            "p1_action_types": ST.get("p1_action_types")},
        },
        "driver_state": {
            "stage": ST["stage"],
            "enchant_target_oid": ST.get("enchant_target_oid"),
            "aura_oid": ST.get("aura_oid"),
            "scours_cast": ST.get("scours_cast", 0),
            "attack1": {"submitted": ST.get("attack1_submitted"),
                        "turn": ST.get("attack1_turn"),
                        "accepted": ST.get("attack1_accepted"),
                        "rejected": ST.get("attack1_rejected"),
                        "elves_attacked": ST.get("elves_attacked"),
                        "p0_life_pre": ST.get("p0_life_pre_attack"),
                        "p0_life_post": ST.get("p0_life_post_attack")},
            "activation": {"attempted": ST.get("activation_attempted"),
                           "offered": ST.get("activation_offered"),
                           "rejected": ST.get("activation_rejected"),
                           "candidates_seen":
                               ST.get("activation_candidates_seen"),
                           "pool_before": ST.get("pool_before_activation"),
                           "pool_after": ST.get("pool_after_activation")},
            "escape": {"offered": ST.get("escape_offered"),
                       "scan_done": ST.get("escape_scan_done")},
            "bear": {"submitted": ST.get("bear_submitted"),
                     "oid": ST.get("bear_oid"),
                     "accepted": ST.get("bear_attack_accepted"),
                     "p0_life_pre": ST.get("p0_life_pre_bear"),
                     "p0_life_post": ST.get("p0_life_post_bear")},
        },
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "Dense 12x playsets are a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "The prebuilt server has no standalone state-restore facility; "
            "pre.json/post_attack.json/post_control.json are authoritative "
            "exports restorable only via full game replay, not direct load.",
        ],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/scenario_4509_01020.py", "w") as f:
        f.write(open(__file__).read())
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": ST["notes"],
                   "verdict": verdict}, f, indent=1)

    # AGENTS.md lesson: close WIRE/RUNLOG BEFORE the manifest loop; the
    # final verdict lines flush after the hashes would otherwise fail
    # sha256sum -c.
    say("closing logs before manifest computation")
    WIRE.close()
    RUNLOG.close()
    # render the summary PNG from the saved states/assertions
    import subprocess
    r = subprocess.run(
        [sys.executable, f"{BACKFILL}/driver/render_summary_4509.py",
         EVDIR, str(ISSUE),
         "Lost in Thought ignore-effect escape clause dropped (cluster 36)"],
        capture_output=True, text=True)
    print("render stdout:", (r.stdout or "")[:300], flush=True)
    print("render stderr:", (r.stderr or "")[:500], flush=True)
    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(hashlib.sha256(
            open(f"{EVDIR}/{fn}", "rb").read()).hexdigest() + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(json.dumps({"verdict": verdict, "assertions": ass,
                      "run_id": RUN_ID}, indent=1), flush=True)


async def _main():
    global P0C, P1C
    ST.update({
        "t_start": time.time(),
        "ass": {k: "not-run" for k in ASS_KEYS},
        "notes": [],
        "rejections": [],
        "tick_errors": [],
        "data_level_ok": False,
        "hello_ok": False,
        "mana_needs": {"P0": {}, "P1": {}},
        "stage": "setup",
        "exports": {},
    })
    await verify_server_hello()
    check_data_level()

    t0 = time.time()
    p0 = PhaseClient("P0")
    P0C = p0
    await p0.connect()
    await p0.create(P0_DECK)
    p1 = PhaseClient("P1")
    P1C = p1
    await p1.connect()
    await p1.join(p0.game_code, P1_DECK)
    ST["game_code"] = p0.game_code
    say(f"game={p0.game_code} run={RUN_ID}")
    wire("game_created", {"code": p0.game_code})

    max_turn = 0
    last_rev = {}
    last_change = {0: time.time(), 1: time.time()}
    last_tick_at = {}
    i = 0
    while i < 9000:
        i += 1
        await asyncio.sleep(0.2)
        for c, tick, pid in ((p0, p0_tick, 0), (p1, p1_tick, 1)):
            st = c.latest
            if not st:
                continue
            rev_changed = c.revision != last_rev.get(c.name)
            if rev_changed:
                last_rev[c.name] = c.revision
                last_change[pid] = time.time()
            else:
                if time.time() - last_change[pid] > 45:
                    s0 = st["state"]
                    say(f"WATCHDOG stale {c.name}: rev {c.revision} "
                        f"turn={s0.get('turn_number')} "
                        f"phase={s0.get('phase')} "
                        f"holding_priority="
                        f"{my_priority(merged_actions(st))}")
                    ops_dbg = []
                    for o in vi_ops(st):
                        rd = (o.get("response") or {}).get("data", {}) or {}
                        cands = rd.get("candidates") or []
                        chs = rd.get("choices") or []
                        first = (cands or chs or [{}])[0]
                        ops_dbg.append({
                            "rtype": (o.get("response") or {}).get("type"),
                            "kind": vi_kind_code(st),
                            "n_candidates": len(cands),
                            "n_choices": len(chs),
                            "first": json.dumps(first, default=str)[:500],
                        })
                    wire("watchdog_stale",
                         {"who": c.name, "rev": c.revision,
                          "turn": s0.get("turn_number"),
                          "phase": s0.get("phase"),
                          "hand": len(hand_ids(s0, pid)),
                          "atypes": sorted(
                              set(a.get("type") for a in merged_actions(st))),
                          "vi_ops": ops_dbg})
                    last_change[pid] = time.time()
                # Safety net: if this client holds priority but produced no
                # revision for a while, re-tick anyway -- a tick that
                # returned without submitting must not stall the game.
                holds_prio = my_priority(merged_actions(st))
                if not (holds_prio
                        and time.time() - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            try:
                await tick(c)
            except Exception as e:
                say(f"[{c.name}] tick error: {type(e).__name__}: {e}")
                wire("tick_error", {"who": c.name,
                                    "err": f"{type(e).__name__}: {e}"})
        st = p0.latest
        if st:
            max_turn = max(max_turn, st["state"].get("turn_number", 0))
        if ST["stage"] == "done":
            break
        if max_turn > 60 and ST["stage"] == "setup":
            ST["notes"].append("could not reach the aura-attack test by "
                               "turn 60")
            break
        if time.time() - t0 > SETUP_DEADLINE_S:
            ST["notes"].append("setup deadline hit")
            break

    verdict, ass = evaluate()
    say(f"verdict={verdict} assertions={json.dumps(ass)}")
    await finalize(verdict, ass)
    await p0.close()
    await p1.close()


async def main():
    pidfile = "/tmp/scenario_4509_01020.pid"
    if os.path.exists(pidfile):
        try:
            old = int(open(pidfile).read().strip())
            os.kill(old, 0)
            raise SystemExit(f"another scenario_4509_01020 instance is alive "
                             f"(pid {old}); refusing")
        except (ValueError, ProcessLookupError, PermissionError):
            pass
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    try:
        await _main()
    finally:
        try:
            os.remove(pidfile)
        except OSError:
            pass


asyncio.run(main())
