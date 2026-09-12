#!/usr/bin/env python3
"""Issue #6899: Moonmist doesn't prevent damage.

Oracle: "Transform all Humans. Prevent all combat damage that would be dealt
this turn by creatures other than Werewolves and Wolves."

Reported (Discord 2026-08-02): Moonmist fails to prevent combat damage caused
by creatures other than wolves/werewolves. (The separate "requires a target"
defect is #6403; this issue is the distinct runtime prevention failure.
Triage acceptance criteria: non-Wolf/Werewolf combat damage is prevented for
the rest of the turn; Wolves/Werewolves deal damage normally; prevention
applies regardless of which player controls the damage source or recipient.)

Behavioral contract (native engine v0.81.1 / protocol 70, two human seats):
  Setup: P0 and P1 each field one Grizzly Bears (2/2 non-wolf) and one
  Young Wolf (1/1 wolf). P0 holds Moonmist.
  Proof A (P1 attacking, prevention while P0 is defender): on P1's turn, P1
  attacks P0 with Bear + Wolf (unblocked). P0 casts Moonmist at first P0
  combat priority. pre.json exported after Moonmist resolves (P0 life 20).
  post.json exported after combat damage. Expected: Bear 0 + Wolf 1 ->
  P0 life 20 -> 19. Bug signature: 20 -> 17 (no prevention).
  Proof B (P0 attacking, prevention while P0 is the damage source's
  controller): on P0's next turn, P0 casts Moonmist pre-combat, attacks P1
  with Bear + Wolf (unblocked). pre2.json / post2.json. Expected:
  P1 life 20 -> 19. Bug signature: 20 -> 17.

  A1 setup_ok        both battlefields fielded Bear+Wolf; Moonmist in P0 hand
                     at proof A start; P0 life 20 in pre.json.
  A2 moonmist_resolves  Moonmist reached P0 graveyard in both proofs (cast
                     completed; any target prompt driven through and logged).
  A3 p1_attack_prevented  P0 life delta across P1's combat == 1 (Bear 0,
                     Wolf 1). Fails on 3 (no prevention) or 0 (over-prevention).
  A4 p0_attack_prevented  P1 life delta across P0's combat == 1.
  A5 cleanup         stack empty in post2.json, game proceeded, no stall.

Verdict proposal: reproduced iff A3 or A4 fails with delta==3 (the reported
no-prevention symptom); not-reproduced iff A3 and A4 pass; blocked iff setup
never completed. Verdict is a proposal only; the assertion table is
authoritative.
"""
import asyncio
import copy
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck, URL  # noqa: E402
import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260912-6899d"
EVDIR = f"{BACKFILL}/evidence/6899/{RUN_ID}"
RUNDIR = f"{BACKFILL}/runs/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(RUNDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")
RUNLOG2 = open(f"{RUNDIR}/scenario_run.log", "w")

MOONMIST = "Moonmist"
BEAR = "Grizzly Bears"
WOLF = "Young Wolf"
FOREST = "Forest"

P0_DECK = [(MOONMIST, 8), (BEAR, 8), (WOLF, 8), (FOREST, 36)]
P1_DECK = [(BEAR, 8), (WOLF, 8), (FOREST, 44)]
TIMEOUT = 1500

ST = {}
SUBMITTED_IIDS = set()
TARGET_ATTEMPTS = {}


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
    return [(oid, o) for oid, o in bf(state, pid) if oname(o) == FOREST]


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


def candidate_info(opp, state):
    data = (opp.get("response") or {}).get("data", {}) or {}
    out = []
    for ch in data.get("choices") or data.get("candidates") or []:
        ref, seat = None, None
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict):
                if "reference" in d:
                    ref = str(d["reference"])
                if "seat" in d:
                    seat = d["seat"]
        o = objs(state).get(str(ref), {}) if ref else {}
        out.append({"choice_id": ch.get("id"), "ref": ref, "seat": seat,
                    "name": oname(o), "zone": o.get("zone"),
                    "controller": o.get("controller"),
                    "text": (ch.get("text") or "")[:160]})
    return out


