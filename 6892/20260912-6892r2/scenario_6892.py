#!/usr/bin/env python3
"""Issue #6892: AI Stuck Bug -- AI played Ob Nixilis, activated its ability,
then a popup said "failed to activate Planeswalker ability" and the game stuck.

Reporter: AI (controller) cast Ob Nixilis of the Black Oath, activated its
[-2] (create a 5/5 Demon, lose 2 life); the activation fully succeeded, but
afterwards a failed-activation popup appeared and the game softlocked with
the AI controller stuck. Maintainer triage found the stuck capture held an
orphaned TriggeredAbility resolution carrier (Voice of the Blessed
PutCounter) with waiting_for = Priority { player: 1 } and an empty stack;
a later replay note says the capture predates the carrier-settle fields
(version skew) and heals on load via normalize_legacy_completed_resolution_carrier.

Triage acceptance criteria (testable on the pinned build):
  - The AI submits each planeswalker activation once and advances after it
    resolves.
  - A resolved activation is not retried against stale state (no second
    same-turn activation of the same permanent).
  - If an AI proposal becomes invalid, recovery returns priority / chooses
    another legal action without locking the game (game keeps advancing).

Plan (native engine, v0.80.0 / protocol 69, native Medium AI):
  P0 (human driver): Plains + Serra Angel (flying blockers to survive Demon
     beats), plays a land each turn, blocks attackers, never attacks, passes
     priority.
  P1 (native Medium AI): 12x Ob Nixilis of the Black Oath + Swamps.
  Watch: AI casts Ob Nixilis -> AI activates a loyalty ability -> ability
     resolves -> game keeps advancing for >=3 further turns.

Behavioral contract:
  A1 setup_ok          Ob Nixilis of the Black Oath observed on P1 (AI)
                       battlefield (pre.json exported).
  A2 ai_activation      AI loyalty activation observed on a P1 Ob Nixilis
                       (loyalty_activations_this_turn 0 -> >=1), turn/oid/
                       loyalty delta recorded.
  A3 activation_resolves  the ability's effect resolved (Demon token on P1
                       BF for -2, or life deltas for +2) and the stack was
                       later observed empty.
  A4 no_double_activation  on the activation turn, that permanent's
                       activation count stayed at 1 until the turn number
                       advanced (no same-turn retry of the resolved ability).
  A5 no_stuck          after the activation resolved, the game advanced
                       >=3 turns with no >180s revision stall and no AI-halt
                       / AI-loop-stop signatures in server.log.

Verdict: reproduced iff A2 passes and (A3 fails or A4 fails or A5 fails).
not-reproduced iff A1..A5 all pass. blocked iff the AI never casts Ob
Nixilis / never activates within the turn cap (reported path not exercised).

Driver history: attempt 1 (run 20260912-6892) completed the full flow but its
inline A3 used stale pre-resolution snapshot values (lives/Demon read at
activation-observation time, before the -2 resolved), wrongly marking A3
failed; the saved states in that run already showed the -2 fully resolved
(Demon token on P1 BF, P1 20->18, stack empty). A3 was reworked to evaluate
from the saved post_activation.json state; this is attempt 2.
"""
import asyncio
import copy
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260912-6892r2"
EVDIR = f"{BACKFILL}/evidence/6892/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

OB = "Ob Nixilis of the Black Oath"
ANGEL = "Serra Angel"
PLAINS = "Plains"
SWAMP = "Swamp"
LANDS = (PLAINS,)
P0_DECK = [(ANGEL, 12), (PLAINS, 48)]
P1_AI_DECK = [(OB, 12), (SWAMP, 48)]
TIMEOUT = 2400
STALL_AFTER = 180  # seconds with zero revisions before declaring a stall
TURN_CAP = 50      # give up waiting for the AI activation past this turn

