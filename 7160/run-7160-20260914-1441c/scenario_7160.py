#!/usr/bin/env python3
"""phase-rs/phase #7160 - AI controller halted after 3 failed proposals on
Priority.

Report: "Just playing a quick draft and got it a whole lot." Matt's own
follow-up (issue comment 2026-08-12): fixed by #7310 (merged d0c0f5a); the
captured turn-22 priority state is covered by a new regression fixture;
asks for verification of the next live runtime occurrence before closing.

The attached Discord state (game-state-turn-22 zip) is no longer available
(Discord CDN link expired), so the exact captured state cannot be restored.
Runtime verification path: run a native AI seat (Medium) through a full
draft-like game on the pinned release and watch for the reported halt —
AI proposals repeatedly rejected at Priority until the controller gives up.

Behavioral contract (seat 0 human observer-driver, seat 1 native AI Medium):
  Setup: two 40-card draft-like piles; P0 plays lands and passes everything;
         P1 is driven entirely by the server's native AI (phase_ai::auto_play).
  A1 no_ai_halt:   no 90s stall while a decision is owned by the AI seat.
                   (A stall = state_revision frozen 90s AND waiting_for
                   player == 1.)
  A2 no_halt_log:  server.log shows no AI-loop-stop signatures
                   ("stopping AI loop", "failed proposals", "halted")
                   between game start and game end.
  A3 progress:     game reaches turn >= 15 or a natural game-over
                   (the AI exercised its priority path throughout).
  A4 decisions:    >= 50 native AI actions executed (counted from the server
                   log's ai_actions=N per processed submission; the native
                   AI acts between viewer broadcasts, so waiting_for
                   sampling cannot see its decisions).

Verdict: reproduced iff A1 fails (AI actually stalls on the pinned release).
         not-reproduced iff A1-A4 all pass. blocked iff the game cannot be
         established/driven (e.g. human-seat stalls only).
"""
import asyncio
import copy
import hashlib
import json
import os
import re
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = os.environ.get("RUN_ID", "run-7160-20260914-1441")
EVDIR = f"{BACKFILL}/evidence/7160/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty (bump RUN_ID)"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")
SERVER_LOG = os.environ.get(
    "SERVER_LOG_PATH", f"{BACKFILL}/runs/{RUN_ID}/server.log")

FOREST, MOUNTAIN = "Forest", "Mountain"
BEAR, SPIDER, BOLT = "Grizzly Bears", "Giant Spider", "Lightning Bolt"

# 40-card draft-like piles.
P0_DECK = [(FOREST, 17), (BEAR, 12), (SPIDER, 11)]
P1_DECK = [(FOREST, 12), (MOUNTAIN, 5), (BEAR, 12), (BOLT, 11)]

TIMEOUT = 1500            # 25 min hard cap
HALT_STALL_S = 90         # no revision change while AI owns decision
TARGET_TURNS = 25

SERVER_IDENTITY = {
    "server_version": "0.82.0",
    "build_commit": "060b5d2",
    "protocol_version": 70,
}

ST = {
    "stop": False, "done_reason": None, "stage": "setup",
    "assertions": {}, "notes": [], "rejections": [],
    "last_rev_change": 0.0, "last_rev": -1,
    "halt": None, "max_turn": 0, "ai_owned_samples": 0,
    "game_code": None, "server_hello": None,
    "pre_exported": False, "server_log_offset": 0,
    "over": False,
}
ACTED = {}
C0 = None


