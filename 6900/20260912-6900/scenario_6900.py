#!/usr/bin/env python3
"""Issue #6900: Promise of Loyalty "does nothing".

Oracle: "Each player puts a vow counter on a creature they control and
sacrifices the rest. Each of those creatures can't attack you or
planeswalkers you control for as long as it has a vow counter on it."

Reported (Discord, via issue #6900 + maintainer note 2026-08-22): "Does
nothing. It should mark a creature for each opponent and the marked
creature cannot attack you. All other creatures opponents control should
be sacrificed." Both halves reported broken: no vow-counter prompt, and
the sacrifice-the-rest clause does not fire.

Card-data state (v0.81.1 pinned dataset): the card carries NO abilities;
its only parse is a malformed CantAttack static entry affecting SelfRef,
with a SwallowedClause parse warning. The vow-counter choice and the
sacrifice-the-rest instructions are absent from the parse. Classifier
verdict: unsupported_aspect.

Behavioral contract (native engine v0.81.1 / protocol 70, two human
seats): P0 and P1 each field two Savannah Lions; P0 holds Promise of
Loyalty ({4}{W}). On a P0 PreCombatMain with 5+ untapped Plains, P0 casts
Promise of Loyalty. pre.json is exported just before the cast; the full
resolution window is scanned for any vow-counter / sacrifice choice
prompt offered to either player; post.json is exported once the spell
reaches P0's graveyard.

  A1 setup_ok            2 Lions each side; Promise of Loyalty in P0 hand
                         at the cast tick; P0 main phase.
  A2 cast_completes      CastSpell advertised (or a manual submission
                         accepted); Promise reaches P0 graveyard.
  A3 no_vow_counter_prompt  no counter-placement / sacrifice-choice
                         opportunity was offered to either player during
                         the resolution window. "passed" = the reported
                         symptom (no prompt) was observed.
  A4 no_sacrifice        all 4 pre-cast Lions still on the battlefield in
                         post.json ("passed" = reported symptom observed).
  A5 no_vow_counters     zero vow counters on battlefield creatures in
                         post.json ("passed" = reported symptom observed).
  A6 cleanup             stack empty in post.json, game proceeded.

Verdict proposal: reproduced iff A2 passes and A3+A4+A5 all pass (the
spell resolved and did nothing - exactly the report). not-reproduced iff
any choice was offered, a sacrifice happened, or counters were placed.
blocked iff setup never completed or the cast could not be attempted.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402
import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260912-6900"
EVDIR = f"{BACKFILL}/evidence/6900/{RUN_ID}"
RUNDIR = f"{BACKFILL}/runs/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(RUNDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")
RUNLOG2 = open(f"{RUNDIR}/scenario_run.log", "w")

PROMISE = "Promise of Loyalty"
LION = "Savannah Lions"
PLAINS = "Plains"

P0_DECK = [(PROMISE, 8), (LION, 8), (PLAINS, 44)]
P1_DECK = [(LION, 12), (PLAINS, 48)]
TIMEOUT = 1200

ST = {}


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


def life(state, pid):
    for p in (state.get("players") or []):
        if p.get("player_id") == pid or p.get("id") == pid \
                or p.get("seat") == pid:
            return p.get("life")
    return None


def objs(state):
    return state.get("objects") or {}


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


def lands(state, pid):
    return [(oid, o) for oid, o in bf(state, pid) if oname(o) == PLAINS]


def untapped_lands(state, pid):
    return [oid for oid, o in lands(state, pid) if not o.get("tapped")]


def gy_named(state, pid, name):
    return [oid for oid, o in objs(state).items()
            if o.get("zone") == "Graveyard" and o.get("controller") == pid
            and oname(o) == name]


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                               "PostCombatMain"))


def wf_type(state):
    return (state.get("waiting_for") or {}).get("type")


def wf_player(state):
    return ((state.get("waiting_for") or {}).get("data") or {}).get("player")


def wf_desc(state):
    return ((state.get("waiting_for") or {}).get("data") or {}
            ).get("description", "")


def counters_of(o):
    """Best-effort extraction of counters on an object."""
    out = []
    for key in ("counters", "counter", "loyalty_counters"):
        v = o.get(key)
        if isinstance(v, list):
            out.extend(v)
        elif v:
            out.append(v)
    return out


def vow_counter_scan(state):
    """Scan every battlefield object for anything vow/counter related."""
    hits = []
    for oid, o in objs(state).items():
        if o.get("zone") != "Battlefield":
            continue
        blob = json.dumps(o, default=str)
        if "vow" in blob.lower():
            hits.append({"oid": oid, "name": oname(o),
                         "controller": o.get("controller"),
                         "counters": counters_of(o)})
    return hits


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


def get_vi(st):
    vi = (st or {}).get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def current_opps(c):
    st = c.latest if c else None
    vi = get_vi(st)
    return vi.get("opportunities") or [] if vi else []


PROMPT_KEYWORDS = ("vow", "counter", "sacrifice")


def scan_for_vow_prompt(c, state):
    """Look for any vow-counter / sacrifice choice offered to this seat.
    Returns True if a matching opportunity was seen this tick."""
    seen = False
    for opp in current_opps(c):
        blob = json.dumps(opp, default=str).lower()
        if any(k in blob for k in PROMPT_KEYWORDS):
            seen = True
            wire("vow_prompt_candidate",
                 {"who": c.name, "iid": opp.get("interactionId"),
                  "response_type": (opp.get("response") or {}).get("type"),
                  "blob": blob[:600], "stage": ST.get("stage")})
    wt = (wf_type(state) or "").lower()
    desc = (wf_desc(state) or "").lower()
    if any(k in wt or k in desc for k in PROMPT_KEYWORDS):
        seen = True
        wire("vow_prompt_waiting_for",
             {"who": c.name, "wf_type": wf_type(state), "desc": desc[:300],
              "stage": ST.get("stage")})
    if seen:
        ST["vow_prompt_seen"] = True
        say(f"[{c.name}] VOW-COUNTER/SACRIFICE PROMPT DETECTED")
    return seen


def log_opportunities(c, state, tag):
    opps = current_opps(c)
    if not opps:
        return
    wire("opportunities", {"who": c.name, "tag": tag,
                           "wf_type": wf_type(state),
                           "desc": wf_desc(state)[:200],
                           "opps": json.loads(json.dumps(opps,
                                                         default=str))[:4]})


# ------------------------------------------------------------------ drivers

C0 = None
C1 = None


def reset_state():
    ST.clear()
    ST.update({
        "stage": "SETUP",   # SETUP -> PROOF -> AWAIT_RESOLVE -> DONE
        "stop": False,
        "turn_cap": 40,
        "rejections": [],
        "game_code": None,
        "setup_turn": None,
        "cast_advertised": None,   # True/False/None
        "cast_submitted": False,
        "cast_manual": False,
        "promise_resolved": False,
        "vow_prompt_seen": False,
        "pre_lions_p0": None,
        "pre_lions_p1": None,
        "post_lions_p0": None,
        "post_lions_p1": None,
        "post_vow_hits": None,
        "held_debug_logged": set(),
        "stall_logged": False,
    })


def board_ready(state):
    return (len(bf_named(state, 0, LION)) >= 2
            and len(bf_named(state, 1, LION)) >= 2
            and find_hand(state, 0, PROMISE) is not None)


def promise_resolved(state):
    return len(gy_named(state, 0, PROMISE)) > 0


def stack_empty(state):
    return not (state.get("stack") or [])


async def cast_promise(c, pid, state, acts):
    oid = find_hand(state, pid, PROMISE)
    a = castspell_advertised(acts, oid)
    if a is not None and len(untapped_lands(state, pid)) >= 5:
        ST["cast_advertised"] = True
        await submit_as_is(c, a)
        ST["cast_submitted"] = True
        say(f"[{c.name}] casts Promise of Loyalty (advertised action)")
        return True
    if a is None and oid and len(untapped_lands(state, pid)) >= 5 \
            and not ST["cast_submitted"] and ST.get("cast_advertised") is not False:
        # not advertised: probe a manual CastSpell once
        ST["cast_advertised"] = False
        manual = {"type": "CastSpell", "data": {"object_id": int(oid)}}
        await submit_as_is(c, manual)
        ST["cast_submitted"] = True
        ST["cast_manual"] = True
        say(f"[{c.name}] CastSpell NOT advertised; manual probe submitted")
        return True
    return False


async def p0_tick(c, pid, state, acts):
    stage = ST["stage"]
    if stage in ("PROOF", "AWAIT_RESOLVE"):
        scan_for_vow_prompt(c, state)
        log_opportunities(c, state, "proof_window")

    if wf_player(state) == pid and wf_type(state) not in (
            None, "Priority", "DeclareAttackers", "DeclareBlockers"):
        dbg_key = ("held", stage, wf_type(state))
        if dbg_key not in ST.setdefault("held_debug_logged", set()):
            ST["held_debug_logged"].add(dbg_key)
            vi = (c.latest or {}).get("viewer_interaction")
            wire("held_decision_debug",
                 {"who": c.name, "stage": stage,
                  "wf": (c.latest or {}).get("state", {}).get("waiting_for"),
                  "vi": json.dumps(vi, default=str)[:3000]})
        # any non-priority decision during the proof window is interesting:
        # hold it (do not auto-answer a vow/sacrifice choice blindly)
        wire("decision_held", {"who": c.name, "wf_type": wf_type(state),
                               "desc": wf_desc(state)[:160], "stage": stage})
        return True

    if stage == "SETUP":
        if not is_my_main(state, pid):
            return False
        if len(bf_named(state, pid, LION)) < 2:
            oid = find_hand(state, pid, LION)
            a = castspell_advertised(acts, oid)
            if a and len(untapped_lands(state, pid)) >= 1:
                await submit_as_is(c, a)
                say("P0 casts Savannah Lions")
                return True
        if board_ready(state):
            ST["setup_turn"] = state.get("turn_number")
            ST["stage"] = "PROOF"
            say(f"board ready at turn {ST['setup_turn']}; stage -> PROOF")
            return True
        await play_land(c, pid, state, acts)
        return False

    if stage == "PROOF":
        if not is_my_main(state, pid):
            return False
        if ST["promise_resolved"]:
            return False
        if ST["cast_submitted"]:
            # cast in flight: let it resolve; keep passing priority via
            # the default fallthrough below (handled in tick).
            return False
        if await cast_promise(c, pid, state, acts):
            await export_now("pre.json")
            ST["pre_lions_p0"] = len(bf_named(state, 0, LION))
            ST["pre_lions_p1"] = len(bf_named(state, 1, LION))
            ST["stage"] = "AWAIT_RESOLVE"
            say("pre.json exported at cast; stage -> AWAIT_RESOLVE")
            return True
        await play_land(c, pid, state, acts)
        return False

    if stage == "AWAIT_RESOLVE":
        if promise_resolved(state):
            ST["promise_resolved"] = True
            ST["post_lions_p0"] = len(bf_named(state, 0, LION))
            ST["post_lions_p1"] = len(bf_named(state, 1, LION))
            ST["post_vow_hits"] = vow_counter_scan(state)
            # settle window: wait a few more ticks so any delayed trigger
            # or cleanup effect lands before the post export
            ST.setdefault("settle_ticks", 0)
            ST["settle_ticks"] += 1
            if ST["settle_ticks"] >= 6 and stack_empty(state):
                await export_now("post.json")
                ST["stage"] = "DONE"
                ST["stop"] = True
                say("post.json exported; DONE")
                return True
            return False
        return False

    return False


async def p1_tick(c, pid, state, acts):
    stage = ST["stage"]
    if stage in ("PROOF", "AWAIT_RESOLVE"):
        scan_for_vow_prompt(c, state)
        log_opportunities(c, state, "proof_window")

    if wf_player(state) == pid and wf_type(state) not in (
            None, "Priority", "DeclareAttackers", "DeclareBlockers"):
        wire("decision_held", {"who": c.name, "wf_type": wf_type(state),
                               "stage": stage})
        return True

    if stage == "SETUP":
        if not is_my_main(state, pid):
            return False
        if len(bf_named(state, pid, LION)) < 2:
            oid = find_hand(state, pid, LION)
            a = castspell_advertised(acts, oid)
            if a and len(untapped_lands(state, pid)) >= 1:
                await submit_as_is(c, a)
                say("P1 casts Savannah Lions")
                return True
        await play_land(c, pid, state, acts)
        return False

    if stage in ("PROOF", "AWAIT_RESOLVE"):
        if is_my_main(state, pid):
            await play_land(c, pid, state, acts)
        return False

    return False


async def play_land(c, pid, state, acts):
    lid = find_hand(state, pid, PLAINS)
    if not lid:
        return False
    la = next((x for x in acts if x["type"] == "PlayLand"
               and str(x.get("data", {}).get("object_id")) == lid), None)
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

    take(PLAINS, 2)
    take(LION, 2)
    if pid == 0:
        take(PROMISE, 2)  # protect Promise of Loyalty
    else:
        take(PROMISE, 0)
    for oid in hand:
        if len(picks) >= over:
            break
        if oid not in picks:
            picks.append(oid)
    return [int(x) for x in picks[:over]]


async def tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    drain_rejections(c)
    if settle_pending(c):
        return False
    for a in acts:
        if a["type"] == "MulliganDecision":
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
            await submit_as_is(c, a)
            return True
    if wf_player(state) == pid and wf_type(state) in (
            "TargetSelection", "TriggerTargetSelection"):
        wire("target_held", {"who": c.name, "stage": ST.get("stage")})
        return True
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


# ------------------------------------------------------------------- run

async def get_server_hello():
    async with websockets.connect(URL, max_size=200_000_000) as ws:
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


async def main():
    reset_state()
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
        if not ST["stall_logged"] and turn > 20 and ST["stage"] == "SETUP":
            ST["stall_logged"] = True
            sstate = (st0.get("state") or {}) if st0 else {}
            hand = [oname(objs(sstate).get(oid, {}))
                    for oid in hand_oids(sstate, 0)]
            say(f"SETUP STALL? turn {turn} stage SETUP: "
                f"P0 hand={hand} p0bf={[oname(o) for _, o in bf(sstate, 0)]} "
                f"p1bf={[oname(o) for _, o in bf(sstate, 1)]} "
                f"wf={sstate.get('waiting_for')}")
            wire("setup_stall", {"turn": turn, "hand": hand})
        if time.time() - last_progress > 300:
            say("no progress for 300s; stopping")
            for c in (C0, C1):
                st = c.latest
                if st:
                    wire("stall_state",
                         {"who": c.name,
                          "wf": st.get("state", {}).get("waiting_for"),
                          "phase": st.get("state", {}).get("phase"),
                          "acts": [a.get("type")
                                   for a in st.get("legal_actions", [])],
                          "stage": ST.get("stage")})
            break
        await asyncio.sleep(0.15)

    say(f"loop ended: stage={ST['stage']} stop={ST['stop']}")
    wire("loop_end", {"stage": ST["stage"], "stop": ST["stop"]})
    await C0.close()
    await C1.close()

    # ---- assertions from SAVED states ----
    A, D = {}, {}
    pre = env_state(load_env("pre.json"))
    post = env_state(load_env("post.json"))

    # A1
    if pre and ST["pre_lions_p0"] is not None:
        ok = (ST["pre_lions_p0"] >= 2 and ST["pre_lions_p1"] >= 2)
        A["A1_setup_ok"] = "passed" if ok else "failed"
        D["A1_setup_ok"] = (f"p0_lions_pre={ST['pre_lions_p0']} "
                            f"p1_lions_pre={ST['pre_lions_p1']} "
                            f"(need >=2 each); Promise in P0 hand at cast")
    else:
        A["A1_setup_ok"] = "not-run"
        D["A1_setup_ok"] = "pre.json missing (cast never attempted)"

    # A2
    if post and ST["cast_submitted"]:
        gy = len(gy_named(post, 0, PROMISE)) >= 1
        A["A2_cast_completes"] = "passed" if gy else "failed"
        D["A2_cast_completes"] = (
            f"CastSpell advertised={ST['cast_advertised']} "
            f"manual_probe={ST['cast_manual']} "
            f"rejections={len(ST['rejections'])}; "
            f"Promise in P0 gy in post.json: {gy}")
    else:
        A["A2_cast_completes"] = "not-run"
        D["A2_cast_completes"] = ("cast never submitted"
                                  if not ST["cast_submitted"]
                                  else "post.json missing")

    # A3
    if post and ST["cast_submitted"]:
        A["A3_no_vow_counter_prompt"] = \
            "failed" if ST["vow_prompt_seen"] else "passed"
        D["A3_no_vow_counter_prompt"] = (
            f"vow_prompt_seen={ST['vow_prompt_seen']} "
            f"(wire_log.jsonl holds the full opportunity scan; "
            f"'passed' = the reported no-prompt symptom was observed)")
    else:
        A["A3_no_vow_counter_prompt"] = "not-run"
        D["A3_no_vow_counter_prompt"] = "resolution window never reached"

    # A4
    if post and ST["post_lions_p0"] is not None:
        ok = (ST["post_lions_p0"] == ST["pre_lions_p0"]
              and ST["post_lions_p1"] == ST["pre_lions_p1"])
        A["A4_no_sacrifice"] = "passed" if ok else "failed"
        D["A4_no_sacrifice"] = (
            f"lions pre: p0={ST['pre_lions_p0']} p1={ST['pre_lions_p1']}; "
            f"post: p0={ST['post_lions_p0']} p1={ST['post_lions_p1']} "
            f"('passed' = reported symptom: sacrifice-the-rest did not fire)")
    else:
        A["A4_no_sacrifice"] = "not-run"
        D["A4_no_sacrifice"] = "post.json missing"

    # A5
    if post and ST["post_vow_hits"] is not None:
        n = len(ST["post_vow_hits"])
        A["A5_no_vow_counters"] = "passed" if n == 0 else "failed"
        D["A5_no_vow_counters"] = (
            f"vow-counter-bearing battlefield objects in post.json: {n} "
            f"{ST['post_vow_hits'][:3] if n else ''} "
            f"('passed' = reported symptom: no counters placed)")
    else:
        A["A5_no_vow_counters"] = "not-run"
        D["A5_no_vow_counters"] = "post.json missing"

    # A6
    if post:
        ok = stack_empty(post) and ST["stage"] == "DONE"
        A["A6_cleanup"] = "passed" if ok else "failed"
        D["A6_cleanup"] = (f"stack_empty={stack_empty(post)} "
                           f"final_stage={ST['stage']} "
                           f"turn={post.get('turn_number')}")
    else:
        A["A6_cleanup"] = "not-run"
        D["A6_cleanup"] = "no post state exported"

    if A["A1_setup_ok"] != "passed" or A["A2_cast_completes"] != "passed":
        verdict = "blocked"
    elif (A["A3_no_vow_counter_prompt"] == "passed"
          and A["A4_no_sacrifice"] == "passed"
          and A["A5_no_vow_counters"] == "passed"):
        verdict = "reproduced"
    elif (A["A3_no_vow_counter_prompt"] == "failed"
          or A["A4_no_sacrifice"] == "failed"
          or A["A5_no_vow_counters"] == "failed"):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    say("ASSERTIONS: " + json.dumps(A, indent=1))
    say("VERDICT (proposal): " + verdict)

    # ---- run.json ----
    sh = ST["server_hello"]
    run = {
        "issue": 6900,
        "run_id": RUN_ID,
        "title": "Promise of Loyalty does nothing",
        "server": {
            "observed_hello": sh,
            "server_version": sh.get("server_version"),
            "build_commit": sh.get("build_commit"),
            "protocol_version": sh.get("protocol_version"),
            "mode": sh.get("mode"),
        },
        "binary_sha256":
            "6a33738e215f9c5dde5fd6f2e72568992a83d668778d593b9d65fab657c0d8c0",
        "card_data_sha256":
            "c1bdd90380ecf9cf414c62dc57f41f2035e02d81c14c266237ddc79430361c1a",
        "draft_pools_sha256":
            "c79abf75cfb3d628906942b2707b047387d444559b5e25d32a411e9ab21f3f7c",
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(t0)),
        "ended_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
        "game_code": ST["game_code"],
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "decisions": {
            "setup_turn": ST["setup_turn"],
            "cast_advertised": ST["cast_advertised"],
            "cast_submitted": ST["cast_submitted"],
            "cast_manual": ST["cast_manual"],
            "promise_resolved": ST["promise_resolved"],
            "vow_prompt_seen": ST["vow_prompt_seen"],
            "pre_lions_p0": ST["pre_lions_p0"],
            "pre_lions_p1": ST["pre_lions_p1"],
            "post_lions_p0": ST["post_lions_p0"],
            "post_lions_p1": ST["post_lions_p1"],
            "post_vow_hits": ST["post_vow_hits"],
            "rejections": ST["rejections"],
        },
        "assertions": A,
        "assertion_details": D,
        "verdict": verdict,
        "verdict_note": ("proposal only, from A1-A6; the assertion table "
                         "is authoritative"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "8x/12x card density is a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "The prebuilt server has no standalone state-restore; states "
            "are authoritative exports (restorable only via full replay).",
        ],
        "stats": {
            "final_stage": ST["stage"],
        },
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say("wrote run.json")

    # ---- scenario copy, server excerpts ----
    with open(f"{BACKFILL}/driver/scenario_6900.py") as f:
        src = f.read()
    with open(f"{EVDIR}/scenario_6900.py", "w") as f:
        f.write(src)
    say("copied scenario_6900.py to evidence")

    excerpts = []
    try:
        slog = open(f"{RUNDIR}/server.log").read().splitlines()
        gc = ST["game_code"] or ""
        excerpts = [ln for ln in slog if gc and gc in ln][-500:]
    except Exception as e:
        excerpts = [f"server log unreadable: {e}"]
    with open(f"{EVDIR}/server_excerpts.log", "w") as f:
        f.write("\n".join(excerpts) + "\n")
    say(f"wrote server_excerpts.log ({len(excerpts)} lines)")

    WIRE.close()
    RUNLOG.close()
    RUNLOG2.close()

    render_png()
    write_manifest()
    validate()


def render_png():
    """Render summary.png from the SAVED states + assertion results."""
    from PIL import Image, ImageDraw
    run = json.load(open(f"{EVDIR}/run.json"))

    def st_of(fn):
        e = load_env(fn)
        s = env_state(e)
        return s

    W, H = 1040, 1000
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
    d.text((24, y), "#6900 - Promise of Loyalty does nothing", fill=ACCENT)
    y += 26
    d.text((24, y),
           f"server v{srv.get('server_version')} ({srv.get('build_commit')}, "
           f"protocol {srv.get('protocol_version')}) | run {run['run_id']} | "
           f"{run['started_at'][:10]} | verdict: {run['verdict']} (proposal)",
           fill=DIM)
    y += 30

    lines = [
        'Oracle: "Each player puts a vow counter on a creature they control',
        'and sacrifices the rest. Each of those creatures can\'t attack you',
        'or planeswalkers you control for as long as it has a vow counter."',
        "Reported: no vow-counter prompt; sacrifice-the-rest never fires.",
        "Proof: 2 Savannah Lions each side; P0 casts Promise of Loyalty",
        "({4}{W}). Expected if working: 1 Lion each remains, each bearing",
        "a vow counter. Bug signature: 0 prompts, 4 Lions remain, 0 vow",
        "counters.",
    ]
    for ln in lines:
        d.text((24, y), ln, fill=TEXT)
        y += 20
    y += 8

    dec = run["decisions"]
    rows = [
        ("pre.json (just before the cast)",
         f"P0 Lions {dec.get('pre_lions_p0')}, P1 Lions "
         f"{dec.get('pre_lions_p1')}; CastSpell advertised: "
         f"{dec.get('cast_advertised')}"),
        ("Resolution window (both seats scanned)",
         f"vow-counter/sacrifice prompt offered: "
         f"{dec.get('vow_prompt_seen')}"),
        ("post.json (after Promise resolved)",
         f"P0 Lions {dec.get('post_lions_p0')}, P1 Lions "
         f"{dec.get('post_lions_p1')}; vow-bearing objects: "
         f"{len(dec.get('post_vow_hits') or [])}; Promise in P0 gy"),
    ]
    d.rectangle([16, y, W - 16, y + 30 + len(rows) * 44], fill=PANEL,
                outline=(45, 52, 64))
    d.text((28, y + 8), "Observed (from saved states)", fill=YELLOW)
    y += 34
    for tag, val in rows:
        d.text((28, y), tag, fill=TEXT)
        d.text((28, y + 20), val[:150], fill=DIM)
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
        d.text((28, y + 20), Dd.get(k, "")[:150], fill=DIM)
        y += 44
    y += 12
    d.text((24, y), "Limitations: " + "; ".join(run["limitations"])[:200],
           fill=DIM)
    y += 24
    d.text((24, y), "Evidence summary (not a gameplay screenshot). "
           "States: pre/post.json + manifest.sha256",
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
    from PIL import Image
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
        im = Image.open(os.path.join(EVDIR, "summary.png"))
        im.verify()
        print("VALIDATE: summary.png opens OK", flush=True)
    except Exception as e:
        print(f"VALIDATE FAIL: summary.png: {e}", flush=True)
        ok = False
    print("VALIDATE: " + ("ALL OK" if ok else "FAILURES"), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
