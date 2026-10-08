#!/usr/bin/env python3
"""Issue #6917: Oko, Thief of Crowns +1 is only until end of turn in game.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.103.0):
  Oko, Thief of Crowns ({1}{G}{U}, loyalty 4):
    "[+1]: Target artifact or creature loses all abilities and becomes a
     green Elk creature with base power and toughness 3/3."
  No duration is given: the effect is an indefinite continuous effect.

Reported symptom: in-game the elk effect expires at end of turn.

Protocol-106 port of driver/scenario_6917.py (v0.81.3 / protocol 70) for
pinned v0.103.0 (protocol 106). Conventions from scenario_6914_01030.py:
  - HELLO advertises protocol 106 (exact match); CreateGameWithSettings
    + JoinGameWithPassword + start_when_full; deck schema
    {"main_deck": [...]} via deck(); default Bo1 format (format_config=None).
  - waiting_for is gone (null): priority = top-level PassPriority legal
    action; MulliganDecision via legacy Action; bottom-after-mulligan via
    the vi schema/select opportunity gated on
    waitingForKind.code == 'mulligan' AND turn 1 / Untap; DiscardToHandSize
    via vi schema/select (the ONLY accepted submission for a select schema
    is {"type": "select", "data": {"choiceIds": [...]}}).
  - CastSpell via legacy Action; the v0.103.0 engine auto-taps reliably
    (payment_mode Auto); driver taps are never used (driver taps on top of
    engine auto-taps DOUBLE-PAY -- no mana payment is involved in Oko's +1
    anyway: cost is +1 loyalty).
  - ActivateAbility MUST be submitted while holding priority; submitted
    before the priority-pass gate. On 106 the activation may appear as a
    legacy Action or as a vi choose/select opportunity; both shapes are
    handled (ability_index 1 == the +1).
  - Target selection: candidates arrive as string references (e.g. "85");
    the ACTUALLY submitted candidate reference is recorded at answer time
    (as a string, no isinstance(int) filtering).
  - real_decision_pending excludes the 106 priority-menu codes and any
    already-answered opportunity; the stack-watch branch FALLS THROUGH to
    the priority-pass gate (never return early; both players must pass in
    succession for stack resolution on 106).
  - Exports go through the host client only; data.state is a JSON STRING
    parsed once.
  - sleep(0) yield before leg evaluation; 5s re-tick backstop for
    priority-holding clients; rejection-drain resync per tick.

Setup (native engine, two human-client seats, default Bo1):
  P0: 4x Oko, Thief of Crowns + 28x Forest + 28x Island. Casts Oko, then
      activates its +1 targeting one of P1's creatures.
  P1: 12x Grizzly Bears + 48x Forest. Casts bears on its turns so the +1's
      target prompt has >=2 legal candidates (single legal target may be
      auto-targeted by the engine).

Plan:
  1. P0 casts Oko ({1}{G}{U}) on its main phase (engine Auto payment).
  2. P1 casts Grizzly Bears on its main phases.
  3. On a later P0 main phase, P0 activates Oko's +1 (ability_index 1),
     answering the target prompt with one bear; the ACTUALLY submitted
     candidate reference (string) is recorded at answer time.
  4. Export PRE (just before the activation), MID (same turn, after the
     ability resolves and the stack empties), POST (P0's NEXT turn).

Assertions:
  A1_setup_ok        PRE: Oko on P0's BF with loyalty 4; >=2 Grizzly Bears
                     on P1's BF.
  A2_activation      Oko's loyalty is 5 in MID (4->5); the +1 resolved.
  A3_elk_this_turn   MID: the targeted bear is a green 3/3 Elk with no
                     abilities (power==3, toughness==3, subtypes has Elk,
                     color has Green, abilities empty).
  A4_elk_persists    POST (P0's next turn): the targeted bear is STILL a
                     green 3/3 Elk. FAILED = it reverted to a 2/2 Bear ->
                     the reported duration bug is REPRODUCED.
  A5_cleanup         POST: stack empty, game proceeding.

Verdict rule: reproduced iff A1-A3 pass and A4 fails. not-reproduced iff
A1-A5 all pass. blocked iff A1 fails (or A2/A3 fail for setup reasons).
Never "fixed".

Evidence: evidence/6917/<run-id>/pre.json, mid.json, post.json, run.json,
assertions.json, observations.json, data_evidence.json, manifest.sha256,
summary.png, scenario_6917_01030.py, wire_log.jsonl, scenario_run.log,
server.log
"""
import asyncio
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402

import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 6917
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261007-6917")
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
assert not os.path.exists(EVDIR), f"EVDIR {EVDIR} already exists -- refusing"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")


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
        WIRE.flush()
    except Exception:
        pass


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


SERVER_IDENTITY = {
    "server_version": "0.103.0",
    "build_commit": "ec27a8d",
    "protocol_version": 106,
    "server_binary_sha256": None,
    "card_data_sha256": None,
    "draft_pools_sha256": None,
    "signature_verified": True,
}
for _f, _k in (
        ("server/releases/v0.103.0/phase-server-slim-x86_64-unknown-linux-musl",
         "server_binary_sha256"),
        ("server/releases/v0.103.0/data/card-data.json", "card_data_sha256"),
        ("server/releases/v0.103.0/data/draft-pools.json",
         "draft_pools_sha256")):
    SERVER_IDENTITY[_k] = sha256_of_file(f"{BACKFILL}/{_f}")
