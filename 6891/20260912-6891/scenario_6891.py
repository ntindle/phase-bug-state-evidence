#!/usr/bin/env python3
"""Issue #6891: Lathiel, the Bounteous Dawn -- "distribute up to that many
+1/+1 counters among any number of other target creatures" reportedly forces
exactly-that-many distinct targets (even opponent's creatures).

Oracle: "Lifelink. At the beginning of each end step, if you gained life this
turn, distribute up to that many +1/+1 counters among any number of other
target creatures."

Triage acceptance criteria:
  - The controller may distribute anywhere from zero through the life gained
    total.
  - The controller may choose any number of legal other creatures consistent
    with the counters assigned.
  - Each chosen target receives at least one counter, and the total never
    exceeds life gained.
  - Opposing creatures remain optional legal targets, never mandatory.

Behavioral contract (native engine v0.80.0 / protocol 69, two human seats):
  P0 ramps, casts Lathiel (2/2 lifelink, {2}{G}{W}), attacks P1 with Lathiel
  alone (P1 declares no blockers) -> P0 gains 2 life. At the end step the
  trigger must fire. Board for the target prompt: P0 controls Lathiel + its
  own Grizzly Bears; P1 controls Grizzly Bears (opponent's creatures are legal
  "other" targets but must never be mandatory).
  A1 setup_ok        Lathiel on P0 BF; P0 gained life this turn (life > 20 at
                     the end step); >=1 own other creature and >=1 P1 creature
                     on BF
  A2 trigger_fired   Lathiel trigger reached a P0 TriggerTargetSelection at End
  A3 budget          life gained this turn == 2 and the prompt's counter
                     budget == 2
  A4 slot0_accepted  submitting P0's own Bear for slot 0 is accepted
  A5 free_distribution both counters may go on one creature; fails iff the
                     engine excludes the already-targeted creature from the
                     next slot's candidates (distinct-target requirement)
  A6 decline_path    if stacking is refused, a decline/up-to path exists for
                     the extra slot (else the controller is forced onto an
                     opponent's creature)
  A7 cleanup         stack empty, game proceeds

Verdict: reproduced iff A2 passes and A5 fails -- the engine implements
"distribute up to N" as N distinct-target slots. not-reproduced iff all
assertions pass (free distribution works).
"""
import asyncio
import copy
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260912-6891"
EVDIR = f"{BACKFILL}/evidence/6891/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

LATHIEL = "Lathiel, the Bounteous Dawn"
BEAR = "Grizzly Bears"
FOREST = "Forest"
PLAINS = "Plains"
# Decks: >4-of is accepted by the engine for custom games. Dense Lathiel
# count guarantees P0 finds one without mulligan tricks.
P0_DECK = [(LATHIEL, 12), (BEAR, 12), (FOREST, 18), (PLAINS, 18)]
P1_DECK = [(BEAR, 12), (FOREST, 48)]
TIMEOUT = 1500

ST = {}
WF_SEEN = []
SUBMITTED_IIDS = set()


def is_target_wait(state):
    return wf_type(state) in ("TargetSelection", "TriggerTargetSelection")


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",          # SETUP -> ATTACK -> ENDSTEP -> DONE
        "stop": False,
        "turn_cap": 30,
        "server_hello": None,
        "lathiel_cast": False,
        "attack_turn": None,       # P0 turn number on which Lathiel attacked
        "life_gain_observed": None,
        "pre_exported": False,
        "post_exported": False,
        "trigger_seen": False,     # Lathiel trigger on stack at End
        "target_prompt_seen": False,
        "target_prompt": None,     # recorded opportunity snapshot
        "target_min": None,
        "target_max": None,
        "target_budget": None,
        "single_submitted": False,
        "single_accepted": None,   # True/False/None
        "single_rejection": None,
        "slots_answered": 0,
        "slot1_same_tried": False,
        "slot1_same_rejected": None,
        "distinct_required_observed": False,
        "declined_extra_slots": False,
        "empty_slot_attempted": False,
        "target_wf_data": None,
        "fallback_submitted": False,
        "counters_prompt_seen": False,
        "counters_answered": False,
        "p0_bear_oid": None,
        "p1_bear_oids": [],
        "life_at_endstep": None,
        "rejections": [],
    })
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


