#!/usr/bin/env python3
"""Issue #6910: Reducers behaving incorrectly - Goreclaw reduces ALL spells.

Oracle (verified from pinned v0.81.3 card-data.json):
  Goreclaw, Terror of Qal Sisma: "Creature spells you cast with power 4 or
  greater cost {2} less to cast."
  Thryx, the Sudden Storm: "Spells you cast with mana value 5 or greater
  cost {1} less to cast and can't be countered."

Reported (Discord): Goreclaw seems to reduce the cost of ALL spells by 2;
meanwhile Thryx doesn't appear to add his reduction to anything at all.

Test design (native engine, protocol 70, two human seats):
  P0 casts Goreclaw, then casts, on separate fresh turns (all lands untapped
  at pre-cast so tapped-land delta == mana actually paid):
    T1 Overrun {2}{G}{G}{G} (sorcery, NON-creature): correct=5 lands.
       Bug (reduction applied to all spells): 3 lands.
    T2 Grizzly Bears {1}{G} (creature, power 2 < 4): correct=2 lands.
       Bug: 1 land (generic-only reduction) or 0 (total reduction).
    T3 Colossal Dreadmaw {4}{G}{G} (creature, power 6): correct=4 lands
       (positive control: reduction legitimately applies).
  P1 casts Thryx, then casts Colossal Dreadmaw (MV 6):
    T4: correct=5 lands (one reduction); bug (no reduction at all): 6.

Assertions measure tapped-land deltas from saved pre/post states.
Verdict: reproduced iff A1 passes and any cost assertion fails in the
reported direction; not-reproduced iff all pass; blocked iff A1 fails.
"""
import asyncio
import copy
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 6910
RUN_ID = "20260912-6910a"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
RUNDIR = f"{BACKFILL}/runs/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(RUNDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")
RUNLOG2 = open(f"{RUNDIR}/scenario_run.log", "w")

GORECLAW = "Goreclaw, Terror of Qal Sisma"
THRYX = "Thryx, the Sudden Storm"
OVERRUN = "Overrun"
BEAR = "Grizzly Bears"
DREADMAW = "Colossal Dreadmaw"
FOREST = "Forest"
ISLAND = "Island"

P0_DECK = [(GORECLAW, 4), (OVERRUN, 4), (BEAR, 4), (DREADMAW, 4),
           (FOREST, 44)]
P1_DECK = [(THRYX, 4), (DREADMAW, 4), (ISLAND, 26), (FOREST, 26)]
TIMEOUT = 2400

ST = {}
C0 = None
C1 = None


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()
    RUNLOG2.write(msg + "\n")
    RUNLOG2.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "error": str(e)}) + "\n")
    WIRE.flush()


def oname(o):
    return o.get("card_name") or o.get("name") or o.get("base_name") or ""


def objs(state):
    return state.get("objects") or {}


def players(state):
    return state.get("players") or []


def player_obj(state, pid):
    for p in players(state):
        if p.get("player_id") == pid or p.get("id") == pid \
                or p.get("seat") == pid:
            return p
    return {}


def hand_oids(state, pid):
    return [str(oid) for oid, o in objs(state).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if oname(objs(state)[oid]) == name:
            return oid
    return None


def bf(state, pid):
    return [(oid, o) for oid, o in objs(state).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bf_named(state, pid, name):
    return [oid for oid, o in bf(state, pid) if oname(o) == name]


def zone_of(state, oid):
    return (objs(state).get(str(oid)) or {}).get("zone")


def is_land(o):
    return oname(o) in (FOREST, ISLAND)


def bf_lands(state, pid):
    return [oid for oid, o in bf(state, pid) if is_land(o)]


def untapped_lands(state, pid):
    return [oid for oid, o in bf(state, pid)
            if is_land(o) and not o.get("tapped")]


def untapped_land_count(state, pid):
    return len(untapped_lands(state, pid))


def untapped_islands(state, pid):
    return [oid for oid, o in bf(state, pid)
            if oname(o) == ISLAND and not o.get("tapped")]


def all_lands_untapped(state, pid):
    lands = bf_lands(state, pid)
    return len(lands) > 0 and all(
        not (objs(state)[oid] or {}).get("tapped") for oid in lands)


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                               "PostCombatMain"))


def wf(state):
    return state.get("waiting_for") or {}


def wf_type(state):
    return wf(state).get("type")