ST = {}
SUBMITTED = set()
MULLS = {}
WF_SEEN = []


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",          # SETUP -> WATCH -> RESOLVED -> DONE
        "stop": False,
        "ob_seen": False, "ob_oid": None, "ob_seen_turn": None,
        "pre_exported": False,
        "activation_observed": False, "activation_turn": None,
        "activation_oid": None, "loyalty_before": None,
        "loyalty_after": None, "p0_life_before": None,
        "p1_life_before": None, "p0_life_after": None,
        "p1_life_after": None, "demon_before": 0, "demon_after": 0,
        "stack_empty_after": False,
        "post_activation_exported": False, "post_activation_turn": None,
        "double_activation": False, "activation_via": None,
        "stall_observed": False, "stall_wf": None,
        "last_rev_change": None, "game_started": False, "game_over": False,
        "rejections": [],
        "turns_seen": set(),
    })
    SUBMITTED.clear()
    MULLS.clear()
    MULLS.update({"P0": 0})
    WF_SEEN.clear()


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
        WIRE.write(json.dumps({"t": time.time(),
                               "event": event + "_unserializable",
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


def life_of(state, pid):
    ps = state.get("players") or []
    if pid < len(ps):
        return ps[pid].get("life")
    return None


def loyalty_activations(o):
    """Number of loyalty activations this turn, defensively read."""
    for k in ("loyalty_activations_this_turn",
              "loyalty_abilities_activated_this_turn"):
        v = o.get(k)
        if isinstance(v, int):
            return v
        if isinstance(v, dict):
            try:
                return sum(int(x) for x in v.values())
            except Exception:
                pass
    return 0


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


def record_wf(state):
    wf = wf_type(state)
    if wf and (not WF_SEEN or WF_SEEN[-1][0] != wf):
        WF_SEEN.append((wf, time.time()))
        wire("waiting_for", {"type": wf, "data": wf_data(state),
                             "stage": ST.get("stage")})
        say(f"waiting_for: {wf} player={wf_player(state)} "
            f"stage={ST.get('stage')}")


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
            wire("rejected", {"who": c.name, "type": t, "data": data,
                              "stage": ST.get("stage")})
            say(f"[{c.name}] {t}: {json.dumps(data)[:300]}")
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


def untapped_of(state, pid, name):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == name and not o.get("tapped"))


async def tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    for a in acts:
        if a["type"] == "MulliganDecision":
            n_lands = sum(1 for o in hand_oids(state, pid)
                          if oname(state["objects"][o]) in LANDS)
            keep_ok = n_lands >= 2 or MULLS[c.name] >= 2
            choice = "Keep" if keep_ok else "Mulligan"
            if choice == "Mulligan":
                MULLS[c.name] += 1
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": choice}}})
            say(f"{c.name} mulligan -> {choice} (lands={n_lands})")
            return True
    for a in acts:
        if a["type"] == "SelectCards" and wf_type(state) == "MulliganDecision":
            count = 1
            for p in wf_data(state).get("pending", []):
                if p.get("player") == pid:
                    ph = p.get("phase", {}) or {}
                    if ph.get("type") == "BottomCards":
                        count = int(ph.get("count", 1))
            picks = hand_oids(state, pid)[:count]
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x) for x in picks]}})
            say(f"{c.name} bottoms {count}")
            return True
    if wf_type(state) == "DiscardToHandSize":
        pend = wf_data(state)
        if pend.get("player") == pid:
            n = pend.get("count") or max(0, len(hand_oids(state, pid)) - 7)
            h = hand_oids(state, pid)
            pref = [o for o in h if oname(state["objects"][o]) not in LANDS]
            pref += [o for o in h if o not in pref]
            picks = pref[:n]
            if picks:
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": [int(x)
                                                          for x in picks]}})
                say(f"{c.name} discards {len(picks)}")
                return True
    for a in acts:
        if "Legend" in a["type"]:
            await submit_as_is(c, a)
            say(f"{c.name} legend-choice submitted as-is: {a['type']}")
            return True
    for a in acts:
        if a["type"] == "DeclareBlockers":
            sub = copy.deepcopy(a)
            attackers = [oid for oid, o in bf(state, 1)
                         if o.get("tapped")
                         and "Creature" in str(o.get("type_line") or "")]
            if not attackers:
                attackers = [oid for oid, o in bf(state, 1)
                             if o.get("tapped") and oname(o) == "Demon"]
            blockers = [oid for oid, o in bf(state, pid)
                        if oname(o) == ANGEL and not o.get("tapped")]
            sub["data"]["assignments"] = [
                [int(blockers[i]), int(attackers[i])]
                for i in range(min(len(blockers), len(attackers)))]
            if sub["data"]["assignments"]:
                say(f"[P0] blocks {len(sub['data']['assignments'])} attackers")
            await submit_as_is(c, sub)
            return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    if state.get("active_player") == 0 \
            and (state.get("phase") or "") == "DeclareAttackers":
        for a in acts:
            if a["type"] != "DeclareAttackers":
                continue
            sub = copy.deepcopy(a)
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
            return True
    if is_my_main(state, pid):
        lid = find_hand(state, pid, PLAINS)
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
                    return True
        # cast an Angel blocker if we can spare the mana
        if untapped_of(state, pid, PLAINS) >= 5 \
                and len(bf_named(state, pid, ANGEL)) < 4:
            oid = find_hand(state, pid, ANGEL)
            a = castspell_advertised(acts, oid)
            if a:
                await submit_as_is(c, a)
                say(f"[P0] casts {ANGEL} (oid={oid})")
                return True
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