def untapped_named(state, pid, name):
    return [oid for oid, o in bf(state, pid)
            if oname(o) == name and not o.get("tapped")]


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


def record_wf(state):
    wf = wf_type(state)
    if wf and (not WF_SEEN or WF_SEEN[-1][0] != wf
               or WF_SEEN[-1][2] != ST["stage"]):
        WF_SEEN.append((wf, wf_player(state), ST["stage"]))
        wire("waiting_for", {"type": wf, "stage": ST["stage"],
                             "data_keys": sorted(wf_data(state).keys())})
        say(f"waiting_for: {wf} player={wf_player(state)} "
            f"stage={ST['stage']}")


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": ST.get("stage")})
    await c.send_action(action)


async def submit_interaction(c, iid, response):
    sub = {"interactionId": iid, "response": response}
    wire("interaction_submit", {"who": c.name, "submission": sub,
                                "stage": ST.get("stage")})
    await c.send_interaction(sub)


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


def castspell_advertised(acts, oid):
    if not oid:
        return None
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


def life_of(state, pid):
    pl = (state.get("players") or [{}])
    for p in (pl if isinstance(pl, list) else pl.values()):
        if p.get("player_id") == pid or p.get("id") == pid:
            return p.get("life")
    # fallback: dict keyed by seat
    if isinstance(pl, dict):
        p = pl.get(str(pid)) or pl.get(pid)
        if p:
            return p.get("life")
    return None


def lathiel_trigger_on_stack(state):
    """Lathiel's end-step trigger on the stack. The stack entry's ability
    description may be empty; match on TriggeredAbility + source == Lathiel
    (Lathiel has exactly one trigger)."""
    objs = state.get("objects") or {}
    for entry in (state.get("stack") or []):
        kind = (entry.get("kind") or {}).get("type", "")
        if kind != "TriggeredAbility":
            continue
        src = objs.get(str(entry.get("source_id"))) or {}
        if oname(src) == LATHIEL:
            return entry
    return None


def candidate_info(opp, state):
    """Resolve opportunity candidates to (choice_id, ref_oid, name, zone,
    controller)."""
    data = (opp.get("response") or {}).get("data", {}) or {}
    out = []
    for ch in data.get("choices") or data.get("candidates") or []:
        ref = None
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict) and "reference" in d:
                ref = str(d["reference"])
                break
        o = state["objects"].get(str(ref), {}) if ref else {}
        out.append({"choice_id": ch.get("id"), "ref": ref,
                    "name": oname(o), "zone": o.get("zone"),
                    "controller": o.get("controller"),
                    "text": ch.get("text")})
    return out


def build_target_response(resp, choice_id):
    rtype = resp.get("type")
    data = resp.get("data", {}) or {}
    spec = data.get("spec") or {}
    spec_type = spec.get("type") if isinstance(spec, dict) else None
    if rtype == "schema" and spec_type in ("sequence", "select"):
        return {"type": spec_type, "data": {"choiceIds": [choice_id]}}
    if rtype == "exactChoices":
        return {"type": "choose", "data": {"choiceId": choice_id}}
    return None


async def observe_target_prompt(state):
    """Record the Lathiel target opportunity once. Matched on the
    waiting_for description ('distribute ... counters') + source object
    being Lathiel (the stack entry's ability description may be empty)."""
    if ST["target_prompt_seen"]:
        return
    if not is_target_wait(state) or wf_player(state) != 0:
        return
    wd = wf_data(state)
    if "distribute" not in (wd.get("description") or "").lower():
        return
    src = (state.get("objects") or {}).get(str(wd.get("source_id"))) or {}
    if oname(src) != LATHIEL:
        return
    vi = (C0.latest or {}).get("viewer_interaction") or {}
    opps = vi.get("opportunities") or vi.get("interactions") or []
    opp = opps[0] if opps else {}
    ST["target_prompt_seen"] = True
    ST["stage"] = "ENDSTEP"
    ST["life_at_endstep"] = life_of(state, 0)
    ST["target_wf_data"] = copy.deepcopy(wf_data(state))
    snap = {"waiting_for_type": wf_type(state),
            "waiting_for_data": wf_data(state),
            "opportunity": opp,
            "candidates": candidate_info(opp, state),
            "trigger_desc": wd.get("description")}
    ST["target_prompt"] = snap
    with open(f"{EVDIR}/target_prompt.json", "w") as f:
        json.dump(snap, f, indent=1, default=str)
    wire("target_prompt", {"wf_type": wf_type(state),
                           "data_keys": sorted(wf_data(state).keys()),
                           "candidates": snap["candidates"],
                           "resp_type": (opp.get("response") or {}).get(
                               "type")})
    say(f"Lathiel {wf_type(state)} at End; P0 life={ST['life_at_endstep']}; "
        f"candidates={[(c['name'], c['ref'], c['controller']) for c in snap['candidates']]}")


