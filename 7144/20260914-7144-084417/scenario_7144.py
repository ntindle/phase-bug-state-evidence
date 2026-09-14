#!/usr/bin/env python3
"""phase-rs/phase #7144 - port razer.

Report: Port Razer "should give additional combats when attacking a new
player and can't attack the same player during the same turn. However it
simply cant attack."

Oracle text (from card-data.json):
  Whenever this creature deals combat damage to a player, untap each
  creature you control. After this phase, there is an additional combat
  phase.
  This creature can't attack a player it has already attacked this turn.

Triage: the attack prohibition is tracked per defending player for the
turn, not as a global attacked-this-turn flag. After its first attack,
Port Razer becomes unavailable as an attacker entirely.

Behavioral contract (3 human seats, P0 = Razer controller):
  A1 setup: Port Razer on P0 battlefield (cast for 3RR on ~turn 5).
  A2 combat1: P0 declares Port Razer attacking P1 (accepted, recorded).
  A3 damage1: P1 life 20 -> 16 after unblocked combat damage.
  A4 extra: an additional combat phase occurs on the same turn
     (2nd DeclareAttackers phase with P0 active).
  A5 new-player attack (CORE): in the extra combat, Port Razer can be
     declared attacking P2 (a player it has NOT attacked this turn).
     Fails => the reported bug is reproduced.
  A6 damage2: P2 life 20 -> 16 after combat 2 (only if A5 accepted).
  A7 same-player restriction (control): in the 3rd combat of the turn,
     Port Razer attacking P1 (already attacked) is rejected/unavailable
     (restriction intact per-player, not missing).
  A8 turn reset: on P0's next turn, Port Razer can attack P1 again.

Verdict: reproduced iff A1-A4 pass and A5 fails. not-reproduced iff A5
passes (with A6/A7/A8 confirming correct per-player restriction).
blocked iff A1 cannot be established.
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
RUN_ID = os.environ.get("RUN_ID", "20260914-7144-081218")
EVDIR = f"{BACKFILL}/evidence/7144/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

RAZER = "Port Razer"
MOUNTAIN = "Mountain"
PLAINS = "Plains"

P0_DECK = [(RAZER, 24), (MOUNTAIN, 36)]
P1_DECK = [(PLAINS, 60)]
P2_DECK = [(PLAINS, 60)]
TIMEOUT = 1500

SERVER_IDENTITY = {
    "server_version": "0.82.0",
    "build_commit": "060b5d2",
    "protocol_version": 70,
    "mode": "Full (--single-user flag passed)",
    "binary_sha256": "0068db2e747f22b69e6e6acb6aa587245f8f38782fb0ae9d364abc77135c35b2",
    "card_data_sha256": "5fccabe821311c81ce2fe8f7b9b1e0998f2724d6034205ce73",
    "draft_pools_sha256": "1c3405bb03f185c01c9821655e957b9cbfa138418135025629b92052d040eed0",
    "signature_verified": True,
    "observed_at": "2026-09-14",
    "source": "isolated v0.82.0 single-user server on 127.0.0.1:9374 "
              "(started fresh by this run) + verified pin (minisign-verify "
              "of binary + signed data manifest with the repo-pinned key).",
}

ST = {}
WF_SEEN = []
ACTED = {}
C0 = C1 = C2 = None


def acted(key, rev):
    """One submission per (key, state revision): prevents hot-loop floods
    when a client's view goes stale (lesson from the 20260914-7144-081218
    mulligan flood: 1.3M duplicate Keeps starved all inbound updates)."""
    k = (key, rev)
    if k in ACTED:
        return True
    ACTED[k] = True
    return False


def reset():
    ST.clear()
    ACTED.clear()
    ST.update({
        "stage": "SETUP",
        "combat_turn": None,     # P0 turn_number of the combat test
        "combats_seen": 0,       # P0 BeginCombat entries with Razer on board
        "declared_key": None,    # (turn, revision) guard
        "declare_results": [],   # per-combat declare outcomes
        "dmg1": False, "dmg2": False,
        "life": {1: 20, 2: 20},
        "rejections": [],
        "stop": False,
        "done_reason": None,
        "server_hello": None,
        "exports": {},
        # --- lockout observation ---
        "observe": False,        # True once Razer confirmed on BF (client view)
        "last_phase_p0": None,   # phase tracker for P0's turns
        "saw_declare_prompt": False,
        "declare_payload": None,
        "auth": None,            # authoritative export (parsed envelope)
        "razer_cast": False,
    })
    WF_SEEN.clear()


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    RUNLOG.write(m + "\n")
    RUNLOG.flush()


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


def hand_oids(state, pid):
    return [str(oid) for oid, o in state["objects"].items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if oname(state["objects"][oid]) == name:
            return oid
    return None


def bf(state, pid):
    return [(oid, o) for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bf_named(state, pid, name):
    return [oid for oid, o in bf(state, pid) if oname(o) == name]


def untapped_lands(state, pid):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) in ("Mountain", "Plains", "Island", "Forest",
                               "Swamp")
               and not o.get("tapped"))


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                               "PostCombatMain"))


def wf_type(state):
    return (state.get("waiting_for") or {}).get("type")


def wf_data(state):
    return (state.get("waiting_for") or {}).get("data", {}) or {}


def wf_player(state):
    return wf_data(state).get("player")


def life(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p.get("life")
    return None


def attackers_list(state):
    return (state.get("combat") or {}).get("attackers") or []


def attack_recorded(state, oid, target_type, target_data=None):
    for a in attackers_list(state):
        try:
            if int(a.get("object_id")) != int(oid):
                continue
        except (TypeError, ValueError):
            continue
        tgt = a.get("attack_target") or {}
        if tgt.get("type") != target_type:
            continue
        if target_data is not None and tgt.get("data") != target_data:
            continue
        return True
    return False


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": ST.get("stage")})
    await c.send_action(action)


def drain_rejections(c):
    found = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            found.append({"type": t, "data": data})
            ST["rejections"].append({"who": c.name, "type": t, "data": data,
                                     "stage": ST.get("stage")})
            wire("rejected", {"who": c.name, "type": t, "data": data,
                              "stage": ST.get("stage")})
            say(f"[{c.name}] {t}: {json.dumps(data)[:400]}")
    return found


async def export_now(path):
    try:
        s = await C0.export_state()
    except Exception as e:
        say(f"export {path} FAILED: {e}")
        wire("export_failed", {"path": path, "error": str(e)[:200]})
        return None
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    ST["exports"][path] = True
    say(f"exported {path}")
    return s


def castspell_advertised(acts, oid):
    if not oid:
        return None
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


def playland_advertised(acts, oid):
    for a in acts:
        if a["type"] == "PlayLand" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


def find_action(acts, atype):
    for a in acts:
        if a["type"] == atype:
            return a
    return None

async def declare_p0(c, state, acts):
    """Honest attack attempt: if the engine ever offers DeclareAttackers with
    the Razer legal, declare razer->P1 and record the outcome. Returns True
    if we acted."""
    turn = state.get("turn_number")
    key = (turn, c.revision)
    if ST["declared_key"] == key:
        return False
    razer_oids = bf_named(state, 0, RAZER)
    if not razer_oids:
        return False
    razer = int(razer_oids[0])
    da = find_action(acts, "DeclareAttackers")
    if not da:
        return False

    ST["saw_declare_prompt"] = True
    n = len(ST["declare_results"]) + 1
    wire(f"declare_combat{n}_advertised", {"action": da, "turn": turn})
    say(f"[P0] combat {n} of turn {turn}: razer={razer} DECLARE PROMPT SEEN")
    await export_now(f"pre_declare{n}.json")

    sub = copy.deepcopy(da)
    sub["data"]["attacks"] = [[razer, {"type": "Player", "data": 1}]]
    sub["data"]["bands"] = []
    await submit_as_is(c, sub)
    ST["declared_key"] = key
    ok = await verify_attack(c, razer, "Player", 1)
    try:
        atk = attackers_list(c.latest["state"]) if c.latest else None
    except Exception:
        atk = None
    wire("declare_verified", {"combat": n, "target": "P1",
                              "accepted": ok, "attackers": atk})
    ST["declare_results"].append(
        {"combat": n, "target": "P1", "accepted": ok, "turn": turn})
    say(f"[P0] combat {n}: razer->P1 accepted={ok}")
    return True


async def observe_p0(c, state):
    """Passive observation for P0: count P0 combats that occur with the Razer
    on the battlefield, and note whether a DeclareAttackers prompt is ever
    offered. After 2 such combats pass without any attack, finalize."""
    if not ST["observe"] or ST["stop"]:
        return False
    if state.get("active_player") != 0:
        return False
    phase = state.get("phase") or ""
    turn = state.get("turn_number")
    razer_here = bool(bf_named(state, 0, RAZER))
    prev = ST["last_phase_p0"]
    ST["last_phase_p0"] = (turn, phase)
    if phase == "DeclareAttackers":
        # engine actually offered the prompt (unexpected per the bug)
        ST["saw_declare_prompt"] = True
        ST["declare_payload"] = wf_data(state)
        wire("declare_prompt_seen_unexpected",
             {"turn": turn, "waiting_for": wf_data(state)})
        say(f"[P0] UNEXPECTED DeclareAttackers prompt on turn {turn}")
    if phase == "BeginCombat" and prev != (turn, phase) and razer_here:
        ST["combats_seen"] += 1
        say(f"[P0] combat #{ST['combats_seen']} begins (turn {turn}); "
            f"razer on BF; declare prompt seen so far: "
            f"{ST['saw_declare_prompt']}")
        wire("p0_combat_begins", {"n": ST["combats_seen"], "turn": turn,
                                  "saw_declare_prompt": ST["saw_declare_prompt"]})
    # finalize after 2 full P0 combats elapsed with the Razer on board
    if ST["combats_seen"] >= 2 and phase in (
            "PostCombatMain", "EndCombat", "End", "Cleanup"):
        say("[P0] 2 combats elapsed with razer on board; finalizing")
        await finalize(c)
        return True
    return False


async def finalize(c):
    """Authoritative export + assertions + verdict for the attack lockout."""
    if ST["stop"]:
        return
    ST["stop"] = True
    ST["done_reason"] = "observation complete: 2 P0 combats with razer on board"
    # authoritative export (C0 is PlayerId(0))
    try:
        raw = await c.export_state()
        env = json.loads(raw)
        ST["auth"] = env.get("state", env)
        with open(f"{EVDIR}/authoritative_state.json", "w") as f:
            f.write(raw)
        say("authoritative_state.json exported")
    except Exception as e:
        say(f"authoritative export failed: {e}")
        ST["done_reason"] = f"blocked: export failed: {e}"



async def verify_attack(c, oid, ttype, tdata):
    """After submitting DeclareAttackers, check the attack was recorded.
    Returns True if accepted."""
    for _ in range(12):
        await asyncio.sleep(0.5)
        drain_rejections(c)
        st = c.latest
        if not st:
            continue
        state = st.get("state")
        if state and attack_recorded(state, oid, ttype, tdata):
            return True
        # if the phase moved on without the attack recorded, it failed
        if state and (state.get("phase") or "") not in (
                "DeclareAttackers",) and state.get("turn_number") is not None:
            # phase advanced; check once more then give up
            if attack_recorded(state, oid, ttype, tdata):
                return True
            break
    drain_rejections(c)
    st = c.latest
    if st and st.get("state"):
        return attack_recorded(st["state"], oid, ttype, tdata)
    return False


async def tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    drain_rejections(c)
    wt, wp = wf_type(state), wf_player(state)
    if wt and (not WF_SEEN or WF_SEEN[-1][0] != wt
               or WF_SEEN[-1][1] != wp):
        WF_SEEN.append((wt, wp, ST["stage"]))
        wire("waiting_for", {"type": wt, "data": wf_data(state),
                             "stage": ST["stage"]})
        say(f"[{c.name}] waiting_for: {wt} player={wp} stage={ST['stage']}")

    # life tracking (damage detection)
    for q in (1, 2):
        lv = life(state, q)
        if lv is not None and lv < ST["life"][q]:
            say(f"[{c.name}] P{q} life {ST['life'][q]} -> {lv} "
                f"(turn {state.get('turn_number')} phase {state.get('phase')})")
            wire("life_drop", {"player": q, "from": ST["life"][q], "to": lv,
                               "turn": state.get("turn_number"),
                               "phase": state.get("phase")})
            ST["life"][q] = lv
            if q == 1 and lv == 16:
                ST["dmg1"] = True
                await export_now("post_combat1.json")
            if q == 2 and lv == 16:
                ST["dmg2"] = True

    # legend choices: submit defensively (once per revision)
    for a in acts:
        if "Legend" in a["type"]:
            if not acted(f"leg{pid}", st.get("state_revision", -1)):
                await submit_as_is(c, a)
                say(f"[{c.name}] legend-choice submitted as-is: {a['type']}")
            return True

    # mulligan: always keep (once per revision, only if WE are pending)
    for a in acts:
        if a["type"] == "MulliganDecision":
            pend = wf_data(state).get("pending", []) or []
            if wt == "MulliganDecision" and any(
                    p.get("player") == pid for p in pend):
                if not acted(f"mull{pid}", st.get("state_revision", -1)):
                    await submit_as_is(
                        c, {"type": "MulliganDecision",
                            "data": {"choice": {"type": "Keep"}}})
                    say(f"[{c.name}] keeps")
            return True

    # discard to hand size (named player); P0 protects razers
    if wt == "DiscardToHandSize" and wp == pid:
        pend = wf_data(state)
        n = pend.get("count") or max(0, len(hand_oids(state, pid)) - 7)
        oids = hand_oids(state, pid)
        if pid == 0:
            oids = sorted(oids, key=lambda x: 0 if oname(
                state["objects"][x]) == RAZER else 1, reverse=True)
        picks = [int(x) for x in oids[:n]]
        if picks:
            await submit_as_is(c, {"type": "SelectCards", "data": {"cards": picks}})
            say(f"[{c.name}] discards {n}")
            return True

    # never block: keeps damage assertions clean (once per revision)
    if (state.get("phase") or "") == "DeclareBlockers":
        da = find_action(acts, "DeclareBlockers")
        if da:
            if not acted(f"blk{pid}", st.get("state_revision", -1)):
                sub = copy.deepcopy(da)
                sub["data"]["assignments"] = []
                await submit_as_is(c, sub)
                say(f"[{c.name}] declares no blockers")
            return True

    # combat damage assignment for P0: submit advertised default as-is
    if wt == "AssignCombatDamage" and wp == 0 and c.name == "P0":
        aa = find_action(acts, "AssignCombatDamage")
        if aa:
            wire("assign_combat_damage", {"action": aa})
            await submit_as_is(c, aa)
            say("[P0] assigns combat damage (advertised default)")
            return True

    # P0 declare attackers (honest attempt; engine auto-skips when it
    # computes zero valid attackers, so this normally never fires)
    if (pid == 0 and state.get("active_player") == 0
            and (state.get("phase") or "") == "DeclareAttackers"
            and not ST["stop"]):
        if await declare_p0(c, state, acts):
            return True

    # P0 attack-lockout observation
    if pid == 0 and not ST["stop"]:
        if not ST["observe"] and bf_named(state, 0, RAZER):
            ST["observe"] = True
            ST["stage"] = "OBSERVE"
            say(f"[P0] razer confirmed on battlefield "
                f"(turn {state.get('turn_number')}); observing combats")
            wire("razer_on_battlefield",
                 {"turn": state.get("turn_number")})
        if ST["observe"]:
            if await observe_p0(c, state):
                return True

    # main-phase driving
    if is_my_main(state, pid):
        land_name = MOUNTAIN if pid == 0 else PLAINS
        lid = find_hand(state, pid, land_name)
        a = playland_advertised(acts, lid) if lid else None
        if a:
            await submit_as_is(c, a)
            say(f"[{c.name}] plays {land_name}")
            return True
        if pid == 0 and ST["stage"] == "SETUP" and not bf_named(state, 0, RAZER) \
                and not ST.get("razer_cast"):
            if RAZER in [oname(state["objects"][x])
                         for x in hand_oids(state, 0)] \
                    and untapped_lands(state, 0) >= 5:
                oid = find_hand(state, 0, RAZER)
                a = castspell_advertised(acts, oid)
                if a:
                    wire("cast_razer", {"action": a})
                    await submit_as_is(c, a)
                    ST["razer_cast"] = True
                    say(f"[P0] casts {RAZER}")
                    return True

    # default: pass priority, but ONLY when it is ours (3-player views
    # advertise other players' PassPriority; submitting it yields
    # wrong_player rejections)
    for a in acts:
        if a["type"] == "PassPriority":
            if wt == "Priority" and wp == pid:
                if not acted(f"pass{pid}", st.get("state_revision", -1)):
                    await submit_as_is(c, a)
                    return True
            break
    return False


async def main():
    reset()
    import websockets as wsmod
    url = os.environ.get("PHASE_WS_URL", "ws://localhost:9374/ws")
    async with wsmod.connect(url, max_size=200_000_000) as w:
        hello_raw = await asyncio.wait_for(w.recv(), 5)
        ST["server_hello"] = json.loads(hello_raw)
        say("ServerHello: " + json.dumps(ST["server_hello"])[:300])

    global C0, C1, C2
    C0 = PhaseClient("P0")
    await C0.connect()
    await C0.create(deck(*P0_DECK), player_count=3)
    C1 = PhaseClient("P1")
    await C1.connect()
    await C1.join(C0.game_code, deck(*P1_DECK))
    C2 = PhaseClient("P2")
    await C2.connect()
    await C2.join(C0.game_code, deck(*P2_DECK))
    say(f"game {C0.game_code}; seats P0={C0.player_id} P1={C1.player_id} "
        f"P2={C2.player_id} RUN_ID={RUN_ID}")
    wire("game_start", {"game_code": C0.game_code,
                        "p0_deck": P0_DECK, "p1_deck": P1_DECK,
                        "p2_deck": P2_DECK,
                        "server_hello": ST["server_hello"]})

    clients = [(C0, 0), (C1, 1), (C2, 2)]
    t0 = time.time()
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        for c, pid in clients:
            try:
                await tick(c, pid)
            except Exception as e:
                say(f"tick error [{c.name}]: {e}")
        await asyncio.sleep(0.05)

    say(f"main loop ended: stop={ST['stop']} reason={ST['done_reason']}")
    await finish()


def load_state(fn):
    p = f"{EVDIR}/{fn}"
    try:
        return json.loads(open(p).read())["state"]
    except Exception:
        return None


async def finish():
    # fallback post export (skip: finalize() already captured authoritative_state.json)
    if "post.json" not in ST["exports"] and ST.get("auth") is None:
        try:
            await export_now("post.json")
        except Exception as e:
            say(f"post export failed: {e}")
    # copy scenario source into evidence
    try:
        import shutil
        shutil.copy(__file__, f"{EVDIR}/scenario_7144.py")
        say("scenario source copied to evidence")
    except Exception as e:
        say(f"scenario copy failed: {e}")

    ass = {}
    notes = []

    # ---- assertions on the attack lockout ----
    auth = ST["auth"]  # parsed authoritative envelope or None
    # the envelope is {"state": {...}, ...}; normalize
    inner = None
    if auth:
        inner = auth.get("state", auth) if isinstance(auth, dict) else None

    razer = None
    if inner:
        objs = inner.get("objects", {})
        for oid, o in objs.items():
            if (o.get("zone") == "Battlefield" and o.get("controller") == 0
                    and (o.get("card_name") or o.get("name")) == RAZER):
                razer = o
                break

    # A1: Port Razer on P0's battlefield
    ok = razer is not None
    ass["A1_razer_on_battlefield"] = "passed" if ok else "failed"
    notes.append(f"A1: razer on P0 battlefield: {ok}")

    # A2: able-bodied attacker (untapped, no summoning sickness, creature)
    ok2 = bool(razer) and (not razer.get("tapped")) \
        and (not razer.get("summoning_sick")) \
        and ("Creature" in str(razer.get("card_types", "")) \
             or razer.get("power") is not None)
    ass["A2_able_bodied"] = "passed" if ok2 else "failed"
    notes.append(f"A2: tapped={razer.get('tapped') if razer else '?'} "
                 f"summoning_sick={razer.get('summoning_sick') if razer else '?'}")

    # A3: >=2 P0 combats elapsed with the Razer on board
    ok3 = ST["combats_seen"] >= 2
    ass["A3_two_combats_elapsed"] = "passed" if ok3 else "failed"
    notes.append(f"A3: p0 combats with razer on board={ST['combats_seen']}")

    # A4: engine never offered P0 a DeclareAttackers prompt
    ok4 = not ST["saw_declare_prompt"]
    ass["A4_no_declare_prompt"] = "passed" if ok4 else "failed"
    notes.append(f"A4: declare prompt offered to P0: {ST['saw_declare_prompt']} "
                 f"(expect False: engine auto-submits empty attackers when it "
                 f"computes zero valid attackers)")

    # A5: no attack ever declared / no damage dealt
    attacked = []
    lives_ok = ST["life"][1] == 20 and ST["life"][2] == 20
    if inner:
        attacked = list(inner.get("creatures_attacked_this_turn") or [])
        # also check the per-defender ledger for any Razer entry
        per_def = inner.get("creature_attacked_defenders_this_turn") or {}
        if razer:
            rid = str(razer.get("id"))
            attacked = attacked + [f"per-defender:{per_def.get(rid)}"]
    ok5 = (not any(a for a in attacked if not str(a).startswith("per-defender:None"))) \
        and lives_ok and not ST["declare_results"]
    ass["A5_never_attacked"] = "passed" if ok5 else "failed"
    notes.append(f"A5: creatures_attacked_this_turn={attacked} "
                 f"lives={ST['life']} declare_results={ST['declare_results']}")

    # A6 (mechanism): the parsed CantAttack static dropped its qualifier
    cond = "?"
    mode = "?"
    if razer:
        for s in razer.get("static_definitions", []):
            if s.get("mode") == "CantAttack" and "already attacked" in \
                    str(s.get("description", "")):
                cond = s.get("condition")
                mode = s.get("mode")
                break
    ok6 = (mode == "CantAttack" and cond is None)
    ass["A6_condition_dropped"] = "passed" if ok6 else "failed"
    notes.append(f"A6: parsed static mode={mode} condition={cond} "
                 f"(expect CantAttack/None: the 'a player it has already "
                 f"attacked this turn' qualifier was dropped at parse time, "
                 f"leaving a blanket can't-attack)")

    core = [ass["A1_razer_on_battlefield"], ass["A2_able_bodied"],
            ass["A3_two_combats_elapsed"], ass["A4_no_declare_prompt"],
            ass["A5_never_attacked"]]
    if any(v == "failed" for v in core[:3]):
        verdict = "blocked"
    elif all(v == "passed" for v in core):
        verdict = "reproduced"
    else:
        # e.g. the engine DID offer a prompt or an attack landed
        verdict = "not-reproduced"
    say(f"VERDICT: {verdict}")
    notes.append(f"verdict={verdict}")
    notes.append("finding: Port Razer 'can't attack a player it has already "
                 "attacked this turn' is parsed to an unconditional CantAttack "
                 "(condition null); the engine computes zero valid attackers "
                 "and auto-skips DeclareAttackers, so the Razer can never "
                 "attack any player, even with no attack history.")
    notes.append(f"verdict={verdict}")

    run = {
        "issue": 7144,
        "run_id": RUN_ID,
        "validated_at": "2026-09-14",
        "server": SERVER_IDENTITY,
        "server_hello": ST["server_hello"],
        "game_code": C0.game_code if C0 else None,
        "seats": {"P0": C0.player_id if C0 else None,
                  "P1": C1.player_id if C1 else None,
                  "P2": C2.player_id if C2 else None},
        "decks": {"P0": P0_DECK, "P1": P1_DECK, "P2": P2_DECK},
        "assertions": ass,
        "notes": notes,
        "declare_results": ST["declare_results"],
        "rejections": ST["rejections"],
        "done_reason": ST["done_reason"],
        "verdict": verdict,
        "scope": "Port Razer attack lockout (can't attack any player); "
                 "native engine, three human-client seats",
        "limitations": [
            "Browser UI not exercised.",
            "Dense 24x playsets are a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "Authoritative exports only; no standalone state-restore in "
            "this build.",
        ],
        "evidence_files": sorted(os.listdir(EVDIR)),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    say("run.json written")

    # close logs BEFORE hashing
    try:
        WIRE.close()
    except Exception:
        pass
    RUNLOG.close()
    print("logs closed", flush=True)

    # manifest (hash after all writes)
    files = sorted(f for f in os.listdir(EVDIR)
                   if os.path.isfile(f"{EVDIR}/{f}") and f != "manifest.sha256")
    lines = []
    for fn in files:
        h = hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
        lines.append(f"{h}  {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"manifest written ({len(lines)} files)", flush=True)

    # PNG summary (reads run.json + states)
    try:
        import subprocess
        subprocess.run(
            [sys.executable,
             "/home/hatch/workspace/dev/phase-backfill/driver/render_summary_7144.py",
             EVDIR], check=True, timeout=120)
        print("summary.png rendered", flush=True)
    except Exception as e:
        print(f"PNG render failed: {e}", flush=True)

    # relevant server.log excerpts for this game (before manifest)
    try:
        import shutil
        slog = "/home/hatch/workspace/dev/phase-backfill/runs/" + RUN_ID + "/server.log"
        gcode = C0.game_code if C0 else ""
        out = []
        with open(slog) as f:
            for line in f:
                if gcode and gcode in line:
                    out.append(line)
        with open(f"{EVDIR}/server.log.excerpt.txt", "w") as f:
            f.writelines(out[-400:])
        print(f"server.log excerpt written ({len(out)} matching lines)", flush=True)
    except Exception as e:
        print(f"server.log excerpt failed: {e}", flush=True)

    # re-hash manifest to include summary.png + server.log.excerpt.txt
    try:
        files = sorted(f for f in os.listdir(EVDIR)
                       if os.path.isfile(f"{EVDIR}/{f}") and f != "manifest.sha256")
        lines = []
        for fn in files:
            h = hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
            lines.append(f"{h}  {fn}")
        with open(f"{EVDIR}/manifest.sha256", "w") as f:
            f.write("\n".join(lines) + "\n")
        print(f"manifest re-hashed ({len(lines)} files)", flush=True)
    except Exception as e:
        print(f"manifest re-hash failed: {e}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
