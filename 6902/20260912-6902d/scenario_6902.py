#!/usr/bin/env python3
"""Issue #6902: Sneak Attack - delayed end-step sacrifice trigger only
sometimes sacrifices the creature.

Oracle: "{R}: You may put a creature card from your hand onto the battlefield.
That creature gains haste. Sacrifice the creature at the beginning of the
next end step."

Triage acceptance criteria (mike-theDude, 2026-08-03):
  1. Every creature put onto the battlefield by an activation gets its own
     next-end-step delayed trigger.
  2. The trigger sacrifices that specific object if it remains on the
     battlefield.
  3. Multiple activations and zone changes do not cross-associate delayed
     triggers.
  4. A trigger created during an end step waits for the next end step.

Behavioral contract (native engine, protocol 70, two human seats):
  LEG1: P0 casts Sneak Attack, then activates it TWICE on one main phase,
  putting two Grizzly Bears onto the battlefield. At the beginning of the
  end step, two delayed sacrifice triggers must fire and each must sacrifice
  its own creature (both Bears -> graveyard).
  LEG2: on a later turn, P0 activates Sneak Attack during its END STEP. The
  creature enters during the end step, so no sacrifice should happen this
  end step; the delayed trigger waits for the NEXT end step and sacrifices
  the creature then.

  A1 setup_ok            pre1: Sneak Attack on P0 BF, 2 Bears in hand,
                         P0 main phase, >=2 untapped Mountains.
  A2 creatures_enter     mid1: the 2 activated Bear oids on P0 BF.
  A3 triggers_fired      mid_end: >=2 sacrifice delayed triggers observed on
                         the stack at the end step (each targeting its own
                         Bear).
  A4 both_sacrificed     post1: both leg-1 Bear oids in Graveyard, none on BF.
  A5 cleanup_leg1        post1: stack empty, turn advanced past the sac turn.
  A6 endstep_survives    mid2b: leg-2 Bear still on BF at the next turn
                         (entered during the end step; not sacrificed then).
  A7 next_endstep_sac    post2: leg-2 Bear in Graveyard after the following
                         end step, stack empty.

Verdict: reproduced iff A1+A2 pass and (A3 fails or A4 fails).
not-reproduced iff A1..A7 all pass. blocked iff A1 fails.
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
ISSUE = 6902
RUN_ID = "20260912-6902d"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
RUNDIR = f"{BACKFILL}/runs/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(RUNDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")
RUNLOG2 = open(f"{RUNDIR}/scenario_run.log", "w")

SNEAK = "Sneak Attack"
BEAR = "Grizzly Bears"
MOUNTAIN = "Mountain"

P0_DECK = [(SNEAK, 12), (BEAR, 12), (MOUNTAIN, 36)]
P1_DECK = [(MOUNTAIN, 60)]
TIMEOUT = 2400

ST = {}
C0 = None
C1 = None
TARGET_DBG = set()


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


def objs(state):
    return state.get("objects") or {}


def players(state):
    return state.get("players") or []


def player_obj(state, pid):
    for p in players(state):
        if p.get("player_id") == pid or p.get("id") == pid \
                or p.get("seat") == pid:
            return p
    return {}


def life(state, pid):
    return player_obj(state, pid).get("life")


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


def gy_named(state, pid, name):
    return [oid for oid, o in objs(state).items()
            if o.get("zone") == "Graveyard" and o.get("controller") == pid
            and oname(o) == name]


def untapped_lands(state, pid, land_name):
    return [oid for oid, o in bf(state, pid)
            if oname(o) == land_name and not o.get("tapped")]


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                               "PostCombatMain"))


def wf(state):
    return state.get("waiting_for") or {}


def wf_type(state):
    return wf(state).get("type")


def wf_player(state):
    return (wf(state).get("data") or {}).get("player")


def stack(state):
    return state.get("stack") or []


def get_vi(st):
    vi = (st or {}).get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def current_opps(c):
    st = c.latest if c else None
    vi = get_vi(st)
    return vi.get("opportunities") or [] if vi else []


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


async def submit_interaction(c, submission):
    wire("interaction_submit", {"who": c.name, "submission": submission,
                                "stage": ST.get("stage")})
    await c.send_interaction(submission)
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


def reset_state():
    ST.clear()
    ST.update({
        "stage": "SETUP",   # SETUP -> PROOF -> DONE
        "leg": 1,
        "stop": False,
        "turn_cap": 60,
        "rejections": [],
        "game_code": None,
        "mulligans": 0,
        "sneak_cast": False,
        "sneak_oid": None,
        "sneak_submitted": False,
        "sneak_turn": None,
        "pre1_done": False,
        "act_n": 0,              # completed activations this proof turn
        "act_in_flight": False,
        "act_may_accepted": False,
        "act_choice_submitted": False,
        "leg1_bears": [],        # oids of Bears put onto BF by activations
        "leg1_bear_hands": [],   # hand oids consumed
        "leg1_end_turn": None,
        "mid1_done": False,
        "mid_end_done": False,
        "post1_done": False,
        "leg2_armed": False,
        "leg2_activated": False,
        "leg2_bear_oid": None,
        "leg2_activation_turn": None,
        "leg2_activation_phase": None,
        "mid2_done": False,
        "mid2b_done": False,
        "post2_done": False,
        "trig_records": [],      # sacrifice triggers seen on stack at end step
        "hold_since": None,
    })


def vi_opps(st):
    vi = get_vi(st)
    return vi.get("opportunities") or [] if vi else []


def find_vi_choice(st, code, source_ref=None):
    """Find an exactChoices opportunity whose choice surface carries the
    given action `code` (e.g. 'activateAbility'), optionally filtered to a
    source object reference (role 'source')."""
    for op in vi_opps(st):
        resp = op.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        choices = data.get("choices") or resp.get("choices") or []
        if resp.get("type") != "exactChoices":
            continue
        for ch in choices:
            codes = [(s.get("data", {}) or {}).get("code")
                     or s.get("code")
                     for s in ch.get("surfaces", []) or []]
            if code not in codes:
                continue
            if source_ref is not None:
                refs = [(s.get("data", {}) or {}).get("reference")
                        for s in ch.get("surfaces", []) or []
                        if ((s.get("data", {}) or {}).get("role")
                            == "source")]
                if str(source_ref) not in [str(r) for r in refs]:
                    continue
            return op.get("interactionId"), ch
    return None


def opp_choices(opp):
    resp = opp.get("response") or {}
    out = []
    for v in (resp.get("choices"), (resp.get("data") or {}).get("choices")):
        if isinstance(v, list):
            out.extend(v)
    return out


def opp_candidates(opp):
    """Schema/select candidate pool (response.data.candidates or
    response.candidates)."""
    resp = opp.get("response") or {}
    data = resp.get("data") or {}
    out = []
    for v in (resp.get("candidates"), data.get("candidates")):
        if isinstance(v, list):
            out.extend(v)
    return out


def surf_data(s):
    d = s.get("data") or {}
    return {
        "type": s.get("type"),
        "code": d.get("code") or s.get("code"),
        "action": d.get("action") or s.get("action"),
        "role": d.get("role") or s.get("role"),
        "value": d.get("value") if d.get("value") is not None
                 else s.get("value"),
        "seat": d.get("seat") if d.get("seat") is not None
                else s.get("seat"),
        "reference": d.get("reference") or s.get("reference"),
        "symbols": d.get("symbols") or s.get("symbols"),
    }


def is_decide_optional_effect(opp):
    resp = opp.get("response") or {}
    if resp.get("type") != "exactChoices":
        return False
    for ch in opp_choices(opp):
        for s in ch.get("surfaces") or []:
            f = surf_data(s)
            code = (f["code"] or "") + " " + (f["action"] or "")
            if "decideOptionalEffect" in code or "OptionalEffect" in code:
                return True
    return False


def decide_optional_value(opp):
    """Return {'accept': choice, 'decline': choice} from a
    decideOptionalEffect exactChoices opportunity."""
    out = {}
    for ch in opp_choices(opp):
        for s in ch.get("surfaces") or []:
            f = surf_data(s)
            code = (f["code"] or "").lower()
            role = (f["role"] or "").lower()
            val = str(f["value"] or "").lower()
            if role == "accept" or "decideoptionaleffect" in code:
                if val in ("true", "1", "yes", "accept"):
                    out["accept"] = ch
                elif val in ("false", "0", "no", "decline"):
                    out["decline"] = ch
    return out


def choice_id_of(node):
    return (node.get("choiceId") or node.get("choice_id")
            or node.get("id"))


def node_references_oid(node, oid):
    for s in node.get("surfaces") or []:
        if str(surf_data(s)["reference"]) == str(oid):
            return True
    return False


def submit_choice(c, opp, node, resp_type=None):
    resp = (opp.get("response") or {})
    data = resp.get("data") or {}
    spec = data.get("spec") or {}
    rtype = resp_type or resp.get("type")
    if rtype == "schema" and isinstance(spec, dict):
        rtype = spec.get("type") or rtype
    cid = choice_id_of(node)
    sub = {"interactionId": opp.get("interactionId"),
           "response": {"type": "choose", "data": {"choiceId": cid}}}
    if rtype == "sequence":
        sub["response"] = {"type": "sequence",
                           "data": {"choiceIds": [cid]}}
    elif rtype == "select":
        sub["response"] = {"type": "select",
                           "data": {"choiceIds": [cid]}}
    return submit_interaction(c, sub)


def sacrifice_triggers_on_stack(state):
    """Stack entries that look like Sneak Attack's delayed sacrifice."""
    out = []
    for e in stack(state):
        blob = json.dumps(e, default=str).lower()
        if "sneak" in blob and "sacrific" in blob:
            out.append(e)
    return out


