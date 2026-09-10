#!/usr/bin/env python3
"""Issue #6759 follow-up: run ONLY the full-party control game (game 2).

Game 1 (no full party: A1-A4 passed, trigger correctly withheld) already
completed in scenario_6759.py. This script re-runs the g2_fullparty control
with keep-always mulligans (avoiding the mulligan-bottom path that stalled
in the first attempt) plus a stuck watchdog that logs waiting_for and the
legal-action types every 20s without progress.

On success it evaluates A5-A7, merges the verdict into run.json, and
re-renders summary.png + manifest.sha256.
"""
import asyncio
import copy
import hashlib
import json
import logging
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
EVID_RUN_ID = "20260910-6759"
EVDIR = f"{BACKFILL}/evidence/6759/{EVID_RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "a")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "a")

NALIA = "Nalia de'Arnise"
ACOLYTE = "Acolyte of Xathrid"
AVEN = "Aven Skirmisher"
AUGUR = "Augur of Skulls"
PLAINS = "Plains"
SWAMP = "Swamp"
LANDS = (PLAINS, SWAMP)
PLAN = [NALIA, ACOLYTE, AVEN, AUGUR]
WANT = {NALIA: 1, ACOLYTE: 1, AVEN: 1, AUGUR: 1}
MAIN_PHASES = ("PreCombatMain", "PostCombatMain", "Main")
GAME_TIMEOUT = 1200
TURN_CAP = 35

WF_SEEN = []


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def oname(o):
    return o.get("base_name") or o.get("name")


def bf(state, pid):
    return [o for o in state["objects"].values()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def hand_ids(state, pid):
    return [oid for oid in state["players"][pid]["hand"]]


def lname(state, oid):
    o = state["objects"].get(str(oid))
    return oname(o) if o else "?"


def n_on_bf(state, pid, name):
    return sum(1 for o in bf(state, pid) if oname(o) == name)


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and state.get("priority_player") == pid
            and state.get("phase") in MAIN_PHASES)


def wf_of(state):
    return (state.get("waiting_for") or {}).get("type")


async def submit(c, action):
    wire("action_submit", {"who": c.name, "action": action})
    await c.send_action(action)


class Ctx:
    def __init__(self):
        self.armed = False
        self.begin_combat_logged = False


def setup_ok(state, pid):
    return all(n_on_bf(state, pid, n) >= k for n, k in WANT.items())


async def tick(c, pid, ctx):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    for a in acts:
        if a["type"] == "MulliganDecision":
            await submit(c, {"type": "MulliganDecision",
                             "data": {"choice": {"type": "Keep"}}})
            say(f"{c.name} keeps")
            return True
    if wf_of(state) == "DiscardToHandSize":
        pend = (state.get("waiting_for") or {}).get("data") or {}
        if pend.get("player") != pid:
            return False
        count = pend.get("count") or max(0, len(hand_ids(state, pid)) - 7)
        ids = hand_ids(state, pid)
        picks = [int(x) for x in sorted(
            ids, key=lambda oid: (0 if lname(state, oid) in LANDS else 1,
                                   lname(state, oid)))[:count]]
        if picks:
            await submit(c, {"type": "SelectCards", "data": {"cards": picks}})
            say(f"{c.name} discards {len(picks)}")
            return True
        return False
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit(c, a)
            return True
    for a in acts:
        if a["type"] == "OrderTriggers":
            await submit(c, copy.deepcopy(a))
            say(f"{c.name} submits advertised trigger order")
            return True
    wtype = wf_of(state)
    wplayer = (state.get("waiting_for") or {}).get("data", {}).get("player")
    if wtype == "DeclareAttackers":
        # Declare no attackers so combat always advances. When armed, P0's
        # own declaration is the post-trigger capture point: do NOT submit
        # here; the main loop's done-gate exports post.json first.
        if ctx.armed and pid == 0 and state.get("active_player") == 0:
            return False
        for a in acts:
            if a["type"] == "DeclareAttackers":
                sub = copy.deepcopy(a)
                sub["data"]["attacks"] = []
                if "bands" in sub["data"]:
                    sub["data"]["bands"] = []
                await submit(c, {"type": "DeclareAttackers",
                                 "data": sub["data"]})
                say(f"{c.name} declares no attackers")
                return True
        return False
    if wtype in ("OptionalCostChoice", "TargetSelection") and wplayer == pid:
        return False
    if pid == 0 and is_my_main(state, pid):
        for lname_ in LANDS:
            hid = next((oid for oid in hand_ids(state, pid)
                        if lname(state, oid) == lname_), None)
            for a in acts:
                d = a.get("data", {})
                if (a["type"] == "PlayLand"
                        and str(d.get("object_id")) == str(hid if hid is not None else -1)):
                    await submit(c, a)
                    say(f"{c.name} plays {lname_}")
                    return True
        for name in PLAN:
            if n_on_bf(state, pid, name) >= WANT[name]:
                continue
            hid = next((oid for oid in hand_ids(state, pid)
                        if lname(state, oid) == name), None)
            if hid is None:
                continue
            for a in acts:
                d = a.get("data", {})
                if (a["type"] == "CastSpell"
                        and str(d.get("object_id")) == str(hid)):
                    await submit(c, a)
                    say(f"{c.name} casts {name}")
                    return True
    for a in acts:
        if a["type"] == "PassPriority":
            await submit(c, a)
            return True
    return False