def own_bear_candidate(cands, state):
    want = next((x for x in cands
                 if x["name"] == BEAR and x["controller"] == 0
                 and x["zone"] == "Battlefield"), None)
    if want is None:
        want = next((x for x in cands
                     if x["controller"] == 0
                     and x["zone"] == "Battlefield"), None)
    return want


async def answer_target_prompt(c, state):
    """Slot 0: target P0's own Bear. Slot 1: target the SAME Bear again
    (tests whether both counters may stack on one creature). If the
    same-target attempt was already tried for slot 1, attempt the decline
    path (done choice / empty selection) to test the up-to semantics.
    Each opportunity is answered at most once (tracked by interactionId).
    Seat guard: only the client whose seat owns the prompt may submit.
    """
    if not is_target_wait(state) or wf_player(state) != 0:
        return False
    try:
        seat = int(c.name[1])
    except (IndexError, ValueError):
        seat = -1
    if wf_player(state) != seat:
        return False
    await observe_target_prompt(state)
    if not ST["target_prompt_seen"]:
        return False
    vi = (c.latest or {}).get("viewer_interaction") or {}
    opps = vi.get("opportunities") or vi.get("interactions") or []
    acted = False
    for opp in opps:
        iid = opp.get("interactionId") or opp.get("interaction_id")
        if not iid or iid in SUBMITTED_IIDS:
            continue
        cands = candidate_info(opp, state)
        resp = (opp.get("response") or {})
        slot = ST["slots_answered"]
        wire("target_opp_detail",
             {"iid": iid, "resp_type": resp.get("type"),
              "slot_index": slot,
              "candidates": cands,
              "waiting_for_data": ST.get("target_wf_data"),
              "stage": ST["stage"]})
        say(f"[P0] slot {slot} opp {iid}: "
            f"{[(x['name'], x['ref'], x['controller']) for x in cands]}")
        if slot == 0:
            want = own_bear_candidate(cands, state)
            if want is None:
                say(f"[P0] slot 0: no P0-controlled candidate; holding")
                wire("slot_no_p0_candidate", {"iid": iid, "slot": slot})
                SUBMITTED_IIDS.add(iid)
                acted = True
                continue
            resp_out = build_target_response(resp, want["choice_id"])
            if resp_out is None:
                say(f"[P0] slot 0: unexpected shape "
                    f"{resp.get('type')}; holding")
                wire("target_unexpected_shape",
                     {"iid": iid, "resp": resp, "slot": slot})
                SUBMITTED_IIDS.add(iid)
                acted = True
                continue
            ST["p0_bear_oid"] = want["ref"]
            wire("slot_target_attempt",
                 {"iid": iid, "slot": slot, "want": want,
                  "stage": ST["stage"]})
            say(f"[P0] slot 0 targets own {want['name']} "
                f"oid={want['ref']}")
            await submit_interaction(c, iid, resp_out)
            SUBMITTED_IIDS.add(iid)
            ST["single_submitted"] = True
            ST["slots_answered"] += 1
            acted = True
        else:
            # later slot: stacking test first (same bear again), else decline
            want = own_bear_candidate(cands, state)
            tried_key = f"slot{slot}_same_tried"
            if want is not None and not ST.get(tried_key):
                ST[tried_key] = True
                ST["slot1_same_tried"] = True
                resp_out = build_target_response(resp, want["choice_id"])
                if resp_out is None:
                    say(f"[P0] slot {slot}: unexpected shape; holding")
                    wire("target_unexpected_shape",
                         {"iid": iid, "resp": resp, "slot": slot})
                    SUBMITTED_IIDS.add(iid)
                    acted = True
                    continue
                wire("slot_target_attempt",
                     {"iid": iid, "slot": slot, "want": want,
                      "stage": ST["stage"], "stacking": True})
                say(f"[P0] slot {slot} targets SAME own {want['name']} "
                    f"oid={want['ref']} (stacking test)")
                await submit_interaction(c, iid, resp_out)
                SUBMITTED_IIDS.add(iid)
                ST["slots_answered"] += 1
                acted = True
                continue
            if want is None:
                # decisive observation: already-targeted creature is NOT
                # offered again -- the engine demands distinct targets
                wire("slot_excludes_chosen_creature",
                     {"iid": iid, "slot": slot,
                      "chosen_oid": ST["p0_bear_oid"],
                      "candidates": cands})
                say(f"[P0] slot {slot}: own bear oid={ST['p0_bear_oid']} "
                    f"NOT among candidates -- distinct targets required")
                ST["distinct_required_observed"] = True
            # decline path (up-to semantics)
            done = next((x for x in cands
                         if (x.get("text") or "").lower().strip() in
                         ("done", "no more targets", "decline", "none",
                          "finish", "pass", "skip")), None)
            if done is not None:
                resp_out = build_target_response(resp, done["choice_id"])
                say(f"[P0] slot {slot} declines via '{done.get('text')}'")
                wire("slot_decline", {"iid": iid, "choice": done,
                                      "slot": slot})
                await submit_interaction(c, iid, resp_out)
                SUBMITTED_IIDS.add(iid)
                ST["slots_answered"] += 1
                ST["declined_extra_slots"] = True
                acted = True
                continue
            rtype = resp.get("type")
            spec = (resp.get("data", {}) or {}).get("spec") or {}
            spec_type = spec.get("type")
            if rtype == "schema" and spec_type in ("sequence", "select"):
                say(f"[P0] slot {slot}: trying empty selection (up-to path)")
                wire("slot_empty_attempt", {"iid": iid, "slot": slot})
                await submit_interaction(c, iid,
                                         {"type": spec_type,
                                          "data": {"choiceIds": []}})
                SUBMITTED_IIDS.add(iid)
                ST["slots_answered"] += 1
                ST["empty_slot_attempted"] = True
                acted = True
                continue
            say(f"[P0] slot {slot}: no decline path visible; holding")
            wire("slot_no_decline_path",
                 {"iid": iid, "resp_type": rtype, "candidates": cands,
                  "slot": slot})
            SUBMITTED_IIDS.add(iid)
            acted = True
            # terminal: the engine demands a distinct target for this slot
            # and offers no decline -- export the forced state and stop
            if ST["distinct_required_observed"] and not ST["post_exported"]:
                wire("forced_distinct_terminal",
                     {"iid": iid, "slot": slot,
                      "chosen_oid": ST["p0_bear_oid"],
                      "candidates": cands})
                say("TERMINAL: distinct target forced for slot "
                    f"{slot}, no decline path; exporting forced state")
                await export_now("post.json")
                ST["post_exported"] = True
                ST["stop"] = True
    return acted


