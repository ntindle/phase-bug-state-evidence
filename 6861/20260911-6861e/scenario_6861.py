#!/usr/bin/env python3
"""Issue #6861: Squee, the Immortal cannot be cast from exile.

Reported (Discord): "the card says you can cast it from grave and exile but
after exiling him I can't cast him anymore; tested also from graveyard and
from the grave seems to work just fine."

Oracle: "You may cast this card from your graveyard or from exile."
Pinned v0.79.0 card data parses this as ONLY a GraveyardCastPermission
(active_zones=["Graveyard"]) -- no Exile permission. Classifier + triage
analysis agree the exile half is dropped.

Plan (two human seats, native engine):
  SETUP - land drops; cast Faithless Looting, discard 2 Squees; cast
          Scavenging Ooze; activate Ooze targeting a Squee in P0's
          graveyard (exiles it). Gate: 1 Squee in exile, 1 Squee in gy,
          >=6 untapped lands, P0 main phase -> export pre.json.
  EXILE - attempt to cast the exiled Squee. Record whether CastSpell is
          advertised (A2) and whether it reaches stack -> battlefield (A3).
  GYCTL - control: cast a Squee from the graveyard (A4); answer the
          legend rule as-is if it appears.

Behavioral contract:
  A1 setup_ok            pre.json: Squee in exile + Squee in gy, P0 main
                         phase priority, >=6 untapped lands
  A2 exile_cast_offered  CastSpell advertised for the exiled Squee (legal
                         actions or viewer interaction)
  A3 exile_cast_completes exiled Squee reaches Stack then Battlefield
  A4 gy_cast_completes   control: gy Squee reaches Stack then Battlefield
  A5 cleanup             game proceeds after both attempts (stack empty,
                         no stuck prompt)

Verdict = reproduced iff A2 or A3 fails; not-reproduced iff A2, A3, A4
all pass.
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
RUN_ID = "20260911-6861e"
EVID_ISSUE = "6861"
EVDIR = f"{BACKFILL}/evidence/{EVID_ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty"

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
        WIRE.write(json.dumps({"t": time.time(),
                               "event": event + "_unserializable",
                               "error": str(e)}) + "\n")
    WIRE.flush()


SQUEE = "Squee, the Immortal"
LOOTING = "Faithless Looting"
OOZE = "Scavenging Ooze"
MOUNTAIN = "Mountain"
FOREST = "Forest"

ST = {"stage": "SETUP", "step": 0, "stop": False,
      "a2_offered": None, "a2_source": None}
CAST = {}          # active cast tracking: tag/oid/in_flight/announced/...
LOOT = {"cast": False, "in_flight": False, "discarded": False}
OOZE_ST = {"cast": False, "active": False, "done": False}
MULLS = {"P0": 0}
SHAPES = set()
WF_SEEN = []
SUBMITTED = set()
LAST_SUBMIT = {"iid": None}


def oname(o):
    return o.get("card_name") or o.get("name") or ""


def owner_of(o, pid):
    return o.get("owner") == pid or o.get("controller") == pid


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


def zone_oids(state, pid, zone, name=None):
    out = []
    for oid, o in state["objects"].items():
        if o.get("zone") == zone and owner_of(o, pid):
            if name is None or oname(o) == name:
                out.append(str(oid))
    return out


def untapped_land(state, pid):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) in (MOUNTAIN, FOREST) and not o.get("tapped"))


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain"))


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def action_codes(ch):
    return [((s.get("data") or {}).get("code") or "")
            for s in ch.get("surfaces", []) or [] if s.get("type") == "action"]


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


def drain_rejections(c):
    found = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            found.append({"type": t, "data": data})
            wire("rejected", {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {json.dumps(data)[:220]}")
    return found


async def export_now(path):
    s = await C0.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return s


def castspell_advertised(acts, oid):
    for a in acts:
        if a["type"] == "CastSpell" and str(
                a.get("data", {}).get("object_id", "")) == str(oid):
            return a
    return None


def vi_cast_choice(st, oid):
    """Look for a viewer_interaction castSpell choice naming the object."""
    vi = get_vi(st)
    if not vi:
        return None
    for opp in vi.get("opportunities", []) or []:
        data = (opp.get("response") or {}).get("data", {}) or {}
        for ch in data.get("choices") or data.get("candidates") or []:
            codes = action_codes(ch)
            if "castSpell" not in codes:
                continue
            blob = json.dumps(ch, default=str)
            if str(oid) in blob or SQUEE in blob:
                return opp, ch
    return None

# ------------------------------------------------------- interaction driver

async def scan_interactions(c, st, state, acts):
    """Answer P0's pending decisions. Returns True if it acted."""
    vi = get_vi(st)
    if not vi:
        return False
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
                                      "step": ST["step"], "interaction": opp})
            say(f"[P0] new interaction shape rtype={rtype} spec={spec_type} "
                f"wf={wtype} n={len(chs)}")
        # --- Scavenging Ooze activation: choose the activateAbility choice ---
        if OOZE_ST.get("active") and not OOZE_ST.get("activated"):
            for ch in chs:
                if "activateAbility" in action_codes(ch):
                    await send_interaction(
                        c, {"interactionId": iid,
                            "response": {"type": "choose",
                                         "data": {"choiceId": ch["id"]}}})
                    SUBMITTED.add(iid)
                    OOZE_ST["activated"] = True
                    say("[P0] activates Scavenging Ooze (viewer interaction)")
                    return True
        # --- Ooze target selection: exile a Squee from P0's graveyard -------
        if OOZE_ST.get("activated") and not OOZE_ST.get("targeted") \
                and wtype == "TargetSelection":
            sq = zone_oids(state, 0, "Graveyard", SQUEE)
            if not sq:
                say("[P0] Ooze target: no Squee in gy; deferring")
                return False
            want = sq[0]
            for ch in chs:
                ref = None
                for s in ch.get("surfaces", []) or []:
                    d = s.get("data") or {}
                    if isinstance(d, dict) and "reference" in d:
                        ref = str(d["reference"])
                if ref == str(want):
                    if rtype == "schema" and spec_type in ("sequence", "select"):
                        resp_out = {"type": spec_type,
                                    "data": {"choiceIds": [ch["id"]]}}
                    elif rtype == "exactChoices":
                        resp_out = {"type": "choose",
                                    "data": {"choiceId": ch["id"]}}
                    else:
                        resp_out = {"type": rtype,
                                    "data": {"choiceIds": [ch["id"]]}}
                    await send_interaction(
                        c, {"interactionId": iid, "response": resp_out})
                    SUBMITTED.add(iid)
                    OOZE_ST["targeted"] = True
                    OOZE_ST["target_oid"] = want
                    say(f"[P0] Ooze exiles Squee {want}")
                    return True
            say("[P0] Ooze target: Squee not among candidates; deferring")
            return False
        # --- Faithless Looting discard: discard 2 Squees ---------------------
        if LOOT.get("in_flight") and not LOOT.get("discarded") \
                and wtype == "DiscardChoice":
            picks = [str(oid) for oid in hand_oids(state, 0)
                     if oname(state["objects"][oid]) == SQUEE][:2]
            if len(picks) < 2:
                say("[P0] Looting discard: <2 Squee in hand; deferring")
                return False
            # map object oids -> candidate choice ids via reference surface
            want_refs = set(picks)
            choice_ids = []
            for ch in chs:
                for s in ch.get("surfaces", []) or []:
                    d = s.get("data") or {}
                    if isinstance(d, dict) \
                            and str(d.get("reference")) in want_refs:
                        choice_ids.append(ch["id"])
                        break
            if len(choice_ids) < 2:
                say(f"[P0] Looting discard: only {len(choice_ids)} Squee "
                    f"candidates found (spec={spec_type}); deferring")
                return False
            if rtype == "schema" and spec_type in ("sequence", "select"):
                resp_out = {"type": spec_type,
                            "data": {"choiceIds": choice_ids}}
            elif rtype == "exactChoices" and len(choice_ids) == 1:
                resp_out = {"type": "choose",
                            "data": {"choiceId": choice_ids[0]}}
            else:
                say(f"[P0] Looting discard: unexpected rtype={rtype} "
                    f"spec={spec_type}; deferring")
                return False
            await send_interaction(
                c, {"interactionId": iid, "response": resp_out})
            SUBMITTED.add(iid)
            LOOT["discarded"] = True
            say(f"[P0] Looting discards {picks} via {choice_ids}")
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
                good = any(oname(state["objects"][o]) in (SQUEE, LOOTING)
                           for o in hand_oids(state, pid))
                choice = "Keep" if (good or MULLS["P0"] >= 2) else "Mulligan"
                if choice == "Mulligan":
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
    # legend rule: keep the first (submit advertised choice as-is)
    for a in acts:
        if a["type"] == "ChooseLegend":
            await submit_as_is(c, copy.deepcopy(a))
            say(f"{c.name} answers ChooseLegend (keep first)")
            wire("legend_answered", {"player": pid})
            return True
    # P0 pending decisions via viewer interaction
    wplayer = (state.get("waiting_for") or {}).get("data", {}).get("player")
    if is_p0 and await scan_interactions(c, st, state, acts):
        return True
    # never pass while P0 has a cast/ability decision pending
    if is_p0 and wt0 in ("OptionalCostChoice", "TargetSelection",
                        "ManaPayment", "ChooseXValue", "DiscardChoice") \
            and wplayer == 0:
        return False
    # a cast announcement in flight: hold until the spell is on the stack
    # (live state, not the announced flag -- the flag is set in main_step).
    # Release the hold once the spell resolved (announced, no longer on
    # stack) so the completion branch runs, on rejection, or after enough
    # revisions pass with no stack sighting (silent-fail path).
    if is_p0 and CAST.get("in_flight"):
        on_stack = (squee_on_stack(state, CAST.get("oid"))
                    if CAST.get("oid") else False)
        resolved = CAST.get("announced") and not on_stack
        if not on_stack and not resolved and not CAST.get("rejected"):
            revs = c.revision - (CAST.get("rev_at_submit") or c.revision)
            if revs < 15:
                return False
    # ooze activation in flight: hold only while P0 actually has an ooze
    # decision pending; otherwise P0 must pass so the ability can resolve.
    if is_p0 and OOZE_ST.get("active") and not OOZE_ST.get("done"):
        bwf = (state.get("waiting_for") or {}).get("type")
        bwplayer = (state.get("waiting_for") or {}).get("data", {}).get("player")
        if bwf in ("TargetSelection", "PayCost", "OptionalCostChoice",
                   "ManaPayment") and bwplayer == 0:
            return False
    if is_p0 and is_my_main(state, pid):
        if ST["stage"] == "SETUP":
            if await setup_step(c, pid, state, acts):
                return True
        elif await main_step(c, pid, state, acts, st):
            return True
        # else: fall through to default pass below
    # default: pass priority
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return False


