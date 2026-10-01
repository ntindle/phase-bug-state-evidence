#!/usr/bin/env python3
"""Issue #6860 revalidation on v0.98.0 (protocol 94): Dargo, the Shipwrecker.

Reported (Discord): "I can only cast him for his original cost, he has 2
colorless discount for every creature/artifact sac that turn and you can sac
creatures and artifact as an additional cost while casting him; neither the
first nor the second discount can be activated."

Oracle: "As an additional cost to cast this spell, you may sacrifice any
number of artifacts and/or creatures. This spell costs {2} less to cast for
each permanent sacrificed this way and {2} less to cast for each other
artifact or creature you've sacrificed this turn."  (pinned cost: {6}{R})

The v0.79.0 run (2026-09-11, run 20260911-6860g, evidence commit
21bd480c669b24409a9f81730aa0001c0cd590ce) reproduced: LEG1 (1 prior sac, 0
additional) charged 7 instead of 5; LEG2 (1 prior + 2 additional) charged 3
instead of 1. The pinned v0.98.0 card data still collapses the cost into a
single ModifyCost { Reduce {2}, dynamic_count: TrackedSetSize } with
condition AdditionalCostPaid - the "each other artifact or creature you've
sacrificed this turn" reduction is still absent from the parse.

Plan (single P0 turn, two human seats; mana spent = untapped-Mountain delta,
engine auto-taps):
  SETUP - 15+ untapped Mountains, Goblin Bombardment + 4+ Memnites on BF,
          2+ Dargos in hand. Export pre.json.
  STEP0 - Activate Bombardment, sacrifice 1 Memnite (prior sac this turn).
  LEG1  - Cast Dargo#1 DECLINING the additional cost.
          Oracle: {6}{R} - {2} (prior sac) = 5 mana spent.
  LEG2  - Cast Dargo#2, PAYING the additional cost with 2 Memnites.
          Oracle: {6}{R} - {2} (prior) - {4} (additional) = 1 mana spent.

Behavioral contract:
  A1 setup_ok            pre.json: 2+ Dargo in hand, Bombardment + 4 Memnites
                         on BF, >=15 untapped Mountains, empty pool
  A2 additional_offered  Dargo announcement offered the optional any-number
                         artifact/creature sacrifice choice (OptionalCostChoice)
  A3 prior_discount      LEG1 (1 prior sac, 0 additional): mana spent == 5
  A4 additional_discount LEG2 (1 prior + 2 additional): mana spent == 1
  A5 cleanup             both casts resolved; legend rule answered; game
                         proceeds without a stuck prompt

Verdict = reproduced iff A3 or A4 fails; not-reproduced iff all pass;
blocked if setup never reaches or a leg cannot run.

Protocol-94 driver (v0.98.0, per scenario_301_0980): HELLO advertises protocol
94 (exact match); MulliganDecision as {"choice":{"type":"Keep"}} gated on the
seat's pending Declare; BottomCards/DiscardToHandSize via single SelectCards
{"cards":[...]}; ActivateAbility/CastSpell via advertised actions as-is;
optional-cost / number / permanent-selection decisions via viewer_interaction
opportunities (exactChoices role/value; number schema; sequence/select
schema); PassPriority only when the seat genuinely holds Priority; per-client
stale watchdog.
"""
import asyncio
import hashlib
import json
import logging
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client as _client
_client.URL = "ws://127.0.0.1:9374/ws"
from client import PhaseClient, deck  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario6860")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
EVID_RUN_ID = "20261001-6860"
EVDIR = f"{BACKFILL}/evidence/6860/{EVID_RUN_ID}"
assert not os.path.exists(EVDIR) or not os.listdir(EVDIR), "EVDIR not empty"
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
        WIRE.write(json.dumps({"t": time.time(), "event": event, "payload": payload}) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(), "event": event, "unserializable": str(e)}) + "\n")
    WIRE.flush()


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


SERVER_IDENTITY = {
    "validated_version": "v0.98.0",
    "build_commit": "61e8550",
    "protocol_version": 94,
    "server_binary_sha256": "15c50bbd3e90b9af49c851d9a56a775f4d74f5874f19e5ea231520816c8170a9",
    "card_data_sha256": "1a5919f2a50754c7f5e48922390816150a20b703821114adfe08427ff0b11960",
    "draft_pools_sha256": "bf3316202d84068ac38bcec48c5fc57d38d7834aef5f18c6b410ba9f64afd594",
    "signature_verified": True,
}