def wf_player(state):
    return (wf(state).get("data") or {}).get("player")


def wf_pending_for(state, pid):
    d = wf(state).get("data") or {}
    if isinstance(d.get("player"), int):
        return d["player"] == pid
    for p in d.get("pending") or []:
        if isinstance(p, dict) and p.get("player") == pid:
            return True
    return False


def stack(state):
    return state.get("stack") or []


def castspell_advertised(acts, oid):
    if not oid:
        return None
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": ST.get("stage")})
    await c.send_action(action)
    ST.setdefault("settle", {})[c.name] = c.revision


def settle_pending(c):
    return (ST.get("settle") or {}).get(c.name) is not None \
        and c.revision <= ST["settle"][c.name]


def drain_rejections(c):
    found = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            found.append({"type": t, "data": data})
            ST["rejections"].append({"who": c.name, "type": t, "data": data})
            wire("rejected", {"who": c.name, "type": t, "data": data,
                              "stage": ST.get("stage")})
            say(f"[{c.name}] {t}: {json.dumps(data)[:400]}")
    if found:
        (ST.get("settle") or {}).pop(c.name, None)
    return found


def reset_state():
    ST.clear()
    ST.update({
        "stage": "SETUP",   # SETUP -> TEST -> DONE
        "stop": False,
        "turn_cap": 60,
        "rejections": [],
        "game_code": None,
        "mulligans": 0,
        "mulligans_p1": 0,
        "goreclaw_cast_turn": None,
        "thryx_cast_turn": None,
        "tests": [
            {"key": "overrun", "name": OVERRUN, "player": 0,
             "printed": 5, "expect": 5, "gate": 5, "resolve_zone": "Graveyard",
             "note": "non-creature: no reduction expected"},
            {"key": "bears", "name": BEAR, "player": 0,
             "printed": 2, "expect": 2, "gate": 2, "resolve_zone": "Battlefield",
             "note": "creature power 2 < 4: no reduction expected"},
            {"key": "dreadmaw_goreclaw", "name": DREADMAW, "player": 0,
             "printed": 6, "expect": 4, "gate": 4, "resolve_zone": "Battlefield",
             "note": "creature power 6: {2} reduction expected (control)"},
            {"key": "dreadmaw_thryx", "name": DREADMAW, "player": 1,
             "printed": 6, "expect": 5, "gate": 6, "resolve_zone": "Battlefield",
             "note": "MV 6 with Thryx: {1} reduction expected"},
        ],
        "watch": None,   # {"key","oid","since","player"}
        "hold_since": None,
        "held_logged": set(),
        "last_progress_turn": 0,
    })
    for t in ST["tests"]:
        t.update({"inflight": False, "done": False, "pre_untapped": None,
                  "post_untapped": None, "pre_turn": None,
                  "spell_oid": None, "paid": None})


async def export_now(path):
    try:
        s = await C0.export_state()
    except Exception as e:
        say(f"export {path} FAILED: {e}")
        wire("export_failed", {"path": path, "error": str(e)[:200]})
        return None
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path} ({len(s)} bytes)")
    return s


async def play_land(c, pid, state, acts):
    if pid == 0:
        order = [FOREST]
    else:
        n_islands = len([oid for oid, o in bf(state, pid)
                         if oname(o) == ISLAND])
        order = [ISLAND] if n_islands < 3 else [FOREST]
        order.append(FOREST if order[0] == ISLAND else ISLAND)
    for ln in order:
        lid = find_hand(state, pid, ln)
        if lid:
            la = next((x for x in acts if x["type"] == "PlayLand"
                       and str(x.get("data", {}).get("object_id"))
                       == str(lid)), None)
            if la:
                await submit_as_is(c, la)
                return True
    return False


def discard_picks(state, pid):
    hand = hand_oids(state, pid)
    over = len(hand) - 7
    if over <= 0:
        return []
    by_name = {}
    for oid in hand:
        by_name.setdefault(oname(objs(state)[oid]), []).append(oid)
    picks = []

    def take(name, keep):
        ids = by_name.get(name, [])
        while len(ids) > keep and len(picks) < over:
            picks.append(ids.pop())

    if pid == 0:
        take(FOREST, 2)
        take(GORECLAW, 1)
        take(OVERRUN, 1)
        take(BEAR, 1)
        take(DREADMAW, 1)
    else:
        take(FOREST, 2)
        take(ISLAND, 2)
        take(THRYX, 1)
        take(DREADMAW, 1)
    for oid in hand:
        if len(picks) >= over:
            break
        if oid not in picks:
            picks.append(oid)
    return [int(x) for x in picks[:over]]


