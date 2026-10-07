#!/usr/bin/env python3
"""Issue #6910: Reducers behaving incorrectly - Goreclaw reduces ALL spells;
Thryx reduces nothing.

Oracle (verified from pinned v0.103.0 card-data.json):
  Goreclaw, Terror of Qal Sisma: "Creature spells you cast with power 4 or
  greater cost {2} less to cast."
  Thryx, the Sudden Storm: "Spells you cast with mana value 5 or greater
  cost {1} less to cast and can't be countered."

Reported (Discord 2026-08-02): Goreclaw seems to reduce the cost of ALL
spells by 2; meanwhile Thryx doesn't appear to add his reduction to
anything at all.

Data-level premise (v0.103.0 card-data.json, see data_evidence.json):
  - Goreclaw's static_abilities[0] = ModifyCost/Reduce {2} with
    affected = Typed Card you cast, properties = [] -- the "power 4 or
    greater" qualifier is LOST (type_filters ["Card"], no properties).
    The engine therefore reduces every spell by {2}.
  - Thryx has only CantBeCountered; the "cost {1} less" half is absent.
    Thryx reduces nothing.

Behavioral contract (native engine, protocol 106, two human seats):
  P0 casts Goreclaw, then casts, on separate fresh turns (all lands
  untapped at pre-cast so tapped-land delta == mana actually paid):
    T1 Overrun {2}{G}{G}{G} (sorcery, NON-creature): correct=5.
       Bug (reduction applied to all spells): 3.
    T2 Grizzly Bears {1}{G} (creature, power 2 < 4): correct=2.
       Bug: 1 (generic-only reduction) or 0 (total reduction).
    T3 Colossal Dreadmaw {4}{G}{G} (creature, power 6): correct=4
       (positive control: reduction legitimately applies).
  P1 casts Thryx, then casts Colossal Dreadmaw (MV 6):
    T4: correct=5 (one reduction); bug (no reduction at all): 6.

Assertions measure tapped-land deltas from saved pre/post states.
Verdict: reproduced iff A1 passes and any cost assertion fails in the
reported direction; not-reproduced iff all pass; blocked iff A1 fails.

Protocol-106 port of driver/scenario_6910.py (verified 2026-09-13 on
protocol 70, run 20260912-6910a, verdict reproduced). Conventions taken
from the proven scenario_6907_01030.py template:
  - CreateGameWithSettings + JoinGameWithPassword + start_when_full
  - merged_actions/vi, PASSED_REV gating, Select-model discards
    (schema+select required), schema-select BottomCards
  - stack-watch fall-through: never hold priority while waiting on a
    resolution; in-flight watch always falls through to the pass gate
  - host-only exports (P0C)
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 6910
RUN_ID = "20261007-6910"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.103.0/data/card-data.json"))

GORECLAW = "goreclaw, terror of qal sisma"
THRYX = "thryx, the sudden storm"
OVERRUN = "overrun"
BEAR = "grizzly bears"
DREADMAW = "colossal dreadmaw"
FOREST = "forest"
ISLAND = "island"

P0_DECK = [(GORECLAW, 4), (OVERRUN, 4), (BEAR, 4), (DREADMAW, 4),
           (FOREST, 44)]
P1_DECK = [(THRYX, 4), (DREADMAW, 4), (ISLAND, 26), (FOREST, 26)]

TIMEOUT = 2400
PROGRESS_WATCHDOG_S = 180
SETUP_ABORT_S = 600

ST = {}
MULLS = set()
SUBMITTED_OPPS = set()
LAND_PLAYED_TURN = {}
PASSED_REV = {}
WF_SEEN = []
DISCARD_WIRED = set()
DISCARD_SUBMITTED = set()
P0C = None  # host client (P0 created the game); only the host may export


def reset_state():
    ST.clear()
    ST.update({
        "stage": "SETUP",   # SETUP -> TEST -> DONE
        "stop": False,
        "states_seen": 0,
        "mulligans": 0,
        "mulligans_p1": 0,
        "goreclaw_cast_turn": None,
        "thryx_cast_turn": None,
        "tests": [
            {"key": "overrun", "name": OVERRUN, "player": 0,
             "printed": 5, "expect": 5, "gate": 5,
             "resolve_zone": "Graveyard",
             "note": "non-creature: no reduction expected",
             "needs": {"G": 3, "generic": 2}},
            {"key": "bears", "name": BEAR, "player": 0,
             "printed": 2, "expect": 2, "gate": 2,
             "resolve_zone": "Battlefield",
             "note": "creature power 2 < 4: no reduction expected",
             "needs": {"G": 1, "generic": 1}},
            {"key": "dreadmaw_goreclaw", "name": DREADMAW, "player": 0,
             "printed": 6, "expect": 4, "gate": 4,
             "resolve_zone": "Battlefield",
             "note": "creature power 6: {2} reduction expected (control)",
             "needs": {"G": 2, "generic": 4}},
            {"key": "dreadmaw_thryx", "name": DREADMAW, "player": 1,
             "printed": 6, "expect": 5, "gate": 6,
             "resolve_zone": "Battlefield",
             "note": "MV 6 with Thryx: {1} reduction expected",
             "needs": {"G": 2, "generic": 4}},
        ],
        "watch": None,   # {"key","oid","since","player"}
        "mana_needs": {},
        "setup_t0": None,
        "game_code": None,
    })
    for t in ST["tests"]:
        t.update({"inflight": False, "done": False, "pre_untapped": None,
                  "post_untapped": None, "pre_turn": None,
                  "spell_oid": None, "paid": None})


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


def untapped_named(state, pid, name):
    return sum(1 for o in bf_oids(state, pid)
               if obj_lname(state, o) == name
               and not get_obj(state, o).get("tapped"))


def untapped_land_count(state, pid):
    return sum(1 for o in bf_oids(state, pid)
               if is_land(get_obj(state, o))
               and not get_obj(state, o).get("tapped"))


def all_lands_untapped(state, pid):
    lands = [o for o in bf_oids(state, pid) if is_land(get_obj(state, o))]
    return len(lands) > 0 and all(
        not get_obj(state, o).get("tapped") for o in lands)


def zone_of(state, oid):
    return get_obj(state, oid).get("zone")


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


def choice_id_of(ch):
    return ch.get("id") or ch.get("choiceId") or ch.get("choice_id")


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
    import websockets
    ws = await websockets.connect("ws://127.0.0.1:9374/ws",
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
    """Document the data-level premise of the bug: Goreclaw's reducer lost
    its 'power 4 or greater' qualifier (affected properties empty), and
    Thryx has no cost-reduction static at all."""
    notes = []
    g = CARD_DATA.get(GORECLAW, {})
    t = CARD_DATA.get(THRYX, {})
    g_sas = g.get("static_abilities") or []
    t_sas = t.get("static_abilities") or []
    def is_modify_cost(sa):
        mode = sa.get("mode") or {}
        if isinstance(mode, dict):
            return bool(mode.get("ModifyCost"))
        return str(mode).lower().startswith("modifycost")
    g_red = [sa for sa in g_sas if is_modify_cost(sa)]
    t_red = [sa for sa in t_sas if is_modify_cost(sa)]
    goreclaw_qualifier_lost = False
    if not g_red:
        notes.append("Goreclaw: no ModifyCost static at all (premise changed)")
    else:
        aff = (g_red[0].get("affected") or {})
        props = aff.get("properties") or []
        tfs = aff.get("type_filters") or []
        goreclaw_qualifier_lost = not props
        _gmode = g_red[0].get("mode") or {}
        _amt = (_gmode.get("ModifyCost") or {}).get("amount") \
            if isinstance(_gmode, dict) else _gmode
        notes.append(f"Goreclaw: ModifyCost reduce={_amt}; "
                     f"affected type_filters={tfs} properties={props} "
                     f"-> qualifier_lost={goreclaw_qualifier_lost}")
    thryx_reduction_absent = not t_red
    def mode_key(sa):
        m = sa.get("mode")
        return list(m.keys()) if isinstance(m, dict) else [str(m)]
    notes.append(f"Thryx: ModifyCost statics={len(t_red)} "
                 f"-> reduction_absent={thryx_reduction_absent}; "
                 f"static modes={[mode_key(sa) for sa in t_sas]}")
    for nm, key in (("Overrun", OVERRUN), ("Grizzly Bears", BEAR),
                    ("Colossal Dreadmaw", DREADMAW),
                    ("Goreclaw, Terror of Qal Sisma", GORECLAW),
                    ("Thryx, the Sudden Storm", THRYX)):
        cc = CARD_DATA.get(key, {})
        notes.append(f"{nm}: cost={cc.get('mana_cost')} power={cc.get('power')}")
    payload = {"goreclaw_static_abilities": g_sas,
               "thryx_static_abilities": t_sas,
               "goreclaw_qualifier_lost": goreclaw_qualifier_lost,
               "thryx_reduction_absent": thryx_reduction_absent,
               "notes": notes}
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(payload, f, indent=1, default=str)
    say("data-level check: " + " | ".join(notes))
    return goreclaw_qualifier_lost and thryx_reduction_absent


# ------------------------------------------------- mulligan / bottom / discard

def mulligan_pending_for(state, pid):
    d = ((state.get("waiting_for") or {}).get("data") or {})
    for p in d.get("pending", []) or []:
        ph = p.get("phase") or {}
        if p.get("player") == pid and str(ph.get("type")) == "Declare":
            return True
    return False


async def do_mulligan(c, acts, st, pid, tag):
    state = st["state"]
    if not mulligan_pending_for(state, pid):
        return False
    key = (tag, "mull", f"rev{c.revision}")
    if key in MULLS:
        return True
    adv = next((a for a in acts if a.get("type") == "MulliganDecision"),
               None)
    if adv is None:
        return False
    MULLS.add(key)
    hn = hand_lnames(state, pid)
    lands = sum(1 for o in hand_ids(state, pid)
                if is_land(get_obj(state, o)))
    if pid == 0:
        want, cap, mkey = GORECLAW, 3, "mulligans"
    else:
        want, cap, mkey = (THRYX, DREADMAW), 2, "mulligans_p1"
    if lands < 2:
        ST[mkey] += 1
        say(f"[{tag}] mulligans ({ST[mkey]}) - land-light")
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
    elif ST[mkey] < cap and not all(w in hn for w in (
            want if isinstance(want, tuple) else (want,))):
        ST[mkey] += 1
        say(f"[{tag}] mulligans ({ST[mkey]}) seeking {want} "
            f"(hand={hn[:6]})")
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
    else:
        say(f"[{tag}] keeps (hand={len(hn)}: {hn[:6]})")
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Keep"}}})
    return True


async def do_bottom(c, acts, st, pid, tag):
    """Protocol 106: bottom-after-mulligan surfaces as SelectCards legal
    actions plus a vi schema/select opportunity. Proven pattern from
    scenario_1234_01030.py."""
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
        return False
    opp, cands, spec = target
    iid = opp.get("interactionId")
    key = (tag, "bottom", iid)
    if key in SUBMITTED_OPPS:
        return False
    con = ((spec.get("data", {}) or {}).get("constraint", {}) or {}) \
        .get("data", {}) or {}
    n = int(con.get("min") or con.get("max") or 1)
    if n <= 0:
        return False
    keep = (GORECLAW,) if pid == 0 else (THRYX, DREADMAW)

    def bkey(ch):
        oid = None
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict) and "reference" in d:
                oid = d.get("reference")
                break
        nm = obj_lname(state, oid) if oid is not None else "?"
        if nm in keep:
            return (2, str(oid))
        if oid is not None and is_land(get_obj(state, oid)):
            return (0, str(oid))
        return (1, str(oid))

    ranked = sorted(cands, key=bkey)
    picks = ranked[:n]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {n} cards")
    wire("bottom", {"who": tag, "iid": iid,
                    "picks": [x.get("id") for x in picks]})
    await interact_as(c, {"interactionId": iid,
                          "response": {"type": "select",
                                       "data": {"choiceIds":
                                                [x.get("id")
                                                 for x in picks]}}}, tag)
    return True


def is_select_schema_opp(opp):
    resp = opp.get("response", {}) or {}
    if resp.get("type") != "schema":
        return False
    rdata = resp.get("data", {}) or {}
    spec = rdata.get("spec", {}) or {}
    return spec.get("type") == "select"


def discard_rank(pid):
    keep0 = {GORECLAW, OVERRUN, BEAR, DREADMAW}
    keep1 = {THRYX, DREADMAW}

    def rank(state):
        def _rank(oid):
            nm = obj_lname(state, oid)
            if pid == 0:
                if nm in keep0:
                    return (1, nm)
            else:
                if nm in keep1:
                    return (1, nm)
            return (0, nm)  # lands first
        return _rank
    return rank


async def do_discard(c, st, tag, pid):
    state = st["state"]
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    select_opps = [o for o in vi_ops(st) if is_select_schema_opp(o)]
    if not select_opps:
        return False
    rank = discard_rank(pid)
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

        def _ref(ch):
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") or {}
                if isinstance(d, dict) and "reference" in d:
                    return d.get("reference")
            return None

        ranked = sorted(cands, key=lambda ch: rank(state)(_ref(ch)))
        picks = [ch["id"] for ch in ranked[:n] if ch.get("id")]
        if len(picks) < n:
            continue
        DISCARD_SUBMITTED.add(iid)
        say(f"[{tag}] discarding {n} to hand size")
        wire("discard_submit", {"who": tag, "iid": iid, "n": n})
        await interact_as(c, {"interactionId": iid,
                              "response": {"type": "select",
                                           "data": {"choiceIds": picks}}},
                          tag)
        return True
    return False


# ------------------------------------------------------------------ payment

async def pay_tick(c, acts, tag=None):
    # Only pay via legacy actions when this seat has outstanding mana
    # needs (setup casts). Test casts rely on the engine's Auto payment;
    # answering here would double-pay.
    if tag is not None and sum(ST["mana_needs"].get(tag, {}).values()) <= 0:
        return False
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
                   "response": {"type": stype,
                               "data": {"choiceIds": [cid]}}}
        else:
            sub = {"interactionId": iid2,
                   "response": {"type": "choose",
                               "data": {"choiceId": cid}}}
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
                await interact_as(
                    c, {"interactionId": opp.get("interactionId"),
                        "response": {"type": "choose",
                                     "data": {"choiceId": ch.get("id")}}},
                    c.name)
                return True
    return False


async def play_a_land(c, state, pid, acts, tag, prefer=None):
    turn = state.get("turn_number")
    if LAND_PLAYED_TURN.get(tag) == turn:
        return False
    cands = [o for o in hand_ids(state, pid) if is_land(get_obj(state, o))]
    if prefer:
        cands.sort(key=lambda o: (prefer.index(obj_lname(state, o))
                                  if obj_lname(state, o) in prefer else 99))
    for o in cands:
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


def test_by_key(key):
    return next(t for t in ST["tests"] if t["key"] == key)


def p0_tests_done():
    return all(t["done"] for t in ST["tests"] if t["player"] == 0)


# ------------------------------------------------------------------ exports

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


# ------------------------------------------------------------------ tests

async def start_test(c, pid, state, acts, t, tag):
    """Export pre-cast state and submit the test cast. Exports always go
    through the host client (P0C): only the game host may export."""
    t["pre_untapped"] = untapped_land_count(state, pid)
    t["pre_turn"] = state.get("turn_number")
    await do_export(P0C, f"pre_test_{t['key']}.json")
    a, oid = cast_action_for(acts, state, t["name"])
    if a is None:
        wire("test_cast_not_advertised", {"key": t["key"]})
        say(f"[{tag}] test {t['key']}: cast not advertised; marking done "
            f"(failure)")
        t["done"] = True  # avoid spinning; recorded as failure
        t["note"] += " [cast never advertised]"
        return True
    # NOTE: no driver-side mana payment for test casts. The CastSpell
    # action carries payment_mode Auto and the engine taps lands itself;
    # driver vi taps were double-paying (engine auto-tapped the reduced
    # cost on top), confounding the tapped-land delta. The delta now
    # measures exactly what the engine computed.
    await submit_as_is(c, a)
    t["inflight"] = True
    t["spell_oid"] = str(oid)
    ST["watch"] = {"key": t["key"], "oid": str(oid),
                   "since": time.time(), "player": pid}
    say(f"[{tag}] test {t['key']}: cast {t['name']} (oid {oid}); "
        f"pre_untapped={t['pre_untapped']}")
    return True


async def watch_inflight(c, state, tag):
    """Check whether the in-flight test spell resolved. NEVER returns True
    in a way that skips the priority-pass logic -- callers fall through."""
    w = ST.get("watch")
    if not w:
        return
    t = test_by_key(w["key"])
    pid = w["player"]
    oid = w["oid"]
    z = zone_of(state, oid)
    resolved = z == t["resolve_zone"]
    if not resolved and time.time() - w["since"] > 120:
        wire("test_watch_timeout", {"key": t["key"], "zone": z})
        await do_export(P0C, f"mid_test_{t['key']}_timeout.json")
        t["done"] = True
        t["inflight"] = False
        ST["watch"] = None
        say(f"[{tag}] test {t['key']}: watch timed out (zone={z})")
        return
    if resolved:
        t["post_untapped"] = untapped_land_count(state, pid)
        t["paid"] = (t["pre_untapped"] or 0) - (t["post_untapped"] or 0)
        t["done"] = True
        t["inflight"] = False
        ST["watch"] = None
        await do_export(P0C, f"post_test_{t['key']}.json")
        say(f"[{tag}] test {t['key']}: resolved to {z}; paid={t['paid']} "
            f"(expect {t['expect']})")


# ------------------------------------------------------------------ ticks

async def p0_tick(c, st, tag):
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 0, tag):
        return
    if await do_bottom(c, acts, st, 0, tag):
        return
    if await do_discard(c, st, tag, 0):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a.get("type") == "DeclareAttackers"), None)
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
    if await pay_tick(c, acts, tag):
        return
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return

    # in-flight test watch: check resolution, then ALWAYS fall through to
    # the priority-pass logic below (never hold priority while watching).
    if ST.get("watch") and ST["watch"]["player"] == 0:
        await watch_inflight(c, state, tag)

    if ST["stage"] == "SETUP":
        if bf_by_name(state, 0, GORECLAW):
            if ST["goreclaw_cast_turn"] is None:
                ST["goreclaw_cast_turn"] = state.get("turn_number")
            if bf_by_name(state, 1, THRYX):
                ST["stage"] = "TEST"
                say(f"Goreclaw + Thryx on battlefield "
                    f"(turn {state.get('turn_number')}); stage -> TEST")
            # NOTE: no early return here -- P0 must keep passing priority
            # while waiting for P1's Thryx, or the game deadlocks.
        elif my_main(state, 0):
            if await play_a_land(c, state, 0, acts, tag,
                                 prefer=[FOREST]):
                return
            goid = next((o for o in hand_ids(state, 0)
                         if obj_lname(state, o) == GORECLAW), None)
            a, _ = cast_action_for(acts, state, GORECLAW)
            if goid is not None and a is not None \
                    and untapped_land_count(state, 0) >= 4 \
                    and untapped_named(state, 0, FOREST) >= 1 \
                    and not bf_by_name(state, 0, GORECLAW):
                say(f"[{tag}] casting Goreclaw (oid {goid})")
                wire("cast_goreclaw", {"oid": goid})
                ST["mana_needs"][tag] = {"G": 1, "generic": 3}
                await submit_as_is(c, a)
                ST["goreclaw_cast_turn"] = state.get("turn_number")
                return
        if real_decision_pending(st):
            return
        if my_priority(acts):
            if PASSED_REV.get(c.name, -1) < c.revision:
                await pass_priority(c, st, acts)
                PASSED_REV[c.name] = c.revision
        return

    if ST["stage"] == "TEST":
        for t in ST["tests"]:
            if t["player"] != 0 or t["done"]:
                continue
            if t["inflight"]:
                break  # watch handles it; else fall through to pass
            if my_main(state, 0) and all_lands_untapped(state, 0) \
                    and untapped_land_count(state, 0) >= t["gate"] \
                    and t["name"] in hand_lnames(state, 0):
                await start_test(c, 0, state, acts, t, tag)
                return
            break  # only the earliest pending test
        if my_main(state, 0):
            if await play_a_land(c, state, 0, acts, tag, prefer=[FOREST]):
                return
        if real_decision_pending(st):
            return
        if my_priority(acts):
            if PASSED_REV.get(c.name, -1) < c.revision:
                await pass_priority(c, st, acts)
                PASSED_REV[c.name] = c.revision
        return


async def p1_tick(c, st, tag):
    state = st["state"]
    acts = merged_actions(st)
    atypes = set(a.get("type") for a in acts)
    if await do_mulligan(c, acts, st, 1, tag):
        return
    if await do_bottom(c, acts, st, 1, tag):
        return
    if await do_discard(c, st, tag, 1):
        return
    if "DeclareAttackers" in atypes:
        da = next((a for a in acts if a.get("type") == "DeclareAttackers"), None)
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
    if await pay_tick(c, acts, tag):
        return
    needs = ST["mana_needs"].get(tag, {})
    if sum(needs.values()) > 0:
        if await pay_mana_vi(c, st, tag, needs):
            return

    # in-flight test watch: check resolution, then ALWAYS fall through.
    if ST.get("watch") and ST["watch"]["player"] == 1:
        await watch_inflight(c, state, tag)

    if ST["stage"] == "SETUP":
        if bf_by_name(state, 1, THRYX):
            if ST["thryx_cast_turn"] is None:
                ST["thryx_cast_turn"] = state.get("turn_number")
            if bf_by_name(state, 0, GORECLAW):
                ST["stage"] = "TEST"
                say(f"Goreclaw + Thryx on battlefield "
                    f"(turn {state.get('turn_number')}); stage -> TEST")
            # NOTE: no early return here -- P1 must keep passing priority
            # while waiting for P0's Goreclaw, or the game deadlocks.
        elif my_main(state, 1):
            if await play_a_land(c, state, 1, acts, tag,
                                 prefer=[ISLAND, FOREST]):
                return
            toid = next((o for o in hand_ids(state, 1)
                         if obj_lname(state, o) == THRYX), None)
            a, _ = cast_action_for(acts, state, THRYX)
            if toid is not None and a is not None \
                    and untapped_land_count(state, 1) >= 5 \
                    and untapped_named(state, 1, ISLAND) >= 2 \
                    and not bf_by_name(state, 1, THRYX):
                say(f"[{tag}] casting Thryx (oid {toid})")
                wire("cast_thryx", {"oid": toid})
                ST["mana_needs"][tag] = {"U": 2, "generic": 3}
                await submit_as_is(c, a)
                ST["thryx_cast_turn"] = state.get("turn_number")
                return
        if real_decision_pending(st):
            return
        if my_priority(acts):
            if PASSED_REV.get(c.name, -1) < c.revision:
                await pass_priority(c, st, acts)
                PASSED_REV[c.name] = c.revision
        return

    if ST["stage"] == "TEST" and p0_tests_done():
        t = test_by_key("dreadmaw_thryx")
        if not t["done"] and not t["inflight"]:
            if my_main(state, 1) and all_lands_untapped(state, 1) \
                    and untapped_land_count(state, 1) >= t["gate"] \
                    and t["name"] in hand_lnames(state, 1):
                await start_test(c, 1, state, acts, t, tag)
                return
        if my_main(state, 1):
            if await play_a_land(c, state, 1, acts, tag,
                                 prefer=[ISLAND, FOREST]):
                return
        if real_decision_pending(st):
            return
        if my_priority(acts):
            if PASSED_REV.get(c.name, -1) < c.revision:
                await pass_priority(c, st, acts)
                PASSED_REV[c.name] = c.revision
        return

    # P1 passive otherwise: land, then pass.
    if ST["stage"] == "TEST" and my_main(state, 1):
        if await play_a_land(c, state, 1, acts, tag, prefer=[ISLAND, FOREST]):
            return
    if real_decision_pending(st):
        return
    if my_priority(acts):
        if PASSED_REV.get(c.name, -1) < c.revision:
            await pass_priority(c, st, acts)
            PASSED_REV[c.name] = c.revision


# ------------------------------------------------------------- evaluation

def evaluate(notes):
    A, D = {}, {}
    pre_overrun = env_state("pre_test_overrun.json")
    pre_thryxleg = env_state("pre_test_dreadmaw_thryx.json")
    post_all = env_state("post_all_tests.json")

    # A1: Goreclaw on P0 BF (pre_test_overrun) and Thryx on P1 BF (any
    # state that exists: pre_test_dreadmaw_thryx preferred, else the
    # latest available post state).
    g_ok = pre_overrun and len(bf_by_name(pre_overrun, 0, GORECLAW)) >= 1
    thryx_state = pre_thryxleg or post_all
    if not thryx_state:
        for t in reversed(ST["tests"]):
            thryx_state = env_state(f"post_test_{t['key']}.json")
            if thryx_state:
                break
    t_ok = thryx_state and len(bf_by_name(thryx_state, 1, THRYX)) >= 1
    A["A1_setup_ok"] = "passed" if (g_ok and t_ok) else "failed"
    D["A1_setup_ok"] = (f"goreclaw_on_p0_bf={bool(g_ok)} "
                        f"thryx_on_p1_bf={bool(t_ok)}")

    # cost assertions: paid = pre_untapped - post_untapped
    for t, aid in (("overrun", "A2_overrun_cost"),
                   ("bears", "A3_bears_cost"),
                   ("dreadmaw_goreclaw", "A4_dreadmaw_goreclaw_control"),
                   ("dreadmaw_thryx", "A5_thryx_dreadmaw_cost")):
        tt = test_by_key(t)
        if tt["paid"] is None:
            A[aid] = "not-run"
            D[aid] = (f"test {t} never completed "
                      f"(inflight={tt['inflight']})")
            continue
        ok = tt["paid"] == tt["expect"]
        A[aid] = "passed" if ok else "failed"
        D[aid] = (f"{tt['name']} (P{tt['player']}): paid {tt['paid']} mana "
                  f"(printed {tt['printed']}, expect {tt['expect']}); "
                  f"{tt['note']}")

    # A6 cleanup
    final = post_all
    if not final:
        for t in reversed(ST["tests"]):
            final = env_state(f"post_test_{t['key']}.json")
            if final:
                break
    if final:
        ok = not (final.get("stack") or [])
        A["A6_cleanup"] = "passed" if ok else "failed"
        D["A6_cleanup"] = (f"stack empty={ok}; "
                           f"turn={final.get('turn_number')} "
                           f"phase={final.get('phase')}")
    else:
        A["A6_cleanup"] = "not-run"
        D["A6_cleanup"] = "no final post state"

    if A.get("A1_setup_ok") != "passed":
        verdict = "blocked"
    elif all(A.get(k) == "passed" for k in
             ("A2_overrun_cost", "A3_bears_cost",
              "A4_dreadmaw_goreclaw_control", "A5_thryx_dreadmaw_cost",
              "A6_cleanup")):
        verdict = "not-reproduced"
    else:
        verdict = "reproduced"
    return A, D, verdict


# ------------------------------------------------------------------- main

async def main():
    reset_state()
    t0 = time.time()
    notes = []
    ST["setup_t0"] = t0
    await verify_server_hello()
    data_ok = check_data_level()

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
                        ("A1_setup_ok", "A2_overrun_cost",
                         "A3_bears_cost", "A4_dreadmaw_goreclaw_control",
                         "A5_thryx_dreadmaw_cost", "A6_cleanup")},
                       "notes": notes}, f, indent=2)
        await p0.close()
        return
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(*P1_DECK))
    ST["game_code"] = p0.game_code
    global P0C
    P0C = p0
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
            for r in c.rejections:
                wire("rejected", {"who": c.name, "type": r["type"],
                                  "data": r["data"],
                                  "stage": ST.get("stage")})
                say(f"[{c.name}] {r['type']}: "
                    f"{json.dumps(r['data'])[:300]}")
            c.rejections.clear()

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

        # SETUP abort: both reducer creatures never landed
        if ST["stage"] == "SETUP" and time.time() - t0 > SETUP_ABORT_S:
            await do_export(p0, "mid_setup.json")
            notes.append(f"SETUP abort after {SETUP_ABORT_S}s (turn "
                         f"{state.get('turn_number')})")
            say("[setup-abort] reducer creatures never both landed; ending")
            ST["stop"] = True
            continue

        # all tests done -> settle a turn, export final, done
        if all(t["done"] for t in ST["tests"]):
            if not ST.get("settle_turn"):
                ST["settle_turn"] = state.get("turn_number")
                await do_export(p0, "post_all_tests.json")
                say("all tests complete; post_all_tests exported")
            if (state.get("turn_number") or 0) > ST["settle_turn"]:
                ST["stage"] = "DONE"
                ST["stop"] = True
                say("settled one turn past completion; stopping")

        if time.time() - last_diag > 60:
            last_diag = time.time()
            say(f"DIAG turn={state.get('turn_number')} "
                f"active={state.get('active_player')} phase={state.get('phase')} "
                f"wf={wf} stage={ST['stage']} "
                f"goreclaw={bool(bf_by_name(state, 0, GORECLAW))} "
                f"thryx={bool(bf_by_name(state, 1, THRYX))} "
                f"tests_done={[t['key'] for t in ST['tests'] if t['done']]} "
                f"P0hand={hand_lnames(state, 0)[:6]}")

    if not env_state("post_all_tests.json"):
        try:
            await do_export(p0, "post.json")
            notes.append("post.json exported at finish() fallback")
        except Exception as e:
            notes.append(f"post export failed: {e}")

    ass, det, verdict = evaluate(notes)
    for k in sorted(ass):
        say(f"{k}: {ass[k]} -- {det.get(k, '')}")
    say("WF sequence:", WF_SEEN)

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": ass, "details": det, "notes": notes,
                   "wf_sequence": WF_SEEN,
                   "data_level_ok": data_ok}, f, indent=2)

    scenario_src = open(__file__, "rb").read()
    with open(f"{EVDIR}/scenario_6910.py", "w") as f:
        f.write(scenario_src.decode())

    def sha(p):
        return hashlib.sha256(open(p, "rb").read()).hexdigest()

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
        },
        "scenario": "driver/scenario_6910_01030.py",
        "scenario_sha256": hashlib.sha256(scenario_src).hexdigest(),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "verdict": verdict,
        "assertions": ass,
        "notes": notes,
        "stats": {"states_seen": ST["states_seen"]},
        "setup_line": ("P0: 4x Goreclaw + 4x Overrun + 4x Grizzly Bears + 4x "
                       "Colossal Dreadmaw + 44x Forest; "
                       "P1: 4x Thryx + 4x Colossal Dreadmaw + 26x Island + "
                       "26x Forest; native human seats"),
        "contract_line": ("P0 casts Overrun (non-creature), Grizzly Bears "
                          "(P2), Colossal Dreadmaw (P6) with Goreclaw on "
                          "board; P1 casts Colossal Dreadmaw (MV6) with "
                          "Thryx on board. Each test cast on a fresh turn "
                          "with all lands untapped; mana paid = tapped-land "
                          "delta. Expected payments: 5 / 2 / 4 / 5."),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "4x densities are test-harness conveniences (engine accepts >4-of "
            "for custom games).",
            "States are authoritative exports, restorable only via full game "
            "replay (the phase-server has no standalone state-import path).",
        ],
        "driver_notes": [
            "Protocol-106 port of scenario_6910.py (verified 2026-09-13, "
            "run 20260912-6910a, verdict reproduced).",
            "Conventions from the proven scenario_6907_01030.py template: "
            "merged_actions/vi, PASSED_REV gating, Select-model hand-size "
            "discards (schema+select required), schema-select BottomCards, "
            "vi tapLandForMana payment, and the never-hold-priority-while-"
            "watching fall-through rule.",
            "Pinned v0.103.0 data parses Goreclaw's reducer with the 'power "
            "4 or greater' qualifier LOST (affected properties empty) and "
            "carries no cost-reduction static for Thryx at all -- see "
            "data_evidence.json.",
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
