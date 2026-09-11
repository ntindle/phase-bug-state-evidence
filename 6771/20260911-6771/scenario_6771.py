#!/usr/bin/env python3
"""Issue #6771: Breakthrough card not working correctly.

Reported (Discord): "it lets you draw 4 cards but doesn't make you
discard any"

Oracle (pinned v0.79.0 card-data.json), Breakthrough ({X}{U}, Sorcery):
  "Draw four cards, then choose X cards in your hand and discard the rest."

Parser state in pinned data: Draw child supported; both the
"choose X cards in your hand" and "discard the rest" children are
explicitly Unimplemented. Triage acceptance criteria: after drawing, the
player chooses exactly X cards from their resulting hand when possible,
every other card is discarded, X comes from the announced casting value.

Behavioral contract (single game, two human-client seats, v0.79.0/proto 69):
  RAMP    - P0 plays an Island per tick; keeps mulligan.
  ARM     - at P0's PreCombatMain, when a CastSpell-for-Breakthrough action
            is advertised: export pre_cast, then arm the cast.
  CASTING - submit CastSpell; answer ChooseXValue with X=2; record every
            waiting_for/viewer_interaction opportunity; never pass priority
            while a P0 decision is pending.
  STOP    - once Breakthrough is in P0's graveyard and the stack is empty:
            export post_cast, stop.

  A1 setup_ok            pre_cast: P0 main phase, Breakthrough in hand,
                         >=3 Islands on P0 battlefield (cast affordable).
  A2 x_announced         a ChooseXValue prompt was offered and answered X=2.
  A3 draw_completed      post_cast: Breakthrough in P0 gy, P0 library -4 vs
                         pre, stack empty.
  A4 choose_prompted     a "choose X cards in hand" prompt appeared after
                         the draw (expected to fail: parser unsupported).
  A5 discard_happened    post_cast: P0 hand size == X (=2), rest discarded
                         (expected to fail: no discard happens).
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
RUN_ID = "20260911-6771"
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
      "pre_exported": False, "post_exported": False}
WF_SEEN = []
X_SEEN = []          # interactionIds of answered X prompts
X_ANSWERED = False
CHOOSE_SEEN = []     # any hand-selection prompt observed after the draw
CHOOSE_REFS = {}     # interactionId -> snapshot of the opportunity


def oname(o):
    return o.get("card_name") or o.get("name") or ""


def lname(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def objs(state):
    return state.get("objects", {}) or {}


def hand(state, pid):
    return [oid for oid, o in objs(state).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def gy_named(state, pid, key):
    return [int(oid) for oid, o in objs(state).items()
            if o.get("zone") == "Graveyard" and o.get("controller") == pid
            and lname(o) == key.lower()]


def bf_lands(state, pid):
    return [oid for oid, o in objs(state).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and lname(o) == LAND.lower()]


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
    for oid in hand(state, pid):
        if lname(objs(state)[oid]) == name.lower():
            return oid
    return None


def find_action(acts, atype):
    for a in acts or []:
        if a.get("type") == atype:
            return a
    return None


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain"))


def vi_opps(c):
    st = c.latest
    if not st:
        return []
    return (st.get("viewer_interaction") or {}).get("opportunities", []) or []


def record_wf(state):
    wf = (state.get("waiting_for") or {}).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf, "data": (state.get("waiting_for") or {}).get("data")})


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


C0 = None


async def do_export(path):
    s = await C0.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


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

    # cleanup discard at 8+: prefer Breakthroughs last, keep lands
    if wtype == "DiscardToHandSize" and wplayer == 0:
        n = (wf.get("data") or {}).get("count") \
            or max(0, len(hand(state, pid)) - 7)
        oids = hand(state, pid)

        def rank(oid):
            nm = lname(objs(state)[oid])
            return 0 if nm == LAND.lower() else (1 if nm == SPELL.lower() else 0)
        picks = sorted(oids, key=rank)[:n]
        if picks:
            wire("action_submit", {"who": "P0", "action": "SelectCards/discard",
                                   "picks": picks})
            await c.send_action({"type": "SelectCards",
                                 "data": {"cards": [int(x) for x in picks]}})
            say(f"[P0] discards {len(picks)} to hand size")
            return True
        return False

    # ChooseXValue for Breakthrough: announce X=2
    if wtype == "ChooseXValue" and wplayer == 0:
        for opp in vi_opps(c):
            iid = opp.get("interactionId")
            resp = opp.get("response") or {}
            spec = (resp.get("data") or {}).get("spec") or {}
            if iid in X_SEEN:
                continue
            if resp.get("type") == "schema" and spec.get("type") == "number":
                sub = {"interactionId": iid,
                       "response": {"type": "number",
                                    "data": {"value": X_CHOSEN}}}
                wire("x_answer", {"iid": iid, "x": X_CHOSEN})
                await c.send_interaction(sub)
                X_SEEN.append(iid)
                global X_ANSWERED
                X_ANSWERED = True
                say(f"[P0] Breakthrough X-choice -> X={X_CHOSEN}")
                return True
        wire("x_noopportunity", {"wtype": wtype})
        return False

    # After resolution, scan opportunities for a hand-keep prompt (the bug
    # says none appears; record if one ever does).
    if ST["cast_submitted"] and not ST["post_exported"]:
        for opp in vi_opps(c):
            iid = opp.get("interactionId")
            if iid in CHOOSE_REFS:
                continue
            CHOOSE_REFS[iid] = True
            wire("opp_during_casting", {"iid": iid, "opp": opp})
            if looks_like_hand_keep(opp):
                CHOOSE_SEEN.append(iid)
                wire("hand_keep_prompt_seen", {"iid": iid})
                say(f"[P0] hand-keep prompt observed: {iid}")

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
        picks = hand(state, pid)[:n]
        if picks:
            await c.send_action({"type": "SelectCards",
                                 "data": {"cards": [int(x) for x in picks]}})
            return True
        return False
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

        wf = state.get("waiting_for") or {}
        wtype = wf.get("type")
        wplayer = (wf.get("data") or {}).get("player")

        # ARM -> pre_cast export at P0 main phase when the cast is advertised
        if ST["stage"] == "RAMP" and not ST["pre_exported"] \
                and is_my_main(state, 0):
            mid = find_hand(state, 0, SPELL)
            castable = any(a.get("type") == "CastSpell"
                           and str(a.get("data", {}).get("object_id")) == mid
                           for a in (st.get("legal_actions") or []))
            if mid and castable:
                pre = await do_export("pre_cast.json")
                wire("pre_cast", {"hand": len(hand(pre, 0)),
                                  "library": lib_count(pre, 0),
                                  "turn": pre.get("turn_number")})
                ST["pre_exported"] = True
                ST["cast_armed"] = True
                ST["stage"] = "CASTING"
                say(f"[armed] pre_cast exported turn {pre.get('turn_number')} "
                    f"hand={len(hand(pre, 0))} lib={lib_count(pre, 0)}")

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
            wire("post_cast", {"hand": len(hand(post, 0)),
                               "library": lib_count(post, 0),
                               "turn": post.get("turn_number"),
                               "phase": post.get("phase"),
                               "x_answered": X_ANSWERED,
                               "hand_keep_prompts": len(CHOOSE_SEEN)})
            ST["post_exported"] = True
            say(f"[post] post_cast exported turn {post.get('turn_number')} "
                f"phase={post.get('phase')} hand={len(hand(post, 0))} "
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
        ok_lands = len(bf_lands(pre, 0)) >= 3
        obs["notes"].append(f"pre_cast: phase_ok={ok_main} spell_in_hand={ok_hand} "
                            f"islands_bf={len(bf_lands(pre, 0))} "
                            f"hand={len(hand(pre, 0))} lib={lib_count(pre, 0)}")
        A["A1_setup_ok"] = "passed" if (ok_main and ok_hand and ok_lands) else "failed"

    # A2
    A["A2_x_announced"] = "passed" if X_ANSWERED else "failed"
    obs["notes"].append(f"x prompt offered & answered X={X_CHOSEN}: {X_ANSWERED} "
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
        h0, h1 = len(hand(pre, 0)), len(hand(post, 0))
        gy0 = sum(1 for o in objs(pre).values()
                  if o.get("zone") == "Graveyard" and o.get("controller") == 0)
        gy1 = sum(1 for o in objs(post).values()
                  if o.get("zone") == "Graveyard" and o.get("controller") == 0)
        expect_kept = X_CHOSEN  # Oracle: choose X, discard the rest
        expect_bug = h0 - 1 + 4  # cast from hand, draw 4, nothing discarded
        obs["notes"].append(f"hand: pre={h0} post={h1}; oracle expects {expect_kept}; "
                            f"bug expects {expect_bug}; gy 0:{gy0} -> 1:{gy1}")
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
            "version": "v0.79.0",
            "build_commit": "1cde7a2",
            "protocol_version": 69,
            "binary_sha256": "46d89146bf3e051cf22591bd195b7d15d4806a8f6d226bb8792dbcfe479fef94",
            "card_data_sha256": "75cbfe139b220b8267c4d99b1d478b59d7298a86dfc3def83fbb31eaa970b5b3",
            "draft_pools_sha256": "7518817d5db317ccba9f6d197648677a8ff8341700e14b4b54f8bdf2d71b0b8b",
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
    with open(f"{EVDIR}/scenario_6771.py", "w") as f:
        f.write(scenario_src.decode())

    await p0.close()
    await p1.close()
    return obs


if __name__ == "__main__":
    obs = asyncio.run(main())
    print(json.dumps(obs["assert"], indent=2))