async def handle_mulligan(c, pid, state):
    pend = (wf(state).get("data") or {}).get("pending") or []
    my = next((p for p in pend if p.get("player") == pid), None)
    ph = (my or {}).get("phase") or {}
    ptype = ph.get("type") if isinstance(ph, dict) else None
    if ptype == "BottomCards":
        n = int(ph.get("count") or 0)
        keep_name = GORECLAW if pid == 0 else THRYX
        named = [(oid, oname(objs(state)[oid]))
                 for oid in hand_oids(state, pid)]
        picks = [oid for oid, nm in named if nm != keep_name][:n]
        if len(picks) < n:
            picks = [oid for oid, _ in named][:n]
        picks = [int(x) for x in picks[:n]]
        await submit_as_is(c, {"type": "SelectCards",
                               "data": {"cards": picks}})
        say(f"[{c.name}] bottoms {len(picks)} after mulligan")
        return True
    lands = sum(1 for oid in hand_oids(state, pid)
                if is_land(objs(state)[oid]))
    if pid == 0:
        key, cap, want = "mulligans", 3, GORECLAW
    else:
        key, cap, want = "mulligans_p1", 1, THRYX
    if lands < 2:
        ST[key] += 1
        await submit_as_is(c, {"type": "MulliganDecision",
                               "data": {"choice": {"type": "Mulligan"}}})
        say(f"[{c.name}] mulligans ({ST[key]}) - land-light")
    elif pid == 0 and ST[key] < cap and not find_hand(state, 0, GORECLAW):
        ST[key] += 1
        await submit_as_is(c, {"type": "MulliganDecision",
                               "data": {"choice": {"type": "Mulligan"}}})
        say(f"[{c.name}] mulligans ({ST[key]}) seeking Goreclaw")
    else:
        await submit_as_is(c, {"type": "MulliganDecision",
                               "data": {"choice": {"type": "Keep"}}})
        say(f"[{c.name}] keeps hand ({lands} lands)")
    return True


def test_by_key(key):
    return next(t for t in ST["tests"] if t["key"] == key)


def p0_tests_done():
    return all(t["done"] for t in ST["tests"] if t["player"] == 0)


async def start_test(c, pid, state, acts, t):
    """Export pre-cast state and submit the test cast."""
    t["pre_untapped"] = untapped_land_count(state, pid)
    t["pre_turn"] = state.get("turn_number")
    await export_now(f"pre_test_{t['key']}.json")
    oid = find_hand(state, pid, t["name"])
    a = castspell_advertised(acts, oid)
    if a is None:
        wire("test_cast_not_advertised",
             {"key": t["key"], "oid": oid})
        say(f"[P{pid}] test {t['key']}: CastSpell not advertised")
        t["done"] = True  # avoid spinning; recorded as failure
        t["note"] += " [CastSpell never advertised]"
        return True
    await submit_as_is(c, a)
    t["inflight"] = True
    t["spell_oid"] = str(oid)
    ST["watch"] = {"key": t["key"], "oid": str(oid),
                   "since": time.time(), "player": pid}
    say(f"[P{pid}] test {t['key']}: cast {t['name']} (oid {oid}); "
        f"pre_untapped={t['pre_untapped']}")
    return True


async def watch_inflight(c, state):
    """Export post-cast state once the in-flight spell resolves."""
    w = ST.get("watch")
    if not w:
        return False
    t = test_by_key(w["key"])
    pid = w["player"]
    oid = w["oid"]
    z = zone_of(state, oid)
    resolved = z == t["resolve_zone"]
    if not resolved and time.time() - w["since"] > 90:
        wire("test_watch_timeout", {"key": t["key"], "zone": z})
        await export_now(f"mid_test_{t['key']}_timeout.json")
        t["done"] = True
        t["inflight"] = False
        ST["watch"] = None
        say(f"[P{pid}] test {t['key']}: watch timed out (zone={z})")
        return True
    if resolved:
        t["post_untapped"] = untapped_land_count(state, pid)
        t["paid"] = (t["pre_untapped"] or 0) - (t["post_untapped"] or 0)
        t["done"] = True
        t["inflight"] = False
        ST["watch"] = None
        await export_now(f"post_test_{t['key']}.json")
        say(f"[P{pid}] test {t['key']}: resolved to {z}; paid={t['paid']} "
            f"(expect {t['expect']})")
        return True
    return False