# cross-check against the values recorded by the 6911/6914 runs on the pin
assert SERVER_IDENTITY["server_binary_sha256"] == \
    "a991fec48a21e11d8892200fa10fcd9e830bb2adf97ba6dc7b8255d907d54dbc", \
    "binary hash drift from the v0.103.0 pin"
assert SERVER_IDENTITY["card_data_sha256"] == \
    "40aa768ead511bcdff5df65e0022ecb5b95c474558ec5dc661467c8ce5d3f4fe", \
    "card-data hash drift from the v0.103.0 pin"
assert SERVER_IDENTITY["draft_pools_sha256"] == \
    "b4fcf6dde106bcdcc40f2a0593dc2eb4e2c7c4221354ecf69665263b6fb1edbd", \
    "draft-pools hash drift from the v0.103.0 pin"
say("server identity hashes verified against the v0.103.0 pin")


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
    assert str(ver).startswith("0.103.0"), f"unexpected version {ver}"
    assert int(proto) == 106, f"unexpected protocol {proto}"
    assert str(build) == "ec27a8d", f"unexpected build {build}"
    return {"server_version": str(ver), "build_commit": str(build),
            "protocol_version": int(proto), "mode": d.get("mode")}


CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.103.0/"
                           "data/card-data.json"))


def check_data_level():
    """Record the v0.103.0 parse of Oko (oracle text and ability structure).
    The reported defect is the +1's duration at resolution time, not a
    missing parse -- but the oracle text must show no duration."""
    c = CARD_DATA.get("oko, thief of crowns", {})
    oracle = str(c.get("oracle_text") or "")
    abils = c.get("abilities") or []
    plus_one = [a for a in abils if "+1" in str(a.get("oracle_text") or "")]
    out = {
        "name": c.get("name"),
        "oracle_text": oracle,
        "ability_count": len(abils),
        "plus_one_abilities": [
            {"oracle_text": a.get("oracle_text"),
             "trigger": a.get("trigger"),
             "effect": a.get("effect")} for a in plus_one],
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(out, f, indent=1, default=str)
    ok = (c.get("name") == "Oko, Thief of Crowns"
          and "loses all abilities" in oracle
          and "Elk" in oracle
          and "until end of turn" not in oracle)
    say(f"data-level: oko parse ok={ok} plus_one_abilities="
        f"{len(plus_one)}")
    wire("data_level", {"oko_parse_ok": ok,
                        "n_plus_one": len(plus_one)})
    assert ok, "v0.103.0 card-data lost or changed Oko's +1 text"
    return out


OKO_T = "Oko, Thief of Crowns"
OKO_L = "oko, thief of crowns"
BEAR_T = "Grizzly Bears"
BEAR_L = "grizzly bears"
FOREST_L = "forest"
ISLAND_L = "island"

P0_DECK = ((OKO_T, 4), ("Forest", 28), ("Island", 28))
P1_DECK = ((BEAR_T, 12), ("Forest", 48))

MAIN_PHASES = ("PreCombatMain", "PostCombatMain", "Main")

GAME_TIMEOUT = 1500
STALL_AFTER = 150
TURN_CAP = 40

STAGE = {"stage": "SETUP", "stop": False, "game_code": None,
         "mulls": {"P0": 0, "P1": 0},
         "opp_shapes_logged": set()}
SUBMITTED_OPPS = set()
LAST_IID = {"iid": None}
LAND_PLAYED_TURN = {}
PASSED_REV = {}
ST = {"oko_cast": False, "oko_cast_turn": None, "oko_oid": None,
      "plus_one_done": False, "act_turn": None, "target_oid": None,
      "target_candidates": [], "act_in_flight": False, "mid_exported": False,
      "post_exported": False, "post_at": None, "mid_act_turn": None,
      "oko_loyalty_mid": None, "pre_exported": False,
      "plus_one_act_via": None}
OBS = {"unexpected_prompts": [], "auto_answered": [], "rejections": [],
       "target_selections": [], "tick_errors": [], "notes": []}


def st_of(c):
    return c.latest or {}


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    return str(get_obj(state, oid).get("base_name")
               or get_obj(state, oid).get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players") or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def life_of(state, pid):
    return player_of(state, pid).get("life")


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def bf_oids(state, pid, lname=None):
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)
            and (lname is None or obj_lname(state, oid) == lname)]


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def untapped_lands(state, pid):
    return [oid for oid in bf_oids(state, pid)
            if is_land(get_obj(state, oid))
            and not get_obj(state, oid).get("tapped")]


def stack_empty(state):
    return not (state.get("stack") or [])


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
    return (state.get("phase") in MAIN_PHASES
            and state.get("active_player") == pid
            and stack_empty(state))


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)
            and s.get("data", {}).get("code") is not None]


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def _cand_reference(ch):
    """The engine issues target-candidate references as STRINGS
    (e.g. "85"); return the reference verbatim."""
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


def candidate_refs(ch):
    out = set()
    for s in (ch or {}).get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            out.add(str(d.get("reference")))
    return out


NON_DECISION_CODES = {"passPriority", "tapLandForMana", "untapLandForMana",
                      "castSpell", "activateAbility", "candidate", "mana",
                      "mulliganDecision", "playLand"}


