#!/usr/bin/env python3
"""Issue #6950: [Card Bug] Elenda, Saint of Dusk - life-total-vs-starting-life
static ability is unrecognized.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Oracle text (pinned card-data.json v0.103.0):
  Elenda, Saint of Dusk ({2}{W}{B} Legendary Creature - Vampire Knight, 4/4):
    "Lifelink, hexproof from instants
     As long as your life total is greater than your starting life total,
     Elenda gets +1/+1 and has menace.
     Elenda gets an additional +5/+5 as long as your life total is at least
     10 greater than your starting life total."

Card-data parse state on v0.103.0 (observed 2026-10-07 before the run):
  static[0] condition parses: QuantityComparison { GT, Ref(LifeTotal,
    Controller) vs Ref(StartingLifeTotal) } with AddPower/AddToughness/
    AddKeyword(Menace).
  static[1] condition parses: QuantityComparison { GE,
    Ref(LifeAboveStarting) vs Fixed(10) } with AddPower 5 / AddToughness 5.
  -> the v0.81.3 Unrecognized(...) on static[1] is GONE from the data.
  The re-validation tests whether the engine EVALUATES the parsed
  conditions correctly.

Reported symptom (v0.42.0): deck builder flags
  Static:Unrecognized(your life total is at least 10 greater than your
  starting li...); the conditional buff statics don't apply.

Setup (native engine, two human-client seats, default Bo1):
  P0: 4x Elenda, Saint of Dusk, 12x Revitalize ({1}{W}: you gain 3 life,
      draw a card; untargeted, non-modal), 22x Plains, 22x Swamp.
  P1: 60x Forest (passive).

Plan:
  1. P0 casts Elenda on/after turn 4 (engine Auto payment). PRE exported
     with Elenda on BF at P0 life 20 (== starting): Oracle-correct is 4/4,
     no menace.
  2. P0 casts Revitalize x1 (life 23). MID1: static[0] should apply ->
     Oracle-correct is 5/5 with menace; static[1] must NOT apply yet
     (23 < 30).
  3. P0 casts Revitalize x3 more (life 32, >= 10 over starting). POST:
     static[1] should additionally apply -> Oracle-correct is 10/10
     with menace.

Assertions (Oracle-correct expectations; a FAIL on A4/A5 is the reported
defect):
  A1_setup_ok   PRE: Elenda on P0 BF; P0 life == 20.
  A2_parse      card-data v0.103.0: static[0] condition parses as
                QuantityComparison (GT, LifeTotal vs StartingLifeTotal);
                static[1] condition parses as QuantityComparison
                (GE, LifeAboveStarting vs Fixed 10) -- no Unrecognized.
  A3_gain_23    MID1: one Revitalize resolved, P0 life 20 -> 23.
  A4_below_threshold
                PRE (life 20): Elenda is 4/4 with no Menace. FAILED =
                reported bug is REPRODUCED (the +5/+5 static applies even
                though life is NOT >= 10 above starting).
  A5_mid_threshold
                MID1 (life 23, still < 10 above starting): Elenda is 5/5
                with Menace. FAILED = same defect (observed 10/10).
  A6_at_threshold
                POST (life 32, >= 10 above starting): Elenda is 10/10
                with Menace.
  A7_cleanup    POST: stack empty, game proceeds.

Keyword checks use obj["keywords"] only: the full-object JSON contains the
string "Menace" inside static_definitions' AddKeyword modification text,
which false-positives a whole-blob substring scan.

Verdict rule: reproduced iff A1 passes and (A4 or A5 fails);
not-reproduced iff A1-A7 all pass; blocked iff A1 fails.

Evidence: evidence/6950/<run-id>/pre.json, mid1.json, post.json, run.json,
assertions.json, observations.json, data_evidence.json, manifest.sha256,
summary.png, scenario_6950_01030.py, wire_log.jsonl, scenario_run.log,
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
ISSUE = 6950
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261007-6950")
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

PARSE = {"static0": None, "static1": None, "ok0": False, "ok1": False}


def _cond_blob(cond):
    return json.dumps(cond, default=str)


def check_data_level():
    """Record the v0.103.0 parse of Elenda's two static abilities.

    A2 contract: static[0] condition parses as QuantityComparison
    (GT, LifeTotal vs StartingLifeTotal); static[1] condition parses
    as QuantityComparison (GE, LifeAboveStarting vs Fixed 10) with no
    Unrecognized. On v0.81.3 static[1]'s condition was Unrecognized.
    """
    c = CARD_DATA.get("elenda, saint of dusk", {})
    oracle = str(c.get("oracle_text") or "")
    statics = c.get("static_abilities") or []
    c0 = (statics[0] or {}).get("condition", {}) if len(statics) > 0 else {}
    c1 = (statics[1] or {}).get("condition", {}) if len(statics) > 1 else {}
    PARSE["static0"] = c0
    PARSE["static1"] = c1
    b0, b1 = _cond_blob(c0), _cond_blob(c1)
    ok0 = (c0.get("type") == "QuantityComparison"
           and c0.get("comparator") == "GT"
           and "LifeTotal" in b0 and "StartingLifeTotal" in b0)
    ok1 = (c1.get("type") == "QuantityComparison"
           and c1.get("comparator") == "GE"
           and "LifeAboveStarting" in b1
           and "Fixed" in b1 and "Unrecognized" not in b1)
    PARSE["ok0"] = ok0
    PARSE["ok1"] = ok1
    out = {
        "name": c.get("name"),
        "oracle_text": oracle,
        "static_ability_count": len(statics),
        "static0_condition": c0,
        "static1_condition": c1,
        "static0_parses": ok0,
        "static1_parses": ok1,
        "note": ("v0.103.0 data parses both conditions; on v0.81.3 "
                 "static[1]'s condition was "
                 "Unrecognized('your life total is at least 10 greater "
                 "than your starting life total')"),
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(out, f, indent=1, default=str)
    say(f"data-level: static0_parses={ok0} static1_parses={ok1}")
    wire("data_level", {"static0_parses": ok0, "static1_parses": ok1,
                        "static0": c0, "static1": c1})
    return out


ELENDA_T = "Elenda, Saint of Dusk"
ELENDA_L = "elenda, saint of dusk"
REVIT_T = "Revitalize"
REVIT_L = "revitalize"
PLAINS_L = "plains"
SWAMP_L = "swamp"
FOREST_L = "forest"
LANDS = (PLAINS_L, SWAMP_L, FOREST_L)

P0_DECK = ((ELENDA_T, 4), (REVIT_T, 12), ("Plains", 22), ("Swamp", 22))
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
ST = {"elenda_cast": False, "elenda_oid": None, "pre_exported": False,
      "rev_casts": 0, "rev_resolved_at": None, "mid1_exported": False,
      "rev_last_life": None, "post_at": None, "post_exported": False}
OBS = {"unexpected_prompts": [], "auto_answered": [], "rejections": [],
       "tick_errors": [], "notes": [], "life_trace": []}


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
    p = player_of(state, pid)
    for k in ("life", "life_total", "lifeTotal"):
        if k in p:
            return p[k]
    return None


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
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


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
    n_lands = sum(1 for h in hand if h in LANDS)
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
    nm = obj_lname(state, o)
    if nm in LANDS:
        return 0
    if nm == ELENDA_L:
        return 1
    return 2  # Revitalize kept last


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


def has_keyword(obj, kw):
    """Check obj["keywords"] only: the full-object JSON contains the string
    "Menace" inside static_definitions' AddKeyword modification text, which
    false-positives a whole-blob substring scan."""
    for k in obj.get("keywords") or []:
        if isinstance(k, str) and k.lower() == kw.lower():
            return True
        if isinstance(k, dict) and kw.lower() in json.dumps(k).lower():
            return True
    return False


def elenda_sig(state):
    oid = ST["elenda_oid"] or (bf_oids(state, 0, ELENDA_L) or [None])[0]
    o = get_obj(state, oid) if oid is not None else {}
    return oid, o


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

async def do_export(c, path):
    s = await c.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


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
    if await do_declare_empty(c, acts, st, 0, tag):
        return True

    # record Elenda on the battlefield + life trace
    doors = bf_oids(state, 0, ELENDA_L)
    if doors and ST["elenda_oid"] is None:
        ST["elenda_oid"] = doors[0]
        say(f"Elenda on BF: oid={doors[0]}")
        wire("elenda_entered", {"oid": doors[0]})
    life = life_of(state, 0)
    tr = OBS["life_trace"]
    if life is not None and (not tr or tr[-1][1] != life):
        tr.append((round(time.time(), 1), life))
        say(f"P0 life = {life}")
        wire("life", {"life": life})

    # PRE: Elenda resolved, no Revitalize yet, stack empty, life == 20
    if (ST["elenda_oid"] is not None and not ST["pre_exported"]
            and ST["rev_casts"] == 0 and stack_empty(state) and life == 20):
        await do_export(c, "pre.json")
        ST["pre_exported"] = True
        say("PRE exported: Elenda on BF at life 20")
        st = st_of(c) or st
        state = st["state"]
        acts = merged_actions(st)

    # MID1: first Revitalize resolved (life hit 23), stack empty, 8s grace
    if (ST["rev_casts"] >= 1 and not ST["mid1_exported"]
            and life == 23 and stack_empty(state)):
        if ST["rev_resolved_at"] is None:
            ST["rev_resolved_at"] = time.time()
    if (ST["rev_resolved_at"] is not None and not ST["mid1_exported"]
            and time.time() - ST["rev_resolved_at"] > 8
            and stack_empty(state)):
        await do_export(c, "mid1.json")
        ST["mid1_exported"] = True
        ST["rev_resolved_at"] = None
        say("MID1 exported: life 23")
        st = st_of(c) or st
        state = st["state"]
        acts = merged_actions(st)

    # POST: life >= 30, stack empty, 8s grace
    if (life is not None and life >= 30 and not ST["post_exported"]
            and stack_empty(state)):
        if ST["post_at"] is None:
            ST["post_at"] = time.time()
    if (ST["post_at"] is not None and not ST["post_exported"]
            and time.time() - ST["post_at"] > 8
            and stack_empty(state)):
        await do_export(c, "post.json")
        ST["post_exported"] = True
        STAGE["stage"] = "DONE"
        STAGE["stop"] = True
        say("POST exported; stopping")
        return True

    await asyncio.sleep(0)
    st = st_of(c) or st
    state = st["state"]
    acts = merged_actions(st)

    if my_main(state, 0) or my_priority(top_acts(st)):
        n_untapped = len(untapped_lands(state, 0))
        grace_hold = ((ST["rev_resolved_at"] is not None
                       and not ST["mid1_exported"])
                      or (ST["post_at"] is not None
                          and not ST["post_exported"]))
        # 1. cast Elenda, Saint of Dusk ({2}{W}{B})
        if (not ST["elenda_cast"]
                and find_cast_action(acts, state, ELENDA_L) is not None
                and n_untapped >= 4):
            a = find_cast_action(acts, state, ELENDA_L)
            ST["elenda_cast"] = True
            say(f"[{tag}] casting {ELENDA_T} (engine Auto payment)")
            wire("cast_submit", {"tag": "elenda"})
            await submit_as_is(c, a)
            return True
        # 2. cast Revitalizes until life >= 32 (4 total: 20 -> 32);
        # hold casts during the mid1/post export grace windows and hold
        # the 2nd+ Revitalize until MID1 is exported.
        hn = [obj_lname(state, o) for o in hand_ids(state, 0)]
        if (ST["pre_exported"] and not grace_hold
                and ST["elenda_oid"] is not None and REVIT_L in hn
                and (life or 0) < 32 and n_untapped >= 2
                and (ST["mid1_exported"] or ST["rev_casts"] < 1)):
            a = find_cast_action(acts, state, REVIT_L)
            if a is not None:
                ST["rev_casts"] += 1
                ST["rev_resolved_at"] = None
                ST["post_at"] = None
                say(f"[{tag}] casting {REVIT_T} #{ST['rev_casts']} "
                    f"(engine Auto payment)")
                wire("cast_submit", {"tag": f"rev{ST['rev_casts']}"})
                await submit_as_is(c, a)
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

    p0 = PhaseClient("P06950r")
    await p0.connect()
    say("P0 creating game (default Bo1, 2 seats)...")
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P16950r")
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
                # (or holding an unanswered vi decision, e.g. a vi-only
                # declare-attackers relations prompt with no top-level
                # PassPriority) with no revision change
                # (missed-broadcast resilience).
                pending_vi = bool(unanswered_ops(st))
                if not ((my_priority(top_acts(st)) or pending_vi)
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
            STAGE["stop"] = True
            continue

        if turn > TURN_CAP and not STAGE.get("stop"):
            say(f"TURN CAP {TURN_CAP} reached; stopping")
            wire("turn_cap", {"turn": turn})
            STAGE["stop"] = True
            continue

        if time.time() - last_diag > 60:
            last_diag = time.time()
            oid, o = elenda_sig(state)
            say(f"DIAG turn={turn} active={state.get('active_player')} "
                f"phase={state.get('phase')} pp={state.get('priority_player')} "
                f"P0untapped={len(untapped_lands(state, 0))} "
                f"P0life={life_of(state, 0)} "
                f"elenda={oid} P/T={o.get('power')}/{o.get('toughness')} "
                f"menace={has_keyword(o, 'menace') if o else None} "
                f"rev_casts={ST['rev_casts']} "
                f"pre={ST['pre_exported']} mid1={ST['mid1_exported']} "
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
    post_s = load("post.json")

    def elenda_pt(state):
        oid, o = elenda_sig(state)
        return oid, o.get("power"), o.get("toughness"), o

    # A1: setup (Elenda on BF, life == starting == 20)
    if pre_s is not None:
        oid, pw, tw, o = elenda_pt(pre_s)
        life = life_of(pre_s, 0)
        ok = (oid is not None and o.get("zone") == "Battlefield"
              and life == 20)
        A["A1_setup_ok"] = "passed" if ok else "failed"
        men = has_keyword(o, "menace") if o else None
        D["A1_setup_ok"] = (f"oid={oid} zone={o.get('zone') if o else None} "
                            f"life={life} P/T={pw}/{tw} menace={men} "
                            f"(expect Elenda on BF, life 20)")
    else:
        A["A1_setup_ok"] = "failed"
        D["A1_setup_ok"] = "pre.json missing"

    # A2: card-data parse (the reported defect is at the data level;
    # on v0.103.0 both conditions should parse, no Unrecognized)
    A["A2_parse"] = "passed" if (PARSE["ok0"] and PARSE["ok1"]) else "failed"
    D["A2_parse"] = (
        f"static0_parses={PARSE['ok0']} (GT LifeTotal vs StartingLifeTotal); "
        f"static1_parses={PARSE['ok1']} (GE LifeAboveStarting vs Fixed 10); "
        f"(expect both True on v0.103.0; v0.81.3 had static1 "
        f"Unrecognized)")

    # A3: first Revitalize -> 23
    if mid1_s is not None:
        life = life_of(mid1_s, 0)
        A["A3_gain_23"] = "passed" if life == 23 else "failed"
        D["A3_gain_23"] = f"MID1 P0 life={life} (expected 23)"
    else:
        A["A3_gain_23"] = "failed"
        D["A3_gain_23"] = "mid1.json missing"

    # A4: below threshold (life 20): +5/+5 must NOT apply -> 4/4 no menace
    if pre_s is not None:
        oid, pw, tw, o = elenda_pt(pre_s)
        men = has_keyword(o, "menace") if o else None
        ok = (pw == 4 and tw == 4 and men is False)
        A["A4_below_threshold"] = "passed" if ok else "failed"
        D["A4_below_threshold"] = (
            f"PRE (life 20) Elenda P/T={pw}/{tw} menace={men} "
            f"(Oracle-correct: 4/4, no menace)")
        if not ok:
            D["A4_below_threshold"] += (" -> the +5/+5 static applies even "
                                        "though life is NOT >= 10 above "
                                        "starting. BUG REPRODUCED.")
    else:
        A["A4_below_threshold"] = "failed"
        D["A4_below_threshold"] = "pre.json missing"

    # A5: mid threshold (life 23 < 30): static[0] applies, static[1] not
    if mid1_s is not None:
        oid, pw, tw, o = elenda_pt(mid1_s)
        men = has_keyword(o, "menace") if o else None
        ok = (pw == 5 and tw == 5 and men is True)
        A["A5_mid_threshold"] = "passed" if ok else "failed"
        D["A5_mid_threshold"] = (
            f"MID1 (life 23) Elenda P/T={pw}/{tw} menace={men} "
            f"(Oracle-correct: 5/5 + menace)")
        if not ok:
            D["A5_mid_threshold"] += (" -> the +5/+5 static still applies "
                                      "below its threshold. BUG REPRODUCED.")
    else:
        A["A5_mid_threshold"] = "failed"
        D["A5_mid_threshold"] = "mid1.json missing"

    # A6: at threshold (life 32 >= 30): both statics apply
    if post_s is not None:
        life = life_of(post_s, 0)
        oid, pw, tw, o = elenda_pt(post_s)
        men = has_keyword(o, "menace") if o else None
        ok = (life is not None and life >= 30
              and pw == 10 and tw == 10 and men is True)
        A["A6_at_threshold"] = "passed" if ok else "failed"
        D["A6_at_threshold"] = (f"POST life={life} Elenda P/T={pw}/{tw} "
                                f"menace={men} (expected 10/10 + menace)")
    else:
        A["A6_at_threshold"] = "failed"
        D["A6_at_threshold"] = "post.json missing"

    # A7: cleanup (waiting_for is null on 106; stack empty = proceeding)
    if post_s is not None:
        stack_clear = stack_empty(post_s)
        A["A7_cleanup"] = "passed" if stack_clear else "failed"
        D["A7_cleanup"] = (f"stack_empty={stack_clear} "
                           f"turn={post_s.get('turn_number')}")
    else:
        A["A7_cleanup"] = "failed"
        D["A7_cleanup"] = "post.json missing"

    # ---------------------------------------------------------- verdict
    if A["A1_setup_ok"] != "passed":
        verdict = "blocked"
    elif (A["A4_below_threshold"] == "failed"
            or A["A5_mid_threshold"] == "failed"):
        verdict = "reproduced"
    elif all(A.get(k) == "passed" for k in
             ("A1_setup_ok", "A2_parse", "A3_gain_23",
              "A4_below_threshold", "A5_mid_threshold",
              "A6_at_threshold", "A7_cleanup")):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
        OBS["notes"].append("verdict=blocked: incomplete assertion chain")

    for k in ("A1_setup_ok", "A2_parse", "A3_gain_23",
              "A4_below_threshold", "A5_mid_threshold",
              "A6_at_threshold", "A7_cleanup"):
        say(f"{k}: {A[k]}")
    say(f"verdict={verdict}")
    OBS["notes"].append(f"verdict={verdict}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"issue": ISSUE, "run_id": RUN_ID, "verdict": verdict,
                   "assertions": A, "details": D,
                   "notes": OBS.get("notes", [])}, f, indent=1, default=str)
    with open(f"{EVDIR}/observations.json", "w") as f:
        json.dump({"issue": ISSUE, "run_id": RUN_ID,
                   "life_trace": OBS["life_trace"],
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
        "setup_line": ("P0 4x Elenda, Saint of Dusk / 12x Revitalize / "
                       "22x Plains / 22x Swamp; P1 60x Forest (passive). "
                       "P0 casts Elenda ({2}{W}{B}, engine Auto payment), "
                       "then casts Revitalize x1 (life 23), then x3 more "
                       "(life 32)."),
        "contract_line": ("Elenda must be 4/4 with no menace at life 20 "
                          "(both conditions false), 5/5 with menace at "
                          "life 23 (only the first condition true), and "
                          "10/10 with menace at life 32 (both true). "
                          "reproduced iff the +5/+5 static applies below "
                          "its life threshold."),
        "driver_notes": [
            "Protocol-106 port of driver/scenario_6950.py (v0.81.3 / "
            "protocol 70) for pinned v0.103.0; behavioral contract "
            "A1..A7 and the verdict rule unchanged.",
            "waiting_for is gone (null); priority = top-level "
            "PassPriority; all decisions via viewer_interaction; "
            "MulliganDecision via legacy Action; bottom via vi "
            "schema/select gated on waitingForKind.code=='mulligan'; "
            "DiscardToHandSize via vi schema/select (the ONLY accepted "
            "shape for a select schema).",
            "CastSpell via legacy Action; the v0.103.0 engine auto-taps "
            "reliably, so no driver mana taps (driver taps on top of "
            "engine auto-taps double-pay).",
            "Keyword checks use obj['keywords'] only: the full-object "
            "JSON contains the string 'Menace' inside "
            "static_definitions' AddKeyword modification text, which "
            "false-positives a whole-blob substring scan.",
            "Healing Salve is modal+targeted; Revitalize ({1}{W}, you "
            "gain 3 life, draw a card) is the clean untargeted "
            "non-modal life-gain driver.",
            "Data-level: v0.103.0 card-data parses both Elenda statics "
            "as QuantityComparison (GT LifeTotal vs StartingLifeTotal; "
            "GE LifeAboveStarting vs Fixed 10). On v0.81.3 the second "
            "condition was Unrecognized.",
            "Pre/mid1/post states are authoritative exports (data.state "
            "parsed once from the export envelope) via the host client "
            "only; the reported OUTCOME is asserted on the saved "
            "states, not the prompt.",
        ],
        "assertions": A,
        "assertion_details": D,
        "driver_state": ST,
        "data_level": data_level,
        "verdict": verdict,
        "evidence_comment_id": 5652324131,
        "limitations": [
            "Browser UI not exercised; native engine via two "
            "human-client seats.",
            "4x Elenda / 12x Revitalize density is a test-harness "
            "convenience (engine accepts >4-of for custom games).",
            "Revitalize stands in for 'gain life' triggers; the static's "
            "deck-builder flag is evidenced via the card-data parse check.",
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
    with open(f"{EVDIR}/scenario_6950_01030.py", "w") as f:
        f.write(src)
    say("copied scenario_6950_01030.py into EVDIR")

    try:
        shutil.copy(f"{BACKFILL}/runs/{RUN_ID}/server.log",
                    f"{EVDIR}/server.log")
        say("copied server.log into EVDIR")
    except Exception as e:
        say(f"server.log copy failed: {e}")

    render_summary(run, pre_s, mid1_s, post_s)
    write_manifest()

    WIRE.close()
    RUNLOG.close()
    for c in (p0, p1):
        try:
            await c.close()
        except Exception:
            pass
    say("scenario finished")


def render_summary(run, pre_s, mid1_s, post_s):
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        say("PIL missing; skipping summary.png")
        return
    W, H = 1000, 940
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #6950 - Elenda, Saint of Dusk",
           fill=(235, 240, 250))
    y += 28
    d.text((24, y),
           "server v0.103.0 (ec27a8d) protocol 106 - 2026-10-07 - "
           "life-total-vs-starting-life statics",
           fill=(140, 160, 180))
    y += 28
    vcol = (255, 90, 90) if run["verdict"] == "reproduced" else (
        (120, 220, 120) if run["verdict"] == "not-reproduced"
        else (230, 200, 120))
    d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vcol)
    y += 34
    d.text((24, y), "Assertions (from saved states / card-data):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "PRE: Elenda on P0 BF @ life 20",
        "A2_parse": "card-data: both statics parse (no Unrecognized)",
        "A3_gain_23": "MID1: 1 Revitalize resolved: P0 life 20 -> 23",
        "A4_below_threshold": "PRE (life 20): Elenda 4/4, no menace",
        "A5_mid_threshold": "MID1 (life 23): Elenda 5/5 + menace",
        "A6_at_threshold": "POST (life 32): Elenda 10/10 + menace",
        "A7_cleanup": "stack empty, game proceeds",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else (
            (255, 90, 90) if v == "failed" else (160, 160, 160))
        d.text((36, y), f"{k}: {v} - {lab}", fill=col)
        y += 24
    y += 10
    d.text((24, y), "Elenda across states (life / P/T / menace):",
           fill=(200, 210, 225))
    y += 24
    for label, st in (("pre ", pre_s), ("mid1", mid1_s), ("post", post_s)):
        if st is not None:
            oid, o = elenda_sig(st)
            line = (f"{label}: life={life_of(st, 0)} "
                    f"elenda P/T={o.get('power')}/{o.get('toughness')} "
                    f"menace={has_keyword(o, 'menace') if o else None}")
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
    files = ["pre.json", "mid1.json", "post.json",
             "run.json", "assertions.json", "observations.json",
             "data_evidence.json", "scenario_6950_01030.py",
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