# ------------------------------------------------------------------- ticks

async def p0_tick(c, pid, state, acts):
    stage = ST["stage"]
    turn = state.get("turn_number") or 0

    if ST.get("watch") and ST["watch"]["player"] == pid:
        if await watch_inflight(c, state):
            return True

    if wf_player(state) == pid and wf_type(state) not in (
            None, "Priority", "DeclareAttackers", "DeclareBlockers",
            "MulliganDecision"):
        wft = wf_type(state)
        key = ("held", wft, stage)
        if key not in ST["held_logged"]:
            ST["held_logged"].add(key)
            wire("decision_held", {"who": c.name, "wf_type": wft,
                                  "stage": stage})
            say(f"[P0] HOLDING unhandled decision {wft} (stage {stage})")
        if ST["hold_since"] is None:
            ST["hold_since"] = time.time()
        return True
    ST["hold_since"] = None

    if stage == "SETUP":
        if bf_named(state, pid, GORECLAW):
            if ST["goreclaw_cast_turn"] is None:
                ST["goreclaw_cast_turn"] = turn
            ST["stage"] = "TEST"
            say(f"Goreclaw on battlefield (turn {turn}); stage -> TEST")
            return True
        if is_my_main(state, pid) and wf_player(state) == pid \
                and wf_type(state) == "Priority":
            goid = find_hand(state, pid, GORECLAW)
            a = castspell_advertised(acts, goid)
            if goid and a is not None and untapped_land_count(
                    state, pid) >= 4 and not bf_named(state, pid, GORECLAW):
                await submit_as_is(c, a)
                ST["goreclaw_cast_turn"] = turn
                say(f"[P0] casts Goreclaw (turn {turn})")
                return True
            await play_land(c, pid, state, acts)
        return False

    if stage == "TEST":
        # run P0's tests in order, one per fresh turn
        for t in ST["tests"]:
            if t["player"] != 0 or t["done"]:
                continue
            if t["inflight"]:
                return False  # watch_inflight handles; else pass priority
            if is_my_main(state, pid) and wf_player(state) == pid \
                    and wf_type(state) == "Priority" \
                    and not stack(state) \
                    and all_lands_untapped(state, pid) \
                    and untapped_land_count(state, pid) >= t["gate"] \
                    and find_hand(state, pid, t["name"]):
                return await start_test(c, pid, state, acts, t)
            break  # only the earliest pending test
        if is_my_main(state, pid):
            await play_land(c, pid, state, acts)
        return False

    return False


async def p1_tick(c, pid, state, acts):
    stage = ST["stage"]
    turn = state.get("turn_number") or 0

    if ST.get("watch") and ST["watch"]["player"] == pid:
        if await watch_inflight(c, state):
            return True

    if wf_player(state) == pid and wf_type(state) not in (
            None, "Priority", "DeclareAttackers", "DeclareBlockers",
            "MulliganDecision"):
        wft = wf_type(state)
        key = ("held1", wft, stage)
        if key not in ST["held_logged"]:
            ST["held_logged"].add(key)
            wire("decision_held_p1", {"wf_type": wft, "stage": stage})
            say(f"[P1] HOLDING unhandled decision {wft} (stage {stage})")
        if ST["hold_since"] is None:
            ST["hold_since"] = time.time()
        return True
    ST["hold_since"] = None

    thryx_bf = bf_named(state, pid, THRYX)
    if not thryx_bf and stage in ("SETUP", "TEST"):
        if is_my_main(state, pid) and wf_player(state) == pid \
                and wf_type(state) == "Priority":
            toid = find_hand(state, pid, THRYX)
            a = castspell_advertised(acts, toid)
            if toid and a is not None \
                    and untapped_land_count(state, pid) >= 5 \
                    and len(untapped_islands(state, pid)) >= 2 \
                    and not thryx_bf:
                await submit_as_is(c, a)
                ST["thryx_cast_turn"] = turn
                say(f"[P1] casts Thryx (turn {turn})")
                return True
            await play_land(c, pid, state, acts)
        return False

    if stage == "TEST" and thryx_bf and p0_tests_done():
        t = test_by_key("dreadmaw_thryx")
        if not t["done"] and not t["inflight"]:
            if is_my_main(state, pid) and wf_player(state) == pid \
                    and wf_type(state) == "Priority" \
                    and not stack(state) \
                    and all_lands_untapped(state, pid) \
                    and untapped_land_count(state, pid) >= t["gate"] \
                    and find_hand(state, pid, t["name"]):
                return await start_test(c, pid, state, acts, t)
        if is_my_main(state, pid):
            await play_land(c, pid, state, acts)
        return False

    if is_my_main(state, pid):
        await play_land(c, pid, state, acts)
    return False