async def setup_step(c, pid, state, acts):
    # land drop (retry every tick)
    lid = find_hand(state, pid, MOUNTAIN) or find_hand(state, pid, FOREST)
    for a in acts:
        if a["type"] == "PlayLand" and lid and str(
                a.get("data", {}).get("object_id")) == lid:
            await submit_as_is(c, a)
            return True
    # cast Faithless Looting
    if not LOOT["cast"] and untapped_land(state, 0) >= 1:
        lid2 = find_hand(state, pid, LOOTING)
        a = castspell_advertised(acts, lid2) if lid2 else None
        if a:
            LOOT["cast"] = True
            LOOT["in_flight"] = True
            await submit_as_is(c, a)
            say("P0 casts Faithless Looting")
            return True
    # cast Scavenging Ooze once 2+ Squees are in the graveyard
    if LOOT.get("discarded") and not OOZE_ST["cast"] \
            and untapped_land(state, 0) >= 2:
        n_gy = len(zone_oids(state, 0, "Graveyard", SQUEE))
        if n_gy >= 2:
            oid = find_hand(state, pid, OOZE)
            a = castspell_advertised(acts, oid) if oid else None
            if a:
                OOZE_ST["cast"] = True
                await submit_as_is(c, a)
                say("P0 casts Scavenging Ooze")
                return True
    # activate Ooze once it's on the battlefield and untapped
    if OOZE_ST["cast"] and not OOZE_ST["active"] and not OOZE_ST["done"]:
        ooze = [oid for oid, o in bf(state, 0)
                if oname(o) == OOZE and not o.get("tapped")]
        n_gy = len(zone_oids(state, 0, "Graveyard", SQUEE))
        if ooze and n_gy >= 1 and untapped_land(state, 0) >= 1:
            OOZE_ST["active"] = True
            say("P0: Ooze activation pending (via viewer interaction)")
            return True
    return False