def build_submission(opp, choice_ids):
    """Build an Interaction submission matching the advertised response
    shape (AGENTS.md: exactChoices -> choose/choiceId; schema sequence ->
    sequence/choiceIds). choice_ids: list (possibly empty)."""
    resp = opp.get("response") or {}
    rtype = resp.get("type")
    spec = ((resp.get("data") or {}).get("spec")) or {}
    spec_type = spec.get("type") if isinstance(spec, dict) else None
    if rtype == "schema" and spec_type in ("sequence", "select"):
        return {"type": spec_type, "data": {"choiceIds": list(choice_ids)}}
    if rtype == "exactChoices":
        if len(choice_ids) == 1:
            return {"type": "choose",
                    "data": {"choiceId": choice_ids[0]}}
        return None
    return None


async def answer_moonmist_target(c, pid, state):
    """Drive through any target prompt raised while casting Moonmist (the
    #6403 symptom: Moonmist's Transform clause carries target Typed(Human)
    scope All; there are no Humans on board). Log the full opportunity;
    for schema sequence/select submit ALL candidates (scope All), for
    exactChoices submit the first candidate. On rejection, retry with an
    alternate selection up to 3 attempts per iid, then hold."""
    wt = wf_type(state)
    if wt not in ("TargetSelection", "TriggerTargetSelection",
                  "EffectZoneChoice") or wf_player(state) != pid:
        return False
    if not ST.get("moonmist_cast_pending"):
        return False
    opps = current_opps(c)
    for opp in opps:
        iid = opp.get("interactionId")
        if iid in SUBMITTED_IIDS:
            wire("prompt_reopen_hold", {"iid": iid, "stage": ST.get("stage")})
            return True
        attempts = TARGET_ATTEMPTS.get(iid, 0)
        if attempts >= 3:
            wire("prompt_max_attempts_hold",
                 {"iid": iid, "stage": ST.get("stage")})
            return True
        cands = candidate_info(opp, state)
        rtype = (opp.get("response") or {}).get("type")
        spec = (((opp.get("response") or {}).get("data") or {}).get("spec")
                 or {})
        spec_type = spec.get("type") if isinstance(spec, dict) else None
        wire("moonmist_prompt_shape",
             {"iid": iid, "wf_type": wt, "rtype": rtype,
              "spec_type": spec_type, "attempt": attempts,
              "desc": wf_desc(state)[:200], "cands": cands,
              "stage": ST.get("stage")})
        say(f"[{c.name}] Moonmist target prompt (attempt {attempts}): "
            f"{rtype}/{spec_type}, {len(cands)} candidates")
        ST["target_prompt_seen"] = True
        ids = [x["choice_id"] for x in cands if x["choice_id"]]
        if rtype == "schema" and spec_type in ("sequence", "select"):
            pick = ids if attempts == 0 else (ids[:1] if attempts == 1
                                              else [])
        elif rtype == "exactChoices":
            pick = ids[attempts:attempts + 1] if ids else []
        else:
            wire("prompt_unknown_shape", {"iid": iid, "rtype": rtype})
            return True
        sub = build_submission(opp, pick)
        if sub is None:
            wire("prompt_unanswerable_shape",
                 {"iid": iid, "stage": ST.get("stage")})
            return True
        wire("interaction_submit",
             {"who": c.name, "iid": iid, "kind": "moonmist_target",
              "response": sub, "pick_count": len(pick),
              "stage": ST.get("stage")})
        await c.send_interaction({"interactionId": iid, "response": sub})
        TARGET_ATTEMPTS[iid] = attempts + 1
        # do NOT add to SUBMITTED_IIDS until a rejection-free settle; the
        # rejection drain clears the settle key and we retry with a variant
        ST.setdefault("settle", {})[c.name] = c.revision
        say(f"[{c.name}] answered Moonmist target prompt "
            f"(attempt {attempts}, {len(pick)} choices)")
        return True
    dbg_key = (ST.get("stage"), wf_type(state))
    if dbg_key not in ST.setdefault("target_debug_logged", set()):
        ST["target_debug_logged"].add(dbg_key)
        vi = (c.latest or {}).get("viewer_interaction")
        wire("target_no_opp_debug",
             {"who": c.name, "stage": ST.get("stage"),
              "wf": (c.latest or {}).get("state", {}).get("waiting_for"),
              "vi": json.dumps(vi, default=str)[:3000]})
    return True