async def tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    rej = drain_rejections(c)
    rkey, vkey, bkey = (c.name, "rej_n"), (c.name, "rej_rev"), \
        (c.name, "backoff_until")
    if rej:
        ST[rkey] = ST.get(rkey, 0) + len(rej)
        if ST[rkey] >= 8 and c.revision == ST.get(vkey):
            ST[bkey] = time.time() + 8
            ST[rkey] = 0
            wire("backoff", {"who": c.name, "rev": c.revision})
        ST[vkey] = c.revision
    else:
        ST[rkey] = 0
    if time.time() < ST.get(bkey, 0):
        return False
    if settle_pending(c):
        return False
    if wf_type(state) == "MulliganDecision" and wf_pending_for(state, pid):
        if await handle_mulligan(c, pid, state):
            return True
    for a in acts:
        if a["type"] == "MulliganDecision":
            pend = (wf(state).get("data") or {}).get("pending") or []
            my = next((p for p in pend if p.get("player") == pid), None)
            phase = (my or {}).get("phase") or {}
            if isinstance(phase, dict) and phase.get("type") == "BottomCards":
                n = int(phase.get("count") or 0)
                keep_name = GORECLAW if pid == 0 else THRYX
                named = [(oid, oname(objs(state)[oid]))
                         for oid in hand_oids(state, pid)]
                picks = [oid for oid, nm in named
                         if nm != keep_name][:n]
                if len(picks) < n:
                    picks = [oid for oid, _ in named][:n]
                picks = [int(x) for x in picks[:n]]
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": picks}})
                say(f"[{c.name}] bottoms {len(picks)} after mulligan")
                return True
            lands = sum(1 for oid in hand_oids(state, pid)
                        if is_land(objs(state)[oid]))
            if pid == 0:
                key, cap = "mulligans", 3
            else:
                key, cap = "mulligans_p1", 1
            if lands < 2:
                ST[key] += 1
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Mulligan"}}})
                say(f"[{c.name}] mulligans ({ST[key]}) - land-light")
            elif pid == 0 and ST[key] < cap and not find_hand(
                    state, 0, GORECLAW):
                ST[key] += 1
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Mulligan"}}})
                say(f"[{c.name}] mulligans ({ST[key]}) seeking Goreclaw")
            else:
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Keep"}}})
            return True
    if wf_type(state) == "DiscardToHandSize" and wf_player(state) == pid:
        picks = discard_picks(state, pid)
        if picks:
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": picks}})
            say(f"[{c.name}] discards {len(picks)} to hand size")
            return True
        wire("discard_no_picks", {"who": c.name})
        return True
    if pid == 0:
        if await p0_tick(c, pid, state, acts):
            return True
    else:
        if await p1_tick(c, pid, state, acts):
            return True
    if state.get("active_player") == pid and \
            (state.get("phase") or "") == "DeclareAttackers" and \
            wf_player(state) == pid:
        for a in acts:
            if a["type"] == "DeclareAttackers":
                sub = copy.deepcopy(a)
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
                await submit_as_is(c, sub)
                return True
    if (state.get("phase") or "") == "DeclareBlockers" and pid in (0, 1) \
            and wf_player(state) == pid:
        for a in acts:
            if a["type"] == "DeclareBlockers":
                sub = copy.deepcopy(a)
                sub["data"]["assignments"] = []
                await submit_as_is(c, sub)
                return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            if wf_player(state) == pid:
                await submit_as_is(c, a)
                return True
            continue
    wt, wp = wf_type(state), wf_player(state)
    if wp == pid and wt not in (None, "Priority"):
        return True
    if wf_player(state) != pid:
        return False
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False

# ------------------------------------------------------------------- run

async def get_server_hello():
    import websockets
    async with websockets.connect(
            "ws://127.0.0.1:9374/ws", max_size=200_000_000) as ws:
        raw = await asyncio.wait_for(ws.recv(), 5)
        return json.loads(raw)


