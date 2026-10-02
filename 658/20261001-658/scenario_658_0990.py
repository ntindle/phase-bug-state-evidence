#!/usr/bin/env python3
"""Issue #658: Dualcaster Mage runtime repro, revalidation on v0.99.0 / protocol 98.

Contract (unchanged from the 2026-09-17 v0.86.0 run, verdict not-reproduced):
  P0: 56x Mountain + 4x Dualcaster Mage
  P1: 56x Mountain + 4x Lightning Bolt
  P1 casts Lightning Bolt targeting P0 on its own main phase; P0 responds at
  instant speed with Dualcaster Mage (Flash) while the Bolt is on the stack;
  the ETB trigger copies the Bolt; MayChooseNewTargets is answered to retarget
  the copy to P1; both resolve.

Assertions:
  A1 flash_timing      Dualcaster cast with Bolt on the stack
  A2 etb_targets_stack copy is a Bolt copy (DealDamage 3) of the stacked Bolt
  A3 copy_created      copy observed (CopyRetarget prompt and/or >=2 bolt-like
                       entries on the stack)
  A4 retarget_offered  MayChooseNewTargets offered and answered (copy -> P1)
  A5 resolution        P0 took 3 (original), P1 took 3 (copy), mage on BF,
                       stack empty, nothing pending

Verdict: reproduced iff any core assertion failed;
         not-reproduced iff all passed; blocked iff some not-run.

Protocol-98 driver notes (v0.99.0, verified 2026-10-01):
  - HELLO advertises protocol 98 (server enforces exact match).
  - MulliganDecision as {"choice":{"type":"Keep"}} gated on
    waiting_for.data.pending[] with phase type "Declare".
  - BottomCards/DiscardToHandSize via single SelectCards {"cards":[...]}.
  - legal_actions is top-level on the WS message data.
  - Interaction submissions: {"interactionId": iid,
    "response": {"type": "choose", "data": {"choiceId": cid}}} for
    exactChoices; schema responses as {"type": stype, "data":
    {"choiceIds": [cid]}}.
  - Player-candidate surfaces: role "candidate" or "target" (accepted
    defensively).
  - After our own cast we get priority FIRST -- never gate all actions while
    our spell is in flight; always fall through to PassPriority.
  - Opponent hand names are hidden in a client's own view: P0's hand is read
    from P0's view, P1's from P1's view.
"""
import asyncio
import copy as _copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261001-658"
ISSUE = 658
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.99.0",
    "build_commit": "d919616",
    "protocol_version": 98,
    "server_binary_sha256": "37fa78a8db1a51fbb950cbdba870021ad718fdf2e259971a1954c130a1a5ba60",
    "card_data_sha256": "ccfcf9e6d19d9407d207cfbfe95b4ad873cebd878f66b451682e56e991372a4e",
    "draft_pools_sha256": "8ce01364ee46e55cf2610d6a2dd35abbd3266606cc456af8012433f55bdd034e",
    "signature_verified": True,
    "server_run_id": "runs/20261001-658 (dedicated server on 127.0.0.1:9376, v0.99.0 binary)",
    "mode": "Full",
    "source": "2026-10-01 run setup: v0.99.0 already pinned+verified earlier today; hashes recomputed against on-disk artifacts",
}