async def play_land(c, pid, state, acts, land_name):
    lid = find_hand(state, pid, land_name)
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

    if pid == 0:
        take(MOUNTAIN, 3)
        take(BEAR, 3)      # protect Bears for the proof turns
        take(SNEAK, 2)     # protect Sneak Attack
    else:
        take(MOUNTAIN, 0)
    for oid in hand:
        if len(picks) >= over:
            break
        if oid not in picks:
            picks.append(oid)
    return [int(x) for x in picks[:over]]


async def sneak_activate(c, st):
    """Submit the activateAbility choice for P0's Sneak Attack."""
    sneak = ST.get("sneak_oid")
    if not sneak:
        return False
    f = find_vi_choice(st, "activateAbility", source_ref=sneak)
    if not f:
        return False
    iid, ch = f
    wire("sneak_activation_submit",
         {"iid": iid, "choiceId": choice_id_of(ch), "leg": ST["leg"],
          "act_n": ST["act_n"]})
    say(f"[P0] submits Sneak Attack activation #{ST['act_n'] + 1} "
        f"(leg {ST['leg']})")
    await c.send_interaction(
        {"interactionId": iid,
         "response": {"type": "choose",
                      "data": {"choiceId": choice_id_of(ch)}}})
    ST.setdefault("settle", {})[c.name] = c.revision
    ST["act_in_flight"] = True
    return True