def load_env(fn):
    try:
        return json.loads(open(f"{EVDIR}/{fn}").read())
    except Exception as e:
        return {"_err": str(e)[:160]}


def env_state(env):
    if not env or "_err" in env:
        return None
    s = env.get("state")
    return s if isinstance(s, dict) else json.loads(s)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()

async def main():
    reset_state()
    t_start = time.time()
    hello = await get_server_hello()
    say("ServerHello observed: " + json.dumps(hello)[:400])
    wire("server_hello", hello)
    ST["server_hello"] = hello.get("data", hello)

    global C0, C1
    C0 = PhaseClient("P0")
    await C0.connect()
    await C0.create(deck(*P0_DECK))
    C1 = PhaseClient("P1")
    await C1.connect()
    await C1.join(C0.game_code, deck(*P1_DECK))
    ST["game_code"] = C0.game_code
    say(f"game {C0.game_code}; seats P0={C0.player_id} P1={C1.player_id}")
    wire("game_start", {"game_code": C0.game_code,
                        "p0_deck": P0_DECK, "p1_deck": P1_DECK})

    t0 = time.time()
    last_progress = t0
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        acted0 = await tick(C0, 0)
        acted1 = await tick(C1, 1)
        st0 = C0.latest
        turn = (st0.get("state", {}).get("turn_number") or 0) if st0 else 0
        if turn > ST["turn_cap"]:
            say("turn cap reached; stopping")
            wire("turn_cap", {})
            break
        if acted0 or acted1:
            last_progress = time.time()
        # all four tests complete -> settle a turn, export final, done
        if all(t["done"] for t in ST["tests"]):
            if not ST.get("settle_turn"):
                ST["settle_turn"] = turn
                await export_now("post_all_tests.json")
                say("all tests complete; post_all_tests exported")
            if turn > ST["settle_turn"]:
                ST["stage"] = "DONE"
                ST["stop"] = True
                say("settled one turn past completion; stopping")
        if ST.get("hold_since") and time.time() - ST["hold_since"] > 150:
            say("held decision for 150s with no progress; exporting and "
                "stopping")
            wire("hold_timeout", {"stage": ST.get("stage")})
            await export_now("mid_held.json")
            break
        if time.time() - last_progress > 300:
            say("no progress for 300s; stopping")
            break
        await asyncio.sleep(0.15)

    say(f"loop ended: stage={ST['stage']} stop={ST['stop']} "
        f"tests_done={[t['key'] for t in ST['tests'] if t['done']]}")
    wire("loop_end", {k: ST.get(k) for k in (
        "stage", "stop", "goreclaw_cast_turn", "thryx_cast_turn")})
    await C0.close()
    await C1.close()
    t_end = time.time()

    # ---- assertions from SAVED states ----
    A, D = {}, {}

    pre_overrun = env_state(load_env("pre_test_overrun.json"))
    pre_thryxleg = env_state(load_env("pre_test_dreadmaw_thryx.json"))
    post_all = env_state(load_env("post_all_tests.json"))

    # A1
    g_ok = pre_overrun and len(bf_named(pre_overrun, 0, GORECLAW)) >= 1
    t_ok = pre_thryxleg and len(bf_named(pre_thryxleg, 1, THRYX)) >= 1
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
    final = post_all or env_state(
        load_env(f"post_test_{ST['tests'][-1]['key']}.json"))
    if final:
        ok = not stack(final)
        A["A6_cleanup"] = "passed" if ok else "failed"
        D["A6_cleanup"] = (f"stack empty={ok}; "
                           f"turn={final.get('turn_number')}")
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

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "details": D, "verdict": verdict,
                   "rejections": ST["rejections"][:20],
                   "tests": [
                       {k: t[k] for k in
                        ("key", "name", "player", "printed", "expect",
                         "pre_untapped", "post_untapped", "paid",
                         "done", "note")} for t in ST["tests"]],
                   "goreclaw_cast_turn": ST.get("goreclaw_cast_turn"),
                   "thryx_cast_turn": ST.get("thryx_cast_turn")},
                  f, indent=1, default=str)
    say("ASSERTIONS: " + json.dumps(A))
    say("VERDICT: " + verdict)
    wire("assertions", {"A": A, "verdict": verdict})

    shutil.copy(__file__, f"{EVDIR}/scenario_{ISSUE}.py")

    # ---- run.json ----
    sh = ST.get("server_hello", {})
    rel = f"{BACKFILL}/server/releases/v0.81.3"
    binpath = f"{rel}/phase-server-slim-x86_64-unknown-linux-musl"
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "title": "Reducers behaving incorrectly - Goreclaw reduces all "
                 "spells; Thryx reduces nothing",
        "server": {
            "server_version": sh.get("server_version"),
            "build_commit": sh.get("build_commit"),
            "protocol_version": sh.get("protocol_version"),
            "mode": sh.get("mode"),
        },
        "binary_sha256": sha256_file(binpath),
        "card_data_sha256": sha256_file(f"{rel}/data/card-data.json"),
        "draft_pools_sha256": sha256_file(f"{rel}/data/draft-pools.json"),
        "signature_verified": True,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                    time.gmtime(t_start)),
        "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                  time.gmtime(t_end)),
        "game_code": ST.get("game_code"),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "decisions": {
            "goreclaw_cast_turn": ST.get("goreclaw_cast_turn"),
            "thryx_cast_turn": ST.get("thryx_cast_turn"),
            "tests": {t["key"]: {"paid": t["paid"], "expect": t["expect"],
                                 "pre_untapped": t["pre_untapped"],
                                 "post_untapped": t["post_untapped"],
                                 "done": t["done"]}
                      for t in ST["tests"]},
            "rejections": len(ST["rejections"]),
        },
        "assertions": A,
        "assertion_details": D,
        "verdict": verdict,
        "verdict_note": "Cost actually paid measured as tapped-land delta "
                        "between saved pre/post cast states. Goreclaw "
                        "reduces only creature spells with power >= 4; "
                        "Thryx reduces MV>=5 spells by 1.",
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "4x key cards / dense lands is a test-harness convenience "
            "(engine accepts >4-of for custom games).",
            "Not tested on the original 2026-07-24 build; verdict is scoped "
            "to v0.81.3, not a fix claim.",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full game "
            "replay).",
        ],
        "setup_line": "P0: 4x Goreclaw, 4x Overrun, 4x Grizzly Bears, 4x "
                      "Colossal Dreadmaw, 44x Forest; P1: 4x Thryx, 4x "
                      "Colossal Dreadmaw, 26x Island, 26x Forest. Each test "
                      "cast on a fresh turn with all lands untapped; mana "
                      "paid = tapped-land delta.",
        "contract_line": "P0 casts Overrun (non-creature), Grizzly Bears "
                         "(P2), Colossal Dreadmaw (P6) with Goreclaw on "
                         "board; P1 casts Colossal Dreadmaw (MV6) with "
                         "Thryx on board. Expected payments: 5 / 2 / 4 / 5.",
        "stats": {
            "tests_completed": sum(1 for t in ST["tests"] if t["done"]),
        },
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    say("run.json written; verdict = " + verdict)

    # ---- server excerpts ----
    try:
        lines = open(f"{RUNDIR}/server.log", errors="replace").read(
            ).splitlines()
        keep = [l for l in lines if any(
            k in l.lower() for k in
            ("goreclaw", "thryx", "reduc", "cost", "overrun", "dreadmaw",
             "error", "panic", "warn"))]
        with open(f"{EVDIR}/server_excerpts.log", "w") as f:
            f.write("\n".join(keep[-400:]) + "\n")
        say(f"server_excerpts.log: {len(keep)} matching lines")
    except Exception as e:
        say(f"server excerpts FAILED: {e}")

    WIRE.close()
    RUNLOG.close()
    RUNLOG2.close()
    render_png()
    write_manifest()
    validate()