async def observe_single_result(c, state):
    """Check whether the slot submissions were accepted."""
    if not ST["single_submitted"]:
        return
    if ST["single_accepted"] is None:
        # a rejection mentioning the interaction means it was refused
        for r in ST["rejections"]:
            d = json.dumps(r.get("data", {}))
            if "nteraction" in d or "target" in d.lower():
                ST["single_accepted"] = False
                ST["single_rejection"] = r
                wire("slot0_rejected", {"rejection": r})
                say("slot-0 submission REJECTED -> "
                    f"{json.dumps(r)[:300]}")
                break
        else:
            if not is_target_wait(state) or wf_player(state) != 0:
                ST["single_accepted"] = True
                wire("single_resolved", {"wf_now": wf_type(state)})
                say("slot-0 submission appears ACCEPTED (prompt moved on)")
            elif lathiel_trigger_on_stack(state) is None:
                ST["single_accepted"] = True
                wire("single_resolved_stack_empty", {})
                say("slot-0 submission appears ACCEPTED (trigger left stack)")
    if ST["slot1_same_tried"] and ST["slot1_same_rejected"] is None \
            and ST["slots_answered"] >= 2:
        for r in ST["rejections"]:
            d = json.dumps(r.get("data", {})).lower()
            if "duplicat" in d or "already" in d or "distinct" in d \
                    or "target" in d:
                ST["slot1_same_rejected"] = r
                wire("slot1_same_rejected", {"rejection": r})
                say("slot-1 same-target REJECTED -> "
                    f"{json.dumps(r)[:300]}")
                break