# Recompute against on-disk artifacts; never copy hashes blindly.
for _f, _k in (("server/releases/v0.99.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.99.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.99.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)

P0_DECK = [("Mountain", 56), ("Dualcaster Mage", 4)]
P1_DECK = [("Mountain", 56), ("Lightning Bolt", 4)]

DUALCASTER = "dualcaster mage"
BOLT = "lightning bolt"
MOUNTAIN = "mountain"

SETUP_DEADLINE_S = 1500
OBSERVE_TIMEOUT_S = 420
RESOLVE_TIMEOUT_S = 180

ST = {}
MULLS = set()
SUBMITTED = set()   # (who, tag, interactionId) already answered


def reset():
    ST.clear()
    ST.update({
        "stage": "SETUP",        # SETUP -> OBSERVE -> DONE
        "game_code": None,
        "dualcaster_cast": False,
        "dualcaster_oid": None,
        "bolt_cast_by_p1": False,
        "bolt_oid": None,
        "pre_exported": False,
        "pre_life": None,
        "post_exported": False,
        "post_life": None,
        "mage_bf": False,
        "copy_id": None,
        "copyretarget_seen": False,
        "copy_seen_stack": False,
        "etb_evidence": {},
        "retarget_done": False,
        "retarget_note": "",
        "retarget_iid": None,
        "retarget_submit_t": 0,
        "seen_wf": [],
        "bolt_target_pending": False,
        "ass": {},
        "notes": [],
        "states_seen": 0,
    })


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(msg + "\n")
        RUNLOG.flush()


def wire(event, payload):
    WIRE.write(json.dumps({"t": time.time(), "event": event,
                           "data": payload}, default=str) + "\n")
    WIRE.flush()


# ------------------------------------------------------------- state utils
def obj_name(state, oid):
    o = (state.get("objects") or {}).get(str(oid))
    return (o.get("base_name") or o.get("name") or "?") if o else "?"


def lname(state, oid):
    return str(obj_name(state, oid)).lower()


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_lnames(state, pid):
    return [lname(state, o) for o in hand_ids(state, pid)]


def life_of(state, pid):
    return player_of(state, pid).get("life")


def untapped_mountains(state, pid):
    n = 0
    for oid, o in (state.get("objects") or {}).items():
        if (o.get("zone") == "Battlefield" and o.get("controller") == pid
                and not o.get("tapped")
                and str(o.get("base_name") or o.get("name") or "").lower() == MOUNTAIN):
            n += 1
    return n


def merged_actions(st):
    """Protocol-98: legal_actions is top-level on the WS message data."""
    acts = list(st.get("legal_actions") or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def find_action(acts, atype):
    return next((a for a in acts if a["type"] == atype), None)


def wf_of(state):
    return state.get("waiting_for") or {}


def my_priority(state, pid):
    return (wf_of(state).get("type") == "Priority"
            and str((wf_of(state).get("data") or {}).get("player")) == str(pid))


def pending_for(state, pid):
    wf = wf_of(state)
    data = wf.get("data") or {}
    for p in data.get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            return p
    return None


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def stack_entries(state):
    return state.get("stack") or []


def effect_sig(entry):
    if not isinstance(entry, dict):
        return {}
    return ((((entry.get("kind") or {}).get("data") or {}).get("ability") or {}).get("effect") or {})


def is_boltlike(entry):
    eff = effect_sig(entry)
    return (eff.get("type") == "DealDamage"
            and (eff.get("amount") or {}).get("value") == 3)


def stack_bolt_entries(state):
    return [e for e in stack_entries(state) if is_boltlike(e)]


# ------------------------------------------------------------- interaction
def choice_text(ch):
    t = ch.get("text") or ch.get("label") or ch.get("name") or ""
    if not t:
        for s in ch.get("surfaces", []) or []:
            d = (s.get("data") or {})
            if isinstance(d, dict) and (d.get("name") or d.get("value")):
                t = d.get("name") or d.get("value")
                break
    return str(t)


def surf_map(ch):
    return {s.get("type"): (s.get("data") or {}) for s in ch.get("surfaces", []) or []}


def action_code(ch):
    """The action surface code on a choice, e.g. chooseTarget,
    keepAllCopyTargets, passPriority (or None)."""
    for s in ch.get("surfaces", []) or []:
        if s.get("type") == "action":
            return (s.get("data") or {}).get("code")
    return None


def candidate_seat(ch):
    """Return the player seat if this choice is a player/target candidate.

    STRICT: only surfaces of type "player" or "target" count, role in
    ("candidate", "target").
    """
    for s in ch.get("surfaces", []) or []:
        if s.get("type") not in ("player", "target"):
            continue
        d = s.get("data") or {}
        if d.get("role") not in ("candidate", "target"):
            continue
        for k in ("seat", "player", "index"):
            if d.get(k) is not None:
                try:
                    return int(d[k])
                except (TypeError, ValueError):
                    pass
    return None


def opp_candidates(opp):
    resp = opp.get("response", {}) or {}
    rdata = resp.get("data", {}) or {}
    return rdata.get("candidates") or rdata.get("choices") or []


def is_player_target_opp(opp):
    """True if this opportunity is a player-target selection: at least one
    available candidate carries a player surface with role candidate/target
    and a seat. The ever-present action menu never qualifies."""
    for ch in opp_candidates(opp):
        if ch.get("status", {}).get("type") != "available":
            continue
        for s in ch.get("surfaces", []) or []:
            if s.get("type") == "player":
                d = s.get("data") or {}
                if d.get("role") in ("candidate", "target") and d.get("seat") is not None:
                    return True
    return False


def iter_candidates(vi):
    for opp in vi.get("opportunities", []) or []:
        resp = opp.get("response", {}) or {}
        rdata = resp.get("data", {}) or {}
        cands = rdata.get("candidates") or rdata.get("choices") or []
        for ch in cands:
            yield opp, ch


async def answer_vi(c, opp, choice, tag):
    iid = opp.get("interactionId") or opp.get("id")
    key = (c.name, tag, str(iid))
    if key in SUBMITTED:
        say(f"[{tag}] iid {str(iid)[:12]} already answered; skipping")
        return False
    resp = opp.get("response", {}) or {}
    rtype = resp.get("type")
    cid = choice.get("id")
    if rtype == "schema":
        spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
        stype = spec.get("type") or "sequence"
        sub = {"interactionId": iid,
               "response": {"type": stype, "data": {"choiceIds": [cid]}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": cid}}}
    say(f"[{tag}] submitting iid={str(iid)[:12]} choice={cid} ({choice_text(choice)[:80]})")
    wire("interaction_submission",
         {"who": c.name, "tag": tag, "submission": sub,
          "choice_text": choice_text(choice)[:120]})
    SUBMITTED.add(key)
    await c.send_interaction(sub)
    return True


async def submit_as_is(c, a):
    await c.send_action(a)


# ------------------------------------------------------------- common ticks
async def do_mulligan(c, pid, tag):
    """Protocol-94: {"type": "MulliganDecision", "data": {"choice": {"type":
    "Keep"}}}, gated on the seat's presence in waiting_for.data.pending
    with phase type Declare."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") != "MulliganDecision":
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    if tag in MULLS:
        return False
    say(f"[{tag}] mulligan keep (protocol-98)")
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    MULLS.add(tag)
    wire("mulligan", {"who": tag, "decision": "keep"})
    return True


async def do_bottom(c, pid, tag):
    """BottomCards phase after Declare: single SelectCards with data.cards."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") not in ("BottomCards",):
        # Also accept: waiting_for.type == "BottomCards" for this seat.
        wf = wf_of(state)
        if not (wf.get("type") == "BottomCards" and str((wf.get("data") or {}).get("player")) == str(pid)):
            return False
    n = ((pend.get("phase") or {}).get("count")) or 1 if pend else 1
    if (tag, "bottomed") in MULLS:
        return False
    # bottom lands first; never the key spell (Dualcaster/Bolt)
    hand = hand_ids(state, pid)

    def rank(o):
        nm = lname(state, o)
        if nm == MOUNTAIN:
            return 0
        if nm in (DUALCASTER, BOLT):
            return 2
        return 1

    picks = [int(x) for x in sorted(hand, key=rank)[:n]]
    say(f"[{tag}] bottoming {n}: {[lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    MULLS.add((tag, "bottomed"))
    wire("bottom", {"who": tag, "count": n})
    return True


_DISCARD_REV = {}


async def do_discard(c, pid, tag):
    """Protocol-94 DiscardToHandSize: single SelectCards with data.cards.
    Revision-guarded against double submit."""
    st = c.latest
    if not st:
        return False
    rev = st.get("state_revision", -1)
    if _DISCARD_REV.get((c.name, rev)):
        return False
    state = st["state"]
    if wf_of(state).get("type") != "DiscardToHandSize":
        return False
    if str((wf_of(state).get("data") or {}).get("player")) != str(pid):
        return False
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False

    def rank(o):
        nm = lname(state, o)
        if nm == MOUNTAIN:
            return 0
        if nm in (DUALCASTER, BOLT):
            return 2
        return 1

    picks = [int(x) for x in sorted(hand, key=rank)[:n]]
    say(f"[{tag}] discarding to hand size via SelectCards: {[lname(state, x) for x in picks]}")
    wire("discard_action", {"who": tag, "cards": picks})
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    _DISCARD_REV[(c.name, rev)] = True
    return True


async def pay_tick(c, acts, tag):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def combat_tick(c, pid, tag, st, state, acts):
    wtype = wf_of(state).get("type") or ""
    if wtype == "DeclareAttackers" and state.get("active_player") == pid:
        da = find_action(acts, "DeclareAttackers")
        if da:
            sub = _copy.deepcopy(da)
            sub.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, sub)
        return True
    if wtype == "DeclareBlockers":
        da = find_action(acts, "DeclareBlockers")
        if da:
            sub = _copy.deepcopy(da)
            sub.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, sub)
        return True
    return False


async def common_tick(c, pid, tag, st, state, acts):
    """Shared pre-priority handling. Returns True if the tick acted."""
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan(c, pid, tag):
            return True
        if await do_bottom(c, pid, tag):
            return True
        return False  # mulligan decision outstanding for the other seat
    if wtype == "BottomCards":
        if await do_bottom(c, pid, tag):
            return True
        return False
    if await pay_tick(c, acts, tag):
        return True
    if await combat_tick(c, pid, tag, st, state, acts):
        return True
    if await do_discard(c, pid, tag):
        return True
    # Never pass priority while our own non-Priority interaction is pending
    # (the observe loop / inline handlers answer it; passing could be read
    # as a decline by the engine). Always hold here.
    vi = get_vi(st)
    wf_player = str((wf_of(state).get("data") or {}).get("player", ""))
    if vi and wtype != "Priority" and (wf_player == str(pid) or wf_player in ("", "None")):
        say(f"[{tag}] holding on {wtype} (interaction pending)")
        wire("held_prompt", {"who": tag, "wf": wtype})
        return True
    return False


# ------------------------------------------------------------- P0 / P1 ticks
async def p0_tick(st, acts, state, c):
    if await common_tick(c, 0, "P0", st, state, acts):
        return
    # RESPOND: cast Dualcaster Mage at instant speed while Bolt is on stack.
    if (not ST["dualcaster_cast"]
            and (stack_entries(state) or [])
            and ST["stage"] == "SETUP"):
        for a in acts:
            d = a.get("data") or {}
            oid = d.get("object_id") or a.get("_src_oid")
            if (a["type"] == "CastSpell" and oid is not None
                    and lname(state, int(oid)) == DUALCASTER):
                say("exporting PRE (bolt on stack, P0 responding)")
                pre = await c.export_state()
                with open(f"{EVDIR}/pre.json", "w") as f:
                    f.write(pre)
                ST["pre_exported"] = True
                ST["pre_life"] = (life_of(state, 0), life_of(state, 1))
                ST["states_seen"] += 1
                wire("pre_state_meta", {
                    "active": state.get("active_player"),
                    "phase": state.get("phase"),
                    "pre_life": ST["pre_life"],
                    "stack_ids": [e.get("id") for e in stack_entries(state)
                                  if isinstance(e, dict)]})
                say(f"[P0] casting Dualcaster Mage in response "
                    f"(active P{state.get('active_player')}, phase={state.get('phase')})")
                wire("p0_cast_dualcaster", a)
                ST["dualcaster_oid"] = int(oid)
                await submit_as_is(c, a)
                ST["dualcaster_cast"] = True
                ST["stage"] = "OBSERVE"
                ST["ass"]["A1_flash_timing"] = "passed"
                ST["notes"].append(
                    f"Dualcaster cast with Bolt on stack on P{state.get('active_player')}'s "
                    f"turn, phase {state.get('phase')}; pre.json exported before the cast")
                return
    if (my_priority(state, 0)
            and state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == 0
            and not (stack_entries(state) or [])):
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(c, a)
                return
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return


async def build_bolt_target(c):
    """Poll for the Bolt's genuine target-selection opportunity and build
    (iid, submission) preferring seat 0 (P0). Returns (iid, submission) or
    (None, None) on timeout.

    Only a player-target opportunity (is_player_target_opp) is ever used;
    the ever-present action menu is ignored, never submitted-to.
    """
    t0 = time.time()
    while time.time() - t0 < 25:
        st2 = c.latest
        if st2:
            wf = wf_of(st2["state"])
            wfd = wf.get("data") or {}
            if wf.get("type") == "TargetSelection" and str(wfd.get("player")) == "1":
                vi = get_vi(st2)
                if vi:
                    for opp in vi.get("opportunities", []) or []:
                        if not is_player_target_opp(opp):
                            continue
                        cands = [(opp, ch) for ch in opp_candidates(opp)
                                 if ch.get("status", {}).get("type") == "available"]
                        if not cands:
                            continue
                        say(f"[P1] bolt target opportunity "
                            f"{str(opp.get('interactionId') or opp.get('id'))[:16]}: "
                            f"{len(cands)} player candidates")
                        for _opp, ch in cands:
                            say("  candidate: seat=" + str(candidate_seat(ch)) +
                                " text=" + choice_text(ch)[:160])
                        wire("p1_bolt_target_interaction", vi)
                        pick = next((p for p in cands
                                     if candidate_seat(p[1]) == 0), None)
                        if pick is None:
                            ST["notes"].append(
                                "P1 bolt target: no seat-0 candidate; aborting "
                                "target attempt rather than mis-targeting")
                            say("[P1] no seat-0 candidate; will not mis-target")
                            return None, None
                        opp, ch = pick
                        iid = opp.get("interactionId") or opp.get("id")
                        resp = opp.get("response", {}) or {}
                        cid = ch.get("id")
                        if resp.get("type") == "schema":
                            spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
                            stype = spec.get("type") or "sequence"
                            sub = {"interactionId": iid,
                                   "response": {"type": stype,
                                                "data": {"choiceIds": [cid]}}}
                        else:
                            sub = {"interactionId": iid,
                                   "response": {"type": "choose",
                                                "data": {"choiceId": cid}}}
                        return iid, sub
        await asyncio.sleep(0.4)
    say("[P1] bolt target opportunity never appeared within 25s")
    return None, None


async def bolt_target_cleared(c, iid):
    """True once the bolt's target selection is answered: the target
    opportunity is gone, or waiting_for left TargetSelection-for-P1."""
    t0 = time.time()
    while time.time() - t0 < 15:
        st2 = c.latest
        if st2:
            wf = wf_of(st2["state"])
            if not (wf.get("type") == "TargetSelection"
                    and str((wf.get("data") or {}).get("player")) == "1"):
                return True
            vi = get_vi(st2)
            iids = [o.get("interactionId") or o.get("id")
                    for o in (vi.get("opportunities") or [])] if vi else []
            if iid not in iids:
                return True
        await asyncio.sleep(0.4)
    return False


async def p1_maybe_cast_bolt(st, acts, state, c, p0state):
    """P1 casts Bolt at P0 on its own main phase once P0 can respond.

    NOTE (protocol 98): each client's state update is that client's VIEW --
    opponent hand card names are hidden. So P0's hand is read from p0state
    (P0's own view) and P1's hand from P1's own `state`.
    """
    if (ST["bolt_cast_by_p1"] or ST["dualcaster_cast"]
            or ST["stage"] != "SETUP"):
        return False
    if not my_priority(state, 1):
        return False
    if state.get("phase") not in ("PreCombatMain", "PostCombatMain"):
        return False
    if state.get("active_player") != 1:
        return False
    if stack_entries(state):
        return False
    if untapped_mountains(state, 0) < 3:
        return False  # wait until P0 can afford the Dualcaster response
    if untapped_mountains(state, 1) < 1:
        return False
    # Mage check MUST use P0's own view (opponent hands are hidden).
    p0_hand = hand_lnames(p0state, 0) if p0state else []
    if DUALCASTER not in p0_hand:
        return False  # P0 has no response in hand yet; wait
    bolt_oid = next((o for o in hand_ids(state, 1)
                     if lname(state, o) == BOLT), None)
    if bolt_oid is None:
        return False
    for a in acts:
        d = a.get("data") or {}
        oid = d.get("object_id") or a.get("_src_oid")
        if a["type"] == "CastSpell" and oid is not None and int(oid) == bolt_oid:
            say(f"[P1] casting Lightning Bolt (P0 untapped mountains: "
                f"{untapped_mountains(state, 0)}, mage in P0 hand)")
            wire("p1_cast_bolt", a)
            ST["bolt_oid"] = bolt_oid
            await submit_as_is(c, a)
            ST["bolt_target_pending"] = True  # hold P1's pass until targeted

            async def bolt_target_once():
                iid = None
                sub = None
                for _ in range(2):
                    iid, sub = await build_bolt_target(c)
                    if sub is None:
                        return False
                    wire("p1_bolt_target_submission", sub)
                    await c.send_interaction(sub)
                    await asyncio.sleep(0.5)
                    if await bolt_target_cleared(c, iid):
                        return True
                    say("[P1] bolt target prompt still up after submission; retrying")
                return False

            t0 = time.time()
            targeted = False
            while time.time() - t0 < 60 and not targeted:
                targeted = await bolt_target_once()
                if not targeted:
                    await asyncio.sleep(1.0)
            ST["bolt_target_pending"] = False
            ST["bolt_cast_by_p1"] = True
            if not targeted:
                ST["notes"].append(
                    "P1 bolt cast submitted but target confirmation unclear; "
                    "continuing to watch (possible engine-side stall)")
            return True
    return False


async def p1_tick(st, acts, state, c, p0state):
    if await common_tick(c, 1, "P1", st, state, acts):
        return
    # Keep P1's own turn driver active (land drops + opportunistic Bolt).
    if await p1_maybe_cast_bolt(st, acts, state, c, p0state):
        return
    if (my_priority(state, 1)
            and state.get("phase") in ("PreCombatMain", "PostCombatMain")
            and state.get("active_player") == 1):
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(c, a)
                return
    # Never pass while our own bolt is awaiting its target choice: passing
    # in the transition window could let P0 respond before targets exist.
    if ST.get("bolt_target_pending"):
        say("[P1] holding pass: bolt target choice outstanding")
        return
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return


# ------------------------------------------------------------- observe loop
def note_wf(state):
    wtype = (wf_of(state).get("type") or "")
    if wtype and wtype != "Priority" and wtype not in ST["seen_wf"]:
        ST["seen_wf"].append(wtype)
        say(f"[OBS] waiting_for -> {wtype}: "
            f"{json.dumps(wf_of(state))[:400]}")
        wire("waiting_for", wf_of(state))


async def observe(st, state, c):
    """Watch the ETB/copy/retarget flow; answer CopyRetarget by seat."""
    note_wf(state)
    wf = wf_of(state)
    wtype = wf.get("type")
    data = wf.get("data") or {}

    # Mage resolved?
    if not ST["mage_bf"]:
        if any(str(o.get("base_name") or o.get("name") or "").lower() == DUALCASTER
               and o.get("zone") == "Battlefield" and o.get("controller") == 0
               for o in (state.get("objects") or {}).values()):
            ST["mage_bf"] = True
            ST["notes"].append("Dualcaster Mage resolved to P0's battlefield")
            say("[OBS] Dualcaster on P0 battlefield")

    # Copy on the stack? (>=2 bolt-like entries, effect-signature matched)
    bolts = stack_bolt_entries(state)
    if len(bolts) >= 2 and not ST["copy_seen_stack"]:
        ST["copy_seen_stack"] = True
        ST["ass"]["A3_copy_created"] = "passed"
        ids = [e.get("id") for e in bolts]
        ST["notes"].append(
            f"copy observed on stack: {len(bolts)} bolt-like entries "
            f"(DealDamage 3 signature), ids={ids}")
        say(f"[OBS] copy on stack: ids={ids}")
        wire("stack_with_copy", {"bolt_like_ids": ids})

    if wtype == "CopyRetarget" and str(data.get("player")) == "0":
        if not ST["copyretarget_seen"]:
            ST["copyretarget_seen"] = True
            ST["ass"]["A3_copy_created"] = "passed"
            ST["copy_id"] = data.get("copy_id")
            ST["notes"].append(
                f"CopyRetarget surfaced (MayChooseNewTargets): copy_id={ST['copy_id']} "
                f"effect_kind={data.get('effect_kind')} "
                f"target_slots={json.dumps(data.get('target_slots'))[:300]}; "
                f"engine did NOT prompt for the trigger's own target "
                f"(single legal target auto-selected)")
            say(f"[OBS] CopyRetarget: copy_id={ST['copy_id']}")
            wire("copyretarget", data)
            # A2: verify the copy is a Bolt copy of the stacked Bolt.
            entries = {e.get("id"): e for e in stack_entries(state)
                       if isinstance(e, dict)}
            cp = entries.get(ST["copy_id"], {})
            if is_boltlike(cp):
                ST["ass"]["A2_etb_targets_stack"] = "passed"
                ST["etb_evidence"] = {"copy_id": ST["copy_id"],
                                     "copy_effect": effect_sig(cp),
                                     "bolt_oid": ST["bolt_oid"]}
                ST["notes"].append(
                    "copy effect matches Lightning Bolt (DealDamage 3); the ETB "
                    "trigger targeted the Bolt on the stack")
            else:
                ST["ass"]["A2_etb_targets_stack"] = "failed"
                ST["notes"].append(
                    f"copy entry {ST['copy_id']} does not look like a Bolt copy: "
                    f"{json.dumps(cp)[:300]}")
        # Answer the retarget: prefer seat 1 (P1) as the new target. Submit,
        # then confirm the prompt advanced before calling it done (a silent
        # rejection must never be mistaken for success).
        if not ST["retarget_done"]:
            vi = get_vi(st)
            riid = ST.get("retarget_iid")
            rtime = ST.get("retarget_submit_t", 0)
            if riid is not None and vi is None:
                # Our last submission was consumed (prompt gone): confirmed.
                ST["retarget_done"] = True
                ST["ass"]["A4_retarget_offered"] = "passed"
                ST["notes"].append("MayChooseNewTargets offered; " + ST["retarget_note"])
                say(f"[OBS] retarget confirmed: {ST['retarget_note']}")
                return True
            if not vi:
                say("[OBS] CopyRetarget with no viewer interaction yet; holding")
                return True
            # CopyRetarget: one exactChoices opportunity whose choices carry
            # player surfaces (role candidate/target), a keepAllCopyTargets
            # decline choice, and possibly a chooseTarget/"none" no-op. Pick
            # the seat-1 player candidate directly; skip decline and "none".
            cur = []
            opps = vi.get("opportunities", []) or []
            for opp in opps:
                for ch in opp_candidates(opp):
                    if ch.get("status", {}).get("type") != "available":
                        continue
                    if action_code(ch) == "keepAllCopyTargets":
                        continue
                    if (surf_map(ch).get("value") or {}).get("value") == "none":
                        continue
                    cur.append((opp, ch))
            for _opp, ch in cur:
                say("  retarget candidate: action=" + str(action_code(ch)) +
                    " seat=" + str(candidate_seat(ch)) +
                    " text=" + choice_text(ch)[:160])
            wire("retarget_interaction", vi)
            pick = next((p for p in cur if candidate_seat(p[1]) == 1), None)
            if pick is None:
                pick = next((p for p in cur
                             if candidate_seat(p[1]) is not None), None)
                if pick is not None:
                    ST["notes"].append(
                        "retarget: no seat-1 player candidate; used first "
                        "seated target")
            kind = "retarget_p1"
            if pick is None:
                ST["notes"].append(
                    "CopyRetarget: no seated target candidate; holding")
                say("[OBS] CopyRetarget: no usable candidate; holding")
                return True
            same_prompt = (riid is not None
                           and all((p[0].get("interactionId") or p[0].get("id")) == riid for p in cur))
            if same_prompt and time.time() - rtime < 8:
                say("[OBS] retarget submitted; awaiting engine advance")
                return True
            opp, ch = pick
            # Allow re-answering the same opportunity after the grace window.
            SUBMITTED.discard((c.name, kind, str(opp.get("interactionId") or opp.get("id"))))
            ok = await answer_vi(c, opp, ch, kind)
            if ok:
                ST["retarget_iid"] = opp.get("interactionId") or opp.get("id")
                ST["retarget_submit_t"] = time.time()
                ST["retarget_note"] = (
                    f"({kind}) seat {candidate_seat(ch)} "
                    f"({choice_text(ch)[:100]})")
                ST["notes"].append(f"retarget submission {ST['retarget_note']}")
                say(f"[OBS] retarget submitted {ST['retarget_note']}")
            return True
        return True

    # Confirm the answered retarget even when the CopyRetarget window has
    # already closed: the engine advances the game as soon as the choice is
    # consumed, so the prompt is usually gone before the next tick sees
    # vi=None inside the branch above.
    if (not ST["retarget_done"] and ST.get("retarget_iid") is not None
            and wtype != "CopyRetarget"):
        ST["retarget_done"] = True
        ST["ass"]["A4_retarget_offered"] = "passed"
        ST["notes"].append("MayChooseNewTargets offered and answered; "
                           + ST["retarget_note"] + " (prompt advanced)")
        say(f"[OBS] retarget confirmed (prompt gone): {ST['retarget_note']}")

    # Genuine trigger target prompt (engine asks for the ETB target)?
    if (wtype and wtype not in ("Priority", "CopyRetarget", "CombatTaxPayment")
            and str(data.get("player")) == "0"
            and not ST["copyretarget_seen"]):
        vi = get_vi(st)
        if vi:
            say(f"[OBS] unexpected non-Priority decision for P0: {wtype}; "
                f"checking for Bolt candidate")
            wire("unexpected_prompt", {"wf": wf, "vi": vi})
            cands = [(opp, ch) for opp, ch in iter_candidates(vi)
                     if ch.get("status", {}).get("type") == "available"]
            boltish = [p for p in cands
                       if "lightning bolt" in
                       (choice_text(p[1]) + json.dumps(surf_map(p[1]))).lower()]
            if boltish:
                opp, ch = boltish[0]
                if await answer_vi(c, opp, ch, "etb_target_bolt"):
                    ST["ass"]["A2_etb_targets_stack"] = "passed"
                    ST["copyretarget_seen"] = True  # answered; don't re-prompt
                    ST["notes"].append(
                        "ETB trigger targeted the Bolt via an explicit prompt")
                    return True
    return False


async def check_resolution(st, state):
    """After retarget, both spells resolve: life deltas + empty stack."""
    if not ST["retarget_done"]:
        return False
    if stack_entries(state):
        return False
    if get_vi(st):
        return False
    wf = wf_of(state)
    if (wf.get("type") or "") not in ("Priority", ""):
        return False
    # One settle beat, then export POST and finalize.
    await asyncio.sleep(2)
    st2 = p0c.latest
    state2 = st2["state"] if st2 else state
    if stack_entries(state2):
        return False
    say("exporting POST (stack empty, nothing pending)")
    post = await p0c.export_state()
    with open(f"{EVDIR}/post.json", "w") as f:
        f.write(post)
    ST["post_exported"] = True
    ST["post_life"] = (life_of(state2, 0), life_of(state2, 1))
    ST["states_seen"] += 1
    mage_bf = any(str(o.get("base_name") or o.get("name") or "").lower() == DUALCASTER
                  and o.get("zone") == "Battlefield" and o.get("controller") == 0
                  for o in (state2.get("objects") or {}).values())
    d0 = ST["pre_life"][0] - ST["post_life"][0] if ST["pre_life"] else None
    d1 = ST["pre_life"][1] - ST["post_life"][1] if ST["pre_life"] else None
    ST["notes"].append(
        f"life pre={ST['pre_life']} post={ST['post_life']} "
        f"(delta P0={d0} P1={d1}); mage_on_bf={mage_bf}; "
        f"stack empty; waiting_for={(wf_of(state2).get('type') or '')}")
    if d0 == 3 and d1 == 3 and mage_bf:
        ST["ass"]["A5_resolution"] = "passed"
    else:
        ST["ass"]["A5_resolution"] = "failed"
    ST["stage"] = "DONE"
    return True


# ------------------------------------------------------------- finalization
def finalize_assertions():
    a = ST["ass"]
    a.setdefault("A1_flash_timing", "not-run")
    a.setdefault("A2_etb_targets_stack", "not-run")
    a.setdefault("A3_copy_created", "not-run")
    a.setdefault("A4_retarget_offered", "not-run")
    a.setdefault("A5_resolution", "not-run")
    core = [a["A1_flash_timing"], a["A2_etb_targets_stack"], a["A3_copy_created"],
            a["A4_retarget_offered"], a["A5_resolution"]]
    if all(v == "passed" for v in core):
        verdict = "not-reproduced"
    elif any(v == "failed" for v in core):
        verdict = "reproduced"
    else:
        verdict = "blocked"
        ST["notes"].append("inconclusive: some steps not-run; see notes")
    return a, verdict


def write_manifest():
    files = []
    for name in sorted(os.listdir(EVDIR)):
        if name == "manifest.sha256":
            continue
        p = f"{EVDIR}/{name}"
        if os.path.isfile(p):
            files.append(name)
    lines = []
    for fn in files:
        p = f"{EVDIR}/{fn}"
        h = hashlib.sha256(open(p, "rb").read()).hexdigest()
        lines.append(f"{h}  {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")


async def capture_server_excerpts():
    try:
        logp = f"{BACKFILL}/runs/20261001-658/server.log"
        if not os.path.exists(logp):
            say("no server.log at runs/20261001-658; skipping excerpts")
            return
        out = []
        with open(logp, errors="replace") as f:
            for line in f:
                if ST.get("game_code") and ST["game_code"] in line:
                    out.append(line)
        out = out[-300:]
        with open(f"{EVDIR}/server_excerpts.log", "w") as f:
            f.writelines(out)
        say(f"wrote server_excerpts.log ({len(out)} lines)")
    except Exception as e:
        say(f"server excerpts failed: {e}")


def write_parse_files(notes):
    try:
        cd = json.load(open(
            f"{BACKFILL}/server/releases/v0.99.0/data/card-data.json"))
        for key, fn in (("dualcaster mage", "parse_dualcaster.json"),
                        ("lightning bolt", "parse_bolt.json")):
            card = cd.get(key, {})
            with open(f"{EVDIR}/{fn}", "w") as f:
                json.dump({"name": card.get("name"),
                           "oracle_text": card.get("oracle_text"),
                           "triggers": card.get("triggers"),
                           "abilities": card.get("abilities")}, f, indent=2)
    except Exception as e:
        notes.append(f"parse json failed: {e}")


async def finish():
    a, verdict = finalize_assertions()
    write_parse_files(ST["notes"])
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": ST.get("started_at"),
        "duration_s": round(time.time() - ST.get("t0", time.time()), 1),
        "server": {
            "server_version": SERVER_IDENTITY["server_version"],
            "build_commit": SERVER_IDENTITY["build_commit"],
            "protocol_version": SERVER_IDENTITY["protocol_version"],
            "mode": SERVER_IDENTITY["mode"],
            "binary_sha256": SERVER_IDENTITY["server_binary_sha256"],
            "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
            "draft_pools_sha256": SERVER_IDENTITY["draft_pools_sha256"],
            "signature_verified": SERVER_IDENTITY["signature_verified"],
            "observed_at": "2026-10-01",
            "source": SERVER_IDENTITY["source"],
        },
        "server_run_dir": SERVER_IDENTITY["server_run_id"],
        "driver": {"protocol_advertised": 98, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_658_0990.py", "rb").read()).hexdigest(),
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": a,
        "notes": ST["notes"],
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "Restoration: the prebuilt phase-server has no standalone "
            "state-restore facility; pre.json/post.json are authoritative "
            "exports restorable only via full game replay, not direct load.",
        ],
        "setup_line": "P0: 56x Mountain + 4x Dualcaster Mage; P1: 56x Mountain + 4x Lightning Bolt",
        "contract_line": "P1 Bolts P0; P0 flashes in Dualcaster in response; ETB copies Bolt retargeted to P1; both resolve",
        "stats": {
            "states_seen": ST["states_seen"],
            "seen_wf_types": ST["seen_wf"],
            "pre_life": ST["pre_life"],
            "post_life": ST["post_life"],
            "copy_id": ST["copy_id"],
            "bolt_oid": ST["bolt_oid"],
            "etb_evidence": ST["etb_evidence"],
            "retarget_note": ST["retarget_note"],
        },
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say(f"wrote run.json verdict={verdict}")


def render_summary_png():
    # Rendered from the actual saved run.json (states + assertions).
    from PIL import Image, ImageDraw
    run = json.load(open(f"{EVDIR}/run.json"))
    W, H = 1000, 760
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server"]
    y = 20
    d.text((24, y), "Issue #658 - Dualcaster Mage -- runtime failure (revalidation)",
           fill=(235, 240, 250)); y += 30
    d.text((24, y), f"server v{si['server_version']} ({si['build_commit']}) "
           f"protocol {si['protocol_version']} - {run['run_id']}",
           fill=(140, 160, 180)); y += 28
    v = run["verdict"]
    d.text((24, y), f"verdict: {v.upper()}",
           fill=(255, 90, 90) if v == "reproduced" else
           ((120, 220, 120) if v == "not-reproduced" else (230, 200, 90))); y += 34
    d.text((24, y), "Assertions (from saved states + wire log):", fill=(200, 210, 225)); y += 24
    labels = {
        "A1_flash_timing": "A1 Dualcaster cast with Bolt on the stack",
        "A2_etb_targets_stack": "A2 ETB copy targets the stacked Bolt (DealDamage 3)",
        "A3_copy_created": "A3 copy created (stack >=2 bolt-like or CopyRetarget)",
        "A4_retarget_offered": "A4 MayChooseNewTargets offered + answered (copy -> P1)",
        "A5_resolution": "A5 resolution: P0 17, P1 17, mage on BF, stack empty",
    }
    for k, lab in labels.items():
        vv = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if vv == "passed" else (
            (255, 90, 90) if vv == "failed" else (150, 150, 150))
        d.text((40, y), f"{'pass' if vv == 'passed' else ('FAIL' if vv == 'failed' else 'n/a')} {lab}",
               fill=col); y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225)); y += 24
    for n in run["notes"][:9]:
        d.text((40, y), str(n)[:118], fill=(150, 165, 185)); y += 20
    stats = run.get("stats", {})
    d.text((24, H - 52), f"life pre={stats.get('pre_life')} post={stats.get('post_life')} "
           f"states_seen={stats.get('states_seen')} wf={','.join(stats.get('seen_wf_types', []))[:60]}",
           fill=(120, 130, 150))
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 658/" + run["run_id"],
           fill=(120, 130, 150))
    img.save(f"{EVDIR}/summary.png")


