#!/usr/bin/env python3
"""Issue #6861 revalidation on v0.98.0 (protocol 94): Squee, the Immortal.

Reported (Discord): "the card says you can cast it from grave and exile but
after exiling him I can't cast him anymore; tested also from graveyard and
from the grave seems to work just fine."

Oracle: "You may cast this card from your graveyard or from exile."  The
v0.79.0 run (2026-09-11, run 20260911-6861e, evidence commit
1d35aa69275a30414fff6359323b1b3351a4cea5) reproduced: no CastSpell advertised
for the exiled Squee in legal actions or the viewer-interaction Priority
menu; a raw CastSpell attempt was rejected invalid_action; the graveyard
control cast completed. Pinned card data parsed only a GraveyardCastPermission.

Plan (two human seats, native engine):
  SETUP - land drops; cast Faithless Looting, discard 2 Squees; cast
          Scavenging Ooze; activate Ooze targeting a Squee in P0's
          graveyard (exiles it). Gate: 1 Squee in exile, 1 Squee in gy,
          >=6 untapped lands, P0 main phase priority -> export pre.json.
  EXILE - attempt to cast the exiled Squee. Record whether CastSpell is
          advertised (A2) and whether it reaches stack -> battlefield (A3).
  GYCTL - control: cast a Squee from the graveyard (A4); answer the
          legend rule as-is if it appears.

Behavioral contract:
  A1 setup_ok            pre.json: Squee in exile + Squee in gy, P0 main
                         phase priority, >=6 untapped lands
  A2 exile_cast_offered  CastSpell advertised for the exiled Squee (legal
                         actions or viewer interaction)
  A3 exile_cast_completes exiled Squee reaches Stack then Battlefield
  A4 gy_cast_completes   control: gy Squee reaches Stack then Battlefield
  A5 cleanup             game proceeds after both attempts (stack empty,
                         no stuck prompt)

Verdict = reproduced iff A2 or A3 fails; not-reproduced iff A2, A3, A4
all pass.

Protocol-94 driver (v0.98.0, per scenario_301_0980 / scenario_6860_0980):
HELLO advertises protocol 94 (server enforces exact match); MulliganDecision
as {"choice":{"type":"Keep"}} gated on the seat's pending Declare;
BottomCards/DiscardToHandSize via single SelectCards {"cards":[...]} (int
oids); DeclareAttackers/Blockers via relations-schema interaction (no legacy
actions on protocol 94); CastSpell/ActivateAbility via advertised actions;
PayMana* via pay_tick; optional decisions via viewer_interaction
opportunities; PassPriority only when the seat genuinely holds Priority;
per-client stale watchdog.
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
log = logging.getLogger("scenario6861")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
EVID_RUN_ID = "20261001-6861"
EVDIR = f"{BACKFILL}/evidence/6861/{EVID_RUN_ID}"
assert not os.path.exists(EVDIR) or not os.listdir(EVDIR), "EVDIR not empty"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(),
                               "event": event + "_unserializable",
                               "error": str(e)}) + "\n")
    WIRE.flush()


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
say("server identity hashes verified against on-disk pinned artifacts")

SQUEE = "Squee, the Immortal"
LOOTING = "Faithless Looting"
OOZE = "Scavenging Ooze"
MOUNTAIN = "Mountain"
FOREST = "Forest"

ST = {"stage": "SETUP", "step": 0, "stop": False,
      "a2_offered": None, "a2_source": None}
CAST = {}          # active cast tracking: tag/oid/in_flight/announced/...
LOOT = {"cast": False, "in_flight": False, "discarded": False}
OOZE_ST = {"cast": False, "active": False, "done": False,
           "activated": False, "targeted": False, "target_oid": None}
MULLS = {}
SHAPES = set()
WF_SEEN = []
SUBMITTED = set()
LAST_SUBMIT = {"iid": None}
_WATCH = {}


# ------------------------------------------------------------------ helpers

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


def untapped_land(state, pid):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) in (MOUNTAIN, FOREST) and not o.get("tapped"))


def my_main(state, pid):
    return (state.get("active_player") == pid
            and state.get("priority_player") == pid
            and state.get("phase") in ("PreCombatMain", "PostCombatMain", "Main"))


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


def action_codes(ch):
    return [((s.get("data") or {}).get("code") or "")
            for s in ch.get("surfaces", []) or [] if s.get("type") == "action"]


def record_wf(state):
    wf = wf_of(state).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf, "stage": ST["stage"], "step": ST["step"]})


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": ST["stage"], "step": ST["step"]})
    await c.send_action(action)


async def send_interaction(c, sub):
    iid = (sub or {}).get("interactionId")
    if iid:
        LAST_SUBMIT["iid"] = iid
    wire("interaction_submit", {"who": c.name, "submission": sub,
                                "stage": ST["stage"], "step": ST["step"]})
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


async def export_now(path):
    s = await C0.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return s


def castspell_advertised(acts, oid):
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


def any_castspell_action(acts):
    for a in acts:
        if a["type"] == "CastSpell":
            return a
    return None


def vi_cast_choice(c, oid):
    """A viewer_interaction castSpell choice for the exact object: match the
    object surface's reference id (protocol 94), not a blob substring."""
    for opp in vi_ops(c):
        data = (opp.get("response") or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            if "castSpell" not in action_codes(ch):
                continue
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if s.get("type") == "object" \
                        and str(d.get("reference")) == str(oid):
                    return opp, ch
    return None


# ------------------------------------------------------- shared p94 ticks

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
    await c.send_action({"type": "MulliganDecision", "data": {"choice": {"type": choice}}})
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
    picks = [int(x) for x in sorted(hand_oids(state, pid), key=rank_fn(state))[:n]]
    say(f"[{tag}] bottoming {n}: {[oname(state['objects'][str(x)]) for x in picks]}")
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
    picks = [int(x) for x in sorted(hand_oids(state, pid), key=rank_fn(state))[:n]]
    say(f"[{tag}] discarding to hand size: {[oname(state['objects'][str(x)]) for x in picks]}")
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


def find_relations_op(c):
    for op in vi_ops(c):
        resp = op.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        spec = data.get("spec") or {}
        if resp.get("type") == "schema" and isinstance(spec, dict) \
                and spec.get("type") == "relations":
            return op.get("interactionId") or op.get("id")
    return None


async def answer_declare(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    wf = wf_of(state)
    if wf.get("type") not in ("DeclareAttackers", "DeclareBlockers"):
        return False
    if str(wf_player(state)) != str(pid):
        return False
    iid = find_relations_op(c)
    if not iid:
        return False
    sub = {"interactionId": iid,
           "response": {"type": "relations", "data": {"relations": []}}}
    wire("declare_empty", {"who": tag, "wf": wf.get("type"), "submission": sub})
    say(f"[{tag}] declares empty ({wf.get('type')})")
    await c.send_interaction(sub)
    SUBMITTED.add(iid)
    return True


# ------------------------------------------------------- interaction driver

async def scan_interactions(c, st, state, acts):
    """Answer P0's pending decisions. Returns True if it acted."""
    wtype = wf_of(state).get("type")
    if str(wf_player(state)) != "0":
        return False
    for opp in vi_ops(c):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        iid = opp.get("interactionId") or opp.get("id")
        if not iid or iid in SUBMITTED:
            continue
        chs = data.get("choices") or data.get("candidates") or []
        spec = data.get("spec") or {}
        spec_type = spec.get("type") if isinstance(spec, dict) else None
        key = (rtype, spec_type, wtype, len(chs))
        if key not in SHAPES:
            SHAPES.add(key)
            wire("interaction_shape", {"rtype": rtype, "spec": spec_type,
                                      "wf": wtype, "n": len(chs),
                                      "stage": ST["stage"], "step": ST["step"],
                                      "interaction": opp})
            say(f"[P0] new interaction shape rtype={rtype} spec={spec_type} "
                f"wf={wtype} n={len(chs)}")
        # --- Scavenging Ooze activation: advertised action preferred; the
        # protocol-94 server may instead offer activateAbility choices ---
        if OOZE_ST.get("active") and not OOZE_ST.get("activated"):
            for ch in chs:
                if "activateAbility" in action_codes(ch):
                    await send_interaction(
                        c, {"interactionId": iid,
                            "response": {"type": "choose",
                                         "data": {"choiceId": ch["id"]}}})
                    SUBMITTED.add(iid)
                    OOZE_ST["activated"] = True
                    say("[P0] activates Scavenging Ooze (viewer interaction)")
                    return True
        # --- Ooze target selection: exile a Squee from P0's graveyard -------
        if OOZE_ST.get("activated") and not OOZE_ST.get("targeted") \
                and wtype == "TargetSelection":
            sq = zone_oids(state, 0, "Graveyard", SQUEE)
            if not sq:
                say("[P0] Ooze target: no Squee in gy; deferring")
                return False
            want = sq[0]
            for ch in chs:
                ref = None
                for s in ch.get("surfaces", []) or []:
                    d = s.get("data") or {}
                    if isinstance(d, dict) and "reference" in d:
                        ref = str(d["reference"])
                if ref == str(want):
                    if rtype == "schema" and spec_type in ("sequence", "select"):
                        resp_out = {"type": spec_type,
                                    "data": {"choiceIds": [ch["id"]]}}
                    elif rtype == "exactChoices":
                        resp_out = {"type": "choose",
                                    "data": {"choiceId": ch["id"]}}
                    else:
                        resp_out = {"type": rtype,
                                    "data": {"choiceIds": [ch["id"]]}}
                    await send_interaction(
                        c, {"interactionId": iid, "response": resp_out})
                    SUBMITTED.add(iid)
                    OOZE_ST["targeted"] = True
                    OOZE_ST["target_oid"] = want
                    say(f"[P0] Ooze exiles Squee {want}")
                    return True
            say("[P0] Ooze target: Squee not among candidates; deferring")
            return False
        # --- Faithless Looting discard: discard 2 Squees ---------------------
        if LOOT.get("in_flight") and not LOOT.get("discarded") \
                and wtype == "DiscardChoice":
            picks = [str(oid) for oid in hand_oids(state, 0)
                     if oname(state["objects"][oid]) == SQUEE][:2]
            fallback = False
            if len(picks) < 2:
                # defensive: discard 2 lowest-rank non-key cards and retry a
                # fresh Looting later (the cast gate above should prevent this)
                fallback = True
                rest = sorted(hand_oids(state, 0), key=p0_rank(state))
                picks = [x for x in rest
                         if oname(state["objects"][x]) != SQUEE][:2]
                if len(picks) < 2:
                    say("[P0] Looting discard: nothing to discard; deferring")
                    return False
            want_refs = set(picks)
            choice_ids = []
            for ch in chs:
                for s in ch.get("surfaces", []) or []:
                    d = s.get("data") or {}
                    if isinstance(d, dict) \
                            and str(d.get("reference")) in want_refs:
                        choice_ids.append(ch["id"])
                        break
            if len(choice_ids) < 2:
                say(f"[P0] Looting discard: only {len(choice_ids)} Squee "
                    f"candidates found (spec={spec_type}); deferring")
                return False
            if rtype == "schema" and spec_type in ("sequence", "select"):
                resp_out = {"type": spec_type,
                            "data": {"choiceIds": choice_ids}}
            elif rtype == "exactChoices" and len(choice_ids) == 1:
                resp_out = {"type": "choose",
                            "data": {"choiceId": choice_ids[0]}}
            else:
                say(f"[P0] Looting discard: unexpected rtype={rtype} "
                    f"spec={spec_type}; deferring")
                return False
            await send_interaction(
                c, {"interactionId": iid, "response": resp_out})
            SUBMITTED.add(iid)
            if fallback:
                # no Squees hit the graveyard: allow a fresh Looting later
                LOOT["cast"] = False
                LOOT["in_flight"] = False
                say("[P0] Looting discarded non-Squees; will recast when able")
            else:
                LOOT["discarded"] = True
                say(f"[P0] Looting discards {picks} via {choice_ids}")
            return True
    return False


# ------------------------------------------------------------------ tick

def p0_rank(state):
    def rank(oid):
        nm = oname(state["objects"][oid])
        if nm in (SQUEE, LOOTING):
            return 2
        if nm == OOZE:
            return 1
        return 0
    return rank


def p1_rank(state):
    def rank(oid):
        return 0
    return rank


async def tick(c, pid, is_p0):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), merged_actions(st)
    tag = "P0" if is_p0 else "P1"
    if is_p0:
        if await do_mulligan(c, pid, tag, lambda s: any(
                oname(s["objects"][o]) in (SQUEE, LOOTING)
                for o in hand_oids(s, pid))):
            return True
    else:
        if await do_mulligan(c, pid, tag, lambda s: True):
            return True
    if await do_bottom(c, pid, tag, p0_rank if is_p0 else p1_rank):
        return True
    if await do_discard(c, pid, tag, p0_rank if is_p0 else p1_rank):
        return True
    if await pay_tick(c):
        return True
    if await answer_declare(c, pid, tag):
        return True
    # legend rule: keep the first via the advertised action
    for a in acts:
        if a.get("type") == "ChooseLegend":
            await submit_as_is(c, copy.deepcopy(a))
            say(f"{c.name} answers ChooseLegend (keep first)")
            wire("legend_answered", {"player": pid})
            return True
    # P0 pending decisions via viewer interaction
    if is_p0 and await scan_interactions(c, st, state, acts):
        return True
    wt0 = wf_of(state).get("type")
    wplayer = wf_player(state)
    # never pass while P0 has a cast/ability decision pending
    if is_p0 and wt0 in ("OptionalCostChoice", "TargetSelection",
                        "ManaPayment", "ChooseXValue", "DiscardChoice") \
            and str(wplayer) == "0":
        return False
    # a cast announcement in flight: hold until the spell is on the stack
    # (or the submission is answered). Escape on revision progress OR a
    # wall-clock timeout so a lost submission can't deadlock the driver.
    if is_p0 and CAST.get("in_flight"):
        on_stack = (squee_on_stack(state, CAST.get("oid"))
                    if CAST.get("oid") else False)
        resolved = CAST.get("announced") and not on_stack
        if not on_stack and not resolved and not CAST.get("rejected"):
            revs = c.revision - (CAST.get("rev_at_submit") or c.revision)
            wall = time.time() - CAST.get("wall_at_submit", time.time())
            if revs < 15 and wall < 90:
                return False
    # ooze activation in flight: hold only while P0 actually has an ooze
    # decision pending; otherwise P0 must pass so the ability can resolve.
    if is_p0 and OOZE_ST.get("active") and not OOZE_ST.get("done"):
        bwf = wf_of(state).get("type")
        if bwf in ("TargetSelection", "PayCost", "OptionalCostChoice",
                   "ManaPayment") and str(wplayer) == "0":
            return False
    if is_p0 and my_main(state, pid):
        if ST["stage"] == "SETUP":
            if await setup_step(c, pid, state, acts):
                return True
        elif await main_step(c, pid, state, acts):
            return True
    # default: pass priority only when genuinely holding it
    if wt0 == "Priority" and str(wplayer) == str(pid):
        for a in acts:
            if a.get("type") == "PassPriority":
                await submit_as_is(c, a)
                return True
    return False


async def setup_step(c, pid, state, acts):
    # land drop (retry every tick)
    lid = find_hand(state, pid, MOUNTAIN) or find_hand(state, pid, FOREST)
    for a in acts:
        if a["type"] == "PlayLand" and lid and str(
                a.get("data", {}).get("object_id")) == lid:
            await submit_as_is(c, a)
            return True
    # cast Faithless Looting (only when holding 2 Squees to discard)
    if not LOOT["cast"] and untapped_land(state, 0) >= 1:
        n_squee_hand = sum(1 for oid in hand_oids(state, pid)
                           if oname(state["objects"][oid]) == SQUEE)
        if n_squee_hand < 2:
            return False  # wait: keep drawing until 2 Squees are in hand
        lid2 = find_hand(state, pid, LOOTING)
        a = castspell_advertised(acts, lid2) if lid2 else None
        if a:
            LOOT["cast"] = True
            LOOT["in_flight"] = True
            await submit_as_is(c, a)
            say("P0 casts Faithless Looting")
            return True
    # cast Scavenging Ooze once 2+ Squees are in the graveyard
    if LOOT.get("discarded") and not OOZE_ST["cast"] \
            and untapped_land(state, 0) >= 2:
        n_gy = len(zone_oids(state, 0, "Graveyard", SQUEE))
        if n_gy >= 2:
            oid = find_hand(state, pid, OOZE)
            a = castspell_advertised(acts, oid) if oid else None
            if a:
                OOZE_ST["cast"] = True
                await submit_as_is(c, a)
                say("P0 casts Scavenging Ooze")
                return True
    # activate Ooze once it's on the battlefield and untapped (retries each
    # main phase until the activation is actually submitted: summoning
    # sickness may delay the first attempt)
    if OOZE_ST["cast"] and not OOZE_ST["activated"] and not OOZE_ST["done"]:
        ooze = [oid for oid, o in bf(state, 0)
                if oname(o) == OOZE and not o.get("tapped")]
        n_gy = len(zone_oids(state, 0, "Graveyard", SQUEE))
        if ooze and n_gy >= 1 and untapped_land(state, 0) >= 1:
            # advertised activation preferred on protocol 94; the viewer
            # interaction path is a fallback inside scan_interactions.
            ooze_oid = ooze[0]
            act = next((a for a in acts
                        if a.get("type") == "ActivateAbility"
                        and str((a.get("data") or {}).get("source_id")) == str(ooze_oid)), None)
            if act is None:
                act = next((a for a in acts if a.get("type") == "ActivateAbility"), None)
            if act is not None:
                wire("activate_ooze", {"action": act})
                await submit_as_is(c, act)
                OOZE_ST["activated"] = True
                OOZE_ST["active"] = True
                say("P0 activates Scavenging Ooze (advertised action)")
            else:
                OOZE_ST["active"] = True
                say("P0: Ooze activation pending (via viewer interaction)")
            return True
    return False


async def main_step(c, pid, state, acts):
    step = ST["step"]
    if step == 1:
        return await exile_cast_step(c, pid, state, acts)
    if step == 2:
        return await gy_cast_step(c, pid, state, acts)
    return False


def squee_on_stack(state, oid):
    o = state["objects"].get(str(oid))
    return bool(o and o.get("zone") == "Stack" and oname(o) == SQUEE)


def squee_on_bf(state, oid):
    o = state["objects"].get(str(oid))
    return bool(o and o.get("zone") == "Battlefield" and oname(o) == SQUEE)


def new_cast(tag, oid):
    CAST.clear()
    CAST.update({"tag": tag, "oid": str(oid), "in_flight": True,
                 "announced": False, "stack_seen": False, "bf_seen": False,
                 "done": False, "rejected": False, "silent_fail": False,
                 "offered": None, "rev_at_submit": None, "submitted": False})


def exile_raw_attempt(state, eid, acts):
    """Build a raw CastSpell attempt for the exiled Squee: clone an
    advertised CastSpell shape if one exists (swap object/card ids),
    else fall back to the protocol-69 wire shape."""
    ref = any_castspell_action(acts)
    if ref is not None:
        out = copy.deepcopy(ref)
        obj = state["objects"].get(str(eid), {})
        out["data"]["object_id"] = int(eid)
        out["data"]["card_id"] = int(obj.get("card_id", eid))
        out.pop("metadata", None)
        return out
    obj = state["objects"].get(str(eid), {})
    cid = obj.get("card_id", int(eid))
    return {"type": "CastSpell",
            "data": {"object_id": int(eid), "card_id": int(cid),
                     "targets": [], "payment_mode": {"type": "Auto"}}}


async def submit_cast_attempt(c, a, vich):
    """Submit a cast via the advertised legacy action if present, else via
    the viewer-interaction castSpell choice."""
    if a:
        await submit_as_is(c, a)
        return "legal_actions"
    opp, ch = vich
    iid = opp.get("interactionId") or opp.get("id")
    await send_interaction(c, {"interactionId": iid,
                               "response": {"type": "choose",
                                            "data": {"choiceId": ch["id"]}}})
    SUBMITTED.add(iid)
    return "viewer_interaction"


def menu_cast_spells(c):
    """(offered_for_oid_set, summary) of castSpell choices in the current
    Priority menu: choice id -> (object reference, object name, zone)."""
    out = {}
    for opp in vi_ops(c):
        data = (opp.get("response") or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            if "castSpell" not in action_codes(ch):
                continue
            ref = name = zone = None
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if s.get("type") == "object":
                    ref = str(d.get("reference"))
                    name = d.get("name")
                    zone = d.get("zone")
            out[ch.get("id")] = (ref, name, zone)
    return out


async def exile_cast_step(c, pid, state, acts):
    if not CAST.get("in_flight"):
        sq = zone_oids(state, 0, "Exile", SQUEE)
        if not sq:
            say("EXILE step: no Squee in exile; aborting")
            ST["stop"] = True
            return False
        eid = sq[0]
        a = castspell_advertised(acts, eid)
        vich = vi_cast_choice(c, eid)
        menu = menu_cast_spells(c)
        wire("exile_menu", {"oid": eid, "menu_cast_spells": menu,
                            "n_vi_ops": len(vi_ops(c))})
        say(f"EXILE menu: {len(menu)} castSpell choices: "
            f"{[(v[0], v[1], v[2]) for v in menu.values()]}")
        if not a and not vich:
            # the Priority menu can lag a tick behind the gate; wait a bit
            # before concluding the cast is not offered.
            t0 = CAST.get("menu_wait_t0") or time.time()
            CAST["menu_wait_t0"] = t0
            if time.time() - t0 < 20:
                return False
            say("EXILE menu: no castSpell choice for the exiled Squee "
                "after 20s; concluding not offered")
        CAST.pop("menu_wait_t0", None)
        new_cast("EXILE", eid)
        CAST["offered"] = bool(a or vich)
        ST["a2_offered"] = CAST["offered"]
        ST["a2_source"] = ("legal_actions" if a else
                           ("viewer_interaction" if vich else "none"))
        wire("exile_cast_attempt", {"oid": eid, "offered": CAST["offered"],
                                    "source": ST["a2_source"]})
        say(f"EXILE cast: oid={eid} offered={CAST['offered']} "
            f"({ST['a2_source']})")
        if a or vich:
            CAST["via"] = await submit_cast_attempt(c, a, vich)
        else:
            # attempt the raw action anyway; capture the rejection (or not).
            await submit_as_is(c, exile_raw_attempt(state, eid, acts))
            CAST["via"] = "raw_action"
        CAST["submitted"] = True
        CAST["rev_at_submit"] = c.revision
        CAST["wall_at_submit"] = time.time()
        return True
    # in flight: track the spell
    if squee_on_stack(state, CAST["oid"]):
        CAST["announced"] = True
        CAST["stack_seen"] = True
        wire("exile_stack_seen", {"oid": CAST["oid"]})
        say("EXILE cast: Squee on stack")
    if squee_on_bf(state, CAST["oid"]):
        CAST["bf_seen"] = True
        wire("exile_bf_seen", {"oid": CAST["oid"]})
    if CAST.get("rejected"):
        CAST["in_flight"] = False
        CAST["done"] = True
        await export_now("post_exile.json")
        ST["step"] = 2
        say("=== EXILE cast rejected -> GY control (step 2) ===")
        return True
    if CAST.get("announced") and not squee_on_stack(state, CAST["oid"]):
        CAST["in_flight"] = False
        CAST["done"] = True
        await export_now("post_exile.json")
        ST["step"] = 2
        say(f"=== EXILE cast done (bf_seen={CAST['bf_seen']}) -> step 2 ===")
        return True
    if (not CAST.get("announced") and CAST.get("submitted")
            and (c.revision - (CAST.get("rev_at_submit") or c.revision) >= 15
                 or time.time() - CAST.get("wall_at_submit", time.time()) > 90)):
        # revisions advanced with no stack sighting, or the submission went
        # unanswered for 90s: silent failure
        wf = wf_of(state).get("type")
        wplayer = wf_player(state)
        if not (wf in ("TargetSelection", "OptionalCostChoice", "ManaPayment")
                and str(wplayer) == "0"):
            CAST["in_flight"] = False
            CAST["done"] = True
            CAST["silent_fail"] = True
            await export_now("post_exile.json")
            ST["step"] = 2
            say("=== EXILE cast silent-fail (no stack sighting) -> step 2 ===")
            return True
    # while the spell is on the stack, pass so it can resolve
    if CAST.get("announced"):
        for a in acts:
            if a.get("type") == "PassPriority":
                await submit_as_is(c, a)
                say("P0 passes (EXILE cast on stack)")
                return True
    return False


async def gy_cast_step(c, pid, state, acts):
    if not CAST.get("in_flight"):
        sq = zone_oids(state, 0, "Graveyard", SQUEE)
        if not sq:
            say("GY step: no Squee in graveyard; aborting")
            ST["stop"] = True
            return False
        gid = sq[0]
        a = castspell_advertised(acts, gid)
        vich = vi_cast_choice(c, gid)
        menu = menu_cast_spells(c)
        wire("gy_menu", {"oid": gid, "menu_cast_spells": menu,
                         "n_vi_ops": len(vi_ops(c))})
        say(f"GY menu: {len(menu)} castSpell choices: "
            f"{[(v[0], v[1], v[2]) for v in menu.values()]}")
        if not a and not vich:
            t0 = CAST.get("menu_wait_t0") or time.time()
            CAST["menu_wait_t0"] = t0
            if time.time() - t0 < 20:
                return False
            say("GY menu: no castSpell choice for the gy Squee "
                "after 20s; concluding not offered")
        CAST.pop("menu_wait_t0", None)
        new_cast("GYCTL", gid)
        CAST["offered"] = bool(a or vich)
        wire("gy_cast_attempt", {"oid": gid, "offered": CAST["offered"],
                                 "source": ("legal_actions" if a else
                                            ("viewer_interaction" if vich else "none"))})
        say(f"GY control cast: oid={gid} offered={CAST['offered']}")
        if a or vich:
            CAST["via"] = await submit_cast_attempt(c, a, vich)
            CAST["submitted"] = True
            CAST["rev_at_submit"] = c.revision
            CAST["wall_at_submit"] = time.time()
        else:
            # control: attempt the raw legacy CastSpell for the gy Squee
            # (same path the v0.79.0 run's control completed through).
            await submit_as_is(c, exile_raw_attempt(state, gid, acts))
            CAST["via"] = "raw_action"
            CAST["submitted"] = True
            CAST["rev_at_submit"] = c.revision
            CAST["wall_at_submit"] = time.time()
            say("GY control: no advertised CastSpell; attempting raw action")
        return True
    if squee_on_stack(state, CAST["oid"]):
        CAST["announced"] = True
        CAST["stack_seen"] = True
        wire("gy_stack_seen", {"oid": CAST["oid"]})
        say("GY cast: Squee on stack")
    if squee_on_bf(state, CAST["oid"]):
        CAST["bf_seen"] = True
        wire("gy_bf_seen", {"oid": CAST["oid"]})
    if CAST.get("rejected"):
        CAST["in_flight"] = False
        CAST["done"] = True
        await export_now("post_gy.json")
        ST["stop"] = True
        say("GY cast rejected; finishing")
        return True
    if CAST.get("announced") and not squee_on_stack(state, CAST["oid"]):
        CAST["in_flight"] = False
        CAST["done"] = True
        await export_now("post_gy.json")
        ST["stop"] = True
        say(f"GY cast done (bf_seen={CAST['bf_seen']}); finishing")
        return True
    if (not CAST.get("announced") and CAST.get("submitted")
            and time.time() - CAST.get("wall_at_submit", time.time()) > 90):
        CAST["in_flight"] = False
        CAST["done"] = True
        CAST["silent_fail"] = True
        await export_now("post_gy.json")
        ST["stop"] = True
        say("GY cast silent-fail (submission unanswered 90s); finishing")
        return True
    if CAST.get("announced"):
        for a in acts:
            if a.get("type") == "PassPriority":
                await submit_as_is(c, a)
                say("P0 passes (GY cast on stack)")
                return True
    return False


# ------------------------------------------------------------------ main

async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    A = obs["assert"]

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck((SQUEE, 12), (LOOTING, 4), (OOZE, 4),
                         (MOUNTAIN, 20), (FOREST, 20)))
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck((MOUNTAIN, 30), (FOREST, 30)))
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
            if rej and is_p0 and CAST.get("in_flight") \
                    and not CAST.get("announced"):
                CAST["rejected"] = True
                CAST["reject_data"] = rej
                say(f"[P0] cast {CAST.get('tag')} rejected")
                force_tick[c.name] = True
            if rej and LAST_SUBMIT["iid"] in SUBMITTED:
                SUBMITTED.discard(LAST_SUBMIT["iid"])
                say(f"[{c.name}] resync: retrying {LAST_SUBMIT['iid']} "
                    f"after rejection")
                LAST_SUBMIT["iid"] = None
                force_tick[c.name] = True
            # periodic re-tick even without a revision change (missed
            # broadcast resilience); at most every 5s per client.
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

        # Faithless Looting resolution bookkeeping
        if LOOT.get("in_flight") and LOOT.get("discarded"):
            if not zone_oids(state, 0, "Stack", LOOTING):
                LOOT["in_flight"] = False
                say("Looting resolved (discard done)")
        # Ooze exile bookkeeping
        if OOZE_ST.get("targeted") and not OOZE_ST.get("done"):
            tgt = OOZE_ST.get("target_oid")
            o = state["objects"].get(str(tgt), {})
            if o.get("zone") == "Exile":
                OOZE_ST["done"] = True
                OOZE_ST["active"] = False
                say(f"Ooze exile done: Squee {tgt} now in Exile")
                wire("ooze_exile_done", {"oid": tgt})

        # SETUP -> MAIN gate
        if ST["stage"] == "SETUP" and my_main(state, 0):
            n_ex = len(zone_oids(state, 0, "Exile", SQUEE))
            n_gy = len(zone_oids(state, 0, "Graveyard", SQUEE))
            un = untapped_land(state, 0)
            if n_ex >= 1 and n_gy >= 1 and un >= 6:
                await export_now("pre.json")
                ST["stage"] = "MAIN"
                ST["step"] = 1
                SUBMITTED.clear()
                say(f"=== stage -> MAIN (exile={n_ex} gy={n_gy} "
                    f"untapped={un}) ===")
                wire("setup_ready", {"exile": n_ex, "gy": n_gy,
                                     "untapped": un})
                continue

    # ------------------------------------------------------- evaluate
    def load_state(path):
        env = json.load(open(f"{EVDIR}/{path}"))
        return env["state"]

    pre = load_state("pre.json")
    A["A1_setup_ok"] = ("passed"
                        if len(zone_oids(pre, 0, "Exile", SQUEE)) >= 1
                        and len(zone_oids(pre, 0, "Graveyard", SQUEE)) >= 1
                        and untapped_land(pre, 0) >= 6
                        else "failed")
    A["A2_exile_cast_offered"] = ("passed" if ST["a2_offered"]
                                  else ("failed" if ST["a2_offered"] is False
                                        else "not-run"))

    # rebuild EXILE / GY results from wire events
    exile_stack = exile_bf = gy_stack = gy_bf = False
    exile_offered = ST["a2_offered"]
    gy_offered = None
    exile_rejected = False
    with open(f"{EVDIR}/wire_log.jsonl") as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            ev = e.get("event")
            if ev == "exile_stack_seen":
                exile_stack = True
            elif ev == "exile_bf_seen":
                exile_bf = True
            elif ev == "gy_stack_seen":
                gy_stack = True
            elif ev == "gy_bf_seen":
                gy_bf = True
            elif ev == "gy_cast_attempt":
                gy_offered = bool((e.get("payload") or {}).get("offered"))
            elif ev == "rejected":
                exile_rejected = True
    A["A3_exile_cast_completes"] = ("passed" if exile_stack and exile_bf
                                    else ("failed" if ST["step"] >= 2
                                          or exile_rejected
                                          else "not-run"))
    A["A4_gy_cast_completes"] = ("passed" if gy_stack and gy_bf
                                 else ("failed" if gy_offered is False
                                       or ST.get("stop")
                                       else "not-run"))
    # cleanup: game advanced past the attempts with an empty stack
    try:
        post = load_state("post_gy.json")
        stack_empty = not any(o.get("zone") == "Stack"
                              for o in post["objects"].values())
        A["A5_cleanup"] = ("passed" if stack_empty else "failed")
    except Exception:
        A["A5_cleanup"] = "not-run"

    obs["notes"].append(f"WF sequence: {WF_SEEN}")
    obs["notes"].append(f"exile offered={exile_offered} "
                        f"(source={ST['a2_source']}) stack={exile_stack} "
                        f"bf={exile_bf} rejected={exile_rejected}")
    obs["notes"].append(f"gy offered={gy_offered} stack={gy_stack} "
                        f"bf={gy_bf}")
    obs["notes"].append("protocol-94 driver (v0.98.0): mulligan as "
                        "{choice:{type:Keep}} gated on pending Declare; "
                        "bottom/discard-to-hand-size via single "
                        "SelectCards(data.cards); "
                        "DeclareAttackers/Blockers via relations-schema "
                        "interaction; CastSpell/ActivateAbility via advertised "
                        "actions (raw CastSpell attempt on cloned shape when "
                        "not advertised); priority-gated passes; "
                        "stale-client watchdog.")

    if A["A2_exile_cast_offered"] == "failed" or A["A3_exile_cast_completes"] == "failed":
        verdict = "reproduced"
    elif (A["A2_exile_cast_offered"] == "passed"
          and A["A3_exile_cast_completes"] == "passed"
          and A["A4_gy_cast_completes"] == "passed"):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    run = {
        "issue": 6861,
        "run_id": EVID_RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(time.time() - t0, 1),
        "server_identity": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 94, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(f"{BACKFILL}/driver/scenario_6861_0980.py"),
        "decks": {
            "P0": [[SQUEE, 12], [LOOTING, 4], [OOZE, 4], [MOUNTAIN, 20], [FOREST, 20]],
            "P1": [[MOUNTAIN, 30], [FOREST, 30]],
        },
        "assertions": A,
        "notes": obs["notes"],
        "wf_sequence": WF_SEEN,
        "a2_source": ST["a2_source"],
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full game replay (scenario_6861_0980.py).",
        ],
        "setup_line": "P0: 12x Squee, 4x Faithless Looting, 4x Scavenging Ooze, 20x Mountain, 20x Forest; P1: 60 lands (draw-go)",
        "contract_line": ("Cast exiled Squee: advertised and completes to the battlefield. "
                          "Control: cast graveyard Squee completes to the battlefield."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "wf_sequence": WF_SEEN, "a2_source": ST["a2_source"]}, f, indent=2)
    await render_summary(run, f"{EVDIR}/summary.png")
    WIRE.close()
    RUNLOG.close()
    await write_manifest()
    print(f"DONE verdict={verdict} assertions={json.dumps(A)}", flush=True)


async def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 760
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #6861 - Squee, the Immortal cast from exile (revalidation)", fill=(235, 240, 250))
    y += 30
    d.text((24, y), f"server v{si['validated_version']} ({si['build_commit']}) protocol {si['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180))
    y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced" else (230, 200, 90)))
    y += 34
    d.text((24, y), "Assertions (from saved states + wire log):", fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "A1 setup: Squee in exile + Squee in gy, P0 main priority, 6+ untapped lands",
        "A2_exile_cast_offered": "A2 CastSpell advertised for the exiled Squee",
        "A3_exile_cast_completes": "A3 exiled Squee reaches Stack then Battlefield",
        "A4_gy_cast_completes": "A4 control: graveyard Squee reaches Stack then Battlefield",
        "A5_cleanup": "A5 stack empty after attempts; game proceeds",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else ((255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y), f"{'pass' if v == 'passed' else ('FAIL' if v == 'failed' else 'n/a')} {lab}", fill=col)
        y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in run["notes"][:8]:
        d.text((40, y), n[:120], fill=(150, 165, 185))
        y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 6861/" + run["run_id"], fill=(120, 130, 150))
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
    asyncio.run(main())
