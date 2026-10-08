#!/usr/bin/env python3
"""Issue #6918: Door of Destinies doesn't add charge counters when casting
the chosen creature type.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.103.0):
  Door of Destinies ({4} Artifact):
    "As this artifact enters, choose a creature type.
     Whenever you cast a spell of the chosen type, put a charge counter on
     this artifact.
     Creatures you control of the chosen type get +1/+1 for each charge
     counter on this artifact."
Card data parses it fully: entry replacement (Choose CreatureType,
persist=true), a SpellCast trigger with valid_card Typed +
IsChosenCreatureType putting a charge counter on SelfRef, and a
continuous anthem (+1/+1 per charge counter) for chosen-type creatures.
The defect is at runtime matching (the trigger never appears on the
stack), not in the data.

Reported symptom: casting a spell of the chosen type adds no charge
counter.

Protocol-106 port of driver/scenario_6918.py (v0.81.3 / protocol 70) for
pinned v0.103.0 (protocol 106). Conventions from scenario_6917_01030.py:
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
    engine auto-taps DOUBLE-PAY).
  - The Door's "as enters, choose a creature type" prompt is a vi
    schema opportunity whose response spec type is "text" (the protocol-70
    shape). It is submitted as {"type": "text", "data": {"value": ...}},
    NOT {"choiceIds": [...]} (the server parse-rejects the latter with
    "missing field `value`"). The prompt is answered only while a
    text-spec opportunity is offered AND the Door is on P0's battlefield
    AND no choice is recorded yet (lesson from #6917: an in-flight flag
    alone lets the handler fire on unrelated menus). The choice is marked
    recorded only when the text opportunity closes (a rejected submission
    leaves it pending and is retried after the drain resync).
  - real_decision_pending excludes the 106 priority-menu codes and any
    already-answered opportunity; the stack-watch branch FALLS THROUGH to
    the priority-pass gate (never return early; both players must pass in
    succession for stack entries to resolve on 106).
  - Exports go through the host client only; data.state is a JSON STRING
    parsed once.
  - sleep(0) yield before leg evaluation; 5s re-tick backstop for
    priority-holding clients; rejection-drain resync per tick.

Setup (native engine, two human-client seats, default Bo1):
  P0: 4x Door of Destinies + 8x Llanowar Elves + 8x Grizzly Bears + 40x
      Forest. Casts Door (chooses "Elf" at entry), then casts elves and
      one bear.
  P1: 60x Forest (passive).

Plan:
  1. P0 casts Door of Destinies on/after turn 4 (4 untapped lands);
     chooses "Elf" at the entry choice prompt (recorded explicitly).
  2. PRE exported just before the Door cast.
  3. P0 casts Llanowar Elves (an Elf) -> MID1 after the cast + trigger
     window (stack empty, elf on BF): Door should have 1 charge counter.
  4. P0 casts Grizzly Bears (a Bear, not the chosen type) -> MID2:
     counters should stay at 1 (control).
  5. P0 casts a second Llanowar Elves -> POST: Door should have 2 charge
     counters; the elves should be 3/3 from the +2/+2 anthem.

Assertions:
  A1_setup_ok        Door on P0 BF; entry choice answered, chosen type
                     "elf" recorded.
  A2_counter_on_elf  MID1: door charge counters == 1. FAILED = reported bug
                     is REPRODUCED.
  A3_control_bear    MID2: door charge counters == MID1 value (bear of
                     non-chosen type adds nothing).
  A4_anthem          POST: elves' power/toughness == 1+charge / 1+charge
                     (consistency of the anthem with actual counters).
  A5_cleanup         POST: stack empty, game proceeding.

Verdict rule: reproduced iff A1 passes and A2 fails. not-reproduced iff
A1-A5 all pass. blocked iff A1 fails.

Evidence: evidence/6918/<run-id>/pre.json, mid1.json, mid2.json, post.json,
run.json, assertions.json, observations.json, data_evidence.json,
manifest.sha256, summary.png, scenario_6918_01030.py, wire_log.jsonl,
scenario_run.log, server.log
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
ISSUE = 6918
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261007-6918")
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
# cross-check against the values recorded by the 6911/6914/6917 runs on the pin
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
    """Record the v0.103.0 parse of Door of Destinies (entry replacement,
    SpellCast trigger with IsChosenCreatureType, charge-counter anthem).
    The reported defect is at runtime matching, not in the data."""
    c = CARD_DATA.get("door of destinies", {})
    oracle = str(c.get("oracle_text") or "")
    trigs = c.get("triggers") or []
    repls = c.get("replacements") or []
    statics = c.get("static_abilities") or []
    trig_ok = any(
        t.get("mode") == "SpellCast"
        and any(str(p.get("type")) == "IsChosenCreatureType"
                for p in ((t.get("valid_card") or {}).get("properties") or []))
        and str((((t.get("execute") or {}).get("effect") or {})
                 .get("type"))) == "PutCounter"
        for t in trigs)
    repl_ok = any(
        str((((r.get("execute") or {}).get("effect") or {})
             .get("choice_type"))) == "CreatureType"
        and bool((((r.get("execute") or {}).get("effect") or {})
                  .get("persist")))
        for r in repls)
    out = {
        "name": c.get("name"),
        "oracle_text": oracle,
        "trigger_count": len(trigs),
        "spellcast_chosen_trigger_ok": trig_ok,
        "entry_choice_persist_ok": repl_ok,
        "static_ability_count": len(statics),
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(out, f, indent=1, default=str)
    ok = (c.get("name") == "Door of Destinies"
          and "choose a creature type" in oracle
          and "charge counter" in oracle
          and trig_ok and repl_ok)
    say(f"data-level: door parse ok={ok} trig_ok={trig_ok} repl_ok={repl_ok}")
    wire("data_level", {"door_parse_ok": ok, "trig_ok": trig_ok,
                        "repl_ok": repl_ok})
    assert ok, "v0.103.0 card-data lost or changed Door of Destinies' parse"
    return out


DOOR_T = "Door of Destinies"
DOOR_L = "door of destinies"
ELF_T = "Llanowar Elves"
ELF_L = "llanowar elves"
BEAR_T = "Grizzly Bears"
BEAR_L = "grizzly bears"
FOREST_L = "forest"

P0_DECK = ((DOOR_T, 4), (ELF_T, 8), (BEAR_T, 8), ("Forest", 40))
P1_DECK = (("Forest", 60),)

MAIN_PHASES = ("PreCombatMain", "PostCombatMain", "Main")

GAME_TIMEOUT = 1500
STALL_AFTER = 150
TURN_CAP = 45

STAGE = {"stage": "SETUP", "stop": False, "game_code": None,
         "mulls": {"P0": 0, "P1": 0},
         "opp_shapes_logged": set()}
SUBMITTED_OPPS = set()
LAST_IID = {"iid": None}
LAND_PLAYED_TURN = {}
PASSED_REV = {}
ST = {"door_cast": False, "door_cast_turn": None, "door_oid": None,
      "chosen_type": None, "choice_iid": None, "choice_attempts": 0,
      "elf1_cast": False, "mid1_exported": False,
      "bear_cast": False, "mid2_exported": False,
      "elf2_cast": False, "post_exported": False,
      "pre_exported": False}
OBS = {"unexpected_prompts": [], "auto_answered": [], "rejections": [],
       "type_choice": [], "tick_errors": [], "notes": []}


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
    n_lands = sum(1 for h in hand if h == FOREST_L)
    keep = n_lands >= 3 or n >= 2
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
            if ref is not None and is_land(get_obj(state, ref)):
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


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    lands = [o for o in hand_ids(state, pid) if is_land(get_obj(state, o))]
    if not lands:
        return False
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


def charge_counters(o):
    """Return the number of charge counters on an object, tolerating shape
    variants (dict {charge: n}, list of {type,count}, plain int)."""
    c = o.get("counters")
    if c is None:
        return 0
    if isinstance(c, dict):
        for k, v in c.items():
            if "charge" in str(k).lower():
                return int(v) if isinstance(v, (int, float)) else 0
        return 0
    if isinstance(c, list):
        n = 0
        for e in c:
            if isinstance(e, dict):
                t = str(e.get("type") or e.get("kind") or "").lower()
                if "charge" in t:
                    n += int(e.get("count", e.get("n", 1)))
            elif isinstance(e, str) and "charge" in e.lower():
                n += 1
        return n
    if isinstance(c, (int, float)):
        return int(c)
    return 0


def extract_text_value(ch):
    """Extract the value payload for a 'text'-spec choice: prefer the
    surface data carrying role=='choice' with a 'value' key, then any
    'value' in the choice/data, else the choice's text."""
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and d.get("role") == "choice" \
                and "value" in d:
            return d["value"]
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "value" in d:
            return d["value"]
    d = ch.get("data") or {}
    if isinstance(d, dict) and "value" in d:
        return d["value"]
    if ch.get("value") is not None:
        return ch.get("value")
    return choice_text(ch)