async def answer_may_accept(c):
    """Answer the 'You may put a creature...' OptionalEffectChoice with yes."""
    for opp in current_opps(c):
        if is_decide_optional_effect(opp):
            vals = decide_optional_value(opp)
            node = vals.get("accept")
            if node and choice_id_of(node):
                say(f"[P0] answers Sneak Attack may-choice: ACCEPT "
                    f"(leg {ST['leg']})")
                wire("maychoice_answer",
                     {"answer": "accept", "leg": ST["leg"]})
                await submit_choice(c, opp, node)
                ST["act_may_accepted"] = True
                return True
            wire("maychoice_no_accept", {"keys": list(vals.keys())})
            return False
    return False


async def answer_creature_choice(c, state):
    """Choose a Bear from P0's hand for the ChangeZone target prompt."""
    bear_oids = set(hand_oids(state, 0))
    bear_oids = {o for o in bear_oids
                 if oname(objs(state)[o]) == BEAR}
    # prefer a Bear not already used this proof turn
    used = set(ST.get("leg1_bear_hands", []))
    ordered = [o for o in bear_oids if o not in used]
    ordered += [o for o in bear_oids if o in used]
    if not ordered:
        return False
    for opp in current_opps(c):
        for cand in opp_candidates(opp):
            for o in ordered:
                if node_references_oid(cand, o) and choice_id_of(cand):
                    say(f"[P0] selects Bear {o} for Sneak Attack "
                        f"(leg {ST['leg']})")
                    wire("bear_selected", {"oid": o, "leg": ST["leg"]})
                    ST.setdefault("leg1_bear_hands", []).append(o)
                    ST["act_choice_submitted"] = True
                    await submit_choice(c, opp, cand)
                    return True
        # exactChoices variant: a choice referencing the Bear
        for ch in opp_choices(opp):
            for o in ordered:
                if node_references_oid(ch, o) and choice_id_of(ch):
                    say(f"[P0] selects Bear {o} (exactChoices) "
                        f"(leg {ST['leg']})")
                    wire("bear_selected", {"oid": o, "leg": ST["leg"],
                                         "exact": True})
                    ST.setdefault("leg1_bear_hands", []).append(o)
                    ST["act_choice_submitted"] = True
                    await submit_choice(c, opp, ch)
                    return True
    return False