async def main():
    t0 = time.time()
    p0 = PhaseClient("P0")
    await p0.connect()
    p1 = PhaseClient("P1")
    await p1.connect()
    say("=== Game 2 retry: full party (Nalia+Cleric+Warrior+Wizard), keep-always ===")
    await p0.create(deck((PLAINS, 20), (SWAMP, 20), (NALIA, 4),
                         (ACOLYTE, 8), (AVEN, 8), (AUGUR, 8)))
    await p1.join(p0.game_code, deck(("Island", 60)))
    ctx = Ctx()
    last_rev = {}
    last_progress = time.time()
    last_watchdog = time.time()
    post = None
    while time.time() - t0 < GAME_TIMEOUT:
        await asyncio.sleep(0.25)
        # done-gate FIRST: P0's DeclareAttackers decision after arming is the
        # post-trigger capture point (BeginCombat fully resolved by rules).
        # Export before any tick can submit the declaration.
        st = p0.latest
        if st and ctx.armed and wf_of(st["state"]) == "DeclareAttackers" \
                and st["state"].get("active_player") == 0:
            post = await p0.export_state()
            with open(f"{EVDIR}/g2_fullparty_post.json", "w") as f:
                f.write(post)
            wire("post_export", {"turn": st["state"].get("turn_number"),
                                 "stack": st["state"].get("stack")})
            say("exported g2 post.json at DeclareAttackers decision")
            break
        for c, pid in ((p0, 0), (p1, 1)):
            if c.revision == last_rev.get(c.name):
                continue
            if await tick(c, pid, ctx):
                last_rev[c.name] = c.revision
                last_progress = time.time()
        st = p0.latest
        if not st:
            continue
        state = st["state"]
        wf = wf_of(state)
        if not WF_SEEN or WF_SEEN[-1] != wf:
            WF_SEEN.append(wf)
            wire("waiting_for", {"type": wf,
                                 "phase": state.get("phase"),
                                 "turn": state.get("turn_number"),
                                 "active": state.get("active_player"),
                                 "priority": state.get("priority_player"),
                                 "data": (state.get("waiting_for") or {}).get("data")})
        if state.get("turn_number", 0) > TURN_CAP:
            say("turn cap hit; aborting"); break
        if time.time() - last_watchdog > 20:
            last_watchdog = time.time()
            idle = round(time.time() - last_progress, 1)
            if idle > 20:
                acts = st.get("legal_actions", [])
                say(f"WATCHDOG idle={idle}s wf={wf} phase={state.get('phase')} "
                    f"turn={state.get('turn_number')} active={state.get('active_player')} "
                    f"prio={state.get('priority_player')} n_acts={len(acts)} "
                    f"act_types={sorted(set(a['type'] for a in acts))[:12]}")
                wire("watchdog", {"idle_s": idle, "wf": wf,
                                  "phase": state.get("phase"),
                                  "turn": state.get("turn_number"),
                                  "act_types": sorted(set(a["type"] for a in acts))})
        if time.time() - last_progress > 120:
            say("120s without progress; aborting")
            wire("stall_abort", {"waiting_for": state.get("waiting_for"),
                                 "phase": state.get("phase")})
            break
        if (not ctx.armed and setup_ok(state, 0)
                and state.get("active_player") == 0
                and state.get("priority_player") == 0
                and state.get("phase") == "PreCombatMain"):
            s = await p0.export_state()
            with open(f"{EVDIR}/g2_fullparty_pre.json", "w") as f:
                f.write(s)
            ctx.armed = True
            wire("pre_export", {"turn": state.get("turn_number"),
                                "bf": [(oname(o), o.get("id")) for o in bf(state, 0)]})
            say(f"exported g2 pre.json (turn {state.get('turn_number')})")
        if (ctx.armed and not ctx.begin_combat_logged
                and state.get("phase") == "BeginCombat"):
            ctx.begin_combat_logged = True
            stack = [{"id": e.get("id"),
                      "effects": [((e.get("kind") or {}).get("data") or {})
                                  .get("ability", {}).get("effect", {}).get("type")]}
                     for e in (state.get("stack") or [])]
            wire("begin_combat", {"turn": state.get("turn_number"),
                                  "stack": stack,
                                  "waiting_for": state.get("waiting_for")})
            say(f"BeginCombat: stack={stack}")
        # Fallback gate: Priority wait inside DeclareAttackers phase with an
        # empty stack is also post-resolution (BeginCombat fully done).
        if (ctx.armed and state.get("phase") == "DeclareAttackers"
                and state.get("active_player") == 0
                and state.get("priority_player") == 0
                and not (state.get("stack") or [])):
            post = await p0.export_state()
            with open(f"{EVDIR}/g2_fullparty_post.json", "w") as f:
                f.write(post)
            say("exported g2 post.json (fallback priority gate)")
            break
    await p0.close(); await p1.close()

    with open(f"{EVDIR}/run.json") as f:
        run = json.load(f)
    a = run["assertions"]
    if post is None:
        for k in ("A5_counters", "A6_deathtouch", "A7_cleanup"):
            a[k] = "not-run"
        run["notes"].append("g2 retry: control game did not complete")
        verdict, reason = "blocked", "control branch did not complete on retry"
    else:
        with open(f"{EVDIR}/g2_fullparty_pre.json") as f:
            pre = json.load(f)["state"]
        st2 = json.loads(post)["state"]

        def snap(s):
            out = {}
            for o in s["objects"].values():
                if o.get("zone") == "Battlefield" and o.get("controller") == 0:
                    cnts = o.get("counters") or {}
                    out[str(o["id"])] = {
                        "name": oname(o), "p1p1": int(cnts.get("P1P1", 0)),
                        "deathtouch": "Deathtouch" in (o.get("keywords") or [])}
            return out

        c_pre, c_post = snap(pre), snap(st2)
        per = {}
        for k, v in c_post.items():
            if v["name"] in WANT:
                per.setdefault(v["name"], []).append(
                    (v["p1p1"] - c_pre.get(k, {}).get("p1p1", 0),
                     v["deathtouch"]))
        ok_c = all(n in per and all(d >= 1 for d, _ in per[n]) for n in WANT)
        ok_d = all(n in per and all(dt for _, dt in per[n]) for n in WANT)
        a["A5_counters"] = "passed" if ok_c else "failed"
        a["A6_deathtouch"] = "passed" if ok_d else "failed"
        a["A7_cleanup"] = "passed" if not (st2.get("stack") or []) else "failed"
        run["notes"].append(
            f"g2 retry: per-creature (delta_p1p1, deathtouch)={per}")
        for k in ("A5_counters", "A6_deathtouch", "A7_cleanup"):
            say(f"{k}: {a[k]}")
        if a["A1_setup_ok"] == "passed" and a["A2_no_counters"] == "passed" \
                and a["A3_no_deathtouch"] == "passed" and ok_c and ok_d:
            verdict, reason = ("not-reproduced",
                               "trigger correctly withheld with party=1 "
                               "(no counters, no deathtouch) and correctly "
                               "fired with party=4 (+1/+1 and deathtouch on "
                               "all four creatures)")
        else:
            verdict, reason = "blocked", "control branch inconclusive"
    run["verdict"] = verdict
    run["verdict_reason"] = reason
    run["elapsed_s"] = round(time.time() - t0, 1)
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say(f"VERDICT: {verdict} — {reason}")
    return 0 if verdict in ("reproduced", "not-reproduced") else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