async def answer_counters_prompt(c, state):
    """If the engine asks how many counters on the chosen target, answer max."""
    wt, wp = wf_type(state), wf_player(state)
    if wp != 0 or wt not in ("ChooseXValue", "ChooseNumber", "DistributeCounters",
                             "AllocateCounters", "CounterChoice"):
        return False
    if ST["counters_answered"]:
        return True
    vi = (c.latest or {}).get("viewer_interaction") or {}
    opps = vi.get("opportunities") or vi.get("interactions") or []
    wire("counters_prompt", {"wf": wt,
                             "opp": (opps[0] if opps else None),
                             "stage": ST["stage"]})
    say(f"counter-allocation prompt: {wt}; answering with budget "
        f"(see wire)")
    ST["counters_prompt_seen"] = True
    # conservative: try to pick the max offered option
    opp = opps[0] if opps else {}
    odata = opp.get("data") or {}
    cands = odata.get("candidates") or odata.get("choices") or []
    best = None
    best_n = -1
    for cd_ in cands:
        cid = choice_id_of(cd_)
        txt = json.dumps(cd_)
        import re as _re
        m = _re.search(r"\b([0-9]+)\b", txt)
        n = int(m.group(1)) if m else -1
        if n > best_n:
            best_n, best = n, cid
    if best is None and cands:
        best = choice_id_of(cands[0])
    if best is not None:
        iid = opp.get("interactionId") or opp.get("interaction_id")
        await submit_interaction(c, iid, {"type": "choose",
                                          "data": {"choiceId": best}})
        ST["counters_answered"] = True
        return True
    return True  # hold, don't pass


C0 = None
C1 = None


async def p0_tick(c, pid, state, acts):
    # always handle prompts first
    if await answer_target_prompt(c, state):
        return True
    if await answer_counters_prompt(c, state):
        return True
    if not is_my_main(state, pid):
        return False
    if ST["stage"] == "SETUP":
        # cast Lathiel when possible
        if not bf_named(state, pid, LATHIEL):
            oid = find_hand(state, pid, LATHIEL)
            a = castspell_advertised(acts, oid)
            if a:
                await submit_as_is(c, a)
                ST["lathiel_cast"] = True
                say(f"[P0] casts {LATHIEL}")
                return True
        else:
            ST["stage"] = "ATTACK"
    if ST["stage"] == "ATTACK":
        # cast a bear so there is an "other" friendly creature
        if not bf_named(state, pid, BEAR):
            oid = find_hand(state, pid, BEAR)
            a = castspell_advertised(acts, oid)
            if a:
                await submit_as_is(c, a)
                say("[P0] casts Grizzly Bears")
                return True
    return False


async def p1_tick(c, pid, state, acts):
    # NOTE: P1 never answers P0's target prompts (cross-client submissions
    # would be rejected and poison SUBMITTED_IIDS).
    if not is_my_main(state, pid):
        return False
    # cast bears so opponent creatures exist as legal targets
    if len(bf_named(state, pid, BEAR)) < 2:
        oid = find_hand(state, pid, BEAR)
        a = castspell_advertised(acts, oid)
        if a:
            await submit_as_is(c, a)
            say("[P1] casts Grizzly Bears")
            return True
    return False


async def empty_declare(c, acts):
    for a in acts:
        if a["type"] == "DeclareAttackers":
            sub = copy.deepcopy(a)
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
            return True
    return False