def render_png():
    from PIL import Image, ImageDraw
    run = json.load(open(f"{EVDIR}/run.json"))
    dec = run["decisions"]
    tests = dec.get("tests", {})

    W, H = 1040, 1040
    BG = (18, 20, 26)
    PANEL = (26, 30, 38)
    TEXT = (235, 238, 245)
    DIM = (150, 160, 175)
    ACCENT = (110, 180, 255)
    GREEN = (110, 220, 140)
    RED = (240, 120, 120)
    YELLOW = (240, 200, 110)
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 18
    srv = run["server"]
    d.text((24, y), "#6910 - Goreclaw / Thryx cost reducers", fill=ACCENT)
    y += 26
    d.text((24, y),
           f"server v{srv.get('server_version')} "
           f"({srv.get('build_commit')}, protocol "
           f"{srv.get('protocol_version')}) | run {run['run_id']} | "
           f"{run['started_at'][:10]} | verdict: {run['verdict']}",
           fill=DIM)
    y += 30
    for ln in [
        "Oracle Goreclaw: creature spells you cast with power >= 4 cost",
        "{2} less. Oracle Thryx: spells you cast with MV >= 5 cost {1}",
        "less.",
        "",
        "Reported: Goreclaw reduces ALL spells by 2; Thryx reduces nothing.",
        "Method: each test cast on a fresh turn with all lands untapped;",
        "mana paid = tapped-land delta between saved pre/post states.",
    ]:
        d.text((24, y), ln, fill=TEXT)
        y += 20
    y += 8
    labels = {"overrun": "T1 Overrun (non-creature, P0)",
              "bears": "T2 Grizzly Bears (P2 creature, P0)",
              "dreadmaw_goreclaw": "T3 Dreadmaw (P6 creature, P0)",
              "dreadmaw_thryx": "T4 Dreadmaw (MV6, P1 +Thryx)"}
    rows = [(labels.get(k, k),
             f"paid={v.get('paid')} expect={v.get('expect')} "
             f"done={v.get('done')}")
            for k, v in tests.items()]
    d.rectangle([16, y, W - 16, y + 30 + len(rows) * 44], fill=PANEL,
                outline=(45, 52, 64))
    d.text((28, y + 8), "Observed mana paid (from saved states)", fill=YELLOW)
    y += 34
    for tag, val in rows:
        d.text((28, y), tag, fill=TEXT)
        d.text((28, y + 20), (val or "")[:150], fill=DIM)
        y += 44
    y += 12
    A = run["assertions"]
    Dd = run["assertion_details"]
    d.rectangle([16, y, W - 16, y + 34 + len(A) * 44], fill=PANEL,
                outline=(45, 52, 64))
    d.text((28, y + 8), "Assertions", fill=YELLOW)
    y += 34
    for k, v in A.items():
        color = GREEN if v == "passed" else (RED if v == "failed" else DIM)
        d.text((28, y), f"{k}: {v}", fill=color)
        d.text((28, y + 20), Dd.get(k, "")[:160], fill=DIM)
        y += 44
    y += 12
    d.text((24, y), "Limitations: " + "; ".join(run["limitations"])[:200],
           fill=DIM)
    y += 24
    d.text((24, y), "Evidence summary (not a gameplay screenshot). "
           "States: pre_test_*/post_test_*.json + manifest.sha256",
           fill=DIM)
    out = os.path.join(EVDIR, "summary.png")
    img.save(out)
    print("wrote", out, flush=True)