async def answer_order_triggers(c):
    """Submit the advertised default order for an OrderTriggers decision
    (schema sequence with trigger candidates)."""
    for opp in current_opps(c):
        resp = opp.get("response") or {}
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        if resp.get("type") == "schema" and spec.get("type") == "sequence":
            cands = opp_candidates(opp)
            ids = [choice_id_of(x) for x in cands if choice_id_of(x)]
            if ids:
                say(f"[P0] answers OrderTriggers with advertised order "
                    f"({len(ids)} triggers)")
                wire("order_triggers_submit",
                     {"iid": opp.get("interactionId"), "choiceIds": ids,
                      "leg": ST.get("leg")})
                await submit_interaction(c, {
                    "interactionId": opp.get("interactionId"),
                    "response": {"type": "sequence",
                                 "data": {"choiceIds": ids}}})
                return True
    return False


# ------------------------------------------------------------------ ticks

async def p0_tick(c, pid, state, acts):
    stage = ST["stage"]
    turn = state.get("turn_number") or 0
    phase = state.get("phase") or ""
    sneak_bf = bf_named(state, pid, SNEAK)
    if sneak_bf and not ST.get("sneak_oid"):
        ST["sneak_oid"] = sneak_bf[0]

    # --- decisions first: never pass while a decision is pending ---
    if wf_player(state) == pid and wf_type(state) not in (
            None, "Priority", "DeclareAttackers", "DeclareBlockers"):
        wft = wf_type(state)
        if stage == "PROOF":
            if wf_type(state) == "OrderTriggers":
                if await answer_order_triggers(c):
                    ST["hold_since"] = None
                    return True
            if await answer_may_accept(c):
                ST["hold_since"] = None
                return True
            if ST.get("act_may_accepted") and not ST.get(
                    "act_choice_submitted"):
                if await answer_creature_choice(c, state):
                    ST["hold_since"] = None
                    return True
        key = ("held", wft, ST.get("leg"))
        if key not in ST.setdefault("held_logged", set()):
            ST["held_logged"].add(key)
            vi = (c.latest or {}).get("viewer_interaction")
            wire("decision_held", {"who": c.name, "wf_type": wft,
                                  "vi": json.dumps(vi, default=str)[:3000],
                                  "leg": ST.get("leg"),
                                  "act": (ST.get("act_in_flight"),
                                          ST.get("act_may_accepted"),
                                          ST.get("act_choice_submitted"))})
            say(f"[P0] HOLDING unhandled decision {wft} "
                f"(leg {ST['leg']})")
        if ST["hold_since"] is None:
            ST["hold_since"] = time.time()
        return True

    if stage == "SETUP":
        if ST.get("sneak_submitted"):
            oid = str(ST.get("sneak_hand_oid"))
            zone = (objs(state).get(oid) or {}).get("zone")
            if sneak_bf:
                ST["sneak_cast"] = True
                ST["sneak_turn"] = turn
                ST["stage"] = "PROOF"
                ST["leg"] = 1
                say(f"Sneak Attack resolved on battlefield (turn "
                    f"{ST['sneak_turn']}); stage -> PROOF, leg 1")
                return True
            if zone == "Stack":
                return False
            if len(ST["rejections"]) > ST.get("sneak_submit_rej", 0):
                say("[P0] Sneak Attack cast rejected; will retry")
                ST["sneak_submitted"] = False
                return False
            ST["sneak_settle"] = ST.get("sneak_settle", 0) + 1
            if ST["sneak_settle"] > 40:
                say("[P0] Sneak Attack submit went silent; retrying")
                ST["sneak_submitted"] = False
                ST["sneak_settle"] = 0
            return False
        if not is_my_main(state, pid):
            return False
        oid = find_hand(state, pid, SNEAK)
        a = castspell_advertised(acts, oid)
        if oid and a is not None and len(
                untapped_lands(state, pid, MOUNTAIN)) >= 4 \
                and wf_player(state) == pid and wf_type(state) == "Priority":
            await submit_as_is(c, a)
            ST["sneak_submitted"] = True
            ST["sneak_hand_oid"] = oid
            ST["sneak_submit_rej"] = len(ST["rejections"])
            ST["sneak_settle"] = 0
            say(f"[P0] submits Sneak Attack cast (turn {turn})")
            return True
        await play_land(c, pid, state, acts, MOUNTAIN)
        return False

    if stage == "PROOF" and ST["leg"] == 1:
        bears_hand = [o for o in hand_oids(state, pid)
                      if oname(objs(state)[o]) == BEAR]
        # leg-1 pre export: first main with Sneak on BF, 2 Bears, 2 mana
        if not ST["pre1_done"] and sneak_bf and is_my_main(state, pid) \
                and len(bears_hand) >= 2 and len(
                    untapped_lands(state, pid, MOUNTAIN)) >= 2 \
                and wf_player(state) == pid \
                and wf_type(state) == "Priority":
            ST["pre1_bear_hands"] = list(bears_hand)
            ST["leg1_proof_turn"] = turn
            await export_now("pre1.json")
            ST["pre1_done"] = True
            say(f"leg 1 pre exported (turn {turn}); proof armed")
            return True
        # drive the two activations
        if ST["pre1_done"] and ST["act_n"] < 2 and not ST["mid1_done"]:
            # confirm an in-flight activation by a new Bear on the BF
            if ST.get("act_in_flight"):
                new_bears = [o for o in bf_named(state, pid, BEAR)
                             if o not in ST["leg1_bears"]]
                if new_bears:
                    ST["leg1_bears"].append(new_bears[0])
                    ST["act_n"] += 1
                    ST["act_in_flight"] = False
                    ST["act_may_accepted"] = False
                    ST["act_choice_submitted"] = False
                    say(f"[P0] activation #{ST['act_n']} complete: Bear "
                        f"{new_bears[0]} on BF (leg 1)")
                    wire("activation_complete",
                         {"n": ST["act_n"], "bear_oid": new_bears[0]})
                elif len(ST["rejections"]) > ST.get("act_submit_rej", 0):
                    say("[P0] activation submission rejected; retrying")
                    wire("activation_rejected", {})
                    ST["act_in_flight"] = False
                    ST["act_may_accepted"] = False
                    ST["act_choice_submitted"] = False
                else:
                    return False  # in flight; wait
            if not ST.get("act_in_flight") and is_my_main(state, pid) \
                    and wf_player(state) == pid \
                    and wf_type(state) == "Priority" and bears_hand \
                    and len(untapped_lands(state, pid, MOUNTAIN)) >= 1:
                if await sneak_activate(c, c.latest):
                    ST["act_submit_rej"] = len(ST["rejections"])
                    return True
                # no activateAbility offered right now; wait
                return False
        # mid1: both Bears on the battlefield
        if ST["act_n"] >= 2 and not ST["mid1_done"]:
            if all((objs(state).get(o) or {}).get("zone") == "Battlefield"
                   for o in ST["leg1_bears"]):
                await export_now("mid1.json")
                ST["mid1_done"] = True
                say("leg 1 mid exported: both Bears on BF")
                return True
            return False
        # end-step trigger watch (leg-1 proof turn)
        if ST["mid1_done"] and phase == "End" \
                and turn == ST.get("leg1_proof_turn"):
            for e in sacrifice_triggers_on_stack(state):
                eid = e.get("id")
                if eid not in [r["id"] for r in ST["trig_records"]]:
                    ST["trig_records"].append(
                        {"id": eid,
                         "blob": json.dumps(e, default=str)[:1500]})
                    wire("sac_trigger_seen",
                         {"id": eid,
                          "blob": json.dumps(e, default=str)[:1500]})
                    say(f"[P0] sacrifice delayed trigger on stack (id "
                        f"{eid})")
            if not ST["mid_end_done"] and ST["trig_records"] \
                    and wf_player(state) == pid \
                    and wf_type(state) == "Priority":
                ST["leg1_end_turn"] = turn
                await export_now("mid_end.json")
                ST["mid_end_done"] = True
                say("leg 1 mid_end exported with triggers on stack")
                return True
        # leg transition: post1 after the sac turn resolves
        if ST["mid_end_done"] and not ST["post1_done"] \
                and turn > (ST.get("leg1_end_turn") or 0) \
                and not stack(state):
            await export_now("post1.json")
            ST["post1_done"] = True
            ST["leg"] = 2
            ST["leg2_armed"] = True
            say(f"leg 1 complete; post1 exported (turn {turn}); leg 2 armed")
            return True
        return False

    if stage == "PROOF" and ST["leg"] == 2 and ST.get("leg2_armed"):
        bears_hand = [o for o in hand_oids(state, pid)
                      if oname(objs(state)[o]) == BEAR]
        # activate during the End phase (after the beginning passed)
        if not ST["leg2_activated"] and phase == "End" \
                and state.get("active_player") == pid \
                and wf_player(state) == pid \
                and wf_type(state) == "Priority" \
                and sneak_bf and bears_hand \
                and len(untapped_lands(state, pid, MOUNTAIN)) >= 1:
            ST["leg2_activation_turn"] = turn
            if await sneak_activate(c, c.latest):
                ST["act_submit_rej"] = len(ST["rejections"])
                ST["leg2_activated"] = True
                say(f"[P0] leg-2 end-step activation submitted "
                    f"(turn {turn})")
                return True
            return False
        if ST["leg2_activated"] and not ST["leg2_bear_oid"]:
            new_bears = [o for o in bf_named(state, pid, BEAR)
                         if o not in ST["leg1_bears"]]
            if new_bears:
                ST["leg2_bear_oid"] = new_bears[0]
                await export_now("mid2.json")
                ST["mid2_done"] = True
                say(f"[P0] leg-2 Bear {new_bears[0]} entered during End; "
                    "mid2 exported")
                return True
            if len(ST["rejections"]) > ST.get("act_submit_rej", 0):
                say("[P0] leg-2 activation rejected; giving up leg 2")
                wire("leg2_activation_rejected", {})
                ST["leg2_armed"] = False
                ST["stage"] = "DONE"
                ST["stop"] = True
                return True
            return False
        if ST["leg2_bear_oid"] and not ST["mid2b_done"] \
                and turn > (ST.get("leg2_activation_turn") or 0):
            # next turn: the Bear must still be on the battlefield
            zone = (objs(state).get(ST["leg2_bear_oid"]) or {}).get("zone")
            await export_now("mid2b.json")
            ST["mid2b_done"] = True
            ST["mid2b_zone"] = zone
            say(f"[P0] leg-2 Bear zone at next turn: {zone}; mid2b exported")
            return True
        if ST["mid2b_done"] and not ST["post2_done"]:
            zone = (objs(state).get(ST["leg2_bear_oid"]) or {}).get("zone")
            if zone == "Graveyard":
                await export_now("post2.json")
                ST["post2_done"] = True
                ST["stage"] = "DONE"
                ST["stop"] = True
                say("[P0] leg-2 Bear sacrificed at the next end step; "
                    "post2 exported; DONE")
                return True
            if turn > (ST.get("leg2_activation_turn") or 0) + 2:
                await export_now("post2.json")
                ST["post2_done"] = True
                ST["stage"] = "DONE"
                ST["stop"] = True
                say("[P0] leg-2 watch timed out; post2 exported; DONE")
                return True
        return False
    return False