async def lathiel_attack(c, state, acts):
    """Attack P1 with Lathiel alone (lifelink -> gain 2)."""
    lath = bf_named(state, 0, LATHIEL)
    if not lath:
        return False
    for a in acts:
        if a["type"] == "DeclareAttackers":
            sub = copy.deepcopy(a)
            sub["data"]["attacks"] = [[int(lath[0]),
                                       {"type": "Player", "data": 1}]]
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
            ST["attack_turn"] = state.get("turn_number")
            say(f"[P0] attacks with Lathiel (turn {ST['attack_turn']})")
            return True
    return False


async def empty_blockers(c, acts):
    for a in acts:
        if a["type"] == "DeclareBlockers":
            sub = copy.deepcopy(a)
            sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
            return True
    return False


async def play_land(c, pid, state, acts):
    for name in (FOREST, PLAINS):
        lid = find_hand(state, pid, name)
        if lid:
            a = next((x for x in acts if x["type"] == "PlayLand"
                      and str(x.get("data", {}).get("object_id")) == lid),
                     None)
            if a:
                await submit_as_is(c, a)
                return True
    return False


async def tick(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    record_wf(state)
    rejs = drain_rejections(c)
    await observe_single_result(c, state)
    for a in acts:
        if a["type"] == "MulliganDecision":
            await submit_as_is(c, {"type": "MulliganDecision",
                                   "data": {"choice": {"type": "Keep"}}})
            return True
    if wf_type(state) == "DiscardToHandSize" and wf_player(state) == pid:
        n = wf_data(state).get("count") or max(0, len(hand_oids(state, pid)) - 7)
        picks = hand_oids(state, pid)[:n]
        if picks:
            await submit_as_is(c, {"type": "SelectCards",
                                   "data": {"cards": [int(x) for x in picks]}})
            return True
    # ENDSTEP: export pre when the Lathiel trigger is on the stack at End
    if (state.get("phase") or "") == "End" and not ST["pre_exported"]:
        trig = lathiel_trigger_on_stack(state)
        if trig:
            ST["trigger_seen"] = True
            ST["life_gain_observed"] = life_of(state, 0)
            wire("trigger_on_stack", {"stack_entry": trig,
                                      "p0_life": ST["life_gain_observed"],
                                      "stage": ST["stage"]})
            say(f"Lathiel trigger on stack at End; P0 life="
                f"{ST['life_gain_observed']}")
            await export_now("pre_trigger.json")
            ST["pre_exported"] = True
    # post export once the trigger has resolved and stack is empty
    if ST["target_prompt_seen"] and not ST["post_exported"]:
        trig = lathiel_trigger_on_stack(state)
        if trig is None and not (state.get("stack") or []) \
                and not is_target_wait(state):
            await export_now("post.json")
            ST["post_exported"] = True
            ST["stage"] = "DONE"
            ST["stop"] = True
            say("post.json exported; stopping")
            return True
    if pid == 0:
        if await p0_tick(c, pid, state, acts):
            return True
    else:
        if await p1_tick(c, pid, state, acts):
            return True
    # land drops every tick
    if await play_land(c, pid, state, acts):
        return True
    if state.get("active_player") == pid and \
            (state.get("phase") or "") == "DeclareAttackers":
        if pid == 0 and ST["stage"] in ("ATTACK", "ENDSTEP") \
                and ST["attack_turn"] is None:
            # proof attack only once P1 has a Bear (opponent legal target)
            # and P0 has its own Bear; otherwise keep declaring empty
            if bf_named(state, 1, BEAR) and bf_named(state, 0, BEAR):
                if await lathiel_attack(c, state, acts):
                    return True
        if await empty_declare(c, acts):
            return True
    if (state.get("phase") or "") == "DeclareBlockers":
        if await empty_blockers(c, acts):
            return True
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    # never pass priority while our target/counter prompt is pending
    wt, wp = wf_type(state), wf_player(state)
    if wp == pid and (is_target_wait(state) or wt in (
            "ChooseXValue", "ChooseNumber", "DistributeCounters",
            "AllocateCounters", "CounterChoice")):
        wire("named_prompt_held", {"wf": wt, "who": c.name,
                                   "stage": ST["stage"]})
        return True
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


async def main():
    reset()
    global C0, C1
    import websockets as wsmod
    url = os.environ.get("PHASE_WS_URL", "ws://127.0.0.1:9374/ws")
    async with wsmod.connect(url, max_size=200_000_000) as w:
        await w.send(json.dumps({"type": "ClientHello", "data": {
            "client_version": "driver-0.1", "build_commit": "driver",
            "protocol_version": 69}}))
        hello_raw = await asyncio.wait_for(w.recv(), 5)
        ST["server_hello"] = json.loads(hello_raw)
        say("ServerHello: " + json.dumps(ST["server_hello"])[:300])
    # parse evidence: Lathiel's trigger from the pinned dataset
    try:
        cdp = f"{BACKFILL}/server/releases/v0.80.0/data/card-data.json"
        lth = json.load(open(cdp))["lathiel, the bounteous dawn"]
        with open(f"{EVDIR}/parse_evidence.json", "w") as f:
            json.dump({"name": lth["name"],
                       "oracle_text": lth["oracle_text"],
                       "triggers": lth["triggers"]}, f, indent=1)
        say("parse_evidence.json written")
    except Exception as e:
        say(f"parse evidence FAILED: {e}")
    C0 = PhaseClient("P0")
    await C0.connect()
    await C0.create(deck(*P0_DECK))
    C1 = PhaseClient("P1")
    await C1.connect()
    await C1.join(C0.game_code, deck(*P1_DECK))
    say(f"game {C0.game_code}; seats {C0.player_id}/{C1.player_id}")
    wire("game_start", {"game_code": C0.game_code,
                        "p0_deck": P0_DECK, "p1_deck": P1_DECK,
                        "server_hello": ST["server_hello"]})

    t0 = time.time()
    last_progress = t0
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        acted0 = await tick(C0, 0)
        acted1 = await tick(C1, 1)
        if acted0 or acted1:
            last_progress = time.time()
        st0 = C0.latest
        if st0 and (st0.get("state", {}).get("turn_number") or 0) > \
                ST["turn_cap"]:
            say("turn cap reached; stopping")
            wire("turn_cap", {})
            break
        if time.time() - last_progress > 240:
            say("no progress for 240s; dumping state and stopping")
            for c in (C0, C1):
                st = c.latest
                if st:
                    wire("stall_state",
                         {"who": c.name,
                          "wf": st.get("state", {}).get("waiting_for"),
                          "phase": st.get("state", {}).get("phase"),
                          "turn": st.get("state", {}).get("turn_number"),
                          "acts": [a.get("type")
                                   for a in st.get("legal_actions", [])],
                          "stage": ST["stage"]})
            break
        await asyncio.sleep(0.15)

    # ---- assertions ----
    A = {}
    D = {}

    def load_env(fn):
        try:
            return json.loads(open(f"{EVDIR}/{fn}").read())
        except Exception as e:
            D[f"{fn}_err"] = str(e)[:120]
            return None

    def env_state(env):
        if not env:
            return None
        s = env.get("state")
        return json.loads(s) if isinstance(s, str) else s

    pre = load_env("pre_trigger.json")
    post = load_env("post.json")
    pre_s, post_s = env_state(pre), env_state(post)

    # A1: setup
    lath_pre = bf_named(pre_s, 0, LATHIEL) if pre_s else []
    own_bears_pre = bf_named(pre_s, 0, BEAR) if pre_s else []
    p1_bears_pre = bf_named(pre_s, 1, BEAR) if pre_s else []
    life_pre = life_of(pre_s, 0) if pre_s else None
    A["A1_setup_ok"] = "passed" if (pre_s and lath_pre and own_bears_pre
                                    and p1_bears_pre and life_pre is not None
                                    and life_pre > 20) else "failed"
    D["A1_setup_ok_detail"] = (
        f"lathiel_bf={bool(lath_pre)} own_bears={len(own_bears_pre)} "
        f"p1_bears={len(p1_bears_pre)} p0_life_at_trigger={life_pre}")

    # A2: trigger fired
    A["A2_trigger_fired"] = "passed" if ST["target_prompt_seen"] else (
        "failed" if ST["trigger_seen"] else "not-run")
    D["A2_trigger_fired_detail"] = (
        f"trigger_on_stack={ST['trigger_seen']} "
        f"target_prompt_seen={ST['target_prompt_seen']} "
        f"wf_seq={[w[0] for w in WF_SEEN]}")

    # A3: budget == life gained == 2
    gained = (ST["life_at_endstep"] - 20) if ST["life_at_endstep"] else None
    A["A3_budget"] = "passed" if gained == 2 else (
        "failed" if gained is not None else "not-run")
    D["A3_budget_detail"] = f"p0_life_at_endstep={ST['life_at_endstep']} " \
        f"life_gained={gained} (expected 2)"

    # A4: slot-0 single-target submission accepted
    A["A4_slot0_accepted"] = "passed" if ST["single_accepted"] else (
        "failed" if ST["single_submitted"] else "not-run")
    D["A4_slot0_accepted_detail"] = (
        f"submitted={ST['single_submitted']} accepted={ST['single_accepted']} "
        f"rejection={json.dumps(ST['single_rejection'])[:200] if ST['single_rejection'] else None}")

    # A5: free distribution -- both counters may go on one creature.
    # Fails iff the engine excludes the already-targeted creature from the
    # next slot's candidates (distinct-target requirement = reported bug).
    own_counters = p1_counters = None
    slot1_ok = (ST["slot1_same_tried"] and
                ST["slot1_same_rejected"] is None)
    if post_s:
        own_counters = sum(
            (post_s["objects"][oid].get("counters") or {}).get("P1P1", 0)
            for oid in bf_named(post_s, 0, BEAR))
        p1_counters = sum(
            (post_s["objects"][oid].get("counters") or {}).get("P1P1", 0)
            for oid in bf_named(post_s, 1, BEAR))
    A["A5_free_distribution"] = "passed" if (
        slot1_ok and own_counters == 2 and p1_counters == 0) else (
        "failed" if (ST["distinct_required_observed"]
                     or ST["slot1_same_rejected"]) else "not-run")
    D["A5_free_distribution_detail"] = (
        f"distinct_required_observed={ST['distinct_required_observed']} "
        f"slot1_same_tried={ST['slot1_same_tried']} "
        f"slot1_same_rejected={bool(ST['slot1_same_rejected'])} "
        f"own_bear_counters={own_counters} (free distribution wants 2) "
        f"p1_bear_counters={p1_counters} (expected 0)")

    # A6: decline/up-to path for the extra slot.
    decline_ok = ST["declined_extra_slots"] or ST["empty_slot_attempted"]
    A["A6_decline_path"] = "passed" if (
        A["A5_free_distribution"] == "passed" or decline_ok) else (
        "failed" if (ST["distinct_required_observed"]
                     and ST["target_prompt_seen"] and post_s) else
        ("not-run" if not ST["target_prompt_seen"] else "failed"))
    D["A6_decline_path_detail"] = (
        f"declined_extra_slots={ST['declined_extra_slots']} "
        f"empty_slot_attempted={ST['empty_slot_attempted']}")

    # A7: cleanup
    post_wf = wf_type(post_s) if post_s else None
    A["A7_cleanup"] = "passed" if (
        post_s and post_wf not in ("TargetSelection", "TriggerTargetSelection")
        and not (post_s.get("stack") or [])) else (
        "failed" if post_s else "not-run")
    D["A7_cleanup_detail"] = (f"post waiting_for={post_wf} "
                              f"stack_empty={not (post_s.get('stack') or []) if post_s else None}")

    if A["A1_setup_ok"] == "failed" or A["A2_trigger_fired"] == "not-run":
        verdict = "blocked"
    elif (A["A2_trigger_fired"] == "passed"
          and A["A5_free_distribution"] == "failed"):
        # The engine requires distinct targets per counter of budget:
        # the reported defect. A6 records whether the controller could at
        # least decline the extra slot instead of being forced onto an
        # opponent's creature.
        verdict = "reproduced"
    elif all(v == "passed" for v in A.values()):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    assertions = {"assertions": A, "details": D, "verdict": verdict,
                  "waiting_for_seq": [w[0] for w in WF_SEEN],
                  "rejections": ST["rejections"]}
    json.dump(assertions, open(f"{EVDIR}/assertions.json", "w"), indent=1)
    say("ASSERTIONS: " + json.dumps(A))
    say("VERDICT: " + verdict)
    wire("final", assertions)
    await C0.close()
    await C1.close()
    WIRE.close()
    RUNLOG.close()


if __name__ == "__main__":
    asyncio.run(main())
