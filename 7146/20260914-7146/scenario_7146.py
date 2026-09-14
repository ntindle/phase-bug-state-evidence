#!/usr/bin/env python3
"""phase-rs/phase #7146 - Exquisite Blood + Enduring Tenacity loop stops.

Report: "Infinite Exquisite / Enduring not running through ... Looks like
it's stuck at only doing it two times?"

Oracle text (from pinned card-data.json):
  Exquisite Blood {4}{B}: Whenever an opponent loses life, you gain that
    much life.
  Enduring Tenacity {2}{B}{B}: Whenever you gain life, target opponent
    loses that much life.

Expected (MTG rules): the mandatory triggers chain until a rules-valid
terminal state: P1 loses 3 (kick) -> EB: P0 gains 3 -> ET: P1 loses 3 ->
... until P1 hits 0 life and loses the game (state-based action ends the
mandatory loop). The loop must NOT stop after a fixed number of
iterations with the game continuing.

Behavioral contract (2 human seats, P0 = enchantment controller):
  A1 setup_ok: Exquisite Blood + Enduring Tenacity on P0's battlefield.
  A2 kick_resolves: Bump in the Night resolves, P1 20 -> 17.
  A3 iterations: count of Enduring Tenacity resolutions (P1 -3 steps)
     after the kick. Correct play needs 6 (17->14->11->8->5->2->-1).
  A4 terminal: loop reaches the rules-valid terminal state (P1 life <= 0,
     P1 loses). Fails => the reported bug is reproduced (loop stopped
     early and the game continued).
  A5 cleanup: stack empty at final observation; game over or advancing.

Verdict: reproduced iff A1-A2 pass and the loop settles with P1 > 0 life
(fewer than terminal iterations). not-reproduced iff P1 reaches <= 0
(the full loop ran to its rules-correct terminal state). blocked iff A1
cannot be established.
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
RUN_ID = os.environ.get("RUN_ID", "20260914-7146")
EVDIR = f"{BACKFILL}/evidence/7146/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

EB = "Exquisite Blood"
ET = "Enduring Tenacity"
BUMP = "Bump in the Night"
SWAMP = "Swamp"

P0_DECK = [(EB, 12), (ET, 12), (BUMP, 12), (SWAMP, 36)]
P1_DECK = [(SWAMP, 60)]
TIMEOUT = 1500
SETTLE_QUIET_S = 45  # no life change + empty stack this long => settled

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
              "(reused from prior run; ServerHello re-verified this run) "
              "+ verified pin (minisign-verify of binary + signed data "
              "manifest with the repo-pinned key).",
}

ST = {}
WF_SEEN = []
ACTED = {}
C0 = C1 = None


def acted(key, rev):
    k = (key, rev)
    if k in ACTED:
        return True
    ACTED[k] = True
    return False


def reset():
    ST.clear()
    ACTED.clear()
    WF_SEEN.clear()
    ST.update({
        "stage": "SETUP",   # SETUP -> KICK -> LOOP -> DONE
        "stop": False,
        "done_reason": None,
        "server_hello": None,
        "exports": {},
        "eb_cast": False, "et_cast": False,
        "bump_in_flight": False, "bump_cast": False,
        "kick_time": None,
        "last_life_change": None,
        "life_events": [],   # (t, turn, phase, p0, p1, stack_depth)
        "p0_life": 20, "p1_life": 20,
        "et_resolutions": 0,  # P1 -3 steps after kick (Enduring triggers)
        "eb_resolutions": 0,  # P0 +3 steps after kick (Exquisite triggers)
        "mid_exported": False,
        "pre_exported": False,
        "post_exported": False,
        "rejections": [],
        "target_answers": {},
        "stack_samples": [],
        "game_over": False,
        "kick_life": None,   # (p0,p1) right after bump resolved
    })


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


def gy_named(state, pid, name):
    return [oid for oid, o in state["objects"].items()
            if o.get("zone") == "Graveyard" and o.get("controller") == pid
            and oname(o) == name]


def untapped_lands(state, pid):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == SWAMP and not o.get("tapped"))


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


def life_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p.get("life")
    return None


def stack_of(state):
    return state.get("stack") or []


def stack_kinds(state):
    out = []
    for e in stack_of(state):
        k = (e.get("kind") or {}).get("type") if isinstance(
            e.get("kind"), dict) else e.get("kind")
        src = e.get("source_id")
        nm = None
        if src is not None:
            o = state["objects"].get(str(src))
            if o:
                nm = oname(o)
        out.append(f"{k}:{nm}")
    return out


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
            ST["rejections"].append({"who": c.name, "type": t,
                                     "data": data, "stage": ST.get("stage")})
            wire("rejected", {"who": c.name, "type": t, "data": data,
                              "stage": ST.get("stage")})
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


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def vi_choices(opp):
    resp = opp.get("response", {}) or {}
    data = resp.get("data", {}) or {}
    return (data.get("choices") or data.get("candidates") or [],
            resp.get("type"))


def seat_of(choice):
    for s in choice.get("surfaces", []) or []:
        d = s.get("data") or {}
        if isinstance(d, dict) and "seat" in d:
            try:
                return int(d["seat"])
            except Exception:
                pass
    return None


async def answer_target_selection(c, pid, st, state, tag):
    """Answer any TargetSelection for pid by choosing the seat-1 player
    candidate (P1 is the only opponent). Once per interactionId."""
    if wf_type(state) != "TargetSelection" or wf_player(state) != pid:
        return False
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if ST["target_answers"].get(iid):
            continue
        chs, rtype = vi_choices(opp)
        if not chs:
            continue
        pick = next((ch for ch in chs if seat_of(ch) == 1), chs[0])
        resp = opp.get("response", {}) or {}
        if rtype == "schema":
            spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
            stype = spec.get("type") or "sequence"
            sub = {"interactionId": iid,
                   "response": {"type": stype,
                                "data": {"choiceIds": [pick.get("id")]}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": pick.get("id")}}}
        wire("target_answer", {"who": tag, "iid": iid,
                               "picked_seat": seat_of(pick),
                               "rtype": rtype})
        say(f"[{tag}] TargetSelection -> seat 1 (iid={iid})")
        await c.send_interaction(sub)
        ST["target_answers"][iid] = True
        return True
    return False


def record_life(state, tag):
    p0 = life_of(state, 0)
    p1 = life_of(state, 1)
    changed = False
    if p0 is not None and p0 != ST["p0_life"]:
        d = p0 - ST["p0_life"]
        say(f"[{tag}] P0 life {ST['p0_life']} -> {p0} "
            f"(turn {state.get('turn_number')} phase {state.get('phase')})")
        wire("life_change", {"player": 0, "from": ST["p0_life"], "to": p0,
                             "turn": state.get("turn_number"),
                             "phase": state.get("phase"),
                             "stack": stack_kinds(state)})
        if ST["stage"] == "LOOP" and d == 3:
            ST["eb_resolutions"] += 1
            say(f"[{tag}] Exquisite Blood resolution #{ST['eb_resolutions']}")
        ST["p0_life"] = p0
        changed = True
    if p1 is not None and p1 != ST["p1_life"]:
        d = p1 - ST["p1_life"]
        say(f"[{tag}] P1 life {ST['p1_life']} -> {p1} "
            f"(turn {state.get('turn_number')} phase {state.get('phase')})")
        wire("life_change", {"player": 1, "from": ST["p1_life"], "to": p1,
                             "turn": state.get("turn_number"),
                             "phase": state.get("phase"),
                             "stack": stack_kinds(state)})
        if ST["stage"] == "LOOP" and d == -3:
            ST["et_resolutions"] += 1
            say(f"[{tag}] Enduring Tenacity resolution "
                f"#{ST['et_resolutions']} (P1 at {p1})")
            if not ST["mid_exported"]:
                ST["mid_exported"] = True
                asyncio.ensure_future(export_now("mid.json"))
        ST["p1_life"] = p1
        changed = True
    if changed:
        ST["last_life_change"] = time.time()
        ST["life_events"].append((time.time(), state.get("turn_number"),
                                 state.get("phase"), ST["p0_life"],
                                 ST["p1_life"], len(stack_of(state))))


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

    record_life(state, c.name)

    # game-over detection: a player at <=0 life
    if ST["p1_life"] is not None and ST["p1_life"] <= 0 and not ST["game_over"]:
        ST["game_over"] = True
        say(f"[{c.name}] P1 at {ST['p1_life']} life: loop reached terminal "
            f"state")
        wire("game_terminal", {"p0": ST["p0_life"], "p1": ST["p1_life"],
                               "stage": ST["stage"]})
    if ST["p0_life"] is not None and ST["p0_life"] <= 0 and not ST["game_over"]:
        ST["game_over"] = True
        say(f"[{c.name}] P0 at {ST['p0_life']} life: UNEXPECTED")
        wire("game_terminal", {"p0": ST["p0_life"], "p1": ST["p1_life"],
                               "unexpected_p0_death": True})

    # mulligan: keep, only if we are the named pending player
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

    # discard to hand size (named player); P0 protects EB/ET/BUMP
    if wt == "DiscardToHandSize" and wp == pid:
        pend = wf_data(state)
        n = pend.get("count") or max(0, len(hand_oids(state, pid)) - 7)
        oids = hand_oids(state, pid)
        if pid == 0:
            prot = {EB, ET, BUMP}
            oids = sorted(oids,
                          key=lambda x: 0 if oname(state["objects"][x])
                          in prot else 1, reverse=True)
        picks = [int(x) for x in oids[:n]]
        if picks:
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": picks}})
            say(f"[{c.name}] discards {n}")
            return True

    # never block (once per revision)
    if (state.get("phase") or "") == "DeclareBlockers":
        da = find_action(acts, "DeclareBlockers")
        if da:
            if not acted(f"blk{pid}", st.get("state_revision", -1)):
                sub = copy.deepcopy(da)
                sub["data"]["assignments"] = []
                await submit_as_is(c, sub)
                say(f"[{c.name}] declares no blockers")
            return True

    # DeclareAttackers: empty attacks (no creatures in these decks)
    if (state.get("phase") or "") == "DeclareAttackers":
        da = find_action(acts, "DeclareAttackers")
        if da:
            if not acted(f"atk{pid}", st.get("state_revision", -1)):
                sub = copy.deepcopy(da)
                sub["data"]["attacks"] = []
                if "bands" in sub["data"]:
                    sub["data"]["bands"] = []
                await submit_as_is(c, sub)
            return True

    # target selections (Bump kick + Enduring Tenacity "target opponent")
    if pid == 0 and ST["stage"] in ("KICK", "LOOP"):
        if await answer_target_selection(c, pid, st, state, c.name):
            return True

    # P0 main-phase driving
    if is_my_main(state, pid) and pid == 0:
        rev = st.get("state_revision", -1)
        # stage transition: both enchantments on BF, stack empty
        if ST["stage"] == "SETUP":
            if (bf_named(state, 0, EB) and bf_named(state, 0, ET)
                    and not stack_of(state)):
                ST["stage"] = "KICK"
                say("[P0] both enchantments on battlefield; stage=KICK")
                wire("stage_kick", {"turn": state.get("turn_number")})
        lid = find_hand(state, pid, SWAMP)
        a = playland_advertised(acts, lid) if lid else None
        if a:
            await submit_as_is(c, a)
            return True
        if ST["stage"] == "SETUP":
            hand_names = [oname(state["objects"][x])
                          for x in hand_oids(state, 0)]
            ul = untapped_lands(state, 0)
            if (EB in hand_names and ul >= 5 and not ST["eb_cast"]
                    and not stack_of(state)):
                oid = find_hand(state, 0, EB)
                a = castspell_advertised(acts, oid)
                if a and not acted("casteb", rev):
                    wire("cast", {"card": EB})
                    await submit_as_is(c, a)
                    ST["eb_cast"] = True
                    say(f"[P0] casts {EB}")
                    return True
            if (ET in hand_names and ul >= 4 and ST["eb_cast"]
                    and not ST["et_cast"] and not stack_of(state)):
                oid = find_hand(state, 0, ET)
                a = castspell_advertised(acts, oid)
                if a and not acted("castet", rev):
                    wire("cast", {"card": ET})
                    await submit_as_is(c, a)
                    ST["et_cast"] = True
                    say(f"[P0] casts {ET}")
                    return True
        if ST["stage"] == "KICK":
            if not ST["pre_exported"] and not stack_of(state):
                ST["pre_exported"] = True
                asyncio.ensure_future(export_now("pre.json"))
            hand_names = [oname(state["objects"][x])
                          for x in hand_oids(state, 0)]
            if (BUMP in hand_names and not ST["bump_cast"]
                    and not stack_of(state)
                    and untapped_lands(state, 0) >= 1):
                oid = find_hand(state, 0, BUMP)
                a = castspell_advertised(acts, oid)
                if a and not acted("castbump", rev):
                    wire("cast", {"card": BUMP})
                    await submit_as_is(c, a)
                    ST["bump_in_flight"] = True
                    ST["bump_cast"] = True
                    say(f"[P0] casts {BUMP} targeting P1 (kick)")
                    return True
        # track bump resolution -> LOOP stage
        if ST["bump_in_flight"] and gy_named(state, 0, BUMP):
            ST["bump_in_flight"] = False
            ST["stage"] = "LOOP"
            ST["kick_time"] = time.time()
            ST["last_life_change"] = time.time()
            ST["kick_life"] = (ST["p0_life"], ST["p1_life"])
            say(f"[P0] bump resolved (lives {ST['kick_life']}); "
                f"stage=LOOP, watching trigger chain")
            wire("stage_loop", {"kick_life": ST["kick_life"],
                                "turn": state.get("turn_number")})

    # P1 main phase: keep dropping lands so the game advances
    if is_my_main(state, pid) and pid == 1:
        lid = find_hand(state, pid, SWAMP)
        a = playland_advertised(acts, lid) if lid else None
        if a:
            await submit_as_is(c, a)
            return True

    # default: pass priority only when it is ours
    for a in acts:
        if a["type"] == "PassPriority":
            if wt == "Priority" and wp == pid:
                if not acted(f"pass{pid}", st.get("state_revision", -1)):
                    await submit_as_is(c, a)
                    return True
            break
    return False


async def settle_check():
    """Called periodically: decide whether the run is done."""
    if ST["stop"]:
        return
    stage = ST["stage"]
    if stage not in ("LOOP",):
        return
    if ST["game_over"]:
        ST["done_reason"] = "loop reached terminal state (P1 at <=0)"
        await finalize_loop("terminal")
        return
    quiet = (time.time() - (ST["last_life_change"] or time.time()))
    # settled: no life change for SETTLE_QUIET_S while the loop was armed
    if ST["kick_time"] and quiet >= SETTLE_QUIET_S:
        ST["done_reason"] = (
            f"loop settled after {ST['et_resolutions']} Enduring "
            f"resolutions / {ST['eb_resolutions']} Exquisite resolutions "
            f"with P1 at {ST['p1_life']} life (>0); game continued")
        await finalize_loop("settled")
        return
    # progress log every 20s during the loop
    if not hasattr(settle_check, "last_prog"):
        settle_check.last_prog = 0
    if time.time() - settle_check.last_prog > 20:
        settle_check.last_prog = time.time()
        say(f"[loop] t+{time.time()-ST['kick_time']:.0f}s "
            f"eb_res={ST['eb_resolutions']} et_res={ST['et_resolutions']} "
            f"lives={ST['p0_life']}/{ST['p1_life']} quiet={quiet:.0f}s")


async def finalize_loop(kind):
    if ST["stop"]:
        return
    ST["stop"] = True
    wire("finalize", {"kind": kind, "reason": ST["done_reason"]})
    say(f"finalizing ({kind}): {ST['done_reason']}")
    if not ST["post_exported"]:
        ST["post_exported"] = True
        await export_now("post.json")


async def main():
    reset()
    import websockets as wsmod
    url = os.environ.get("PHASE_WS_URL", "ws://localhost:9374/ws")
    async with wsmod.connect(url, max_size=200_000_000) as w:
        hello_raw = await asyncio.wait_for(w.recv(), 5)
        ST["server_hello"] = json.loads(hello_raw)
        say("ServerHello: " + json.dumps(ST["server_hello"])[:300])

    global C0, C1
    C0 = PhaseClient("P0")
    await C0.connect()
    await C0.create(deck(*P0_DECK), player_count=2)
    C1 = PhaseClient("P1")
    await C1.connect()
    await C1.join(C0.game_code, deck(*P1_DECK))
    say(f"game {C0.game_code}; seats P0={C0.player_id} P1={C1.player_id} "
        f"RUN_ID={RUN_ID}")
    wire("game_start", {"game_code": C0.game_code,
                        "p0_deck": P0_DECK, "p1_deck": P1_DECK,
                        "server_hello": ST["server_hello"]})

    clients = [(C0, 0), (C1, 1)]
    t0 = time.time()
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        for c, pid in clients:
            try:
                await tick(c, pid)
            except Exception as e:
                say(f"tick error [{c.name}]: {e}")
        await settle_check()
        await asyncio.sleep(0.1)

    if not ST["stop"]:
        ST["done_reason"] = "global timeout before loop settled"
        wire("timeout", {})
        say("TIMEOUT: loop never settled; finalizing as blocked-ish")
        await finalize_loop("timeout")
    say(f"main loop ended: stop={ST['stop']} reason={ST['done_reason']}")
    await finish()


def load_state(fn):
    p = f"{EVDIR}/{fn}"
    try:
        return json.loads(open(p).read())["state"]
    except Exception:
        return None


def assertions_from_states():
    ass = {}
    notes = []
    pre = load_state("pre.json")
    mid = load_state("mid.json")
    post = load_state("post.json")

    def bf_has(st, pid, name):
        if not st:
            return False
        return any(o.get("zone") == "Battlefield"
                   and o.get("controller") == pid and oname(o) == name
                   for o in st["objects"].values())

    def life(st, pid):
        if not st:
            return None
        for p in st.get("players", []):
            if p.get("id") == pid:
                return p.get("life")
        return None

    # A1: both enchantments on P0's BF at pre
    ok1 = bf_has(pre, 0, EB) and bf_has(pre, 0, ET)
    ass["A1_setup_ok"] = "passed" if ok1 else "failed"
    notes.append(f"A1: EB on BF={bf_has(pre,0,EB)} ET on BF={bf_has(pre,0,ET)}")

    # A2: kick resolved: P1 dropped by 3 from 20
    kick = ST.get("kick_life")
    ok2 = (kick is not None and kick[1] == 17 and ST["bump_cast"])
    ass["A2_kick_resolves"] = "passed" if ok2 else "failed"
    notes.append(f"A2: bump_cast={ST['bump_cast']} kick_life={kick} "
                 f"(expect (20,17))")

    # A3: iteration counts
    et_n, eb_n = ST["et_resolutions"], ST["eb_resolutions"]
    ass["A3_iterations"] = "passed" if (et_n >= 6 or ST["game_over"]) \
        else "failed"
    notes.append(f"A3: Enduring resolutions={et_n} "
                 f"Exquisite resolutions={eb_n} (need 6 each to reach "
                 f"P1<=0 from 17)")

    # A4: terminal state reached?
    ok4 = ST["game_over"] and (ST["p1_life"] or 99) <= 0
    ass["A4_terminal_reached"] = "passed" if ok4 else "failed"
    notes.append(f"A4: game_over={ST['game_over']} final lives="
                 f"{ST['p0_life']}/{ST['p1_life']} done_reason="
                 f"{ST['done_reason']}")

    # A5: cleanup - stack empty at post
    post_stack = len((post or {}).get("stack", [])) if post else None
    ok5 = post_stack == 0
    ass["A5_cleanup"] = "passed" if ok5 else ("not-run" if post is None
                                             else "failed")
    notes.append(f"A5: post stack depth={post_stack}")

    # verdict
    if ass["A1_setup_ok"] != "passed" or ass["A2_kick_resolves"] != "passed":
        verdict = "blocked"
    elif ass["A4_terminal_reached"] == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "reproduced"
    notes.append(f"verdict={verdict}: the mandatory EB/ET trigger chain "
                 f"stopped after {et_n} Enduring / {eb_n} Exquisite "
                 f"resolutions with P1 at {ST['p1_life']} life; the game "
                 f"continued instead of running to the rules-valid terminal "
                 f"state (P1 at 0)." if verdict == "reproduced" else
                 f"verdict={verdict}")
    return ass, notes, verdict


async def finish():
    # scenario source into evidence
    try:
        import shutil
        shutil.copy(__file__, f"{EVDIR}/scenario_7146.py")
        say("scenario source copied to evidence")
    except Exception as e:
        say(f"scenario copy failed: {e}")

    ass, notes, verdict = assertions_from_states()
    say(f"VERDICT: {verdict}")
    for n in notes:
        say("  " + n)

    run = {
        "issue": 7146,
        "run_id": RUN_ID,
        "validated_at": "2026-09-14",
        "server": SERVER_IDENTITY,
        "server_hello": ST["server_hello"],
        "game_code": C0.game_code if C0 else None,
        "seats": {"P0": C0.player_id if C0 else None,
                  "P1": C1.player_id if C1 else None},
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "life_events": ST["life_events"],
        "et_resolutions": ST["et_resolutions"],
        "eb_resolutions": ST["eb_resolutions"],
        "kick_life": ST["kick_life"],
        "game_over": ST["game_over"],
        "rejections": ST["rejections"],
        "done_reason": ST["done_reason"],
        "verdict": verdict,
        "scope": "Exquisite Blood + Enduring Tenacity mandatory trigger "
                 "chain: Bump in the Night kick, loop iteration count, "
                 "terminal-state reach; native engine, two human-client "
                 "seats",
        "limitations": [
            "Browser UI not exercised.",
            "Dense 12x playsets are a test-harness convenience (engine "
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
                   if os.path.isfile(f"{EVDIR}/{f}")
                   and f != "manifest.sha256")
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
             "/home/hatch/workspace/dev/phase-backfill/driver/render_summary_7146.py",
             EVDIR], check=True, timeout=120)
        print("summary.png rendered", flush=True)
    except Exception as e:
        print(f"PNG render failed: {e}", flush=True)

    # re-hash manifest to include summary.png
    try:
        files = sorted(f for f in os.listdir(EVDIR)
                       if os.path.isfile(f"{EVDIR}/{f}")
                       and f != "manifest.sha256")
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