def ai_stall_signatures(log_path):
    hits = []
    try:
        with open(log_path, errors="replace") as f:
            for line in f:
                low = line.lower()
                if "halted after" in low and "failed proposals" in low:
                    hits.append(("ai_halted", line.strip()[:300]))
                if "choose_action returned none" in low \
                        and "stopping ai loop" in low:
                    hits.append(("ai_loop_stop", line.strip()[:300]))
    except FileNotFoundError:
        pass
    return hits


async def observe(state):
    """Track the AI's Ob Nixilis: casting, loyalty activation, resolution."""
    obs = bf_named(state, 0, OB)  # P0 never runs Ob; sanity
    p1_obs = bf_named(state, 1, OB)
    turn = state.get("turn_number") or 0
    if p1_obs and not ST["ob_seen"]:
        ST["ob_seen"] = True
        ST["ob_oid"] = int(p1_obs[0])
        ST["ob_seen_turn"] = turn
        ST["stage"] = "WATCH"
        o = state["objects"][str(p1_obs[0])]
        ST["loyalty_before"] = o.get("loyalty")
        ST["demon_before"] = len(bf_named(state, 1, "Demon"))
        ST["p0_life_before"] = life_of(state, 0)
        ST["p1_life_before"] = life_of(state, 1)
        say(f"Ob Nixilis on P1 BF oid={ST['ob_oid']} turn={turn} "
            f"loyalty={ST['loyalty_before']} "
            f"counter_fields={[k for k in o.keys() if 'loyalty' in k.lower()]}")
        if await export_now("pre.json") is not None:
            ST["pre_exported"] = True
    if ST["stage"] in ("WATCH", "RESOLVED") and ST["ob_oid"] is not None:
        o = state["objects"].get(str(ST["ob_oid"]))
        if o is not None and o.get("zone") == "Battlefield":
            n = loyalty_activations(o)
            loyalty_now = o.get("loyalty")
            activated = n >= 1 or (
                ST["loyalty_before"] is not None
                and loyalty_now is not None
                and loyalty_now != ST["loyalty_before"])
            if activated and not ST["activation_observed"]:
                ST["activation_observed"] = True
                ST["activation_via"] = ("counter" if n >= 1
                                        else "loyalty-delta")
                ST["activation_turn"] = turn
                ST["activation_oid"] = ST["ob_oid"]
                ST["loyalty_after"] = loyalty_now
                ST["p0_life_after"] = life_of(state, 0)
                ST["p1_life_after"] = life_of(state, 1)
                ST["demon_after"] = len(bf_named(state, 1, "Demon"))
                say(f"AI loyalty activation observed ({ST['activation_via']}): "
                    f"oid={ST['ob_oid']} turn={turn} loyalty "
                    f"{ST['loyalty_before']} -> {ST['loyalty_after']} "
                    f"p0life {ST['p0_life_before']} -> {ST['p0_life_after']} "
                    f"p1life {ST['p1_life_before']} -> {ST['p1_life_after']} "
                    f"demons {ST['demon_before']} -> {ST['demon_after']}")
                wire("ai_activation", {
                    "turn": turn, "oid": ST["ob_oid"],
                    "via": ST["activation_via"],
                    "loyalty_before": ST["loyalty_before"],
                    "loyalty_after": ST["loyalty_after"]})
            if ST["activation_observed"] and n >= 2 \
                    and turn == ST["activation_turn"]:
                ST["double_activation"] = True
                say("DOUBLE ACTIVATION: same-turn second activation on "
                    f"oid={ST['ob_oid']} turn={turn}")
                wire("double_activation", {"turn": turn,
                                            "oid": ST["ob_oid"]})
            # resolution: effect visible + stack empty after activation.
            # NOTE (driver bug fixed 20260912): the *_after values captured
            # at activation-observation time come from the pre-resolution
            # snapshot, so A3 must be evaluated from the saved states /
            # freshly re-read values here, not the stale ST snapshot.
            if ST["activation_observed"] and not ST["post_activation_exported"]:
                stack = state.get("stack") or []
                demon_now = len(bf_named(state, 1, "Demon"))
                effect_seen = (
                    demon_now > ST["demon_before"]
                    or (ST["p0_life_after"] is not None
                        and life_of(state, 0) is not None
                        and life_of(state, 0) < ST["p0_life_before"])
                    or (ST["p1_life_after"] is not None
                        and life_of(state, 1) is not None
                        and life_of(state, 1) < ST["p1_life_before"])
                    or (ST["loyalty_after"] is not None
                        and o.get("loyalty") != ST["loyalty_before"])
                )
                if effect_seen and not stack:
                    ST["stack_empty_after"] = True
                    ST["post_activation_turn"] = turn
                    ST["res_demon"] = demon_now
                    ST["res_p0_life"] = life_of(state, 0)
                    ST["res_p1_life"] = life_of(state, 1)
                    ST["res_loyalty"] = o.get("loyalty")
                    ST["res_acts"] = n
                    ST["stage"] = "RESOLVED"
                    say(f"activation resolved: stack empty, turn={turn} "
                        f"demons={demon_now} p0life={ST['res_p0_life']} "
                        f"p1life={ST['res_p1_life']} loyalty={ST['res_loyalty']}")
                    if await export_now("post_activation.json") is not None:
                        ST["post_activation_exported"] = True


