#!/usr/bin/env python3
"""Issue #6860: Dargo, the Shipwrecker - cost-reduction discounts not applied.

Reported (Discord): "I can only cast him for his original cost, he has 2
colorless discount for every creature/artifact sac that turn and you can sac
creatures and artifact as an additional cost while casting him; neither the
first nor the second discount can be activated."

Oracle: "As an additional cost to cast this spell, you may sacrifice any
number of artifacts and/or creatures. This spell costs {2} less to cast for
each permanent sacrificed this way and {2} less to cast for each other
artifact or creature you've sacrificed this turn."
Pinned v0.79.0 card data prices Dargo at {6}{R} (generic 6 + Red shard).

Plan (single P0 turn, two human seats):
  SETUP  - 15+ Mountains, Goblin Bombardment, 4+ Memnites on BF,
           2+ Dargos in hand. Export pre.json.
           (15 untapped: the engine charges the buggy full 7 for LEG1, so 7+
           must remain for LEG2's CastSpell to be advertised.)
  LEG1   - Activate Bombardment, sacrifice Memnite#1 (prior sac this turn),
           damage to P1. Cast Dargo#1 DECLINING the additional cost.
           Expected discount: {2} (one prior sacrifice) -> mana spent 5.
  LEG2   - Cast Dargo#2, PAYING the additional cost with Memnite#2,#3.
           Expected discount: {2} (prior) + {4} (two additional) = {6}
           -> mana spent 1. Answer the legend rule (keep first).
Mana spent is measured as the delta of untapped Mountains (engine auto-taps).

Behavioral contract:
  A1 setup_ok            pre.json: 2+ Dargo in hand, Bombardment + 4 Memnites
                         on BF, >=15 untapped Mountains, empty pool
  A2 additional_offered  the Dargo announcement offered the optional
                         any-number artifact/creature sacrifice choice
  A3 prior_discount      LEG1 (1 prior sac, 0 additional): mana spent == 5
  A4 additional_discount LEG2 (1 prior + 2 additional): mana spent == 1
  A5 cleanup             game proceeds; legend rule answered; no stuck prompt

Verdict = reproduced iff A3 or A4 fails (discount not applied as Oracle
requires). A2 failing means the additional cost is never offered.
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
RUN_ID = "20260911-6860g"
EVID_ISSUE = "6860"
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


DARGO = "Dargo, the Shipwrecker"
BOMBARD = "Goblin Bombardment"
MEMNITE = "Memnite"
MOUNTAIN = "Mountain"

ST = {"stage": "SETUP", "step": 0, "stop": False}
CAST = {}          # active cast tracking
MULLS = {"P0": 0}
SHAPES = set()
WF_SEEN = []
SUBMITTED = set()  # interactionIds answered
LEG = {}           # leg measurements: untapped_before/after, etc.


def new_cast(tag, plan):
    CAST.clear()
    CAST.update({"tag": tag, "plan": plan, "in_flight": True,
                 "announced": False, "paid": False, "done": False,
                 "sac_chosen": False, "sac_done": False, "sac_oids": []})


# ------------------------------------------------------------------ helpers

def oname(o):
    return o.get("card_name") or o.get("name") or ""


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


def untapped_land(state, pid, name):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == name and not o.get("tapped"))


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain"))


def dargo_on_bf(state, pid):
    return [oid for oid, o in bf(state, pid) if oname(o) == DARGO]


def dargo_on_stack(state):
    return [oid for oid, o in state["objects"].items()
            if oname(o) == DARGO and o.get("zone") == "Stack"]


def bombard_on_stack(state):
    return [oid for oid, o in state["objects"].items()
            if o.get("zone") == "Stack" and "Bombardment" in oname(o)]


def pool_len(state, pid):
    p = state["players"][pid]
    return len(list((p.get("mana_pool") or {}).get("mana") or []))


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict) and d.get("text"):
                t = d["text"]
                break
    return str(t)


def action_codes(ch):
    return [((s.get("data") or {}).get("code") or "")
            for s in ch.get("surfaces", []) or [] if s.get("type") == "action"]


def value_flags(ch):
    return [((s.get("data") or {}).get("role"), (s.get("data") or {}).get("value"))
            for s in ch.get("surfaces", []) or [] if s.get("type") == "value"]


def choice_obj(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if s.get("type") == "object" and isinstance(d, dict):
            return d
    return {}


def record_wf(state):
    wf = (state.get("waiting_for") or {}).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf,
                             "data": (state.get("waiting_for") or {}).get("data"),
                             "stage": ST["stage"], "step": ST["step"]})


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action,
                           "stage": ST["stage"], "step": ST["step"]})
    await c.send_action(action)


async def send_interaction(c, sub):
    iid = (sub or {}).get("interactionId")
    if iid:
        LAST_SUBMIT["iid"] = iid
    wire("interaction_submit", {"who": c.name, "submission": sub,
                                "stage": ST["stage"], "step": ST["step"]})
    await c.send_interaction(sub)


LAST_SUBMIT = {"iid": None}
REJECTIONS = []


def resp_for(rtype, spec_type, ids):
    """Build the correct response envelope for an opportunity.

    exactChoices -> {"type":"choose","data":{"choiceId":...}} (singular);
    schema with spec sequence/select -> the spec variant with choiceIds.
    """
    if rtype == "schema" and spec_type in ("sequence", "select"):
        return {"type": spec_type, "data": {"choiceIds": list(ids)}}
    if rtype == "exactChoices":
        return {"type": "choose", "data": {"choiceId": ids[0] if ids else None}}
    return {"type": rtype, "data": {"choiceIds": list(ids)}}


def drain_rejections(c):
    """Pull ActionRejected/Error messages from the client inbox."""
    found = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            found.append({"type": t, "data": data})
            wire("rejected", {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {json.dumps(data)[:200]}")
    return found


async def export_now(path):
    s = await C0.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return s


# ------------------------------------------------------- interaction driver

async def scan_interactions(c, st):
    """Answer P0's pending decisions. Returns True if it acted."""
    vi = get_vi(st)
    if not vi:
        return False
    state = st.get("state") or {}
    wf = (state.get("waiting_for") or {})
    wtype = wf.get("type")
    wdata = wf.get("data") or {}
    if wdata.get("player") != 0:
        return False
    for opp in vi.get("opportunities", []) or []:
        resp = opp.get("response", {}) or {}
        rtype = resp.get("type")
        data = resp.get("data", {}) or {}
        iid = opp.get("interactionId")
        if iid in SUBMITTED:
            continue
        chs = data.get("choices") or data.get("candidates") or []
        spec = data.get("spec") or {}
        spec_type = spec.get("type") if isinstance(spec, dict) else None
        key = (rtype, spec_type, wtype, len(chs))
        if key not in SHAPES:
            SHAPES.add(key)
            wire("interaction_shape", {"rtype": rtype, "spec": spec_type,
                                      "wf": wtype, "n": len(chs),
                                      "wdata": wdata, "stage": ST["stage"],
                                      "interaction": opp})
            say(f"[P0] new interaction shape rtype={rtype} spec={spec_type} "
                f"wf={wtype} n={len(chs)}")
        # --- Dargo optional additional cost: pay or decline ---
        if wtype == "OptionalCostChoice" and CAST.get("in_flight"):
            cost = wdata.get("cost") or {}
            wire("optional_cost", {"iid": iid, "cost": cost,
                                  "tag": CAST.get("tag")})
            say(f"[P0] OptionalCostChoice during {CAST.get('tag')}: "
                f"{json.dumps(cost)[:200]}")
            want_pay = CAST["plan"].get("pay_additional", False)
            for ch in chs:
                codes = action_codes(ch)
                flags = value_flags(ch)
                if "decideOptionalCost" not in codes:
                    continue
                if want_pay and ("pay", "true") in flags:
                    await send_interaction(
                        c, {"interactionId": iid,
                            "response": {"type": "choose",
                                        "data": {"choiceId": ch["id"]}}})
                    SUBMITTED.add(iid)
                    CAST["paid"] = True
                    say(f"[P0] {CAST['tag']}: additional cost -> PAY")
                    return True
                if not want_pay and ("pay", "false") in flags:
                    await send_interaction(
                        c, {"interactionId": iid,
                            "response": {"type": "choose",
                                        "data": {"choiceId": ch["id"]}}})
                    SUBMITTED.add(iid)
                    say(f"[P0] {CAST['tag']}: additional cost -> DECLINE")
                    return True
            say("[P0] pay/decline choice not identified; deferring")
            return False
        # --- additional-cost count: ChooseXValue number -> how many to sac ---
        if (wtype == "ChooseXValue" and CAST.get("in_flight")
                and CAST.get("paid") and not CAST.get("sac_chosen")):
            if rtype == "schema" and spec_type == "number":
                want_n = len(CAST["plan"].get("sac_oids") or [])
                await send_interaction(
                    c, {"interactionId": iid,
                        "response": {"type": "number",
                                    "data": {"value": want_n}}})
                SUBMITTED.add(iid)
                CAST["sac_chosen"] = True
                say(f"[P0] {CAST['tag']}: sacrifice count -> {want_n}")
                return True
            say("[P0] ChooseXValue not a number schema; deferring")
            return False
        # --- permanent selection for the additional cost (sacrifice N) ---
        if CAST.get("in_flight") and CAST.get("paid") and not CAST.get("sac_done"):
            want = CAST["plan"].get("sac_oids") or []
            if rtype in ("sequence", "select") or spec_type in ("sequence", "select"):
                # schema-style candidate selection
                picked = []
                for ch in chs:
                    obj = choice_obj(ch)
                    ref = str(obj.get("reference") or "")
                    if ref and ref in want and ch.get("id") not in picked:
                        picked.append(ch["id"])
                if picked or not want:
                    await send_interaction(
                        c, {"interactionId": iid,
                            "response": resp_for(rtype, spec_type, picked)})
                    SUBMITTED.add(iid)
                    CAST["sac_done"] = True
                    CAST["sac_oids"] = picked
                    say(f"[P0] {CAST['tag']}: sacrificed {len(picked)} permanents")
                    return True
                say("[P0] wanted permanents not among candidates; deferring")
                return False
        # --- Bombardment activation: target P1, sacrifice a Memnite ---
        if ST.get("bombard_active"):
            # target selection: pick player 1 (seat 1)
            if wtype == "TargetSelection" or spec_type == "sequence":
                for ch in chs:
                    for s in ch.get("surfaces", []) or []:
                        d = s.get("data") or {}
                        if isinstance(d, dict) and d.get("seat") == 1:
                            await send_interaction(
                                c, {"interactionId": iid,
                                    "response": resp_for(rtype, spec_type,
                                                         [ch["id"]])})
                            SUBMITTED.add(iid)
                            say("[P0] Bombardment targets P1")
                            return True
                # boolean / single-candidate fallback
                if len(chs) == 1:
                    await send_interaction(
                        c, {"interactionId": iid,
                            "response": resp_for(rtype, spec_type,
                                                 [chs[0]["id"]])})
                    SUBMITTED.add(iid)
                    say("[P0] Bombardment: single candidate chosen")
                    return True
            # sacrifice-cost selection: pick the planned Memnite
            want = ST.get("bombard_sac_oid")
            if want and (rtype in ("sequence", "select")
                         or spec_type in ("sequence", "select")):
                for ch in chs:
                    obj = choice_obj(ch)
                    if str(obj.get("reference") or "") == str(want):
                        await send_interaction(
                            c, {"interactionId": iid,
                                "response": resp_for(rtype, spec_type,
                                                     [ch["id"]])})
                        SUBMITTED.add(iid)
                        ST["bombard_sac_paid"] = True
                        say("[P0] Bombardment sacrifices Memnite")
                        return True
    return False


