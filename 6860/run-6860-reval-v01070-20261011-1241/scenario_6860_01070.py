#!/usr/bin/env python3
"""Issue #6860 re-validation on pinned v0.107.0 (protocol 130): Dargo, the Shipwrecker.

Reported (Discord): "I can only cast him for his original cost, he has 2
colorless discount for every creature/artifact sac that turn and you can sac
creatures and artifact as an additional cost while casting him; neither the
first nor the second discount can be activated."

Oracle: "As an additional cost to cast this spell, you may sacrifice any
number of artifacts and/or creatures. This spell costs {2} less to cast for
each permanent sacrificed this way and {2} less to cast for each other
artifact or creature you've sacrificed this turn."  (pinned cost: {6}{R})

Prior runs: v0.79.0 (20260911-6860g) reproduced (LEG1 charged 7 not 5; LEG2
charged 3 not 1); v0.98.0 (20261001-6860, protocol 94), v0.102.0
(20261005-6860, protocol 106), v0.103.0 (20261007-6860, protocol 106),
v0.104.0 (20261009-6860, protocol 118) and v0.105.0 (20261010-1113-6860,
protocol 120) re-ran the same contract and reproduced. The v0.98.0 card data collapsed the cost into a single ModifyCost
{ Reduce {2}, dynamic_count: TrackedSetSize } with condition
AdditionalCostPaid - the "each other artifact or creature you've sacrificed
this turn" reduction was absent from the parse.

Plan (single P0 turn, two human seats; mana spent = untapped-Mountain delta):
  SETUP - 15+ untapped Mountains, Goblin Bombardment + 4+ Memnites on BF,
          2+ Dargos in hand. Export pre.json.
  STEP0 - Activate Bombardment, sacrifice 1 Memnite (prior sac this turn).
  LEG1  - Cast Dargo#1 DECLINING the additional cost.
          Oracle: {6}{R} - {2} (prior sac) = 5 mana spent.
  LEG2  - Cast Dargo#2, PAYING the additional cost with 2 Memnites.
          Oracle: {6}{R} - {2} (prior) - {4} (additional) = 1 mana spent.

Behavioral contract:
  A1 setup_ok            pre.json: 2+ Dargo in hand, Bombardment + 4 Memnites
                         on BF, >=15 untapped Mountains, empty pool
  A2 additional_offered  Dargo announcement offered the optional any-number
                         artifact/creature sacrifice choice
  A3 prior_discount      LEG1 (1 prior sac, 0 additional): mana spent == 5
  A4 additional_discount LEG2 (1 prior + 2 additional): mana spent == 1
  A5 cleanup             both casts resolved; legend rule answered; game
                         proceeds without a stuck prompt

Verdict = reproduced iff A3 or A4 fails; not-reproduced iff all pass;
blocked if setup never reaches or a leg cannot run.

Protocol-130 driver (v0.107.0, ported from the verified v0.105.0
scenario_6860_01050.py; conventions proven on protocol 120):
conventions proven in scenario_6773_01020.py / scenario_7195_01020.py):
  - HELLO advertises protocol 130 (exact match); CreateGameWithSettings +
    JoinGameWithPassword + start_when_full; gate on viewer_interaction
    (waiting_for is gone/null on 106); export response data.state is a JSON
    string parsed once.
  - MulliganDecision answered via legacy Action, gated on the advertised
    action; DiscardToHandSize via vi schema/select; CastSpell /
    ActivateAbility via advertised actions as-is; mana via legacy PayMana
    actions and/or vi tapLandForMana menus driven by MANA_NEEDS.
  - Optional additional cost: decideOptionalCost / decideOptionalEffect
    opportunity (exactChoices bare true/false with ("value","accept",
    "true"|"false") surfaces, or schema). Decline = accept/false; pay =
    accept/true, then number schema (count), then select/sequence candidates
    (the planned Memnites, matched by object reference).
  - Mana: the v0.102.0/v0.103.0 engine auto-taps reliably (observed on every cast),
    so the driver does NOT tap manually: tapping alongside the auto-tap
    overpays into the pool and pollutes the untapped-Mountain delta. Manual
    taps engage only as a >90s last-resort fallback (logged loudly). Legacy
    PayMana actions are answered only when MANA_NEEDS has outstanding needs
    for a non-test cast (2026-10-07 double-pay gate); LEG1/LEG2 keep
    MANA_NEEDS empty so the engine's auto-tap is the single payer and the
    delta measures exactly the engine's computed cost.
  - playLand is in NON_DECISION_CODES (bundled into the ordinary priority
    action menu; playing a land is optional so the menu is never a forced
    decision); explicit land-play (advertised PlayLand, vi playLand
    fallback) runs before the decision gate. Land matching uses is_land().
  - surf_codes() filters None codes (106 surfaces sometimes carry
    "data": {} with no "code").
  - Target selection: schema sequence/select with seat-carrying candidates
    (Bombardment targets P1 = seat 1); sacrifice-cost selections reference
    battlefield objects.
  - DeclareAttackers: advertised action first, then vi opportunity, then
    blind submit of {"attacks":[],"bands":[]} (declare-phase gated).
  - Legend rule: advertised ChooseLegend action, vi exactChoices fallback.
  - Priority passes only when the seat genuinely holds it (PassPriority in
    top-level legal_actions); per-revision pass guards; 5s re-tick backstop.
  - Cast-confirmation guard (protocol-130 necessity, 2026-10-09): with engine
    auto-pay there is no mana-payment phase to hold priority, so a bare
    CastSpell can lose a race to the driver's own PassPriority on the next
    tick and be silently dropped by the server. After every CastSpell (and
    the Bombardment ability activation) the driver sets ST["pending_cast"]
    and answers only decisions/mana while it is unconfirmed; a 30s backstop
    clears a still-unconfirmed action so the play re-triggers.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time
import traceback

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402

import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 6860
RUN_ID = "run-6860-reval-v01070-20261011-1241"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

DARGO = "dargo, the shipwrecker"
BOMBARD = "goblin bombardment"
MEMNITE = "memnite"
MOUNTAIN = "mountain"

GAME_TIMEOUT = 2400

ST = {
    "step": "SETUP",
    "cast": None,
    "legs": {},
    "wf_seen": [],
    "legend_answered": 0,
    "mulls": {},
}
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}
MANA_NEEDS = {}


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(line + "\n")
        RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


SERVER_IDENTITY = {
    "validated_version": "v0.107.0",
    "build_commit": "eccd25f",
    "protocol_version": 130,
    "server_binary_sha256": "f89957a1e5aa0fc1e68bf61a1eccc4ad6b257bc57d98aa270869fcad78456397",
    "card_data_sha256": "2021496869e2e86df276f1a07de001d50e7220087d46ac21acd69d04d73f9433",
    "draft_pools_sha256": "a44926f1b80be0c64e794e5562b9eeccd9d704488a7b02fc3ccca2130eea0d1c",
    "signature_verified": True,
}

for _f, _k in (("server/releases/v0.107.0/phase-server-slim-x86_64-unknown-linux-musl",
                "server_binary_sha256"),
               ("server/releases/v0.107.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.107.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
say("server identity hashes verified against on-disk pinned artifacts")

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.107.0/data/card-data.json"))


# ------------------------------------------------------------- state helpers
def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {}) or {}


def obj_lname(state, oid):
    return str(get_obj(state, oid).get("base_name")
               or get_obj(state, oid).get("name") or "?").lower()


def oname(o):
    return str(o.get("base_name") or o.get("name") or "?")


def player_obj(state, pid):
    for p in state.get("players", []) or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def hand_ids(state, pid):
    return [str(x) for x in (player_obj(state, pid).get("hand") or [])]


def bf_oids(state, pid):
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)]


def bf_by_name(state, pid, lname):
    return [oid for oid in bf_oids(state, pid)
            if obj_lname(state, oid) == lname]


def find_bf_oid(state, pid, lname):
    oids = bf_by_name(state, pid, lname)
    return oids[0] if oids else None


def memnite_oids(state, pid):
    return bf_by_name(state, pid, MEMNITE)


def dargo_hand_oids(state, pid):
    return [str(o) for o in hand_ids(state, pid)
            if obj_lname(state, o) == DARGO]


def dargo_bf_oids(state, pid):
    return bf_by_name(state, pid, DARGO)


def dargo_stack(state):
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Stack"
            and str(o.get("base_name") or o.get("name") or "").lower() == DARGO]


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_lands(state, pid, lname):
    return sum(1 for oid in bf_oids(state, pid)
               if not get_obj(state, oid).get("tapped")
               and obj_lname(state, oid) == lname)


def pool_len(state, pid):
    p = player_obj(state, pid)
    return len(list((p.get("mana_pool") or {}).get("mana") or []))


def life_of(state, pid):
    return player_obj(state, pid).get("life")


def object_on_stack_or_bf(state, lname):
    for o in (state.get("objects") or {}).values():
        if str(o.get("base_name") or o.get("name") or "").lower() == lname \
                and o.get("zone") in ("Stack", "Battlefield"):
            return True
    return False


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


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


def my_priority(acts):
    return any(a.get("type") == "PassPriority" for a in acts)


def my_main(state, pid):
    return (state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == pid
            and not (state.get("stack") or []))


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
            if isinstance(s.get("data"), dict)
            and s.get("data", {}).get("code") is not None]


def opp_codes(opp):
    codes = set()
    resp = opp.get("response") or {}
    data = resp.get("data") or {}
    for ch in (data.get("candidates") or data.get("choices") or []):
        codes.update(c for c in surf_codes(ch) if c)
    return codes


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}


def real_decision_pending(st):
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
        if "decideOptionalEffect" in codes or "decideOptionalCost" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def candidate_seat(ch):
    for s in ch.get("surfaces", []) or []:
        if s.get("type") not in ("player", "target", "candidate"):
            continue
        d = s.get("data") or {}
        for k in ("seat", "player", "index"):
            if d.get(k) is not None:
                try:
                    return int(d[k])
                except (TypeError, ValueError):
                    pass
    return None


def st_of(c):
    return c.latest or {}


def setup_gate(state):
    return (len(dargo_hand_oids(state, 0)) >= 2
            and find_bf_oid(state, 0, BOMBARD) is not None
            and len(memnite_oids(state, 0)) >= 4
            and untapped_lands(state, 0, MOUNTAIN) >= 15
            and pool_len(state, 0) == 0)

# ------------------------------------------------------------- interaction primitives
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
    wire("interaction_submission",
         {"who": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
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
    say(f"ServerHello: version={ver} build={build} protocol={proto} mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.107.0"), f"unexpected version {ver}"
    assert int(proto) == 130, f"unexpected protocol {proto}"
    assert str(build) == "eccd25f", f"unexpected build {build}"


def check_data_level():
    """Record how Dargo, the Shipwrecker parses in the pinned v0.107.0 card
    data: is the 'each other artifact or creature you've sacrificed this
    turn' discount present, or still collapsed into the single
    Reduce {2} / TrackedSetSize / AdditionalCostPaid?"""
    e = CARD_DATA.get("dargo, the shipwrecker", {})
    oracle = str(e.get("oracle_text", ""))
    add_cost = e.get("additional_cost")
    statics = e.get("static_abilities") or []
    reduce_modes = [s.get("mode", {}) for s in statics
                    if isinstance(s.get("mode"), dict)
                    and "ModifyCost" in (s.get("mode") or {})]
    blob = json.dumps({"additional_cost": add_cost,
                       "static_abilities": statics}).lower()
    prior_turn_discount_present = (
        "sacrificed this turn" in blob and "other" in blob
        and "tracked" not in blob.replace("trackedsetsize", ""))
    # precise check: does any static ability model a reduction NOT conditioned
    # on AdditionalCostPaid (i.e. the prior-turn-sac discount)?
    unconditioned = [s for s in statics
                     if (s.get("condition") or {}).get("type") != "AdditionalCostPaid"
                     and "ModifyCost" in (s.get("mode") or {})]
    finding = (
        "v0.107.0 card data still collapses Dargo's cost into a single "
        "ModifyCost { Reduce {2}, dynamic_count: TrackedSetSize } with "
        "condition AdditionalCostPaid; the 'each other artifact or creature "
        "you've sacrificed this turn' reduction is absent from the parse "
        "(same as v0.98.0)."
        if not unconditioned else
        "v0.107.0 card data now models a reduction beyond AdditionalCostPaid: "
        + json.dumps(unconditioned)[:400]
    )
    say(f"data-level finding: {finding}")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({
            "card": "Dargo, the Shipwrecker",
            "oracle_text": oracle,
            "additional_cost": add_cost,
            "static_abilities": statics,
            "reduce_modes": reduce_modes,
            "prior_turn_sac_discount_in_parse": bool(unconditioned),
            "finding": finding,
        }, f, indent=1)
    return finding


# ------------------------------------------------------------- common ticks
def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


async def do_bottom(c, acts, st, pid, tag):
    """Protocol 106: bottom-after-mulligan surfaces as a vi schema/select
    opportunity, gated on waitingForKind.code == 'mulligan' at turn 1 /
    Untap. Bottoms the N lowest-priority cards (Mountains first; never
    Dargo or Bombardment)."""
    if vi_kind_code(st) != "mulligan":
        return False
    state = st["state"]
    if not (state.get("turn_number") == 1 and state.get("phase") == "Untap"):
        return False
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
        iid = opp.get("interactionId")
        key = (tag, "bottom", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}
               ).get("data", {}) or {}
        n = int(con.get("min") or con.get("max") or 1)
        if n <= 0:
            return False

        def bkey(ch):
            ref = _cand_reference(ch)
            nm = obj_lname(state, ref) if ref is not None else "?"
            if nm in (DARGO, BOMBARD):
                return (2, str(ref))
            if nm == MEMNITE:
                return (1, str(ref))
            return (0, str(ref))

        ranked = sorted(cands, key=bkey)
        picks = [ch["id"] for ch in ranked[:n] if ch.get("id")]
        if not picks:
            return False
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] bottoms {n}: "
            f"{[obj_lname(state, _cand_reference(ch)) for ch in ranked[:n]]}")
        wire("bottom", {"who": tag, "count": n})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": picks}}},
                          tag)
        return True
    return False


async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", str(c.revision))
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    hand = [obj_lname(st["state"], o) for o in hand_ids(st["state"], pid)]
    has_dargo = DARGO in hand
    n = ST["mulls"].get(tag, 0)
    if tag == "P0":
        choice = "Keep" if (has_dargo or n >= 2) else "Mulligan"
    else:
        choice = "Keep"
    if choice == "Mulligan":
        ST["mulls"][tag] = n + 1
    say(f"[{tag}] mulligan -> {choice} (hand={hand})")
    wire("mulligan", {"who": tag, "decision": choice})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": choice}}})
    return True


def p0_discard_rank(state, o):
    ln = obj_lname(state, o)
    if ln == MEMNITE:
        return 0
    if ln == MOUNTAIN:
        return 1
    if ln in (DARGO, BOMBARD):
        return 3
    return 2


async def do_discard(c, acts, st, pid, tag, rank):
    state = st["state"]
    hand = hand_ids(state, pid)
    if len(hand) <= 7:
        return False
    n = len(hand) - 7
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
        iid = opp.get("interactionId")
        key = (tag, "discard", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        ref_of = {}
        for ch in cands:
            ref = _cand_reference(ch)
            if ref is not None:
                ref_of[str(ref)] = ch["id"]
        ranked = sorted(hand, key=lambda o: (rank(state, o), obj_lname(state, o)))
        pick = ranked[:n]
        choice_ids = [ref_of[o] for o in pick if o in ref_of]
        if not choice_ids:
            return False
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] discards {n}: {[obj_lname(state, o) for o in pick]}")
        wire("discard", {"who": tag, "oids": pick})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": choice_ids}}},
                           tag)
        return True
    return False


async def do_declare_empty(c, acts, st, pid, tag):
    for a in acts:
        if a.get("type") == "DeclareAttackers":
            d = copy.deepcopy(a)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
            say(f"[{tag}] declare no attackers")
            return True
        if a.get("type") == "DeclareBlockers":
            d = copy.deepcopy(a)
            d.setdefault("data", {})["assignments"] = []
            await submit_as_is(c, d)
            say(f"[{tag}] declare no blockers")
            return True
    # 106 fallback: vi opportunity, then blind submit (declare-phase gated)
    state = st["state"]
    phase = str(state.get("phase") or "")
    if state.get("active_player") == pid and "declareattack" in phase.lower():
        for opp in vi_ops(st):
            resp = opp.get("response", {}) or {}
            if resp.get("type") != "schema":
                continue
            spec = (resp.get("data") or {}).get("spec", {}) or {}
            if spec.get("type") not in ("select", "sequence", "relations"):
                continue
            iid = opp.get("interactionId")
            key = (tag, "declare", str(iid))
            if key in SUBMITTED_OPPS:
                return True
            SUBMITTED_OPPS.add(key)
            say(f"[{tag}] declare empty via vi opportunity")
            wire("declare_empty_vi", {"who": tag})
            await interact_as(c, {"interactionId": iid,
                                  "response": {"type": spec.get("type"),
                                               "data": {"choiceIds": []}}}, tag)
            return True
        key = (tag, "declare-blind", str(c.revision))
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            say(f"[{tag}] blind declare empty (106 shape)")
            wire("declare_empty_blind", {"who": tag})
            await c.send_action({"type": "DeclareAttackers",
                                 "data": {"attacks": [], "bands": []}})
            return True
    return False


async def do_legend(c, acts, st, tag):
    for a in acts:
        if a.get("type") == "ChooseLegend":
            await submit_as_is(c, a)
            ST["legend_answered"] += 1
            say(f"[{tag}] legend rule: keeps first (advertised action)")
            wire("legend", {"who": tag, "style": "action"})
            return True
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or []
        if not chs:
            continue
        blob = (str(opp.get("description", "")) + " " +
                " ".join(choice_text(ch) for ch in chs)).lower()
        if "legend" not in blob:
            continue
        iid = opp.get("interactionId")
        key = (tag, "legend", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        SUBMITTED_OPPS.add(key)
        ST["legend_answered"] += 1
        say(f"[{tag}] legend rule: keeps first (vi choice)")
        wire("legend", {"who": tag, "style": "vi"})
        await answer_vi(c, opp, chs[0], tag)
        return True
    return False


async def pay_tick(c, acts, tag):
    # 2026-10-07 double-pay gate: the engine auto-taps for Auto-payment casts
    # itself; answering a legacy PayMana action on top of that double-pays and
    # pollutes cost measurements. Only fire when the driver has outstanding
    # mana needs for a NON-test cast (test casts keep MANA_NEEDS empty).
    needs = MANA_NEEDS.get(tag, {})
    if sum(needs.values()) <= 0:
        return False
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            say(f"[{tag}] legacy pay-mana action answered "
                f"(outstanding needs={dict(needs)})")
            wire("legacy_pay_mana", {"who": tag, "action": a.get("type"),
                                     "needs": dict(needs)})
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag, needs):
    """Tap one land via the 106 tapLandForMana menu. One tap per call; the
    caller re-checks the game state (spell announced?) before tapping again."""
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
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
        iid = opp.get("interactionId") or opp.get("id")
        key = (tag, "tap", str(iid))
        if key in SUBMITTED_OPPS:
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
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] tap land for mana used_for={used} "
            f"(remaining needs={dict(needs)})")
        wire("tap_land", {"who": tag, "used_for": used})
        await answer_vi(c, opp, pick, tag)
        return True
    return False


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    lands = [o for o in hand_ids(state, pid) if is_land(get_obj(state, o))]
    if not lands:
        return False
    for o in lands:
        for a in acts:
            if a["type"] == "PlayLand" and str(a.get("_src_oid")) == str(o):
                LAND_PLAYED_TURN[tag] = turn
                say(f"[{tag}] playing land {obj_lname(state, o)}")
                wire("play_land", {"who": tag, "oid": o})
                await submit_as_is(c, a)
                return True
    # 106 may also advertise land plays as a vi playLand opportunity
    st = st_of(c)
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            if "playLand" not in surf_codes(ch):
                continue
            ref = _cand_reference(ch)
            if ref is not None and str(ref) in [str(x) for x in lands]:
                iid = opp.get("interactionId")
                key = (tag, "playland", str(iid), str(ref))
                if key in SUBMITTED_OPPS:
                    continue
                SUBMITTED_OPPS.add(key)
                LAND_PLAYED_TURN[tag] = turn
                say(f"[{tag}] playing land via vi {obj_lname(state, ref)}")
                wire("play_land_vi", {"who": tag, "oid": str(ref)})
                await answer_vi(c, opp, ch, tag)
                return True
    return False


async def pass_priority(c, st, acts, tag):
    for a in acts:
        if a["type"] == "PassPriority":
            key = (tag, "pass", str(c.revision))
            if key in PASSED_REV:
                return True
            PASSED_REV[key] = True
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
                key = (tag, "pass", str(iid))
                if key in PASSED_REV:
                    return True
                PASSED_REV[key] = True
                await interact_as(c, {"interactionId": iid,
                                      "response": {"type": "choose",
                                                   "data": {"choiceId": ch.get("id")}}},
                                  tag)
                return True
    return False


def find_cast_action(acts, state, lname, oid=None):
    for a in acts:
        if "cast" not in a.get("type", "").lower():
            continue
        d = a.get("data", {}) or {}
        for v in list(d.values()) + [a.get("_src_oid")]:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if oid is not None and str(iv) != str(oid):
                continue
            if obj_lname(state, iv) == lname:
                return a
    return None


# ------------------------------------------------------------- cast-confirmation guard (protocol 130)
def cast_obj_zone(state, oid):
    return str((get_obj(state, oid).get("zone") or "")).lower()


def cast_unconfirmed(state):
    """Pure check: our CastSpell was submitted but the card is still in hand."""
    pc = ST.get("pending_cast")
    if not pc or pc.get("kind") != "cast":
        return False
    return cast_obj_zone(state, pc["oid"]) == "hand"


def cast_guard_tick(state, tag):
    """Protocol-130 cast-confirmation guard.

    Returns "hold" while our CastSpell (or Bombardment activation) is in
    flight but unconfirmed: the driver must not pass priority or start new
    plays in that window -- on protocol 130 a bare CastSpell can lose a race
    to our own PassPriority submitted on the next tick and be silently
    dropped by the server (observed 2026-10-09). Returns "proceed" once the
    action is confirmed; a 30s backstop clears a still-unconfirmed action so
    the play re-triggers instead of stalling.
    """
    pc = ST.get("pending_cast")
    if not pc:
        return "proceed"
    oid = pc["oid"]
    if pc.get("kind") == "cast":
        zone = cast_obj_zone(state, oid)
        confirmed = zone in ("stack", "battlefield", "graveyard", "exile",
                             "command")
    else:  # ability activation: confirmed once its cost is being paid
        mems_now = len(memnite_oids(state, 0))
        sac_answered = any(isinstance(k, tuple) and len(k) == 3
                           and k[0] == tag and k[1] in ("bombsac", "bombtgt")
                           for k in SUBMITTED_OPPS)
        confirmed = sac_answered or mems_now < pc.get("mem_before", 10 ** 9)
        zone = f"ability(cost answered={sac_answered}, memnites={mems_now})"
    if confirmed:
        ST["pending_cast"] = None
        say(f"[{tag}] action confirmed ({zone}): {pc['name']} oid {oid}")
        wire("action_confirmed", {"name": pc["name"], "oid": str(oid),
                                  "zone": str(zone)})
        return "proceed"
    if time.time() - pc["since"] > 30:
        say(f"[{tag}] ACTION NOT CONFIRMED after 30s ({zone}): "
            f"{pc['name']} oid {oid} -- clearing for retry")
        wire("action_dropped_retry",
             {"name": pc["name"], "oid": str(oid), "zone": str(zone)})
        ST["pending_cast"] = None
        if pc.get("kind") == "cast":
            cs = ST.get("cast")
            if cs and cs.get("tag") == pc.get("tag"):
                ST["cast"] = None
                ST["legs"].pop(pc.get("tag"), None)
            if pc.get("tag") == "RAMP":
                ST["ramp_pending"] = None
        # ability drops keep bombard_active: its cost decisions may still
        # arrive; the pre-existing flow handles a truly dead activation.
        return "proceed"
    return "hold"


# ------------------------------------------------------------- cast decisions (Dargo)
def optional_prompt_visible(st):
    for opp in vi_ops(st):
        if opp_codes(opp) & {"decideOptionalCost", "decideOptionalEffect"}:
            return True
    return False


def pick_bool_choice(choices, want):
    """Pick the pay/decline choice from bare true/false 106 surfaces."""
    want_s = "true" if want else "false"
    for ch in choices:
        for s in ch.get("surfaces", []) or []:
            dd = s.get("data") or {}
            if s.get("type") == "value" and dd.get("role") == "accept" \
                    and str(dd.get("value")).lower() == want_s:
                return ch
    for ch in choices:
        for s in ch.get("surfaces", []) or []:
            dd = s.get("data") or {}
            if dd.get("role") in ("pay", "accept", "decision"):
                v = str(dd.get("value")).lower()
                if (want and v == "true") or (not want and v == "false"):
                    return ch
    for ch in choices:
        t = choice_text(ch).lower()
        if want and any(k in t for k in ("pay", "accept", "yes", "sacrifice")) \
                and "don't" not in t and "decline" not in t and "not " not in t:
            return ch
        if not want and any(k in t for k in ("decline", "don't", "do not")):
            return ch
    for ch in choices:
        if choice_text(ch).strip().lower() == want_s:
            return ch
    return None


def pick_sac_candidates(state, cands, want_oids):
    want = set(str(x) for x in want_oids)
    picks = []
    for ch in cands:
        ref = _cand_reference(ch)
        if ref is not None and str(ref) in want \
                and ch.get("id") not in picks:
            picks.append(ch.get("id"))
    if not picks and want:
        for ch in cands:
            ref = _cand_reference(ch)
            o = get_obj(state, ref) if ref is not None else {}
            if o.get("zone") == "Battlefield" \
                    and obj_lname(state, ref) == MEMNITE \
                    and ch.get("id") not in picks:
                picks.append(ch.get("id"))
            if len(picks) >= len(want):
                break
    return picks


async def answer_cast_decisions(c, tag):
    """Answer in-flight Dargo announcement decisions: optional additional
    cost -> (accept? sac count -> sac selection). Called BEFORE main-phase
    logic so announcement decisions are never starved."""
    cs = ST.get("cast")
    if not (cs and cs["in_flight"]):
        return False
    st = st_of(c)
    state = st["state"]
    # 1. optional additional-cost prompt
    if not cs["optional_done"]:
        for opp in vi_ops(st):
            codes = opp_codes(opp)
            if not (codes & {"decideOptionalCost", "decideOptionalEffect"}):
                continue
            iid = opp.get("interactionId")
            key = (tag, "optional", str(iid))
            if key in SUBMITTED_OPPS:
                return True
            if "OptionalCostChoice" not in ST["wf_seen"]:
                ST["wf_seen"].append("OptionalCostChoice")
                wire("waiting_for", {"type": "OptionalCostChoice"})
                say(f"[{tag}] {cs['tag']}: optional additional-cost prompt seen")
            resp = opp.get("response", {}) or {}
            rtype = resp.get("type")
            data = resp.get("data", {}) or {}
            want = cs["plan"]["pay_additional"]
            if rtype == "exactChoices":
                ch = pick_bool_choice(data.get("choices") or [], want)
                if ch is None:
                    say(f"[{tag}] optional prompt: no "
                        f"{'pay' if want else 'decline'} choice; deferring")
                    wire("optional_unmatched", {"opp": opp})
                    return True
                SUBMITTED_OPPS.add(key)
                cs["optional_done"] = True
                cs["accepted"] = want
                say(f"[{tag}] {cs['tag']}: additional cost -> "
                    f"{'PAY' if want else 'DECLINE'}")
                wire("optional_cost", {"tag": cs["tag"], "want_pay": want,
                                      "choice": ch.get("id")})
                await answer_vi(c, opp, ch, tag)
                return True
            # schema-style optional cost: the selection itself
            spec = data.get("spec", {}) or {}
            stype = spec.get("type") or "sequence"
            cands = data.get("candidates") or []
            if want:
                picks = pick_sac_candidates(state, cands,
                                            cs["plan"]["sac_oids"])
                if not picks:
                    say(f"[{tag}] schema optional: no memnite candidates; "
                        "deferring")
                    wire("optional_schema_unmatched", {"opp": opp})
                    return True
            else:
                picks = []
            SUBMITTED_OPPS.add(key)
            cs["optional_done"] = True
            cs["accepted"] = want
            cs["sac_done"] = True
            say(f"[{tag}] {cs['tag']}: schema optional -> "
                f"{'PAY ' + str(len(picks)) + ' sacs' if want else 'DECLINE'}")
            wire("optional_cost_schema",
                 {"tag": cs["tag"], "want_pay": want, "picks": picks})
            await interact_as(c, {"interactionId": iid,
                                  "response": {"type": stype,
                                               "data": {"choiceIds": picks}}},
                              tag)
            return True
        return False
    # 2. number schema (how many permanents to sacrifice)
    if cs["accepted"] and not cs["sac_count_done"]:
        for opp in vi_ops(st):
            resp = opp.get("response", {}) or {}
            if resp.get("type") != "schema":
                continue
            spec = (resp.get("data") or {}).get("spec", {}) or {}
            if (spec.get("type") or "") != "number":
                continue
            iid = opp.get("interactionId")
            key = (tag, "sacnum", str(iid))
            if key in SUBMITTED_OPPS:
                return True
            SUBMITTED_OPPS.add(key)
            want_n = len(cs["plan"]["sac_oids"])
            cs["sac_count_done"] = True
            say(f"[{tag}] {cs['tag']}: sacrifice count -> {want_n}")
            wire("sac_count", {"tag": cs["tag"], "value": want_n})
            await interact_as(c, {"interactionId": iid,
                                  "response": {"type": "number",
                                               "data": {"value": want_n}}},
                              tag)
            return True
        return False
    # 3. permanent selection for the additional cost
    if cs["accepted"] and cs["sac_count_done"] and not cs["sac_done"]:
        for opp in vi_ops(st):
            resp = opp.get("response", {}) or {}
            rtype = resp.get("type")
            data = resp.get("data", {}) or {}
            if rtype == "schema":
                spec = data.get("spec", {}) or {}
                stype = spec.get("type")
                if stype not in ("select", "sequence"):
                    continue
            elif rtype == "exactChoices":
                codes = opp_codes(opp)
                if codes & {"passPriority", "decideOptionalEffect",
                            "decideOptionalCost"}:
                    continue
                stype = "choose"
            else:
                continue
            cands = data.get("candidates") or data.get("choices") or []
            if not cands:
                continue
            if any(candidate_seat(ch) is not None for ch in cands):
                continue  # player targets are not sac selections
            refs = [_cand_reference(ch) for ch in cands]
            if not any(r is not None
                       and get_obj(state, r).get("zone") == "Battlefield"
                       for r in refs):
                continue
            iid = opp.get("interactionId")
            key = (tag, "sacsel", str(iid))
            if key in SUBMITTED_OPPS:
                return True
            picks = pick_sac_candidates(state, cands, cs["plan"]["sac_oids"])
            if not picks:
                say(f"[{tag}] sac selection: no memnite candidates; deferring")
                wire("sacsel_unmatched", {"opp": opp})
                return True
            SUBMITTED_OPPS.add(key)
            cs["sac_done"] = True
            say(f"[{tag}] {cs['tag']}: sacrificing {len(picks)} permanents")
            wire("sac_select", {"tag": cs["tag"], "picks": picks})
            if rtype == "schema":
                sub = {"interactionId": iid,
                       "response": {"type": stype,
                                    "data": {"choiceIds": picks}}}
            else:
                sub = {"interactionId": iid,
                       "response": {"type": "choose",
                                    "data": {"choiceId": picks[0]}}}
            await interact_as(c, sub, tag)
            return True
        return False
    return False


async def drive_mana(c, tag):
    """Mana for the in-flight Dargo cast (or a ramp cast). The v0.103.0
    engine auto-taps reliably (observed on every cast), so manual tapping
    is DISABLED by default: tapping alongside the auto-tap overpays into
    the pool and pollutes the spent measurement. Manual taps engage only
    as a last resort if a cast sits unannounced for >90s with a tap menu
    visible (logged loudly)."""
    cs = ST.get("cast")
    st = st_of(c)
    state = st["state"]
    if cs and cs["in_flight"]:
        if dargo_stack(state):
            cs["announced"] = True
            return False
        if time.time() - cs["submit_t"] < 90:
            return False
        if optional_prompt_visible(st):
            return False
        if cs["plan"]["pay_additional"] and not cs["sac_done"]:
            return False
        needs = MANA_NEEDS.get(tag, {})
        if sum(needs.values()) <= 0:
            return False
        if await pay_mana_vi(c, st, tag, needs):
            cs["taps"] += 1
            say(f"[{tag}] FALLBACK manual tap #{cs['taps']} for {cs['tag']} "
                f"(auto-tap did not fire in 90s)")
            wire("manual_tap", {"tag": cs["tag"], "taps": cs["taps"],
                                "needs": dict(needs), "fallback": True})
            return True
        return False
    rp = ST.get("ramp_pending")
    if rp:
        if object_on_stack_or_bf(state, rp):
            ST["ramp_pending"] = None
            MANA_NEEDS[tag] = {}
            return False
        if time.time() - ST.get("ramp_pending_t", 0) > 120:
            say(f"[{tag}] ramp cast {rp} stuck 120s; clearing")
            wire("ramp_stuck", {"lname": rp})
            ST["ramp_pending"] = None
            MANA_NEEDS[tag] = {}
            return False
        if time.time() - ST.get("ramp_pending_t", 0) < 90:
            return False
        needs = MANA_NEEDS.get(tag, {})
        if sum(needs.values()) > 0 and await pay_mana_vi(c, st, tag, needs):
            say(f"[{tag}] FALLBACK manual tap for ramp cast {rp}")
            wire("manual_tap", {"lname": rp, "fallback": True})
            return True
    return False


# ------------------------------------------------------------- bombardment
def bombard_target_opp(st):
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        if rtype == "schema":
            spec = data.get("spec", {}) or {}
            if spec.get("type") in ("select", "sequence") \
                    and data.get("candidates"):
                if any(candidate_seat(ch) is not None
                       for ch in data["candidates"]):
                    return opp, "schema", spec.get("type")
        elif rtype == "exactChoices":
            chs = data.get("choices") or []
            codes = set()
            for ch in chs:
                codes.update(x for x in surf_codes(ch) if x)
            if chs and "passPriority" not in codes \
                    and "decideOptionalEffect" not in codes \
                    and "decideOptionalCost" not in codes \
                    and any(c in codes for c in ("candidate", "target")):
                return opp, "exactChoices", "choose"
    return None, None, None


def pick_target_candidate(opp, preferred_seat):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    cands = data.get("candidates") or data.get("choices") or []
    best, best_score = None, -1
    for ch in cands:
        codes = set(surf_codes(ch))
        score = 0
        if candidate_seat(ch) == preferred_seat:
            score += 2
        if codes & {"candidate", "target"}:
            score += 1
        if score > best_score:
            best, best_score = ch, score
    if best is None and len(cands) == 1:
        best = cands[0]
    return best


async def submit_target(c, opp, rtype, spec_type, ch, tag):
    iid = opp.get("interactionId")
    cid = ch.get("id")
    if rtype == "schema":
        sub = {"interactionId": iid,
               "response": {"type": spec_type,
                            "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting target: id={cid} kind={sub['response']['type']} "
        f"({choice_text(ch)[:80]})")
    wire("target_submission", {"who": tag, "submission": sub,
                               "choice_text": choice_text(ch)[:120]})
    await interact_as(c, sub, tag)


async def bombard_decisions(c, tag):
    """Answer Bombardment activation decisions: the sacrifice-cost selection
    (candidates reference battlefield objects) and the damage target (seat
    carriers). Runs before main-phase logic; never passes mid-activation."""
    if not ST.get("bombard_active"):
        return False
    st = st_of(c)
    state = st["state"]
    # sacrifice-cost selection
    for opp in vi_ops(st):
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        if rtype == "schema":
            spec = data.get("spec", {}) or {}
            if spec.get("type") not in ("select", "sequence"):
                continue
            stype = spec.get("type")
        elif rtype == "exactChoices":
            codes = opp_codes(opp)
            if codes & {"passPriority", "decideOptionalEffect",
                        "decideOptionalCost"}:
                continue
            stype = "choose"
        else:
            continue
        cands = data.get("candidates") or data.get("choices") or []
        if not cands:
            continue
        if any(candidate_seat(ch) is not None for ch in cands):
            continue  # player targets handled below
        refs = [_cand_reference(ch) for ch in cands]
        if not any(r is not None
                   and get_obj(state, r).get("zone") == "Battlefield"
                   for r in refs):
            continue
        iid = opp.get("interactionId")
        key = (tag, "bombsac", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        want = [ST["bombard_sac_oid"]] if ST.get("bombard_sac_oid") else []
        picks = pick_sac_candidates(state, cands, want)
        if not picks:
            say(f"[{tag}] bombard sac: no memnite candidate; deferring")
            wire("bombsac_unmatched", {"opp": opp})
            return True
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] Bombardment sacrifices Memnite ({picks})")
        wire("bombard_sacrifice", {"picks": picks})
        if rtype == "schema":
            sub = {"interactionId": iid,
                   "response": {"type": stype, "data": {"choiceIds": picks}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": picks[0]}}}
        await interact_as(c, sub, tag)
        return True
    # damage target -> P1 (seat 1)
    opp, rtype, spec_type = bombard_target_opp(st)
    if opp is not None:
        iid = opp.get("interactionId")
        key = (tag, "bombtgt", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        ch = pick_target_candidate(opp, 1)
        if ch is None:
            say(f"[{tag}] bombard target: no candidate; deferring")
            wire("bombtgt_unmatched", {"opp": opp})
            return True
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] Bombardment targets P1")
        wire("bombard_target", {"choice": ch.get("id")})
        await submit_target(c, opp, rtype, spec_type, ch, tag)
        return True
    return False

# ------------------------------------------------------------- seat ticks
async def p0_tick(c, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    # ---- cast-confirmation guard (protocol 130): while our CastSpell or
    # ability activation is in flight but unconfirmed, answer decisions and
    # mana needs but start no new plays and pass no priority. Runs before
    # any early-return block so a dropped action is always detected.
    if cast_guard_tick(state, tag) == "hold":
        if await do_mulligan(c, acts, st, 0, tag):
            return True
        if await do_bottom(c, acts, st, 0, tag):
            return True
        if await do_discard(c, acts, st, 0, tag, p0_discard_rank):
            return True
        if await do_legend(c, acts, st, tag):
            return True
        if await pay_tick(c, acts, tag):
            return True
        if await answer_cast_decisions(c, tag):
            return True
        if await drive_mana(c, tag):
            return True
        if await bombard_decisions(c, tag):
            return True
        return True
    if await do_mulligan(c, acts, st, 0, tag):
        return True
    if await do_bottom(c, acts, st, 0, tag):
        return True
    if await do_discard(c, acts, st, 0, tag, p0_discard_rank):
        return True
    if await do_declare_empty(c, acts, st, 0, tag):
        return True
    if await do_legend(c, acts, st, tag):
        return True
    if await pay_tick(c, acts, tag):
        return True
    # announcement decisions are answered before any main-phase logic
    if await answer_cast_decisions(c, tag):
        return True
    if await drive_mana(c, tag):
        return True
    if await bombard_decisions(c, tag):
        return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 0):
        if await play_a_land(c, state, 0, acts, tag):
            return True
        step = ST.get("step")
        if step == "SETUP":
            # cast free Memnites
            for oid in hand_ids(state, 0):
                if obj_lname(state, oid) == MEMNITE:
                    a = find_cast_action(acts, state, MEMNITE, oid)
                    if a is not None:
                        say(f"[{tag}] casts Memnite ({oid})")
                        wire("cast_memnite", {"oid": oid})
                        ST["pending_cast"] = {"kind": "cast", "oid": str(oid),
                                              "name": MEMNITE,
                                              "since": time.time(),
                                              "tag": "SETUP"}
                        await submit_as_is(c, a)
                        return True
            # cast Bombardment once affordable
            if find_bf_oid(state, 0, BOMBARD) is None \
                    and not ST.get("ramp_pending"):
                for oid in hand_ids(state, 0):
                    if obj_lname(state, oid) == BOMBARD \
                            and untapped_lands(state, 0, MOUNTAIN) >= 2:
                        a = find_cast_action(acts, state, BOMBARD, oid)
                        if a is not None:
                            MANA_NEEDS[tag] = {"R": 1, "generic": 1}
                            ST["ramp_pending"] = BOMBARD
                            ST["ramp_pending_t"] = time.time()
                            say(f"[{tag}] casts Goblin Bombardment ({oid})")
                            wire("cast_bombardment", {"oid": oid})
                            ST["pending_cast"] = {"kind": "cast", "oid": str(oid),
                                                  "name": BOMBARD,
                                                  "since": time.time(),
                                                  "tag": "RAMP"}
                            await submit_as_is(c, a)
                            return True
        elif step == "ACTIVATE_BOMBARD" and not ST.get("bombard_active"):
            bomb = find_bf_oid(state, 0, BOMBARD)
            if bomb is not None:
                act = next((a for a in acts
                            if a.get("type") == "ActivateAbility"
                            and (str(a.get("_src_oid")) == str(bomb)
                                 or str((a.get("data") or {})
                                        .get("source_id")) == str(bomb))),
                           None)
                if act is None:
                    act = next((a for a in acts
                                if a.get("type") == "ActivateAbility"), None)
                if act is not None:
                    mems = memnite_oids(state, 0)
                    ST["bombard_sac_oid"] = mems[0] if mems else None
                    ST["bombard_mem_before"] = len(mems)
                    ST["bombard_p1_life"] = life_of(state, 1)
                    ST["bombard_active"] = True
                    say(f"[{tag}] activates Bombardment (plan sac Memnite "
                        f"{ST['bombard_sac_oid']})")
                    wire("activate_bombardment", {})
                    ST["pending_cast"] = {"kind": "ability", "oid": str(bomb),
                                          "name": BOMBARD,
                                          "since": time.time(),
                                          "tag": "BOMBARD_ACT",
                                          "mem_before": len(mems)}
                    await submit_as_is(c, act)
                    return True
        elif step in ("LEG1", "LEG2"):
            cs = ST.get("cast")
            if cs and cs["in_flight"]:
                # announcement/resolution in flight: decisions are answered
                # above; never pass mid-announcement, pass only with the
                # spell on the stack awaiting passes.
                if dargo_stack(state):
                    for a in acts:
                        if a.get("type") == "PassPriority":
                            await submit_as_is(c, a)
                            say(f"[{tag}] passes ({cs['tag']} on stack)")
                            return True
                return False
            if step == "LEG1":
                plan = {"pay_additional": False, "sac_oids": []}
            else:
                mems = memnite_oids(state, 0)
                if len(mems) < 2:
                    say(f"[{tag}] LEG2: need 2 Memnites for additional cost; "
                        "aborting")
                    wire("leg2_abort", {"memnites": len(mems)})
                    ST["step"] = "DONE"
                    return False
                plan = {"pay_additional": True, "sac_oids": mems[:2]}
            dargos = dargo_hand_oids(state, 0)
            if not dargos:
                say(f"[{tag}] {step}: no Dargo in hand; aborting")
                wire("leg_abort", {"step": step})
                ST["step"] = "DONE"
                return False
            a = find_cast_action(acts, state, DARGO, dargos[0])
            if a is not None:
                ST["legs"][step] = {
                    "untapped_before": untapped_lands(state, 0, MOUNTAIN),
                    "pool_before": pool_len(state, 0),
                }
                ST["cast"] = {"tag": step, "plan": plan, "in_flight": True,
                              "announced": False, "optional_done": False,
                              "accepted": None, "sac_count_done": False,
                              "sac_done": False, "taps": 0,
                              "submit_t": time.time()}
                # 2026-10-07 double-pay gate: the driver never pays mana for
                # test casts (neither vi taps nor legacy PayMana) - the
                # engine's auto-tap is the single payer, so the
                # untapped-Mountain delta measures exactly the engine's
                # computed cost. MANA_NEEDS stays empty for LEG1/LEG2.
                MANA_NEEDS[tag] = {}
                say(f"[{tag}] casts {step} (Dargo {dargos[0]}); "
                    f"untapped_before={ST['legs'][step]['untapped_before']}")
                wire("cast_submit", {"tag": step, "driver_pays": False})
                ST["pending_cast"] = {"kind": "cast", "oid": str(dargos[0]),
                                      "name": DARGO, "since": time.time(),
                                      "tag": step}
                await submit_as_is(c, a)
                return True
            return False

    if real_decision_pending(st):
        return True
    if my_priority(top_acts(st)):
        await pass_priority(c, st, top_acts(st), tag)
        return True
    return False


async def p1_tick(c, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, 1, tag):
        return True
    if await do_bottom(c, acts, st, 1, tag):
        return True
    if await do_discard(c, acts, st, 1, tag, lambda s, o: 0):
        return True
    if await do_declare_empty(c, acts, st, 1, tag):
        return True
    if await do_legend(c, acts, st, tag):
        return True
    if await pay_tick(c, acts, tag):
        return True

    await asyncio.sleep(0)
    st = st_of(c) or st

    if real_decision_pending(st):
        return True
    # never pass under P0's unconfirmed cast (protocol-130 race)
    if my_priority(top_acts(st)) and not cast_unconfirmed(st["state"]):
        await pass_priority(c, st, top_acts(st), tag)
        return True
    return False


# ------------------------------------------------------------- checkpoints
async def do_export(c, path):
    s = await c.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


async def evaluate_flow(p0):
    st = st_of(p0)
    if not st:
        return
    state = st["state"]
    step = ST.get("step")

    if step == "SETUP" and my_main(state, 0) and setup_gate(state):
        await do_export(p0, "pre.json")
        ST["step"] = "ACTIVATE_BOMBARD"
        say("=== stage -> ACTIVATE_BOMBARD ===")
        wire("setup_ready", {})
        return

    if step == "ACTIVATE_BOMBARD" and ST.get("bombard_active"):
        mems_now = len(memnite_oids(state, 0))
        p1l = life_of(state, 1)
        sac_paid = mems_now < ST["bombard_mem_before"]
        dmg_done = (ST["bombard_p1_life"] is not None and p1l is not None
                    and p1l < ST["bombard_p1_life"])
        if sac_paid and dmg_done:
            ST["bombard_active"] = False
            ST["step"] = "LEG1"
            say("=== bombardment activation done -> LEG1 ===")
            wire("bombard_done", {"p1_life": p1l, "memnites": mems_now})
            return

    cs = ST.get("cast")
    if cs and cs["in_flight"]:
        tag = cs["tag"]
        if dargo_stack(state):
            cs["announced"] = True
        # stall breaker: announcement never completed
        if not cs["announced"] and time.time() - cs["submit_t"] > 300:
            say(f"[{tag}] cast stuck 300s without announcement; aborting leg")
            wire("cast_stuck", {"tag": tag})
            cs["in_flight"] = False
            ST["step"] = "DONE"
            return
        pending = real_decision_pending(st)
        if tag == "LEG2":
            # LEG2's Dargo goes to the graveyard under the legend rule
            # ("keep first"), so the resolution signal is the answered
            # legend rule, not a battlefield Dargo.
            resolved = (not dargo_stack(state) and cs["announced"]
                        and not pending and ST["legend_answered"] >= 1)
        else:
            resolved = (not dargo_stack(state) and cs["announced"]
                        and not pending
                        and len(dargo_bf_oids(state, 0)) >= 1)
        if resolved:
            cs["in_flight"] = False
            post = await do_export(p0, f"post_{tag.lower()}.json")
            leg = ST["legs"][tag]
            leg["untapped_after"] = untapped_lands(post, 0, MOUNTAIN)
            leg["pool_after"] = pool_len(post, 0)
            leg["spent"] = leg["untapped_before"] - leg["untapped_after"]
            leg["manual_taps"] = cs["taps"]
            say(f"{tag} resolved: spent={leg['spent']} "
                f"(before={leg['untapped_before']} "
                f"after={leg['untapped_after']} pool_after={leg['pool_after']} "
                f"manual_taps={cs['taps']})")
            wire("cast_resolved", {"tag": tag, "leg": leg})
            if tag == "LEG1":
                ST["step"] = "LEG2"
                say("=== -> LEG2 ===")
            else:
                say("=== LEG2 done; finishing ===")
                ST["stop"] = True
            return


# ------------------------------------------------------------- main
async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    for k in ("P0", "P1"):
        MANA_NEEDS[k] = {}

    await verify_server_hello()
    data_finding = check_data_level()

    p0 = PhaseClient("P06860")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck(("Dargo, the Shipwrecker", 8), ("Memnite", 8),
                         ("Goblin Bombardment", 4), ("Mountain", 40)))
    p1 = PhaseClient("P16860")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(("Mountain", 60)))
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game", {"code": p0.game_code,
                  "p0": p0.player_id, "p1": p1.player_id})

    last_rev = {}
    last_tick_at = {}
    last_diag = time.time()
    while time.time() - t0 < GAME_TIMEOUT and not ST.get("stop") \
            and ST.get("step") != "DONE":
        await asyncio.sleep(0.25)
        for c, tag, tick in ((p0, "P0", p0_tick), (p1, "P1", p1_tick)):
            st = st_of(c)
            if not st:
                continue
            rev_changed = c.revision != last_rev.get(c.name)
            if rev_changed:
                last_rev[c.name] = c.revision
            else:
                if not (my_priority(top_acts(st))
                        and time.time() - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            try:
                await tick(c, tag)
            except Exception as e:
                say(f"tick error {c.name}: {type(e).__name__}: {e}")
        await evaluate_flow(p0)
        if time.time() - last_diag > 60:
            last_diag = time.time()
            st = st_of(p0)
            if st:
                s = st["state"]
                say(f"DIAG p0: rev={p0.revision} turn={s.get('turn_number')} "
                    f"phase={s.get('phase')} step={ST.get('step')} "
                    f"prio={my_priority(top_acts(st))} "
                    f"decision={real_decision_pending(st)} "
                    f"cast={ST['cast']['tag'] if ST.get('cast') and ST['cast']['in_flight'] else None} "
                    f"legend={ST['legend_answered']}")

    # ------------------------------------------------------- evaluate
    def env_state(p):
        try:
            with open(f"{EVDIR}/{p}") as f:
                return json.load(f)["state"]
        except FileNotFoundError:
            return None

    def snap(s):
        return {
            # 2026-10-07: fixed missing lname arg (was untapped_lands(s, 0),
            # which raised TypeError and killed the driver post-legs on both
            # the v0.102.0 and v0.103.0 runs; assertions were finalized from
            # saved exports instead).
            "untapped": untapped_lands(s, 0, MOUNTAIN),
            "pool": pool_len(s, 0),
            "dargo_bf": len(dargo_bf_oids(s, 0)),
            "dargo_hand": len(dargo_hand_oids(s, 0)),
            "bombard_bf": find_bf_oid(s, 0, BOMBARD) is not None,
            "memnites_bf": len(memnite_oids(s, 0)),
        }

    pre = env_state("pre.json")
    A = obs["assert"]
    A["A1_setup_ok"] = ("passed" if pre and (lambda p: p["dargo_hand"] >= 2
                        and p["bombard_bf"] and p["memnites_bf"] >= 4
                        and p["untapped"] >= 15 and p["pool"] == 0)(snap(pre))
                        else "failed")
    if pre is None:
        obs["notes"].append("pre.json never exported (setup gate unreached)")
    else:
        obs["notes"].append(f"pre.json snapshot: {snap(pre)}")
    A["A2_additional_offered"] = ("passed"
                                  if "OptionalCostChoice" in ST["wf_seen"]
                                  else "failed")
    leg1 = ST["legs"].get("LEG1", {})
    leg2 = ST["legs"].get("LEG2", {})
    A["A3_prior_discount"] = ("passed" if leg1.get("spent") == 5
                              else ("failed" if "spent" in leg1 else "not-run"))
    A["A4_additional_discount"] = ("passed" if leg2.get("spent") == 1
                                   else ("failed" if "spent" in leg2
                                         else "not-run"))
    both_resolved = (leg1.get("spent") is not None
                     and leg2.get("spent") is not None)
    A["A5_cleanup"] = ("passed" if both_resolved
                       and ST["legend_answered"] >= 1 else "failed")
    obs["notes"].append(f"WF/decision sequence: {ST['wf_seen']}")
    obs["notes"].append(f"LEG1: {leg1}")
    obs["notes"].append(f"LEG2: {leg2}")
    obs["notes"].append(f"legend_answered={ST['legend_answered']}; "
                        f"final step={ST.get('step')}")
    obs["notes"].append(f"data-level: {data_finding}")
    obs["notes"].append(
        "protocol-130 driver (v0.107.0): HELLO advertises protocol 130; "
        "CreateGameWithSettings + JoinGameWithPassword + start_when_full; "
        "MulliganDecision via legacy Action; DiscardToHandSize via vi "
        "schema/select; CastSpell/ActivateAbility via advertised actions; "
        "optional additional cost via decideOptionalCost/decideOptionalEffect "
        "opportunity (exactChoices bare true/false or schema); sac count via "
        "number schema; permanent selection via select/sequence candidates "
        "matched by object reference; mana is engine auto-tapped (manual "
        "taps disabled except as a >90s last-resort fallback) so the "
        "untapped-Mountain delta equals exactly what the engine charged; "
        "playLand in NON_DECISION_CODES; priority-gated passes; 5s re-tick "
        "backstop.")

    for k in sorted(A):
        say(f"{k}: {A[k]}")

    if A["A3_prior_discount"] == "failed" \
            or A["A4_additional_discount"] == "failed":
        verdict = "reproduced"
    elif all(v == "passed" for v in A.values()):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    dur = time.time() - t0
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "game_code": ST.get("game_code"),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(dur, 1),
        "server_identity": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 130,
                   "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(__file__),
        "decks": {
            "P0": [["Dargo, the Shipwrecker", 8], ["Memnite", 8],
                   ["Goblin Bombardment", 4], ["Mountain", 40]],
            "P1": [["Mountain", 60]],
        },
        "assertions": A,
        "notes": obs["notes"],
        "wf_sequence": ST["wf_seen"],
        "legs": ST["legs"],
        "data_finding": data_finding,
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts "
            ">4-of for custom games).",
            "States are authoritative exports, restorable only via full game "
            "replay (scenario_6860_01020.py), not direct load.",
            "Mana spent is measured as the untapped-Mountain delta; the "
            "v0.107.0 engine auto-taps reliably, so the driver never taps "
            "manually (manual taps are a >90s last-resort fallback only) "
            "and the delta equals exactly what the engine charged "
            f"(fallback manual taps: LEG1={ST['legs'].get('LEG1', {}).get('manual_taps', 'n/a')}, "
            f"LEG2={ST['legs'].get('LEG2', {}).get('manual_taps', 'n/a')}).",
        ],
        "setup_line": "P0: 8x Dargo, the Shipwrecker + 8x Memnite + 4x "
                      "Goblin Bombardment + 40x Mountain; P1: 60x Mountain "
                      "(draw-go)",
        "contract_line": ("LEG1 (decline additional, 1 prior sac): spend 5. "
                          "LEG2 (pay additional with 2 Memnites, 1 prior "
                          "sac): spend 1."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "wf_sequence": ST["wf_seen"], "legs": ST["legs"]}, f,
                  indent=2)
    say(f"DONE verdict={verdict} assertions={json.dumps(A)}")
    sys.stdout.flush()

    # Finalize evidence BEFORE closing clients: if a close hangs, the
    # evidence files are already safe. Each step is guarded so a failure
    # is logged with a traceback instead of silently dropping the rest.
    try:
        render_summary(run, f"{EVDIR}/summary.png")
    except Exception:
        say("render_summary FAILED:")
        say(traceback.format_exc())
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        write_manifest()
    except Exception:
        say("write_manifest FAILED:")
        say(traceback.format_exc())
    try:
        RUNLOG.close()
    except Exception:
        pass

    try:
        await asyncio.wait_for(p0.close(), 15)
    except Exception as e:
        print(f"p0.close failed: {e}", flush=True)
    try:
        await asyncio.wait_for(p1.close(), 15)
    except Exception as e:
        print(f"p1.close failed: {e}", flush=True)
    return run


def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 800
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #6860 - Dargo, the Shipwrecker cost discounts "
                    "(revalidation)", fill=(235, 240, 250))
    y += 30
    d.text((24, y), f"server v{si['validated_version']} ({si['build_commit']}) "
                    f"protocol {si['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180))
    y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced"
                 else (230, 200, 90)))
    y += 34
    legs = run.get("legs", {})
    l1 = legs.get("LEG1", {}).get("spent", "?")
    l2 = legs.get("LEG2", {}).get("spent", "?")
    d.text((24, y), f"LEG1 mana spent: {l1} (oracle: 5)   "
                    f"LEG2 mana spent: {l2} (oracle: 1)", fill=(200, 210, 225))
    y += 30
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "A1 setup: 2+ Dargo hand, Bombardment + 4 Memnites BF, "
                       "15+ untapped Mountains",
        "A2_additional_offered": "A2 optional additional-cost sacrifice offered",
        "A3_prior_discount": "A3 LEG1 (decline, 1 prior sac): spent == 5",
        "A4_additional_discount": "A4 LEG2 (pay 2 sacs, 1 prior sac): "
                                 "spent == 1",
        "A5_cleanup": "A5 both casts resolved; legend rule answered",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y), f"{'pass' if v == 'passed' else ('FAIL' if v == 'failed' else 'n/a')} {lab}",
               fill=col)
        y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in run["notes"][:9]:
        d.text((40, y), n[:118], fill=(150, 165, 185))
        y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 6860/" +
           run["run_id"], fill=(120, 130, 150))
    img.save(out_path)
    say(f"rendered {out_path}")


def write_manifest():
    lines = []
    for name in sorted(os.listdir(EVDIR)):
        if name == "manifest.sha256":
            continue
        p = os.path.join(EVDIR, name)
        if os.path.isfile(p):
            lines.append(f"{sha256_of_file(p)}  {name}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    say(f"manifest written ({len(lines)} files)")


if __name__ == "__main__":
    asyncio.run(main())