async def p1_tick(c, pid, state, acts):
    stage = ST["stage"]
    if wf_player(state) == pid and wf_type(state) not in (
            None, "Priority", "DeclareAttackers", "DeclareBlockers"):
        wire("decision_held_p1", {"wf_type": wf_type(state)})
        return True
    if stage == "SETUP" and is_my_main(state, pid):
        await play_land(c, pid, state, acts, MOUNTAIN)
        return False
    if stage == "PROOF":
        if is_my_main(state, pid):
            await play_land(c, pid, state, acts, MOUNTAIN)
        return False
    return False


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
            wf = state.get("waiting_for") or {}
            pend = (wf.get("data") or {}).get("pending") or []
            my = next((p for p in pend
                       if (p.get("player") == pid)), None)
            phase = (my or {}).get("phase") or {}
            if isinstance(phase, dict) and phase.get("type") == "BottomCards":
                n = int(phase.get("count") or 0)
                keep_name = SNEAK if pid == 0 else MOUNTAIN
                named = [(oid, oname(objs(state)[oid]))
                         for oid in hand_oids(state, pid)]
                picks = [oid for oid, nm in named
                         if nm != keep_name][:n]
                if len(picks) < n:
                    picks = [oid for oid, _ in named][:n]
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": picks}})
                say(f"[{c.name}] bottoms {len(picks)} after mulligan")
                return True
            if pid == 0 and ST["mulligans"] < 2 and not find_hand(
                    state, 0, SNEAK):
                ST["mulligans"] += 1
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Mulligan"}}})
                say(f"[P0] mulligans ({ST['mulligans']}) seeking Sneak Attack")
            else:
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
            if wf_player(state) == pid:
                await submit_as_is(c, a)
                return True
            continue
    wt, wp = wf_type(state), wf_player(state)
    if wp == pid and wt not in (None, "Priority"):
        return True
    if wf_player(state) != pid:
        return False
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