async def main_step(c, pid, state, acts, st):
    step = ST["step"]
    if step == 1:
        return await exile_cast_step(c, pid, state, acts, st)
    if step == 2:
        return await gy_cast_step(c, pid, state, acts, st)
    return False


def squee_on_stack(state, oid):
    o = state["objects"].get(str(oid))
    return bool(o and o.get("zone") == "Stack" and oname(o) == SQUEE)


def squee_on_bf(state, oid):
    o = state["objects"].get(str(oid))
    return bool(o and o.get("zone") == "Battlefield" and oname(o) == SQUEE)


def new_cast(tag, oid):
    CAST.clear()
    CAST.update({"tag": tag, "oid": str(oid), "in_flight": True,
                 "announced": False, "stack_seen": False, "bf_seen": False,
                 "done": False, "rejected": False, "silent_fail": False,
                 "offered": None, "rev_at_submit": None, "submitted": False})


async def exile_cast_step(c, pid, state, acts, st):
    if not CAST.get("in_flight"):
        sq = zone_oids(state, 0, "Exile", SQUEE)
        if not sq:
            say("EXILE step: no Squee in exile; aborting")
            ST["stop"] = True
            return False
        eid = sq[0]
        a = castspell_advertised(acts, eid)
        vich = vi_cast_choice(st, eid)
        new_cast("EXILE", eid)
        CAST["offered"] = bool(a or vich)
        ST["a2_offered"] = CAST["offered"]
        ST["a2_source"] = ("legal_actions" if a else
                           ("viewer_interaction" if vich else "none"))
        wire("exile_cast_attempt", {"oid": eid, "offered": CAST["offered"],
                                    "source": ST["a2_source"]})
        say(f"EXILE cast: oid={eid} offered={CAST['offered']} "
            f"({ST['a2_source']})")
        if a:
            await submit_as_is(c, a)
        else:
            # attempt the raw action anyway; capture the rejection (or not).
            # card_id is required on the wire action; read it from the object.
            obj = state["objects"].get(str(eid), {})
            cid = obj.get("card_id", int(eid))
            await submit_as_is(c, {"type": "CastSpell",
                                  "data": {"object_id": int(eid),
                                           "card_id": int(cid),
                                           "targets": [],
                                           "payment_mode": {"type": "Auto"}}})
        CAST["submitted"] = True
        CAST["rev_at_submit"] = c.revision
        return True
    # in flight: track the spell
    if squee_on_stack(state, CAST["oid"]):
        CAST["announced"] = True
        CAST["stack_seen"] = True
        wire("exile_stack_seen", {"oid": CAST["oid"]})
        say("EXILE cast: Squee on stack")
    if squee_on_bf(state, CAST["oid"]):
        CAST["bf_seen"] = True
        wire("exile_bf_seen", {"oid": CAST["oid"]})
    if CAST.get("rejected"):
        CAST["in_flight"] = False
        CAST["done"] = True
        await export_now("post_exile.json")
        ST["step"] = 2
        say("=== EXILE cast rejected -> GY control (step 2) ===")
        return True
    if CAST.get("announced") and not squee_on_stack(state, CAST["oid"]):
        # stack emptied: resolved or countered; record bf transition
        CAST["in_flight"] = False
        CAST["done"] = True
        await export_now("post_exile.json")
        ST["step"] = 2
        say(f"=== EXILE cast done (bf_seen={CAST['bf_seen']}) -> step 2 ===")
        return True
    if (not CAST.get("announced") and CAST.get("submitted")
            and c.revision - (CAST.get("rev_at_submit") or c.revision) >= 15):
        # revisions advanced with no stack sighting: silent failure
        wf = (state.get("waiting_for") or {}).get("type")
        wplayer = (state.get("waiting_for") or {}).get("data", {}).get("player")
        if not (wf in ("TargetSelection", "OptionalCostChoice", "ManaPayment")
                and wplayer == 0):
            CAST["in_flight"] = False
            CAST["done"] = True
            CAST["silent_fail"] = True
            await export_now("post_exile.json")
            ST["step"] = 2
            say("=== EXILE cast silent-fail (no stack sighting) -> step 2 ===")
            return True
    # while the spell is on the stack, pass so it can resolve
    if CAST.get("announced"):
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(c, a)
                say("P0 passes (EXILE cast on stack)")
                return True
    return False


