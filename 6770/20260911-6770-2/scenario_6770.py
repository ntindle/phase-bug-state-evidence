#!/usr/bin/env python3
"""Issue #6770: Manabond card not working correctly.

Reported (Discord): "it triggers at the end of the turn, discard correctly
the hand but put also the lands on the grave instead of battlefield"

Oracle (pinned v0.79.0 card-data.json):
  Manabond ({G}, Enchantment):
    "At the beginning of your end step, you may reveal your hand and put all
     land cards from it onto the battlefield. If you do, discard your hand."

Parser state in pinned data: trigger mode Phase/End, optional=true.
  RevealHand (target Any) -> ChangeZoneAll (Typed Land, controller=null,
  origin=null -> Battlefield) -> Discard (HandSize of Controller, conditioned
  on OptionalEffectPerformed). The triage notes the ChangeZoneAll target
  records only `land` with no hand zone / controller scope, and the discard
  counts the controller's current hand -- matching lands being discarded
  instead of moved.

Behavioral contract (single game, two human-client seats, v0.79.0/proto 69):
  RAMP    - P0 plays Forest, casts Manabond turn 1; plays one land per turn,
            keeps everything else in hand. Declines every Manabond prompt.
  DECLINE - at the first P0 end step where the hand holds >=3 lands and
            >=1 non-land: export pre_decline, DECLINE the may-choice, export
            post_decline once the stack empties.
  ACCEPT  - at the NEXT P0 end step: export pre_accept, ACCEPT the may-choice,
            export post_accept once the stack empties (lands should move to
            the battlefield, the rest of the hand should be discarded).
  STOP    - after post_accept.

  A1 setup_ok            pre_decline: Manabond on P0 BF, hand >=3 lands + >=1 Bear.
  A2 decline_no_move     post_decline: same hand objects, same BF land count.
  A3 accept_lands_battlefield  every land object in pre_accept P0 hand is on
            P0's battlefield in post_accept (the reported outcome is their
            absence there), and none of them is in the graveyard.
  A4 accept_rest_discarded     every non-land object in pre_accept P0 hand is
            in P0's graveyard in post_accept.
  A5 cleanup             post_accept: stack empty, game advanced past the turn.

Verdict = blocked iff A1 fails (setup never assembled).
Verdict = reproduced iff A1 passes and (A3 or A4) fails.
Verdict = not-reproduced iff A1..A5 all pass.
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
RUN_ID = "20260911-6770-2"
EVID_ISSUE = "6770"
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


MANABOND = "Manabond"
BEAR = "Grizzly Bears"
FOREST = "Forest"

P0_DECK = deck((MANABOND, 12), (BEAR, 12), (FOREST, 36))
P1_DECK = deck((FOREST, 60))

ST = {"stage": "RAMP", "stop": False, "accept_turn": None,
      "decline_turn": None, "decline_exported": False,
      "accept_exported": False, "stall_since": None}
WF_SEEN = []


def oname(o):
    return o.get("card_name") or o.get("name") or ""


def lname(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def objs(state):
    return state.get("objects", {}) or {}


def is_land(o):
    core = (o.get("base_card_types") or {}).get("core_types") or []
    return "Land" in core


def hand(state, pid):
    return [oid for oid, o in objs(state).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def bf_named(state, pid, key):
    return [int(oid) for oid, o in objs(state).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and lname(o) == key.lower()]


def life_of(state, pid):
    players = state.get("players", [])
    if isinstance(players, dict):
        pp = players.get(str(pid), players.get(pid))
        return (pp or {}).get("life") if isinstance(pp, dict) else None
    for p in players or []:
        if isinstance(p, dict) and p.get("id") == pid:
            return p.get("life")
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


def choice_bool_value(ch):
    for s in ch.get("surfaces", []):
        dd = s.get("data", {}) or {}
        if dd.get("role") in ("accept", "value", "pay") \
                and str(dd.get("value", "")).lower() in ("true", "false"):
            return str(dd["value"]).lower()
    return None


def record_wf(state):
    wf = (state.get("waiting_for") or {}).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf})


C0 = None


async def do_export(path):
    s = await C0.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


async def answer_may(c, want):
    """Answer the Manabond OptionalEffectChoice with want ('true'/'false')."""
    for opp in vi_opps(c):
        if ((opp.get("response") or {}).get("type")) != "exactChoices":
            continue
        cands = ((opp.get("response") or {}).get("data") or {}).get("choices", [])
        for ch in cands:
            if choice_bool_value(ch) == want:
                sub = {"interactionId": opp.get("interactionId"),
                       "response": {"type": "choose",
                                    "data": {"choiceId": ch["id"]}}}
                wire("may_answer", {"want": want, "submission": sub})
                await c.send_interaction(sub)
                say(f"[{c.name}] answers Manabond may-choice {want} "
                    f"(choice {ch['id']})")
                return True
    return False


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

    # cleanup discard: Bears first (keep lands for the test)
    if wtype == "DiscardToHandSize" and wplayer == 0:
        n = (wf.get("data") or {}).get("count") \
            or max(0, len(hand(state, pid)) - 7)
        oids = hand(state, pid)

        def rank(oid):
            nm = lname(objs(state)[oid])
            return 0 if nm == BEAR.lower() else 1
        picks = sorted(oids, key=rank)[:n]
        if picks:
            wire("action_submit", {"who": "P0", "action": "SelectCards/discard",
                                   "picks": picks})
            await c.send_action({"type": "SelectCards",
                                 "data": {"cards": [int(x) for x in picks]}})
            say(f"[P0] discards {len(picks)}")
            return True
        return False

    # Manabond may-choice: answer per stage, but hold the answer until the
    # main loop has exported the pre-decision state (pre_decline/pre_accept).
    # Answering first would let the engine resolve before the export.
    if wtype == "OptionalEffectChoice" and wplayer == 0:
        if ST["stage"] == "RAMP":
            want = "false"
        elif ST["stage"] == "DECLINE" and ST["decline_exported"]:
            want = "false"
        elif ST["stage"] == "ACCEPT" and ST["accept_exported"]:
            want = "true"
        else:
            wire("may_hold_for_pre_export", {"stage": ST["stage"]})
            return False
        if await answer_may(c, want):
            wire("may_stage", {"stage": ST["stage"], "want": want,
                               "phase": state.get("phase"),
                               "turn": state.get("turn_number")})
            if want == "true":
                ST["accept_answered"] = True
            return True
        wire("may_noopportunity", {"stage": ST["stage"]})
        return False

    # never pass priority while a P0 decision is pending
    if wtype in ("OptionalCostChoice", "TargetSelection", "ManaPayment") \
            and wplayer == 0:
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
        hid = find_hand(state, pid, FOREST)
        for a in acts:
            if a["type"] == "PlayLand" and hid \
                    and str(a.get("data", {}).get("object_id")) == hid:
                wire("action_submit", {"who": "P0", "action": "PlayLand"})
                await c.send_action(a)
                say("[P0] plays Forest")
                return True
        # cast Manabond once ({G}); skip if already on BF
        if not bf_named(state, pid, MANABOND):
            mid = find_hand(state, pid, MANABOND)
            if mid:
                for a in acts:
                    if a["type"] == "CastSpell" \
                            and str(a.get("data", {}).get("object_id")) == mid:
                        wire("action_submit", {"who": "P0",
                                               "action": "CastSpell/Manabond"})
                        await c.send_action(a)
                        say("[P0] casts Manabond")
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
        hid = find_hand(state, pid, "forest")
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
            except Exception as e:
                say(f"tick error {c.name}: {e}")
        st = p0.latest
        if not st:
            continue
        state = st["state"]
        record_wf(state)

        def hand_counts(s):
            h = hand(s, 0)
            lands = [x for x in h if is_land(objs(s)[x])]
            nonl = [x for x in h if not is_land(objs(s)[x])]
            return h, lands, nonl

        # RAMP -> DECLINE: armed when setup complete at PostCombatMain
        if ST["stage"] == "RAMP" \
                and state.get("active_player") == 0 \
                and (state.get("phase") or "") == "PostCombatMain" \
                and bf_named(state, 0, MANABOND):
            h, lands, nonl = hand_counts(state)
            if len(lands) >= 3 and len(nonl) >= 1:
                ST["stage"] = "DECLINE"
                say(f"[decline-armed] turn {state.get('turn_number')} hand "
                    f"lands={len(lands)} nonlands={len(nonl)}")

        wf = state.get("waiting_for") or {}
        wtype = wf.get("type")
        wplayer = (wf.get("data") or {}).get("player")

        # stall watchdog: OptionalEffectChoice unanswered >90s
        if wtype == "OptionalEffectChoice" and wplayer == 0:
            if ST["stall_since"] is None:
                ST["stall_since"] = time.time()
            elif time.time() - ST["stall_since"] > 90:
                await do_export("mid_stall.json")
                obs["notes"].append("stall watchdog: Manabond may-choice "
                                    "unanswered >90s")
                say("[stall] watchdog fired")
                ST["stop"] = True
        else:
            ST["stall_since"] = None

        # DECLINE branch: pre/post exports around the declined trigger
        if ST["stage"] == "DECLINE" and not ST["decline_exported"] \
                and wtype == "OptionalEffectChoice" and wplayer == 0 \
                and (state.get("phase") or "") in ("End",):
            pre = await do_export("pre_decline.json")
            ST["decline_turn"] = pre.get("turn_number")
            h, lands, nonl = hand_counts(pre)
            wire("pre_decline", {"hand": h, "lands": lands, "nonlands": nonl,
                                 "turn": ST["decline_turn"]})
            ST["decline_exported"] = True
            say(f"[pre_decline] turn {ST['decline_turn']}: lands={len(lands)} "
                f"nonlands={len(nonl)}")
            # answer decline next tick (stage DECLINE answers "false")
        if ST["stage"] == "DECLINE" and ST["decline_exported"] \
                and not (state.get("stack") or []) \
                and (state.get("phase") or "") in ("End", "Cleanup") \
                and wtype != "OptionalEffectChoice":
            await asyncio.sleep(0.75)
            post = await do_export("post_decline.json")
            ST["stage"] = "ACCEPT"
            say(f"[post_decline] phase={post.get('phase')} turn={post.get('turn_number')}; "
                "arming ACCEPT for next end step")

        # ACCEPT branch: pre export while the choice is pending (tick holds
        # the answer until this export lands), then answer next tick.
        if ST["stage"] == "ACCEPT" and not ST["accept_exported"] \
                and wtype == "OptionalEffectChoice" and wplayer == 0 \
                and (state.get("phase") or "") in ("End",):
            pre = await do_export("pre_accept.json")
            ST["accept_turn"] = pre.get("turn_number")
            h, lands, nonl = hand_counts(pre)
            wire("pre_accept", {"hand": h, "lands": lands, "nonlands": nonl,
                                "turn": ST["accept_turn"]})
            ST["accept_exported"] = True
            say(f"[pre_accept] turn {ST['accept_turn']}: lands={len(lands)} "
                f"nonlands={len(nonl)}")
        # post export: first P1 turn after the accept was answered (trigger
        # resolved, discard done, game advanced).
        if ST["stage"] == "ACCEPT" and ST.get("accept_answered") \
                and state.get("active_player") == 1 \
                and (state.get("turn_number") or 0) > (ST.get("accept_turn") or 0):
            await asyncio.sleep(0.75)
            post = await do_export("post_accept.json")
            wire("post_accept", {"phase": post.get("phase"),
                                 "turn": post.get("turn_number")})
            say(f"[post_accept] phase={post.get('phase')} "
                f"turn={post.get('turn_number')}")
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

    pre_d = env_state("pre_decline.json")
    post_d = env_state("post_decline.json")
    pre_a = env_state("pre_accept.json")
    post_a = env_state("post_accept.json")

    # A1
    if pre_d is None:
        A["A1_setup_ok"] = "failed"
        obs["notes"].append("pre_decline.json never exported")
    else:
        mb = bf_named(pre_d, 0, MANABOND)
        hd = hand(pre_d, 0)
        ln = [x for x in hd if is_land(objs(pre_d)[x])]
        nl = [x for x in hd if not is_land(objs(pre_d)[x])]
        ok = (len(mb) >= 1 and len(ln) >= 3 and len(nl) >= 1)
        A["A1_setup_ok"] = "passed" if ok else "failed"
        obs["notes"].append(f"pre_decline: manabond_bf={len(mb)} "
                            f"hand_lands={len(ln)} hand_nonlands={len(nl)}")

    # A2: decline moved nothing
    if pre_d is None or post_d is None:
        A["A2_decline_no_move"] = "not-run"
        obs["notes"].append("decline pre/post pair incomplete; A2 not-run")
    else:
        h0 = sorted(int(x) for x in hand(pre_d, 0))
        h1 = sorted(int(x) for x in hand(post_d, 0))
        bf0 = sorted(bf_named(pre_d, 0, FOREST))
        bf1 = sorted(bf_named(post_d, 0, FOREST))
        same = (h0 == h1 and bf0 == bf1)
        A["A2_decline_no_move"] = "passed" if same else "failed"
        obs["notes"].append(f"decline: hand_same={h0 == h1} bf_lands_same={bf0 == bf1}")

    # A3/A4: accept branch zone changes
    if pre_a is None or post_a is None:
        A["A3_accept_lands_battlefield"] = "not-run"
        A["A4_accept_rest_discarded"] = "not-run"
        obs["notes"].append("accept pre/post pair incomplete; A3/A4 not-run")
    else:
        ha = hand(pre_a, 0)
        lands_a = [int(x) for x in ha if is_land(objs(pre_a)[x])]
        nonl_a = [int(x) for x in ha if not is_land(objs(pre_a)[x])]
        ob = objs(post_a)
        zones = {int(oid): o.get("zone") for oid, o in ob.items()}
        ctrls = {int(oid): o.get("controller") for oid, o in ob.items()}

        bf_lands = [x for x in lands_a
                    if zones.get(x) == "Battlefield" and ctrls.get(x) == 0]
        gy_lands = [x for x in lands_a if zones.get(x) == "Graveyard"]
        gy_nonl = [x for x in nonl_a
                   if zones.get(x) == "Graveyard" and ctrls.get(x) == 0]
        obs["notes"].append(
            f"accept: hand_lands={len(lands_a)} hand_nonlands={len(nonl_a)}; "
            f"post: lands->bf={len(bf_lands)} lands->gy={len(gy_lands)} "
            f"nonlands->gy={len(gy_nonl)}")
        A["A3_accept_lands_battlefield"] = \
            "passed" if (len(bf_lands) == len(lands_a) and not gy_lands) \
            else "failed"
        A["A4_accept_rest_discarded"] = \
            "passed" if len(gy_nonl) == len(nonl_a) else "failed"
        # dump where each hand card went for the record
        move_map = {x: zones.get(x) for x in lands_a + nonl_a}
        obs["notes"].append(f"accept zone map: {move_map}")

    # A5
    if post_a is not None:
        empty = not (post_a.get("stack") or [])
        adv = (post_a.get("turn_number") or 0) > (ST.get("accept_turn") or 0) \
            or post_a.get("active_player") == 1
        A["A5_cleanup"] = "passed" if (empty and adv) else "failed"
        obs["notes"].append(f"post_accept: phase={post_a.get('phase')} "
                            f"turn={post_a.get('turn_number')} "
                            f"stack_empty={empty} advanced={adv}")
    else:
        A["A5_cleanup"] = "not-run"
        obs["notes"].append("post_accept.json missing; A5 not-run")

    for k in sorted(A):
        say(f"{k}: {A[k]}")
    say("WF sequence:", WF_SEEN)

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "wf_sequence": WF_SEEN}, f, indent=2)

    scenario_src = open(__file__, "rb").read()
    verdict = "blocked"
    if A.get("A1_setup_ok") not in ("failed", "blocked"):
        if A.get("A3_accept_lands_battlefield") == "failed" \
                or A.get("A4_accept_rest_discarded") == "failed":
            verdict = "reproduced"
        elif all(v == "passed" for v in A.values()):
            verdict = "not-reproduced"
    run_meta = {
        "run_id": RUN_ID,
        "issue": 6770,
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
            "P0": {"Manabond": 12, "Grizzly Bears": 12, "Forest": 36},
            "P1": {"Forest": 60},
        },
        "verdict": verdict,
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run_meta, f, indent=2)
    say("verdict:", verdict)
    with open(f"{EVDIR}/scenario_6770.py", "w") as f:
        f.write(scenario_src.decode())

    await p0.close()
    await p1.close()
    return obs


if __name__ == "__main__":
    obs = asyncio.run(main())
    print(json.dumps(obs["assert"], indent=2))