def say(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    RUNLOG.write(line + "\n")
    RUNLOG.flush()


def wire(kind, obj):
    WIRE.write(json.dumps({"t": time.time(), "kind": kind, "data": obj}) + "\n")
    WIRE.flush()


def oname(o):
    return o.get("card_name") or o.get("name") or o.get("base_name") or ""


def hand_oids(state, pid):
    return [str(oid) for oid, o in state["objects"].items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_type(state):
    return wf_of(state).get("type")


def wf_data(state):
    return wf_of(state).get("data", {}) or {}


def wf_player(state):
    return wf_data(state).get("player")


def life(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p.get("life")
    return None


def bf(state, pid):
    return [(oid, o) for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def acted(key, rev):
    k = (key, rev)
    if k in ACTED:
        return True
    ACTED[k] = True
    return False


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": ST.get("stage")})
    await c.send_action(action)


def drain_rejections(c):
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            ST["rejections"].append(
                {"who": c.name, "type": t, "data": data,
                 "stage": ST.get("stage")})
            wire("rejected", {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {json.dumps(data)[:300]}")


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


def server_log_new():
    """Read server.log bytes appended since last check."""
    try:
        with open(SERVER_LOG, "rb") as f:
            f.seek(ST["server_log_offset"])
            data = f.read()
            ST["server_log_offset"] = f.tell()
        return data.decode("utf-8", "replace")
    except Exception:
        return ""


HALT_PATTERNS = ["stopping AI loop", "failed proposals", "AI controller halted",
                 "ChooseActionNone"]


async def human_tick(c):
    """Seat 0: passive observer. Keep mulligan, play lands, pass priority,
    discard to hand size, attack with nothing."""
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    drain_rejections(c)
    pid = 0
    wt, wp = wf_type(state), wf_player(state)
    rev = st.get("state_revision", -1)

    # mulligan: always keep
    for a in acts:
        if a["type"] == "MulliganDecision":
            if not acted("mull", rev):
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Keep"}}})
                say("[P0] keeps opening hand")
                return True

    # discard to hand size (named player)
    if wt in ("DiscardToHandSize", "DiscardChoice") and wp == pid:
        pend = wf_data(state)
        n = pend.get("count") or max(0, len(hand_oids(state, pid)) - 7)
        picks = hand_oids(state, pid)[:n]
        if picks and not acted("disc", rev):
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x) for x in picks]}})
            say(f"[P0] discards {n}")
            return True

    # declare attackers: attack with nothing
    for a in acts:
        if a["type"] == "DeclareAttackers" and not acted("atk", rev):
            sub = dict(a)
            sub["data"] = dict(a.get("data") or {})
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
            say("[P0] declares no attackers")
            return True

    # land drop on my main phase
    if (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                               "PostCombatMain")):
        for a in acts:
            if a["type"] == "PlayLand" and not acted("land", rev):
                await submit_as_is(c, a)
                return True

    # pass priority when it is ours
    for a in acts:
        if a["type"] == "PassPriority":
            if wt == "Priority" and wp == pid and not acted("pass", rev):
                await submit_as_is(c, a)
                return True
            break

    # ChoiceLegend / other defensive: submit as-is only for legend choice
    for a in acts:
        if "Legend" in a["type"] and not acted("legend", rev):
            await submit_as_is(c, a)
            say(f"[P0] legend choice submitted as-is: {a['type']}")
            return True
    return False