async def play_land(c, pid, state, acts):
    lid = find_hand(state, pid, FOREST)
    if not lid:
        return False
    la = next((x for x in acts if x["type"] == "PlayLand"
               and str(x.get("data", {}).get("object_id")) == lid), None)
    if la:
        await submit_as_is(c, la)
        return True
    return False

# ------------------------------------------------------------------ drivers

C0 = None
C1 = None

MAIN_PHASES = ("PreCombatMain", "PostCombatMain")
COMBAT_CAST_PHASES = ("BeginCombat", "DeclareAttackers", "DeclareBlockers")


def reset_state():
    ST.clear()
    ST.update({
        "stage": "SETUP",   # SETUP -> AWAIT_A -> PROOF_A -> AWAIT_A_DMG ->
                            # SETUP_B -> PROOF_B_ATK -> AWAIT_B_DMG -> DONE
        "stop": False,
        "turn_cap": 80,
        "rejections": [],
        "game_code": None,
        "setup_turn": None,
        "p1_attack_done": False,
        "moonmist_cast_pending": False,   # cast submitted, awaiting resolve
        "moonmist_a_resolved": False,
        "moonmist_b_resolved": False,
        "b_cast_done": False,
        "gy_at_b_cast": 0,
        "target_prompt_seen": False,
        "life0_pre": None,
        "life0_post": None,
        "life1_pre": None,
        "life1_post": None,
        "target_debug_logged": set(),
        "held_debug_logged": set(),
        "stall_logged": False,
    })
    SUBMITTED_IIDS.clear()
    TARGET_ATTEMPTS.clear()


def board_ready(state):
    """Both sides field Bear+Wolf; P0 holds Moonmist."""
    return (len(bf_named(state, 0, BEAR)) >= 1
            and len(bf_named(state, 0, WOLF)) >= 1
            and len(bf_named(state, 1, BEAR)) >= 1
            and len(bf_named(state, 1, WOLF)) >= 1
            and find_hand(state, 0, MOONMIST) is not None)


def moonmist_resolved(state, pid):
    return len(gy_named(state, pid, MOONMIST)) > 0


def stack_empty(state):
    return not (state.get("stack") or [])


async def cast_moonmist(c, pid, state, acts):
    oid = find_hand(state, pid, MOONMIST)
    a = castspell_advertised(acts, oid)
    if a and len(untapped_lands(state, pid)) >= 2:
        await submit_as_is(c, a)
        ST["moonmist_cast_pending"] = True
        say(f"[{c.name}] casts Moonmist")
        return True
    return False


