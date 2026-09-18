#!/usr/bin/env python3
"""Issue #6766: Sprout Swarm convoke — dismissing the payment ("pay 3") dialog
untaps a previously tapped creature.

Revalidation of the v0.79.0 run (verdict: not-reproduced) on v0.86.0
(protocol 72). Same behavioral contract as scenario_6766.py; adapted for
protocol-72 interaction shapes:

  - MulliganDecision: no top-level data.player; gate on my seat's presence in
    waiting_for.data.pending[] with phase Declare; answer the advertised
    action as-is (Keep) once per client.
  - DiscardToHandSize: data={player,count,cards}; answer via
    viewer_interaction (spec.type, usually "select") with choiceIds, else
    advertised SelectCards as-is with the id list replaced. Gate on
    data.player == my seat.
  - exactChoices viewer_interaction: response type "choose" + singular
    choiceId (never "exactChoices").
  - OptionalCostChoice buyback pay choice: identify via action-surface
    decideOptionalCost + value ("pay","true"); defensively also accept
    ("accept","true") style flags and non-empty text hints; never by text
    alone (texts can be empty).
  - Convoke tap: action code "tapForConvoke" with mana symbols ["G"].
  - CancelCast: submit the advertised legal_action as-is.
  - DeclareAttackers/Blockers: submit advertised as-is with attacks/bands/
    assignments cleared.
  - Per-client revision gating; interaction retry: if no revision advances
    within ~15s of a submission, drop the iid from SUBMITTED and retry.

Stages (single game): RAMP -> EXPLOIT1 -> EXPLOIT2 -> VARIANT -> CONTROL.
Assertions A1..A9 as in the v0.79.0 run.
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
RUN_ID = "20260917-6766"
EVID_ISSUE = "6766"
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


SHAPES = set()
WF_SEEN = []
ST = {"stage": "RAMP", "stop": False}
CAST = {}
SUBMITTED = {}  # iid -> (submit_time, stage)
MID_EXPORTED = {"EXPLOIT1": False, "EXPLOIT2": False}
MULL_ANSWERED = set()  # client names


def new_cast(stage):
    CAST.clear()
    CAST.update({"stage": stage, "attempted": False, "buyback": False,
                 "tapped_oid": None, "cancel_submitted": False,
                 "swarm_seen_on_stack": False, "done": False})


new_cast("RAMP")

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


def is_creature(o):
    ct = (o.get("card_type") or {}).get("core_types") or []
    return "Creature" in ct or "Saproling" in oname(o) or "Elves" in oname(o)


def creatures(state, pid):
    return [(oid, o) for oid, o in bf(state, pid) if is_creature(o)]


def untapped_land(state, pid, name):
    return sum(1 for _, o in bf(state, pid)
               if oname(o) == name and not o.get("tapped"))


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain"))


def swarm_on_stack(state):
    return [oid for oid, o in state["objects"].items()
            if oname(o) == "Sprout Swarm" and o.get("zone") == "Stack"]


def saproling_tokens(state):
    return [oid for oid, o in state["objects"].items()
            if "Saproling" in oname(o) and o.get("zone") == "Battlefield"]


def pool_mana(state, pid):
    p = state["players"][pid]
    return list((p.get("mana_pool") or {}).get("mana") or [])


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def action_codes(ch):
    return [((s.get("data") or {}).get("code") or "")
            for s in ch.get("surfaces", []) or [] if s.get("type") == "action"]


def value_flags(ch):
    return [((s.get("data") or {}).get("role"), (s.get("data") or {}).get("value"))
            for s in ch.get("surfaces", []) or [] if s.get("type") == "value"]


def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = s.get("data") or {}
            if isinstance(d, dict) and d.get("text"):
                t = d["text"]
                break
    return str(t)


def mana_symbols(ch):
    for s in ch.get("surfaces", []) or []:
        d = s.get("data") or {}
        if s.get("type") == "mana" and isinstance(d, dict):
            return d.get("symbols") or []
    return []


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
                             "data": (state.get("waiting_for") or {}).get("data")})


async def submit_as_is(c, action):
    wire("action_submit", {"who": c.name, "action": action})
    await c.send_action(action)


async def send_interaction(c, sub):
    wire("interaction_submit", {"who": c.name, "submission": sub})
    await c.send_interaction(sub)


def export_state_sync(path, state_str):
    with open(path, "w") as f:
        f.write(state_str)


C0 = None


async def export_now(path):
    s = await C0.export_state()
    export_state_sync(f"{EVDIR}/{path}", s)
    say(f"exported {path}")
    return s


def retry_stale_submissions(c):
    """Drop iids whose submission produced no revision in ~15s."""
    now = time.time()
    for iid, (ts, stage) in list(SUBMITTED.items()):
        if now - ts > 15:
            del SUBMITTED[iid]
            wire("submit_retry", {"iid": iid, "stage": stage})
            say(f"[{c.name}] retry: dropped stale iid {iid}")


# ------------------------------------------------------- interaction driver

async def scan_interactions(c, st):
    vi = get_vi(st)
    if not vi:
        return False
    state = st.get("state") or {}
    wf = (state.get("waiting_for") or {})
    wtype = wf.get("type")
    wplayer = (wf.get("data") or {}).get("player")
    if wplayer != 0:
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
                                      "interaction": opp})
        # --- buyback optional-cost decision ---
        if wtype == "OptionalCostChoice":
            cost = ((wf.get("data") or {}).get("cost") or {})
            wire("optional_cost", {"iid": iid, "cost": cost,
                                  "stage": ST["stage"]})
            say(f"[P0] OptionalCostChoice (stage {ST['stage']})")
            if ST["stage"] == "VARIANT":
                for a in (st.get("legal_actions") or []):
                    if a.get("type") == "CancelCast":
                        await submit_as_is(c, copy.deepcopy(a))
                        CAST["cancel_submitted"] = True
                        say("[P0] VARIANT: CancelCast at OptionalCostChoice")
                        SUBMITTED[iid] = (time.time(), ST["stage"])
                        return True
                say("[P0] VARIANT: CancelCast not advertised!")
                return False
            # EXPLOIT1/2/CONTROL: pay the buyback. Identify the pay choice
            # via decideOptionalCost action code + affirmative value flag;
            # texts can be empty so never match on text alone.
            paid = False
            for ch in chs:
                codes = action_codes(ch)
                flags = value_flags(ch)
                aff = [f for f in flags if f[1] == "true"]
                if "decideOptionalCost" in codes and aff:
                    await send_interaction(
                        c, {"interactionId": iid,
                            "response": {"type": "choose",
                                        "data": {"choiceId": ch["id"]}}})
                    SUBMITTED[iid] = (time.time(), ST["stage"])
                    CAST["buyback"] = True
                    say(f"[P0] buyback -> pay (flags={aff})")
                    paid = True
                    return True
            wire("buyback_no_pay_choice", {"iid": iid, "stage": ST["stage"],
                                          "choices": [
                                              {"id": ch.get("id"),
                                               "codes": action_codes(ch),
                                               "flags": value_flags(ch),
                                               "text": choice_text(ch)}
                                              for ch in chs]})
            say("[P0] buyback pay choice not identified; deferring")
            return False
        # --- mana payment: tap exactly one creature for convoke (Green) ---
        if wtype == "ManaPayment" and ST["stage"] in (
                "EXPLOIT1", "EXPLOIT2", "CONTROL"):
            if not CAST["tapped_oid"]:
                for ch in chs:
                    if "tapForConvoke" not in action_codes(ch):
                        continue
                    if mana_symbols(ch) != ["G"]:
                        continue
                    obj = choice_obj(ch)
                    if obj.get("tapped"):
                        continue
                    nm = obj.get("name") or ""
                    if "Elves" not in nm and "Saproling" not in nm:
                        continue
                    await send_interaction(
                        c, {"interactionId": iid,
                            "response": {"type": "choose",
                                        "data": {"choiceId": ch["id"]}}})
                    SUBMITTED[iid] = (time.time(), ST["stage"])
                    CAST["tapped_oid"] = str(obj.get("reference"))
                    say(f"[P0] tapped {nm} (oid {CAST['tapped_oid']}) "
                        f"for convoke (Green)")
                    wire("convoke_tap", {"oid": CAST["tapped_oid"],
                                        "name": nm, "stage": ST["stage"]})
                    return True
                # fallthrough: no Green tap found this opportunity
            else:
                if ST["stage"] in ("EXPLOIT1", "EXPLOIT2") \
                        and not CAST["cancel_submitted"]:
                    if not MID_EXPORTED[ST["stage"]]:
                        await export_now(
                            f"mid_{ST['stage'].lower()}_tapped.json")
                        MID_EXPORTED[ST["stage"]] = True
                        wire("mid_exported", {"stage": ST["stage"]})
                    for a in (st.get("legal_actions") or []):
                        if a.get("type") == "CancelCast":
                            await submit_as_is(c, copy.deepcopy(a))
                            CAST["cancel_submitted"] = True
                            say(f"[P0] {ST['stage']}: CancelCast at "
                                f"ManaPayment (dismiss analog)")
                            return True
                    say(f"[P0] {ST['stage']}: CancelCast not advertised!")
    return False


def pending_mulligan(state, pid):
    wf = state.get("waiting_for") or {}
    if wf.get("type") != "MulliganDecision":
        return False
    return any(p.get("player") == pid
               and (p.get("phase") or {}).get("type") == "Declare"
               for p in (wf.get("data") or {}).get("pending") or [])


async def discard_to_hand_size(c, st, pid):
    """Answer DiscardToHandSize for my seat; True if acted."""
    state = st.get("state") or {}
    wf = state.get("waiting_for") or {}
    data = wf.get("data") or {}
    if wf.get("type") != "DiscardToHandSize" or data.get("player") != pid:
        return False
    oids = hand_oids(state, pid)
    n = data.get("count") or max(0, len(oids) - 7)
    # rank discards: spare spells first, Forests next, witness cards last
    def rank(oid):
        nm = oname(state["objects"][oid])
        if nm == "Forest":
            return 1
        if nm in ("Sprout Swarm", "Llanowar Elves"):
            return 3
        return 0
    picks = sorted(oids, key=rank)[:n]
    if not picks:
        return False
    vi = get_vi(st)
    if vi:
        for opp in vi.get("opportunities", []) or []:
            resp = opp.get("response", {}) or {}
            rdata = resp.get("data") or {}
            spec = rdata.get("spec") or {}
            stype = spec.get("type") if isinstance(spec, dict) else None
            iid = opp.get("interactionId")
            if iid in SUBMITTED:
                continue
            chs = rdata.get("choices") or rdata.get("candidates") or []
            # candidate ids are "<iid>.s<N>"; match via surfaces object
            # reference (string oid), never raw oids
            ref_to_id = {}
            for ch in chs:
                for s in ch.get("surfaces", []) or []:
                    d = s.get("data") or {}
                    if s.get("type") == "object" and "reference" in d:
                        ref_to_id[str(d["reference"])] = ch.get("id")
            want = [ref_to_id[o] for o in picks if o in ref_to_id]
            if stype and want:
                key = (resp.get("type"), stype, "DiscardToHandSize", len(chs))
                if key not in SHAPES:
                    SHAPES.add(key)
                    wire("interaction_shape", {"rtype": resp.get("type"),
                                              "spec": stype,
                                              "wf": "DiscardToHandSize",
                                              "n": len(chs),
                                              "interaction": opp})
                await send_interaction(
                    c, {"interactionId": iid,
                        "response": {"type": stype,
                                    "data": {"choiceIds": want}}})
                SUBMITTED[iid] = (time.time(), ST["stage"])
                say(f"[{c.name}] discards {len(want)} via {stype} "
                    f"(candidates matched by reference)")
                return True
            wire("discard_no_candidates", {"iid": iid, "stage": ST["stage"],
                                          "picks": picks,
                                          "ref_keys": list(ref_to_id)[:8]})
    for a in (st.get("legal_actions") or []):
        if a.get("type") == "SelectCards":
            sub = copy.deepcopy(a)
            d = sub.setdefault("data", {})
            if "cardIds" in d:
                d["cardIds"] = [int(x) for x in picks]
            elif "cards" in d:
                d["cards"] = [int(x) for x in picks]
            else:
                d["cardIds"] = [int(x) for x in picks]
            await submit_as_is(c, sub)
            say(f"[{c.name}] discards {len(picks)} via SelectCards")
            return True
    say(f"[{c.name}] DiscardToHandSize: no answer path!")
    return False


async def clear_declare(c, a):
    sub = copy.deepcopy(a)
    d = sub.setdefault("data", {})
    for k in ("attacks", "bands", "assignments"):
        if k in d:
            d[k] = []
    await submit_as_is(c, sub)


# ------------------------------------------------------------------ tick

async def tick(c, pid, is_p0):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    retry_stale_submissions(c)
    # mulligan (per-client, gated on pending[])
    if pending_mulligan(state, pid) and c.name not in MULL_ANSWERED:
        for a in acts:
            if a.get("type") == "MulliganDecision":
                await submit_as_is(c, copy.deepcopy(a))
                MULL_ANSWERED.add(c.name)
                say(f"{c.name} keeps (advertised as-is)")
                return True
    # discard to hand size
    if await discard_to_hand_size(c, st, pid):
        return True
    # combat: declare empty (not blind-passed)
    for a in acts:
        if a.get("type") in ("DeclareAttackers", "DeclareBlockers"):
            await clear_declare(c, a)
            say(f"{c.name} declares empty")
            return True
    # mana payments: NOT in exploit stages (we cancel instead)
    if ST["stage"] in ("RAMP", "CONTROL"):
        for a in acts:
            if a.get("type") in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(c, copy.deepcopy(a))
                return True
    if is_p0 and await scan_interactions(c, st):
        return True
    # hold while a cast decision for P0 is pending
    wt = (state.get("waiting_for") or {}).get("type")
    wplayer = (state.get("waiting_for") or {}).get("data", {}).get("player")
    hold = False
    if wt in ("OptionalCostChoice", "TargetSelection") and wplayer == 0:
        hold = True
    if wt == "ManaPayment" and wplayer == 0:
        if ST["stage"] in ("EXPLOIT1", "EXPLOIT2"):
            hold = True
        elif ST["stage"] == "CONTROL" and not CAST.get("tapped_oid"):
            hold = True
    if hold:
        return False
    if is_p0 and is_my_main(state, pid):
        if ST["stage"] in ("RAMP", "CONTROL"):
            hid = find_hand(state, pid, "Forest")
            for a in acts:
                if (a["type"] == "PlayLand" and hid
                        and str(a.get("data", {}).get("object_id")) == hid):
                    await submit_as_is(c, copy.deepcopy(a))
                    say(f"{c.name} plays Forest")
                    return True
        if ST["stage"] == "RAMP":
            eid = find_hand(state, pid, "Llanowar Elves")
            if eid and untapped_land(state, pid, "Forest") >= 1:
                for a in acts:
                    if (a["type"] == "CastSpell"
                            and str(a.get("data", {}).get("object_id")) == eid):
                        await submit_as_is(c, copy.deepcopy(a))
                        say(f"{c.name} casts Llanowar Elves")
                        return True
        if ST["stage"] in ("EXPLOIT1", "EXPLOIT2", "VARIANT", "CONTROL") \
                and not CAST["attempted"]:
            sid = find_hand(state, pid, "Sprout Swarm")
            if sid:
                for a in acts:
                    if (a["type"] == "CastSpell"
                            and str(a.get("data", {}).get("object_id")) == sid):
                        if ST["stage"] == "EXPLOIT1":
                            s = await c.export_state()
                            export_state_sync(f"{EVDIR}/pre.json", s)
                            say("[P0] exported pre.json")
                        await submit_as_is(c, copy.deepcopy(a))
                        CAST["attempted"] = True
                        say(f"[P0] {ST['stage']}: casts Sprout Swarm")
                        wire("swarm_cast", {"stage": ST["stage"]})
                        return True
    # idle: pass priority
    for a in acts:
        if a.get("type") == "PassPriority":
            await submit_as_is(c, copy.deepcopy(a))
            return True
    return False


# --------------------------------------------------------------- assertions

def tapped_sets(state, pid):
    cr = sorted(oid for oid, o in creatures(state, pid) if o.get("tapped"))
    ld = sorted(oid for oid, o in bf(state, pid)
                if oname(o) == "Forest" and o.get("tapped"))
    return cr, ld


def snapshot(path):
    env = json.load(open(path))
    st = env["state"]
    cr, ld = tapped_sets(st, 0)
    return {
        "tokens": len(saproling_tokens(st)),
        "swarm_hand": sum(1 for oid in hand_oids(st, 0)
                          if oname(st["objects"][oid]) == "Sprout Swarm"),
        "swarm_stack": len(swarm_on_stack(st)),
        "pool": len(pool_mana(st, 0)),
        "creatures_tapped": cr,
        "lands_tapped": ld,
        "cancelled_casts": list(st.get("cancelled_casts") or []),
        "hand_size": len(hand_oids(st, 0)),
    }


async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    A = obs["assert"]

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    await p0.create(deck(("Sprout Swarm", 12), ("Llanowar Elves", 12),
                         ("Forest", 36)))
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, deck(("Forest", 60)))
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")

    global C0
    C0 = p0

    def advance_to(stage):
        ST["stage"] = stage
        new_cast(stage)
        SUBMITTED.clear()
        say(f"=== stage -> {stage} ===")

    async def double_cancel_check(tag):
        st = p0.latest["state"]
        offered = any(a.get("type") == "CancelCast"
                      for a in (p0.latest.get("legal_actions") or []))
        before = snapshot(f"{EVDIR}/post_{tag}.json")
        result = {"offered": offered, "state_changed": None}
        if offered:
            for a in (p0.latest.get("legal_actions") or []):
                if a.get("type") == "CancelCast":
                    await submit_as_is(p0, copy.deepcopy(a))
                    say(f"[P0] {tag}: second CancelCast submitted")
                    break
            await asyncio.sleep(2)
            await export_now(f"post_{tag}b.json")
            after = snapshot(f"{EVDIR}/post_{tag}b.json")
            result["state_changed"] = (before != after)
            wire("double_cancel", {"tag": tag, "before": before,
                                  "after": after})
        else:
            say(f"[P0] {tag}: CancelCast not offered after cancel")
        return result

    last_rev = {}
    exploit1_done = exploit2_done = variant_done = control_done = False
    TIMEOUT = 2400
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        for c, pid, is_p0 in ((p0, p0.player_id, True),
                              (p1, p1.player_id, False)):
            if c.revision == last_rev.get(c.name):
                continue
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
        if ST["stage"] == "RAMP" and is_my_main(state, 0):
            un_creatures = [o for _, o in creatures(state, 0)
                            if not o.get("tapped")]
            if (find_hand(state, 0, "Sprout Swarm")
                    and len(un_creatures) >= 2
                    and untapped_land(state, 0, "Forest") >= 6):
                advance_to("EXPLOIT1")
                continue
        if swarm_on_stack(state):
            CAST["swarm_seen_on_stack"] = True
        wf = (state.get("waiting_for") or {})
        wtype = wf.get("type")
        cast_pending = wtype in ("OptionalCostChoice", "ManaPayment",
                                 "TargetSelection") and \
            (wf.get("data") or {}).get("player") == 0
        cancelled = (CAST["attempted"] and CAST["cancel_submitted"]
                     and find_hand(state, 0, "Sprout Swarm") is not None
                     and not swarm_on_stack(state) and not cast_pending)
        if cancelled and ST["stage"] in ("EXPLOIT1", "EXPLOIT2"):
            if (ST["stage"] == "EXPLOIT1" and exploit1_done) or \
               (ST["stage"] == "EXPLOIT2" and exploit2_done):
                continue
            tag = f"cancel{ST['stage'][-1]}"
            await export_now(f"post_{tag}.json")
            dbl = await double_cancel_check(tag)
            wire("cancel_complete", {"stage": ST["stage"], "double": dbl,
                                    "wf": wtype})
            if ST["stage"] == "EXPLOIT1":
                exploit1_done = True
                advance_to("EXPLOIT2")
            else:
                exploit2_done = True
                advance_to("VARIANT")
            continue
        if cancelled and ST["stage"] == "VARIANT" and not variant_done:
            await export_now("post_cancel3.json")
            dbl = await double_cancel_check("cancel3")
            wire("cancel_complete", {"stage": "VARIANT", "double": dbl,
                                    "wf": wtype})
            variant_done = True
            advance_to("CONTROL")
            continue
        if ST["stage"] == "CONTROL" and CAST["swarm_seen_on_stack"] \
                and not swarm_on_stack(state) and not control_done:
            pre_toks = snapshot(f"{EVDIR}/pre.json")["tokens"]
            if len(saproling_tokens(state)) > pre_toks:
                await export_now("post_resolve.json")
                control_done = True
                wire("control_resolved", {})
                say("CONTROL resolved; finishing")
                ST["stop"] = True

    # ------------------------------------------------------- evaluate
    def snap(p):
        return snapshot(f"{EVDIR}/{p}")

    pre = snap("pre.json")
    mid1 = snap("mid_exploit1_tapped.json")
    post1 = snap("post_cancel1.json")
    post2 = snap("post_cancel2.json")
    post3 = snap("post_cancel3.json")
    postr = snap("post_resolve.json") if control_done else None
    mid1_raw = json.load(open(f"{EVDIR}/mid_exploit1_tapped.json"))["state"]
    tap_oids = {}
    for line in open(f"{EVDIR}/wire_log.jsonl"):
        d = json.loads(line)
        if d["event"] == "convoke_tap":
            tap_oids[d["payload"].get("stage")] = d["payload"]["oid"]
    tapped_oid = tap_oids.get("EXPLOIT1")
    mid_tapped_flag = (tapped_oid is not None
                       and mid1_raw["objects"].get(tapped_oid, {})
                       .get("tapped") is True)
    mid_wf = mid1_raw.get("waiting_for") or {}
    mid_pending = (mid_wf.get("type") == "ManaPayment"
                   and (mid_wf.get("data") or {}).get("player") == 0)

    A["A1_setup_ok"] = ("passed" if pre["swarm_hand"] >= 1
                        and pre["pool"] == 0 else "failed")
    A["A2_buyback_prompted"] = ("passed" if "OptionalCostChoice" in WF_SEEN
                                else "failed")
    A["A3_convoke_tapped"] = ("passed" if mid_tapped_flag and mid_pending
                              else "failed")
    A["A4_cast_aborted"] = ("passed" if post1["swarm_hand"] >= 1
                            and post1["swarm_stack"] == 0 else "failed")
    A["A5_no_token"] = ("passed" if post1["tokens"] == pre["tokens"]
                        else "failed")
    dbl_ok = True
    for line in open(f"{EVDIR}/wire_log.jsonl"):
        d = json.loads(line)
        if d["event"] == "double_cancel":
            if d["payload"].get("state_changed"):
                dbl_ok = False
    A["A6_cancel_idempotent"] = ("passed" if dbl_ok
                                 and post1["creatures_tapped"] == []
                                 else "failed")
    A["A7_loop_no_advantage"] = ("passed"
                                 if post2["tokens"] == pre["tokens"]
                                 and post2["pool"] == 0
                                 and post2["creatures_tapped"] == []
                                 and post2["lands_tapped"] == pre["lands_tapped"]
                                 and post2["swarm_hand"] >= 1
                                 else "failed")
    A["A8_dismiss_at_buyback"] = ("passed"
                                  if post3["swarm_hand"] >= 1
                                  and post3["swarm_stack"] == 0
                                  and post3["tokens"] == pre["tokens"]
                                  and post3["creatures_tapped"] == []
                                  else "failed")
    if postr:
        ctrl_tapped_oid = None
        for line in open(f"{EVDIR}/wire_log.jsonl"):
            d = json.loads(line)
            if d["event"] == "convoke_tap" and \
                    d["payload"].get("stage") == "CONTROL":
                ctrl_tapped_oid = d["payload"]["oid"]
        A["A9_control_completes"] = ("passed"
                                    if postr["tokens"] == pre["tokens"] + 1
                                    and postr["swarm_hand"] >= 1
                                    and postr["swarm_stack"] == 0
                                    and (ctrl_tapped_oid in
                                         postr["creatures_tapped"])
                                    else "failed")
    else:
        A["A9_control_completes"] = "not-run"
        obs["notes"].append("control stage did not complete in time")

    for k in sorted(A):
        say(f"{k}: {A[k]}")
    say("WF sequence:", WF_SEEN)

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "wf_sequence": WF_SEEN}, f, indent=2)

    await p0.close()
    await p1.close()
    return obs


if __name__ == "__main__":
    obs = asyncio.run(main())
    print(json.dumps(obs["assert"], indent=2))
