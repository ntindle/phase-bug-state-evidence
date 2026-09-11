#!/usr/bin/env python3
"""Issue #6774: Tifa Lockhart landfall doubles power 2-3x per single landfall.

Reported (Discord, synced to GitHub): "Just played a game with a tifa deck
and her power is multiplying 2-3x more than it should with a single landfall."

Oracle (pinned v0.79.0 card-data.json):
  Tifa Lockhart ({1}{G}, 1/2, Legendary Creature - Human Monk):
    "Trample
     Landfall -- Whenever a land you control enters, double Tifa Lockhart's
     power until end of turn."
Parsed trigger: mode ChangesZone (You-controlled Land -> Battlefield),
execute Spell DoublePT { mode: Power, target: SelfRef, factor: 2 },
duration UntilEndOfTurn, optional false, batched false.

Behavioral contract (single game, two human-client seats, v0.79.0/proto 69):
  RAMP   - P0 drops one land per own main phase, keeps mulligan, casts
           Tifa Lockhart when affordable (turn 2) and none is on BF.
  TEST   - On P0's next turn after the cast (turn 4): export PRE at main
           phase start (Tifa on BF, power 1/2, land not yet played), then
           play exactly ONE Forest.
  MID    - export at the first revision where a Tifa-sourced landfall
           trigger sits on the stack; count Tifa-trigger stack entries.
  POST   - export once the stack empties on the same turn; read Tifa's
           power/toughness.
  CLEAN  - on P0's following turn (turn 6), before the land drop, observe
           Tifa's power back at 1 (UntilEndOfTurn expired).

  A1 setup_ok        pre: Tifa on P0 BF with power==1 and toughness==2;
                     a land in P0 hand; no landfall since she entered.
  A2 single_landfall  exactly one P0-controlled land entered between pre
                     and post (Forest BF count +1); a Tifa-sourced
                     landfall trigger was seen on the stack.
  A3 power_once       post: Tifa power == 2 (doubled exactly once) and
                     toughness == 2. FAILED (reproduced) iff 4 or 8.
  A4 trigger_count    mid: number of Tifa-sourced DoublePT trigger entries
                     on the stack (1 expected; >1 points at duplicate
                     triggers, 1+wrong power points at repeated apply).
  A5 temp_expires     turn 6 (before P0's land drop): Tifa power back to 1.

Verdict = blocked iff A1 fails (setup never assembled).
Verdict = reproduced iff A1 passes and A3 fails (power 4 or 8).
Verdict = not-reproduced iff A1..A5 all pass.
"""
import asyncio
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client as client_mod  # noqa: E402
from client import PhaseClient, deck  # noqa: E402

client_mod.URL = "ws://127.0.0.1:9374/ws"

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260911-6774c"
EVID_ISSUE = "6774"
EVDIR = f"{BACKFILL}/evidence/{EVID_ISSUE}/{RUN_ID}"
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
        WIRE.write(json.dumps({"t": time.time(), "event": event + "_unserializable",
                               "error": str(e)}) + "\n")
    WIRE.flush()


TIFA = "Tifa Lockhart"
FOREST = "Forest"
ISLAND = "Island"

P0_DECK = deck((TIFA, 20), (FOREST, 40))
P1_DECK = deck((ISLAND, 60))

ST = {"tifa_cast_turn": None, "land_turn": -1, "pre_exported": False,
      "test_land_played": False, "mid_exported": False,
      "mid_turn": None, "post_exported": False, "post_power": None,
      "cleanup_observed": False, "cleanup_power": None,
      "trigger_seen": False, "stop": False,
      "mid_trigger_count": None, "tifa_oid": None}
WF_SEEN = []
SUBMITTED_IID = set()