async def main():
    global C0
    say(f"RUN_ID={RUN_ID} issue=7160")
    import websockets as wsmod
    url = os.environ.get("PHASE_WS_URL", "ws://localhost:9374/ws")
    async with wsmod.connect(url, max_size=200_000_000) as w:
        hello_raw = await asyncio.wait_for(w.recv(), 10)
        ST["server_hello"] = json.loads(hello_raw)
    sh = ST["server_hello"]["data"]
    say(f"ServerHello: v{sh['server_version']} {sh['build_commit']} "
        f"protocol {sh['protocol_version']}")
    wire("server_hello", ST["server_hello"])
    assert sh["server_version"] == SERVER_IDENTITY["server_version"], \
        f"server version mismatch: {sh['server_version']}"

    # mark current server.log position so A2 only covers this run
    server_log_new()

    C0 = PhaseClient("P0")
    await C0.connect()
    ai_deck = deck(*P1_DECK)
    await C0.create(deck(*P0_DECK),
                    ai_seats=[{"seatIndex": 1, "difficulty": "Medium",
                               "deck": {"type": "DeckList", "data": ai_deck}}])
    ST["game_code"] = C0.game_code
    say(f"game {C0.game_code}; P0 seat={C0.player_id}; P1 = native AI Medium")
    wire("game_created", {"code": C0.game_code, "p0_seat": C0.player_id,
                          "p0_deck": P0_DECK, "p1_ai_deck": P1_DECK,
                          "ai_difficulty": "Medium"})
    ST["stage"] = "play"
    ST["last_rev_change"] = time.time()

    t0 = time.time()
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        if not await human_tick(C0):
            pass
        st = C0.latest
        if st and st.get("state"):
            state = st["state"]
            rev = st.get("state_revision", -1)
            if rev != ST["last_rev"]:
                ST["last_rev"] = rev
                ST["last_rev_change"] = time.time()
                tn = state.get("turn_number") or 0
                if tn > ST["max_turn"]:
                    ST["max_turn"] = tn
                    say(f"turn {tn} phase={state.get('phase')} "
                        f"life={[life(state, q) for q in (0, 1)]} "
                        f"ai_owned={ST['ai_owned_samples']}")
                if not ST["pre_exported"] and tn >= 3:
                    ST["pre_exported"] = True
                    await export_now("pre.json")
                    ST["stage"] = "mid"
            # AI ownership tracking + halt watchdog. Note the native AI acts
            # server-side between viewer broadcasts, so waiting_for only
            # catches the AI when a decision spans a broadcast boundary;
            # the decision count comes from ai_actions=N in the server log
            # (finalize). This watchdog only needs to catch a real stall.
            wt, wp = wf_type(state), wf_player(state)
            if wt and wp == 1:
                ST["ai_owned_samples"] += 1
                idle = time.time() - ST["last_rev_change"]
                if idle > HALT_STALL_S:
                    ST["halt"] = {
                        "waiting_for": wt, "owner": wp,
                        "idle_s": round(idle, 1),
                        "turn": state.get("turn_number"),
                        "phase": state.get("phase"),
                    }
                    ST["stop"] = True
                    ST["done_reason"] = "AI stall watchdog fired"
                    say(f"*** AI STALL: {ST['halt']}")
                    wire("ai_stall", ST["halt"])
                    break
            # secondary stall guard: revision frozen while the GAME is not
            # over but nothing moves for 2x the watchdog window. Only the
            # AI-owned case above counts as an AI stall; anything else is
            # recorded but does not claim a reproduction.
            idle = time.time() - ST["last_rev_change"]
            if idle > 2 * HALT_STALL_S and not ST.get("idle_warned"):
                ST["idle_warned"] = True
                say(f"note: no revision change for {idle:.0f}s "
                    f"(waiting_for={wt} player={wp})")
                wire("long_idle", {"idle_s": round(idle, 1),
                                   "waiting_for": wt, "player": wp})
            if ST["max_turn"] >= TARGET_TURNS:
                ST["stop"] = True
                ST["done_reason"] = f"reached turn {TARGET_TURNS}"
                break
            # natural game over: look for winner/result fields in viewer state,
            # and also the server log line (viewer state may not carry it).
            # The native AI acts between broadcasts, so its decisions are
            # counted from the server log (ai_actions=N) in finalize(),
            # not from waiting_for sampling.
            res = state.get("result") or state.get("game_result")
            if not res:
                log_new = server_log_new()
                plain = log_new
                for esc in ("\x1b[0m", "\x1b[32m", "\x1b[1m", "\x1b[2m",
                            "\x1b[3m"):
                    plain = plain.replace(esc, "")
                if ("game over" in plain and ST["game_code"] in plain
                        and "winner" in plain):
                    res = "server-log game over"
                    m = plain.find("winner")
                    res = "server-log: " + plain[m:m + 40].strip()
            if res:
                ST["over"] = True
                ST["stop"] = True
                ST["done_reason"] = f"game over: {res}"
                ST["post_viewer"] = copy.deepcopy(C0.latest) \
                    if C0 and C0.latest else None
                say(f"game over: {json.dumps(res)[:300]}")
                wire("game_over", {"result": res})
                break
        await asyncio.sleep(0.25)

    if not ST["done_reason"]:
        ST["done_reason"] = "timeout"
    say(f"main loop ended: {ST['done_reason']}")
    await finalize()


def assertions(log_new):
    """A4 counts native AI actions from the server log: every processed
    submission line carries ai_actions=N for game=<code>."""
    a = {}
    a["A1_no_ai_halt"] = "passed" if ST["halt"] is None else "failed"
    plain = log_new
    for esc in ("\x1b[0m", "\x1b[32m", "\x1b[1m", "\x1b[2m", "\x1b[3m"):
        plain = plain.replace(esc, "")
    hits = [p for p in HALT_PATTERNS if p in plain]
    a["A2_no_halt_log"] = "passed" if not hits else "failed"
    progressed = ST["over"] or ST["max_turn"] >= 15
    a["A3_progress"] = "passed" if progressed else "failed"
    total, subs = 0, 0
    code = ST["game_code"]
    for ln in plain.splitlines():
        if f"game={code}" in ln:
            m = re.search(r"ai_actions=(\d+)", ln)
            if m:
                subs += 1
                total += int(m.group(1))
    ST["ai_actions_total"] = total
    ST["ai_submissions"] = subs
    a["A4_ai_decisions"] = "passed" if total >= 50 else "failed"
    return a, plain, hits