async def p0_tick(c, pid, state, acts):
    # 1. pending decisions for this seat take absolute precedence
    if await answer_moonmist_target(c, pid, state):
        return True
    if wf_player(state) == pid and wf_type(state) not in (
            None, "Priority", "DeclareAttackers", "DeclareBlockers"):
        dbg_key = ("held", ST.get("stage"), wf_type(state))
        if dbg_key not in ST.setdefault("held_debug_logged", set()):
            ST["held_debug_logged"].add(dbg_key)
            vi = (c.latest or {}).get("viewer_interaction")
            wire("held_decision_debug",
                 {"who": c.name, "stage": ST.get("stage"),
                  "wf": (c.latest or {}).get("state", {}).get("waiting_for"),
                  "vi": json.dumps(vi, default=str)[:3000]})
        wire("decision_held", {"who": c.name, "wf_type": wf_type(state),
                               "desc": wf_desc(state)[:160],
                               "stage": ST.get("stage")})
        return True

    stage = ST["stage"]
    turn = state.get("turn_number") or 0

    if stage == "SETUP":
        if not is_my_main(state, pid):
            return False
        # cast one Bear and one Wolf when affordable; keep Moonmist
        if not bf_named(state, pid, BEAR):
            oid = find_hand(state, pid, BEAR)
            a = castspell_advertised(acts, oid)
            if a and len(untapped_lands(state, pid)) >= 2:
                await submit_as_is(c, a)
                say("P0 casts Grizzly Bears")
                return True
        if not bf_named(state, pid, WOLF):
            oid = find_hand(state, pid, WOLF)
            a = castspell_advertised(acts, oid)
            if a and len(untapped_lands(state, pid)) >= 1:
                await submit_as_is(c, a)
                say("P0 casts Young Wolf")
                return True
        if board_ready(state):
            ST["setup_turn"] = turn
            ST["stage"] = "AWAIT_A"
            say(f"board ready at turn {turn}; stage -> AWAIT_A")
            return True
        await play_land(c, pid, state, acts)
        return False

    if stage == "AWAIT_A":
        # waiting for P1's combat; keep developing mana
        if is_my_main(state, pid):
            await play_land(c, pid, state, acts)
        return False

    if stage == "PROOF_A":
        # P1's combat is live; cast Moonmist at first P0 priority before
        # damage. Check resolution FIRST (clears moonmist_cast_pending);
        # while a cast is in flight, take no further combat actions -- fall
        # through to the default PassPriority instead of double-casting
        # (the caster gets priority back first after casting).
        if ST["moonmist_a_resolved"]:
            return False
        if moonmist_resolved(state, pid):
            ST["moonmist_a_resolved"] = True
            ST["moonmist_cast_pending"] = False
            ST["life0_pre"] = life(state, 0)
            await export_now("pre.json")
            ST["stage"] = "AWAIT_A_DMG"
            say(f"Moonmist A resolved; P0 life {ST['life0_pre']}; "
                f"stage -> AWAIT_A_DMG")
            return True
        if ST["moonmist_cast_pending"]:
            return False
        if (state.get("active_player") == 1
                and (state.get("phase") or "") in COMBAT_CAST_PHASES
                and wf_player(state) == pid and wf_type(state) == "Priority"):
            if await cast_moonmist(c, pid, state, acts):
                return True
        return False

    if stage == "AWAIT_A_DMG":
        l0 = life(state, 0)
        if ST["life0_pre"] is not None and l0 is not None \
                and l0 < ST["life0_pre"]:
            await export_now("post.json")
            ST["life0_post"] = l0
            ST["stage"] = "SETUP_B"
            say(f"P1 combat damage applied: P0 {ST['life0_pre']} -> {l0}; "
                f"stage -> SETUP_B")
            return True
        # fallback: combat ended with no life change (unexpected)
        if ((state.get("phase") or "") == "PostCombatMain"
                and state.get("active_player") == 1
                and stack_empty(state)):
            await export_now("post.json")
            ST["life0_post"] = l0
            ST["stage"] = "SETUP_B"
            say(f"P1 combat ended with no life change (P0 {l0}); "
                f"stage -> SETUP_B")
            return True
        return False

    if stage == "SETUP_B":
        # P0's own turn: cast Moonmist pre-combat, then attack P1.
        # A B cast in flight is detected by graveyard-count delta from the
        # moment of THIS stage's cast (proof A already put copies there);
        # while in flight, pass priority through (the caster gets priority
        # back first after casting).
        if not is_my_main(state, pid):
            return False
        if ST["moonmist_b_resolved"]:
            return False
        if ST.get("b_cast_done"):
            if len(gy_named(state, pid, MOONMIST)) > \
                    ST.get("gy_at_b_cast", 0):
                ST["moonmist_b_resolved"] = True
                ST["moonmist_cast_pending"] = False
                ST["life1_pre"] = life(state, 1)
                await export_now("pre2.json")
                ST["stage"] = "PROOF_B_ATK"
                say(f"Moonmist B resolved; P1 life {ST['life1_pre']}; "
                    f"stage -> PROOF_B_ATK")
                return True
            return False
        if await cast_moonmist(c, pid, state, acts):
            ST["b_cast_done"] = True
            ST["gy_at_b_cast"] = len(gy_named(state, pid, MOONMIST))
            return True
        await play_land(c, pid, state, acts)
        return False

    if stage == "PROOF_B_ATK":
        if (state.get("active_player") == 0
                and (state.get("phase") or "") == "DeclareAttackers"):
            bears = bf_named(state, 0, BEAR)
            wolves = bf_named(state, 0, WOLF)
            if bears and wolves:
                for a in acts:
                    if a["type"] == "DeclareAttackers":
                        sub = copy.deepcopy(a)
                        sub["data"]["attacks"] = [
                            [int(bears[0]), {"type": "Player", "data": 1}],
                            [int(wolves[0]), {"type": "Player", "data": 1}]]
                        sub["data"]["bands"] = []
                        await submit_as_is(c, sub)
                        ST["stage"] = "AWAIT_B_DMG"
                        say("P0 attacks P1 with Bear + Wolf; "
                            "stage -> AWAIT_B_DMG")
                        return True
        return False

    if stage == "AWAIT_B_DMG":
        l1 = life(state, 1)
        if ST["life1_pre"] is not None and l1 is not None \
                and l1 < ST["life1_pre"]:
            await export_now("post2.json")
            ST["life1_post"] = l1
            ST["stage"] = "DONE"
            ST["stop"] = True
            say(f"P0 combat damage applied: P1 {ST['life1_pre']} -> {l1}; DONE")
            return True
        if ((state.get("phase") or "") == "PostCombatMain"
                and state.get("active_player") == 0
                and stack_empty(state)):
            await export_now("post2.json")
            ST["life1_post"] = l1
            ST["stage"] = "DONE"
            ST["stop"] = True
            say(f"P0 combat ended with no life change (P1 {l1}); DONE")
            return True
        return False

    return False