async def gy_cast_step(c, pid, state, acts, st):
    if not CAST.get("in_flight"):
        sq = zone_oids(state, 0, "Graveyard", SQUEE)
        if not sq:
            say("GY step: no Squee in graveyard; aborting")
            ST["stop"] = True
            return False
        gid = sq[0]
        a = castspell_advertised(acts, gid)
        new_cast("GYCTL", gid)
        CAST["offered"] = bool(a)
        wire("gy_cast_attempt", {"oid": gid, "offered": CAST["offered"]})
        say(f"GY control cast: oid={gid} offered={CAST['offered']}")
        if a:
            await submit_as_is(c, a)
            CAST["submitted"] = True
            CAST["rev_at_submit"] = c.revision
        else:
            say("GY control: no advertised CastSpell; marking failed")
            CAST["in_flight"] = False
            CAST["done"] = True
            await export_now("post_gy.json")
            ST["stop"] = True
        return True
    if squee_on_stack(state, CAST["oid"]):
        CAST["announced"] = True
        CAST["stack_seen"] = True
        wire("gy_stack_seen", {"oid": CAST["oid"]})
        say("GY cast: Squee on stack")
    if squee_on_bf(state, CAST["oid"]):
        CAST["bf_seen"] = True
        wire("gy_bf_seen", {"oid": CAST["oid"]})
    if CAST.get("rejected"):
        CAST["in_flight"] = False
        CAST["done"] = True
        await export_now("post_gy.json")
        ST["stop"] = True
        say("GY cast rejected; finishing")
        return True
    if CAST.get("announced") and not squee_on_stack(state, CAST["oid"]):
        CAST["in_flight"] = False
        CAST["done"] = True
        await export_now("post_gy.json")
        ST["stop"] = True
        say(f"GY cast done (bf_seen={CAST['bf_seen']}); finishing")
        return True
    if CAST.get("announced"):
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(c, a)
                say("P0 passes (GY cast on stack)")
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
    await p0.create(deck((SQUEE, 12), (LOOTING, 4), (OOZE, 4),
                         (MOUNTAIN, 20), (FOREST, 20)))
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck((MOUNTAIN, 30), (FOREST, 30)))
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    wire("game", {"code": p0.game_code})

    global C0
    C0 = p0

    last_rev = {}
    force_tick = {}
    last_tick_wall = {}
    TIMEOUT = 2400
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        now = time.time()
        for c, pid, is_p0 in ((p0, p0.player_id, True),
                              (p1, p1.player_id, False)):
            rej = drain_rejections(c)
            if rej and is_p0 and CAST.get("in_flight") \
                    and not CAST.get("announced"):
                CAST["rejected"] = True
                CAST["reject_data"] = rej
                say(f"[P0] cast {CAST.get('tag')} rejected")
                force_tick[c.name] = True
            if rej and LAST_SUBMIT["iid"] in SUBMITTED:
                SUBMITTED.discard(LAST_SUBMIT["iid"])
                say(f"[{c.name}] resync: retrying {LAST_SUBMIT['iid']} "
                    f"after rejection")
                LAST_SUBMIT["iid"] = None
                force_tick[c.name] = True
            # periodic re-tick even without a revision change (missed
            # broadcast resilience); at most every 5s per client.
            if now - last_tick_wall.get(c.name, 0) >= 5:
                force_tick[c.name] = True
            if c.revision == last_rev.get(c.name) \
                    and not force_tick.get(c.name):
                continue
            say(f"[{c.name}] rev {last_rev.get(c.name)} -> {c.revision}")
            force_tick[c.name] = False
            last_tick_wall[c.name] = now
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

        # Faithless Looting resolution bookkeeping
        if LOOT.get("in_flight") and LOOT.get("discarded"):
            if not zone_oids(state, 0, "Stack", LOOTING):
                LOOT["in_flight"] = False
                say("Looting resolved (discard done)")
        # Ooze exile bookkeeping
        if OOZE_ST.get("targeted") and not OOZE_ST.get("done"):
            tgt = OOZE_ST.get("target_oid")
            o = state["objects"].get(str(tgt), {})
            if o.get("zone") == "Exile":
                OOZE_ST["done"] = True
                OOZE_ST["active"] = False
                say(f"Ooze exile done: Squee {tgt} now in Exile")
                wire("ooze_exile_done", {"oid": tgt})

        # SETUP -> MAIN gate
        if ST["stage"] == "SETUP" and is_my_main(state, 0):
            n_ex = len(zone_oids(state, 0, "Exile", SQUEE))
            n_gy = len(zone_oids(state, 0, "Graveyard", SQUEE))
            un = untapped_land(state, 0)
            if n_ex >= 1 and n_gy >= 1 and un >= 6:
                await export_now("pre.json")
                ST["stage"] = "MAIN"
                ST["step"] = 1
                SUBMITTED.clear()
                say(f"=== stage -> MAIN (exile={n_ex} gy={n_gy} "
                    f"untapped={un}) ===")
                wire("setup_ready", {"exile": n_ex, "gy": n_gy,
                                     "untapped": un})
                continue

    # ------------------------------------------------------- evaluate
    def load_state(path):
        env = json.load(open(f"{EVDIR}/{path}"))
        return env["state"]

    pre = load_state("pre.json")
    A["A1_setup_ok"] = ("passed"
                        if len(zone_oids(pre, 0, "Exile", SQUEE)) >= 1
                        and len(zone_oids(pre, 0, "Graveyard", SQUEE)) >= 1
                        and untapped_land(pre, 0) >= 6
                        else "failed")
    A["A2_exile_cast_offered"] = ("passed" if ST["a2_offered"]
                                  else ("failed" if ST["a2_offered"] is False
                                        else "not-run"))
    A["A5_cleanup"] = "not-run"

    # rebuild EXILE / GY results from wire events
    exile_stack = exile_bf = gy_stack = gy_bf = False
    exile_offered = ST["a2_offered"]
    gy_offered = None
    exile_rejected = False
    with open(f"{EVDIR}/wire_log.jsonl") as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            ev = e.get("event")
            if ev == "exile_stack_seen":
                exile_stack = True
            elif ev == "exile_bf_seen":
                exile_bf = True
            elif ev == "gy_stack_seen":
                gy_stack = True
            elif ev == "gy_bf_seen":
                gy_bf = True
            elif ev == "gy_cast_attempt":
                gy_offered = bool((e.get("payload") or {}).get("offered"))
            elif ev == "rejected":
                exile_rejected = True
    A["A3_exile_cast_completes"] = ("passed" if exile_stack and exile_bf
                                    else ("failed" if ST["step"] >= 2
                                          or exile_rejected
                                          else "not-run"))
    A["A4_gy_cast_completes"] = ("passed" if gy_stack and gy_bf
                                 else ("failed" if gy_offered is False
                                       or ST.get("stop")
                                       else "not-run"))
    # cleanup: game advanced past the attempts with an empty stack
    try:
        post = load_state("post_gy.json")
        stack_empty = not any(o.get("zone") == "Stack"
                              for o in post["objects"].values())
        A["A5_cleanup"] = ("passed" if stack_empty else "failed")
    except Exception:
        A["A5_cleanup"] = "not-run"

    obs["notes"].append(f"WF sequence: {WF_SEEN}")
    obs["notes"].append(f"exile offered={exile_offered} "
                        f"(source={ST['a2_source']}) stack={exile_stack} "
                        f"bf={exile_bf} rejected={exile_rejected}")
    obs["notes"].append(f"gy offered={gy_offered} stack={gy_stack} "
                        f"bf={gy_bf}")
    for k in sorted(A):
        say(f"{k}: {A[k]}")

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "wf_sequence": WF_SEEN,
                   "a2_source": ST["a2_source"]}, f, indent=2)

    await p0.close()
    await p1.close()
    return obs


if __name__ == "__main__":
    obs = asyncio.run(main())
    print(json.dumps(obs["assert"], indent=2))