# ------------------------------------------------------------------ tick

async def tick(c, pid, is_p0):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    # mulligan
    for a in acts:
        if a["type"] == "MulliganDecision":
            if is_p0:
                has_dargo = any(oname(state["objects"][o]) == DARGO
                                for o in hand_oids(state, pid))
                if has_dargo or MULLS["P0"] >= 2:
                    choice = "Keep"
                else:
                    choice = "Mulligan"
                    MULLS["P0"] += 1
            else:
                choice = "Keep"
            await submit_as_is(c, {"type": "MulliganDecision",
                                  "data": {"choice": {"type": choice}}})
            say(f"{c.name} mulligan -> {choice}")
            return True
    # BottomCards
    for a in acts:
        if a["type"] == "SelectCards" and \
                (state.get("waiting_for") or {}).get("type") == "MulliganDecision":
            pending = ((state.get("waiting_for") or {}).get("data", {})
                       or {}).get("pending", [])
            count = 1
            for p in pending:
                if p.get("player") == pid:
                    ph = p.get("phase", {}) or {}
                    if ph.get("type") == "BottomCards":
                        count = int(ph.get("count", 1))
            picks = hand_oids(state, pid)[:count]
            await submit_as_is(c, {"type": "SelectCards",
                                  "data": {"cards": [int(x) for x in picks]}})
            say(f"{c.name} bottoms {count}")
            return True
    # DiscardToHandSize
    wt0 = (state.get("waiting_for") or {}).get("type")
    if wt0 == "DiscardToHandSize":
        pend = (state.get("waiting_for") or {}).get("data") or {}
        if pend.get("player") == pid:
            n = pend.get("count") or max(0, len(hand_oids(state, pid)) - 7)
            picks = hand_oids(state, pid)[:n]
            if picks:
                await submit_as_is(c, {"type": "SelectCards",
                                      "data": {"cards": [int(x) for x in picks]}})
                say(f"{c.name} discards {len(picks)} to hand size")
                return True
    # combat: declare empty
    for a in acts:
        if a["type"] == "DeclareAttackers":
            sub = copy.deepcopy(a)
            sub["data"]["attacks"] = []
            sub["data"]["bands"] = []
            await submit_as_is(c, sub)
            return True
        if a["type"] == "DeclareBlockers":
            sub = copy.deepcopy(a)
            sub["data"]["assignments"] = []
            await submit_as_is(c, sub)
            return True
    # legend rule: keep the first Dargo (submit advertised choice as-is)
    for a in acts:
        if a["type"] == "ChooseLegend":
            await submit_as_is(c, copy.deepcopy(a))
            say(f"{c.name} answers ChooseLegend (keep first)")
            wire("legend_answered", {"player": pid})
            return True
    # P0 cast-decision pending: answer via interactions, never pass.
    wtype = wt0
    wplayer = (state.get("waiting_for") or {}).get("data", {}).get("player")
    if is_p0 and await scan_interactions(c, st):
        return True
    # Bombardment sacrifice offered as a plain SelectCards action
    if is_p0 and ST.get("bombard_active") and not ST.get("bombard_sac_paid"):
        for a in acts:
            if a["type"] == "SelectCards":
                oid = ST.get("bombard_sac_oid")
                await submit_as_is(c, {"type": "SelectCards",
                                      "data": {"cards": [int(oid)]}})
                ST["bombard_sac_paid"] = True
                say(f"[P0] Bombardment sacrifices Memnite {oid} (SelectCards)")
                return True
    # Dargo additional-cost permanents offered as a plain SelectCards action
    if is_p0 and CAST.get("in_flight") and CAST.get("paid") \
            and not CAST.get("sac_done"):
        for a in acts:
            if a["type"] == "SelectCards":
                want = [int(x) for x in (CAST["plan"].get("sac_oids") or [])]
                await submit_as_is(c, {"type": "SelectCards",
                                      "data": {"cards": want}})
                CAST["sac_done"] = True
                say(f"[P0] {CAST['tag']}: sacrificed {want} (SelectCards)")
                return True
    hold = False
    if wtype in ("OptionalCostChoice", "TargetSelection", "ManaPayment",
                 "ChooseXValue") \
            and wplayer == 0:
        hold = True
    if is_p0 and CAST.get("in_flight") and not dargo_on_stack(state):
        hold = True  # never pass mid-announcement; once the spell is on the
        # stack P0 must pass so it can resolve (main_step passes then)
    if is_p0 and ST.get("bombard_active"):
        # hold only while P0 actually has a bombard decision pending;
        # otherwise P0 must pass so the ability can resolve.
        bwf = (state.get("waiting_for") or {}).get("type")
        bwplayer = (state.get("waiting_for") or {}).get("data", {}).get("player")
        if bwf in ("TargetSelection", "PayCost", "OptionalCostChoice",
                   "ManaPayment") and bwplayer == 0:
            hold = True
    if hold:
        return False
    if is_p0 and is_my_main(state, pid):
        if ST["stage"] == "SETUP":
            hid = find_hand(state, pid, MOUNTAIN)
            for a in acts:
                if (a["type"] == "PlayLand" and hid
                        and str(a.get("data", {}).get("object_id")) == hid):
                    await submit_as_is(c, a)
                    return True
            for o in hand_oids(state, pid):
                if oname(state["objects"][o]) == MEMNITE:
                    for a in acts:
                        if (a["type"] == "CastSpell" and str(
                                a.get("data", {}).get("object_id")) == o):
                            await submit_as_is(c, a)
                            say("P0 casts Memnite")
                            return True
            if not any(oname(o) == BOMBARD for _, o in bf(state, 0)):
                bid = find_hand(state, pid, BOMBARD)
                if bid and untapped_land(state, 0, MOUNTAIN) >= 2:
                    for a in acts:
                        if (a["type"] == "CastSpell" and str(
                                a.get("data", {}).get("object_id")) == bid):
                            await submit_as_is(c, a)
                            say("P0 casts Goblin Bombardment")
                            return True
        elif ST["stage"] == "MAIN":
            return await main_step(c, pid, state, acts)
    # default: pass priority
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