def lname(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def objs(state):
    return state.get("objects", {}) or {}


def hand(state, pid):
    return [oid for oid, o in objs(state).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def bf_named(state, pid, name):
    return [oid for oid, o in objs(state).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and lname(o) == name.lower()]


def count_bf_named(state, pid, name):
    return len(bf_named(state, pid, name))


def find_hand(state, pid, name):
    for oid in hand(state, pid):
        if lname(objs(state)[oid]) == name.lower():
            return oid
    return None


def find_action(acts, atype):
    for a in acts or []:
        if a.get("type") == atype:
            return a


def power_of(obj):
    for k in ("power", "current_power"):
        v = obj.get(k)
        if isinstance(v, int):
            return v
        if isinstance(v, dict):
            v2 = v.get("value")
            if isinstance(v2, int):
                return v2
    return None


def toughness_of(obj):
    for k in ("toughness", "current_toughness"):
        v = obj.get(k)
        if isinstance(v, int):
            return v
        if isinstance(v, dict):
            v2 = v.get("value")
            if isinstance(v2, int):
                return v2
    return None


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and state.get("priority_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain"))


def vi_opps(st):
    return ((st.get("viewer_interaction") or {}).get("opportunities", [])) or []


def record_wf(state):
    wf = (state.get("waiting_for") or {}).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf,
                             "data": (state.get("waiting_for") or {}).get("data")})


C0 = None


async def do_export(path):
    s = await C0.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


def stack_entries(state):
    return state.get("stack") or []


def _ability_description(entry):
    try:
        return entry["kind"]["data"]["ability"]["description"] or ""
    except (KeyError, TypeError):
        return ""


def is_tifa_trigger(entry, tifa_oid):
    """True iff this stack entry is Tifa Lockhart's landfall power-doubling
    trigger. Discriminated on kind.type == TriggeredAbility + the trigger's
    own ability description mentioning 'double', and (when known) source_id
    matching Tifa's object id."""
    try:
        if entry["kind"]["type"] != "TriggeredAbility":
            return False
    except (KeyError, TypeError):
        return False
    desc = _ability_description(entry).lower()
    if "double" not in desc:
        return False
    if tifa_oid is not None:
        try:
            sid = entry.get("source_id")
            if sid is not None and str(sid) != str(tifa_oid):
                return False
        except (KeyError, TypeError):
            pass
    return True


# ------------------------------------------------------------- tick (P0)

