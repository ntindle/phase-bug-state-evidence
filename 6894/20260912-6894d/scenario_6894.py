#!/usr/bin/env python3
"""Issue #6894: AI attempts to cast Cyclonic Rift overloaded without the mana.

Reporter (Discord): "It went to the stack. Then said that it was unable to
pay but it locks the game."
Triage: the AI attempts Cyclonic Rift for its overload cost ({6}{U}) without
enough mana, puts it on the stack, then fails payment and leaves the game
locked. Acceptance criteria: (1) AI offers overload only when it can legally
complete all casting costs; (2) an unaffordable cast is rejected before the
spell is committed to the stack; (3) any failed proposal rolls back cleanly;
(4) affordable normal and overload modes still resolve correctly.

Plan (native engine, v0.80.0 / protocol 69, native Hard AI):
  P0 (human driver): Forests + Grizzly Bears (bear-dense 24/36 so the
     board goes wide). Plays a land each turn, casts bears (up to 10 on
     the battlefield), never attacks, passes priority. The wide bear
     board is the overload temptation; bounced bears return to hand and
     are recast, keeping the board wide through the late game.
  P1 (native Hard AI): 8x Cyclonic Rift + 52x Island.
  The driver OBSERVES BEFORE ACTING each iteration: a P0 priority pass
  resolves the AI's spell, so the stack must be scanned before the tick
  submits PassPriority (attempt 1 missed all 5 AI casts by observing after
  acting).

  Watch every state: Stack-zone objects named Cyclonic Rift controlled by
  P1 + matching stack entries; overload markers (stack kind context
  alternative_mana_cost_paid, resolved text "each nonland permanent" vs
  "target nonland permanent"); P1 mana (untapped Islands + pool) from the
  previous observation (pre-auto-tap); payment outcome; game advancement.

Behavioral contract:
  A1 setup_ok            Rift live in the AI's hand on a P1 main phase
                         (pre.json exported at the first such window).
  A2 overload_attempted   AI committed a Cyclonic Rift cast in overload mode.
  A3 unaffordable        at the first overload attempt P1 could not pay
                         {6}{U} (untapped Islands + mana pool < 7, read
                         pre-auto-tap).
  A4 committed_to_stack  the unaffordable overload spell was observed on
                         the stack ("it went to the stack").
  A5 no_lock             after the unaffordable attempt the game kept
                         advancing: no >180s revision stall, no AI-halt /
                         unable-to-pay signatures in server.log, stack
                         eventually empty.
  A6 affordable_control   affordable Rift cast(s) resolved and the game
                         kept advancing (passed if observed, not-run
                         otherwise).

Verdict: reproduced iff an unaffordable overload attempt reached the stack
(A2+A3+A4) or the game locked after one (A5 failed) or an affordable cast
mis-resolved (A6 failed); not-reproduced iff the reported path was
exercised and every evaluated assertion passed; blocked iff the AI never
cast Cyclonic Rift within the turn cap (reported path not exercised).
Never "fixed".
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
RUN_ID = "20260912-6894d"
EVDIR = f"{BACKFILL}/evidence/6894/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

RIFT = "Cyclonic Rift"
BEAR = "Grizzly Bears"
FOREST = "Forest"
ISLAND = "Island"
P0_DECK = [(BEAR, 24), (FOREST, 36)]
P1_AI_DECK = [(RIFT, 8), (ISLAND, 52)]
TIMEOUT = 2400
STALL_AFTER = 180  # seconds with zero revisions before declaring a stall
TURN_CAP = 40
OVERLOAD_PRICE = 7  # {6}{U}

ST = {}
MULLS = {}
WF_SEEN = []


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",            # SETUP -> WINDOW -> ATTEMPT -> DONE
        "stop": False,
        "game_started": False,
        "game_over": False,
        "turns_seen": set(),
        "window_seen": False,        # Rift in P1 hand on a P1 main phase
        "pre_exported": False,
        "any_rift_cast": False,      # AI put Rift on the stack (any mode)
        "rift_casts": [],            # records of each AI Rift cast
        "seen_spell_oids": set(),    # Stack-zone spell oids already recorded
        "overload_attempts": [],     # subset of rift_casts in overload mode
        "p1_avail_prev": None,       # (untapped_islands, pool_total)
        "post_attempt_turn": None,
        "post_exported": False,
        "stall_observed": False,
        "stall_wf": None,
        "last_rev_change": None,
        "rejections": [],
    })
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


def objs_of(state):
    return state.get("objects") or {}


def hand_oids(state, pid):
    return [str(oid) for oid, o in objs_of(state).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if oname(objs_of(state)[oid]) == name:
            return oid
    return None


def bf(state, pid):
    return [(oid, o) for oid, o in objs_of(state).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bf_named(state, pid, name):
    return [oid for oid, o in bf(state, pid) if oname(o) == name]


def life_of(state, pid):
    ps = state.get("players") or []
    if pid < len(ps):
        return ps[pid].get("life")
    return None


def untapped_islands(state, pid):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == ISLAND and not o.get("tapped"))


def untapped_forests(state, pid):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == FOREST and not o.get("tapped"))


def pool_total(state, pid):
    ps = state.get("players") or []
    if pid >= len(ps):
        return 0
    return len(((ps[pid].get("mana_pool") or {}).get("mana")) or [])


def p1_available(state):
    """(untapped_islands, pool_total) for the AI seat."""
    return untapped_islands(state, 1), pool_total(state, 1)


def p0_nonland_permanents(state):
    return sum(1 for _, o in bf(state, 0) if oname(o) != FOREST)


def is_p1_main(state):
    return (state.get("active_player") == 1
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


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


async def tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), merged_actions(st)
    for a in acts:
        if a["type"] == "MulliganDecision":
            n_lands = sum(1 for o in hand_oids(state, pid)
                          if oname(objs_of(state)[o]) == FOREST)
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
            pref = [o for o in h
                    if oname(objs_of(state)[o]) not in (FOREST,)]
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
            sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
            return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    # P0 never attacks: the bear board is the overload temptation; keep the
    # AI alive for the full observation window.
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
    if state.get("active_player") == pid \
            and (state.get("phase") or "") in ("PreCombatMain",
                                              "PostCombatMain"):
        lid = find_hand(state, pid, FOREST)
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        a.get("data", {}).get("object_id")) == lid:
                    await submit_as_is(c, a)
                    return True
        # flood bears: the overload temptation (cap keeps the board readable)
        if untapped_forests(state, pid) >= 2 \
                and len(bf_named(state, pid, BEAR)) < 10:
            oid = find_hand(state, pid, BEAR)
            if oid:
                for a in acts:
                    if a["type"] == "CastSpell" and str(
                            a.get("data", {}).get("object_id")) == str(oid):
                        await submit_as_is(c, a)
                        say(f"[P0] casts {BEAR} (oid={oid})")
                        return True
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


def stack_rift_spells(state):
    """(spell_oid, spell_obj) for Stack-zone Cyclonic Rifts controlled by P1."""
    out = []
    for oid, o in objs_of(state).items():
        if o.get("zone") == "Stack" and o.get("controller") == 1 \
                and oname(o) == RIFT:
            out.append((oid, o))
    return out


def match_stack_entry(state, spell_oid):
    """Best-effort match of a stack entry to the spell object."""
    for e in state.get("stack") or []:
        if not isinstance(e, dict):
            continue
        if str(e.get("id")) == str(spell_oid):
            return e
        if str(spell_oid) in json.dumps(e):
            return e
    return None


def overload_markers(spell_obj, entry):
    """Detect overload mode from the spell object + stack entry payloads."""
    blobs = []
    for x in (spell_obj, entry):
        if x is not None:
            blobs.append(json.dumps(x))
    flat = " ".join(blobs)
    nospace = flat.replace(" ", "")
    alt_paid = '"alternative_mana_cost_paid":true' in nospace
    each_text = "each nonland permanent" in flat
    target_text = "target nonland permanent" in flat
    overload_kw = "verload" in flat  # "Overload" in any casing/context
    return {"alternative_mana_cost_paid": alt_paid,
            "each_text": each_text, "target_text": target_text,
            "overload_mentioned": overload_kw,
            "overload": alt_paid or (each_text and not target_text)}


async def observe(state):
    turn = state.get("turn_number") or 0
    # A1 window: Rift live in the AI's hand on a P1 main phase.
    if is_p1_main(state) and not ST["window_seen"]:
        if find_hand(state, 1, RIFT) is not None:
            ST["window_seen"] = True
            ST["stage"] = "WINDOW"
            isl, pool = p1_available(state)
            say(f"window: turn={turn} Rift in P1 hand on P1 main; P1 mana "
                f"{isl} untapped Islands + {pool} pool; P0 nonland={p0_nonland_permanents(state)}")
            wire("window", {"turn": turn, "untapped_islands": isl,
                            "pool": pool,
                            "p0_nonland": p0_nonland_permanents(state)})
            if await export_now("pre.json") is not None:
                ST["pre_exported"] = True
    # AI Rift casts: Stack-zone spell objects (controller P1).
    for spell_oid, spell_obj in stack_rift_spells(state):
        if spell_oid in ST["seen_spell_oids"]:
            continue
        ST["seen_spell_oids"].add(spell_oid)
        ST["any_rift_cast"] = True
        entry = match_stack_entry(state, spell_oid)
        marks = overload_markers(spell_obj, entry)
        prev = ST["p1_avail_prev"]
        isl, pool = p1_available(state)
        if prev is None:
            prev = (isl, pool)
        affordable = (prev[0] + prev[1]) >= OVERLOAD_PRICE
        rec = {
            "n": len(ST["rift_casts"]) + 1,
            "turn": turn,
            "phase": state.get("phase"),
            "spell_oid": spell_oid,
            "entry_matched": entry is not None,
            "overload_mode": marks["overload"],
            "markers": marks,
            "avail_prev_untapped_islands": prev[0],
            "avail_prev_pool": prev[1],
            "avail_prev_total": prev[0] + prev[1],
            "affordable_overload": affordable,
            "p1_life": life_of(state, 1),
            "p0_nonland_at_cast": p0_nonland_permanents(state),
            "resolved_cleanly": None,
        }
        ST["rift_casts"].append(rec)
        say(f"AI RIFT CAST #{rec['n']}: turn={turn} phase={state.get('phase')} "
            f"oid={spell_oid} overload={marks['overload']} "
            f"mana_prev={prev[0]}+{prev[1]}={prev[0]+prev[1]} "
            f"affordable={affordable}")
        wire("ai_rift_cast", rec)
        wire("ai_rift_spell_object", {"n": rec["n"], "object": spell_obj})
        if entry is not None:
            wire("ai_rift_stack_entry", {"n": rec["n"], "entry": entry})
        if marks["overload"]:
            ST["overload_attempts"].append(rec)
            fn = f"mid_stack_{len(ST['overload_attempts'])}.json"
            await export_now(fn)
            rec["mid_file"] = fn
            if ST["stage"] in ("SETUP", "WINDOW"):
                ST["stage"] = "ATTEMPT"
                ST["post_attempt_turn"] = turn
        else:
            fn = f"mid_cast_normal_{len(ST['rift_casts'])}.json"
            await export_now(fn)
            rec["mid_file"] = fn
    # per-cast resolution: stack no longer carries the spell oid
    live_oids = {oid for oid, _ in stack_rift_spells(state)}
    for rec in ST["rift_casts"]:
        if rec["resolved_cleanly"] is None \
                and rec["spell_oid"] not in live_oids:
            # confirm via zone: spell object left the Stack
            o = objs_of(state).get(str(rec["spell_oid"]))
            if o is not None and o.get("zone") != "Stack":
                rec["resolved_cleanly"] = True
                rec["resolved_zone"] = o.get("zone")
                rec["resolved_turn"] = turn
                say(f"AI RIFT CAST #{rec['n']} resolved -> "
                    f"{o.get('zone')} (turn {turn})")
                wire("ai_rift_resolved", {"n": rec["n"],
                                          "zone": o.get("zone"),
                                          "turn": turn})
    # remember this observation's AI mana for the next attempt's pre-read
    ST["p1_avail_prev"] = p1_available(state)
    # stop 3 turns after the first UNAFFORDABLE overload attempt resolves
    # (or immediately on stall, handled by the stall watch)
    unafford = [r for r in ST["overload_attempts"]
                if not r["affordable_overload"]]
    if unafford and not ST["post_exported"]:
        first = unafford[0]
        if first.get("resolved_cleanly") and turn >= first["turn"] + 3:
            say(f"post-attempt: unaffordable overload #{first['n']} resolved "
                f"turn {first['turn']} -> {first.get('resolved_zone')}; "
                f"exporting post.json")
            if await export_now("post.json") is not None:
                ST["post_exported"] = True
                ST["stage"] = "DONE"
                ST["stop"] = True


def ai_log_signatures(log_path):
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
                if "unable to pay" in low:
                    hits.append(("unable_to_pay", line.strip()[:300]))
    except FileNotFoundError:
        pass
    return hits


async def main():
    reset()
    t0 = time.time()
    p0 = PhaseClient("P0")
    await p0.connect()
    ai_deck = deck(*P1_AI_DECK)
    await p0.create(deck(*P0_DECK),
                    ai_seats=[{"seatIndex": 1, "difficulty": "Hard",
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
        st = p0.latest
        if not st:
            continue
        # OBSERVE BEFORE ACTING: a P0 priority pass resolves the AI's
        # spell, so the stack must be scanned before the tick acts.
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

        try:
            await observe(state)
        except Exception as e:
            say(f"observe error: {e}")

        if now - last_tick_wall >= 5 or p0.revision != last_rev:
            last_tick_wall = now
            try:
                await tick(p0, p0.player_id)
            except Exception as e:
                say(f"tick error: {e}")
            if p0.revision != last_rev:
                ST["last_rev_change"] = now
                last_rev = p0.revision

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

        # turn cap
        if turn > TURN_CAP and not ST["stop"]:
            say(f"TURN CAP {TURN_CAP} reached; stopping")
            ST["stop"] = True
            continue

    if not ST["post_exported"] and ST["game_started"]:
        await export_now("post.json")
        ST["post_exported"] = True

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


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


async def write_run_json(p0):
    A, D = {}, {}
    pre = load_env("pre.json")
    post = load_env("post.json")
    pre_s, post_s = env_state(pre), env_state(post)

    A["A1_setup_ok"] = ("passed" if ST["window_seen"] and ST["pre_exported"]
                        else "failed")
    D["A1_setup_ok_detail"] = (
        f"window_seen={ST['window_seen']} pre_exported={ST['pre_exported']}")

    A["A2_overload_attempted"] = (
        "passed" if ST["overload_attempts"]
        else ("failed" if ST["any_rift_cast"] else "not-run"))
    D["A2_overload_attempted_detail"] = (
        f"overload_attempts={len(ST['overload_attempts'])} "
        f"rift_casts_total={len(ST['rift_casts'])}")

    unafford = [r for r in ST["overload_attempts"]
                if not r["affordable_overload"]]
    first_unafford = unafford[0] if unafford else None
    A["A3_unaffordable"] = (
        "passed" if first_unafford
        else ("failed" if ST["overload_attempts"] else "not-run"))
    fu_txt = (json.dumps(first_unafford, default=str)[:400]
              if first_unafford else None)
    D["A3_unaffordable_detail"] = f"first_unaffordable={fu_txt}"

    # A4: detection is via the Stack-zone spell object, so a recorded
    # unaffordable overload attempt IS the stack-commit observation.
    A["A4_committed_to_stack"] = (
        "passed" if first_unafford
        else ("failed" if (ST["overload_attempts"] and not first_unafford)
              else "not-run"))
    D["A4_committed_to_stack_detail"] = (
        "detection is via the Stack-zone spell object; "
        f"mid_file={(first_unafford or {}).get('mid_file')}")

    log_path = f"{BACKFILL}/runs/{RUN_ID}/server.log"
    sigs = ai_log_signatures(log_path)
    turns_advanced = (max(ST["turns_seen"]) - first_unafford["turn"]
                      if first_unafford and ST["turns_seen"] else 0)
    A["A5_no_lock"] = (
        "passed" if (first_unafford and not ST["stall_observed"]
                     and not sigs and first_unafford.get("resolved_cleanly")
                     and (turns_advanced >= 2 or ST["game_over"]))
        else ("failed" if (first_unafford
                           and (ST["stall_observed"] or sigs))
              else "not-run"))
    D["A5_no_lock_detail"] = (
        f"turns_past_attempt={turns_advanced} "
        f"stall_observed={ST['stall_observed']} stall_wf={ST['stall_wf']} "
        f"log_signatures={sigs} "
        f"resolved={first_unafford.get('resolved_cleanly') if first_unafford else None}")

    afford_casts = [r for r in ST["rift_casts"] if r["affordable_overload"]]
    afford_bad = [r for r in afford_casts
                  if r.get("resolved_cleanly") is False]
    last_afford_turn = max([r["turn"] for r in afford_casts]
                           or [0])
    adv_after = ((max(ST["turns_seen"]) - last_afford_turn >= 2)
                 if ST["turns_seen"] else False)
    A["A6_affordable_control"] = (
        "passed" if (afford_casts and not afford_bad
                     and not ST["stall_observed"] and adv_after)
        else ("failed" if afford_bad else "not-run"))
    D["A6_affordable_control_detail"] = (
        f"affordable_casts={len(afford_casts)} "
        f"modes={[r['overload_mode'] for r in afford_casts]} "
        f"resolved={[r.get('resolved_cleanly') for r in afford_casts]} "
        f"advanced_after={adv_after}")

    bad_attempt = all(A[k] == "passed" for k in ("A2_overload_attempted",
                                                "A3_unaffordable",
                                                "A4_committed_to_stack"))
    if not ST["any_rift_cast"]:
        verdict = "blocked"
    elif bad_attempt or A["A5_no_lock"] == "failed" \
            or A["A6_affordable_control"] == "failed":
        verdict = "reproduced"
    else:
        verdict = "not-reproduced"

    bindir = f"{BACKFILL}/server/releases/v0.80.0"
    run = {
        "issue": 6894,
        "run_id": RUN_ID,
        "validated_version": "v0.80.0",
        "server_version": "0.80.0",
        "build_commit": "22cca6d",
        "protocol_version": 69,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "server": {
            "server_version": "0.80.0",
            "build_commit": "22cca6d",
            "protocol_version": 69,
            "binary_sha256": sha(bindir + "/phase-server-slim-x86_64-unknown-linux-musl"),
            "card_data_sha256": sha(bindir + "/data/card-data.json"),
            "draft_pools_sha256": sha(bindir + "/data/draft-pools.json"),
            "signature_verified": True,
            "signature_key_id": "436711b6a2d36828",
            "mode": "Full",
        },
        "game_code": p0.game_code,
        "scenario": "driver/scenario_6894.py",
        "setup_line": ("P0 Forests+Bears floods board (never attacks); "
                       "P1 native Hard AI on 8x Cyclonic Rift + 52x Island"),
        "contract_line": ("AI must offer overload only when it can pay "
                          "{6}{U}; unaffordable casts rejected pre-stack; "
                          "no lock"),
        "limitations": [
            "Browser UI not exercised; native engine via one human-driver "
            "seat + one native AI seat.",
            "8x Rift / 12x Bears deck density is a test-harness convenience "
            "(engine accepts >4-of for custom games).",
            "AI proposals rejected server-side before the stack are not "
            "visible to the driver; only committed casts are observable.",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full game "
            "replay).",
        ],
        "p0_deck": P0_DECK,
        "p1_ai_deck": P1_AI_DECK,
        "p1_ai_difficulty": "Hard",
        "assertions": A,
        "assertion_details": D,
        "all_rift_casts": ST["rift_casts"],
        "waiting_for_sequence": [w for w, _ in WF_SEEN],
        "verdict": verdict,
        "validated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rejections_p0": ST["rejections"],
        "mulligans_p0": MULLS.get("P0"),
        "stats": {"states_seen": len(ST["turns_seen"]),
                  "trigger_observations": len(ST["rift_casts"])},
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    say(f"verdict={verdict} assertions={json.dumps(A)}")
    with open(__file__) as f:
        src = f.read()
    with open(f"{EVDIR}/scenario_6894.py", "w") as f:
        f.write(src)
    try:
        with open(log_path, errors="replace") as f:
            lines = f.readlines()
        keep = [l for l in lines
                if any(k in l.lower() for k in
                       ("ai ", "ai_", "choose_action", "halted",
                        "unable to pay", "failed proposal", "cyclonic",
                        "overload"))]
        with open(f"{EVDIR}/server_excerpts.log", "w") as f:
            f.writelines(keep[-400:])
    except FileNotFoundError:
        pass
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