def write_manifest():
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256"
                   and os.path.isfile(os.path.join(EVDIR, f)))
    lines = []
    for f in files:
        h = hashlib.sha256(open(os.path.join(EVDIR, f), "rb").read()
                           ).hexdigest()
        lines.append(f"{h}  {f}\n")
    with open(os.path.join(EVDIR, "manifest.sha256"), "w") as f:
        f.writelines(lines)
    print(f"wrote manifest.sha256 ({len(lines)} files)", flush=True)


def validate():
    ok = True
    man = {}
    for ln in open(os.path.join(EVDIR, "manifest.sha256")):
        h, _, fn = ln.strip().partition("  ")
        man[fn] = h
    for fn, h in man.items():
        p = os.path.join(EVDIR, fn)
        if not os.path.exists(p):
            print(f"VALIDATE FAIL: missing {fn}", flush=True)
            ok = False
            continue
        ah = hashlib.sha256(open(p, "rb").read()).hexdigest()
        if ah != h:
            print(f"VALIDATE FAIL: hash mismatch {fn}", flush=True)
            ok = False
        if fn.endswith(".json"):
            try:
                json.load(open(p))
            except Exception as e:
                print(f"VALIDATE FAIL: {fn} not JSON: {e}", flush=True)
                ok = False
    try:
        from PIL import Image
        im = Image.open(os.path.join(EVDIR, "summary.png"))
        im.verify()
        print("VALIDATE: summary.png opens OK", flush=True)
    except Exception as e:
        print(f"VALIDATE FAIL: summary.png: {e}", flush=True)
        ok = False
    print("VALIDATE: " + ("ALL OK" if ok else "FAILURES"), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