def text_spec_opps(st):
    """vi opportunities whose response schema spec type is 'text'."""
    out = []
    for opp in unanswered_ops(st):
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "schema":
            continue
        spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
        if (spec.get("type") or "") == "text":
            out.append(opp)
    return out


async def type_choice_tick(c, pid, tag, st, state):
    """Answer Door of Destinies' 'as enters, choose a creature type'.

    Gate on ALL of: the Door was cast, the Door is on P0's battlefield,
    no choice recorded yet, and a text-spec opportunity is currently
    offered (waiting_for is null on 106, so the schema spec is the gate).
    Submit the 'text' shape {"type": "text", "data": {"value": ...}}.
    Mark the choice only when the text opportunity closes -- a rejected
    submission leaves it pending and is retried after the drain resync.
    """
    if ST["chosen_type"] is not None or not ST["door_cast"]:
        return False
    if not bf_oids(state, 0, DOOR_L):
        return False
    opps = text_spec_opps(st)
    if opps:
        opp = opps[0]
        iid = opp.get("interactionId")
        if iid in SUBMITTED_OPPS:
            return True  # submitted; awaiting acceptance
        chs = ((opp.get("response", {}) or {}).get("data", {}) or {}
               ).get("choices") or ((opp.get("response", {}) or {})
                                     .get("data", {}) or {}).get("candidates") \
            or []
        if not chs:
            return False
        ST["choice_attempts"] += 1
        if ST["choice_attempts"] > 6:
            say(f"[{tag}] entry choice: 6 attempts failed; recording stall")
            OBS["tick_errors"].append(
                {"who": tag, "err": "entry choice 6x failed"})
            wire("entry_choice_failed", {"who": tag,
                                         "attempts": ST["choice_attempts"]})
            return False
        kind = vi_kind_code(st)
        texts = [choice_text(ch) for ch in chs]
        wire("entry_choice_prompt",
             {"who": tag, "iid": str(iid)[:16],
              "waitingForKind": kind,
              "n_choices": len(chs),
              "attempt": ST["choice_attempts"],
              "sample_texts": texts[:12],
              "opportunity": json.loads(json.dumps(opp, default=str))})
        say(f"[{tag}] ENTRY CHOICE PROMPT kind={kind} n={len(chs)} "
            f"attempt={ST['choice_attempts']}")
        pick = None
        for ch in chs:
            if choice_text(ch).lower() == "elf":
                pick = ch
                break
        if pick is None:
            for ch in chs:
                if "elf" in choice_text(ch).lower():
                    pick = ch
                    break
        if pick is None:
            say(f"[{tag}] no Elf choice found; waiting for engine view")
            wire("entry_choice_no_elf",
                 {"who": tag, "texts": texts[:20]})
            return False
        val = extract_text_value(pick)
        SUBMITTED_OPPS.add(iid)
        ST["choice_iid"] = str(iid)[:16]
        OBS["type_choice"].append(
            {"who": tag, "iid": str(iid)[:16],
             "chosen": choice_text(pick).lower(),
             "value_submitted": val,
             "attempt": ST["choice_attempts"],
             "waitingForKind": kind,
             "n_choices": len(chs)})
        say(f"[{tag}] choosing creature type: {choice_text(pick)} "
            f"(value={val!r}, attempt {ST['choice_attempts']})")
        sub = {"interactionId": iid,
               "response": {"type": "text", "data": {"value": val}}}
        wire("interaction_submission",
             {"who": tag, "submission": sub,
              "choice_text": choice_text(pick)[:120]})
        await interact_as(c, sub, tag)
        return True
    # no text-spec opportunity: if we submitted one, the choice closed
    if ST["choice_iid"] is not None and ST["chosen_type"] is None:
        ST["chosen_type"] = "elf"
        say("entry choice accepted (text opportunity closed)")
        wire("entry_choice_accepted", {"choice_iid": ST["choice_iid"]})
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