async def main():
    reset()
    t0 = time.time()
    p0 = PhaseClient("P0")
    await p0.connect()
    ai_deck = deck(*P1_AI_DECK)
    await p0.create(deck(*P0_DECK),
                    ai_seats=[{"seatIndex": 1, "difficulty": "Medium",
                               "deck": {"type": "DeckList",
                                        "data": ai_deck}}])
    global C0
    C0 = p0
    say(f"game {p0.game_code}; P0 seat={p0.player_id}; P1 = native AI Medium")
    wire("game_created", {"code": p0.game_code,
                          "p0_seat": p0.player_id,
                          "p0_deck": P0_DECK, "p1_ai_deck": P1_AI_DECK})
    ST["last_rev_change"] = time.time()

    last_rev = -1
    last_tick_wall = 0
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        rej = drain_rejections(p0)
        if rej:
            ST["rejections"].extend(
                {"at": now, "who": p0.name, "type": r["type"],
                 "data": r["data"]} for r in rej)
        if now - last_tick_wall >= 5:
            last_tick_wall = now
            try:
                await tick(p0, p0.player_id)
            except Exception as e:
                say(f"tick error: {e}")
            if p0.revision != last_rev:
                ST["last_rev_change"] = now
                last_rev = p0.revision
        elif p0.revision != last_rev:
            try:
                await tick(p0, p0.player_id)
            except Exception as e:
                say(f"tick error: {e}")
            ST["last_rev_change"] = now
            last_rev = p0.revision

        st = p0.latest
        if not st:
            continue
        record_wf(st["state"])
        state = st["state"]
        turn = state.get("turn_number") or 0
        if turn >= 1:
            ST["game_started"] = True
            ST["turns_seen"].add(turn)

        if wf_type(state) == "GameOver":
            say("game over")
            ST["game_over"] = True
            ST["stop"] = True
            continue

        await observe(state)

        # stall watch: no revision progress while the game is live
        if ST["game_started"] and not ST["stop"] \
                and now - ST["last_rev_change"] > STALL_AFTER:
            ST["stall_observed"] = True
            ST["stall_wf"] = {"type": wf_type(state),
                              "player": wf_player(state)}
            say(f"STALL: no revision for {STALL_AFTER}s; "
                f"waiting_for={ST['stall_wf']}")
            wire("stall", ST["stall_wf"])
            await export_now("mid_stall.json")
            ST["stop"] = True
            continue

        # done watch: >=3 turns past the post-activation turn
        if ST["stage"] == "RESOLVED" and ST["post_activation_turn"] is not None:
            if turn >= ST["post_activation_turn"] + 3 \
                    and not (state.get("stack") or []):
                say(f"game advanced {turn - ST['post_activation_turn']} "
                    f"turns past activation; exporting post.json")
                if await export_now("post.json") is not None:
                    ST["stage"] = "DONE"
                    ST["stop"] = True
                continue

        # turn cap: AI never got there
        if turn > TURN_CAP and ST["stage"] in ("SETUP", "WATCH"):
            say(f"TURN CAP {TURN_CAP} reached without activation; stopping")
            ST["stop"] = True
            continue

    # final export for context if we stopped early
    if ST["stage"] != "DONE" and ST["ob_seen"]:
        await export_now("final.json")

    await write_run_json(p0)
    try:
        await p0.close()
    except Exception:
        pass
    say("scenario finished")