for _f, _k in (("server/releases/v0.98.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.98.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.98.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = sha256_of_file(f"{BACKFILL}/{_f}")
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
say("server identity hashes verified against on-disk pinned artifacts")

DARGO = "Dargo, the Shipwrecker"
BOMBARD = "Goblin Bombardment"
MEMNITE = "Memnite"
MOUNTAIN = "Mountain"

WF_SEEN = []
SUBMITTED = set()   # interactionIds answered
SHAPES = set()


# ------------------------------------------------------------------ helpers

def oname(o):
    return o.get("base_name") or o.get("card_name") or o.get("name") or ""


def bf(state, pid):
    return [o for o in state["objects"].values()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def find_bf(state, pid, name):
    for o in bf(state, pid):
        if oname(o) == name:
            return o["id"]
    return None


def untapped_mountains(state, pid):
    return sum(1 for o in bf(state, pid)
               if oname(o) == MOUNTAIN and not o.get("tapped"))


def memnites(state, pid):
    return [o for o in bf(state, pid) if oname(o) == MEMNITE]


def dargo_hand(state, pid):
    return [o for o in state["objects"].values()
            if o.get("zone") == "Hand" and o.get("controller") == pid and oname(o) == DARGO]


def dargo_bf(state, pid):
    return [o for o in bf(state, pid) if oname(o) == DARGO]


def dargo_stack(state):
    return [o for o in state["objects"].values()
            if oname(o) == DARGO and o.get("zone") == "Stack"]


def hand_ids(state, pid):
    return [str(o["id"]) for o in state["objects"].values()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def life(state, pid):
    return state["players"][pid]["life"]


def pool_len(state, pid):
    p = state["players"][pid]
    return len(list((p.get("mana_pool") or {}).get("mana") or []))


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_player(state):
    return (wf_of(state).get("data") or {}).get("player")


def merged_actions(st):
    return st.get("legal_actions") or []


def vi_ops(c):
    st = c.latest
    if not st:
        return []
    return (st.get("viewer_interaction") or {}).get("opportunities", []) or []


def pending_for(state, pid):
    wf = wf_of(state)
    data = wf.get("data") or {}
    for p in data.get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            return p
    return None


async def do_mulligan(c, pid, tag, mulls):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "MulliganDecision":
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    if mulls.get((tag, "kept")):
        return False
    mull_count = mulls.get((tag, "mulls"), 0)
    if tag == "P0":
        has_dargo = any(oname(state["objects"][oid]) == DARGO for oid in hand_ids(state, pid))
        choice = "Keep" if (has_dargo or mull_count >= 2) else "Mulligan"
    else:
        choice = "Keep"
    say(f"[{tag}] mulligan -> {choice} (prior mulligans: {mull_count})")
    await c.send_action({"type": "MulliganDecision", "data": {"choice": {"type": choice}}})
    if choice == "Mulligan":
        mulls[(tag, "mulls")] = mull_count + 1
    else:
        mulls[(tag, "kept")] = True
    wire("mulligan", {"who": tag, "decision": choice})
    return True


async def do_bottom(c, pid, tag, mulls):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    pend = pending_for(state, pid)
    if not pend:
        return False
    if (pend.get("phase") or {}).get("type") != "BottomCards":
        return False
    if (tag, "bottomed") in mulls:
        return False
    n = (pend.get("phase") or {}).get("count") or 1

    def rank(oid):
        nm = oname(state["objects"][oid])
        if nm == DARGO:
            return 2
        if nm in (BOMBARD, MEMNITE):
            return 1
        return 0

    picks = [int(x) for x in sorted(hand_ids(state, pid), key=rank)[:n]]
    say(f"[{tag}] bottoming {n}: {[oname(state['objects'][str(x)]) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    mulls[(tag, "bottomed")] = True
    wire("bottom", {"who": tag, "count": n})
    return True


_DISCARD_REV = {}


async def do_discard(c, pid, tag, mulls):
    st = c.latest
    if not st:
        return False
    rev = st.get("state_revision", -1)
    if _DISCARD_REV.get((c.name, rev)):
        return False
    state = st["state"]
    if wf_of(state).get("type") != "DiscardToHandSize":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    n = len(hand_ids(state, pid)) - 7
    if n <= 0:
        return False

    def rank(oid):
        nm = oname(state["objects"][oid])
        if nm in (DARGO, BOMBARD):
            return 2
        if nm == MEMNITE:
            return 1
        return 0

    picks = [int(x) for x in sorted(hand_ids(state, pid), key=rank)[:n]]
    say(f"[{tag}] discarding to hand size: {[oname(state['objects'][str(x)]) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    _DISCARD_REV[(c.name, rev)] = True
    return True


async def pay_tick(c):
    st = c.latest
    if not st:
        return False
    for a in merged_actions(st):
        if a.get("type") in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    return False


def my_main(state, pid):
    return (state.get("active_player") == pid
            and state.get("priority_player") == pid
            and state.get("phase") in ("PreCombatMain", "PostCombatMain", "Main"))


_PASSED_REV = {}
_WATCH = {}


def watch(c):
    now = time.time()
    last = _WATCH.get(c.name)
    if last and last["rev"] == c.revision and now - last["t"] > 45:
        st = c.latest
        view = "no-state"
        if st:
            s = st["state"]
            view = (f"turn={s.get('turn')} phase={s.get('phase')} "
                    f"active={s.get('active_player')} wf={json.dumps(wf_of(s))[:160]}")
        say(f"WATCHDOG [{c.name}] revision {c.revision} stale 45s+: {view}")
        last["t"] = now
    elif not last or last["rev"] != c.revision:
        _WATCH[c.name] = {"rev": c.revision, "t": now}


def record_wf(state):
    wf = wf_of(state).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf})


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
            say(f"[{c.name}] {t}: {json.dumps(data)[:300]}")
    return found


def log_new_shapes(c, tag):
    for op in vi_ops(c):
        resp = op.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        spec = data.get("spec") or {}
        key = (resp.get("type"), spec.get("type") if isinstance(spec, dict) else None,
               wf_of(c.latest["state"]).get("type"), len(data.get("choices") or data.get("candidates") or []))
        if key not in SHAPES:
            SHAPES.add(key)
            wire("interaction_shape", {"tag": tag, "shape": key,
                                      "interaction": op})
            say(f"[{tag}] new interaction shape: {key}")


# ------------------------------------------------------- interaction answers

def find_relations_op(c):
    """interactionId for a relations-schema (DeclareAttackers/DeclareBlockers)
    opportunity on protocol 94."""
    for op in vi_ops(c):
        resp = op.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        spec = data.get("spec") or {}
        if resp.get("type") == "schema" and isinstance(spec, dict) \
                and spec.get("type") == "relations":
            return op.get("interactionId") or op.get("id")
    return None


async def answer_declare(c, pid, tag):
    """Declare empty attackers/blockers via the relations schema (protocol
    94 has no legacy DeclareAttackers/DeclareBlockers action)."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    wf = wf_of(state)
    if wf.get("type") not in ("DeclareAttackers", "DeclareBlockers"):
        return False
    if str(wf_player(state)) != str(pid):
        return False
    iid = find_relations_op(c)
    if not iid:
        return False
    # InteractionResponse::Relations { relations: Vec<InteractionRelation> }
    # serde(tag="type", content="data", camelCase): each relation is
    # {"sourceId","targetId","group"}. Empty vec = declare nothing.
    sub = {"interactionId": iid,
           "response": {"type": "relations", "data": {"relations": []}}}
    wire("declare_empty", {"who": tag, "wf": wf.get("type"), "submission": sub})
    say(f"[{tag}] declares empty ({wf.get('type')})")
    await c.send_interaction(sub)
    SUBMITTED.add(iid)
    return True


def find_optional_cost(c, want_pay):
    """(iid, choice_id) for Dargo's optional any-number sacrifice cost."""
    for op in vi_ops(c):
        resp = op.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        for ch in data.get("choices", []) or []:
            surfs = ch.get("surfaces", []) or []
            codes = [s.get("data", {}).get("code") for s in surfs]
            if "decideOptionalCost" not in codes:
                continue
            is_pay = None
            for s in surfs:
                d = s.get("data", {}) or {}
                if d.get("role") == "pay":
                    is_pay = str(d.get("value")).lower() == "true"
            if is_pay is None:
                txt = json.dumps(surfs).lower()
                if "pay" in txt and "decline" not in txt:
                    is_pay = True
                elif "decline" in txt or "not pay" in txt:
                    is_pay = False
            if is_pay == want_pay:
                return op.get("interactionId") or op.get("id"), ch.get("id")
    return None, None


def find_number_op(c):
    for op in vi_ops(c):
        resp = op.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        spec = data.get("spec") or {}
        if resp.get("type") == "schema" and isinstance(spec, dict) \
                and spec.get("type") == "number":
            return op.get("interactionId") or op.get("id"), spec
    return None, None


def find_perm_select(c, want_oids):
    """(iid, submission, picked_count) for sacrificing permanents. Picks the
    candidate ids whose object reference matches the planned object ids;
    falls back to the first len(want) Memnite battlefield candidates."""
    for op in vi_ops(c):
        resp = op.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        cands = data.get("candidates") or data.get("choices") or []
        if not cands:
            continue
        want = set(str(x) for x in want_oids)
        picked = []
        for ch in cands:
            ref = None
            for s in ch.get("surfaces", []) or []:
                d = s.get("data", {}) or {}
                if isinstance(d, dict) and d.get("reference"):
                    ref = str(d["reference"])
            if ref and ref in want and ch.get("id") not in picked:
                picked.append(ch["id"])
        if not picked and want:
            # fallback: Memnite candidates
            for ch in cands:
                blob = json.dumps(ch.get("surfaces", [])).lower()
                if "memnite" in blob and ch.get("id") not in picked:
                    picked.append(ch["id"])
                if len(picked) >= len(want):
                    break
        if picked or not want:
            kind = resp.get("type")
            spec = data.get("spec") or {}
            spec_type = spec.get("type") if isinstance(spec, dict) else None
            if kind == "exactChoices":
                sub = {"type": "choose", "data": {"choiceId": picked[0] if picked else None}}
            else:
                sub = {"type": spec_type or "sequence", "data": {"choiceIds": picked}}
            return op.get("interactionId") or op.get("id"), sub, len(picked)
    return None, None, 0


def find_player_target(c, seat):
    """(iid, submission) targeting the given player seat."""
    for op in vi_ops(c):
        resp = op.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        cands = data.get("candidates") or data.get("choices") or []
        if not cands:
            continue
        for ch in cands:
            for s in ch.get("surfaces", []) or []:
                d = s.get("data", {}) or {}
                if d.get("role") in ("candidate", "target") and str(d.get("seat")) == str(seat):
                    kind = resp.get("type")
                    spec = data.get("spec") or {}
                    spec_type = spec.get("type") if isinstance(spec, dict) else None
                    if kind == "exactChoices":
                        sub = {"type": "choose", "data": {"choiceId": ch["id"]}}
                    else:
                        sub = {"type": spec_type or "sequence", "data": {"choiceIds": [ch["id"]]}}
                    return op.get("interactionId") or op.get("id"), sub
        if len(cands) == 1:
            ch = cands[0]
            kind = resp.get("type")
            spec = data.get("spec") or {}
            spec_type = spec.get("type") if isinstance(spec, dict) else None
            if kind == "exactChoices":
                sub = {"type": "choose", "data": {"choiceId": ch["id"]}}
            else:
                sub = {"type": spec_type or "sequence", "data": {"choiceIds": [ch["id"]]}}
            return op.get("interactionId") or op.get("id"), sub
    return None, None


# ------------------------------------------------------------------ flow

class Flow:
    def __init__(self):
        self.step = "SETUP"
        self.bombard_sac_oid = None
        self.bombard_mem_before = 0
        self.bombard_p1_life = None
        self.bombard_active = False
        self.cast = None  # dict(tag, plan, in_flight, announced, sac_oids)
        self.legs = {}
        self.legend_answered = 0


async def answer_cast_decisions(c, pid, flow):
    """Answer in-flight cast decisions (OptionalCostChoice / sac count /
    permanent selection). Called BEFORE the my_main block: announcement
    decisions arrive while P0 holds main-phase priority, so they must not
    be starved by the never-pass-mid-announcement guard."""
    cs = flow.cast
    if not (cs and cs["in_flight"] and flow.step in ("LEG1", "LEG2")):
        return False
    st = c.latest
    if not st:
        return False
    state = st["state"]
    wf = wf_of(state)
    if wf.get("type") == "OptionalCostChoice" and str(wf_player(state)) == str(pid):
        iid, cid = find_optional_cost(c, want_pay=cs["plan"]["pay_additional"])
        if iid:
            sub = {"interactionId": iid, "response": {"type": "choose", "data": {"choiceId": cid}}}
            wire("optional_cost", {"tag": cs["tag"], "want_pay": cs["plan"]["pay_additional"],
                                  "submission": sub})
            say(f"P0 {cs['tag']}: additional cost -> {'PAY' if cs['plan']['pay_additional'] else 'DECLINE'}")
            await c.send_interaction(sub)
            cs["paid"] = cs["plan"]["pay_additional"]
            SUBMITTED.add(iid)
            return True
        say(f"P0 {cs['tag']}: OptionalCostChoice pending but pay/decline not identified")
        wire("optional_cost_unanswered", {"interaction": vi_ops(c)})
        return False
    if cs["paid"] and not cs["sac_done"]:
        iid, spec = find_number_op(c)
        if iid:
            want_n = len(cs["plan"]["sac_oids"])
            sub = {"interactionId": iid, "response": {"type": "number", "data": {"value": want_n}}}
            wire("sac_count", {"tag": cs["tag"], "submission": sub})
            say(f"P0 {cs['tag']}: sacrifice count -> {want_n}")
            await c.send_interaction(sub)
            SUBMITTED.add(iid)
            return True
        iid, sub, n = find_perm_select(c, cs["plan"]["sac_oids"])
        if iid:
            wire("sac_select", {"tag": cs["tag"], "submission": sub})
            say(f"P0 {cs['tag']}: sacrificing {n} permanents")
            await c.send_interaction({"interactionId": iid, "response": sub})
            cs["sac_done"] = True
            SUBMITTED.add(iid)
            return True
    return False


async def p0_tick(c, pid, flow, mulls):
    st = c.latest
    if not st:
        return False
    if await do_mulligan(c, pid, "P0", mulls):
        return True
    if await do_bottom(c, pid, "P0", mulls):
        return True
    if await do_discard(c, pid, "P0", mulls):
        return True
    st = c.latest
    state, acts = st["state"], merged_actions(st)
    if await pay_tick(c):
        return True
    if await answer_declare(c, pid, "P0"):
        return True
    # legend rule: answer keep-first via the advertised action or a vi choice
    for a in acts:
        if a.get("type") == "ChooseLegend":
            wire("legend_action", {"action": a})
            await c.send_action(a)
            flow.legend_answered += 1
            say("P0 answers ChooseLegend (advertised action as-is)")
            return True
    log_new_shapes(c, "P0")
    # announcement decisions are answered before any main-phase logic
    if await answer_cast_decisions(c, pid, flow):
        return True
    if my_main(state, pid):
        if flow.step == "SETUP":
            # ramp: land, Memnite, Bombardment
            hid = None
            for oid in hand_ids(state, pid):
                if oname(state["objects"][oid]) == MOUNTAIN:
                    hid = oid
                    break
            for a in acts:
                if a.get("type") == "PlayLand" and hid and str(a.get("data", {}).get("object_id")) == hid:
                    await c.send_action(a)
                    return True
            for oid in hand_ids(state, pid):
                if oname(state["objects"][oid]) == MEMNITE:
                    for a in acts:
                        if a.get("type") == "CastSpell" and str(a.get("data", {}).get("object_id")) == oid:
                            await c.send_action(a)
                            say("P0 casts Memnite")
                            return True
            if find_bf(state, pid, BOMBARD) is None:
                for oid in hand_ids(state, pid):
                    if oname(state["objects"][oid]) == BOMBARD and untapped_mountains(state, pid) >= 2:
                        for a in acts:
                            if a.get("type") == "CastSpell" and str(a.get("data", {}).get("object_id")) == oid:
                                await c.send_action(a)
                                say("P0 casts Goblin Bombardment")
                                return True
        elif flow.step == "ACTIVATE_BOMBARD":
            bomb = find_bf(state, pid, BOMBARD)
            if bomb is not None and not flow.bombard_active:
                act = next((a for a in acts
                            if a.get("type") == "ActivateAbility"
                            and str((a.get("data") or {}).get("source_id")) == str(bomb)), None)
                if act is None:
                    act = next((a for a in acts if a.get("type") == "ActivateAbility"), None)
                if act is not None:
                    mems = memnites(state, pid)
                    flow.bombard_sac_oid = mems[0]["id"] if mems else None
                    flow.bombard_mem_before = len(mems)
                    flow.bombard_p1_life = life(state, 1)
                    flow.bombard_active = True
                    wire("activate_bombardment", {"action": act})
                    say(f"P0 activates Bombardment (plan sac Memnite {flow.bombard_sac_oid})")
                    await c.send_action(act)
                    return True
        elif flow.step in ("LEG1", "LEG2"):
            cs = flow.cast
            if cs and cs["in_flight"]:
                # announcement/resolution in flight: decisions are answered
                # below; never pass mid-announcement, pass only with the
                # spell on the stack awaiting passes.
                if dargo_stack(state):
                    for a in acts:
                        if a.get("type") == "PassPriority":
                            await c.send_action(a)
                            say(f"P0 passes ({cs['tag']} on stack)")
                            return True
                return False
            if flow.step == "LEG1":
                plan = {"pay_additional": False, "sac_oids": []}
            else:
                mems = memnites(state, pid)
                if len(mems) < 2:
                    say("LEG2: need 2 Memnites for additional cost; aborting")
                    flow.step = "DONE"
                    return False
                plan = {"pay_additional": True, "sac_oids": [m["id"] for m in mems[:2]]}
            did = dargo_hand(state, pid)
            if not did:
                say(f"{flow.step}: no Dargo in hand; aborting")
                flow.step = "DONE"
                return False
            oid = did[0]["id"]
            act = next((a for a in acts
                        if a.get("type") == "CastSpell"
                        and str((a.get("data") or {}).get("object_id")) == str(oid)), None)
            if act is not None:
                flow.legs[flow.step] = {
                    "untapped_before": untapped_mountains(state, pid),
                    "pool_before": pool_len(state, pid),
                }
                flow.cast = {"tag": flow.step, "plan": plan, "in_flight": True,
                             "announced": False, "paid": None, "sac_done": False}
                wire("cast_submit", {"tag": flow.step, "action": act})
                say(f"P0 casts {flow.step} (Dargo oid {oid}); "
                    f"untapped_before={flow.legs[flow.step]['untapped_before']}")
                await c.send_action(act)
                return True
            return False
    # P0 mid-step decisions (not on own main): answer interactions
    if flow.step == "ACTIVATE_BOMBARD" and flow.bombard_active:
        wf = wf_of(state)
        if wf.get("type") == "TargetSelection" and str(wf_player(state)) == str(pid):
            iid, sub = find_player_target(c, 1)
            if iid:
                wire("bombard_target", {"submission": sub})
                await c.send_interaction({"interactionId": iid, "response": sub})
                say("P0 Bombardment targets P1")
                SUBMITTED.add(iid)
                return True
        # sacrifice-cost prompt
        iid, sub, n = find_perm_select(c, [flow.bombard_sac_oid] if flow.bombard_sac_oid else [])
        if iid:
            wire("bombard_sacrifice", {"submission": sub})
            await c.send_interaction({"interactionId": iid, "response": sub})
            say(f"P0 Bombardment sacrifices Memnite (picked {n})")
            SUBMITTED.add(iid)
            return True
    # default: pass priority only when genuinely holding it
    if wf_of(state).get("type") == "Priority" and str(wf_player(state)) == str(pid):
        for a in acts:
            if a.get("type") == "PassPriority":
                await c.send_action(a)
                return True
    return False


async def p1_tick(c, pid, mulls):
    st = c.latest
    if not st:
        return False
    if await do_mulligan(c, pid, "P1", mulls):
        return True
    if await do_bottom(c, pid, "P1", mulls):
        return True
    if await do_discard(c, pid, "P1", mulls):
        return True
    st = c.latest
    state, acts = st["state"], merged_actions(st)
    if await pay_tick(c):
        return True
    if await answer_declare(c, pid, "P1"):
        return True
    if wf_of(state).get("type") == "Priority" and str(wf_player(state)) == str(pid):
        for a in acts:
            if a.get("type") == "PassPriority":
                await c.send_action(a)
                return True
    return False


def setup_gate(state, pid):
    return (len(dargo_hand(state, pid)) >= 2
            and find_bf(state, pid, BOMBARD) is not None
            and len(memnites(state, pid)) >= 4
            and untapped_mountains(state, pid) >= 15
            and pool_len(state, pid) == 0)


async def export_env(c, path):
    s = await c.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    A = obs["assert"]
    flow = Flow()
    mulls = {}

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

    last_rev = {}
    TIMEOUT = 2400
    stop = False
    while time.time() - t0 < TIMEOUT and not stop:
        await asyncio.sleep(0.25)
        for c, pid, is_p0 in ((p0, p0.player_id, True), (p1, p1.player_id, False)):
            watch(c)
            drain_rejections(c)
            if c.revision == last_rev.get(c.name):
                continue
            try:
                if is_p0:
                    acted = await p0_tick(c, pid, flow, mulls)
                else:
                    acted = await p1_tick(c, pid, mulls)
                if acted:
                    last_rev[c.name] = c.revision
            except Exception as e:
                say(f"tick error {c.name}: {e}")
        st = p0.latest
        if not st:
            continue
        record_wf(st["state"])
        state = st["state"]

        if flow.step == "SETUP" and my_main(state, p0.player_id) and setup_gate(state, p0.player_id):
            await export_env(p0, "pre.json")
            flow.step = "ACTIVATE_BOMBARD"
            say("=== stage -> ACTIVATE_BOMBARD ===")
            wire("setup_ready", {})
            continue
        if flow.step == "ACTIVATE_BOMBARD" and flow.bombard_active:
            mems_now = len(memnites(state, p0.player_id))
            p1_life = life(state, 1)
            sac_paid = mems_now < flow.bombard_mem_before
            dmg_done = (flow.bombard_p1_life is not None and p1_life is not None
                        and p1_life < flow.bombard_p1_life)
            if sac_paid and dmg_done:
                flow.bombard_active = False
                flow.step = "LEG1"
                SUBMITTED.clear()
                say("=== bombardment activation done -> LEG1 ===")
                wire("bombard_done", {"p1_life": p1_life, "memnites": mems_now})
                continue
        cs = flow.cast
        if cs and cs["in_flight"]:
            tag = cs["tag"]
            if dargo_stack(state):
                cs["announced"] = True
            wf = wf_of(state)
            wtype = wf.get("type")
            pending_p0 = wtype in ("OptionalCostChoice", "TargetSelection",
                                   "ManaPayment", "ChooseLegend") and str(wf_player(state)) == "0"
            # LEG2's second Dargo goes to the graveyard under the legend rule
            # ("keep first"), so post-resolution dargo_bf is 1, not 2: the
            # resolution signal for LEG2 is the answered legend rule.
            if tag == "LEG2":
                resolved = (not dargo_stack(state) and cs["announced"] and not pending_p0
                            and flow.legend_answered >= 1)
            else:
                resolved = (not dargo_stack(state) and cs["announced"] and not pending_p0
                            and len(dargo_bf(state, p0.player_id)) >= 1)
            if resolved:
                cs["in_flight"] = False
                post = await export_env(p0, f"post_{tag.lower()}.json")
                leg = flow.legs[tag]
                leg["untapped_after"] = untapped_mountains(post, p0.player_id)
                leg["pool_after"] = pool_len(post, p0.player_id)
                leg["spent"] = leg["untapped_before"] - leg["untapped_after"]
                say(f"{tag} resolved: spent={leg['spent']} "
                    f"(before={leg['untapped_before']} after={leg['untapped_after']})")
                wire("cast_resolved", {"tag": tag, "leg": leg})
                SUBMITTED.clear()
                if tag == "LEG1":
                    flow.step = "LEG2"
                    say("=== -> LEG2 ===")
                else:
                    say("=== LEG2 done; finishing ===")
                    stop = True
                continue

    # ------------------------------------------------------- evaluate
    def snap(path):
        env = json.load(open(f"{EVDIR}/{path}"))
        s = env["state"]
        return {
            "untapped": untapped_mountains(s, 0),
            "pool": pool_len(s, 0),
            "dargo_bf": len(dargo_bf(s, 0)),
            "dargo_hand": len(dargo_hand(s, 0)),
            "bombard_bf": find_bf(s, 0, BOMBARD) is not None,
            "memnites_bf": len(memnites(s, 0)),
        }

    try:
        pre = snap("pre.json")
    except FileNotFoundError:
        pre = None
    A["A1_setup_ok"] = ("passed" if pre and pre["dargo_hand"] >= 2 and pre["bombard_bf"]
                        and pre["memnites_bf"] >= 4 and pre["untapped"] >= 15 and pre["pool"] == 0
                        else "failed")
    A["A2_additional_offered"] = "passed" if "OptionalCostChoice" in WF_SEEN else "failed"
    leg1 = flow.legs.get("LEG1", {})
    leg2 = flow.legs.get("LEG2", {})
    A["A3_prior_discount"] = ("passed" if leg1.get("spent") == 5
                              else ("failed" if "spent" in leg1 else "not-run"))
    A["A4_additional_discount"] = ("passed" if leg2.get("spent") == 1
                                   else ("failed" if "spent" in leg2 else "not-run"))
    both_resolved = (leg1.get("spent") is not None and leg2.get("spent") is not None)
    A["A5_cleanup"] = ("passed" if both_resolved and flow.legend_answered >= 1 else "failed")
    obs["notes"].append(f"WF sequence: {WF_SEEN}")
    obs["notes"].append(f"LEG1: {leg1}")
    obs["notes"].append(f"LEG2: {leg2}")
    obs["notes"].append(f"legend_answered={flow.legend_answered}; final step={flow.step}")
    obs["notes"].append("protocol-94 driver (v0.98.0): mulligan as {choice:{type:Keep}} gated on "
                        "pending Declare; bottom/discard via single SelectCards(data.cards); "
                        "ActivateAbility/CastSpell via advertised actions; optional-cost via "
                        "exactChoices role/value; number schema for sac count; sequence schema "
                        "for permanent selection; priority-gated passes; stale-client watchdog.")

    if A["A3_prior_discount"] == "failed" or A["A4_additional_discount"] == "failed":
        verdict = "reproduced"
    elif all(v == "passed" for v in A.values()):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    run = {
        "issue": 6860,
        "run_id": EVID_RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(time.time() - t0, 1),
        "server_identity": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 94, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(f"{BACKFILL}/driver/scenario_6860_0980.py"),
        "decks": {
            "P0": [[DARGO, 8], [MEMNITE, 8], [BOMBARD, 4], [MOUNTAIN, 40]],
            "P1": [[MOUNTAIN, 60]],
        },
        "assertions": A,
        "notes": obs["notes"],
        "wf_sequence": WF_SEEN,
        "legs": flow.legs,
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Dense playsets are a test-harness convenience (engine accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full game replay (scenario_6860_0980.py).",
            "Mana spent is measured as the untapped-Mountain delta (engine auto-taps).",
        ],
        "setup_line": "P0: 8x Dargo, 8x Memnite, 4x Goblin Bombardment, 40x Mountain; P1: 60x Mountain (draw-go)",
        "contract_line": ("LEG1 (decline additional, 1 prior sac): spend 5. "
                          "LEG2 (pay additional with 2 Memnites, 1 prior sac): spend 1."),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "wf_sequence": WF_SEEN, "legs": flow.legs}, f, indent=2)
    await render_summary(run, f"{EVDIR}/summary.png")
    WIRE.close()
    RUNLOG.close()
    await write_manifest()
    print(f"DONE verdict={verdict} assertions={json.dumps(A)}", flush=True)


async def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 760
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #6860 - Dargo, the Shipwrecker cost discounts (revalidation)", fill=(235, 240, 250))
    y += 30
    d.text((24, y), f"server v{si['validated_version']} ({si['build_commit']}) protocol {si['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180))
    y += 28
    d.text((24, y), f"verdict: {run['verdict'].upper()}",
           fill=(255, 90, 90) if run["verdict"] == "reproduced" else
                ((120, 220, 120) if run["verdict"] == "not-reproduced" else (230, 200, 90)))
    y += 34
    d.text((24, y), "Assertions (from saved states + wire log):", fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup_ok": "A1 setup: 2+ Dargo hand, Bombardment + 4 Memnites BF, 15+ untapped Mountains",
        "A2_additional_offered": "A2 optional additional-cost sacrifice offered",
        "A3_prior_discount": "A3 LEG1 (decline, 1 prior sac): mana spent == 5",
        "A4_additional_discount": "A4 LEG2 (pay 2 sacs, 1 prior sac): mana spent == 1",
        "A5_cleanup": "A5 both casts resolved; legend rule answered",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else ((255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y), f"{'pass' if v == 'passed' else ('FAIL' if v == 'failed' else 'n/a')} {lab}", fill=col)
        y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    for n in run["notes"][:8]:
        d.text((40, y), n[:120], fill=(150, 165, 185))
        y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 6860/" + run["run_id"], fill=(120, 130, 150))
    img.save(out_path)


async def write_manifest():
    lines = []
    for name in sorted(os.listdir(EVDIR)):
        if name == "manifest.sha256":
            continue
        p = os.path.join(EVDIR, name)
        if os.path.isfile(p):
            lines.append(f"{sha256_of_file(p)}  {name}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
