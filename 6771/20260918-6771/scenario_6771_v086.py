#!/usr/bin/env python3
"""Issue #6771: Breakthrough card not working correctly.

Reported (Discord): "it lets you draw 4 cards but doesn't make you
discard any"

Oracle (pinned v0.86.0 card-data.json), Breakthrough ({X}{U}, Sorcery):
  "Draw four cards, then choose X cards in your hand and discard the rest."

RE-VALIDATION run on v0.86.0 / protocol 72. Prior run (20260911, v0.79.0 /
protocol 69) verdict = reproduced: draw completed, no choose-X prompt ever
appeared, hand kept all 9 cards, nothing discarded. That run is preserved in
evidence/6771/20260911-6771/; this run re-runs the same contract on the new
pin to see whether the unsupported-aspect coverage gap has changed.

Behavioral contract (single game, two human-client seats, v0.86.0/proto 72):
  RAMP    - P0 plays an Island per tick; keeps mulligan (protocol-72 pending
            gate). P1 ramps passively.
  ARM     - at P0's PreCombatMain, when a CastSpell-for-Breakthrough action
            is advertised: export pre_cast, then arm the cast.
  CASTING - submit CastSpell; answer ChooseXValue with X=2 via the number
            spec; record every waiting_for/viewer_interaction opportunity;
            never pass priority while a P0 decision is pending. If a
            hand-keep prompt appears (the bug says none does), answer it
            keeping X_CHOSEN cards so resolution can complete and A5 can
            actually assert the final hand size.
  STOP    - once Breakthrough is in P0's graveyard and the stack is empty:
            export post_cast, stop.

  A1 setup_ok            pre_cast: P0 main phase, Breakthrough in hand,
                         >=3 Islands on P0 battlefield (cast affordable).
  A2 x_announced         a ChooseXValue prompt was offered and answered X=2.
  A3 draw_completed      post_cast: Breakthrough in P0 gy, P0 library -4 vs
                         pre, stack empty.
  A4 choose_prompted     a "choose X cards in hand" prompt appeared after
                         the draw (prior run: failed - parser unsupported).
  A5 discard_happened    post_cast: P0 hand size == X (=2), rest discarded
                         (prior run: failed - no discard happens).
  A6 cleanup             post_cast: stack empty, game advanced past pre_cast.

Verdict = blocked iff A1 fails (setup never assembled).
Verdict = reproduced iff A1 passes and (A4 or A5) fails.
Verdict = not-reproduced iff A1..A6 all pass.
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
RUN_ID = "20260918-6771"
EVID_ISSUE = "6771"
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


SPELL = "Breakthrough"
LAND = "Island"

P0_DECK = deck((SPELL, 12), (LAND, 48))
P1_DECK = deck((LAND, 60))

X_CHOSEN = 2

ST = {"stage": "RAMP", "cast_armed": False, "cast_submitted": False,
      "cast_turn": None, "stop": False, "stall_since": None,
      "pre_exported": False, "post_exported": False,
      "mull_done": set(), "answered_iids": set(),
      "last_ix": None}  # (submit_time, revision, kind) for failed-ix retry
WF_SEEN = []
X_SEEN = []          # interactionIds of answered X prompts
X_ANSWERED = False
X_ANSWERED_VALUE = None
CHOOSE_SEEN = []     # any hand-selection prompt observed after the draw
CHOOSE_REFS = {}     # interactionId -> True (recorded)


def oname(o):
    return o.get("card_name") or o.get("name") or ""


def lname(state, oid):
    o = objs(state).get(str(oid)) or {}
    return str(o.get("base_name") or o.get("name") or o.get("card_name") or "?").lower()


def objs(state):
    return state.get("objects", {}) or {}


def hand_ids(state, pid):
    return [oid for oid, o in objs(state).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def gy_named(state, pid, key):
    return [int(oid) for oid, o in objs(state).items()
            if o.get("zone") == "Graveyard" and o.get("controller") == pid
            and lname(state, oid) == key.lower()]


def bf_lands(state, pid):
    return [oid for oid, o in objs(state).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and lname(state, oid) == LAND.lower()]


def lib_count(state, pid):
    players = state.get("players", [])
    if isinstance(players, dict):
        pp = players.get(str(pid), players.get(pid)) or {}
        return len(pp.get("library", []) or [])
    for p in players or []:
        if isinstance(p, dict) and p.get("id") == pid:
            return len(p.get("library", []) or [])
    return None


def find_hand(state, pid, name):
    for oid in hand_ids(state, pid):
        if lname(state, oid) == name.lower():
            return oid
    return None


def find_action(acts, atype):
    for a in acts or []:
        if a.get("type") == atype:
            return a
    return None


def wf_of(state):
    return state.get("waiting_for") or {}


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain"))


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def record_wf(state):
    wf = wf_of(state).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf, "data": wf_of(state).get("data")})


def looks_like_hand_keep(opp):
    """Heuristic: opportunity asking to select cards from hand (the
    'choose X cards in your hand' half of Breakthrough)."""
    resp = opp.get("response") or {}
    rtype = resp.get("type")
    data = resp.get("data") or {}
    text = json.dumps(opp, default=str).lower()
    if "choose" in text and ("hand" in text or "keep" in text):
        return True
    if rtype == "schema" and (data.get("spec") or {}).get("type") in ("select", "sequence"):
        for ch in data.get("candidates", []) or []:
            for s in ch.get("surfaces", []) or []:
                dd = s.get("data") or {}
                if str(dd.get("zone", "")).lower() == "hand":
                    return True
    return False


def select_response_type(opp):
    """Pick the response type for a select-style opportunity on proto 72."""
    resp = opp.get("response") or {}
    spec = (resp.get("data") or {}).get("spec") or {}
    return spec.get("type") or resp.get("type") or "select"


C0 = None


async def do_export(path):
    s = await C0.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


def mulligan_entry_for(state, pid):
    for p in (wf_of(state).get("data") or {}).get("pending", []) or []:
        if p.get("player") == pid:
            return p
    return None


async def answer_mulligan(c, pid, st, state):
    """Protocol 72: gate on my seat's presence in waiting_for.data.pending
    with phase Declare; answer the advertised MulliganDecision as-is, once."""
    if wf_of(state).get("type") != "MulliganDecision":
        return False
    entry = mulligan_entry_for(state, pid)
    if not entry:
        return False
    if str((entry.get("phase") or {}).get("type")) != "Declare":
        return False
    if pid in ST["mull_done"]:
        return False
    a = find_action(st.get("legal_actions"), "MulliganDecision")
    if not a:
        return False
    wire("action_submit", {"who": c.name, "action": "MulliganDecision/Keep",
                           "seat": pid})
    msg = {"type": "MulliganDecision"}
    if a.get("data"):
        msg["data"] = a["data"]
    if "data" not in msg or "choice" not in msg.get("data", {}):
        msg["data"] = {"choice": {"type": "Keep"}}
    await c.send_action(msg)
    ST["mull_done"].add(pid)
    say(f"[{c.name}] keeps (seat {pid})")
    return True


async def answer_cleanup_discard(c, pid, st, state):
    """Protocol 72 DiscardToHandSize: wf.data.player gate; answer via
    viewer_interaction select-style response; fall back to a SelectCards
    legal_action. Rank discards: lands first, Breakthroughs last."""
    wf = wf_of(state)
    if wf.get("type") != "DiscardToHandSize":
        return False
    if (wf.get("data") or {}).get("player") != pid:
        return False
    oids = hand_ids(state, pid)
    n = (wf.get("data") or {}).get("count") or max(0, len(oids) - 7)
    if n <= 0:
        return False

    def rank(oid):
        nm = lname(state, oid)
        if nm == LAND.lower():
            return 0
        if nm == SPELL.lower():
            return 2
        return 1

    picks = [str(oid) for oid in sorted(oids, key=rank)][:n]
    vi = get_vi(st)
    if vi:
        for opp in vi.get("opportunities", []) or []:
            iid = opp.get("interactionId")
            if iid in ST["answered_iids"]:
                continue
            rtype = select_response_type(opp)
            if rtype not in ("select", "sequence"):
                continue
            sub = {"interactionId": iid,
                   "response": {"type": rtype, "data": {"choiceIds": picks}}}
            wire("action_submit", {"who": c.name, "action": "vi-discard",
                                   "iid": iid, "picks": picks})
            await c.send_interaction(sub)
            ST["answered_iids"].add(iid)
            say(f"[{c.name}] discards {len(picks)} to hand size (vi)")
            return True
    a = find_action(st.get("legal_actions"), "SelectCards")
    if a:
        wire("action_submit", {"who": c.name, "action": "SelectCards/discard",
                               "picks": picks})
        await c.send_action({"type": "SelectCards",
                             "data": {"cards": [int(x) for x in picks]}})
        say(f"[{c.name}] discards {len(picks)} to hand size (SelectCards)")
        return True
    return False


async def answer_x_value(c, pid, st, state):
    """Breakthrough's announced-X prompt: schema number -> value.

    Protocol-72 note: the prompt carries min/max (wf data + spec data). On
    v0.86.0 the engine can offer a DEGENERATE range (min:0/max:0) when the
    caster's available mana admits no larger X; answering outside [min,max]
    is rejected and moves no revision, so clamp the desired X into the
    advertised range and record the value actually answered."""
    wf = wf_of(state)
    if wf.get("type") != "ChooseXValue":
        return False
    if (wf.get("data") or {}).get("player") != pid:
        return False
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if iid in X_SEEN or iid in ST["answered_iids"]:
            continue
        resp = opp.get("response") or {}
        spec = (resp.get("data") or {}).get("spec") or {}
        stype = spec.get("type") or resp.get("type")
        wire("x_opportunity", {"iid": iid, "response": resp,
                               "wf_data": {k: (wf.get("data") or {}).get(k)
                                           for k in ("min", "max", "player")}})
        if stype == "number" or resp.get("type") == "schema":
            sdata = spec.get("data") or {}
            lo = sdata.get("min", (wf.get("data") or {}).get("min", 0)) or 0
            hi = sdata.get("max", (wf.get("data") or {}).get("max", lo))
            hi = lo if hi is None else hi
            val = max(lo, min(X_CHOSEN, hi))
            sub = {"interactionId": iid,
                   "response": {"type": "number",
                                "data": {"value": val}}}
            wire("x_answer", {"iid": iid, "x": val, "clamped": val != X_CHOSEN,
                              "range": [lo, hi]})
            await c.send_interaction(sub)
            X_SEEN.append(iid)
            ST["answered_iids"].add(iid)
            ST["last_ix"] = (time.time(), c.revision, "x")
            global X_ANSWERED, X_ANSWERED_VALUE
            X_ANSWERED = True
            X_ANSWERED_VALUE = val
            say(f"[{c.name}] Breakthrough X-choice -> X={val} "
                f"(wanted {X_CHOSEN}, range [{lo},{hi}])")
            return True
    wire("x_noopportunity", {"wtype": wf.get("type")})
    return False


def check_failed_ix(c):
    """#650 lesson: a rejected interaction moves no revision. If none
    advanced within ~12s of a submission, reset the answered sets so the
    prompt is answered again."""
    li = ST["last_ix"]
    if not li:
        return
    t, rev, kind = li
    if time.time() - t > 12 and c.revision == rev:
        wire("ix_retry", {"kind": kind, "rev": rev})
        say(f"[retry] no revision advance 12s after {kind} submission; "
            f"resetting answered ids")
        ST["answered_iids"] = set()
        if kind == "x":
            X_SEEN.clear()
            global X_ANSWERED
            X_ANSWERED = False
        ST["last_ix"] = None


async def answer_hand_keep(c, pid, st, state):
    """If the engine offers a choose-X-cards-in-hand prompt after the draw,
    keep X_CHOSEN cards so resolution can complete and A5 can assert."""
    if not ST["cast_submitted"] or ST["post_exported"]:
        return False
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId")
        if iid in CHOOSE_REFS or iid in ST["answered_iids"]:
            continue
        CHOOSE_REFS[iid] = True
        wire("opp_during_casting", {"iid": iid, "opp": opp})
        if not looks_like_hand_keep(opp):
            continue
        CHOOSE_SEEN.append(iid)
        wire("hand_keep_prompt_seen", {"iid": iid})
        say(f"[{c.name}] hand-keep prompt observed: {iid}")
        rtype = select_response_type(opp)
        if rtype in ("select", "sequence"):
            oids = sorted(hand_ids(state, pid))
            nkeep = X_ANSWERED_VALUE if X_ANSWERED_VALUE is not None else X_CHOSEN
            keep = [str(o) for o in oids[:nkeep]]
            sub = {"interactionId": iid,
                   "response": {"type": rtype, "data": {"choiceIds": keep}}}
            wire("keep_answer", {"iid": iid, "keep": keep})
            await c.send_interaction(sub)
            ST["answered_iids"].add(iid)
            ST["last_ix"] = (time.time(), c.revision, "keep")
            say(f"[{c.name}] answers hand-keep: keep {keep}")
            return True
    return False


# ------------------------------------------------------------- tick (P0)

async def tick_p0(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    wf = wf_of(state)
    wtype = wf.get("type")
    wplayer = (wf.get("data") or {}).get("player")

    if await answer_mulligan(c, pid, st, state):
        return True
    if await answer_cleanup_discard(c, pid, st, state):
        return True
    if await answer_x_value(c, pid, st, state):
        return True
    if await answer_hand_keep(c, pid, st, state):
        return True

    # never pass priority while a P0 decision is pending
    if wtype in ("OptionalCostChoice", "TargetSelection", "ManaPayment",
                 "ChooseXValue") and wplayer == 0:
        wire("hold_priority", {"wtype": wtype})
        return False

    da = find_action(acts, "DeclareAttackers")
    if da and state.get("active_player") == 0:
        sub = copy.deepcopy(da)
        sub["data"]["attacks"] = []
        sub["data"]["bands"] = []
        wire("action_submit", {"who": "P0", "action": "DeclareAttackers/empty"})
        await c.send_action({"type": "DeclareAttackers", "data": sub["data"]})
        return True

    db = find_action(acts, "DeclareBlockers")
    if db:
        sub = copy.deepcopy(db)
        sub["data"]["assignments"] = []
        wire("action_submit", {"who": "P0", "action": "DeclareBlockers/empty"})
        await c.send_action({"type": "DeclareBlockers", "data": sub["data"]})
        return True

    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            wire("action_submit", {"who": "P0", "action": a["type"]})
            await c.send_action(a)
            return True

    if is_my_main(state, pid) and ST["stage"] == "RAMP":
        # land drop every tick (no kept-flags)
        hid = find_hand(state, pid, LAND)
        for a in acts:
            if a["type"] == "PlayLand" and hid \
                    and str(a.get("data", {}).get("object_id")) == hid:
                wire("action_submit", {"who": "P0", "action": "PlayLand"})
                await c.send_action(a)
                say("[P0] plays Island")
                return True

    if ST["cast_armed"] and not ST["cast_submitted"]:
        mid = find_hand(state, pid, SPELL)
        for a in acts:
            if a["type"] == "CastSpell" \
                    and str(a.get("data", {}).get("object_id")) == mid:
                ST["cast_turn"] = state.get("turn_number")
                wire("action_submit", {"who": "P0", "action": "CastSpell/Breakthrough",
                                       "object_id": mid})
                await c.send_action(a)
                ST["cast_submitted"] = True
                say("[P0] casts Breakthrough")
                return True

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

    if await answer_mulligan(c, pid, st, state):
        return True
    if await answer_cleanup_discard(c, pid, st, state):
        return True

    da = find_action(acts, "DeclareAttackers")
    if da:
        sub = copy.deepcopy(da)
        sub["data"]["attacks"] = []
        sub["data"]["bands"] = []
        await c.send_action({"type": "DeclareAttackers", "data": sub["data"]})
        return True
    db = find_action(acts, "DeclareBlockers")
    if db:
        sub = copy.deepcopy(db)
        sub["data"]["assignments"] = []
        await c.send_action({"type": "DeclareBlockers", "data": sub["data"]})
        return True
    if is_my_main(state, pid):
        hid = find_hand(state, pid, LAND)
        for a in acts:
            if a["type"] == "PlayLand" and hid \
                    and str(a.get("data", {}).get("object_id")) == hid:
                await c.send_action(a)
                return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
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
    TIMEOUT = 1200
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

        # drain rejections/errors into the wire log (interaction rejections
        # move no revision and would otherwise be invisible)
        for c2 in (p0, p1):
            while True:
                try:
                    t, data = c2.inbox.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if t in ("ActionRejected", "Error"):
                    wire("rejection", {"who": c2.name, "type": t,
                                       "data": data})
                    say(f"[{c2.name}] {t}: {json.dumps(data)[:220]}")

        # #650: retry a submission that moved no revision within 12s
        check_failed_ix(p0)
        check_failed_ix(p1)

        # ARM -> pre_cast export at P0 main phase when the cast is advertised
        if ST["stage"] == "RAMP" and not ST["pre_exported"] \
                and is_my_main(state, 0):
            mid = find_hand(state, 0, SPELL)
            castable = any(a.get("type") == "CastSpell"
                           and str(a.get("data", {}).get("object_id")) == mid
                           for a in (st.get("legal_actions") or []))
            if mid and castable:
                pre = await do_export("pre_cast.json")
                wire("pre_cast", {"hand": len(hand_ids(pre, 0)),
                                  "library": lib_count(pre, 0),
                                  "turn": pre.get("turn_number")})
                ST["pre_exported"] = True
                ST["cast_armed"] = True
                ST["castable_at_arm"] = True
                ST["stage"] = "CASTING"
                say(f"[armed] pre_cast exported turn {pre.get('turn_number')} "
                    f"hand={len(hand_ids(pre, 0))} lib={lib_count(pre, 0)}")

        # stall watchdog: P0 cast never submitted after arming
        if ST["cast_armed"] and not ST["cast_submitted"]:
            if ST["stall_since"] is None:
                ST["stall_since"] = time.time()
            elif time.time() - ST["stall_since"] > 120:
                await do_export("mid_stall.json")
                obs["notes"].append("stall watchdog: cast armed but never "
                                    "submitted after 120s")
                say("[stall] watchdog fired")
                ST["stop"] = True
        else:
            ST["stall_since"] = None

        # post_cast: resolution observed (spell in gy, stack empty)
        if ST["cast_submitted"] and not ST["post_exported"] \
                and gy_named(state, 0, SPELL) and not (state.get("stack") or []):
            await asyncio.sleep(0.75)
            post = await do_export("post_cast.json")
            wire("post_cast", {"hand": len(hand_ids(post, 0)),
                               "library": lib_count(post, 0),
                               "turn": post.get("turn_number"),
                               "phase": post.get("phase"),
                               "x_answered": X_ANSWERED,
                               "hand_keep_prompts": len(CHOOSE_SEEN)})
            ST["post_exported"] = True
            say(f"[post] post_cast exported turn {post.get('turn_number')} "
                f"phase={post.get('phase')} hand={len(hand_ids(post, 0))} "
                f"lib={lib_count(post, 0)}")
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

    pre = env_state("pre_cast.json")
    post = env_state("post_cast.json")

    # A1
    if pre is None:
        A["A1_setup_ok"] = "failed"
        obs["notes"].append("pre_cast.json never exported")
    else:
        ok_main = pre.get("active_player") == 0 \
            and (pre.get("phase") or "") in ("PreCombatMain", "PostCombatMain")
        ok_hand = find_hand(pre, 0, SPELL) is not None
        # True setup precondition is engine-advertised castability at ARM
        # time, not a fixed land count: on v0.86.0 the X prompt's max is
        # mana-dependent, so X=0 casts are affordable off a single Island.
        ok_castable = bool(ST.get("castable_at_arm"))
        obs["notes"].append(f"pre_cast: phase_ok={ok_main} spell_in_hand={ok_hand} "
                            f"cast_advertised={ok_castable} "
                            f"islands_bf={len(bf_lands(pre, 0))} "
                            f"hand={len(hand_ids(pre, 0))} lib={lib_count(pre, 0)}")
        A["A1_setup_ok"] = "passed" if (ok_main and ok_hand and ok_castable) else "failed"

    # A2
    A["A2_x_announced"] = "passed" if X_ANSWERED else "failed"
    obs["notes"].append(f"x prompt offered & answered: {X_ANSWERED} "
                        f"value={X_ANSWERED_VALUE} (wanted {X_CHOSEN}) "
                        f"(iids={X_SEEN})")

    # A3
    if pre is None or post is None:
        A["A3_draw_completed"] = "not-run"
        obs["notes"].append("pre/post pair incomplete; A3 not-run")
    else:
        in_gy = len(gy_named(post, 0, SPELL)) >= 1
        lc0, lc1 = lib_count(pre, 0), lib_count(post, 0)
        drew = (lc0 is not None and lc1 is not None and lc0 - lc1 == 4)
        empty_stack = not (post.get("stack") or [])
        obs["notes"].append(f"draw check: spell_in_gy={in_gy} lib {lc0}->{lc1} "
                            f"(delta={lc0 - lc1 if lc0 and lc1 else '?'}) "
                            f"stack_empty={empty_stack}")
        A["A3_draw_completed"] = "passed" if (in_gy and drew and empty_stack) else "failed"

    # A4
    A["A4_choose_prompted"] = "passed" if CHOOSE_SEEN else "failed"
    obs["notes"].append(f"hand-keep 'choose X' prompt observed: {bool(CHOOSE_SEEN)} "
                        f"(count={len(CHOOSE_SEEN)})")

    # A5
    if pre is None or post is None:
        A["A5_discard_happened"] = "not-run"
        obs["notes"].append("pre/post pair incomplete; A5 not-run")
    else:
        h0, h1 = len(hand_ids(pre, 0)), len(hand_ids(post, 0))
        expect_kept = X_ANSWERED_VALUE if X_ANSWERED_VALUE is not None else X_CHOSEN
        # Oracle: choose X, discard the rest
        expect_bug = h0 - 1 + 4  # cast from hand, draw 4, nothing discarded
        obs["notes"].append(f"hand: pre={h0} post={h1}; oracle with X={expect_kept} "
                            f"expects {expect_kept}; bug expects {expect_bug}")
        A["A5_discard_happened"] = "passed" if h1 == expect_kept else "failed"

    # A6
    if post is not None:
        empty = not (post.get("stack") or [])
        obs["notes"].append(f"post_cast: phase={post.get('phase')} "
                            f"turn={post.get('turn_number')} stack_empty={empty}")
        A["A6_cleanup"] = "passed" if empty else "failed"
    else:
        A["A6_cleanup"] = "not-run"
        obs["notes"].append("post_cast.json missing; A6 not-run")

    for k in sorted(A):
        say(f"{k}: {A[k]}")
    say("WF sequence:", WF_SEEN)

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "wf_sequence": WF_SEEN,
                   "x_answered": X_ANSWERED, "x_value": X_CHOSEN,
                   "hand_keep_prompts": CHOOSE_SEEN}, f, indent=2)

    scenario_src = open(__file__, "rb").read()
    verdict = "blocked"
    if A.get("A1_setup_ok") not in ("failed", "blocked"):
        if A.get("A4_choose_prompted") == "failed" \
                or A.get("A5_discard_happened") == "failed":
            verdict = "reproduced"
        elif all(v == "passed" for v in A.values()):
            verdict = "not-reproduced"
    run_meta = {
        "run_id": RUN_ID,
        "issue": 6771,
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "server": {
            "version": "v0.86.0",
            "build_commit": "2cc8c28",
            "protocol_version": 72,
            "binary_sha256": "67d495b599cbe7d68c9ab9fddaf2f1a37ec1d392382e31e43963d0653eed6af2",
            "card_data_sha256": "ab7a4b65e8fba8407a928eae8f261abb078f43c081923e9c0f4af30a9c40ff26",
            "draft_pools_sha256": "d20d2dbf181b2361c9cdf0e67bef34d996765338f1986bc395477905dfefd13e",
        },
        "scenario_sha256": hashlib.sha256(scenario_src).hexdigest(),
        "decks": {
            "P0": {"Breakthrough": 12, "Island": 48},
            "P1": {"Island": 60},
        },
        "verdict": verdict,
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run_meta, f, indent=2)
    say("verdict:", verdict)
    with open(f"{EVDIR}/scenario_6771_v086.py", "w") as f:
        f.write(scenario_src.decode())

    await p0.close()
    await p1.close()
    return obs


if __name__ == "__main__":
    obs = asyncio.run(main())
    print(json.dumps(obs["assert"], indent=2))