async def tick_p0(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    wf = state.get("waiting_for") or {}
    wtype = wf.get("type")
    wplayer = (wf.get("data") or {}).get("player")

    for a in acts:
        if a["type"] == "MulliganDecision":
            wire("action_submit", {"who": "P0", "action": "MulliganDecision/Keep"})
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            say("[P0] keeps")
            return True

    if wtype == "DiscardToHandSize" and wplayer == 0:
        n = (wf.get("data") or {}).get("count") \
            or max(0, len(hand(state, pid)) - 7)
        oids = hand(state, pid)

        def rank(oid):
            return 0 if lname(objs(state)[oid]) == TIFA.lower() else 1
        # keep one Tifa; dump extras first
        seen = set()
        ranked = []
        for oid in sorted(oids, key=rank):
            nm = lname(objs(state)[oid])
            if nm == TIFA.lower() and nm not in seen:
                seen.add(nm)
                continue
            ranked.append(oid)
        picks = ranked[:n]
        if picks:
            wire("action_submit", {"who": "P0", "action": "SelectCards/discard",
                                  "picks": picks})
            await c.send_action({"type": "SelectCards",
                                 "data": {"cards": [int(x) for x in picks]}})
            say(f"[P0] discards {len(picks)} to hand size")
            return True
        return False

    if wtype == "ChooseLegend" and wplayer == 0:
        ca = find_action(acts, "ChooseLegend")
        if ca:
            wire("action_submit", {"who": "P0", "action": "ChooseLegend/asis"})
            await c.send_action(ca)
            say("[P0] legend rule: keeps first")
            return True
        return False

    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            wire("action_submit", {"who": "P0", "action": a["type"]})
            await c.send_action(a)
            return True

    if wtype == "OrderTriggers" and wplayer == 0:
        oa = find_action(acts, "OrderTriggers")
        if oa:
            wire("action_submit", {"who": "P0", "action": "OrderTriggers/asis"})
            await c.send_action(oa)
            return True

    # never pass priority while a P0 decision is pending
    if wtype in ("OptionalCostChoice", "OptionalEffectChoice", "TargetSelection",
                 "ManaPayment", "ChooseXValue", "SurveilChoice",
                 "ScryChoice", "DiscardChoice") and wplayer == 0:
        wire("hold_priority", {"wtype": wtype})
        return False

    # hold the cleanup window: on P0's post-test turns, keep priority (and
    # crucially play NO land) so the main loop observes the expired buff on
    # the first main-phase revision. Must run BEFORE the land-drop block,
    # or the cleanup turn's land poisons the turn != land_turn gate.
    if ST["post_exported"] and not ST["cleanup_observed"] \
            and ST["mid_turn"] is not None \
            and state.get("active_player") == 0 \
            and (state.get("turn_number") or 0) > ST["mid_turn"] \
            and is_my_main(state, pid):
        tifa = bf_named(state, 0, TIFA)
        if tifa:
            ST["cleanup_observed"] = True
            ST["cleanup_power"] = power_of(objs(state)[tifa[0]])
            ST["cleanup_turn"] = state.get("turn_number")
            wire("cleanup_observed",
                 {"turn": ST["cleanup_turn"],
                  "phase": state.get("phase"),
                  "tifa_power": ST["cleanup_power"]})
            say(f"[cleanup] turn {ST['cleanup_turn']}: Tifa power="
                f"{ST['cleanup_power']}")
            ST["stop"] = True
            return False
        wire("hold_priority", {"why": "cleanup_window",
                               "turn": state.get("turn_number"),
                               "phase": state.get("phase")})
        return False

    da = find_action(acts, "DeclareAttackers")
    if da and state.get("active_player") == 0:
        import copy
        sub = copy.deepcopy(da)
        sub["data"]["attacks"] = []
        sub["data"]["bands"] = []
        wire("action_submit", {"who": "P0", "action": "DeclareAttackers/empty"})
        await c.send_action({"type": "DeclareAttackers", "data": sub["data"]})
        return True

    db = find_action(acts, "DeclareBlockers")
    if db:
        import copy
        sub = copy.deepcopy(db)
        sub["data"]["assignments"] = []
        wire("action_submit", {"who": "P0", "action": "DeclareBlockers/empty"})
        await c.send_action({"type": "DeclareBlockers", "data": sub["data"]})
        return True

    turn = state.get("turn_number")
    tifa_bf = bf_named(state, 0, TIFA)
    test_turn = (ST["tifa_cast_turn"] + 2) if ST["tifa_cast_turn"] is not None else None

    if is_my_main(state, pid):
        # land drop: once per own main phase, but NOT on the test turn
        # after the single test land has been played
        skip_land = ST["test_land_played"] and turn == test_turn
        if turn != ST["land_turn"] and not skip_land:
            hid = find_hand(state, pid, FOREST)
            for a in acts:
                if a["type"] == "PlayLand" and hid \
                        and str(a.get("data", {}).get("object_id")) == str(hid):
                    ST["land_turn"] = turn
                    # arm the test export: pre must be taken BEFORE this land
                    if turn == test_turn and not ST["pre_exported"]:
                        pre = await do_export("pre_landfall.json")
                        ST["pre_exported"] = True
                        tifa0 = bf_named(pre, 0, TIFA)
                        ST["tifa_oid"] = str(tifa0[0]) if tifa0 else None
                        ST["pre_forests"] = count_bf_named(pre, 0, FOREST)
                        wire("pre_landfall", {
                            "turn": pre.get("turn_number"),
                            "phase": pre.get("phase"),
                            "tifa_oid": ST["tifa_oid"],
                            "tifa_power": power_of(objs(pre)[tifa0[0]]) if tifa0 else None,
                            "tifa_toughness": toughness_of(objs(pre)[tifa0[0]]) if tifa0 else None,
                            "forests_bf": ST["pre_forests"],
                        })
                        say(f"[pre] exported turn {pre.get('turn_number')} "
                            f"phase {pre.get('phase')} "
                            f"tifa power={power_of(objs(pre)[tifa0[0]]) if tifa0 else '?'}")
                    wire("action_submit", {"who": "P0", "action": "PlayLand/Forest",
                                          "test_turn": turn == test_turn})
                    await c.send_action(a)
                    if turn == test_turn:
                        ST["test_land_played"] = True
                        say("[P0] plays the single TEST land (turn "
                            f"{turn})")
                    return True
        # cast Tifa if affordable and none on BF
        if not tifa_bf:
            oid = find_hand(state, pid, TIFA)
            for a in acts:
                if a["type"] == "CastSpell" and oid \
                        and str(a.get("data", {}).get("object_id")) == str(oid):
                    wire("action_submit", {"who": "P0", "action": "CastSpell/Tifa",
                                          "object_id": oid})
                    await c.send_action(a)
                    say("[P0] casts Tifa Lockhart")
                    return True
        elif ST["tifa_cast_turn"] is None:
            ST["tifa_cast_turn"] = turn
            wire("tifa_on_bf", {"turn": turn,
                               "oid": str(tifa_bf[0]),
                               "obj": objs(state)[tifa_bf[0]]})
            say(f"[P0] Tifa on battlefield (turn {turn})")

    for a in acts:
        if a["type"] == "PassPriority":
            wire("action_submit", {"who": "P0", "action": "PassPriority"})
            await c.send_action(a)
            return True
    return False


# ------------------------------------------------------------- tick (P1)

async def tick_p1(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    wf = state.get("waiting_for") or {}
    wtype = wf.get("type")
    wplayer = (wf.get("data") or {}).get("player")

    for a in acts:
        if a["type"] == "MulliganDecision":
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            return True
    if wtype == "DiscardToHandSize" and wplayer == 1:
        n = (wf.get("data") or {}).get("count") \
            or max(0, len(hand(state, pid)) - 7)
        oids = hand(state, pid)
        picks = oids[:n]
        if picks:
            await c.send_action({"type": "SelectCards",
                                 "data": {"cards": [int(x) for x in picks]}})
            return True
        return False
    if wtype == "OrderTriggers" and wplayer == 1:
        oa = find_action(acts, "OrderTriggers")
        if oa:
            await c.send_action(oa)
            return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    da = find_action(acts, "DeclareAttackers")
    if da:
        import copy
        sub = copy.deepcopy(da)
        sub["data"]["attacks"] = []
        sub["data"]["bands"] = []
        await c.send_action({"type": "DeclareAttackers", "data": sub["data"]})
        return True
    db = find_action(acts, "DeclareBlockers")
    if db:
        import copy
        sub = copy.deepcopy(db)
        sub["data"]["assignments"] = []
        await c.send_action({"type": "DeclareBlockers", "data": sub["data"]})
        return True
    if is_my_main(state, pid):
        if state.get("turn_number") != ST.get("land_turn_p1"):
            hid = find_hand(state, pid, ISLAND)
            for a in acts:
                if a["type"] == "PlayLand" and hid \
                        and str(a.get("data", {}).get("object_id")) == str(hid):
                    ST["land_turn_p1"] = state.get("turn_number")
                    await c.send_action(a)
                    return True
    if wtype not in ("Priority", None) and wplayer == 1:
        wire("p1_hold", {"wtype": wtype})
        return False
    for a in acts:
        if a["type"] == "PassPriority":
            await c.send_action(a)
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
    try:
        await p0.create(P0_DECK)
    except Exception as e:
        say(f"game creation failed: {e}")
        A["A1_setup_ok"] = "blocked"
        obs["notes"].append(f"deck/game creation failed: {e}")
        with open(f"{EVDIR}/assertions.json", "w") as f:
            json.dump({"assertions": A, "notes": obs["notes"],
                       "wf_sequence": WF_SEEN}, f, indent=2)
        await p0.close()
        return obs
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, P1_DECK)
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    wire("game", {"code": p0.game_code, "p0": p0.player_id, "p1": p1.player_id})

    global C0
    C0 = p0

    last_rev = {}
    TIMEOUT = 1500
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        for c, pid, tick in ((p0, p0.player_id, tick_p0),
                             (p1, p1.player_id, tick_p1)):
            if c.revision == last_rev.get(c.name):
                continue
            try:
                if await tick(c, pid):
                    last_rev[c.name] = c.revision
            except Exception as e:
                say(f"tick error {c.name}: {e}")
        st = p0.latest
        if not st:
            continue
        state = st["state"]
        record_wf(state)

        turn = state.get("turn_number")
        phase = state.get("phase") or ""
        test_turn = (ST["tifa_cast_turn"] + 2) \
            if ST["tifa_cast_turn"] is not None else None

        # stack audit during the test window
        if ST["pre_exported"] and not ST["post_exported"]:
            stack = stack_entries(state)
            n_tifa = sum(1 for e in stack
                         if is_tifa_trigger(e, ST["tifa_oid"]))
            if stack or n_tifa:
                sig = json.dumps(
                    [(e.get("id"), _ability_description(e)[:60]) for e in stack],
                    default=str)
                if sig != ST.get("last_stack_sig"):
                    ST["last_stack_sig"] = sig
                    wire("stack_snapshot", {"turn": turn, "phase": phase,
                                            "tifa_triggers": n_tifa,
                                            "stack_size": len(stack)})
            if n_tifa >= 1:
                ST["trigger_seen"] = True
            # MID: first revision with a Tifa landfall trigger on the stack
            if ST["trigger_seen"] and not ST["mid_exported"] and n_tifa >= 1:
                mid = await do_export("mid_trigger.json")
                ST["mid_exported"] = True
                ST["mid_turn"] = mid.get("turn_number")
                ST["mid_trigger_count"] = sum(
                    1 for e in stack_entries(mid)
                    if is_tifa_trigger(e, ST["tifa_oid"]))
                wire("mid_trigger", {"turn": ST["mid_turn"],
                                    "tifa_triggers": ST["mid_trigger_count"],
                                    "stack_size": len(stack_entries(mid))})
                say(f"[mid] exported: {ST['mid_trigger_count']} Tifa "
                    f"triggers on stack")

        # POST: stack empty on the test turn after the trigger resolved
        if ST["mid_exported"] and not ST["post_exported"]:
            if not stack_entries(state) and turn == ST["mid_turn"]:
                await asyncio.sleep(1.0)
                post = await do_export("post_landfall.json")
                ST["post_exported"] = True
                tifa = bf_named(post, 0, TIFA)
                if tifa:
                    ST["post_power"] = power_of(objs(post)[tifa[0]])
                    ST["post_toughness"] = toughness_of(objs(post)[tifa[0]])
                    ST["post_forests"] = count_bf_named(post, 0, FOREST)
                    wire("post_landfall", {"turn": post.get("turn_number"),
                                           "phase": post.get("phase"),
                                           "tifa_power": ST["post_power"],
                                           "tifa_toughness": ST["post_toughness"],
                                           "forests_bf": ST["post_forests"]})
                    say(f"[post] exported: Tifa power={ST['post_power']} "
                        f"toughness={ST['post_toughness']}")
                else:
                    wire("post_landfall", {"error": "Tifa not on BF in post"})

        # CLEANUP: P0's following turn, main phase, before P0's land drop
        if ST["post_exported"] and not ST["cleanup_observed"]:
            if state.get("active_player") == 0 and turn is not None \
                    and ST["mid_turn"] is not None and turn > ST["mid_turn"] \
                    and phase in ("PreCombatMain",) \
                    and state.get("priority_player") == 0 \
                    and turn != ST["land_turn"]:
                tifa = bf_named(state, 0, TIFA)
                if tifa:
                    ST["cleanup_observed"] = True
                    ST["cleanup_power"] = power_of(objs(state)[tifa[0]])
                    ST["cleanup_turn"] = turn
                    wire("cleanup_observed",
                         {"turn": turn, "phase": phase,
                          "tifa_power": ST["cleanup_power"]})
                    say(f"[cleanup] turn {turn}: Tifa power="
                        f"{ST['cleanup_power']}")
                    ST["stop"] = True

    # ------------------------------------------------------- evaluate
    def load_env(p):
        with open(f"{EVDIR}/{p}") as f:
            return json.load(f)

    def env_state(p):
        try:
            return load_env(p)["state"]
        except FileNotFoundError:
            return None

    pre = env_state("pre_landfall.json")
    mid = env_state("mid_trigger.json")
    post = env_state("post_landfall.json")

    # A1
    if pre is None:
        A["A1_setup_ok"] = "failed"
        obs["notes"].append("pre_landfall.json never exported")
    else:
        tifa = bf_named(pre, 0, TIFA)
        p = power_of(objs(pre)[tifa[0]]) if tifa else None
        t = toughness_of(objs(pre)[tifa[0]]) if tifa else None
        has_land = find_hand(pre, 0, FOREST) is not None
        obs["notes"].append(
            f"pre_landfall: turn={pre.get('turn_number')} "
            f"phase={pre.get('phase')} tifa_bf={bool(tifa)} "
            f"power={p} toughness={t} forest_in_hand={has_land}")
        A["A1_setup_ok"] = "passed" if (tifa and p == 1 and t == 2
                                       and has_land) else "failed"

    # A2
    if pre is None or post is None:
        A["A2_single_landfall"] = "not-run"
        obs["notes"].append("pre or post missing; A2 not-run")
    else:
        d_forest = count_bf_named(post, 0, FOREST) - count_bf_named(pre, 0, FOREST)
        obs["notes"].append(
            f"forest_bf pre->post: {count_bf_named(pre, 0, FOREST)} -> "
            f"{count_bf_named(post, 0, FOREST)} (delta={d_forest}); "
            f"tifa_trigger_seen_on_stack={ST['trigger_seen']}")
        A["A2_single_landfall"] = "passed" if (d_forest == 1
                                              and ST["trigger_seen"]) \
            else "failed"

    # A3
    if post is None:
        A["A3_power_once"] = "not-run"
        obs["notes"].append("post_landfall.json missing; A3 not-run")
    else:
        tifa = bf_named(post, 0, TIFA)
        p = power_of(objs(post)[tifa[0]]) if tifa else None
        t = toughness_of(objs(post)[tifa[0]]) if tifa else None
        obs["notes"].append(
            f"post: Tifa power={p} (expected 2), toughness={t} (expected 2)")
        if p == 2 and t == 2:
            A["A3_power_once"] = "passed"
        elif p in (4, 8):
            A["A3_power_once"] = f"failed (bug reproduced: power={p} after one landfall)"
        else:
            A["A3_power_once"] = f"failed (unexpected power={p})"

    # A4
    if mid is None:
        A["A4_trigger_count"] = "not-run"
        obs["notes"].append("mid_trigger.json missing; A4 not-run")
    else:
        n = sum(1 for e in stack_entries(mid)
                if is_tifa_trigger(e, ST["tifa_oid"]))
        obs["notes"].append(
            f"mid: {n} Tifa landfall trigger entries on stack (expected 1)")
        A["A4_trigger_count"] = "passed" if n == 1 else "failed"

    # A5
    if not ST["cleanup_observed"]:
        A["A5_temp_expires"] = "not-run"
        obs["notes"].append("cleanup observation missing; A5 not-run")
    else:
        obs["notes"].append(
            f"cleanup: turn {ST.get('cleanup_turn')} Tifa power="
            f"{ST['cleanup_power']} (expected 1)")
        A["A5_temp_expires"] = "passed" if ST["cleanup_power"] == 1 \
            else "failed"

    for k in sorted(A):
        say(f"{k}: {A[k]}")
    say("WF sequence:", WF_SEEN)

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "wf_sequence": WF_SEEN,
                   "observations": {k: v for k, v in ST.items()
                                    if k in ("mid_trigger_count",
                                             "post_power", "post_toughness",
                                             "cleanup_power")}}, f, indent=2)

    scenario_src = open(__file__, "rb").read()
    verdict = "blocked"
    if A.get("A1_setup_ok") not in ("failed", "blocked"):
        if str(A.get("A3_power_once", "")).startswith("failed"):
            verdict = "reproduced"
        elif all(v == "passed" for v in A.values()):
            verdict = "not-reproduced"
    run_meta = {
        "run_id": RUN_ID,
        "issue": 6774,
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "server": {
            "version": "v0.79.0",
            "build_commit": "1cde7a2",
            "protocol_version": 69,
            "binary_sha256": "46d89146bf3e051cf22591bd195b7d15d4806a8f6d226bb8792dbcfe479fef94",
            "card_data_sha256": "75cbfe139b220b8267c4d99b1d478b59d7298a86dfc3def83fbb31eaa970b5b3",
            "draft_pools_sha256": "7518817d5db317ccba9f6d197648677a8ff8341700e14b4b54f8bdf2d71b0b8b",
        },
        "scenario_sha256": hashlib.sha256(scenario_src).hexdigest(),
        "decks": {
            "P0": {"Tifa Lockhart": 20, "Forest": 40},
            "P1": {"Island": 60},
        },
        "verdict": verdict,
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run_meta, f, indent=2)
    say("verdict:", verdict)
    with open(f"{EVDIR}/scenario_6774.py", "w") as f:
        f.write(scenario_src.decode())

    await p0.close()
    await p1.close()
    return obs


if __name__ == "__main__":
    obs = asyncio.run(main())
    print(json.dumps(obs["assert"], indent=2))