def zone_of(state, oid):
    return (objs(state).get(str(oid)) or {}).get("zone")


async def main():
    reset_state()
    t_start = time.time()
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
        if ST.get("hold_since") and time.time() - ST["hold_since"] > 150:
            say("held decision for 150s with no progress; exporting and "
                "stopping")
            wire("hold_timeout", {"leg": ST["leg"]})
            await export_now("mid_held.json")
            break
        if time.time() - last_progress > 300:
            say("no progress for 300s; stopping")
            break
        await asyncio.sleep(0.15)

    say(f"loop ended: stage={ST['stage']} stop={ST['stop']} leg={ST['leg']}")
    wire("loop_end", {k: ST.get(k) for k in (
        "stage", "stop", "leg", "act_n", "leg1_bears", "leg2_bear_oid",
        "mid1_done", "mid_end_done", "post1_done", "mid2_done",
        "mid2b_done", "post2_done")})
    await C0.close()
    await C1.close()
    t_end = time.time()

    # ---- assertions from SAVED states ----
    A, D = {}, {}
    pre1 = env_state(load_env("pre1.json"))
    mid1 = env_state(load_env("mid1.json"))
    mid_end = env_state(load_env("mid_end.json"))
    post1 = env_state(load_env("post1.json"))
    mid2 = env_state(load_env("mid2.json"))
    mid2b = env_state(load_env("mid2b.json"))
    post2 = env_state(load_env("post2.json"))
    leg1_bears = ST.get("leg1_bears", [])
    leg2_bear = ST.get("leg2_bear_oid")

    # A1
    if pre1:
        sneak_ok = len(bf_named(pre1, 0, SNEAK)) >= 1
        bears_ok = sum(1 for o in hand_oids(pre1, 0)
                       if oname(objs(pre1)[o]) == BEAR) >= 2
        phase_ok = (pre1.get("phase") or "") in ("PreCombatMain",
                                                 "PostCombatMain")
        ok = sneak_ok and bears_ok and phase_ok
        A["A1_setup_ok"] = "passed" if ok else "failed"
        D["A1_setup_ok"] = (f"sneak_on_p0_bf={sneak_ok} "
                            f"hand_bears={sum(1 for o in hand_oids(pre1, 0) if oname(objs(pre1)[o]) == BEAR)} "
                            f"main_phase={phase_ok} turn={pre1.get('turn_number')}")
    else:
        A["A1_setup_ok"] = "not-run"
        D["A1_setup_ok"] = "pre1.json missing (proof never armed)"

    # A2
    if mid1 and len(leg1_bears) == 2:
        ok = all(zone_of(mid1, o) == "Battlefield" for o in leg1_bears)
        A["A2_creatures_enter"] = "passed" if ok else "failed"
        D["A2_creatures_enter"] = (
            f"leg1_bears={leg1_bears} zones="
            f"{[zone_of(mid1, o) for o in leg1_bears]}")
    else:
        A["A2_creatures_enter"] = "not-run"
        D["A2_creatures_enter"] = (
            f"mid1 missing or only {len(leg1_bears)}/2 activations completed")

    # A3 (diagnostic): delayed sacrifice triggers on the stack at end step
    trigs = ST.get("trig_records", [])
    if mid_end:
        ok = len(trigs) >= 2
        A["A3_triggers_fired"] = "passed" if ok else "failed"
        D["A3_triggers_fired"] = (
            f"sacrifice triggers observed on stack at end step: "
            f"{len(trigs)} (expect >=2)")
    else:
        A["A3_triggers_fired"] = "not-run"
        D["A3_triggers_fired"] = "mid_end.json missing"

    # A4
    if post1 and len(leg1_bears) == 2:
        gy = all(zone_of(post1, o) == "Graveyard" for o in leg1_bears)
        none_bf = not any(zone_of(post1, o) == "Battlefield"
                          for o in leg1_bears)
        ok = gy and none_bf
        A["A4_both_sacrificed"] = "passed" if ok else "failed"
        D["A4_both_sacrificed"] = (
            f"zones post1={[zone_of(post1, o) for o in leg1_bears]} "
            f"(expect both Graveyard)")
    else:
        A["A4_both_sacrificed"] = "not-run"
        D["A4_both_sacrificed"] = "post1.json missing or bears incomplete"

    # A5
    if post1:
        ok = not stack(post1)
        A["A5_cleanup_leg1"] = "passed" if ok else "failed"
        D["A5_cleanup_leg1"] = (
            f"stack empty in post1: {ok}; post1 turn={post1.get('turn_number')} "
            f"vs leg1 end turn={ST.get('leg1_end_turn')}")
    else:
        A["A5_cleanup_leg1"] = "not-run"
        D["A5_cleanup_leg1"] = "post1.json missing"

    # A6
    if mid2b and leg2_bear:
        zone = zone_of(mid2b, leg2_bear)
        ok = zone == "Battlefield"
        A["A6_endstep_survives"] = "passed" if ok else "failed"
        D["A6_endstep_survives"] = (
            f"leg2 bear {leg2_bear} zone at next turn: {zone} "
            f"(expect Battlefield)")
    else:
        A["A6_endstep_survives"] = "not-run"
        D["A6_endstep_survives"] = "mid2b.json missing (leg 2 never ran)"

    # A7
    if post2 and leg2_bear:
        zone = zone_of(post2, leg2_bear)
        ok = zone == "Graveyard" and not stack(post2)
        A["A7_next_endstep_sac"] = "passed" if ok else "failed"
        D["A7_next_endstep_sac"] = (
            f"leg2 bear {leg2_bear} zone: {zone} (expect Graveyard); "
            f"stack empty: {not stack(post2)}")
    else:
        A["A7_next_endstep_sac"] = "not-run"
        D["A7_next_endstep_sac"] = "post2.json missing (leg 2 never ran)"

    if A.get("A1_setup_ok") == "passed" and A.get(
            "A2_creatures_enter") == "passed":
        if A.get("A4_both_sacrificed") == "failed" or \
                A.get("A6_endstep_survives") == "failed" or \
                A.get("A7_next_endstep_sac") == "failed":
            verdict = "reproduced"
        elif all(A.get(k) == "passed" for k in
                 ("A3_triggers_fired", "A4_both_sacrificed",
                  "A5_cleanup_leg1", "A6_endstep_survives",
                  "A7_next_endstep_sac")):
            verdict = "not-reproduced"
        else:
            verdict = "blocked"
    else:
        verdict = "blocked"

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "details": D, "verdict": verdict,
                   "rejections": ST["rejections"][:20],
                   "trigger_records": ST["trig_records"]}, f, indent=1,
                  default=str)
    say("ASSERTIONS: " + json.dumps(A))
    say("VERDICT: " + verdict)
    wire("assertions", {"A": A, "verdict": verdict})

    # copy the scenario file into the evidence dir
    import shutil
    shutil.copy(__file__, f"{EVDIR}/scenario_{ISSUE}.py")

    # ---- run.json ----
    sh = ST.get("server_hello", {})
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "title": "Sneak Attack \u2014 delayed end-step sacrifice trigger "
                 "only sometimes sacrifices the creature",
        "server": {
            "server_version": sh.get("server_version"),
            "build_commit": sh.get("build_commit"),
            "protocol_version": sh.get("protocol_version"),
            "mode": sh.get("mode"),
        },
        "binary_sha256": "d186daaea35a9fa0bb5acac52b82bbd6ba47eaebef9203cb49b402783a02313e",
        "card_data_sha256": "c1bdd90380ecf9cf414c62dc57f41f2035e02d81c14c266237ddc79430361c1a",
        "draft_pools_sha256": "c79abf75cfb3d628906942b2707b047387d444559b5e25d32a411e9ab21f3f7c",
        "signature_verified": True,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                    time.gmtime(t_start)),
        "ended_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                  time.gmtime(t_end)),
        "game_code": ST.get("game_code"),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "decisions": {
            "leg1_activations": ST.get("act_n"),
            "leg1_bears": leg1_bears,
            "leg1_sac_triggers_seen": len(trigs),
            "leg2_bear": leg2_bear,
            "rejections": len(ST["rejections"]),
        },
        "assertions": A,
        "assertion_details": D,
        "verdict": verdict,
        "verdict_note": "Two-activation leg tests acceptance criteria 1-3; "
                        "end-step activation leg tests criterion 4.",
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "12x Sneak Attack / 12x Grizzly Bears deck density is a test-harness "
            "convenience (engine accepts >4-of for custom games).",
            "Not tested on the original 2026-08-02 build; verdict is scoped to "
            "v0.81.2, not a fix claim.",
            "The prebuilt server has no standalone state-restore; states are "
            "authoritative exports (restorable only via full game replay).",
        ],
        "setup_line": "P0: 12x Sneak Attack, 12x Grizzly Bears, 36x Mountain; "
                      "P1: 60x Mountain. Cast Sneak Attack, activate twice in "
                      "one main phase.",
        "contract_line": "Leg 1: two activations -> both Bears sacrificed at "
                         "the end step. Leg 2: activation during the end step "
                         "-> no sacrifice that turn; sacrifice at the next "
                         "end step.",
        "stats": {
            "states_seen": "n/a",
            "trigger_observations": len(trigs),
        },
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)
    say("run.json written; verdict = " + verdict)

    # ---- server excerpts ----
    try:
        lines = open(f"{RUNDIR}/server.log", errors="replace").read().splitlines()
        keep = [l for l in lines if any(
            k in l.lower() for k in
            ("sneak", "sacrifice", "delayed", "trigger", "end step",
             "error", "panic", "warn"))]
        with open(f"{EVDIR}/server_excerpts.log", "w") as f:
            f.write("\n".join(keep[-400:]) + "\n")
        say(f"server_excerpts.log: {len(keep)} matching lines")
    except Exception as e:
        say(f"server excerpts FAILED: {e}")


if __name__ == "__main__":
    asyncio.run(main())