def load_env(fn):
    p = f"{EVDIR}/{fn}"
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def env_state(env):
    if not env:
        return None
    s = env.get("state")
    return json.loads(s) if isinstance(s, str) else s


async def write_run_json(p0):
    A, D = {}, {}
    pre = load_env("pre.json")
    post_act = load_env("post_activation.json")
    post = load_env("post.json")
    pre_s, post_act_s, post_s = (env_state(pre), env_state(post_act),
                                 env_state(post))

    A["A1_setup_ok"] = ("passed" if ST["ob_seen"] and ST["pre_exported"]
                        else "failed")
    D["A1_setup_ok_detail"] = (
        f"ob_on_p1_bf={ST['ob_seen']} oid={ST['ob_oid']} "
        f"turn={ST['ob_seen_turn']} pre_exported={ST['pre_exported']}")

    A["A2_ai_activation"] = ("passed" if ST["activation_observed"]
                             else ("failed" if ST["ob_seen"] else "not-run"))
    D["A2_ai_activation_detail"] = (
        f"observed={ST['activation_observed']} via={ST['activation_via']} "
        f"turn={ST['activation_turn']} oid={ST['activation_oid']} "
        f"loyalty {ST['loyalty_before']}->{ST['loyalty_after']} "
        f"demons {ST['demon_before']}->{ST['demon_after']} "
        f"p0life {ST['p0_life_before']}->{ST['p0_life_after']} "
        f"p1life {ST['p1_life_before']}->{ST['p1_life_after']}")

    # A3: effect resolved + stack later empty. Evaluated from the SAVED
    # post_activation.json state (authoritative), not the stale in-memory
    # snapshot taken at activation-observation time.
    pa_demon = len(bf_named(post_act_s, 1, "Demon")) if post_act_s else 0
    pa_p0 = life_of(post_act_s, 0) if post_act_s else None
    pa_p1 = life_of(post_act_s, 1) if post_act_s else None
    pa_loyalty = None
    pa_stack_empty = True
    if post_act_s:
        pa_stack_empty = not (post_act_s.get("stack") or [])
        for oid, o in (post_act_s.get("objects") or {}).items():
            if oname(o) == OB and o.get("zone") == "Battlefield" \
                    and o.get("controller") == 1:
                pa_loyalty = o.get("loyalty")
                break
    # -2 branch: Demon token on P1 BF and/or P1 lost 2 life; +2 branch:
    # P0 lost life. Any of these with an empty stack = resolved ability.
    effect_ok = (pa_demon > 0) or (pa_p1 is not None and pa_p1 < 20) or (
        pa_p0 is not None and pa_p0 < 20)
    A["A3_activation_resolves"] = (
        "passed" if (ST["activation_observed"]
                     and ST["post_activation_exported"]
                     and pa_stack_empty and effect_ok)
        else ("failed" if ST["activation_observed"] else "not-run"))
    D["A3_activation_resolves_detail"] = (
        f"post_activation.json: demons_p1={pa_demon} p0life={pa_p0} "
        f"p1life={pa_p1} ob_loyalty={pa_loyalty} stack_empty={pa_stack_empty} "
        f"effect_seen={effect_ok} (fresh re-read at resolution: "
        f"demons={ST.get('res_demon')} p0={ST.get('res_p0_life')} "
        f"p1={ST.get('res_p1_life')} loyalty={ST.get('res_loyalty')})")

    A["A4_no_double_activation"] = (
        "passed" if (ST["activation_observed"]
                     and not ST["double_activation"])
        else ("failed" if ST["double_activation"] else "not-run"))
    D["A4_no_double_activation_detail"] = (
        f"double_activation={ST['double_activation']} "
        f"activation_turn={ST['activation_turn']}")

    log_path = f"{BACKFILL}/runs/{RUN_ID}/server.log"
    sigs = ai_stall_signatures(log_path)
    turns_advanced = (max(ST["turns_seen"]) - ST["post_activation_turn"]
                      if ST["post_activation_turn"] is not None
                      and ST["turns_seen"] else 0)
    A["A5_no_stuck"] = (
        "passed" if (ST["post_activation_exported"]
                     and not ST["stall_observed"] and not sigs
                     and (turns_advanced >= 3 or ST["game_over"]))
        else ("failed" if (ST["activation_observed"]
                           and (ST["stall_observed"] or sigs))
              else "not-run"))
    D["A5_no_stuck_detail"] = (
        f"turns_past_activation={turns_advanced} "
        f"stall_observed={ST['stall_observed']} stall_wf={ST['stall_wf']} "
        f"ai_log_signatures={sigs} post_exported="
        f"{ST['post_activation_exported']}")

    failed = [k for k, v in A.items() if v == "failed"]
    notrun = [k for k, v in A.items() if v == "not-run"]
    if A["A2_ai_activation"] == "passed" and failed:
        verdict = "reproduced"
    elif all(v == "passed" for v in A.values()):
        verdict = "not-reproduced"
    elif not ST["ob_seen"] or not ST["activation_observed"]:
        verdict = "blocked"
    else:
        verdict = "reproduced" if failed else "not-reproduced"

    # server identity
    import hashlib
    bindir = f"{BACKFILL}/server/releases/v0.80.0"
    def sha(p):
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        return h.hexdigest()
    run = {
        "issue": 6892,
        "run_id": RUN_ID,
        "validated_version": "v0.80.0",
        "build_commit": "22cca6d",
        "protocol_version": 69,
        "server": {
            "binary_sha256": sha(bindir + "/phase-server-slim-x86_64-unknown-linux-musl"),
            "card_data_sha256": sha(bindir + "/data/card-data.json"),
            "draft_pools_sha256": sha(bindir + "/data/draft-pools.json"),
            "signature_verified": True,
            "signature_key_id": "436711b6a2d36828",
            "mode": "Full",
        },
        "game_code": p0.game_code,
        "scenario": "driver/scenario_6892.py",
        "p0_deck": P0_DECK,
        "p1_ai_deck": P1_AI_DECK,
        "p1_ai_difficulty": "Medium",
        "assertions": A,
        "assertion_details": D,
        "waiting_for_sequence": [w for w, _ in WF_SEEN],
        "verdict": verdict,
        "validated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rejections_p0": ST["rejections"],
        "mulligans_p0": MULLS.get("P0"),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    say(f"verdict={verdict} assertions={json.dumps(A)}")
    # copy scenario source into the evidence dir
    with open(__file__) as f:
        src = f.read()
    with open(f"{EVDIR}/scenario_6892.py", "w") as f:
        f.write(src)
    # server log excerpts (AI-relevant lines)
    try:
        with open(log_path, errors="replace") as f:
            lines = f.readlines()
        keep = [l for l in lines
                if any(k in l.lower() for k in
                       ("ai ", "ai_", "choose_action", "halted",
                        "planeswalker", "loyalty"))]
        with open(f"{EVDIR}/server_excerpts.log", "w") as f:
            f.writelines(keep[-400:])
    except FileNotFoundError:
        pass
    # sha256 manifest (render_summary.py adds summary.png afterwards;
    # the manifest is regenerated then)
    files = sorted(os.listdir(EVDIR))
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for fn in files:
            if fn == "manifest.sha256":
                continue
            p = f"{EVDIR}/{fn}"
            if os.path.isfile(p):
                mf.write(f"{sha(p)}  {fn}\n")
    say(f"evidence files: {files}")


if __name__ == "__main__":
    asyncio.run(main())