def is_priority_menu(op):
    for c in (op.get("response") or {}).get("data", {}).get("choices", []):
        for s in c.get("surfaces", []) or []:
            if s.get("type") == "action" \
                    and (s.get("data") or {}).get("code") == "passPriority":
                return True
    return False


def unanswered_ops(st):
    out = []
    for op in vi_ops(st):
        iid = op.get("interactionId") or op.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        if is_priority_menu(op):
            continue
        out.append(op)
    return out


def real_decision_pending(st):
    for opp in unanswered_ops(st):
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


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action_type": action.get("type"),
                           "stage": STAGE["stage"]})
    clean = {k: v for k, v in action.items() if not k.startswith("_")}
    await c.send_action(clean)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag, "submission": sub,
                                "stage": STAGE["stage"],
                                "response": sub.get("response")})
    LAST_IID["iid"] = sub.get("interactionId")
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


async def do_mulligan(c, acts, st, pid, tag):
    if not any(a.get("type") == "MulliganDecision" for a in acts):
        return False
    key = (tag, "mull", str(c.revision))
    if key in SUBMITTED_OPPS:
        return True
    SUBMITTED_OPPS.add(key)
    hand = [obj_lname(st["state"], o) for o in hand_ids(st["state"], pid)]
    n = STAGE["mulls"].get(tag, 0)
    n_lands = sum(1 for h in hand if h in (FOREST_L, ISLAND_L))
    if pid == 0:
        keep = (n_lands >= 3 and FOREST_L in hand and ISLAND_L in hand) \
            or n >= 2
    else:
        keep = n_lands >= 2 or n >= 2
    choice = "Keep" if keep else "Mulligan"
    if not keep:
        STAGE["mulls"][tag] = n + 1
    say(f"[{tag}] mulligan -> {choice} (hand={hand})")
    wire("mulligan", {"who": tag, "decision": choice})
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": choice}}})
    return True


async def do_bottom(c, acts, st, pid, tag):
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
            if is_land(get_obj(state, ref)):
                return (1, str(ref))
            return (0, str(ref))

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


def discard_rank(state, o):
    if is_land(get_obj(state, o)):
        return 0
    return 1