async def p1_tick(c, pid, state, acts):
    if await answer_moonmist_target(c, pid, state):
        return True
    if wf_player(state) == pid and wf_type(state) not in (
            None, "Priority", "DeclareAttackers", "DeclareBlockers"):
        wire("decision_held", {"who": c.name, "wf_type": wf_type(state),
                               "stage": ST.get("stage")})
        return True

    stage = ST["stage"]
    turn = state.get("turn_number") or 0

    if stage == "SETUP":
        if not is_my_main(state, pid):
            if (state.get("phase") or "") == "DeclareBlockers":
                for a in acts:
                    if a["type"] == "DeclareBlockers":
                        sub = copy.deepcopy(a)
                        sub["data"]["assignments"] = []
                        await submit_as_is(c, sub)
                        return True
            return False
        if not bf_named(state, pid, BEAR):
            oid = find_hand(state, pid, BEAR)
            a = castspell_advertised(acts, oid)
            if a and len(untapped_lands(state, pid)) >= 2:
                await submit_as_is(c, a)
                say("P1 casts Grizzly Bears")
                return True
        if not bf_named(state, pid, WOLF):
            oid = find_hand(state, pid, WOLF)
            a = castspell_advertised(acts, oid)
            if a and len(untapped_lands(state, pid)) >= 1:
                await submit_as_is(c, a)
                say("P1 casts Young Wolf")
                return True
        await play_land(c, pid, state, acts)
        return False

    if stage == "AWAIT_A":
        # declare the proof attack on P1's DeclareAttackers, one turn after
        # setup completed (summoning sickness cleared)
        if (state.get("active_player") == 1
                and (state.get("phase") or "") == "DeclareAttackers"
                and ST["setup_turn"] is not None
                and turn > ST["setup_turn"]
                and not ST["p1_attack_done"]):
            bears = bf_named(state, 1, BEAR)
            wolves = bf_named(state, 1, WOLF)
            for a in acts:
                if a["type"] == "DeclareAttackers" and bears and wolves:
                    sub = copy.deepcopy(a)
                    sub["data"]["attacks"] = [
                        [int(bears[0]), {"type": "Player", "data": 0}],
                        [int(wolves[0]), {"type": "Player", "data": 0}]]
                    sub["data"]["bands"] = []
                    await submit_as_is(c, sub)
                    ST["p1_attack_done"] = True
                    ST["stage"] = "PROOF_A"
                    say("P1 attacks P0 with Bear + Wolf; stage -> PROOF_A")
                    return True
        if is_my_main(state, pid):
            await play_land(c, pid, state, acts)
        return False

    if stage in ("PROOF_A", "AWAIT_A_DMG"):
        # P1 passes priority; declares no blockers (P0 attacks later in B,
        # but P1 blocking then would corrupt damage math)
        if (state.get("phase") or "") == "DeclareBlockers":
            for a in acts:
                if a["type"] == "DeclareBlockers":
                    sub = copy.deepcopy(a)
                    sub["data"]["assignments"] = []
                    await submit_as_is(c, sub)
                    return True
        return False

    if stage in ("SETUP_B", "PROOF_B_ATK", "AWAIT_B_DMG"):
        if (state.get("phase") or "") == "DeclareBlockers":
            for a in acts:
                if a["type"] == "DeclareBlockers":
                    sub = copy.deepcopy(a)
                    sub["data"]["assignments"] = []
                    await submit_as_is(c, sub)
                    return True
        if is_my_main(state, pid):
            await play_land(c, pid, state, acts)
        return False

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

    take(FOREST, 2)
    take(BEAR, 1)
    take(WOLF, 1)
    if pid == 0:
        take(MOONMIST, 2)  # protect Moonmist for both proofs
    else:
        take(MOONMIST, 0)
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
    # empty attackers fallback (P1 attacks only in AWAIT_A; P0 in PROOF_B_ATK)
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
        if not ST["stall_logged"] and turn > 25 and ST["stage"] in (
                "SETUP", "AWAIT_A"):
            ST["stall_logged"] = True
            sstate = (st0.get("state") or {}) if st0 else {}
            hand = [oname(objs(sstate).get(oid, {}))
                    for oid in hand_oids(sstate, 0)]
            say(f"SETUP STALL? turn {turn} stage {ST['stage']}: "
                f"P0 hand={hand} p0bf={[oname(o) for _, o in bf(sstate, 0)]} "
                f"p1bf={[oname(o) for _, o in bf(sstate, 1)]} "
                f"wf={sstate.get('waiting_for')}")
            wire("setup_stall", {"turn": turn, "hand": hand,
                                 "stage": ST["stage"]})
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
                          "stage": ST["stage"]})
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
    pre2 = env_state(load_env("pre2.json"))
    post2 = env_state(load_env("post2.json"))

    # A1
    if pre:
        p0b = len(bf_named(pre, 0, BEAR)) >= 1 and \
            len(bf_named(pre, 0, WOLF)) >= 1
        p1b = len(bf_named(pre, 1, BEAR)) >= 1 and \
            len(bf_named(pre, 1, WOLF)) >= 1
        l0 = life(pre, 0)
        ok = p0b and p1b and l0 == 20
        A["A1_setup_ok"] = "passed" if ok else "failed"
        D["A1_setup_ok"] = (f"p0_bear+wolf={p0b} p1_bear+wolf={p1b} "
                            f"p0_life_pre={l0}")
    else:
        A["A1_setup_ok"] = "not-run"
        D["A1_setup_ok"] = "pre.json missing (proof A never reached)"

    # A2
    if pre and pre2:
        a_ok = len(gy_named(pre, 0, MOONMIST)) >= 1 and \
            ST["moonmist_a_resolved"]
        b_ok = ST["moonmist_b_resolved"] and ST.get("b_cast_done", False)
        ok = a_ok and b_ok
        A["A2_moonmist_resolves"] = "passed" if ok else "failed"
        D["A2_moonmist_resolves"] = (
            f"proofA: resolved={ST['moonmist_a_resolved']} "
            f"(gy>=1: {len(gy_named(pre, 0, MOONMIST)) >= 1}); "
            f"proofB: cast={ST.get('b_cast_done', False)} "
            f"resolved={ST['moonmist_b_resolved']}; "
            f"target_prompt_seen={ST['target_prompt_seen']}")
    else:
        A["A2_moonmist_resolves"] = "not-run"
        D["A2_moonmist_resolves"] = "pre/pre2 missing"

    # A3
    if post and ST["life0_pre"] is not None and ST["life0_post"] is not None:
        delta = ST["life0_pre"] - ST["life0_post"]
        A["A3_p1_attack_prevented"] = "passed" if delta == 1 else "failed"
        D["A3_p1_attack_prevented"] = (
            f"P1 attacked P0 with unblocked Bear(2/2)+Wolf(1/1); "
            f"P0 life {ST['life0_pre']} -> {ST['life0_post']} (delta {delta}); "
            f"expected 1 (Bear 0 + Wolf 1); delta 3 = no prevention, "
            f"delta 0 = over-prevention")
    else:
        A["A3_p1_attack_prevented"] = "not-run"
        D["A3_p1_attack_prevented"] = "post.json missing"

    # A4
    if post2 and ST["life1_pre"] is not None \
            and ST["life1_post"] is not None:
        delta = ST["life1_pre"] - ST["life1_post"]
        A["A4_p0_attack_prevented"] = "passed" if delta == 1 else "failed"
        D["A4_p0_attack_prevented"] = (
            f"P0 attacked P1 with unblocked Bear(2/2)+Wolf(1/1); "
            f"P1 life {ST['life1_pre']} -> {ST['life1_post']} (delta {delta}); "
            f"expected 1 (Bear 0 + Wolf 1)")
    else:
        A["A4_p0_attack_prevented"] = "not-run"
        D["A4_p0_attack_prevented"] = "post2.json missing"

    # A5
    if post2:
        ok = stack_empty(post2) and ST["stage"] == "DONE"
        A["A5_cleanup"] = "passed" if ok else "failed"
        D["A5_cleanup"] = (f"stack_empty={stack_empty(post2)} "
                           f"final_stage={ST['stage']} "
                           f"turn={post2.get('turn_number')}")
    elif post:
        ok = stack_empty(post)
        A["A5_cleanup"] = "passed" if ok else "failed"
        D["A5_cleanup"] = (f"post2 missing; post.json stack_empty={ok} "
                           f"final_stage={ST['stage']}")
    else:
        A["A5_cleanup"] = "not-run"
        D["A5_cleanup"] = "no post state exported"

    if A["A1_setup_ok"] != "passed":
        verdict = "blocked"
    elif A["A3_p1_attack_prevented"] == "failed" \
            or A["A4_p0_attack_prevented"] == "failed":
        verdict = "reproduced"
    elif A["A3_p1_attack_prevented"] == "passed" \
            and A["A4_p0_attack_prevented"] == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    say("ASSERTIONS: " + json.dumps(A, indent=1))
    say("VERDICT (proposal): " + verdict)

    # ---- run.json ----
    sh = ST["server_hello"]
    run = {
        "issue": 6899,
        "run_id": RUN_ID,
        "title": "Moonmist doesn't prevent damage",
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
            "p1_attack_done": ST["p1_attack_done"],
            "moonmist_a_resolved": ST["moonmist_a_resolved"],
            "b_cast_done": ST.get("b_cast_done", False),
            "moonmist_b_resolved": ST["moonmist_b_resolved"],
            "target_prompt_seen": ST["target_prompt_seen"],
            "target_attempts": TARGET_ATTEMPTS,
            "life0_pre": ST["life0_pre"],
            "life0_post": ST["life0_post"],
            "life1_pre": ST["life1_pre"],
            "life1_post": ST["life1_post"],
            "rejections": ST["rejections"],
        },
        "assertions": A,
        "assertion_details": D,
        "verdict": verdict,
        "verdict_note": ("proposal only, from A3/A4; the assertion table "
                         "is authoritative"),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client "
            "seats.",
            "8x card density is a test-harness convenience (engine accepts "
            ">4-of for custom games).",
            "No Humans on either battlefield, so the Transform clause was a "
            "no-op; the target prompt (if any) was driven through, not "
            "asserted (that defect is tracked in #6403).",
            "Werewolf subtype not separately tested; Young Wolf (Wolf) is "
            "the wolf-class representative.",
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
    with open(f"{BACKFILL}/driver/scenario_6899.py") as f:
        src = f.read()
    with open(f"{EVDIR}/scenario_6899.py", "w") as f:
        f.write(src)
    say("copied scenario_6899.py to evidence")

    excerpts = []
    try:
        slog = open(f"{BACKFILL}/server/server.log").read().splitlines()
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

    W, H = 1040, 940
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
    d.text((24, y), "#6899 - Moonmist doesn't prevent damage", fill=ACCENT)
    y += 26
    d.text((24, y),
           f"server v{srv.get('server_version')} ({srv.get('build_commit')}, "
           f"protocol {srv.get('protocol_version')}) | run {run['run_id']} | "
           f"{run['started_at'][:10]} | verdict: {run['verdict']} (proposal)",
           fill=DIM)
    y += 30

    lines = [
        "Oracle: Transform all Humans. Prevent all combat damage that would",
        "be dealt this turn by creatures other than Werewolves and Wolves.",
        "Proof A: P1 attacks P0 with unblocked Bear(2/2)+Wolf(1/1); P0 casts",
        "Moonmist in combat. Proof B: P0 casts Moonmist pre-combat, attacks",
        "P1 with Bear+Wolf. Correct: damage delta 1 (Bear 0, Wolf 1).",
    ]
    for ln in lines:
        d.text((24, y), ln, fill=TEXT)
        y += 20
    y += 8

    def plife(fn, pid):
        s = st_of(fn)
        return str(life(s, pid)) if s else "n/a"

    rows = [
        ("pre.json (after Moonmist A resolves)",
         f"P0 life {plife('pre.json', 0)}; Moonmist in P0 gy"),
        ("pre.json -> post.json (P1 combat)",
         f"P0 life {plife('pre.json', 0)} -> {plife('post.json', 0)} "
         f"(delta {run['decisions'].get('life0_pre', '?')}->"
         f"{run['decisions'].get('life0_post', '?')})"),
        ("pre2.json (after Moonmist B resolves)",
         f"P1 life {plife('pre2.json', 1)}; Moonmist in P0 gy"),
        ("pre2.json -> post2.json (P0 combat)",
         f"P1 life {plife('pre2.json', 1)} -> {plife('post2.json', 1)}"),
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
           "States: pre/post/pre2/post2.json + manifest.sha256",
           fill=DIM)
    out = os.path.join(EVDIR, "summary.png")
    img.save(out)
    print("wrote", out, flush=True)


def write_manifest():
    import hashlib
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
    import hashlib
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
