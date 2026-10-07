#!/usr/bin/env python3
"""Issue #7177: Mr. House, President and CEO -- no Treasure on a 6+ roll.

Protocol-106 port (v0.103.0, 2026-10-07) of scenario_7177.py
(verified 2026-09-15 on v0.83.0/protocol 70, run 20260915-7177).

Oracle: "Whenever you roll a 4 or higher, create a 3/3 colorless Robot
artifact creature token. If you rolled 6 or higher, instead create that
token and a Treasure token."
"{4}, {T}: Roll a six-sided die plus an additional six-sided die for each
mana from Treasures spent to activate this ability."

Reported: on a 6+ it only makes the Robot; it should make both.

Parse (pinned v0.103.0 card-data.json -- UNCHANGED from v0.83.0):
  trigger mode RolledDieOnce, die_result AtLeast 4;
  execute Token{Robot 3/3 artifact creature}; sub_ability (SequentialSibling)
  effect Unimplemented{name=instead_condition,
    description="If you rolled 6 or higher, instead create that token and
    a Treasure token"}.
  The activated roll ability still parses as
  Unimplemented{name=unparsed_quantity}, so the die is rolled with Adorable
  Kitten ({W} 1/1, ETB: roll a six-sided die, you gain life equal to the
  result) -- the life-gain delta reveals each exact roll result.

Setup (native engine, two human-client seats, v0.103.0/protocol 106):
  P0: 4x mr. house, president and ceo / 24x adorable kitten /
      11x mountain / 11x plains / 11x swamp (House costs {R}{W}{B}).
  P1: 60x forest, fully passive (lands, passes, never attacks).
  P0 casts Mr. House, then casts Kittens one at a time; each Kitten ETB
  rolls 1d6 and P0 gains life = result. Per roll r, with House on the BF:
    r in 1..3 -> nothing; r in 4..5 -> 1 Robot; r=6 -> 1 Robot + 1 Treasure.
  Measurement per kitten: baseline (life, robot count, treasure count) at
  cast; after the kitten is on the BF and the stack has been empty for
  QUIET_TICKS consecutive main-loop ticks, re-measure. Deltas are
  attributed to that single roll (P1 is passive; P0 declares no attackers).

Behavioral contract:
  A1 setup_ok      House on P0 BF; pre.json exported before first kitten.
  A2 rolls_valid   >=12 rolls recorded, every result in 1..6, >=1 six and
                   >=2 mid (4/5) rolls observed.
  A3 low_clean     every r in 1..3 -> 0 robots, 0 treasures.
  A4 mid_robot     every r in 4..5 -> exactly 1 robot, 0 treasures
                   (also proves the trigger fires at all).
  A5 six_treasure  every r = 6 -> exactly 1 robot AND 1 treasure
                   (expected to FAIL == the reported bug).
  A6 cleanup       stack empty, post.json exported, game proceeding.

Verdict: reproduced iff A1-A4 pass and A5 fails (>=1 six with 0 treasure).
         not-reproduced iff A1-A5 all pass.
         blocked iff A1 fails, or zero usable rolls, or no six after the
         roll cap (the 6+ branch untestable), or life deltas not in 1..6.

Evidence: evidence/7177/20261007-7177/pre.json, post.json, assertions.json,
run.json, data_evidence.json, server_excerpts.log, summary.png, wire_log.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

import websockets

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 7177
RUN_ID = "20261007-7177"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.103.0/data/card-data.json"))

HOUSE = "mr. house, president and ceo"
KITTEN = "adorable kitten"
MOUNTAIN = "mountain"
PLAINS = "plains"
SWAMP = "swamp"
FOREST = "forest"

P0_DECK = [(HOUSE, 4), (KITTEN, 24),
           (MOUNTAIN, 11), (PLAINS, 11), (SWAMP, 10)]
P1_DECK = [(FOREST, 60)]

TIMEOUT = 1500
ROLL_TARGET = 20          # stop after this many recorded rolls
MIN_ROLLS = 12
QUIET_TICKS = 8           # stack-empty ticks before measuring a roll
PENDING_TIMEOUT = 90      # seconds before a pending roll is abandoned
SETUP_ABORT_S = 420       # fail fast if House never lands
PROGRESS_WATCHDOG_S = 180 # no revision advance at all -> stall export + stop

ST = {"stage": "SETUP", "stop": False, "states_seen": 0,
      "house_oid": None, "casting_house": False,
      "mana_needs": {}, "stall_since": None,
      "rolls": [], "pending": None,
      "pre_exported": False, "post_exported": False,
      "game_code": None, "setup_t0": None}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}
WF_SEEN = []
NAMES_LOGGED = False


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    RUNLOG.write(m + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


# ------------------------------------------------------------------ helpers

def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    return str(get_obj(state, oid).get("base_name")
               or get_obj(state, oid).get("name") or "?").lower()


def is_land(o):
    return "Land" in ((o.get("card_types") or {}).get("core_types") or [])


def hand_ids(state, pid):
    for p in state.get("players", []) or []:
        if p.get("id") == pid:
            return [int(o) for o in p.get("hand", [])]
    return []


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def bf_oids(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bf_by_name(state, pid, lname):
    return [o for o in bf_oids(state, pid) if obj_lname(state, o) == lname]


def life_of(state, pid):
    for p in state.get("players", []) or []:
        if p.get("id") == pid:
            return p.get("life")
    return None


def untapped_named(state, pid, name):
    return sum(1 for o in bf_oids(state, pid)
               if obj_lname(state, o) == name
               and not get_obj(state, o).get("tapped"))


def token_subtypes(o):
    subs = []
    ct = o.get("card_type") or {}
    if isinstance(ct, dict):
        subs += [str(s).lower() for s in (ct.get("subtypes") or [])]
    subs += [str(s).lower() for s in (o.get("subtypes") or [])]
    return subs, str(o.get("type_line") or "").lower()


def count_tokens(state, pid, kind):
    """kind in ("robot","treasure"). Count by name, fallback to subtypes."""
    global NAMES_LOGGED
    n = 0
    names = set()
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") != "Battlefield" or o.get("controller") != pid:
            continue
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        names.add(nm)
        subs, tl = token_subtypes(o)
        if kind == "robot":
            if nm == "robot" or "robot" in subs or "robot" in tl:
                n += 1
        else:
            if nm == "treasure" or "treasure" in subs or "treasure" in tl:
                n += 1
    if not NAMES_LOGGED and names:
        NAMES_LOGGED = True
        say(f"P0 BF object names at first token count: {sorted(names)}")
        wire("bf_names", {"names": sorted(names)})
    return n


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
            d = s.get("data") or {}
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def surf_codes(ch):
    return [s.get("data", {}).get("code")
            for s in ch.get("surfaces", []) or []
            if isinstance(s.get("data"), dict)]


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
        if "decideOptionalEffect" in codes:
            return True
        if codes and not (codes <= NON_DECISION_CODES):
            return True
    return False


# ------------------------------------------------------- interaction prims

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


async def verify_server_hello():
    ws = await websockets.connect("ws://localhost:9374/ws",
                                  max_size=1_000_000)
    raw = await asyncio.wait_for(ws.recv(), 10)
    await ws.close()
    d = json.loads(raw).get("data", {}) or {}
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto} "
        f"mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.103.0"), f"unexpected version {ver}"
    assert int(proto) == 106, f"unexpected protocol {proto}"
    assert str(build) == "ec27a8d", f"unexpected build {build}"
    return ver, build, proto


def check_data_level():
    """Confirm the v0.103.0 parse still shows the 6+ branch as
    Unimplemented instead_condition (bug premise unchanged)."""
    ok, notes = True, []
    h = CARD_DATA.get("mr. house, president and ceo", {})
    oracle = str(h.get("oracle_text", ""))
    for ph in ("roll a 4 or higher", "6 or higher",
               "instead create that token and a treasure token"):
        if ph not in oracle.lower():
            ok = False
            notes.append(f"oracle missing phrase: {ph!r}")
    payload = {}
    trig = (h.get("triggers") or [{}])[0]
    mode_ok = trig.get("mode") == "RolledDieOnce"
    dr = trig.get("die_result") or {}
    at_least_4 = dr.get("AtLeast") == 4
    exe = trig.get("execute") or {}
    tok_ok = (exe.get("effect") or {}).get("type") == "Token"
    sub = exe.get("sub_ability") or {}
    sub_eff = sub.get("effect") or {}
    unimpl_ok = (sub_eff.get("type") == "Unimplemented"
                 and sub_eff.get("name") == "instead_condition")
    payload = {"mode": trig.get("mode"), "die_result": dr,
               "effect_type": (exe.get("effect") or {}).get("type"),
               "sub_effect": {"type": sub_eff.get("type"),
                              "name": sub_eff.get("name"),
                              "description": sub_eff.get("description")}}
    if not (mode_ok and at_least_4 and tok_ok and unimpl_ok):
        ok = False
        notes.append("trigger parse shape changed from the known bug premise")
    roll_ab = (h.get("abilities") or [{}])[0]
    roll_eff = roll_ab.get("effect") or {}
    roll_unimpl = (roll_eff.get("type") == "Unimplemented"
                   and roll_eff.get("name") == "unparsed_quantity")
    payload["roll_ability"] = {"type": roll_eff.get("type"),
                               "name": roll_eff.get("name")}
    if not roll_unimpl:
        notes.append("House's own roll ability parse changed "
                     "(kitten proxy still used)")
    k = CARD_DATA.get("adorable kitten", {})
    ktrig = (k.get("triggers") or [{}])[0]
    keff = (ktrig.get("execute") or {}).get("effect") or {}
    rolldie_ok = keff.get("type") == "RollDie" and keff.get("sides") == 6
    payload["kitten_etb"] = {"type": keff.get("type"),
                             "sides": keff.get("sides")}
    if not rolldie_ok:
        ok = False
        notes.append("kitten ETB roll shape unexpected")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump({"ok": ok, "notes": notes, "mr_house_oracle": oracle,
                   "trigger_payload": payload}, f, indent=1)
    say(f"data-level check: ok={ok} notes={notes}")
    return ok


def mulligan_pending_for(state, pid):
    # MulliganDecision's waiting_for carries NO top-level data.player; pending
    # seats live in data.pending[] as {player, phase:{type:"Declare"}}.
    d = ((state.get("waiting_for") or {}).get("data") or {})
    for p in d.get("pending", []) or []:
        ph = p.get("phase") or {}
        if p.get("player") == pid and str(ph.get("type")) == "Declare":
            return True
    return False


def bottom_pending_for(state, pid):
    d = ((state.get("waiting_for") or {}).get("data") or {})
    for p in d.get("pending", []) or []:
        ph = p.get("phase") or {}
        if p.get("player") == pid and str(ph.get("type")) == "BottomCards":
            return int(ph.get("count", 1) or 1)
    return 0


def _cand_reference(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "reference" in d:
            return d.get("reference")
    return None


async def do_mulligan(c, acts, st, pid, tag):
    state = st["state"]
    if mulligan_pending_for(state, pid):
        key = (tag, "mull", f"rev{c.revision}")
        if key in MULLS:
            return True
        adv = next((a for a in acts if a.get("type") == "MulliganDecision"),
                   None)
        if adv is None:
            return False
        MULLS.add(key)
        hn = hand_ids(state, pid)
        say(f"[{tag}] keeps (hand={len(hn)})")
        wire("mulligan", {"who": tag, "decision": "keep",
                          "hand": [obj_lname(state, o) for o in hn]})
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Keep"}}})
        return True
    return False


DISCARD_WIRED = set()      # iids whose raw opportunity was wired
DISCARD_SUBMITTED = set()  # iids submitted and awaiting engine response


def is_select_schema_opp(opp):
    """True only for the engine's Select-model opportunity
    (HumanResponseModel::Select): response.type == "schema" with
    spec.type == "select". DiscardToHandSize is a Select model; its
    submission MUST be {"type": "select", "data": {"choiceIds": [...]}}.
    Anything else (e.g. an ExactCandidates opportunity that happens to
    mention discarding) must not be answered as a discard."""
    resp = opp.get("response", {}) or {}
    if resp.get("type") != "schema":
        return False
    rdata = resp.get("data", {}) or {}
    spec = rdata.get("spec", {}) or {}
    return spec.get("type") == "select"


async def do_discard(c, st, tag, pid, rank_fn):
    """Hand-size discard via the engine's Select-model opportunity.
    Picks the lowest-ranked n cards. rank_fn(oid) -> sortable key."""
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    select_opps = [o for o in vi_ops(st) if is_select_schema_opp(o)]
    other_opps = [o for o in vi_ops(st) if not is_select_schema_opp(o)]
    if not select_opps:
        # diagnostic: what IS in the viewer interaction while over hand size?
        key = (tag, "no_select_opp", str(c.revision))
        if key not in SUBMITTED_OPPS:
            SUBMITTED_OPPS.add(key)
            wire("discard_no_select_opp",
                 {"who": tag, "hand": len(hand), "n": n,
                  "vi_opps": [
                      {"iid": o.get("interactionId"),
                       "rtype": (o.get("response") or {}).get("type"),
                       "spec": ((o.get("response") or {}).get("data", {})
                                or {}).get("spec")}
                      for o in other_opps]})
            say(f"[{tag}] hand {len(hand)} > 7 but no Select discard "
                f"opportunity in vi ({len(other_opps)} other opps)")
        return False
    for opp in select_opps:
        iid = opp.get("interactionId")
        if iid in DISCARD_SUBMITTED:
            continue
        rdata = (opp.get("response", {}) or {}).get("data", {}) or {}
        cands = rdata.get("candidates") or []
        if not cands:
            continue
        if iid not in DISCARD_WIRED:
            DISCARD_WIRED.add(iid)
            wire("discard_opportunity", {"who": tag, "opp": opp})
        picks = [ch["id"] for ch in
                 sorted(cands,
                        key=lambda ch: rank_fn(_cand_reference(ch)))[:max(1, n)]]
        DISCARD_SUBMITTED.add(iid)
        say(f"[{tag}] discarding {n} to hand size via select")
        wire("handsize_discard", {"who": tag, "iid": iid, "picks": picks})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": picks}}}, tag)
        return True
    return False


def p0_discard_rank(state):
    def rank(o):
        nm = obj_lname(state, o)
        if nm in (MOUNTAIN, PLAINS, SWAMP, FOREST):
            return (0, nm)
        if nm == HOUSE:
            return (1, nm)
        return (2, nm)
    return rank


def p1_discard_rank(state):
    def rank(o):
        return (0, obj_lname(state, o))
    return rank


def drain_rejections(c, tag):
    """Drain the client's rejection record into the wire log. A rejected
    submission moves no revision, so a suppressed resubmit would deadlock:
    any rejection re-arms discard submissions."""
    if not c.rejections:
        return 0
    n = 0
    for r in c.rejections:
        n += 1
        say(f"[{tag}] {r['type']}: {json.dumps(r['data'])[:200]}")
        wire("rejection", {"who": tag, "type": r["type"], "data": r["data"]})
    c.rejections.clear()
    if DISCARD_SUBMITTED:
        wire("discard_rearmed", {"who": tag, "cleared": len(DISCARD_SUBMITTED)})
        DISCARD_SUBMITTED.clear()
    return n


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pay_mana_vi(c, st, tag, needs=None):
    ops = vi_ops(st)
    if not ops:
        return False
    for opp in ops:
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status", {}) or {}).get("type") not in (None,
                                                                "available"):
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
        if iid in SUBMITTED_OPPS:
            continue
        pick, used = None, None
        if needs:
            for ch, s in taps:
                for color in ("W", "U", "B", "R", "G"):
                    if needs.get(color, 0) > 0 and color in s:
                        pick, used = ch, color
                        break
                if pick is not None:
                    break
            if pick is None and needs.get("generic", 0) > 0:
                pick, used = taps[0][0], "generic"
        else:
            pick, used = taps[0][0], "any"
        if pick is None:
            continue
        if needs and used != "any":
            needs[used] -= 1
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] tap land for mana used_for={used} needs_left={needs}")
        wire("tap_land", {"who": tag, "used_for": used,
                          "needs_left": dict(needs or {})})
        iid2 = opp.get("interactionId")
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        cid = pick.get("id")
        if rtype == "schema":
            spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
            stype = spec.get("type") or "sequence"
            sub = {"interactionId": iid2,
                   "response": {"type": stype, "data": {"choiceIds": [cid]}}}
        else:
            sub = {"interactionId": iid2,
                   "response": {"type": "choose", "data": {"choiceId": cid}}}
        await interact_as(c, sub, tag)
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
                await interact_as(c, {"interactionId": opp.get("interactionId"),
                                      "response": {"type": "choose",
                                                   "data": {"choiceId": ch.get("id")}}},
                                  c.name)
                return True
    return False


async def play_a_land(c, state, pid, acts, tag):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    for o in hand_ids(state, pid):
        if is_land(get_obj(state, o)):
            for a in acts:
                if a["type"] == "PlayLand" and str(a.get("_src_oid")) == str(o):
                    LAND_PLAYED_TURN[tag] = turn
                    say(f"[{tag}] playing land {obj_lname(state, o)}")
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


def stack_entries(state):
    return state.get("stack") or []


# ------------------------------------------------------------------ ticks

async def p1_tick(c, st, tag):
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 1, tag):
        return
    if await do_discard(c, st, tag, 1, p1_discard_rank(state)):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da.get("data", {}))
            if "attacks" in d:
                d["attacks"] = []
            if "bands" in d:
                d["bands"] = []
            await submit_as_is(c, {"type": "DeclareAttackers", "data": d})
        return
    if "DeclareBlockers" in atypes:
        return
    if await pay_tick(c, acts):
        return
    if await pay_mana_vi(c, st, tag):
        return
    if my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag):
            return
    if real_decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


async def p0_tick(c, st, tag):
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 0, tag):
        return
    if await do_discard(c, st, tag, 0, p0_discard_rank(state)):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da.get("data", {}))
            if "attacks" in d:
                d["attacks"] = []
            if "bands" in d:
                d["bands"] = []
            await submit_as_is(c, {"type": "DeclareAttackers", "data": d})
        return
    if "DeclareBlockers" in atypes:
        return
    if await pay_tick(c, acts):
        return
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return

    if ST["stage"] == "SETUP" and my_main(state, 0) \
            and not ST["casting_house"]:
        if await play_a_land(c, state, 0, acts, tag):
            return
        if HOUSE in hand_lnames(state, 0) \
                and untapped_named(state, 0, MOUNTAIN) >= 1 \
                and untapped_named(state, 0, PLAINS) >= 1 \
                and untapped_named(state, 0, SWAMP) >= 1:
            a, oid = cast_action_for(acts, state, HOUSE)
            if a is not None:
                say(f"[{tag}] casting Mr. House (oid {oid})")
                wire("cast_house", {"oid": oid})
                ST["casting_house"] = True
                ST["mana_needs"][tag] = {"R": 1, "W": 1, "B": 1}
                await submit_as_is(c, a)
                return

    if ST["stage"] == "ROLLING" and my_main(state, 0) \
            and ST["pending"] is None and len(ST["rolls"]) < ROLL_TARGET:
        if await play_a_land(c, state, 0, acts, tag):
            return
        if KITTEN in hand_lnames(state, 0) \
                and untapped_named(state, 0, PLAINS) >= 1:
            a, oid = cast_action_for(acts, state, KITTEN)
            if a is not None:
                say(f"[{tag}] casting Adorable Kitten (oid {oid})")
                wire("cast_kitten", {"oid": oid})
                ST["mana_needs"][tag] = {"W": 1}
                ST["pending"] = {
                    "kitten_oid": oid,
                    "life0": life_of(state, 0),
                    "robots0": count_tokens(state, 0, "robot"),
                    "treasures0": count_tokens(state, 0, "treasure"),
                    "t0": time.time(),
                    "turn": state.get("turn_number"),
                    "quiet": 0,
                    "trigger_seen": False,
                }
                say(f"[P0] roll pending: kitten oid={oid} "
                    f"life0={ST['pending']['life0']} "
                    f"robots0={ST['pending']['robots0']} "
                    f"treasures0={ST['pending']['treasures0']}")
                wire("roll_pending",
                     {k: v for k, v in ST["pending"].items()
                      if k != "quiet"})
                await submit_as_is(c, a)
                return

    if real_decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


# ------------------------------------------------------------ measurement

def settle_pending(state):
    """Attribute a completed kitten roll once the board is quiet."""
    pend = ST.get("pending")
    if pend is None:
        return
    stack = stack_entries(state)
    for e in stack:
        blob = json.dumps(e, default=str).lower()
        if "house" in blob or "rolleddie" in blob.replace(" ", ""):
            pend["trigger_seen"] = True
            break
    stack_quiet = not stack
    if time.time() - pend["t0"] > PENDING_TIMEOUT:
        say(f"[P0] pending roll abandoned (timeout): "
            f"kitten oid={pend['kitten_oid']}")
        wire("roll_abandoned", {k: v for k, v in pend.items()
                                if k != "quiet"})
        ST["rolls"].append({
            "turn": pend["turn"], "kitten_oid": pend["kitten_oid"],
            "result": None, "robots_delta": None,
            "treasures_delta": None,
            "house_trigger_seen": pend["trigger_seen"],
            "note": "abandoned: stack never quieted",
        })
        ST["pending"] = None
        return
    if stack_quiet:
        pend["quiet"] += 1
    else:
        pend["quiet"] = 0
    if pend["quiet"] < QUIET_TICKS:
        return
    kitten = get_obj(state, pend["kitten_oid"])
    if kitten.get("zone") != "Battlefield":
        say(f"[P0] pending roll abandoned: kitten not on BF "
            f"(zone={kitten.get('zone')})")
        wire("roll_abandoned", {"reason": "kitten_not_bf"})
        ST["rolls"].append({
            "turn": pend["turn"], "kitten_oid": pend["kitten_oid"],
            "result": None, "robots_delta": None,
            "treasures_delta": None,
            "house_trigger_seen": pend["trigger_seen"],
            "note": "abandoned: kitten left battlefield",
        })
        ST["pending"] = None
        return
    result = life_of(state, 0) - pend["life0"]
    robots_d = count_tokens(state, 0, "robot") - pend["robots0"]
    treasures_d = (count_tokens(state, 0, "treasure")
                   - pend["treasures0"])
    rec = {
        "turn": pend["turn"], "kitten_oid": pend["kitten_oid"],
        "result": result, "robots_delta": robots_d,
        "treasures_delta": treasures_d,
        "house_trigger_seen": pend["trigger_seen"],
    }
    ST["rolls"].append(rec)
    say(f"[P0] roll #{len(ST['rolls'])}: r={result} "
        f"robotsΔ={robots_d} treasuresΔ={treasures_d} "
        f"trigger_seen={pend['trigger_seen']}")
    wire("roll_recorded", rec)
    ST["pending"] = None


async def do_export(c, path):
    s = await c.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    wire(f"{path}_exported", {})
    return json.loads(s)["state"]


def env_state(p):
    try:
        with open(f"{EVDIR}/{p}") as f:
            return json.load(f)["state"]
    except FileNotFoundError:
        return None


def evaluate(notes):
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_rolls_valid", "A3_low_clean",
            "A4_mid_robot", "A5_six_treasure", "A6_cleanup")}
    pre = env_state("pre.json")
    post = env_state("post.json")

    house_oid = ST.get("house_oid")
    house_bf_pre = (house_oid is not None and pre is not None
                    and get_obj(pre, house_oid).get("zone")
                    == "Battlefield")
    ass["A1_setup_ok"] = ("passed" if (house_bf_pre and ST["pre_exported"])
                          else "failed")
    notes.append(f"A1: house_oid={house_oid} house_bf_pre={house_bf_pre} "
                 f"pre_exported={ST['pre_exported']}")

    rolls = ST["rolls"]
    valid = [r for r in rolls if r.get("result") in (1, 2, 3, 4, 5, 6)]
    sixes = [r for r in valid if r["result"] == 6]
    mids = [r for r in valid if r["result"] in (4, 5)]
    lows = [r for r in valid if r["result"] in (1, 2, 3)]
    ass["A2_rolls_valid"] = ("passed" if (len(valid) >= MIN_ROLLS
                                          and len(sixes) >= 1
                                          and len(mids) >= 2) else "failed")
    notes.append(f"A2: rolls={len(rolls)} valid={len(valid)} sixes={len(sixes)} "
                 f"mids={len(mids)} lows={len(lows)} "
                 f"results={[r.get('result') for r in rolls]}")

    if not lows:
        ass["A3_low_clean"] = "not-run"
    else:
        ass["A3_low_clean"] = ("passed" if all(
            r["robots_delta"] == 0 and r["treasures_delta"] == 0
            for r in lows) else "failed")
    if not mids:
        ass["A4_mid_robot"] = "not-run"
    else:
        ass["A4_mid_robot"] = ("passed" if all(
            r["robots_delta"] == 1 and r["treasures_delta"] == 0
            for r in mids) else "failed")
    if not sixes:
        ass["A5_six_treasure"] = "not-run"
    else:
        ass["A5_six_treasure"] = ("passed" if all(
            r["robots_delta"] == 1 and r["treasures_delta"] == 1
            for r in sixes) else "failed")
    for r in lows + mids + sixes:
        notes.append(f"  roll r={r['result']}: robotsΔ={r['robots_delta']} "
                     f"treasuresΔ={r['treasures_delta']} "
                     f"house_trigger_seen={r['house_trigger_seen']}")

    if post is not None:
        ass["A6_cleanup"] = ("passed" if not (post.get("stack") or [])
                             else "failed")
        notes.append(f"A6: post phase={post.get('phase')} "
                     f"turn={post.get('turn_number')} "
                     f"stack_empty={not (post.get('stack') or [])}")
    else:
        notes.append("A6: post.json missing; not-run")

    if ass["A1_setup_ok"] == "failed" or ass["A2_rolls_valid"] == "failed":
        verdict = "blocked"
    elif ass["A3_low_clean"] == "failed" or ass["A4_mid_robot"] == "failed":
        verdict = "reproduced"  # trigger misbehaves adjacent to the report
    elif ass["A5_six_treasure"] == "failed":
        verdict = "reproduced"  # the reported symptom
    elif ass["A5_six_treasure"] == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    return ass, verdict


# ------------------------------------------------------------------- main

async def main():
    t0 = time.time()
    ST["setup_t0"] = t0
    notes = []

    ver, build, proto = await verify_server_hello()
    data_ok = check_data_level()
    if not data_ok:
        notes.append("data-level check FAILED -- see data_evidence.json")

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    try:
        await p0.create(deck(*P0_DECK))
    except Exception as e:
        say(f"game creation failed: {e}")
        notes.append(f"deck/game creation failed: {e}")
        with open(f"{EVDIR}/assertions.json", "w") as f:
            json.dump({"assertions":
                       {k: "not-run" for k in
                        ("A1_setup_ok", "A2_rolls_valid", "A3_low_clean",
                         "A4_mid_robot", "A5_six_treasure", "A6_cleanup")},
                       "notes": notes}, f, indent=2)
        await p0.close()
        return
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game", {"code": p0.game_code, "p0": p0.player_id,
                  "p1": p1.player_id})

    last = {}
    last_tick_at = {}
    progress_rev = -1
    progress_at = t0
    last_diag = 0.0
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.15)
        for c, tick, tag, pid in ((p0, p0_tick, "P0", 0),
                                  (p1, p1_tick, "P1", 1)):
            st = c.latest
            if not st:
                continue
            rev = c.revision
            stale = time.time() - last_tick_at.get(c.name, 0) > 5
            if rev == last.get(c.name) and not stale:
                continue
            last[c.name] = rev
            last_tick_at[c.name] = time.time()
            try:
                await tick(c, st, tag)
            except Exception as e:
                say(f"tick error {c.name}: {e}")
                wire("tick_error", {"who": c.name, "err": str(e)})
            drain_rejections(c, tag)

        st = p0.latest
        if not st:
            continue
        state = st["state"]
        ST["states_seen"] += 1

        wf = (state.get("waiting_for") or {}).get("type")
        if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
            WF_SEEN.append(wf)
            wire("waiting_for",
                 {"type": wf,
                  "data": (state.get("waiting_for") or {}).get("data")})

        # generic progress watchdog: no revision advance at all
        if p0.revision != progress_rev or p1.revision != progress_rev:
            progress_rev = max(p0.revision, p1.revision)
            progress_at = time.time()
        if time.time() - progress_at > PROGRESS_WATCHDOG_S:
            await do_export(p0, "mid_stall.json")
            notes.append("generic stall watchdog: no revision advance "
                         f">{PROGRESS_WATCHDOG_S}s")
            say("[stall] generic watchdog fired")
            ST["stop"] = True
            continue

        # House on the battlefield?
        if ST["house_oid"] is None:
            h = bf_by_name(state, 0, HOUSE)
            if h:
                ST["house_oid"] = h[0]
                ST["stage"] = "ROLLING"
                say(f"House on BF oid={h[0]}; stage -> ROLLING")
                wire("house_bf", {"oid": h[0]})

        # settle any pending roll measurement
        settle_pending(state)

        # pre export: House on BF, P0 pre/postcombat main, priority, quiet
        if (ST["house_oid"] is not None and not ST["pre_exported"]
                and not ST["rolls"] and ST["pending"] is None
                and my_main(state, 0) and my_priority(merged_actions(st))):
            await do_export(p0, "pre.json")
            ST["pre_exported"] = True
            notes.append(f"pre: turn {state.get('turn_number')} "
                         f"house_oid={ST['house_oid']} "
                         f"life={life_of(state, 0)}/{life_of(state, 1)}")

        # post export: roll target reached, nothing pending, stack empty
        if (len(ST["rolls"]) >= ROLL_TARGET
                and ST["pending"] is None
                and not (state.get("stack") or [])):
            await do_export(p0, "post.json")
            ST["post_exported"] = True
            notes.append(f"post: turn {state.get('turn_number')} "
                         f"rolls={len(ST['rolls'])} "
                         f"life={life_of(state, 0)}/{life_of(state, 1)} "
                         f"robots={count_tokens(state, 0, 'robot')} "
                         f"treasures={count_tokens(state, 0, 'treasure')}")
            say("roll target reached; exporting post and stopping")
            ST["stop"] = True
            break

        # SETUP abort: House never castable/landed
        if ST["stage"] == "SETUP" and time.time() - t0 > SETUP_ABORT_S:
            await do_export(p0, "mid_setup.json")
            notes.append(f"SETUP abort: House not on BF after "
                         f"{SETUP_ABORT_S}s (turn "
                         f"{state.get('turn_number')}, hand="
                         f"{hand_lnames(state, 0)[:8]})")
            say("[setup-abort] House never landed; ending run")
            ST["stop"] = True
            continue

        if time.time() - last_diag > 60:
            last_diag = time.time()
            say(f"DIAG turn={state.get('turn_number')} "
                f"active={state.get('active_player')} phase={state.get('phase')} "
                f"wf={wf} stage={ST['stage']} house={ST['house_oid']} "
                f"rolls={len(ST['rolls'])} pending={ST['pending'] is not None} "
                f"casting_house={ST['casting_house']} "
                f"P0hand={hand_lnames(state, 0)[:6]}")

    if not ST["post_exported"]:
        try:
            await do_export(p0, "post.json")
            ST["post_exported"] = True
            notes.append("post.json exported at finish() fallback")
        except Exception as e:
            notes.append(f"post export failed: {e}")

    ass, verdict = evaluate(notes)
    for k in sorted(ass):
        say(f"{k}: {ass[k]}")
    say("WF sequence:", WF_SEEN)

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "notes": notes,
                   "wf_sequence": WF_SEEN,
                   "rolls": ST["rolls"],
                   "n_rolls": len(ST["rolls"]),
                   "data_level_ok": data_ok}, f, indent=2)

    def sha(p):
        return hashlib.sha256(open(p, "rb").read()).hexdigest()

    scenario_src = open(__file__, "rb").read()
    with open(f"{EVDIR}/scenario_7177.py", "w") as f:
        f.write(scenario_src.decode())

    run_meta = {
        "run_id": RUN_ID,
        "issue": ISSUE,
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S-05:00",
                                    time.localtime(t0)),
        "server": {
            "server_version": "v0.103.0",
            "build_commit": "ec27a8d",
            "protocol_version": 106,
            "binary_sha256": sha(f"{BACKFILL}/server/releases/v0.103.0/"
                                  "phase-server-slim-x86_64-unknown-linux-musl"),
            "card_data_sha256": sha(f"{BACKFILL}/server/releases/v0.103.0/data/"
                                     "card-data.json"),
            "draft_pools_sha256": sha(f"{BACKFILL}/server/releases/v0.103.0/data/"
                                       "draft-pools.json"),
            "port": 9374,
            "run_dir": "runs/run-20261007-1011",
        },
        "scenario_sha256": hashlib.sha256(scenario_src).hexdigest(),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "verdict": verdict,
        "assertions": ass,
        "notes": notes,
        "stats": {"states_seen": ST["states_seen"]},
        "setup_line": ("P0: 4x Mr. House + 24x Adorable Kitten + 33 lands; "
                       "P1: 60x Forest dummy; native human seats"),
        "contract_line": ("cast Mr. House, then cast Kittens one at a time; "
                          "each Kitten ETB rolls 1d6 (life-gain delta = "
                          "result) with House's RolledDieOnce trigger on the "
                          "BF; assert a 6+ roll creates a Robot AND a "
                          "Treasure (reported: only the Robot)"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "24x Kitten / 4x House densities are test-harness conveniences "
            "(engine accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
            "Rolls come from Adorable Kitten's ETB, not House's own {4}{T} "
            "roll ability (still Unimplemented unparsed_quantity in the "
            "pinned data) -- the trigger under test is House's roll trigger.",
        ],
        "driver_notes": [
            "Protocol-106 port of scenario_7177.py (verified 2026-09-15, "
            "run 20260915-7177).",
            "Merged_actions/vi conventions, NON_DECISION_CODES, vi "
            "tapLandForMana payment, PASSED_REV gating, and progress "
            "watchdogs taken from the proven 20261007-6771 template.",
            "Rolls are measured as (life, robot, treasure) deltas per "
            "kitten once the kitten is on the BF and the stack has been "
            "empty for 8 consecutive ticks; abandoned pending rolls are "
            "recorded as not-run with a note.",
            "Pinned v0.103.0 data still parses the 6+ branch as "
            "Unimplemented instead_condition -- see data_evidence.json.",
        ],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run_meta, f, indent=1)

    say(f"DONE verdict={verdict} assertions={json.dumps(ass)}")
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    await p0.close()
    await p1.close()


if __name__ == "__main__":
    asyncio.run(main())