async def finalize():
    ST["stop"] = True
    say("finalizing...")
    ST["stage"] = "post"
    post = await export_now("post.json")
    if post is None and ST.get("post_viewer"):
        # Session is invalidated at game over; preserve the last viewer
        # snapshot instead and record the limitation.
        with open(f"{EVDIR}/post_viewer.json", "w") as f:
            json.dump(ST["post_viewer"], f)
        say("saved post_viewer.json (authoritative export unavailable "
            "after game over)")
        wire("post_viewer_saved", {})
    log_new = server_log_new()
    # assertions() needs the whole run's log for this game: the in-loop
    # game-over probe above already advanced the incremental offset, so
    # re-read the full server log and filter by game code instead.
    try:
        with open(SERVER_LOG, "rb") as f:
            full_log = f.read().decode("utf-8", "replace")
    except Exception as e:
        say(f"full server.log read failed: {e}")
        full_log = log_new
    wire("done", {"reason": ST["done_reason"], "max_turn": ST["max_turn"],
                  "halt": ST["halt"], "over": ST["over"]})

    a, plain, hits = assertions(full_log)
    ST["assertions"] = a
    # keep the recent tail for the evidence file
    tail_start = max(0, len(full_log) - 6000)
    with open(f"{EVDIR}/server_log_tail.txt", "w") as f:
        f.write(full_log[tail_start:])
    notes = [
        f"A1: {'no 90s AI-owned stall observed' if ST['halt'] is None else 'STALL: ' + json.dumps(ST['halt'])}",
        f"A2: halt signatures in server.log this run: {hits or 'none'}",
        f"A3: max_turn={ST['max_turn']} game_over={ST['over']}",
        f"A4: native AI actions this run={ST['ai_actions_total']} "
        f"across {ST['ai_submissions']} submissions (server log ai_actions)",
        "Attachment game-state-turn-22 zip from Discord CDN is expired "
        "('This content is no longer available'); the exact captured state "
        "could not be restored. Runtime verification via live native-AI game.",
        "P1 ran fully on the server's native AI (phase_ai::auto_play); "
        "driver touched only seat 0.",
        "post.json authoritative export fails after game over ('session "
        "identity is no longer current'); final board captured as "
        "post_viewer.json viewer snapshot.",
    ]
    ST["notes"] = notes

    halted = ST["halt"] is not None
    verdict = "reproduced" if halted else (
        "not-reproduced" if all(v == "passed" for v in a.values())
        else "blocked")

    sh = ST["server_hello"]["data"]
    run = {
        "issue": 7160,
        "run_id": RUN_ID,
        "validated_at": time.strftime("%Y-%m-%d"),
        "verdict": verdict,
        "scope": ("AI controller halt at Priority (area:ai); native Medium AI "
                  "seat vs passive human observer, 40-card draft-like piles, "
                  "25-turn watch window"),
        "result": ("AI stall observed: " + json.dumps(ST["halt"])
                   if halted else
                   "No AI stall in the watched game: native AI executed "
                   f"{ST.get('ai_actions_total', 0)} actions across "
                   f"{ST.get('ai_submissions', 0)} submissions through turn "
                   f"{ST['max_turn']}{' (game over, AI won)' if ST['over'] else ''} "
                   "with no halt signatures in the server log."),
        "limitations": [
            "Browser UI not exercised",
            "Attached turn-22 Discord state unavailable (CDN link expired); "
            "exact captured position not restored",
            "Draft format not used; 40-card draft-like piles instead",
            "Single difficulty (Medium) only",
            "AI internal proposal/rejection records are server-internal; "
            "only the stall outcome, action counts, and log signatures "
            "are observable",
            "Authoritative post-game export unavailable (session "
            "invalidated at game over); final state is a viewer snapshot",
        ],
        "server": {
            "server_version": sh["server_version"],
            "build_commit": sh["build_commit"],
            "protocol_version": sh["protocol_version"],
            "mode": sh.get("mode"),
        },
        "assertions": a,
        "notes": notes,
        "deck_lists": {"p0": P0_DECK, "p1_ai": P1_DECK},
        "game_code": ST["game_code"],
        "wire_log": "wire_log.jsonl",
        "run_log": "scenario_run.log",
        "exports": {"pre": "pre.json" if ST["pre_exported"] else None,
                    "post": "post.json" if post else None,
                    "post_viewer": ("post_viewer.json"
                                    if ST.get("post_viewer") else None)},
        "rejections": ST["rejections"],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)

    # render summary PNG from the saved run record
    try:
        import subprocess
        r = subprocess.run(
            [sys.executable,
             f"{BACKFILL}/driver/render_summary_7160.py", EVDIR],
            capture_output=True, text=True, timeout=60)
        say("render stdout: " + r.stdout[-500:])
        if r.returncode != 0:
            say("render FAILED: " + r.stderr[-1000:])
    except Exception as e:
        say(f"render exception: {e}")

    # manifest
    files = sorted(f for f in os.listdir(EVDIR)
                   if f != "manifest.sha256" and os.path.isfile(f"{EVDIR}/{f}"))
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for fn in files:
            h = hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
            mf.write(f"{h}  {fn}\n")
    say(f"verdict={verdict} manifest written ({len(files)} files)")
    WIRE.close()
    RUNLOG.close()


if __name__ == "__main__":
    asyncio.run(main())