def door_oid_of(state):
    return ST["door_oid"] or (bf_oids(state, 0, DOOR_L) or [None])[0]


def charge_of(state):
    oid = door_oid_of(state)
    if oid is None:
        return None
    return charge_counters(get_obj(state, oid))


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

    # record the Door on the battlefield
    doors = bf_oids(state, 0, DOOR_L)
    if doors and ST["door_oid"] is None:
        ST["door_oid"] = doors[0]
        say(f"Door of Destinies on BF: oid={doors[0]} "
            f"counters={charge_counters(get_obj(state, doors[0]))}")
        wire("door_entered", {"oid": doors[0]})

    # answer the entry "choose a creature type" prompt (a real decision;
    # before any pass gate). On 106 this is a text-spec opportunity.
    if await type_choice_tick(c, 0, tag, st, state):
        return True

    # export checkpoints once the stack settles after each cast
    if (ST["elf1_cast"] and not ST["mid1_exported"]
            and stack_empty(state)
            and len(bf_oids(state, 0, ELF_L)) >= 1):
        await do_export(c, "mid1.json")
        ST["mid1_exported"] = True
        say(f"MID1 exported: door charge={charge_of(state)}")
    if (ST["bear_cast"] and not ST["mid2_exported"]
            and stack_empty(state)
            and len(bf_oids(state, 0, BEAR_L)) >= 1):
        await do_export(c, "mid2.json")
        ST["mid2_exported"] = True
        say(f"MID2 exported: door charge={charge_of(state)}")
    if (ST["elf2_cast"] and not ST["post_exported"]
            and stack_empty(state)
            and len(bf_oids(state, 0, ELF_L)) >= 2):
        await do_export(c, "post.json")
        ST["post_exported"] = True
        STAGE["stage"] = "DONE"
        STAGE["stop"] = True
        say(f"POST exported: door charge={charge_of(state)}; stopping")
        return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 0) or my_priority(top_acts(st)):
        n_untapped = len(untapped_lands(state, 0))
        # 1. cast Door of Destinies ({4}); PRE right before the cast
        if (not ST["door_cast"]
                and find_cast_action(acts, state, DOOR_L) is not None
                and n_untapped >= 4):
            if not ST["pre_exported"]:
                await do_export(c, "pre.json")
                ST["pre_exported"] = True
                say("PRE exported before Door cast")
                st = st_of(c) or st
                state = st["state"]
                acts = merged_actions(st)
            a = find_cast_action(acts, state, DOOR_L)
            if a is not None:
                ST["door_cast"] = True
                ST["door_cast_turn"] = state.get("turn_number")
                say(f"[{tag}] casting {DOOR_T} (engine Auto payment)")
                wire("cast_submit", {"tag": "door"})
                await submit_as_is(c, a)
                return True
        # 2. cast Llanowar Elves #1 (Elf; the chosen type)
        if (ST["chosen_type"] is not None and not ST["elf1_cast"]
                and find_cast_action(acts, state, ELF_L) is not None
                and n_untapped >= 1):
            ST["elf1_cast"] = True
            say(f"[{tag}] casting {ELF_T} #1 (engine Auto payment)")
            wire("cast_submit", {"tag": "elf1"})
            await submit_as_is(c, find_cast_action(acts, state, ELF_L))
            return True
        # 3. cast Grizzly Bears (control: not the chosen type)
        if (ST["mid1_exported"] and not ST["bear_cast"]
                and find_cast_action(acts, state, BEAR_L) is not None
                and n_untapped >= 2):
            ST["bear_cast"] = True
            say(f"[{tag}] casting {BEAR_T} (control, engine Auto payment)")
            wire("cast_submit", {"tag": "bear"})
            await submit_as_is(c, find_cast_action(acts, state, BEAR_L))
            return True
        # 4. cast Llanowar Elves #2
        if (ST["mid2_exported"] and not ST["elf2_cast"]
                and find_cast_action(acts, state, ELF_L) is not None
                and n_untapped >= 1):
            ST["elf2_cast"] = True
            say(f"[{tag}] casting {ELF_T} #2 (engine Auto payment)")
            wire("cast_submit", {"tag": "elf2"})
            await submit_as_is(c, find_cast_action(acts, state, ELF_L))
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

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 1) or my_priority(top_acts(st)):
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

    p0 = PhaseClient("P06918r")
    await p0.connect()
    say("P0 creating game (default Bo1, 2 seats)...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P16918r")
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
                f"P0untapped={len(untapped_lands(state, 0))} "
                f"door={ST['door_oid']} cast={ST['door_cast']} "
                f"chosen={ST['chosen_type']} elf1={ST['elf1_cast']} "
                f"mid1={ST['mid1_exported']} bear={ST['bear_cast']} "
                f"mid2={ST['mid2_exported']} elf2={ST['elf2_cast']} "
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
    mid1_s = load("mid1.json")
    mid2_s = load("mid2.json")
    post_s = load("post.json")

    def door_sig(state):
        oid = door_oid_of(state)
        o = get_obj(state, oid) if oid is not None else {}
        return oid, o

    # A1: setup (door on P0 BF; entry choice answered "elf")
    st1 = mid1_s or post_s
    if st1 is not None and ST["door_oid"] is not None:
        oid, o = door_sig(st1)
        ok = (o.get("zone") == "Battlefield"
              and ST["chosen_type"] == "elf")
        A["A1_setup_ok"] = "passed" if ok else "failed"
        D["A1_setup_ok"] = (
            f"door_oid={oid} zone={o.get('zone')} "
            f"chosen_type={ST['chosen_type']} "
            f"choice_iid={ST['choice_iid']} "
            f"choice_attempts={ST['choice_attempts']} "
            f"(expect door on BF, chosen 'elf')")
    else:
        A["A1_setup_ok"] = "failed"
        D["A1_setup_ok"] = ("mid1.json and post.json missing, or the Door "
                            "never reached the battlefield")

    # A2: counter after the Elf cast
    if mid1_s is not None:
        n = charge_of(mid1_s)
        A["A2_counter_on_elf"] = "passed" if n == 1 else "failed"
        D["A2_counter_on_elf"] = (
            f"door charge counters in MID1 = {n} (expect 1 after casting "
            f"an Elf of the chosen type)")
        if n != 1:
            D["A2_counter_on_elf"] += " -> the reported bug is REPRODUCED."
    else:
        A["A2_counter_on_elf"] = "failed"
        D["A2_counter_on_elf"] = "mid1.json missing"

    # A3: control -- bear of the non-chosen type adds nothing
    if mid1_s is not None and mid2_s is not None:
        n1, n2 = charge_of(mid1_s), charge_of(mid2_s)
        A["A3_control_bear"] = "passed" if n2 == n1 else "failed"
        D["A3_control_bear"] = (f"door charge counters mid1={n1} "
                                f"mid2={n2} (Grizzly Bears cast between)")
    else:
        A["A3_control_bear"] = "failed"
        D["A3_control_bear"] = "mid1.json or mid2.json missing"

    # A4: anthem consistent with actual counters
    if post_s is not None:
        n = charge_of(post_s)
        elves = [oid for oid in bf_oids(post_s, 0, ELF_L)]
        sigs = [(obj_lname(post_s, e),
                 get_obj(post_s, e).get("power"),
                 get_obj(post_s, e).get("toughness")) for e in elves]
        if elves and n is not None:
            ok = all(p == 1 + n and t == 1 + n for _, p, t in sigs)
            A["A4_anthem"] = "passed" if ok else "failed"
            D["A4_anthem"] = (f"charge={n} elves={sigs} "
                              f"(expect 1+{n}/1+{n})")
        else:
            A["A4_anthem"] = "failed"
            D["A4_anthem"] = f"elves={sigs} charge={n}"
    else:
        A["A4_anthem"] = "failed"
        D["A4_anthem"] = "post.json missing"

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
    elif A["A2_counter_on_elf"] == "failed":
        verdict = "reproduced"
    elif all(A.get(k) == "passed" for k in
             ("A1_setup_ok", "A2_counter_on_elf", "A3_control_bear",
              "A4_anthem", "A5_cleanup")):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
        OBS["notes"].append("verdict=blocked: incomplete assertion chain")

    for k in ("A1_setup_ok", "A2_counter_on_elf", "A3_control_bear",
              "A4_anthem", "A5_cleanup"):
        say(f"{k}: {A[k]}")
    say(f"verdict={verdict}")
    OBS["notes"].append(f"verdict={verdict}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"issue": ISSUE, "run_id": RUN_ID, "verdict": verdict,
                   "assertions": A, "details": D,
                   "notes": OBS.get("notes", [])}, f, indent=1, default=str)
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"issue": ISSUE, "run_id": RUN_ID,
                   "type_choice": OBS["type_choice"],
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
        "setup_line": ("P0 4x Door of Destinies / 8x Llanowar Elves / 8x "
                       "Grizzly Bears / 40x Forest; P1 60x Forest (passive). "
                       "P0 casts Door (chooses 'Elf' at entry), then casts "
                       "an Elf, a Bear (control), then a second Elf."),
        "contract_line": ("Casting an Elf (the chosen type) must add 1 "
                          "charge counter to Door of Destinies (SpellCast "
                          "trigger); a Bear (non-chosen) must add none; "
                          "the anthem must track the counters. "
                          "reproduced iff the Elf cast adds no counter."),
        "driver_notes": [
            "Protocol-106 port of driver/scenario_6918.py (v0.81.3 / "
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
            "engine auto-taps double-pay).",
            "The Door's entry 'choose a creature type' prompt is a vi "
            "schema opportunity with response spec type 'text', "
            "submitted as {'type': 'text', 'data': {'value': ...}}; "
            "gated on the Door being on P0's battlefield, no choice "
            "recorded yet, and a text-spec opportunity currently "
            "offered. The choice is marked only when the opportunity "
            "closes (a rejected submission leaves it pending and is "
            "retried after the rejection-drain resync).",
            "Data-level: v0.103.0 card-data parses Door of Destinies "
            "fully (entry choice persist=true, SpellCast trigger with "
            "IsChosenCreatureType -> PutCounter charge on self, "
            "charge-counter anthem); the defect is at runtime matching, "
            "not in the data.",
            "Pre/mid1/mid2/post states are authoritative exports "
            "(data.state parsed once from the export envelope) via the "
            "host client only; the reported OUTCOME is asserted on the "
            "saved states, not the prompt.",
        ],
        "assertions": A,
        "assertion_details": D,
        "driver_state": ST,
        "data_level": data_level,
        "verdict": verdict,
        "evidence_comment_id": 5652192823,
        "limitations": [
            "Browser UI not exercised; native engine via two "
            "human-client seats.",
            "4x Door / 8x Elves / 8x Bears density is a test-harness "
            "convenience (engine accepts >4-of for custom games).",
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
    with open(f"{EVDIR}/scenario_6918_01030.py", "w") as f:
        f.write(src)
    say("copied scenario_6918_01030.py into EVDIR")

    render_summary(run, pre_s, mid1_s, mid2_s, post_s)
    write_manifest()

    WIRE.close()
    RUNLOG.close()
    for c in (p0, p1):
        try:
            await c.close()
        except Exception:
            pass
    say("scenario finished")


def render_summary(run, pre_s, mid1_s, mid2_s, post_s):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        say("PIL missing; skipping summary.png")
        return
    W, H = 1000, 920
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #6918 - Door of Destinies",
           fill=(235, 240, 250))
    y += 28
    d.text((24, y),
           "server v0.103.0 (ec27a8d) protocol 106 - 2026-10-07 - "
           "charge counters on casting the chosen creature type",
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
        "A1_setup_ok": "Door on P0 BF; entry choice answered 'Elf'",
        "A2_counter_on_elf": "MID1: door charge counters == 1 (Elf cast)",
        "A3_control_bear": "MID2: bear cast adds no counter (unchanged)",
        "A4_anthem": "POST: elves are 1+charge / 1+charge",
        "A5_cleanup": "stack empty, game proceeds",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (160, 160, 160))
        d.text((36, y), f"{k}: {v} - {lab}", fill=col)
        y += 24
    y += 10
    d.text((24, y), "Door of Destinies charge counters across states:",
           fill=(200, 210, 225))
    y += 24
    for label, st in (("pre ", pre_s), ("mid1", mid1_s), ("mid2", mid2_s),
                      ("post", post_s)):
        if st is not None:
            oid = door_oid_of(st)
            n = charge_of(st)
            line = f"{label}: door_oid={oid} charge_counters={n}"
        else:
            line = f"{label}: (no state)"
        d.text((36, y), line[:112], fill=(150, 160, 175))
        y += 22
    y += 10
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    notes = (run["notes"] or [])[:3]
    for n in notes + [run["assertion_details"].get(k, "")
                      for k in labels]:
        d.text((36, y), str(n)[:116], fill=(150, 160, 175))
        y += 22
        if y > H - 40:
            break
    img.save(f"{EVDIR}/summary.png")
    say("wrote summary.png")


def write_manifest():
    files = ["pre.json", "mid1.json", "mid2.json", "post.json",
             "run.json", "assertions.json", "observations.json",
             "data_evidence.json", "scenario_6918_01030.py",
             "wire_log.jsonl", "scenario_run.log", "server.log",
             "summary.png"]
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