async def do_discard(c, acts, st, pid, tag):
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
        ranked = sorted(hand, key=lambda o: (discard_rank(state, o),
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


def find_relations_op(st):
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        if resp.get("type") == "schema" and isinstance(spec, dict) \
                and spec.get("type") == "relations":
            return opp
    return None


async def do_declare_empty(c, acts, st, pid, tag):
    for a in acts:
        if a.get("type") == "DeclareAttackers":
            d = dict(a)
            d["data"] = dict(d.get("data") or {})
            d["data"].update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
            say(f"[{tag}] declare no attackers")
            return True
        if a.get("type") == "DeclareBlockers":
            d = dict(a)
            d["data"] = dict(d.get("data") or {})
            d["data"]["assignments"] = []
            await submit_as_is(c, d)
            say(f"[{tag}] declare no blockers")
            return True
    state = st["state"]
    phase = str(state.get("phase") or "")
    if state.get("active_player") == pid and "declareattack" in phase.lower():
        opp = find_relations_op(st)
        if opp is not None:
            iid = opp.get("interactionId")
            key = (tag, "declare", str(iid))
            if key in SUBMITTED_OPPS:
                return True
            SUBMITTED_OPPS.add(key)
            say(f"[{tag}] declare empty via vi relations opportunity")
            wire("declare_empty_vi", {"who": tag})
            await interact_as(c, {"interactionId": iid,
                                  "response": {"type": "relations",
                                               "data": {"relations": []}}},
                              tag)
            return True
    return False


async def play_a_land(c, state, pid, acts, tag, prefer_lname=None):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    lands = [o for o in hand_ids(state, pid) if is_land(get_obj(state, o))]
    if not lands:
        return False
    if prefer_lname:
        lands.sort(key=lambda o: 0 if obj_lname(state, o) == prefer_lname
                   else 1)
    for o in lands:
        for a in acts:
            if a.get("type") == "PlayLand" and str(a.get("_src_oid")) == str(o):
                LAND_PLAYED_TURN[tag] = turn
                say(f"[{tag}] playing land {obj_lname(state, o)}")
                wire("play_land", {"who": tag, "oid": o})
                await submit_as_is(c, a)
                return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a.get("type") == "PassPriority":
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


def find_cast_action(acts, state, lname):
    for a in acts:
        if "cast" not in a.get("type", "").lower():
            continue
        d = a.get("data", {}) or {}
        for v in list(d.values()) + [a.get("_src_oid")]:
            try:
                if v is not None and obj_lname(state, v) == lname:
                    return a
            except (TypeError, ValueError):
                pass
    return None


def find_plus_one_action(acts, state, oko_oid):
    """Legacy ActivateAbility action for Oko's +1 (ability_index 1)."""
    for a in acts:
        if a.get("type") != "ActivateAbility":
            continue
        d = a.get("data", {}) or {}
        try:
            idx = int(d.get("ability_index", -1))
        except (TypeError, ValueError):
            continue
        if idx == 1 and str(d.get("source_id")) == str(oko_oid):
            return a
    return None


def find_plus_one_vi(st, oko_oid):
    """vi opportunity shape for Oko's +1 (activateAbility action code
    whose surfaces reference the Oko object)."""
    for opp in unanswered_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        items = data.get("choices") or data.get("candidates") or []
        for ch in items:
            codes = set(surf_codes(ch))
            if "activateAbility" not in codes:
                continue
            refs = candidate_refs(ch)
            blob = json.dumps(ch, default=str).lower()
            mentions_oko = (str(oko_oid) in refs
                            or OKO_L in choice_text(ch).lower()
                            or OKO_L in blob)
            if mentions_oko:
                return opp, ch
    return None


async def handle_oko_activation(c, st, acts, state, tag, pid):
    """Activate Oko's +1 while holding priority (before the pass gate).
    PRE is exported right before submitting the activation."""
    if ST["plus_one_done"] or ST["act_in_flight"]:
        return False
    oko_oid = ST["oko_oid"]
    if oko_oid is None:
        return False
    if not (my_main(state, pid) and my_priority(top_acts(st))):
        return False
    if len(bf_oids(state, 1, BEAR_L)) < 2:
        return False
    o = get_obj(state, oko_oid)
    try:
        lact = int(o.get("loyalty_activations_this_turn") or 0)
    except (TypeError, ValueError):
        lact = 0
    if lact != 0:
        return False

    via = None
    leg = find_plus_one_action(acts, state, oko_oid)
    if leg is not None:
        via = ("legacy-action", leg)
    else:
        res = find_plus_one_vi(st, oko_oid)
        if res is not None:
            via = ("vi",) + res

    if via is None:
        return False

    if not ST["pre_exported"]:
        s = await do_export(c, "pre.json")
        ST["pre_exported"] = True
        say("PRE exported before +1 activation")
        # re-read state after the export (it should be unchanged)
        st = st_of(c) or st
        state = st["state"]

    ST["plus_one_act_via"] = via[0]
    say(f"[{tag}] activating Oko +1 via {via[0]} (oko oid={oko_oid})")
    wire("oko_plus_one_activate", {"via": via[0], "src": str(oko_oid)})
    if via[0] == "legacy-action":
        await submit_as_is(c, via[1])
    else:
        _opp, ch = via[1], via[2]
        await answer_vi(c, _opp, ch, tag)
    ST["act_in_flight"] = True
    ST["act_turn"] = state.get("turn_number")
    return True


async def target_tick(c, pid, tag, st, state):
    """Answer Oko +1's target prompt; record the ACTUALLY submitted
    candidate reference (a string on 106)."""
    if not ST["act_in_flight"] or ST["target_oid"] is not None:
        return False
    for opp in unanswered_ops(st):
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        iid = opp.get("interactionId") or opp.get("id")

        def is_bear_choice(ch):
            blob = (choice_text(ch) + " "
                    + json.dumps(ch, default=str)).lower()
            return "grizzly" in blob

        bears = [ch for ch in chs if is_bear_choice(ch)]
        if not bears:
            continue
        pick = bears[0]
        ref = _cand_reference(pick)  # string on 106; record verbatim
        ST["target_oid"] = str(ref) if ref is not None else None
        ST["target_candidates"] = [choice_text(ch)[:60] for ch in chs]
        OBS["target_selections"].append(
            {"stage": "oko_plus_one", "who": tag,
             "iid": str(iid)[:16], "submitted_ref": ST["target_oid"],
             "candidates": ST["target_candidates"]})
        say(f"[{tag}] OKO TARGET PROMPT candidates={len(chs)} "
            f"submitted_ref={ST['target_oid']!r}")
        wire("oko_target_prompt",
             {"who": tag, "iid": str(iid)[:16],
              "submitted_ref": ST["target_oid"],
              "opportunity": json.loads(json.dumps(opp, default=str))})
        SUBMITTED_OPPS.add(iid)
        await answer_vi(c, opp, pick, tag)
        return True
    return False


async def do_export(c, path):
    s = await c.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


def drain(c):
    out = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("Error", "ActionRejected"):
            out.append((t, data))
            wire("rejected", {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {json.dumps(data, default=str)[:300]}")
    return out


# ------------------------------------------------- issue-specific ticks

def creature_sig(state, oid):
    o = get_obj(state, oid)
    ct = o.get("card_types") or {}
    return {
        "name": str(o.get("base_name") or o.get("name") or "?"),
        "zone": o.get("zone"),
        "power": o.get("power"),
        "toughness": o.get("toughness"),
        "color": o.get("color"),
        "core_types": ct.get("core_types"),
        "subtypes": ct.get("subtypes"),
        "n_abilities": len(o.get("abilities") or []),
        "loyalty": o.get("loyalty"),
        "controller": str(o.get("controller")),
        "tapped": bool(o.get("tapped")),
    }


def is_elk(sig):
    subs = [str(s).lower() for s in (sig.get("subtypes") or [])]
    colors = [str(c).lower() for c in (sig.get("color") or [])]
    return (sig.get("power") == 3 and sig.get("toughness") == 3
            and "elk" in subs and "green" in colors
            and (sig.get("n_abilities") or 0) == 0)


def is_bear(sig):
    subs = [str(s).lower() for s in (sig.get("subtypes") or [])]
    return (sig.get("power") == 2 and sig.get("toughness") == 2
            and "bear" in subs)


async def p0_tick(c, tag):
    st = st_of(c)
    if not st:
        return False
    state = st["state"]
    acts = merged_actions(st)
    if await do_mulligan(c, acts, st, 0, tag):
        return True
    if await do_bottom(c, acts, st, 0, tag):
        return True
    if await do_discard(c, acts, st, 0, tag):
        return True

    # record Oko on the battlefield
    oko = bf_oids(state, 0, OKO_L)
    if oko and ST["oko_oid"] is None:
        ST["oko_oid"] = oko[0]
        say(f"Oko on BF: oid={oko[0]} "
            f"loyalty={get_obj(state, oko[0]).get('loyalty')}")
        wire("oko_entered", {"oid": oko[0]})

    # stack-watch: detect +1 resolution, then FALL THROUGH to priority
    # logic (never return early from here -- both players must pass in
    # succession for stack entries to resolve on protocol 106).
    if (ST["act_in_flight"] and ST["target_oid"] is not None
            and stack_empty(state)):
        ST["act_in_flight"] = False
        ST["plus_one_done"] = True
        ST["mid_act_turn"] = state.get("turn_number")
        say(f"Oko +1 resolved on turn {state.get('turn_number')}; "
            f"target ref={ST['target_oid']!r}")
        wire("oko_plus_one_resolved",
             {"turn": state.get("turn_number"),
              "target_ref": ST["target_oid"]})
        if not ST["mid_exported"]:
            await do_export(c, "mid.json")
            ST["mid_exported"] = True

    # answer the +1's target prompt (a real decision; before pass gate)
    if await target_tick(c, 0, tag, st, state):
        return True

    # POST: P0's next turn after the +1 resolved
    if (ST["plus_one_done"] and ST["mid_exported"]
            and not ST["post_exported"]
            and state.get("active_player") == 0
            and (state.get("turn_number") or 0) > (ST["mid_act_turn"] or 0)
            and state.get("phase") in MAIN_PHASES
            and stack_empty(state)):
        await do_export(c, "post.json")
        ST["post_exported"] = True
        ST["post_at"] = time.time()
        STAGE["stage"] = "DONE"
        STAGE["stop"] = True
        say(f"exported POST on P0 turn {state.get('turn_number')}; stopping")
        return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 0) or my_priority(top_acts(st)):
        # cast Oko from hand (engine Auto payment)
        if not ST["oko_cast"] and not oko:
            a = find_cast_action(acts, state, OKO_L)
            if a is not None:
                ST["oko_cast"] = True
                ST["oko_cast_turn"] = state.get("turn_number")
                say(f"[{tag}] casting {OKO_T}")
                wire("cast_submit", {"tag": "oko"})
                await submit_as_is(c, a)
                return True
        # activate the +1 (while holding priority)
        if await handle_oko_activation(c, st, acts, state, tag, 0):
            return True
        # play a land
        if await play_a_land(c, state, 0, acts, tag):
            return True

    # never hold priority while watching the stack: fall through to pass
    if real_decision_pending(st):
        return True
    if my_priority(top_acts(st)):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, top_acts(st))
            PASSED_REV[c.name] = c.revision
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
    if await do_discard(c, acts, st, 1, tag):
        return True
    if await do_declare_empty(c, acts, st, 1, tag):
        return True
    # target prompt only ever appears for the ability controller (P0),
    # but keep the handler here for symmetry/safety
    if await target_tick(c, 1, tag, st, state):
        return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 1) or my_priority(top_acts(st)):
        # cast bears (engine Auto payment), up to 3
        if (len(bf_oids(state, 1, BEAR_L)) < 3
                and state.get("phase") in MAIN_PHASES
                and state.get("active_player") == 1):
            a = find_cast_action(acts, state, BEAR_L)
            if a is not None:
                say(f"[{tag}] casting {BEAR_T}")
                wire("cast_submit", {"tag": "bear"})
                await submit_as_is(c, a)
                return True
        if await play_a_land(c, state, 1, acts, tag):
            return True

    if real_decision_pending(st):
        return True
    if my_priority(top_acts(st)):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, top_acts(st))
            PASSED_REV[c.name] = c.revision
        return True
    return False


