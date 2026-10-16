#!/usr/bin/env python3
"""Issue #10028 on pinned v0.110.0 (protocol 142): Oath of Teferi.

Reported: Oath of Teferi's "You may activate the loyalty abilities of
planeswalkers you control twice each turn rather than only once" line
parses to Effect::Unimplemented in the pinned card data, so planeswalkers
stay once-per-turn. Only the ETB blink works. Tracked for fix in #10005.

Oracle (Oath of Teferi): "When Oath of Teferi enters, exile another target
permanent you control. Return it to the battlefield under its owner's
control at the beginning of the next end step. / You may activate the
loyalty abilities of planeswalkers you control twice each turn rather
than only once."

Pinned v0.110.0 card-data parse: the ETB ability parses fine; the
twice line is a Spell-kind ability whose effect is
Unimplemented("effect_structure", description="Effect sentence candidate
but line failed effect parser: You may activate the loyalty abilities of
planeswalkers you control twice each turn rather than only once.").
Recorded in data_evidence.json.

Planeswalker pick: Ajani Goldmane ({2}{W}{W}, loyalty 4, NO triggers or
statics): abilities [+1] GainLife (implemented), [-1] PutCounterAll
(implemented), [-6] Unimplemented. The +1 is the activation under test:
loyalty delta 4->5 and life 20->22 are both assertable.

Plan (two human seats, native engine, v0.110.0 / protocol 142):
  GAME A (oath): P0 20x Plains / 16x Island / 4x Ajani Goldmane / 4x Oath of
    Teferi; P1 passive 30x Plains / 30x Island.
    SETUP: develop lands, cast Ajani, cast Oath. Oath ETB trigger target =
      a LAND (Plains preferred; the walker must stay on the battlefield so
      its per-turn activation count is not reset by becoming a new object).
    PRE export (Oath + Ajani on BF, loyalty 4, life 20/20).
    ACT1: activate Ajani +1 (advertised ActivateAbility, cost Loyalty 1);
      assert it resolves: loyalty 4->5, life 20->22.
    ACT2 (reported branch): attempt a 2nd +1 activation the same turn.
      Refusal is recorded either as absence of the advertised
      ActivateAbility (+ a vi/legal_actions snapshot) or as a structured
      rejection of a re-submitted stale action.
    ACT3: attempt a 3rd +1 activation (must be refused as well).
    BLINK: pass to the next end step; the exiled land must return to the
      battlefield (ETB-blink regression).
  GAME B (control, no Oath): P0 32x Plains / 4x Ajani Goldmane; P1 passive.
    Cast Ajani, activate +1 once (assert works), attempt 2nd (assert
    refused). This validates that the test itself is meaningful: if the
    engine allowed unlimited activations without Oath, the verdict would
    be blocked, not reproduced.

Behavioral contract:
  A1 setup_ok      pre.json: Oath + Ajani on BF, Ajani loyalty 4,
                   life 20/20, P0 PreCombatMain
  A2 first_plus1   ACT1 advertised and accepted; resolves to loyalty 5,
                   life 22 (mid1.json)
  A3 second_refused 2nd +1 refused (not advertised, or advertised
                   submission rejected) (mid2.json / refused_2nd.json)
  A4 third_refused  3rd +1 refused
  A5 etb_blink     exiled land back on BF (zone Battlefield, controller 0)
                   by the next end step (post.json)
  B1 control_first_ok control game: +1 works (loyalty 4->5, life 20->22)
  B2 control_second_refused control game: 2nd +1 refused

Verdict = reproduced iff A1,A2,A3,A4,A5,B1,B2 all pass (with Oath out the
2nd activation is refused, exactly the reported outcome).
not-reproduced iff A1,A2 pass and the with-Oath 2nd activation is ACCEPTED
(loyalty 5->6, life 22->24) with the 3rd refused, and B1,B2,A5 pass.
blocked iff the control does not hold (unlimited activations without Oath),
or the fixture cannot reach the trigger (e.g. GainLife unimplemented,
loyalty never paid).

Protocol-142 driver (ported from scenario_6866_01100.py):
  - HELLO advertises protocol 142 (exact match); CreateGameWithSettings +
    JoinGameWithPassword + start_when_full; gate on viewer_interaction
    (waiting_for is null on 142); export response data.state is a JSON
    string parsed once.
  - MulliganDecision via legacy Action; BottomCards/DiscardToHandSize via
    vi schema/select; CastSpell via advertised actions as-is; engine
    auto-taps mana (bare CastSpell, cast-confirmation guard after every
    CastSpell; never inject targets into CastSpell -- choose via the vi
    TargetSelection opportunity afterwards).
  - Oath ETB target prompt: vi schema/exactChoices candidates referencing
    BF permanents controlled by seat 0, answered with a land.
  - ActivateAbility: advertised actions merged from legal_actions_by_object
    (_src_oid injected); matched on source oid + Loyalty cost amount.
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
ISSUE = 10028
RUN_ID = "run-10028-v01100-20261015-2241"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"
WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")
RUNS_DIR = f"{BACKFILL}/runs/{RUN_ID}"
os.makedirs(RUNS_DIR, exist_ok=True)

GAME_TIMEOUT = 2400
STALL_AFTER = 150

OATH = "Oath of Teferi"
AJANI = "Ajani Goldmane"
PLAINS = "Plains"
ISLAND = "Island"

P0_DECK_OATH = [(PLAINS, 20), (ISLAND, 16), (AJANI, 4), (OATH, 4)]
P0_DECK_CTRL = [(PLAINS, 32), (AJANI, 4)]
P1_DECK = [(PLAINS, 30), (ISLAND, 30)]

ST = {
    "game_code": None, "mulls": {}, "legend_answered": 0,
    "opp_shapes_logged": set(),
}
SUBMITTED_OPPS = set()
CAST_PENDING = {}
LAND_PLAYED_TURN = {}
PASSED_REV = {}


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(line + "\n")
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
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


SERVER_IDENTITY = {
    "validated_version": "v0.110.0",
    "build_commit": "9bfd12e",
    "protocol_version": 142,
    "server_binary_sha256": "346ad6a1a8cd887bb49108fa132476bea36e42e7cfc5116fe2cbdf8ad369e3f2",
    "card_data_sha256": "c77b20203d2c21a497e0281d4ebee7a9ad7cb19d8a88c62c45298c7f9a606d74",
    "draft_pools_sha256": "7e45abbf0a964959192d2528c067a2a9caab04438640ab63b9ad79d2ce0e4676",
    "signature_verified": True,
}

for _f, _k in (("server/releases/v0.110.0/phase-server-slim-x86_64-unknown-linux-musl",
                "server_binary_sha256"),
               ("server/releases/v0.110.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.110.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
say("server identity hashes verified against on-disk pinned artifacts")

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.110.0/data/card-data.json"))

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


def find_hand(state, pid, name):
    for o in hand_ids(state, pid):
        if oname(get_obj(state, o)) == name:
            return o
    return None


def loyalty_of(state, oid):
    return get_obj(state, oid).get("loyalty")


def life_of(state, pid):
    return player_obj(state, pid).get("life")


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def zone_of(state, oid):
    return str(get_obj(state, oid).get("zone") or "")


def object_on_stack_or_bf(state, lname):
    for o in (state.get("objects") or {}).values():
        if str(o.get("base_name") or o.get("name") or "").lower() == lname \
                and o.get("zone") in ("Stack", "Battlefield"):
            return True
    return False


# ------------------------------------------------------------- vi helpers
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


def vi_kind_code(st):
    vi = st.get("viewer_interaction") or {}
    return (vi.get("waitingForKind") or {}).get("code") or ""


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
    say(f"ServerHello: version={ver} build={build} protocol={proto} "
        f"mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.110.0"), f"unexpected version {ver}"
    assert int(proto) == 142, f"unexpected protocol {proto}"
    assert str(build) == "9bfd12e", f"unexpected build {build}"


def check_data_level():
    """Record how Oath of Teferi and Ajani Goldmane parse in the pinned
    v0.110.0 card data."""
    oath = CARD_DATA.get("oath of teferi", {})
    ajani = CARD_DATA.get("ajani goldmane", {})
    oath_static = oath.get("static_abilities", [])
    unimpl_twice = [a for a in oath_static
                    if json.dumps(a.get("effect", {}), default=str)
                    .find("Unimplemented") >= 0
                    and "twice each turn" in str(a.get("description", ""))]
    ajani_plus1 = [a for a in (ajani.get("abilities", []) or [])
                   if (a.get("cost") or {}).get("type") == "Loyalty"
                   and (a.get("cost") or {}).get("amount") == 1]
    note = {
        "oath_twice_line_unimplemented": len(unimpl_twice) > 0,
        "oath_static_count": len(oath_static),
        "ajani_plus1_parse": ([{"effect": a.get("effect", {}).get("type"),
                                "description": a.get("description")}
                               for a in ajani_plus1]
                              if ajani_plus1 else "NOT FOUND"),
        "ajani_loyalty": ajani.get("loyalty"),
        "ajani_mana": ajani.get("mana_cost"),
        "oath_mana": oath.get("mana_cost"),
    }
    say(f"data-level: oath twice line Unimplemented: "
        f"{note['oath_twice_line_unimplemented']}; "
        f"ajani +1 parse: {note['ajani_plus1_parse']}")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"card": "Oath of Teferi",
                   "oracle_text": oath.get("oracle_text"),
                   "static_abilities": oath_static,
                   "triggers": oath.get("triggers"),
                   "planeswalker": "Ajani Goldmane",
                   "planeswalker_oracle": ajani.get("oracle_text"),
                   "planeswalker_abilities": ajani.get("abilities"),
                   "finding": note}, f, indent=1)
    return note


# ------------------------------------------------------------- common ticks
async def do_bottom(c, acts, st, pid, tag, obs):
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
        key = (tag, obs["gname"], "bottom", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}
               ).get("data", {}) or {}
        n = int(con.get("min") or con.get("max") or 1)
        if n <= 0:
            return False
        names = [obj_lname(state, _cand_reference(ch)) for ch in cands]
        counts = {}
        for nm in [obj_lname(state, o) for o in hand_ids(state, pid)]:
            counts[nm] = counts.get(nm, 0) + 1

        def bkey(ch):
            nm = obj_lname(state, _cand_reference(ch))
            if is_land(get_obj(state, _cand_reference(ch))):
                return (1, nm)
            if nm in (AJANI.lower(), OATH.lower()) and counts.get(nm, 0) > 1:
                return (0, nm)  # duplicate walker/oath: bottom it
            return (2, nm)

        ranked = sorted(cands, key=bkey)
        picks = [ch["id"] for ch in ranked[:n] if ch.get("id")]
        if not picks:
            return False
        SUBMITTED_OPPS.add(key)
        say(f"[{tag}] bottoms {n}")
        wire("bottom", {"who": tag, "count": n})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": picks}}},
                          tag)
        return True
    return False


async def do_mulligan(c, acts, st, pid, tag, obs):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, obs["gname"], "mull", str(c.revision))
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    hand = [obj_lname(st["state"], o) for o in hand_ids(st["state"], pid)]
    n = ST["mulls"].get(tag + obs["gname"], 0)
    lands = sum(1 for nm in hand if nm in (PLAINS.lower(), ISLAND.lower()))
    has_key = AJANI.lower() in hand or (OATH.lower() in hand and obs["with_oath"])
    if tag == "P0":
        choice = "Keep" if ((has_key and lands >= 2) or n >= 2) else "Mulligan"
    else:
        choice = "Keep"
    if choice == "Mulligan":
        ST["mulls"][tag + obs["gname"]] = n + 1
    say(f"[{tag}] mulligan -> {choice} (hand={hand})")
    wire("mulligan", {"who": tag, "decision": choice})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": choice}}})
    return True


def discard_rank(state, o, obs):
    ln = obj_lname(state, o)
    if ln in (AJANI.lower(), OATH.lower()):
        hand = [obj_lname(state, x) for x in hand_ids(state, obs["pid"])]
        if hand.count(ln) > 1:
            return 0  # duplicate key card: discard
        return 3
    if is_land(get_obj(state, o)):
        return 2
    return 0


async def do_discard(c, acts, st, pid, tag, obs):
    state = st["state"]
    hand = hand_ids(state, pid)
    if len(hand) <= 7:
        return False
    n = len(hand) - 7
    obs["pid"] = pid
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
        key = (tag, obs["gname"], "discard", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        ref_of = {}
        for ch in cands:
            ref = _cand_reference(ch)
            if ref is not None:
                ref_of[str(ref)] = ch["id"]
        ranked = sorted(hand, key=lambda o: (discard_rank(state, o, obs),
                                             obj_lname(state, o)))
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


async def do_declare_empty(c, acts, st, pid, tag, obs, attackers_ok=True):
    for a in acts:
        if a.get("type") == "DeclareAttackers" and attackers_ok:
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
    state = st["state"]
    phase = str(state.get("phase") or "")
    if state.get("active_player") == pid and "declareattack" in phase.lower() \
            and attackers_ok:
        for opp in vi_ops(st):
            resp = opp.get("response", {}) or {}
            if resp.get("type") != "schema":
                continue
            spec = (resp.get("data") or {}).get("spec", {}) or {}
            if spec.get("type") != "relations":
                continue
            iid = opp.get("interactionId")
            key = (tag, obs["gname"], "declare", str(iid))
            if key in SUBMITTED_OPPS:
                return True
            SUBMITTED_OPPS.add(key)
            await interact_as(c, {"interactionId": iid,
                                  "response": {"type": "relations",
                                               "data": {"relations": []}}},
                              tag)
            return True
        key = (tag, obs["gname"], "declare-blind", str(c.revision))
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            say(f"[{tag}] blind declare empty (142 shape)")
            await c.send_action({"type": "DeclareAttackers",
                                 "data": {"attacks": [], "bands": []}})
            return True
    return False


async def do_legend(c, acts, st, tag, obs):
    for a in acts:
        if a.get("type") == "ChooseLegend":
            await submit_as_is(c, a)
            ST["legend_answered"] += 1
            say(f"[{tag}] legend rule: keeps first (advertised action)")
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
        key = (tag, obs["gname"], "legend", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        SUBMITTED_OPPS.add(key)
        ST["legend_answered"] += 1
        say(f"[{tag}] legend rule: keeps first (vi choice)")
        await answer_vi(c, opp, chs[0], tag)
        return True
    return False


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def play_a_land(c, state, pid, acts, tag, obs):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag + obs["gname"]) == turn:
        return False
    lands = [o for o in hand_ids(state, pid) if is_land(get_obj(state, o))]
    if not lands:
        return False
    for o in lands:
        for a in acts:
            if a["type"] == "PlayLand" and str(a.get("_src_oid")) == str(o):
                LAND_PLAYED_TURN[tag + obs["gname"]] = turn
                say(f"[{tag}] playing land {obj_lname(state, o)}")
                wire("play_land", {"who": tag, "oid": o})
                await submit_as_is(c, a)
                return True
    st = st_of(c)
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            if "playLand" not in surf_codes(ch):
                continue
            ref = _cand_reference(ch)
            if ref is not None and str(ref) in [str(x) for x in lands]:
                iid = opp.get("interactionId")
                key = (tag, obs["gname"], "playland", str(iid), str(ref))
                if key in SUBMITTED_OPPS:
                    continue
                SUBMITTED_OPPS.add(key)
                LAND_PLAYED_TURN[tag + obs["gname"]] = turn
                say(f"[{tag}] playing land via vi {obj_lname(state, ref)}")
                wire("play_land_vi", {"who": tag, "oid": str(ref)})
                await answer_vi(c, opp, ch, tag)
                return True
    return False


async def pass_priority(c, st, acts, tag, obs):
    for a in acts:
        if a.get("type") == "PassPriority":
            key = (tag, obs["gname"], "pass", str(c.revision))
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
                key = (tag, obs["gname"], "pass", str(iid))
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


def cast_zone(state, oid):
    return str(get_obj(state, oid).get("zone") or "").lower()


def note_cast_pending(c, kind, oid, name):
    CAST_PENDING[c.name] = {"kind": kind, "oid": str(oid), "name": name,
                            "since": time.time()}


async def guard_cast_pending(c, tag, st):
    pend = CAST_PENDING.get(c.name)
    if not pend:
        return None
    state = (st or {}).get("state") or {}
    oid = pend["oid"]
    zone = cast_zone(state, oid)
    if zone in ("stack", "battlefield"):
        CAST_PENDING.pop(c.name, None)
        say(f"[{tag}] cast confirmed on {zone}: {pend['name']} (oid {oid})")
        wire("cast_confirmed", {"kind": pend["kind"], "oid": oid, "zone": zone})
        return "confirmed"
    if time.time() - pend["since"] > 30:
        CAST_PENDING.pop(c.name, None)
        say(f"[{tag}] cast NOT confirmed after 30s (zone={zone}): "
            f"{pend['name']} (oid {oid}); will retry")
        wire("cast_dropped_retry", {"kind": pend["kind"], "oid": oid,
                                    "zone": zone})
        return "dropped"
    return "waiting"


# ------------------------------------------------------------- Oath ETB blink target
def _is_oath_blink_opp(opp, state):
    """Oath of Teferi ETB 'exile another target permanent you control':
    candidates reference BF permanents controlled by seat 0, excluding Oath
    itself."""
    resp = opp.get("response") or {}
    rtype = resp.get("type")
    data = resp.get("data") or {}
    items = data.get("candidates") or data.get("choices") or []
    if not items:
        return None
    if opp_codes(opp) & {"decideOptionalEffect", "decideOptionalCost"}:
        return None
    if rtype == "schema":
        spec = (data.get("spec") or {}).get("type") or ""
        if spec not in ("select", "sequence"):
            return None
    refs = []
    for ch in items:
        if candidate_seat(ch) is not None:
            return None
        ref = _cand_reference(ch)
        if ref is None:
            return None
        o = get_obj(state, ref)
        if o.get("zone") != "Battlefield" \
                or str(o.get("controller", -1)) != "0" \
                or obj_lname(state, ref) == OATH.lower():
            return None
        refs.append((ch, ref))
    return refs or None


async def answer_oath_blink(c, st, tag, obs):
    """Answer Oath's ETB with a LAND (never the walker: blinking the walker
    would reset its per-turn loyalty-activation count)."""
    state = st["state"]
    turn = state.get("turn_number")
    for opp in vi_ops(st):
        refs = _is_oath_blink_opp(opp, state)
        if not refs:
            continue
        iid = opp.get("interactionId")
        key = (tag, obs["gname"], "oath-blink", str(iid))
        if key in SUBMITTED_OPPS:
            return True
        want = None
        for ch, ref in refs:
            if is_land(get_obj(state, ref)):
                want = (ch, ref)
                break
        if want is None:
            say(f"[{tag}] Oath ETB: no land candidate among "
                f"{len(refs)}; deferring")
            wire("oath_blink_no_land", {"n": len(refs)})
            return True
        ch, ref = want
        SUBMITTED_OPPS.add(key)
        obs["blink_oid"] = str(ref)
        obs["blink_name"] = obj_lname(state, ref)
        say(f"[{tag}] Oath ETB exiles {obs['blink_name']} (oid {ref}, turn {turn})")
        wire("oath_blink_answer", {"turn": turn, "ref": str(ref),
                                   "name": obs["blink_name"],
                                   "n_candidates": len(refs)})
        await answer_vi(c, opp, ch, tag)
        return True
    return False

# ------------------------------------------------------------- loyalty activation
def find_plus1(acts, state, walker_oid):
    """The advertised ActivateAbility for Ajani's +1. Protocol 142
    advertises {"source_id", "ability_index"} with NO cost field, so match
    on source oid + the state's ability description containing "[+1]".
    Never assume an index."""
    cands = []
    for a in acts:
        if a.get("type") != "ActivateAbility":
            continue
        d = a.get("data", {}) or {}
        src = d.get("source_id", a.get("_src_oid"))
        try:
            if str(src) != str(walker_oid):
                continue
        except Exception:
            continue
        cands.append(a)
    if not cands:
        return None
    abilities = get_obj(state, walker_oid).get("abilities", []) or []
    for a in cands:
        idx = (a.get("data", {}) or {}).get("ability_index")
        if isinstance(idx, int) and idx < len(abilities):
            desc = str(abilities[idx].get("description", ""))
            if "[+1]" in desc:
                return a
    wire("plus1_no_match",
         {"n_candidates": len(cands),
          "indexes": [(a.get("data", {}) or {}).get("ability_index")
                      for a in cands],
          "descriptions": [str(ab.get("description", ""))[:80]
                           for ab in abilities]})
    return None


def any_activate_for_walker(acts, state, walker_oid):
    out = []
    for a in acts:
        if a.get("type") != "ActivateAbility":
            continue
        d = a.get("data", {}) or {}
        src = d.get("source_id", a.get("_src_oid"))
        try:
            if str(src) == str(walker_oid):
                out.append(a)
        except Exception:
            continue
    return out


async def submit_plus1(c, a, tag, obs, which):
    """Submit the +1 activation as-is; track it as pending until the
    loyalty counter delta lands (costs are paid on activation)."""
    oid = str((a.get("data", {}) or {}).get("source_id", a.get("_src_oid")))
    obs["act_pending"] = {"which": which, "oid": oid,
                          "action": copy.deepcopy(
                              {k: a[k] for k in ("type", "data") if k in a}),
                          "since": time.time(), "retries": 0}
    say(f"[{tag}] activates Ajani +1 ({which}) via advertised action")
    wire("plus1_submit", {"which": which, "oid": oid,
                          "action": obs["act_pending"]["action"]})
    await submit_as_is(c, a)


async def guard_act_pending(c, tag, obs, st):
    """Hold the tick while our +1 activation is unconfirmed. Confirmation
    = loyalty counter delta on the walker (cost paid on activation)."""
    pend = obs.get("act_pending")
    if not pend:
        return None
    state = (st or {}).get("state") or {}
    oid = pend["oid"]
    loy = loyalty_of(state, oid)
    if isinstance(loy, (int, float)) and loy > obs["act_loyalty_before"]:
        say(f"[{tag}] {pend['which']} activation confirmed: loyalty "
            f"{obs['act_loyalty_before']} -> {loy}")
        wire("plus1_confirmed", {"which": pend["which"],
                                 "loyalty_before": obs["act_loyalty_before"],
                                 "loyalty_now": loy})
        obs["act_pending"] = None
        return "confirmed"
    # structured rejection?
    for r in c.rejections:
        wire("plus1_rejection", {"which": pend["which"],
                                 "rejection": r})
        obs["act_pending"] = None
        obs.setdefault("rejections", []).append(r)
        say(f"[{tag}] {pend['which']} activation REJECTED: {r}")
        return "rejected"
    if time.time() - pend["since"] > 30:
        obs["act_pending"] = None
        say(f"[{tag}] {pend['which']} activation unconfirmed after 30s "
            f"(loyalty still {loy}); treating as refused")
        wire("plus1_dropped", {"which": pend["which"], "loyalty": loy})
        return "dropped"
    return "waiting"


async def refusal_probe(c, acts, state, tag, obs, which):
    """The 2nd/3rd +1 is not advertised. Re-submit the exact engine-issued
    action from the 1st activation (stale probe) and record what the
    engine does with it: structured rejection, silent ignore, or
    (unexpected) acceptance."""
    prev = obs.get("act1_action")
    if prev is None:
        obs[f"{which}_refused"] = "not-run"
        obs["notes"].append(f"{which}: no prior action to probe with")
        return
    wire("refusal_probe_submit", {"which": which, "action": prev})
    say(f"[{tag}] {which}: not advertised; probing with stale action")
    await c.send_action({"type": prev["type"], "data": prev.get("data", {})})
    obs[f"{which}_probe_since"] = time.time()
    obs[f"{which}_probe_action"] = prev
    obs[f"{which}_rej_base"] = len(c.rejections)


def snapshot_refusal(st, state, tag, obs, which):
    """Persist the vi opportunity / legal_actions state at the moment of a
    refused activation."""
    acts = top_acts(st)
    vi_summ = []
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        data = resp.get("data") or {}
        items = data.get("candidates") or data.get("choices") or []
        vi_summ.append({
            "interactionId": opp.get("interactionId"),
            "rtype": resp.get("type"),
            "spec": ((data.get("spec") or {}).get("type")),
            "n_items": len(items),
            "codes": sorted(opp_codes(opp)),
            "desc": str(opp.get("description", ""))[:160],
        })
    snap = {"which": which, "rev": c_revision_tag(st),
            "legal_action_types": sorted({a.get("type") for a in acts}),
            "n_legal_actions": len(acts),
            "vi_opportunities": vi_summ,
            "phase": state.get("phase"), "turn": state.get("turn_number"),
            "stack_len": len(state.get("stack") or [])}
    with open(f"{EVDIR}/refused_{which}.json", "w") as f:
        json.dump(snap, f, indent=1)
    say(f"[{tag}] {which}: refused_*.json snapshot written")
    wire("refusal_snapshot", {"which": which, "snap": snap})


def c_revision_tag(st):
    return (st or {}).get("state_revision")


def dump_act_shapes(c, tag, obs, which):
    """Debug: record how loyalty activation is advertised (or not) on this
    revision: full legal action types + any Activate* actions + vi
    opportunity shapes."""
    st = st_of(c)
    acts = merged_actions(st)
    state = st["state"]
    walker = find_bf_oid(state, 0, AJANI.lower())
    act_like = []
    for a in acts:
        if "ctivat" in a.get("type", ""):
            d = dict(a.get("data", {}) or {})
            act_like.append({"type": a.get("type"), "_src_oid": a.get("_src_oid"),
                             "data": d})
    vi_shapes = []
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        data = resp.get("data") or {}
        items = data.get("candidates") or data.get("choices") or []
        ch_summ = []
        for ch in items[:12]:
            ch_summ.append({"id": ch.get("id"),
                            "text": choice_text(ch)[:80],
                            "codes": surf_codes(ch),
                            "status": (ch.get("status") or {}).get("type")})
        vi_shapes.append({"iid": opp.get("interactionId"),
                          "rtype": resp.get("type"),
                          "spec": ((data.get("spec") or {}).get("type")),
                          "n_items": len(items),
                          "codes": sorted(opp_codes(opp)),
                          "desc": str(opp.get("description", ""))[:160],
                          "choices": ch_summ})
    snap = {"which": which, "rev": c_revision_tag(st),
            "walker_oid": walker,
            "walker_loyalty": loyalty_of(state, walker) if walker else None,
            "phase": state.get("phase"), "turn": state.get("turn_number"),
            "stack_len": len(state.get("stack") or []),
            "priority_player": state.get("priority_player"),
            "loyalty_abilities_activated_this_turn":
                state.get("loyalty_abilities_activated_this_turn"),
            "extra_loyalty_activations_this_turn":
                state.get("extra_loyalty_activations_this_turn"),
            "legal_action_types": sorted({a.get("type") for a in acts}),
            "activate_like_actions": act_like,
            "vi_opportunities": vi_shapes}
    with open(f"{EVDIR}/act_shapes_{which.lower()}.json", "w") as f:
        json.dump(snap, f, indent=1)
    say(f"[{tag}] {which}: act_shapes dump written "
        f"(activate-like={len(act_like)}, vi_opps={len(vi_shapes)})")
    wire("act_shapes", snap)


# ------------------------------------------------------------- develop
async def p0_develop(c, state, acts, tag, obs):
    if await play_a_land(c, state, 0, acts, tag, obs):
        return True
    # cast Ajani first
    if find_bf_oid(state, 0, AJANI.lower()) is None:
        oid = find_hand(state, 0, AJANI)
        if oid:
            a = find_cast_action(acts, state, AJANI.lower(), oid)
            if a is not None:
                say(f"[{tag}] casts Ajani Goldmane ({oid})")
                wire("cast_submit", {"tag": "ajani", "oid": oid})
                await submit_as_is(c, a)
                note_cast_pending(c, "ajani", oid, AJANI)
                return True
    # then Oath (oath game only)
    if obs["with_oath"] and find_bf_oid(state, 0, OATH.lower()) is None:
        oid = find_hand(state, 0, OATH)
        if oid:
            a = find_cast_action(acts, state, OATH.lower(), oid)
            if a is not None:
                say(f"[{tag}] casts Oath of Teferi ({oid})")
                wire("cast_submit", {"tag": "oath", "oid": oid})
                await submit_as_is(c, a)
                note_cast_pending(c, "oath", oid, OATH)
                return True
    return False


async def p1_develop(c, state, acts, tag, obs):
    return await play_a_land(c, state, 1, acts, tag, obs)


# ------------------------------------------------------------- seat ticks
async def p0_tick(c, tag, obs, tickfn):
    st = st_of(c)
    if not st:
        return False
    if await guard_cast_pending(c, tag, st) == "waiting":
        return True
    g = await guard_act_pending(c, tag, obs, st)
    if g == "waiting":
        return True
    if g in ("dropped", "rejected"):
        # activation never took: allow the stage logic to retry/submit
        # again (ACT1) or record the refusal (ACT2/ACT3 advertised path)
        obs["act1_submitted"] = False
        obs["notes"].append(f"activation guard: {g}; will retry")
        wire("act_guard_retry", {"result": g})
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, 0, tag, obs):
        return True
    if await do_bottom(c, acts, st, 0, tag, obs):
        return True
    if await do_discard(c, acts, st, 0, tag, obs):
        return True
    if await pay_tick(c, acts):
        return True
    if await do_declare_empty(c, acts, st, 0, tag, obs, attackers_ok=False):
        return True
    if await do_legend(c, acts, st, tag, obs):
        return True
    if obs["with_oath"] and await answer_oath_blink(c, st, tag, obs):
        return True
    if await tickfn(c, tag, obs, state, acts):
        return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 0):
        if await p0_develop(c, state, acts, tag, obs):
            return True

    if real_decision_pending(st):
        return True
    if obs.get("hold_turn"):
        # holding the turn for the same-turn 2nd/3rd activation attempt:
        # do not pass priority (that would advance the turn)
        return True
    if my_priority(top_acts(st)):
        await pass_priority(c, st, top_acts(st), tag, obs)
        return True
    return False


async def p1_tick(c, tag, obs):
    st = st_of(c)
    if not st:
        return False
    if await guard_cast_pending(c, tag, st) == "waiting":
        return True
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, 1, tag, obs):
        return True
    if await do_bottom(c, acts, st, 1, tag, obs):
        return True
    if await do_discard(c, acts, st, 1, tag, obs):
        return True
    if await pay_tick(c, acts):
        return True
    if await do_declare_empty(c, acts, st, 1, tag, obs):
        return True
    if await do_legend(c, acts, st, tag, obs):
        return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 1):
        if await p1_develop(c, state, acts, tag, obs):
            return True

    if real_decision_pending(st):
        return True
    if my_priority(top_acts(st)):
        await pass_priority(c, st, top_acts(st), tag, obs)
        return True
    return False

# ------------------------------------------------------------- game-specific activation ticks
async def oath_tick(c, tag, obs, state, acts):
    """Oath game activation sequence: ACT1 (advertised), ACT2/ACT3
    (refusal probes), then BLINK (wait for the exiled land to return)."""
    stage = obs["stage"]
    st = st_of(c)
    walker = find_bf_oid(state, 0, AJANI.lower())
    if stage in ("ACT1", "ACT2", "ACT3") and walker is None:
        return False
    if stage == "ACT1":
        # PRE gate: only start when Oath + walker are out and we hold a
        # clean main phase
        if not obs["pre_exported"]:
            oath_bf = find_bf_oid(state, 0, OATH.lower()) is not None
            if oath_bf and my_main(state, 0) \
                    and loyalty_of(state, walker) == 4:
                await do_export(c, "pre.json")
                obs["pre_exported"] = True
                obs["pre_loyalty"] = 4
                obs["pre_life"] = life_of(state, 0)
                obs["blink_zone_pre"] = zone_of(state, obs.get("blink_oid")) \
                    if obs.get("blink_oid") else None
                say(f"[{tag}] PRE exported: oath+ajani BF, loyalty=4, "
                    f"life={obs['pre_life']}, blink land zone="
                    f"{obs['blink_zone_pre']}")
                wire("stage_pre", {"loyalty": 4, "life": obs["pre_life"],
                                   "blink_oid": obs.get("blink_oid"),
                                   "blink_zone": obs["blink_zone_pre"]})
                return True
            return False
        # submit the advertised +1
        if not obs["act1_submitted"]:
            if not (my_main(state, 0) and not state.get("stack")):
                return False
            a = find_plus1(acts, state, walker)
            if a is None:
                # DEBUG: dump advertisement shapes once per revision
                dkey = (tag, obs["gname"], "act1shapes", str(c.revision))
                if dkey not in SUBMITTED_OPPS:
                    SUBMITTED_OPPS.add(dkey)
                    dump_act_shapes(c, tag, obs, "ACT1")
                # no advertised +1 yet (or ever) -- keep waiting a bounded
                # time before declaring the fixture broken
                if time.time() - obs["stage_since"] > 120:
                    obs["notes"].append(
                        "ACT1: +1 never advertised in 120s of main phase; "
                        "fixture cannot reach the trigger")
                    obs["stage"] = "WEDGED"
                    obs["stop"] = True
                return False
            obs["act1_action"] = copy.deepcopy(
                {k: a[k] for k in ("type", "data") if k in a})
            obs["act_loyalty_before"] = loyalty_of(state, walker)
            obs["act_life_before"] = life_of(state, 0)
            obs["act_turn"] = state.get("turn_number")
            obs["act1_submitted"] = True
            await submit_plus1(c, a, tag, obs, "ACT1")
            return True
        # wait for resolution: stack empty + life gained
        if not obs["act1_resolved"]:
            loy = loyalty_of(state, walker)
            life = life_of(state, 0)
            if loy == 5 and life == 22 and not state.get("stack"):
                obs["act1_resolved"] = True
                await do_export(c, "mid1.json")
                obs["stage"] = "ACT2"
                obs["stage_since"] = time.time()
                obs["hold_turn"] = True
                say(f"[{tag}] ACT1 resolved: loyalty 4->5, life 20->22; "
                    f"mid1.json exported; -> ACT2 (holding turn "
                    f"{obs['act_turn']})")
                wire("act1_resolved", {"loyalty": loy, "life": life,
                                       "act_turn": obs["act_turn"]})
                # fall through to the ACT2 block in this same tick: the
                # 2nd attempt must happen on the SAME turn
                stage = obs["stage"]
            if loy == 5 and not state.get("stack") \
                    and time.time() - obs["stage_since"] > 180:
                obs["notes"].append(
                    f"ACT1: loyalty reached 5 but life={life} (expected 22); "
                    f"GainLife may be unimplemented")
                obs["stage"] = "WEDGED"
                obs["stop"] = True
            return False
        return False
    if stage in ("ACT2", "ACT3"):
        which = stage
        key = "act2" if which == "ACT2" else "act3"
        if obs.get(f"{key}_done"):
            return False
        # the twice-per-turn test is only meaningful on the SAME turn as
        # the first activation
        if state.get("turn_number") != obs.get("act_turn"):
            obs["notes"].append(
                f"{which}: turn advanced to {state.get('turn_number')} "
                f"before the same-turn attempt (act_turn={obs.get('act_turn')}); "
                f"same-turn test missed")
            obs["hold_turn"] = False
            obs["stage"] = "BLINK"
            obs["stage_since"] = time.time()
            say(f"[{tag}] {which}: turn advanced; skipping to BLINK")
            return True
        if not obs.get(f"{key}_attempted"):
            # first: is it advertised?
            if not (my_main(state, 0) and not state.get("stack")):
                return False
            a = find_plus1(acts, state, walker)
            if a is not None:
                obs[f"{key}_advertised"] = True
                obs["act_loyalty_before"] = loyalty_of(state, walker)
                obs["act_life_before"] = life_of(state, 0)
                obs[f"{key}_attempted"] = True
                say(f"[{tag}] {which}: +1 IS advertised -- submitting "
                    f"(unexpected on the reported branch)")
                wire("unexpected_advertised", {"which": which})
                await submit_plus1(c, a, tag, obs, which)
                obs[f"{key}_rej_base"] = len(obs.get("rejections", []))
                obs["hold_turn"] = False  # let the ability resolve
                return True
            # not advertised: refusal evidence + stale probe
            obs[f"{key}_advertised"] = False
            obs[f"{key}_attempted"] = True
            snapshot_refusal(st_of(c), state, tag, obs, which.lower())
            await refusal_probe(c, acts, state, tag, obs, which.lower())
            return True
        # advertised path: the engine accepted the submission for a 2nd/3rd
        # activation -- wait for it to resolve (or time out)
        if obs.get(f"{key}_advertised") and not obs.get(f"{key}_probe_since"):
            base = obs.get(f"{key}_rej_base", 0)
            new_rej = (obs.get("rejections") or [])[base:]
            if new_rej:
                obs[f"{key}_accepted"] = False
                obs[f"{key}_done"] = True
                obs["notes"].append(
                    f"{which}: advertised but the engine REJECTED the "
                    f"submission: "
                    f"{json.dumps(new_rej[0], default=str)[:300]}")
                wire("advertised_rejected", {"which": which,
                                             "rejection": new_rej[0]})
                await do_export(c, f"mid{2 if which == 'ACT2' else 3}.json")
                obs["stage"] = "ACT3" if which == "ACT2" else "BLINK"
                obs["hold_turn"] = (obs["stage"] == "ACT3")
                obs["stage_since"] = time.time()
                say(f"[{tag}] {which} rejected after advertise; "
                    f"-> {obs['stage']}")
                return True
            loy = loyalty_of(state, walker)
            life = life_of(state, 0)
            base_loy = obs.get("act_loyalty_before") or 0
            if loy == base_loy + 1 and life == obs.get("act_life_before", 0) + 2 \
                    and not state.get("stack"):
                obs[f"{key}_accepted"] = True
                obs[f"{key}_done"] = True
                obs["notes"].append(
                    f"{which}: ADVERTISED and accepted: loyalty "
                    f"{base_loy}->{loy}, life +2")
                await do_export(c, f"mid{2 if which == 'ACT2' else 3}.json")
                obs["stage"] = "ACT3" if which == "ACT2" else "BLINK"
                obs["hold_turn"] = (obs["stage"] == "ACT3")
                obs["stage_since"] = time.time()
                say(f"[{tag}] {which} accepted (advertised path); "
                    f"-> {obs['stage']}")
                return True
            if time.time() - obs["stage_since"] > 180:
                obs[f"{key}_accepted"] = False
                obs[f"{key}_done"] = True
                obs["notes"].append(
                    f"{which}: advertised but never resolved in 180s; "
                    f"counting as refused")
                obs["stage"] = "ACT3" if which == "ACT2" else "BLINK"
                obs["stage_since"] = time.time()
            return False
        # probe outcome settle (45s window)
        probe_t = obs.get(f"{which.lower()}_probe_since")
        if probe_t and time.time() - probe_t > 45:
            obs[f"{key}_done"] = True
            loy = loyalty_of(state, walker)
            life = life_of(state, 0)
            base = obs.get(f"{which.lower()}_rej_base", 0)
            new_rej = c.rejections[base:]
            for r in new_rej:
                wire("refusal_probe_rejection",
                     {"which": which, "rejection": r})
            accepted = loy > obs["act_life_probe_loyalty"] \
                if obs.get("act_life_probe_loyalty") is not None else False
            obs[f"{key}_accepted"] = accepted
            obs["notes"].append(
                f"{which}: not advertised; stale probe -> "
                f"loyalty={loy} life={life} "
                f"new_rejections={len(new_rej)} accepted={accepted}")
            if new_rej:
                obs["notes"].append(
                    f"{which} rejection detail: "
                    f"{json.dumps(new_rej[0], default=str)[:300]}")
            await do_export(c, f"mid{2 if which == 'ACT2' else 3}.json")
            obs["stage"] = "ACT3" if which == "ACT2" else "BLINK"
            obs["hold_turn"] = (obs["stage"] == "ACT3")
            obs["stage_since"] = time.time()
            obs["act_life_probe_loyalty"] = None
            say(f"[{tag}] {which} done (accepted={accepted}); -> {obs['stage']}")
            return True
        if probe_t and obs.get("act_life_probe_loyalty") is None:
            obs["act_life_probe_loyalty"] = loyalty_of(state, walker)
        return False
    if stage == "BLINK":
        # wait for the exiled land to return to the battlefield
        oid = obs.get("blink_oid")
        if oid and zone_of(state, oid) == "Battlefield" \
                and str(get_obj(state, oid).get("controller", -1)) == "0":
            await do_export(c, "post.json")
            obs["blink_returned"] = True
            obs["stage"] = "DONE"
            obs["stop"] = True
            say(f"[{tag}] BLINK: land {oid} back on BF; post.json exported; "
                f"DONE")
            wire("blink_returned", {"oid": oid})
            return True
        if time.time() - obs["stage_since"] > 240:
            obs["notes"].append(
                f"BLINK: land {oid} still zone={zone_of(state, oid)} after "
                f"240s; giving up")
            await do_export(c, "post.json")
            obs["blink_returned"] = False
            obs["stage"] = "DONE"
            obs["stop"] = True
        return False
    return False


async def control_tick(c, tag, obs, state, acts):
    """Control game (no Oath): +1 once (must work), 2nd attempt (must be
    refused)."""
    stage = obs["stage"]
    walker = find_bf_oid(state, 0, AJANI.lower())
    if stage in ("CACT1", "CACT2") and walker is None:
        return False
    if stage == "CACT1":
        if not obs["pre_exported"]:
            if my_main(state, 0) and loyalty_of(state, walker) == 4:
                await do_export(c, "control_pre.json")
                obs["pre_exported"] = True
                say(f"[{tag}] control PRE exported (no oath)")
                return True
            return False
        if not obs["act1_submitted"]:
            if not (my_main(state, 0) and not state.get("stack")):
                return False
            a = find_plus1(acts, state, walker)
            if a is None:
                dkey = (tag, obs["gname"], "cact1shapes", str(c.revision))
                if dkey not in SUBMITTED_OPPS:
                    SUBMITTED_OPPS.add(dkey)
                    dump_act_shapes(c, tag, obs, "CACT1")
                if time.time() - obs["stage_since"] > 120:
                    obs["notes"].append(
                        "CACT1: +1 never advertised; fixture broken")
                    obs["stage"] = "WEDGED"
                    obs["stop"] = True
                return False
            obs["act1_action"] = copy.deepcopy(
                {k: a[k] for k in ("type", "data") if k in a})
            obs["act_loyalty_before"] = loyalty_of(state, walker)
            obs["act_turn"] = state.get("turn_number")
            obs["act1_submitted"] = True
            await submit_plus1(c, a, tag, obs, "CACT1")
            return True
        if not obs["act1_resolved"]:
            loy = loyalty_of(state, walker)
            life = life_of(state, 0)
            if loy == 5 and life == 22 and not state.get("stack"):
                obs["act1_resolved"] = True
                await do_export(c, "control_mid.json")
                obs["stage"] = "CACT2"
                obs["stage_since"] = time.time()
                obs["hold_turn"] = True
                say(f"[{tag}] control ACT1 resolved; -> CACT2 (holding turn "
                    f"{obs['act_turn']})")
                # fall through to CACT2 in this same tick
                stage = obs["stage"]
            return False
        return False
    if stage == "CACT2":
        if obs.get("act2_done"):
            return False
        if state.get("turn_number") != obs.get("act_turn"):
            obs["notes"].append(
                "control 2nd: turn advanced before the same-turn attempt; "
                "same-turn test missed")
            obs["hold_turn"] = False
            obs["stage"] = "DONE"
            obs["stop"] = True
            say(f"[{tag}] control 2nd: turn advanced; DONE")
            return True
        if not obs.get("act2_attempted"):
            if not (my_main(state, 0) and not state.get("stack")):
                return False
            a = find_plus1(acts, state, walker)
            if a is not None:
                obs["act2_advertised"] = True
                obs["act_loyalty_before"] = loyalty_of(state, walker)
                obs["act2_attempted"] = True
                say(f"[{tag}] control 2nd +1 IS advertised -- submitting")
                wire("control_unexpected_advertised", {})
                await submit_plus1(c, a, tag, obs, "CACT2")
                obs["act2_rej_base"] = len(obs.get("rejections", []))
                obs["hold_turn"] = False  # let the ability resolve
                return True
            obs["act2_advertised"] = False
            obs["act2_attempted"] = True
            snapshot_refusal(st_of(c), state, tag, obs, "control_2nd")
            await refusal_probe(c, acts, state, tag, obs, "control_2nd")
            return True
        if obs.get("act2_advertised") and not obs.get("control_2nd_probe_since"):
            base = obs.get("act2_rej_base", 0)
            new_rej = (obs.get("rejections") or [])[base:]
            if new_rej:
                obs["act2_accepted"] = False
                obs["act2_done"] = True
                obs["notes"].append(
                    "control 2nd: advertised but the engine REJECTED the "
                    f"submission: {json.dumps(new_rej[0], default=str)[:300]}")
                wire("control_advertised_rejected",
                     {"rejection": new_rej[0]})
                await do_export(c, "control_post.json")
                obs["stage"] = "DONE"
                obs["stop"] = True
                say(f"[{tag}] control 2nd rejected after advertise; DONE")
                return True
            loy = loyalty_of(state, walker)
            life = life_of(state, 0)
            if loy == 6 and life == 24 and not state.get("stack"):
                obs["act2_accepted"] = True
                obs["act2_done"] = True
                obs["notes"].append(
                    "control 2nd: ADVERTISED and accepted (unexpected)")
                await do_export(c, "control_post.json")
                obs["stage"] = "DONE"
                obs["stop"] = True
                say(f"[{tag}] control 2nd accepted (advertised); DONE")
                return True
            return False
        probe_t = obs.get("control_2nd_probe_since")
        if probe_t and time.time() - probe_t > 45:
            obs["act2_done"] = True
            loy = loyalty_of(state, walker)
            base = obs.get("control_2nd_rej_base", 0)
            new_rej = c.rejections[base:]
            for r in new_rej:
                wire("control_probe_rejection", {"rejection": r})
            accepted = loy > (obs.get("act_life_probe_loyalty") or 0)
            obs["act2_accepted"] = accepted
            obs["notes"].append(
                f"control 2nd: not advertised; probe -> loyalty={loy} "
                f"new_rejections={len(new_rej)} accepted={accepted}")
            if new_rej:
                obs["notes"].append(
                    "control rejection detail: "
                    f"{json.dumps(new_rej[0], default=str)[:300]}")
            await do_export(c, "control_post.json")
            obs["stage"] = "DONE"
            obs["stop"] = True
            say(f"[{tag}] control 2nd done (accepted={accepted}); DONE")
            return True
        if probe_t and obs.get("act_life_probe_loyalty") is None:
            obs["act_life_probe_loyalty"] = loyalty_of(state, walker)
        return False
    return False


async def do_export(c, path, dest_dir=None):
    s = await c.export_state()
    with open(f"{dest_dir or EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


# ------------------------------------------------------------- game runner
def new_obs(gname, with_oath):
    return {
        "gname": gname, "with_oath": with_oath, "pid": 0,
        "stage": "ACT1" if with_oath else "CACT1",
        "stage_since": time.time(), "stop": False,
        "pre_exported": False, "act1_submitted": False,
        "act1_resolved": False, "act1_action": None,
        "act_pending": None, "act_loyalty_before": None,
        "act_life_before": None, "act_life_probe_loyalty": None,
        "blink_oid": None, "blink_name": None, "blink_returned": None,
        "rejections": [], "notes": [],
    }


async def run_game(p0_deck, gname, with_oath, tickfn, timeout):
    obs = new_obs(gname, with_oath)
    p0 = PhaseClient(f"P0{gname}")
    await p0.connect()
    say(f"[{gname}] P0 creating game...")
    await p0.create(deck(*p0_deck))
    p1 = PhaseClient(f"P1{gname}")
    await p1.connect()
    say(f"[{gname}] P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    obs["game_code"] = p0.game_code
    say(f"[{gname}] game {p0.game_code}; P0 seat={p0.player_id} "
        f"P1 seat={p1.player_id}")
    wire("game", {"gname": gname, "code": p0.game_code,
                  "p0": p0.player_id, "p1": p1.player_id})

    t0 = time.time()
    last_rev = {}
    last_tick_at = {}
    last_progress = time.time()
    last_diag = time.time()
    while time.time() - t0 < timeout and not obs.get("stop") \
            and obs.get("stage") != "DONE":
        await asyncio.sleep(0.25)
        for c, tag, is_p0 in ((p0, f"P0{gname}", True),
                              (p1, f"P1{gname}", False)):
            st = st_of(c)
            if not st:
                continue
            rev_changed = c.revision != last_rev.get(c.name)
            if rev_changed:
                last_rev[c.name] = c.revision
                last_progress = time.time()
            else:
                if not (my_priority(top_acts(st))
                        and time.time() - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            try:
                if is_p0:
                    await p0_tick(c, tag, obs, tickfn)
                else:
                    await p1_tick(c, tag, obs)
            except Exception as e:
                say(f"tick error {c.name}: {type(e).__name__}: {e}")
                wire("tick_error", {"who": c.name, "error": str(e)})
        # game-over detection
        st = st_of(p0)
        if st:
            s = st["state"]
            if s.get("phase") == "GameOver" or s.get("winner") is not None:
                obs["notes"].append(f"game over (winner={s.get('winner')})")
                obs["stop"] = True
        # stall detection
        if not obs.get("stop") and obs.get("stage") not in ("DONE",) \
                and time.time() - last_progress > STALL_AFTER:
            obs["notes"].append(
                f"STALLED: no new revision for {STALL_AFTER}s at "
                f"rev={p0.revision}, stage={obs['stage']}")
            say(f"[{gname}] STALL at rev={p0.revision} stage={obs['stage']}")
            wire("stall", {"gname": gname, "rev": p0.revision,
                           "stage": obs["stage"]})
            obs["stop"] = True
        if time.time() - last_diag > 60:
            last_diag = time.time()
            st = st_of(p0)
            if st:
                s = st["state"]
                say(f"DIAG {gname} p0: rev={p0.revision} "
                    f"turn={s.get('turn_number')} phase={s.get('phase')} "
                    f"stage={obs.get('stage')} prio={my_priority(top_acts(st))} "
                    f"decision={real_decision_pending(st)}")
    try:
        await asyncio.wait_for(p0.close(), 15)
    except Exception as e:
        say(f"p0.close failed: {e}")
    try:
        await asyncio.wait_for(p1.close(), 15)
    except Exception as e:
        say(f"p1.close failed: {e}")
    obs["duration_s"] = round(time.time() - t0, 1)
    return obs

# ------------------------------------------------------------- evaluation + main
def env_state(p):
    try:
        with open(f"{EVDIR}/{p}") as f:
            return json.load(f)["state"]
    except FileNotFoundError:
        return None


def evaluate(obs_a, obs_b):
    A = {}
    notes = []
    pre = env_state("pre.json")
    mid1 = env_state("mid1.json")
    mid2 = env_state("mid2.json")
    mid3 = env_state("mid3.json")
    post = env_state("post.json")
    cpre = env_state("control_pre.json")
    cmid = env_state("control_mid.json")
    cpost = env_state("control_post.json")

    # A1: setup
    if pre is None:
        A["A1_setup_ok"] = "failed"
        notes.append("pre.json never exported (setup gate unreached)")
    else:
        oath_bf = find_bf_oid(pre, 0, OATH.lower()) is not None
        aj = find_bf_oid(pre, 0, AJANI.lower())
        loy = loyalty_of(pre, aj) if aj else None
        life = (life_of(pre, 0), life_of(pre, 1))
        notes.append(f"pre.json: oath_bf={oath_bf} ajani_oid={aj} "
                     f"loyalty={loy} life={life} phase={pre.get('phase')} "
                     f"turn={pre.get('turn_number')}")
        A["A1_setup_ok"] = ("passed" if oath_bf and aj and loy == 4
                            and life == (20, 20) else "failed")

    # A2: first +1 resolves
    if mid1 is None:
        A["A2_first_plus1"] = "failed"
        notes.append("mid1.json never exported (ACT1 unresolved)")
    else:
        aj = find_bf_oid(mid1, 0, AJANI.lower())
        loy = loyalty_of(mid1, aj) if aj else None
        life = life_of(mid1, 0)
        notes.append(f"mid1.json: loyalty={loy} life={life} "
                     f"(expected 5/22)")
        A["A2_first_plus1"] = ("passed" if loy == 5 and life == 22
                               else "failed")

    # A3: second refused
    a2 = obs_a.get("act2_accepted")
    notes.append(f"oath game 2nd attempt: advertised="
                 f"{obs_a.get('act2_advertised')} accepted={a2}")
    if obs_a.get("act2_advertised") is False and a2 is False:
        A["A3_second_refused"] = "passed"
    elif a2 is True:
        A["A3_second_refused"] = "failed"
    else:
        A["A3_second_refused"] = "not-run"

    # A4: third refused
    a3 = obs_a.get("act3_accepted")
    notes.append(f"oath game 3rd attempt: advertised="
                 f"{obs_a.get('act3_advertised')} accepted={a3}")
    if obs_a.get("act3_advertised") is False and a3 is False:
        A["A4_third_refused"] = "passed"
    elif a3 is True:
        A["A4_third_refused"] = "failed"
    else:
        A["A4_third_refused"] = "not-run"

    # A5: ETB blink regression
    oid = obs_a.get("blink_oid")
    if post is None or not oid:
        A["A5_etb_blink"] = "failed" if post is None else "not-run"
        notes.append(f"A5: post={'exported' if post else 'missing'} "
                     f"blink_oid={oid}")
    else:
        o = get_obj(post, oid)
        ok = (o.get("zone") == "Battlefield"
              and str(o.get("controller", -1)) == "0")
        notes.append(f"A5: blink land oid={oid} name={obs_a.get('blink_name')} "
                     f"zone={o.get('zone')} controller={o.get('controller')} "
                     f"(pre zone={obs_a.get('blink_zone_pre')})")
        A["A5_etb_blink"] = "passed" if ok else "failed"

    # B1: control first works
    if cmid is None:
        A["B1_control_first_ok"] = "failed"
        notes.append("control_mid.json never exported")
    else:
        aj = find_bf_oid(cmid, 0, AJANI.lower())
        loy = loyalty_of(cmid, aj) if aj else None
        life = life_of(cmid, 0)
        notes.append(f"control_mid.json: loyalty={loy} life={life} "
                     f"(expected 5/22)")
        A["B1_control_first_ok"] = ("passed" if loy == 5 and life == 22
                                    else "failed")

    # B2: control second refused
    b2 = obs_b.get("act2_accepted")
    notes.append(f"control 2nd attempt: advertised="
                 f"{obs_b.get('act2_advertised')} accepted={b2}")
    if obs_b.get("act2_advertised") is False and b2 is False:
        A["B2_control_second_refused"] = "passed"
    elif b2 is True:
        A["B2_control_second_refused"] = "failed"
    else:
        A["B2_control_second_refused"] = "not-run"

    notes.extend(obs_a["notes"])
    notes.extend([f"control: {n}" for n in obs_b["notes"]])

    # verdict
    keys = ["A1_setup_ok", "A2_first_plus1", "A3_second_refused",
            "A4_third_refused", "A5_etb_blink",
            "B1_control_first_ok", "B2_control_second_refused"]
    vals = [A.get(k) for k in keys]
    if all(v == "passed" for v in vals):
        verdict = "reproduced"
    elif (A.get("A1_setup_ok") == "passed"
          and A.get("A2_first_plus1") == "passed"
          and obs_a.get("act2_accepted") is True
          and obs_a.get("act3_accepted") is False
          and A.get("B1_control_first_ok") == "passed"
          and A.get("B2_control_second_refused") == "passed"
          and A.get("A5_etb_blink") == "passed"):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    return A, notes, verdict


def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 900
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #10028 - Oath of Teferi: loyalty-twice line "
                    "Unimplemented", fill=(235, 240, 250))
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
    d.text((24, y), "Ajani Goldmane +1 (Scry-free: GainLife) as the loyalty "
                    "probe: loyalty 4->5, life +2 per activation",
           fill=(200, 210, 225))
    y += 26
    d.text((24, y), f"oath game: {run['games']['oath']['code']}  "
                    f"control game: {run['games']['control']['code']}",
           fill=(200, 210, 225))
    y += 30
    d.text((24, y), "Assertions (from saved states + wire log):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "A1 setup: Oath + Ajani BF, loyalty 4, life 20/20",
        "A2_first_plus1": "A2 1st +1 works (loyalty 4->5, life 20->22)",
        "A3_second_refused": "A3 2nd +1 with Oath REFUSED (reported outcome)",
        "A4_third_refused": "A4 3rd +1 with Oath refused",
        "A5_etb_blink": "A5 ETB blink: exiled land returns at end step",
        "B1_control_first_ok": "B1 control (no Oath): 1st +1 works",
        "B2_control_second_refused": "B2 control (no Oath): 2nd +1 refused",
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
    for n in run["notes"][:10]:
        d.text((40, y), n[:118], fill=(150, 165, 185))
        y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 10028/" +
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


async def main():
    t0 = time.time()
    await verify_server_hello()
    data_finding = check_data_level()

    say("=== GAME A: oath game ===")
    obs_a = await run_game(P0_DECK_OATH, "oath", True, oath_tick, 1500)
    say(f"=== GAME A ended: stage={obs_a['stage']} "
        f"duration={obs_a['duration_s']}s ===")
    say("=== GAME B: control game ===")
    obs_b = await run_game(P0_DECK_CTRL, "ctrl", False, control_tick, 900)
    say(f"=== GAME B ended: stage={obs_b['stage']} "
        f"duration={obs_b['duration_s']}s ===")

    A, notes, verdict = evaluate(obs_a, obs_b)
    for k in ("A1_setup_ok", "A2_first_plus1", "A3_second_refused",
              "A4_third_refused", "A5_etb_blink",
              "B1_control_first_ok", "B2_control_second_refused"):
        say(f"{k}: {A.get(k, 'not-run')}")

    notes.append("protocol-142 driver: HELLO advertises protocol 142; "
                 "CreateGameWithSettings + JoinGameWithPassword + "
                 "start_when_full; MulliganDecision via legacy Action; "
                 "BottomCards/DiscardToHandSize via vi schema/select; "
                 "CastSpell via advertised actions as-is; mana engine "
                 "auto-tapped with a 30s cast-confirmation guard; never "
                 "inject targets into CastSpell (Oath ETB target chosen via "
                 "vi TargetSelection candidates); ActivateAbility matched on "
                 "source oid + Loyalty cost amount; playLand in "
                 "NON_DECISION_CODES; priority-gated passes; 5s re-tick "
                 "backstop.")
    notes.append(f"data-level: {json.dumps(data_finding, default=str)}")

    dur = time.time() - t0
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(dur, 1),
        "server_identity": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 142,
                   "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(__file__),
        "decks": {
            "P0_oath": [[n, c] for n, c in P0_DECK_OATH],
            "P0_control": [[n, c] for n, c in P0_DECK_CTRL],
            "P1": [[n, c] for n, c in P1_DECK],
        },
        "games": {
            "oath": {"code": obs_a.get("game_code"),
                     "stage": obs_a.get("stage"),
                     "duration_s": obs_a.get("duration_s")},
            "control": {"code": obs_b.get("game_code"),
                        "stage": obs_b.get("stage"),
                        "duration_s": obs_b.get("duration_s")},
        },
        "assertions": A,
        "notes": notes,
        "oath_game": {"act2_advertised": obs_a.get("act2_advertised"),
                      "act2_accepted": obs_a.get("act2_accepted"),
                      "act3_advertised": obs_a.get("act3_advertised"),
                      "act3_accepted": obs_a.get("act3_accepted"),
                      "blink_oid": obs_a.get("blink_oid"),
                      "blink_name": obs_a.get("blink_name"),
                      "blink_returned": obs_a.get("blink_returned"),
                      "rejections": obs_a.get("rejections")},
        "control_game": {"act2_advertised": obs_b.get("act2_advertised"),
                         "act2_accepted": obs_b.get("act2_accepted")},
        "data_finding": data_finding,
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts "
            ">4-of for custom games).",
            "States are authoritative exports, restorable only via full game "
            "replay (scenario_10028.py), not direct load.",
            "Refusal of the 2nd/3rd activation is recorded as the engine "
            "not advertising ActivateAbility for the walker, plus a stale "
            "re-submission probe; a structured rejection is captured when "
            "the engine sends one.",
        ],
        "setup_line": "Oath game: P0 20x Plains / 16x Island / 4x Ajani "
                      "Goldmane / 4x Oath of Teferi; control game: P0 32x "
                      "Plains / 4x Ajani Goldmane; P1 both games 30x Plains / "
                      "30x Island (passive).",
        "contract_line": "Ajani Goldmane +1 (implemented GainLife): with "
                         "Oath of Teferi on the battlefield the 2nd +1 in "
                         "the turn must be refused (reported: the twice line "
                         "is Unimplemented, so once-per-turn stands); without "
                         "Oath the 2nd +1 must also be refused (control); "
                         "the ETB blink must return the exiled land at the "
                         "next end step.",
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": notes,
                   "oath_game": run["oath_game"],
                   "control_game": run["control_game"]}, f, indent=2)
    say(f"DONE verdict={verdict} assertions={json.dumps(A)}")
    sys.stdout.flush()

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
        RUNLOG.close()
    except Exception:
        pass
    try:
        import shutil as _shutil
        _shutil.copy(__file__, f"{EVDIR}/scenario_10028.py")
        say("scenario copied into evidence dir")
    except Exception:
        say("scenario copy FAILED:")
        say(traceback.format_exc())
    try:
        write_manifest()
    except Exception:
        say("write_manifest FAILED:")
        say(traceback.format_exc())
    return run


if __name__ == "__main__":
    asyncio.run(main())