async def main_step(c, pid, state, acts):
    """Execute the current MAIN step. Returns True if it acted."""
    step = ST["step"]
    if step == 0:
        # LEG1a: activate Bombardment, sac one Memnite, target P1.
        # Guard: once an activation is in flight, pass priority so it can
        # resolve instead of stacking another activation (run d burned all
        # Memnites here). The tick's hold logic already keeps P0 from passing
        # while a bombard decision (TargetSelection/PayCost) is pending.
        if ST.get("bombard_active"):
            # Once the sacrifice is paid AND the 1 damage resolved, the main
            # loop will advance the step; do NOT pass here (an empty-stack
            # pass would advance the phase past the cast window). Replicates
            # the completion gate so a lagging main loop can't cause it.
            mems_now = sum(1 for _, o in bf(state, 0) if oname(o) == MEMNITE)
            p1_life = state["players"][1].get("life")
            sac_paid = mems_now < ST.get("bombard_mem_before", mems_now + 1)
            dmg_done = (ST.get("bombard_p1_life") is not None
                        and p1_life is not None
                        and p1_life < ST["bombard_p1_life"])
            if sac_paid and dmg_done:
                return False
            for a in acts:
                if a["type"] == "PassPriority":
                    await submit_as_is(c, a)
                    say("P0 passes (bombardment activation resolving)")
                    return True
            return False
        mems = [oid for oid, o in bf(state, 0) if oname(o) == MEMNITE]
        bomb = next((oid for oid, o in bf(state, 0) if oname(o) == BOMBARD), None)
        if not mems or not bomb:
            say("MAIN step0: missing Memnite/Bombardment; aborting")
            ST["stop"] = True
            return False
        ST["bombard_sac_oid"] = mems[0]
        ST["bombard_mem_before"] = len(mems)
        ST["bombard_p1_life"] = state["players"][1].get("life")
        for a in acts:
            if a["type"] == "ActivateAbility" and str(
                    a.get("data", {}).get("source_id", "")) == str(bomb):
                ST["bombard_active"] = True
                await submit_as_is(c, a)
                say(f"P0 activates Bombardment (sac Memnite {mems[0]})")
                return True
        # fallback: any ActivateAbility whose source is Bombardment
        for a in acts:
            if a["type"] == "ActivateAbility":
                wire("activate_shape", {"action": a})
                ST["bombard_active"] = True
                await submit_as_is(c, a)
                say("P0 activates Bombardment (fallback shape)")
                return True
        say("MAIN step0: ActivateAbility not advertised; waiting")
        return False
    if step in (1, 2):
        tag = "LEG1" if step == 1 else "LEG2"
        if CAST.get("in_flight"):
            # Announcement/resolution in flight: pass priority only while the
            # spell is on the stack awaiting passes; otherwise wait for the
            # main loop to observe resolution (passing on an empty stack
            # would advance the phase past the next cast window).
            if dargo_on_stack(state):
                for a in acts:
                    if a["type"] == "PassPriority":
                        await submit_as_is(c, a)
                        say(f"P0 passes ({tag} on stack)")
                        return True
            return False
        plan = {"pay_additional": step == 2, "sac_oids": []}
        if step == 2:
            mems = [oid for oid, o in bf(state, 0) if oname(o) == MEMNITE]
            plan["sac_oids"] = mems[:2]
            if len(plan["sac_oids"]) < 2:
                say("MAIN step2: need 2 Memnites for additional cost")
                ST["stop"] = True
                return False
        did = find_hand(state, pid, DARGO)
        if not did:
            say(f"MAIN {tag}: no Dargo in hand; aborting")
            ST["stop"] = True
            return False
        for a in acts:
            if (a["type"] == "CastSpell"
                    and str(a.get("data", {}).get("object_id")) == did):
                LEG[tag] = {"untapped_before": untapped_land(state, 0, MOUNTAIN),
                            "pool_before": pool_len(state, 0)}
                new_cast(tag, plan)
                await submit_as_is(c, a)
                say(f"P0 casts {tag} (Dargo oid {did}); "
                    f"untapped_before={LEG[tag]['untapped_before']}")
                return True
        return False
    return False