# ------------------------------------------------------------- main loop

async def main():
    t0 = time.time()
    last_rev_change = t0
    game_started = False

    hello = await verify_server_hello()
    data_level = check_data_level()

    p0 = PhaseClient("P06917r")
    await p0.connect()
    say("P0 creating game (default Bo1, 2 seats)...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P16917r")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    STAGE["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game", {"code": p0.game_code, "p0": p0.player_id, "p1": p1.player_id,
                  "p0_deck": P0_DECK, "p1_deck": P1_DECK})

    last_rev = {}
    last_tick_at = {}
    last_diag = 0.0
    while time.time() - t0 < GAME_TIMEOUT and not STAGE.get("stop"):
        await asyncio.sleep(0.25)
        for c, tag, tick in ((p0, "P0", p0_tick), (p1, "P1", p1_tick)):
            rej = drain(c)
            if rej:
                if LAST_IID["iid"] in SUBMITTED_OPPS:
                    SUBMITTED_OPPS.discard(LAST_IID["iid"])
                    say(f"[{c.name}] resync: retrying {LAST_IID['iid']} "
                        f"after rejection")
                    LAST_IID["iid"] = None
                OBS["rejections"].extend(
                    {"at": time.time(), "who": c.name, "type": r[0],
                     "data": r[1]} for r in rej)
            st = st_of(c)
            if not st:
                continue
            if c.revision != last_rev.get(c.name):
                last_rev[c.name] = c.revision
                last_rev_change = time.time()
                if (st.get("state") or {}).get("turn_number", 0) >= 1:
                    game_started = True
            else:
                # 5s re-tick backstop: re-tick a client holding priority
                # with no revision change (missed-broadcast resilience).
                if not (my_priority(top_acts(st))
                        and time.time() - last_tick_at.get(c.name, 0) > 5):
                    continue
            last_tick_at[c.name] = time.time()
            try:
                await tick(c, tag)
            except Exception as e:
                say(f"tick error {c.name}: {type(e).__name__}: {e}")
                OBS["tick_errors"].append(
                    {"who": c.name, "err": f"{type(e).__name__}: {e}"[:200]})

        st = st_of(p0)
        if not st:
            continue
        state = st["state"]
        turn = state.get("turn_number") or 0

        if str(state.get("phase") or "").lower() == "gameover":
            OBS["notes"].append("game over before sequence completed")
            say("game over before sequence completed")
            STAGE["stop"] = True
            continue

        if game_started and not STAGE.get("stop") \
                and time.time() - last_rev_change > STALL_AFTER:
            OBS["notes"].append(f"stall: no revision for {STALL_AFTER}s")
            say(f"STALL: no revision for {STALL_AFTER}s; stopping")
            wire("stall", {"stage": STAGE["stage"]})
            try:
                await do_export(p0, "mid_stall.json")
            except Exception as e:
                say(f"mid_stall export failed: {e}")
            STAGE["stop"] = True
            continue

        if turn > TURN_CAP and not STAGE.get("stop"):
            say(f"TURN CAP {TURN_CAP} reached; stopping")
            wire("turn_cap", {"turn": turn})
            STAGE["stop"] = True
            continue

        if time.time() - last_diag > 60:
            last_diag = time.time()
            say(f"DIAG turn={turn} active={state.get('active_player')} "
                f"phase={state.get('phase')} pp={state.get('priority_player')} "
                f"P0lands={len(untapped_lands(state, 0))} "
                f"P1bears={len(bf_oids(state, 1, BEAR_L))} "
                f"oko={ST['oko_oid']} cast={ST['oko_cast']} "
                f"act={ST['plus_one_done']} mid={ST['mid_exported']} "
                f"post={ST['post_exported']} "
                f"stack={len(state.get('stack') or [])}")

    say(f"loop ended: stage={STAGE['stage']} elapsed={time.time()-t0:.0f}s")
    wire("loop_end", {"stage": STAGE["stage"]})

    await finish(p0, p1, t0, hello, data_level)


