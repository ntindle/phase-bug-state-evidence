#!/usr/bin/env python3
"""Issue #6774 revalidation on v0.98.0 (protocol 94): Tifa Lockhart landfall
doubles power 2-3x per single landfall.

Reported (Discord, synced to GitHub): "Just played a game with a tifa deck
and her power is multiplying 2-3x more than it should with a single landfall."

Oracle (pinned v0.98.0 card-data.json):
  Tifa Lockhart ({1}{G}, 1/2, Legendary Creature - Human Monk):
    "Trample
     Landfall -- Whenever a land you control enters, double Tifa Lockhart's
     power until end of turn."

Behavioral contract (single game, two human-client seats, v0.98.0/proto 94):
  RAMP   - P0 drops one land per own main phase, keeps mulligan, casts
           Tifa Lockhart when affordable (turn 2) and none is on BF.
  TEST   - On P0's next turn after the cast (tifa_cast_turn + 2): export
           PRE at main phase start (Tifa on BF, power 1/2, land not yet
           played), then play exactly ONE Forest.
  MID    - export at the first revision where a Tifa-sourced landfall
           trigger sits on the stack; count Tifa-trigger stack entries.
  POST   - export once the stack empties on the same turn; read Tifa's
           power/toughness.
  CLEAN  - on P0's following turn (turn > test turn), before any land drop,
           export CLEAN and observe Tifa's power back at 1 (UntilEndOfTurn
           expired). The cleanup hold guard sits BEFORE the land-drop
           block (2026-09-11 lesson), so the cleanup turn's land never
           poisons the turn gate.

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
  A5 temp_expires     clean: Tifa power back to 1 on her next turn before
                     P0's land drop.

Verdict = blocked iff A1 fails (setup never assembled).
Verdict = reproduced iff A1 passes and A3 fails (power 4 or 8, or
otherwise != 2).
Verdict = not-reproduced iff A1..A5 all pass.

Protocol-94 driver notes (v0.98.0, 2026-10-01): HELLO advertises protocol 94
(exact match enforced); MulliganDecision as {"choice":{"type":"Keep"}} gated
on waiting_for.data.pending[] Declare; DiscardToHandSize via single
SelectCards {"cards":[...]} (revision-guarded); PassPriority gated on the
seat genuinely holding priority (revision-aware); no ActivateAbility needed
for this scenario (land drop + cast + automatic non-optional trigger).
"""
import asyncio
import hashlib
import json
import logging
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client as _client  # noqa: E402
_client.URL = "ws://127.0.0.1:9374/ws"
from client import PhaseClient, deck  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario6774")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
EVID_RUN_ID = "20261001-6774"
EVDIR = f"{BACKFILL}/evidence/6774/{EVID_RUN_ID}"
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
        WIRE.write(json.dumps({"t": time.time(), "event": event + "_unserializable",
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

TIFA = "Tifa Lockhart"
FOREST = "Forest"
ISLAND = "Island"

P0_DECK = deck((TIFA, 20), (FOREST, 40))
P1_DECK = deck((ISLAND, 60))

ST = {"tifa_cast_turn": None, "land_turn": -1, "pre_exported": False,
      "test_land_played": False, "mid_exported": False,
      "mid_turn": None, "post_exported": False, "post_power": None,
      "post_toughness": None, "cleanup_observed": False,
      "cleanup_power": None, "cleanup_turn": None,
      "trigger_seen": False, "stop": False,
      "mid_trigger_count": None, "tifa_oid": None,
      "pre_forests": None, "post_forests": None, "last_stack_sig": None}
WF_SEEN = []
_MULL = {}
_DISCARD_REV = {}
_PASSED_REV = {}
_WATCH = {}


def lname(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def objs(state):
    return state.get("objects", {}) or {}


def hand_ids(state, pid):
    return [oid for oid, o in objs(state).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def bf_named(state, pid, name):
    return [oid for oid, o in objs(state).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and lname(o) == name.lower()]


def find_hand(state, pid, name):
    for oid in hand_ids(state, pid):
        if lname(objs(state)[oid]) == name.lower():
            return oid
    return None


def find_action(acts, atype):
    for a in acts or []:
        if a.get("type") == atype:
            return a


def obj_name(state, oid):
    o = objs(state).get(str(oid))
    return (o.get("base_name") or o.get("name")) if o else "?"


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
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain", "Main"))


def my_priority(state, pid):
    wf = wf_of(state)
    return wf.get("type") == "Priority" and str((wf.get("data") or {}).get("player")) == str(pid)


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_player(state):
    return (wf_of(state).get("data") or {}).get("player")


def pending_for(state, pid):
    data = wf_of(state).get("data") or {}
    for p in data.get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            return p
    return None


def vi_opps(st):
    return ((st.get("viewer_interaction") or {}).get("opportunities", [])) or []


def stack_entries(state):
    return state.get("stack") or []


def _entry_desc(entry):
    try:
        kind = entry.get("kind") or {}
        kd = kind.get("data") or {}
        ab = kd.get("ability") or {}
        return str(ab.get("description") or "")
    except Exception:
        return ""


def is_tifa_trigger(entry, tifa_oid):
    """True iff this stack entry is Tifa Lockhart's landfall power-doubling
    trigger. Discriminated defensively on protocol-94 shapes: kind type
    mentions 'trigger' and the ability description mentions 'double'
    (power-doubling text); when the Tifa object id is known and a source
    id is advertised, it must match."""
    try:
        ktype = str((entry.get("kind") or {}).get("type") or "")
    except Exception:
        return False
    if "trigger" not in ktype.lower():
        return False
    desc = _entry_desc(entry).lower()
    if "double" not in desc:
        return False
    if tifa_oid is not None:
        try:
            sid = entry.get("source_id")
            if sid is not None and str(sid) != str(tifa_oid):
                return False
        except Exception:
            pass
    return True


def record_wf(state):
    wf = (state.get("waiting_for") or {}).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf,
                             "data": (state.get("waiting_for") or {}).get("data")})


async def do_export(tag):
    s = await C0.export_state()
    path = f"{EVDIR}/{tag}.json"
    with open(path, "w") as f:
        f.write(s)
    env = json.loads(s)
    say(f"exported {tag}.json")
    return env["state"]


async def do_mulligan(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "MulliganDecision":
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    if tag in _MULL:
        return False
    say(f"[{tag}] mulligan keep")
    await c.send_action({"type": "MulliganDecision", "data": {"choice": {"type": "Keep"}}})
    _MULL[tag] = True
    wire("mulligan", {"who": tag, "decision": "keep"})
    return True


async def do_discard(c, pid, tag):
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
    oids = hand_ids(state, pid)
    n = len(oids) - 7
    if n <= 0:
        return False
    keep_names = {TIFA.lower(), FOREST.lower()}

    def rank(oid):
        nm = lname(objs(state)[oid])
        return 0 if nm in keep_names else 2

    seen = set()
    ranked = []
    for oid in sorted(oids, key=rank):
        nm = lname(objs(state)[oid])
        if nm in keep_names and nm not in seen:
            seen.add(nm)
            continue
        ranked.append(oid)
    picks = [int(x) for x in ranked[:n]]
    if not picks:
        return False
    say(f"[{tag}] discarding to hand size: {[obj_name(state, x) for x in picks]} via SelectCards")
    wire("discard_action", {"who": tag, "type": "SelectCards", "data": {"cards": picks}})
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    _DISCARD_REV[(c.name, rev)] = True
    return True


async def pass_priority(c, pid):
    st = c.latest
    if not st:
        return False
    rev = st.get("state_revision", -1)
    state = st["state"]
    if not my_priority(state, pid):
        return False
    if _PASSED_REV.get(c.name, -1) >= rev:
        return False
    for a in (st.get("legal_actions") or []):
        if a.get("type") == "PassPriority":
            await c.send_action(a)
            _PASSED_REV[c.name] = rev
            return True
    return False


def watch(c):
    now = time.time()
    last = _WATCH.get(c.name)
    if last and last["rev"] == c.revision and now - last["t"] > 60:
        st = c.latest
        view = "no-state"
        if st:
            s = st["state"]
            view = (f"turn_number={s.get('turn_number')} phase={s.get('phase')} "
                    f"active={s.get('active_player')} wf={json.dumps(wf_of(s))[:160]}")
        say(f"WATCHDOG [{c.name}] revision {c.revision} stale 60s+: {view}")
        wire("watchdog_stale", {"who": c.name, "rev": c.revision, "view": view})
        last["t"] = now


C0 = None


# ------------------------------------------------------------- tick (P0)

async def tick_p0(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    wf = wf_of(state)
    wtype = wf.get("type")
    wplayer = wf_player(state)

    if await do_mulligan(c, pid, "P0"):
        return True
    if await do_discard(c, pid, "P0"):
        return True

    ca = find_action(acts, "ChooseLegend")
    if ca and str(wplayer) == "0":
        wire("action_submit", {"who": "P0", "action": "ChooseLegend/asis"})
        await c.send_action(ca)
        say("[P0] legend rule: keeps first")
        return True

    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            wire("action_submit", {"who": "P0", "action": a["type"]})
            await c.send_action(a)
            return True

    if wtype == "OrderTriggers" and str(wplayer) == "0":
        oa = find_action(acts, "OrderTriggers")
        if oa:
            wire("action_submit", {"who": "P0", "action": "OrderTriggers/asis"})
            await c.send_action(oa)
            return True

    # never pass priority while a P0 decision is pending
    if wtype in ("OptionalCostChoice", "OptionalEffectChoice", "TargetSelection",
                 "ManaPayment", "ChooseXValue", "SurveilChoice",
                 "ScryChoice", "DiscardChoice") and str(wplayer) == "0":
        wire("hold_priority", {"wtype": wtype})
        return False

    # hold the cleanup window: after POST is exported, on P0's later turns,
    # export CLEAN on the first main-phase revision BEFORE any land drop so
    # the observed power reflects the expired UntilEndOfTurn buff. This
    # guard MUST run before the land-drop block (2026-09-11 lesson): if the
    # cleanup turn's land were played first, the main-loop turn gate would
    # never fire.
    if (ST["post_exported"] and not ST["cleanup_observed"]
            and ST["mid_turn"] is not None
            and state.get("active_player") == 0
            and (state.get("turn_number") or 0) > ST["mid_turn"]
            and is_my_main(state, pid)):
        tifa = bf_named(state, 0, TIFA)
        if tifa:
            cln = await do_export("cleanup_next_turn")
            ST["cleanup_observed"] = True
            ST["cleanup_turn"] = cln.get("turn_number")
            c_oid = bf_named(cln, 0, TIFA)
            ST["cleanup_power"] = power_of(objs(cln)[c_oid[0]]) if c_oid else None
            wire("cleanup_observed",
                 {"turn": ST["cleanup_turn"],
                  "phase": cln.get("phase"),
                  "tifa_power": ST["cleanup_power"]})
            say(f"[cleanup] turn {ST['cleanup_turn']}: Tifa power="
                f"{ST['cleanup_power']}")
            ST["stop"] = True
            return False
        wire("hold_priority", {"why": "cleanup_window",
                               "turn": state.get("turn_number"),
                               "phase": state.get("phase")})
        return False

    for atype in ("DeclareAttackers", "DeclareBlockers"):
        da = find_action(acts, atype)
        if da and (atype == "DeclareBlockers" or state.get("active_player") == 0):
            import copy
            sub = copy.deepcopy(da)
            key = "attacks" if atype == "DeclareAttackers" else "assignments"
            if atype == "DeclareAttackers":
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
            else:
                sub["data"]["assignments"] = []
            wire("action_submit", {"who": "P0", "action": f"{atype}/empty"})
            await c.send_action({"type": atype, "data": sub["data"]})
            return True

    turn = state.get("turn_number")
    tifa_bf = bf_named(state, 0, TIFA)
    test_turn = (ST["tifa_cast_turn"] + 2) if ST["tifa_cast_turn"] is not None else None

    if is_my_main(state, pid):
        # land drop: once per own main phase, but NOT after the single test
        # land has been played on the test turn
        skip_land = ST["test_land_played"] and turn == test_turn
        if turn != ST["land_turn"] and not skip_land:
            hid = find_hand(state, pid, FOREST)
            for a in acts:
                if a["type"] == "PlayLand" and hid \
                        and str(a.get("data", {}).get("object_id")) == str(hid):
                    # arm the test: export PRE before the single test land
                    if turn == test_turn and not ST["pre_exported"]:
                        pre = await do_export("pre_landfall")
                        ST["pre_exported"] = True
                        tifa0 = bf_named(pre, 0, TIFA)
                        ST["tifa_oid"] = str(tifa0[0]) if tifa0 else None
                        ST["pre_forests"] = sum(
                            1 for o in objs(pre).values()
                            if o.get("zone") == "Battlefield" and o.get("controller") == 0
                            and lname(o) == FOREST.lower())
                        pw = power_of(objs(pre)[tifa0[0]]) if tifa0 else None
                        tw = toughness_of(objs(pre)[tifa0[0]]) if tifa0 else None
                        wire("pre_landfall", {
                            "turn": pre.get("turn_number"),
                            "phase": pre.get("phase"),
                            "tifa_oid": ST["tifa_oid"],
                            "tifa_power": pw,
                            "tifa_toughness": tw,
                            "forests_bf": ST["pre_forests"],
                        })
                        say(f"[pre] exported turn {pre.get('turn_number')} "
                            f"phase {pre.get('phase')} tifa power={pw}")
                    ST["land_turn"] = turn
                    wire("action_submit", {"who": "P0", "action": "PlayLand/Forest",
                                          "test_turn": turn == test_turn})
                    await c.send_action(a)
                    if turn == test_turn:
                        ST["test_land_played"] = True
                        say(f"[P0] plays the single TEST land (turn {turn})")
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
                               "power": power_of(objs(state)[tifa_bf[0]]),
                               "toughness": toughness_of(objs(state)[tifa_bf[0]])})
            say(f"[P0] Tifa on battlefield (turn {turn})")

    return await pass_priority(c, pid)


# ------------------------------------------------------------- tick (P1)

async def tick_p1(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    wplayer = wf_player(state)

    if await do_mulligan(c, pid, "P1"):
        return True
    if await do_discard(c, pid, "P1"):
        return True
    if wf_of(state).get("type") == "OrderTriggers" and str(wplayer) == "1":
        oa = find_action(acts, "OrderTriggers")
        if oa:
            await c.send_action(oa)
            return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    for atype in ("DeclareAttackers", "DeclareBlockers"):
        da = find_action(acts, atype)
        if da:
            import copy
            sub = copy.deepcopy(da)
            if atype == "DeclareAttackers":
                sub["data"]["attacks"] = []
                sub["data"]["bands"] = []
            else:
                sub["data"]["assignments"] = []
            await c.send_action({"type": atype, "data": sub["data"]})
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
    return await pass_priority(c, pid)


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
    _WATCH[p0.name] = {"rev": p0.revision, "t": time.time()}
    _WATCH[p1.name] = {"rev": p1.revision, "t": time.time()}

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
                    _WATCH[c.name] = {"rev": c.revision, "t": time.time()}
            except Exception as e:
                say(f"tick error {c.name}: {e}")
        st = p0.latest
        if not st:
            continue
        state = st["state"]
        record_wf(state)
        watch(p0)
        watch(p1)

        turn = state.get("turn_number")
        phase = state.get("phase") or ""

        # stack audit during the test window
        if ST["pre_exported"] and not ST["post_exported"]:
            stack = stack_entries(state)
            n_tifa = sum(1 for e in stack
                         if is_tifa_trigger(e, ST["tifa_oid"]))
            if stack and ST.get("last_stack_sig") is None:
                wire("first_stack_seen", {
                    "turn": turn, "phase": phase,
                    "tifa_triggers": n_tifa, "stack_size": len(stack),
                    "entries": [
                        {"id": e.get("id"), "kind": (e.get("kind") or {}).get("type"),
                         "desc": _entry_desc(e)[:120], "source_id": e.get("source_id")}
                        for e in stack]})
            if stack or n_tifa:
                sig = json.dumps(
                    [(e.get("id"), _entry_desc(e)[:60]) for e in stack],
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
                mid = await do_export("mid_trigger")
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
                post = await do_export("post_landfall")
                ST["post_exported"] = True
                tifa = bf_named(post, 0, TIFA)
                if tifa:
                    ST["post_power"] = power_of(objs(post)[tifa[0]])
                    ST["post_toughness"] = toughness_of(objs(post)[tifa[0]])
                    ST["post_forests"] = sum(
                        1 for o in objs(post).values()
                        if o.get("zone") == "Battlefield" and o.get("controller") == 0
                        and lname(o) == FOREST.lower())
                    wire("post_landfall", {"turn": post.get("turn_number"),
                                           "phase": post.get("phase"),
                                           "tifa_power": ST["post_power"],
                                           "tifa_toughness": ST["post_toughness"],
                                           "forests_bf": ST["post_forests"]})
                    say(f"[post] exported: Tifa power={ST['post_power']} "
                        f"toughness={ST['post_toughness']}")
                else:
                    wire("post_landfall", {"error": "Tifa not on BF in post"})

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
    cln = env_state("cleanup_next_turn.json")

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
        pre_f = ST.get("pre_forests")
        post_f = ST.get("post_forests")
        d_forest = (post_f - pre_f) if pre_f is not None and post_f is not None else None
        obs["notes"].append(
            f"forest_bf pre->post: {pre_f} -> {post_f} (delta={d_forest}); "
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
    if cln is None:
        A["A5_temp_expires"] = "not-run"
        obs["notes"].append("cleanup_next_turn.json missing; A5 not-run")
    else:
        tifa = bf_named(cln, 0, TIFA)
        p = power_of(objs(cln)[tifa[0]]) if tifa else None
        obs["notes"].append(
            f"cleanup: turn {cln.get('turn_number')} phase={cln.get('phase')} "
            f"Tifa power={p} (expected 1)")
        A["A5_temp_expires"] = "passed" if p == 1 else "failed"

    for k in sorted(A):
        say(f"{k}: {A[k]}")
    say("WF sequence:", WF_SEEN)

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "wf_sequence": WF_SEEN,
                   "observations": {k: ST.get(k) for k in
                                    ("mid_trigger_count", "post_power",
                                     "post_toughness", "cleanup_power",
                                     "cleanup_turn", "pre_forests",
                                     "post_forests")}}, f, indent=2)

    scenario_src = open(__file__, "rb").read()
    verdict = "blocked"
    if A.get("A1_setup_ok") not in ("failed", "blocked"):
        if str(A.get("A3_power_once", "")).startswith("failed"):
            verdict = "reproduced"
        elif all(v == "passed" for v in A.values()):
            verdict = "not-reproduced"
    run_meta = {
        "run_id": EVID_RUN_ID,
        "issue": 6774,
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "server": SERVER_IDENTITY,
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
    with open(f"{EVDIR}/scenario_6774_0980.py", "w") as f:
        f.write(scenario_src.decode())

    await p0.close()
    await p1.close()
    return obs


if __name__ == "__main__":
    obs = asyncio.run(main())
    print(json.dumps(obs["assert"], indent=2))