# ------------------------------------------------------------------ main

async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    A = obs["assert"]

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck((DARGO, 8), (MEMNITE, 8), (BOMBARD, 4), (MOUNTAIN, 40)))
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck((MOUNTAIN, 60)))
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    wire("game", {"code": p0.game_code})

    global C0
    C0 = p0

    last_rev = {}
    force_tick = {}
    TIMEOUT = 2400
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        for c, pid, is_p0 in ((p0, p0.player_id, True),
                              (p1, p1.player_id, False)):
            # A rejection leaves the revision unchanged, so the revision
            # gate alone would never re-tick: drain the inbox here and
            # force a re-tick so the failed decision can be retried.
            rej = drain_rejections(c)
            if rej and LAST_SUBMIT["iid"] in SUBMITTED:
                SUBMITTED.discard(LAST_SUBMIT["iid"])
                say(f"[{c.name}] resync: retrying {LAST_SUBMIT['iid']} "
                    f"after rejection")
                LAST_SUBMIT["iid"] = None
                force_tick[c.name] = True
            if c.revision == last_rev.get(c.name) \
                    and not force_tick.get(c.name):
                continue
            force_tick[c.name] = False
            try:
                if await tick(c, pid, is_p0):
                    last_rev[c.name] = c.revision
            except Exception as e:
                say(f"tick error {c.name}: {e}")
        st = p0.latest
        if not st:
            continue
        record_wf(st["state"])
        state = st["state"]

        # SETUP -> MAIN gate
        if ST["stage"] == "SETUP" and is_my_main(state, 0):
            n_dargo = sum(1 for o in hand_oids(state, 0)
                          if oname(state["objects"][o]) == DARGO)
            n_mem = sum(1 for _, o in bf(state, 0) if oname(o) == MEMNITE)
            has_bomb = any(oname(o) == BOMBARD for _, o in bf(state, 0))
            if (n_dargo >= 2 and n_mem >= 4 and has_bomb
                    and untapped_land(state, 0, MOUNTAIN) >= 15
                    and pool_len(state, 0) == 0):
                await export_now("pre.json")
                ST["stage"] = "MAIN"
                ST["step"] = 0
                say("=== stage -> MAIN ===")
                wire("setup_ready", {"dargo": n_dargo, "memnites": n_mem,
                                    "untapped": untapped_land(state, 0, MOUNTAIN)})
                continue
        # Bombardment activation completion: sacrifice paid (a Memnite left
        # the battlefield) and the 1 damage resolved (P1 life dropped).
        # The ability may resolve between observed revisions, so the
        # stack sighting is not required.
        if ST["stage"] == "MAIN" and ST["step"] == 0 and ST.get("bombard_active"):
            if bombard_on_stack(state) and not ST.get("bombard_stacked"):
                ST["bombard_stacked"] = True
                say("Bombardment ability on stack")
                wire("bombard_stacked", {})
            mems_now = sum(1 for _, o in bf(state, 0) if oname(o) == MEMNITE)
            p1_life = state["players"][1].get("life")
            sac_paid = mems_now < ST.get("bombard_mem_before", mems_now + 1)
            dmg_done = (ST.get("bombard_p1_life") is not None
                        and p1_life is not None
                        and p1_life < ST["bombard_p1_life"])
            if sac_paid and dmg_done:
                ST["bombard_active"] = False
                ST["step"] = 1
                SUBMITTED.clear()
                say("=== bombardment activation done -> step 1 (LEG1 cast) ===")
                wire("bombard_done", {"p1_life": p1_life,
                                     "memnites": mems_now})
                continue
        # cast completion detection
        if CAST.get("in_flight"):
            tag = CAST["tag"]
            if dargo_on_stack(state):
                CAST["announced"] = True
            wf = (state.get("waiting_for") or {})
            wtype = wf.get("type")
            pending_p0 = wtype in ("OptionalCostChoice", "TargetSelection",
                                   "ManaPayment", "ChooseLegend") and \
                (wf.get("data") or {}).get("player") == 0
            resolved = (not dargo_on_stack(state) and CAST.get("announced")
                        and not pending_p0)
            if resolved:
                CAST["in_flight"] = False
                CAST["done"] = True
                s = await export_now(f"post_{tag.lower()}.json")
                env = json.loads(s)
                stt = env["state"]
                LEG[tag]["untapped_after"] = untapped_land(stt, 0, MOUNTAIN)
                LEG[tag]["pool_after"] = pool_len(stt, 0)
                LEG[tag]["spent"] = (LEG[tag]["untapped_before"]
                                     - LEG[tag]["untapped_after"])
                say(f"{tag} resolved: spent={LEG[tag]['spent']} "
                    f"(before={LEG[tag]['untapped_before']} "
                    f"after={LEG[tag]['untapped_after']})")
                wire("cast_resolved", {"tag": tag, "leg": LEG[tag]})
                SUBMITTED.clear()
                if tag == "LEG1":
                    ST["step"] = 2
                    say("=== -> step 2 (LEG2 cast) ===")
                else:
                    say("=== LEG2 done; finishing ===")
                    ST["stop"] = True
                continue

    # ------------------------------------------------------- evaluate
    def snap_untapped(path):
        env = json.load(open(f"{EVDIR}/{path}"))
        stt = env["state"]
        return {
            "untapped": untapped_land(stt, 0, MOUNTAIN),
            "pool": pool_len(stt, 0),
            "dargo_bf": len(dargo_on_bf(stt, 0)),
            "dargo_hand": sum(1 for o in hand_oids(stt, 0)
                              if oname(stt["objects"][o]) == DARGO),
        }

    pre = snap_untapped("pre.json")
    A["A1_setup_ok"] = ("passed" if pre["dargo_hand"] >= 2
                        and pre["untapped"] >= 15 and pre["pool"] == 0
                        else "failed")
    A["A2_additional_offered"] = ("passed" if "OptionalCostChoice" in WF_SEEN
                                  else "failed")
    leg1 = LEG.get("LEG1", {})
    leg2 = LEG.get("LEG2", {})
    # LEG1: 1 prior sac, 0 additional -> Oracle cost {6}{R}-{2} = 5 mana
    A["A3_prior_discount"] = ("passed" if leg1.get("spent") == 5
                              else ("failed" if "spent" in leg1 else "not-run"))
    # LEG2: 1 prior + 2 additional -> {6}{R}-{6} = 1 mana
    A["A4_additional_discount"] = ("passed" if leg2.get("spent") == 1
                                   else ("failed" if "spent" in leg2
                                         else "not-run"))
    A["A5_cleanup"] = ("passed" if ST["stop"] and leg2.get("spent") is not None
                       else "failed")
    obs["notes"].append(f"WF sequence: {WF_SEEN}")
    obs["notes"].append(f"LEG1: {leg1}")
    obs["notes"].append(f"LEG2: {leg2}")
    for k in sorted(A):
        say(f"{k}: {A[k]}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "wf_sequence": WF_SEEN,
                   "legs": LEG}, f, indent=2)

    await p0.close()
    await p1.close()
    return obs


if __name__ == "__main__":
    obs = asyncio.run(main())
    print(json.dumps(obs["assert"], indent=2))