async def finish(p0, p1, t0, hello, data_level):
    # ------------------------------------------------------- assertions
    A, D = {}, {}

    def load(p):
        try:
            with open(f"{EVDIR}/{p}") as f:
                return json.load(f)["state"]
        except Exception:
            return None

    pre_s = load("pre.json")
    mid_s = load("mid.json")
    post_s = load("post.json")

    # A1: setup
    if pre_s is not None and ST["oko_oid"] is not None:
        o = get_obj(pre_s, ST["oko_oid"])
        bears = bf_oids(pre_s, 1, BEAR_L)
        ok = (o.get("zone") == "Battlefield" and o.get("loyalty") == 4
              and len(bears) >= 2)
        A["A1_setup_ok"] = "passed" if ok else "failed"
        D["A1_setup_ok"] = (
            f"oko_oid={ST['oko_oid']} loyalty={o.get('loyalty')} "
            f"zone={o.get('zone')} p1_bears={len(bears)} "
            f"(expect loyalty 4 on BF, >=2 bears)")
    else:
        A["A1_setup_ok"] = "failed"
        D["A1_setup_ok"] = ("pre.json missing or Oko never reached the "
                            "battlefield")

    # A2: activation (+1 resolved: loyalty 4 -> 5)
    if mid_s is not None and ST["oko_oid"] is not None:
        loy = get_obj(mid_s, ST["oko_oid"]).get("loyalty")
        ST["oko_loyalty_mid"] = loy
        A["A2_activation"] = "passed" if loy == 5 else "failed"
        D["A2_activation"] = (
            f"oko loyalty in MID={loy} (expect 5); "
            f"plus_one_done={ST['plus_one_done']} "
            f"activation_via={ST['plus_one_act_via']}")
    else:
        A["A2_activation"] = "failed"
        D["A2_activation"] = "mid.json missing or no oko_oid"

    # A3: elk this turn
    if mid_s is not None and ST["target_oid"] is not None:
        sig = creature_sig(mid_s, ST["target_oid"])
        elk = is_elk(sig)
        A["A3_elk_this_turn"] = "passed" if elk else "failed"
        D["A3_elk_this_turn"] = (
            f"target ref={ST['target_oid']!r} MID sig: "
            f"{sig['power']}/{sig['toughness']} subtypes={sig['subtypes']} "
            f"color={sig['color']} n_abilities={sig['n_abilities']} "
            f"zone={sig['zone']} -> is_elk={elk}")
    else:
        A["A3_elk_this_turn"] = "failed"
        D["A3_elk_this_turn"] = "mid.json missing or no target ref recorded"

    # A4: elk persists to P0's next turn
    if post_s is not None and ST["target_oid"] is not None:
        sig = creature_sig(post_s, ST["target_oid"])
        elk = is_elk(sig)
        bear = is_bear(sig)
        D["A4_elk_persists"] = (
            f"target ref={ST['target_oid']!r} POST sig: "
            f"{sig['power']}/{sig['toughness']} subtypes={sig['subtypes']} "
            f"color={sig['color']} n_abilities={sig['n_abilities']} "
            f"zone={sig['zone']} -> is_elk={elk} is_bear={bear}")
        if elk:
            A["A4_elk_persists"] = "passed"
            D["A4_elk_persists"] += " -> effect persisted: bug NOT reproduced."
        elif bear:
            A["A4_elk_persists"] = "failed"
            D["A4_elk_persists"] += (" -> REVERTED to 2/2 Bear after turn "
                                     "end: the reported duration bug is "
                                     "REPRODUCED.")
        else:
            A["A4_elk_persists"] = "failed"
            D["A4_elk_persists"] += " -> unexpected form."
    else:
        A["A4_elk_persists"] = "failed"
        D["A4_elk_persists"] = "post.json missing or no target ref recorded"

    # A5: cleanup (waiting_for is null on 106; stack empty = proceeding)
    if post_s is not None:
        stack_clear = stack_empty(post_s)
        A["A5_cleanup"] = "passed" if stack_clear else "failed"
        D["A5_cleanup"] = (f"stack_empty={stack_clear} "
                           f"turn={post_s.get('turn_number')}")
    else:
        A["A5_cleanup"] = "not-run"
        D["A5_cleanup"] = "no post.json"

    # ---------------------------------------------------------- verdict
    if A["A1_setup_ok"] != "passed":
        verdict = "blocked"
    elif (A["A2_activation"] == "passed"
          and A["A3_elk_this_turn"] == "passed"
          and A["A4_elk_persists"] == "failed"):
        verdict = "reproduced"
    elif all(A.get(k) == "passed" for k in
             ("A1_setup_ok", "A2_activation", "A3_elk_this_turn",
              "A4_elk_persists", "A5_cleanup")):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    for k in ("A1_setup_ok", "A2_activation", "A3_elk_this_turn",
              "A4_elk_persists", "A5_cleanup"):
        say(f"{k}: {A[k]}")
    say(f"verdict={verdict}")
    OBS["notes"].append(f"verdict={verdict}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"issue": ISSUE, "run_id": RUN_ID, "verdict": verdict,
                   "assertions": A, "details": D,
                   "notes": OBS.get("notes", [])}, f, indent=1, default=str)
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"issue": ISSUE, "run_id": RUN_ID,
                   "target_selections": OBS["target_selections"],
                   "unexpected_prompts": OBS["unexpected_prompts"],
                   "auto_answered": OBS["auto_answered"],
                   "tick_errors": OBS["tick_errors"],
                   "rejections": OBS["rejections"],
                   "notes": OBS["notes"]}, f, indent=1, default=str)

    date_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0))
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "date": date_iso,
        "game_code": STAGE.get("game_code"),
        "server": {
            "server_version": hello.get("server_version"),
            "build_commit": hello.get("build_commit"),
            "protocol_version": hello.get("protocol_version"),
            "mode": hello.get("mode"),
            "server_binary_sha256": SERVER_IDENTITY["server_binary_sha256"],
            "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
            "draft_pools_sha256": SERVER_IDENTITY["draft_pools_sha256"],
            "signature_verified": SERVER_IDENTITY["signature_verified"],
        },
        "driver": {"protocol_advertised": 106, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(__file__),
        "decks": {"P0": [[n, c] for n, c in P0_DECK],
                  "P1": [[n, c] for n, c in P1_DECK]},
        "format_config": "default Bo1 (2 human-client seats)",
        "setup_line": ("P0 4x Oko, Thief of Crowns / 28x Forest / 28x Island; "
                       "P1 12x Grizzly Bears / 48x Forest. P0 casts Oko, P1 "
                       "casts 2+ bears, P0 activates Oko's +1 on a later "
                       "main phase targeting one bear."),
        "contract_line": ("Oko's +1 has no duration: the target should stay "
                          "a green 3/3 Elk with no abilities indefinitely. "
                          "reproduced iff it reverts to a 2/2 Bear on P0's "
                          "next turn."),
        "driver_notes": [
            "Protocol-106 port of driver/scenario_6917.py (v0.81.3 / "
            "protocol 70) for pinned v0.103.0; behavioral contract "
            "A1..A5 and the verdict rule unchanged.",
            "waiting_for is gone (null); priority = top-level "
            "PassPriority; all decisions via viewer_interaction; "
            "MulliganDecision via legacy Action; bottom via vi "
            "schema/select gated on waitingForKind.code=='mulligan'; "
            "DiscardToHandSize via vi schema/select (the ONLY accepted "
            "shape for a select schema).",
            "CastSpell via legacy Action; the v0.103.0 engine auto-taps "
            "reliably, so no driver mana taps (driver taps on top of "
            "engine auto-taps double-pay). Oko's +1 costs +1 loyalty: "
            "no mana payment involved.",
            "ActivateAbility is submitted while holding priority, "
            "before the priority-pass gate (legacy Action preferred; "
            "vi choose/select fallback); the stack-watch branch falls "
            "through to the pass gate (never hold priority while "
            "watching the stack).",
            "Target candidate references arrive as strings on 106; the "
            "ACTUALLY submitted reference is recorded verbatim at "
            "answer time in ST.target_oid and the wire log.",
            "Data-level: v0.103.0 card-data parses Oko's +1 with no "
            "duration ('until end of turn' absent); the defect is at "
            "resolution/effect-duration time.",
            "Pre/post/mid states are authoritative exports (data.state "
            "parsed once from the export envelope) via the host client "
            "only; the reported OUTCOME is asserted on the saved "
            "states, not the prompt.",
        ],
        "assertions": A,
        "assertion_details": D,
        "driver_state": ST,
        "data_level": data_level,
        "verdict": verdict,
        "evidence_comment_id": 5652116124,
        "limitations": [
            "Browser UI not exercised; native engine via two "
            "human-client seats.",
            "4x Oko / 12x Grizzly Bears density is a test-harness "
            "convenience (engine accepts >4-of for custom games).",
            "'Loses all abilities' is not separately asserted: Grizzly "
            "Bears has no abilities; the reported defect is the effect "
            "duration (until-EOT vs indefinite).",
            "The prebuilt server has no standalone state-restore; "
            "states are authoritative exports (restorable only via full "
            "game replay).",
        ],
        "mulligans": STAGE["mulls"],
        "rejections": OBS["rejections"],
        "notes": OBS["notes"],
        "duration_s": round(time.time() - t0, 1),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    say(f"wrote run.json verdict={verdict}")

    with open(__file__) as f:
        src = f.read()
    with open(f"{EVDIR}/scenario_6917_01030.py", "w") as f:
        f.write(src)
    shutil.copy(f"{BACKFILL}/runs/run-20261007-6917/server.log",
                f"{EVDIR}/server.log")
    say("copied scenario_6917_01030.py and server.log into EVDIR")

    render_summary(run, pre_s, mid_s, post_s)
    write_manifest()

    WIRE.close()
    RUNLOG.close()
    for c in (p0, p1):
        try:
            await c.close()
        except Exception:
            pass
    say("scenario finished")


def render_summary(run, pre_s, mid_s, post_s):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        say("PIL missing; skipping summary.png")
        return
    W, H = 1000, 880
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #6917 - Oko, Thief of Crowns +1",
           fill=(235, 240, 250))
    y += 28
    d.text((24, y),
           "server v0.103.0 (ec27a8d) protocol 106 - 2026-10-07 - "
           "elk effect duration (indefinite vs until-EOT)",
           fill=(140, 160, 180))
    y += 28
    vcol = (255, 90, 90) if run["verdict"] == "reproduced" else (
        (120, 220, 120) if run["verdict"] == "not-reproduced"
        else (230, 200, 120))
    d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vcol)
    y += 34
    d.text((24, y), "Assertions (from saved states):", fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "PRE: Oko loyalty 4 on BF; >=2 Bears on P1 BF",
        "A2_activation": "Oko loyalty 4->5 (+1 resolved)",
        "A3_elk_this_turn": "MID: target is green 3/3 Elk, no abilities",
        "A4_elk_persists": "POST (P0 next turn): target STILL 3/3 Elk",
        "A5_cleanup": "stack empty, game proceeds",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (160, 160, 160))
        d.text((36, y), f"{k}: {v} - {lab}", fill=col)
        y += 24
    y += 10
    d.text((24, y), "Target creature signature across states "
                    "(from pre/mid/post.json):", fill=(200, 210, 225))
    y += 24
    tgt = run["driver_state"].get("target_oid")
    for label, st in (("pre ", pre_s), ("mid ", mid_s), ("post", post_s)):
        if st is not None and tgt is not None:
            s = creature_sig(st, tgt)
            line = (f"{label}: ref={tgt} {s['power']}/{s['toughness']} "
                    f"{s['subtypes']} {s['color']} "
                    f"abilities={s['n_abilities']} zone={s['zone']}")
        else:
            line = f"{label}: (no state)"
        d.text((36, y), line[:112], fill=(150, 160, 175))
        y += 22
    y += 10
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in (run["notes"] or [])[:4] + \
            [run["assertion_details"].get(k, "")
             for k in labels]:
        d.text((36, y), str(n)[:116], fill=(150, 160, 175))
        y += 22
        if y > H - 40:
            break
    img.save(f"{EVDIR}/summary.png")
    say("wrote summary.png")


def write_manifest():
    files = ["pre.json", "mid.json", "post.json", "run.json",
             "assertions.json", "observations.json", "data_evidence.json",
             "scenario_6917_01030.py", "wire_log.jsonl", "scenario_run.log",
             "server.log", "summary.png"]
    lines = []
    for fn in files:
        p = f"{EVDIR}/{fn}"
        if os.path.exists(p):
            h = hashlib.sha256(open(p, "rb").read()).hexdigest()
            lines.append(f"{h}  {fn}")
        else:
            say(f"manifest: MISSING {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    say(f"wrote manifest.sha256 ({len(lines)} files)")


if __name__ == "__main__":
    asyncio.run(main())