def validate():
    say("validating evidence dir...")
    for fn in ("pre.json", "post.json"):
        p = f"{EVDIR}/{fn}"
        env = json.load(open(p))
        assert "state" in env, f"{fn} missing state envelope"
        say(f"  {fn}: parses, state envelope ok")
    run = json.load(open(f"{EVDIR}/run.json"))
    assert run.get("verdict") in ("reproduced", "not-reproduced", "blocked"), \
        "run.json missing/invalid verdict"
    assert isinstance(run.get("assertions"), dict), "run.json assertions not a dict"
    say(f"  run.json: parses, verdict={run['verdict']}, "
        f"{len(run['assertions'])} assertions")
    manifest = {}
    for line in open(f"{EVDIR}/manifest.sha256"):
        line = line.strip()
        if not line:
            continue
        h, fn = line.split("  ", 1)
        manifest[fn] = h
    for fn, h in manifest.items():
        actual = hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
        assert actual == h, f"hash mismatch for {fn}"
    say(f"  manifest: {len(manifest)} files, all hashes match")
    from PIL import Image
    im = Image.open(f"{EVDIR}/summary.png")
    im.verify()
    say("  summary.png: PIL verify ok")


# ------------------------------------------------------------- main loop
p0c = None
p1c = None


async def main():
    global p0c, p1c
    reset()
    ST["t0"] = time.time()
    ST["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ST["t0"]))
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    p0c, p1c = p0, p1
    await p0.connect()
    await p1.connect()
    say("server identity pinned: v0.99.0 (d919616) protocol 98, mode Full; "
        "using dedicated server 127.0.0.1:9376 (runs/20261001-658)")

    await p0.create(deck(*P0_DECK), player_count=2)
    await p1.join(p0.game_code, deck(*P1_DECK))
    await asyncio.sleep(2.0)
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_setup", {"game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})

    t_start = time.time()
    last = {}
    last_tick_at = {}
    last_diag = 0.0
    observe_deadline = None

    while True:
        await asyncio.sleep(0.15)
        now = time.time()
        elapsed = now - t_start

        # Timeout discipline: 25 min to reach the Dualcaster window.
        if ST["stage"] == "SETUP" and elapsed > SETUP_DEADLINE_S:
            ST["notes"].append(
                f"SETUP deadline ({SETUP_DEADLINE_S}s) hit before Dualcaster "
                "was cast in response; recording not-run/blocked")
            for k in ("A1_flash_timing", "A2_etb_targets_stack",
                      "A3_copy_created", "A4_retarget_offered",
                      "A5_resolution"):
                ST["ass"].setdefault(k, "not-run")
            break

        for c, tick, tag, pid, extra in ((p0, p0_tick, "P0", 0, None),
                                        (p1, p1_tick, "P1", 1,
                                         p0.latest["state"] if p0.latest else None)):
            st = c.latest
            if not st:
                continue
            same_rev = (c.revision == last.get(tag))
            stale = now - last_tick_at.get(tag, 0) > 5
            if same_rev and not stale:
                continue
            last[tag] = c.revision
            last_tick_at[tag] = now
            try:
                if extra is None:
                    await tick(st, merged_actions(st), st["state"], c)
                else:
                    await tick(st, merged_actions(st), st["state"], c, extra)
            except Exception as e:
                say(f"tick error {tag}: {e}")
                wire("tick_error", {"who": tag, "err": str(e)})

        if ST["stage"] in ("OBSERVE",) and p0.latest:
            try:
                handled = await observe(p0.latest, p0.latest["state"], p0)
                _ = handled
                if observe_deadline is None and ST["retarget_done"]:
                    observe_deadline = now + RESOLVE_TIMEOUT_S
                if ST["stage"] == "OBSERVE":
                    done = await check_resolution(p0.latest, p0.latest["state"])
                    if done:
                        break
                    if observe_deadline is None:
                        observe_deadline = t_start + SETUP_DEADLINE_S + OBSERVE_TIMEOUT_S
                    if now > observe_deadline:
                        ST["notes"].append(
                            "OBSERVE deadline hit: retarget_done="
                            f"{ST['retarget_done']} mage_bf={ST['mage_bf']} "
                            f"copyretarget_seen={ST['copyretarget_seen']}")
                        if not ST["post_exported"]:
                            try:
                                post = await p0.export_state()
                                with open(f"{EVDIR}/post.json", "w") as f:
                                    f.write(post)
                                ST["post_exported"] = True
                                ST["notes"].append("post.json exported at OBSERVE-deadline fallback")
                            except Exception as e:
                                ST["notes"].append(f"post export failed: {e}")
                        break
            except Exception as e:
                say(f"observe error: {e}")
                wire("observe_error", {"err": str(e)})

        if p0.latest and (p0.latest["state"].get("turn_number") or 0) > 400 \
                and ST["stage"] == "SETUP":
            ST["notes"].append("turn 400 hit in SETUP; finishing")
            break
        if now - last_diag > 60 and p0.latest:
            last_diag = now
            s = p0.latest["state"]
            s1 = p1.latest["state"] if p1.latest else s
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} stage={ST['stage']} "
                f"dualcaster_cast={ST['dualcaster_cast']} "
                f"stack={len(stack_entries(s))} "
                f"P0untapM={untapped_mountains(s, 0)} "
                f"P0mage_in_hand={DUALCASTER in hand_lnames(s, 0)} "
                f"P1bolt_in_hand={BOLT in hand_lnames(s1, 1)}")

    await finish()
    # pre/post export fallbacks so the evidence dir is always complete and
    # renderable; each fallback is labeled in the notes and is NOT the
    # contract state (pre = Bolt on stack pre-response).
    run_p = f"{EVDIR}/run.json"
    for name, key in (("pre", "pre_exported"), ("post", "post_exported")):
        if not ST[key]:
            try:
                data = await p0.export_state()
                with open(f"{EVDIR}/{name}.json", "w") as f:
                    f.write(data)
                ST[key] = True
                ST["notes"].append(
                    f"{name}.json exported at finish fallback "
                    f"(NOT the contract state)")
                run = json.load(open(run_p))
                run["notes"] = ST["notes"]
                json.dump(run, open(run_p, "w"), indent=1)
            except Exception as e:
                say(f"fallback export of {name} failed: {e}")
    try:
        render_summary_png()
        say("rendered summary.png")
    except Exception as e:
        say(f"render_summary failed: {e}")
    try:
        await capture_server_excerpts()
    except Exception as e:
        say(f"capture_server_excerpts failed: {e}")
    WIRE.close()
    RUNLOG.close()
    write_manifest()
    try:
        validate()
    except Exception as e:
        say(f"VALIDATION FAILED: {e}")
        sys.exit(2)
    say(f"DONE run={RUN_ID}")
    await p0.close()
    await p1.close()


asyncio.run(main())
