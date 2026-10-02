#!/usr/bin/env python3
"""Issue #7359: Jeleva, Nephalia's Scourge lets the controller cast ALL
instants/sorceries exiled with her, instead of exactly one per attack trigger.

First backfill run for this issue, on v0.99.0 / protocol 98.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (discord 2026-08-13, classifier: supported_aspect_defect,
status:confirmed): "supposed to be able to play one instant/sorcery card
able to play all instant/sorcery cards".

Oracle text (pinned v0.99.0 card-data.json, "jeleva, nephalia's scourge"):
  "Flying. When Jeleva enters, each player exiles the top X cards of their
   library, where X is the amount of mana spent to cast Jeleva. Whenever
   Jeleva attacks, you may cast an instant or sorcery spell from among cards
   exiled with Jeleva without paying its mana cost."

Parse state (verified 2026-10-02 against pinned v0.99.0 card-data.json):
  The Attacks trigger lowers to CastFromZone {
    target: And[Typed(Instant|Sorcery, InZone Exile), ExiledBySource],
    without_paying_mana_cost: true, mode: Cast } with NO count bound,
  optional: true. Matches the triage analysis exactly: nothing limits the
  trigger to a single cast.

Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 8x Jeleva, Nephalia's Scourge ({1}{U}{B}{R} 1/3 flying)
      + 36x Lightning Bolt ({R} instant, "deals 3 to any target")
      + 6x Island + 5x Swamp + 5x Mountain.
  P1: 60x Island dummy (exiles only lands; never blocks).

Drive:
  P0 mulligans (max 2) unless the opener holds Jeleva + 4 lands covering
  U/B/R. P0 ramps, casts Jeleva (X=4 mana spent -> each player exiles top 4),
  then attacks P1 with her on a later turn (summoning sickness; P1 has no
  blockers). The Attacks trigger fires; the driver ACCEPTS the optional
  cast, chooses exactly ONE exiled Lightning Bolt, targets P1, and lets it
  resolve (P1 -> 17, no mana paid). The driver then keeps playing and watches
  for any SECOND free-cast offer attributable to the same trigger (same
  OptionalEffectChoice re-offer, another exile card-choice, or a cast action
  sourced from Exile) through the end of P0's NEXT turn. P0 never attacks a
  second time, so any further exile-cast belongs to trigger #1.
  The driver never casts from hand: every Bolt on the stack is a free
  exile-cast by construction.

Assertions:
  A1_setup        pre.json (trigger on stack, before the optional choice):
                  Jeleva on P0 BF, >=2 instants/sorceries in Exile
                  (every Exile-zone card in this game was exiled by Jeleva).
  A2_first_cast   the first free cast completes: an exiled Bolt resolves
                  from exile, P1 takes 3 (20->17), no mana demanded for it.
  A3_single_bound NO second free cast is offered/completed for the remaining
                  exiled spells within the observation window (through end of
                  P0's next turn). FAIL = the reported bug (>=2 free casts
                  from one attack trigger).
  A4_exile_remainder
                  post.json: exiled-but-uncast instants/sorceries remain in
                  Exile (not returned to library, not vanished).

Verdict rule: reproduced iff A1 passed and A3 failed (>=2 free casts from
              one trigger - the reported outcome);
              not-reproduced iff A1..A4 all passed;
              blocked iff A1 failed, or A2 failed (the bound test is moot
              when the first cast cannot complete).

Protocol-98 driver notes (v0.99.0, verified 2026-10-01/02):
  - HELLO advertises protocol 98 (server enforces exact match).
  - MulliganDecision as {"choice":{"type":"Keep"}} gated on
    waiting_for.data.pending[] with phase type "Declare".
  - BottomCards/DiscardToHandSize via single SelectCards {"cards":[...]}.
  - legal_actions is top-level on the WS message data.
  - DeclareAttackers data: {"attacks": [[oid, {"type":"Player","data":seat}]],
    "bands": []}.
  - Optional effects: accept/decline from decideOptionalEffect surfaces
    role/value (exactChoices response). Card choices: exactChoices or
    schema/sequence with candidate ids. Target selection: advertised schema
    sequence response.
  - TOCTOU lesson (7358): never close an observation window on a
    synchronously-set local flag while the client view lags -- gate on
    actually observing the event in the client view.
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
RUN_ID = "20261002-7359"
ISSUE = 7359
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

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
    "server_run_id": "runs/20261002-7359 (live v0.99.0 server on 127.0.0.1:9374, "
                     "reused per task body: already listening with pinned release)",
    "mode": "Full",
    "source": "2026-10-02: latest stable release v0.99.0 == pinned release dir; "
              "hashes recomputed against on-disk artifacts",
}

# Recompute against on-disk artifacts; never copy hashes blindly.
for _f, _k in (("server/releases/v0.99.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.99.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.99.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)

JELEVA = "jeleva, nephalia's scourge"
BOLT = "lightning bolt"
LANDS = ("island", "swamp", "mountain")

P0_DECK = [("Jeleva, Nephalia's Scourge", 8), ("Lightning Bolt", 36),
           ("Island", 6), ("Swamp", 5), ("Mountain", 5)]
P1_DECK = [("Island", 60)]

STARTING_LIFE = 20
SETUP_DEADLINE_S = 1500

ST = {}
MULLS = set()
MULL_COUNT = {}
SUBMITTED = set()


def reset():
    ST.clear()
    ST.update({
        "t0": time.time(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "game_code": None,
        "jeleva_cast": False,
        "etb_done": False,
        "etb_exported": False,
        "attack_done": False,
        "attack_turn": None,
        "pre_exported": False,
        "trigger_seen": False,
        "trigger_entry": None,
        "mid_exported": False,
        "optional_accepts": 0,
        "card_choices_made": 0,
        "targets_submitted": 0,
        "free_casts": 0,          # exiled Bolts that resolved (Exile->GY via stack)
        "cast_oids": [],
        "exiled_oids": set(),    # every oid ever observed in Exile this game
        "mana_paid_during_free_cast": 0,
        "in_free_cast_window": False,
        "first_cast_turn": None,
        "saw_bolt_on_stack": False,
        "post_exported": False,
        "pre_exiled_spells": [],
        "pre_life_p1": None,
        "pre_untapped_lands": None,
        "ass": {k: "not-run" for k in ("A1_setup", "A2_first_cast",
                                       "A3_single_bound", "A4_exile_remainder")},
        "notes": [],
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


def lib_size(state, pid):
    return len(player_of(state, pid).get("library", []))


def bf_ids(state, pid, key):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and str(o.get("base_name") or o.get("name") or "").lower() == key]


def bf_id(state, pid, key):
    ids = bf_ids(state, pid, key)
    return ids[0] if ids else None


def untapped_lands(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and not o.get("tapped")
            and str(o.get("base_name") or o.get("name") or "").lower() in LANDS]


def exiled_spells(state):
    """Instant/sorcery objects in Exile. In this game every Exile-zone card
    was exiled by Jeleva's ETB (nothing else exiles)."""
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") != "Exile":
            continue
        ct = o.get("card_types") or {}
        core = [str(t).lower() for t in (ct.get("core_types") or [])]
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if "instant" in core or "sorcery" in core:
            out.append((int(oid), nm))
    return out


def merged_actions(st):
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


def jeleva_trigger_entry(state):
    for e in stack_entries(state):
        if not isinstance(e, dict):
            continue
        low = json.dumps(e, default=str).lower()
        if "jeleva" in low and "triggered" in low:
            return e
    return None


def can_attack_now(state, oid):
    o = get_obj(state, oid)
    if o.get("tapped"):
        return False
    if o.get("summoning_sick"):
        kws = o.get("keywords") or []
        if "Haste" not in [str(k) if not isinstance(k, dict)
                           else k.get("name", "") for k in kws]:
            return False
    return True


def cast_action_for(acts, state, key):
    for a in acts:
        if "cast" in a["type"].lower():
            d = a.get("data", {})
            cands = list(d.values()) + [a.get("_src_oid")]
            for v in cands:
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    continue
                if lname(state, iv) == key:
                    return a
    return None


def exile_cast_action(acts, state):
    """A cast action whose source object sits in Exile (the free-cast path).
    Never matches hand casts."""
    for a in acts:
        if "cast" in a["type"].lower():
            d = a.get("data", {})
            cands = list(d.values()) + [a.get("_src_oid")]
            for v in cands:
                try:
                    iv = int(v)
                except (TypeError, ValueError):
                    continue
                o = get_obj(state, iv)
                if o.get("zone") == "Exile":
                    return a, iv
    return None, None


# ------------------------------------------------------------- interaction
async def submit_as_is(c, a):
    await c.send_action(a)


def submit_interaction(c, iid, choice_id, resp_kind):
    if resp_kind == "exactChoices":
        sub = {"interactionId": iid,
               "response": {"type": "choose", "data": {"choiceId": choice_id}}}
    else:
        sub = {"interactionId": iid,
               "response": {"type": "sequence",
                            "data": {"choiceIds": [choice_id]}}}
    return sub


async def answer_vi(c, opp, choice, tag):
    iid = opp.get("interactionId") or opp.get("id")
    key = (c.name, tag, str(iid), str(choice.get("id")))
    if key in SUBMITTED:
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
    SUBMITTED.add(key)
    say(f"[{c.name}] interaction {tag}: iid={iid} choice={cid} ({rtype})")
    wire(f"interaction_{tag}", sub)
    await c.send_interaction(sub)
    return True


def find_optional_accept(vi):
    """decideOptionalEffect opportunity -> (iid, accept_choice, resp_kind)."""
    for opp in vi.get("opportunities", []) or []:
        resp = opp.get("response", {}) or {}
        if resp.get("type") != "exactChoices":
            continue
        for ch in resp.get("data", {}).get("choices", []) or []:
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []]
            if "decideOptionalEffect" not in codes:
                continue
            is_accept = None
            for sf in ch.get("surfaces", []) or []:
                dd = sf.get("data", {}) or {}
                if dd.get("role") == "accept":
                    is_accept = str(dd.get("value")).lower() == "true"
            if is_accept is None:
                txt = " ".join(
                    str((s.get("data", {}) or {}).get("text", ""))
                    for s in ch.get("surfaces", []) or []).lower()
                if "cast" in txt or "yes" in txt:
                    is_accept = True
                elif "decline" in txt or "no" in txt:
                    is_accept = False
            if is_accept:
                return (opp.get("interactionId") or opp.get("id"),
                        ch, resp.get("type"))
    return None, None, None


def find_exile_card_choice(vi, state):
    """Card-choice opportunity whose candidates include an Exile-zone
    instant/sorcery -> (iid, choice_id, resp_kind, card_name)."""
    ex_ids = {oid for oid, _nm in exiled_spells(state)}
    if not ex_ids:
        return None, None, None, None
    for opp in vi.get("opportunities", []) or []:
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        cands = list(data.get("candidates", []) or []) \
            + list(data.get("choices", []) or [])
        for ch in cands:
            cid = ch.get("id")
            try:
                ioid = int(cid)
            except (TypeError, ValueError):
                continue
            if ioid in ex_ids:
                # skip the decideOptionalEffect accept/decline pseudo-choices
                codes = [s.get("data", {}).get("code")
                         for s in ch.get("surfaces", []) or []]
                if "decideOptionalEffect" in codes:
                    continue
                return (opp.get("interactionId") or opp.get("id"), cid,
                        resp.get("type"), lname(state, ioid))
    return None, None, None, None


def find_target_p1(vi):
    """Target-selection opportunity -> (iid, target_id, resp_kind) preferring
    the opponent player (seat 1)."""
    for opp in vi.get("opportunities", []) or []:
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        cands = list(data.get("candidates", []) or []) \
            + list(data.get("choices", []) or [])
        best = None
        for ch in cands:
            for sf in ch.get("surfaces", []) or []:
                dd = sf.get("data", {}) or {}
                if dd.get("role") in ("candidate", "target") \
                        and str(dd.get("seat")) == "1":
                    best = ch.get("id")
        if best is None and len(cands) == 1:
            best = cands[0].get("id")
        if best is not None:
            return (opp.get("interactionId") or opp.get("id"), best,
                    resp.get("type"))
    return None, None, None


async def vi_pass_fallback(c, st):
    vi = get_vi(st)
    if vi:
        for opp in vi.get("opportunities", []) or []:
            data = (opp.get("response", {}) or {}).get("data", {}) or {}
            for ch in data.get("choices") or []:
                codes = [s.get("data", {}).get("code")
                         for s in ch.get("surfaces", []) or []]
                if "passPriority" in codes and ch.get("status", {}) \
                        .get("type") == "available":
                    await answer_vi(c, opp, ch, "pass")
                    return True
    return False


# ------------------------------------------------------------- common ticks
def p0_keepable(hand):
    if JELEVA not in hand:
        return False
    lands = [n for n in hand if n in LANDS]
    return len(lands) >= 4 and all(c in lands for c in LANDS)


async def do_mulligan_p0(c, pid, tag):
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
    hn = hand_lnames(state, pid)
    mkey = (tag, "mulligan")
    mulls = MULL_COUNT.get(mkey, 0)
    if not p0_keepable(hn) and len(hn) > 5 and mulls < 2:
        MULL_COUNT[mkey] = mulls + 1
        say(f"[P0] mulligan #{mulls + 1} (hand not keepable: "
            f"jeleva={JELEVA in hn})")
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
        wire("mulligan", {"who": tag, "decision": "mulligan"})
    else:
        MULLS.add(tag)
        say(f"[P0] keep {len(hn)} (jeleva={'yes' if JELEVA in hn else 'no'})")
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Keep"}}})
        wire("mulligan", {"who": tag, "decision": "keep"})
    return True


async def do_mulligan_keep(c, pid, tag):
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
    MULLS.add(tag)
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    wire("mulligan", {"who": tag, "decision": "keep"})
    return True


def discard_rank(state, o):
    nm = lname(state, o)
    if nm == BOLT:
        return 0
    if nm == JELEVA:
        return 1
    if nm in LANDS:
        return 2
    return 0


async def do_bottom(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") not in ("BottomCards",):
        wf = wf_of(state)
        if not (wf.get("type") == "BottomCards"
                and str((wf.get("data") or {}).get("player")) == str(pid)):
            return False
    n = ((pend.get("phase") or {}).get("count")) or 1 if pend else 1
    if (tag, "bottomed") in MULLS:
        return False
    hand = hand_ids(state, pid)
    picks = [int(x) for x in sorted(hand, key=lambda o: discard_rank(state, o))[:n]]
    say(f"[{tag}] bottoming {n}: {[lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    MULLS.add((tag, "bottomed"))
    wire("bottom", {"who": tag, "count": n})
    return True


_DISCARD_REV = {}


async def do_discard(c, pid, tag):
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
    picks = [int(x) for x in sorted(hand, key=lambda o: discard_rank(state, o))[:n]]
    say(f"[{tag}] discarding to hand size: {[lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    _DISCARD_REV[(c.name, rev)] = True
    return True


async def pay_tick(c, acts, tag):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            if ST["in_free_cast_window"]:
                ST["mana_paid_during_free_cast"] += 1
                say(f"[{tag}] NOTE: mana payment demanded DURING free cast "
                    f"(contradicts 'without paying')")
                wire("mana_during_free_cast", {"action": a})
            await submit_as_is(c, a)
            return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return await vi_pass_fallback(c, st)


# ------------------------------------------------------------- P0 trigger flow
async def handle_trigger_flow(c, st, state):
    """Drive Jeleva's attack-trigger UI. Returns True if it acted.
    Accepts the optional cast, picks exactly ONE exiled Bolt per card-choice,
    targets P1. Any exile-cast offer after the first completed cast belongs
    to the same trigger (P0 never attacks twice) and is likewise taken --
    the second completed cast is the reported bug.

    MECHANISM (attempt 1, 2026-10-02): the engine's CastFromZone grants the
    free cast as a PERMISSION -- a cast legal action sourced from an
    Exile-zone object -- not (only) a viewer-interaction card choice. The
    permission does not stall the game, so it must be checked on EVERY tick
    regardless of viewer_interaction state, before the vi branches."""
    # 0) exile-cast permission as a plain legal action: check FIRST,
    #    regardless of viewer_interaction (the priority menu may be up).
    acts = merged_actions(st)
    a, oid = exile_cast_action(acts, state)
    if a is not None:
        key = (c.name, "exile_cast_action", str(oid))
        if key not in SUBMITTED:
            SUBMITTED.add(key)
            ST["in_free_cast_window"] = True
            say(f"[P0] submitting exile-cast permission for "
                f"{lname(state, oid)} (oid {oid}; "
                f"free_casts_so_far={ST['free_casts']})")
            wire("exile_cast_action", {"action": a, "oid": oid,
                                       "free_casts_so_far": ST["free_casts"]})
            await submit_as_is(c, a)
            return True
        # already submitted (or rejected and still advertised): fall through
        # to the vi branches rather than stalling them.
    vi = get_vi(st)
    if not vi:
        return False
    wtype = wf_of(state).get("type") or ""
    # 1) optional accept/decline
    iid, ch, rtype = find_optional_accept(vi)
    if ch is not None:
        key = (c.name, "optional_accept", str(iid))
        if key not in SUBMITTED:
            SUBMITTED.add(key)
            ST["optional_accepts"] += 1
            ST["in_free_cast_window"] = True
            if ST["pre_untapped_lands"] is None:
                ST["pre_untapped_lands"] = len(untapped_lands(state, 0))
            say(f"[P0] ACCEPT #{ST['optional_accepts']} Jeleva optional cast "
                f"(free_casts_so_far={ST['free_casts']})")
            wire("optional_accept", {"iid": iid,
                                     "n": ST["optional_accepts"],
                                     "free_casts_so_far": ST["free_casts"]})
            await c.send_interaction(submit_interaction(c, iid, ch["id"], rtype))
            return True
        return False
    # 2) choose which exiled card to cast -- exactly one per prompt
    iid, cid, rtype, cname = find_exile_card_choice(vi, state)
    if cid is not None:
        key = (c.name, "card_choice", str(iid), str(cid))
        if key not in SUBMITTED:
            SUBMITTED.add(key)
            ST["card_choices_made"] += 1
            say(f"[P0] card choice #{ST['card_choices_made']}: casting exiled "
                f"{cname} (free_casts_so_far={ST['free_casts']})")
            wire("card_choice", {"iid": iid, "choice": cid, "card": cname,
                                 "n": ST["card_choices_made"],
                                 "free_casts_so_far": ST["free_casts"]})
            await c.send_interaction(submit_interaction(c, iid, cid, rtype))
            return True
        return False
    # 3) target selection for the free Bolt -> P1
    if "TargetSelection" in wtype:
        iid, tid, rtype = find_target_p1(vi)
        if tid is not None:
            key = (c.name, "target", str(iid), str(tid))
            if key not in SUBMITTED:
                SUBMITTED.add(key)
                ST["targets_submitted"] += 1
                say(f"[P0] targeting P1 (seat 1) for free Bolt "
                    f"(#{ST['targets_submitted']})")
                wire("target_submit", {"iid": iid, "target": tid})
                await c.send_interaction(
                    submit_interaction(c, iid, tid, rtype))
                return True
    return False


# ------------------------------------------------------------- P0 tick
async def p0_tick(st, acts, state, c):
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan_p0(c, 0, "P0"):
            return
        if await do_bottom(c, 0, "P0"):
            return
        return
    if wtype == "BottomCards":
        if await do_bottom(c, 0, "P0"):
            return
        return
    if await pay_tick(c, acts, "P0"):
        return
    if await do_discard(c, 0, "P0"):
        return
    if wtype == "DeclareBlockers":
        db = find_action(acts, "DeclareBlockers")
        if db:
            d = _copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return
    # trigger UI takes precedence over everything else once attacking began
    if ST["attack_done"]:
        if await handle_trigger_flow(c, st, state):
            return
    # ---- scripted attack with Jeleva (once) ----
    if wtype == "DeclareAttackers" and state.get("active_player") == 0:
        da = find_action(acts, "DeclareAttackers")
        if da:
            d = _copy.deepcopy(da)
            jid = bf_id(state, 0, JELEVA)
            if (not ST["attack_done"] and ST["etb_done"] and jid is not None
                    and can_attack_now(state, jid)):
                d.setdefault("data", {}).update(
                    {"attacks": [[jid, {"type": "Player", "data": 1}]],
                     "bands": []})
                await submit_as_is(c, d)
                ST["attack_done"] = True
                ST["attack_turn"] = state.get("turn_number")
                say(f"[P0] attacking P1 with Jeleva (oid {jid})")
                wire("attack_jeleva", {"attacker": jid, "defender": 1})
            else:
                # never attack a second time: a second attack would create a
                # second trigger and muddy attribution of further free casts
                d.setdefault("data", {}).update({"attacks": [], "bands": []})
                await submit_as_is(c, d)
            return
    if not my_priority(state, 0):
        return
    phase = state.get("phase")
    active = state.get("active_player")
    in_flight = any((e.get("kind") or {}).get("type") == "Spell"
                    and e.get("controller") == 0
                    for e in stack_entries(state))
    # 1) cast Jeleva when U/B/R + 1 mana is available
    if (not ST["jeleva_cast"] and bf_id(state, 0, JELEVA) is None
            and not in_flight
            and phase in ("PreCombatMain", "PostCombatMain") and active == 0
            and JELEVA in hand_lnames(state, 0)):
        lands = untapped_lands(state, 0)
        have = {lname(state, o) for o in lands}
        if (len(lands) >= 4 and "island" in have and "swamp" in have
                and "mountain" in have):
            a = cast_action_for(acts, state, JELEVA)
            if a:
                say("[P0] casting Jeleva, Nephalia's Scourge (X=4)")
                wire("cast_jeleva", {"action": a["type"]})
                await submit_as_is(c, a)
                ST["jeleva_cast"] = True
                return
    # 2) normal play: land drop (colors first), then pass. Never cast from hand.
    if phase in ("PreCombatMain", "PostCombatMain") and active == 0:
        for want in LANDS:
            for a in acts:
                if a["type"] == "PlayLand":
                    d = a.get("data", {})
                    oid = d.get("object_id") or d.get("card_id") or a.get("_src_oid")
                    try:
                        nm = lname(state, int(oid)) if oid is not None else ""
                    except (TypeError, ValueError):
                        nm = ""
                    if nm == want and bf_id(state, 0, want) is None:
                        await submit_as_is(c, a)
                        return
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(c, a)
                return
    await pass_priority(c, st, acts)


# ------------------------------------------------------------- P1 tick
async def p1_tick(st, acts, state, c):
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan_keep(c, 1, "P1"):
            return
        if await do_bottom(c, 1, "P1"):
            return
        return
    if wtype == "BottomCards":
        if await do_bottom(c, 1, "P1"):
            return
        return
    if await pay_tick(c, acts, "P1"):
        return
    if await do_discard(c, 1, "P1"):
        return
    if wtype == "DeclareAttackers":
        da = find_action(acts, "DeclareAttackers")
        if da:
            d = _copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if wtype == "DeclareBlockers":
        db = find_action(acts, "DeclareBlockers")
        if db:
            d = _copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return
    if not my_priority(state, 1):
        return
    phase = state.get("phase")
    if phase in ("PreCombatMain", "PostCombatMain") \
            and state.get("active_player") == 1:
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(c, a)
                return
    await pass_priority(c, st, acts)


# ------------------------------------------------------------- observe/finish
def note_etb(state):
    """Gate on actually observing the exile in the client view (TOCTOU)."""
    if ST["jeleva_cast"] and not ST["etb_done"]:
        p0lib = lib_size(state, 0)
        ex = [o for o in state.get("objects", {}).values()
              if o.get("zone") == "Exile"]
        if ex and p0lib <= 60 - 7 - 4 + 2:  # drew 7, exiled 4 (+mulligan slack)
            ST["etb_done"] = True
            say(f"ETB exile observed: {len(ex)} objects in Exile, "
                f"P0 library={p0lib}")
            wire("etb_exile", {"exile_count": len(ex), "p0_lib": p0lib,
                               "sample": json.dumps(ex[0], default=str)[:800]})


def note_bolt_on_stack(state):
    if ST["in_free_cast_window"] and not ST["saw_bolt_on_stack"]:
        if any((e.get("kind") or {}).get("type") == "Spell"
               and e.get("controller") == 0
               for e in stack_entries(state)):
            ST["saw_bolt_on_stack"] = True
            say("free-cast Bolt observed on the stack")
            wire("bolt_on_stack", {})


def count_free_casts(state):
    """Completed free casts = oids that were observed in Exile and are now
    in P0's graveyard. The driver never casts from hand, so Exile->Graveyard
    transitions are free casts by construction; hand-size discards never
    touch Exile and are excluded via the exiled_oids set."""
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") == "Exile":
            try:
                ST["exiled_oids"].add(int(oid))
            except (TypeError, ValueError):
                pass
    oids = [oid for oid in ST["exiled_oids"]
            if get_obj(state, oid).get("zone") == "Graveyard"
            and lname(state, oid) == BOLT]
    return len(oids), sorted(oids)


def observation_window_closed(state):
    """True when the post-first-cast observation window is over: the first
    free cast was observed ON the stack and has since resolved (TOCTOU gate),
    and we have played through the end of P0's next turn with no second
    offer. A second completed cast ends the run via the main loop."""
    if ST["free_casts"] < 1 or ST["post_exported"]:
        return False
    if not ST["saw_bolt_on_stack"]:
        return False
    if any((e.get("kind") or {}).get("type") == "Spell"
           and e.get("controller") == 0
           for e in stack_entries(state)):
        return False
    turn = state.get("turn_number") or 0
    ct = ST["first_cast_turn"] or 0
    if turn < ct + 2:
        return False
    phase = state.get("phase")
    active = state.get("active_player")
    return (active == 0 and phase in ("PostCombatMain", "End", "Cleanup"))


async def export_pre(c):
    pre = await c.export_state()
    with open(f"{EVDIR}/pre.json", "w") as f:
        f.write(pre)
    ST["pre_exported"] = True
    pre_st = json.loads(pre)["state"]
    jid = bf_id(pre_st, 0, JELEVA)
    spells = exiled_spells(pre_st)
    ST["pre_exiled_spells"] = [oid for oid, _nm in spells]
    ST["pre_life_p1"] = life_of(pre_st, 1)
    ok = jid is not None and len(spells) >= 2
    ST["ass"]["A1_setup"] = "passed" if ok else "failed"
    ST["notes"].append(
        f"pre: jeleva_on_bf={jid is not None}, exiled instants/sorceries="
        f"{len(spells)} ({[nm for _, nm in spells][:6]}), P1 life="
        f"{ST['pre_life_p1']}")
    # record the exile linkage field for the evidence narrative
    for oid, o in (pre_st.get("objects") or {}).items():
        if o.get("zone") == "Exile":
            keys = sorted(o.keys())
            ST["notes"].append(f"exile object fields: {keys}")
            ST["notes"].append(
                "exile sample: " + json.dumps(o, default=str)[:600])
            break
    say(f"exported pre.json (A1={ST['ass']['A1_setup']})")


async def finish(c):
    notes = ST["notes"]
    ass = ST["ass"]
    if not ST["post_exported"]:
        try:
            post = await c.export_state()
            with open(f"{EVDIR}/post.json", "w") as f:
                f.write(post)
            ST["post_exported"] = True
            notes.append("post.json exported at finish() fallback")
        except Exception as e:
            notes.append(f"post export failed: {e}")
    try:
        post_st = json.loads(open(f"{EVDIR}/post.json").read())["state"] \
            if os.path.exists(f"{EVDIR}/post.json") else None
    except Exception as e:
        post_st = None
        notes.append(f"post reload failed: {e}")

    # A2: first free cast completed?
    if ST["free_casts"] >= 1:
        ass["A2_first_cast"] = "passed"
        notes.append(
            f"A2 passed: {ST['free_casts']} free cast(s) completed from exile "
            f"(cast oids {ST['cast_oids']}); P1 life went "
            f"{ST['pre_life_p1']}->{life_of(post_st, 1) if post_st else '?'}; "
            f"mana demanded during free cast: "
            f"{ST['mana_paid_during_free_cast']}.")
    elif ST["attack_done"]:
        ass["A2_first_cast"] = "failed"
        notes.append("A2 FAILED: attack trigger never produced a completed "
                     "free cast (optional accepts=%d, card choices=%d, "
                     "targets=%d)." % (ST["optional_accepts"],
                                       ST["card_choices_made"],
                                       ST["targets_submitted"]))
    else:
        notes.append("A2 not-run: attack never happened")

    # A3: the bound -- exactly one free cast per trigger?
    if ass["A1_setup"] == "passed" and ass["A2_first_cast"] == "passed":
        if ST["free_casts"] >= 2:
            ass["A3_single_bound"] = "failed"
            notes.append(
                f"A3 FAILED (reported bug): {ST['free_casts']} free casts "
                f"completed from a SINGLE attack trigger (cast oids "
                f"{ST['cast_oids']}); the CastFromZone grant is unbounded.")
        else:
            ass["A3_single_bound"] = "passed"
            notes.append(
                "A3 passed: exactly one free cast from the attack trigger; "
                "no second offer through end of P0's next turn.")
    else:
        notes.append("A3 not-run: setup or first cast did not complete")

    # A4: uncast exiled spells stay in exile
    if post_st is not None and ST["pre_exiled_spells"]:
        remaining = [oid for oid in ST["pre_exiled_spells"]
                     if get_obj(post_st, oid).get("zone") == "Exile"]
        expected = len(ST["pre_exiled_spells"]) - ST["free_casts"]
        if len(remaining) == expected and expected >= 0:
            ass["A4_exile_remainder"] = "passed"
            notes.append(f"A4 passed: {len(remaining)}/{expected} uncast "
                         f"exiled spells remain in Exile.")
        else:
            ass["A4_exile_remainder"] = "failed"
            notes.append(f"A4 FAILED: {len(remaining)} exiled spells remain, "
                         f"expected {expected} "
                         f"(pre={len(ST['pre_exiled_spells'])}, "
                         f"casts={ST['free_casts']}).")
    else:
        notes.append("A4 not-run: no post state or no pre exiled spells")

    if ass["A1_setup"] != "passed":
        verdict = "blocked"
        notes.append("verdict=blocked: setup (A1) failed")
    elif ass["A2_first_cast"] != "passed":
        verdict = "blocked"
        notes.append("verdict=blocked: first free cast never completed; the "
                     "single-cast bound cannot be evaluated")
    elif ass["A3_single_bound"] == "failed":
        verdict = "reproduced"
        notes.append("verdict=reproduced: one attack trigger yielded "
                     f"{ST['free_casts']} free casts (reported outcome: all "
                     "exiled instants/sorceries castable, not one).")
    elif all(ass[k] == "passed" for k in ass):
        verdict = "not-reproduced"
        notes.append("verdict=not-reproduced: exactly one free cast; the "
                     "bound holds.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: inconclusive assertion set")

    try:
        lines = open(f"{BACKFILL}/runs/{RUN_ID}/server.log").read().splitlines()
        excerpt = [l for l in lines
                   if "jeleva" in l.lower() or "castfromzone" in l.lower()
                   or "trigger" in l.lower()]
        with open(f"{EVDIR}/server_log_excerpt.txt", "w") as f:
            f.write("\n".join(excerpt[-120:]) + "\n")
        notes.append(f"server.log excerpt: {len(excerpt)} matching lines "
                     f"(of {len(lines)} total)")
    except Exception as e:
        notes.append(f"server.log excerpt failed: {e}")

    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": ST["started_at"],
        "duration_s": round(time.time() - ST["t0"], 1),
        "server": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 98, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_7359.py", "rb").read()).hexdigest(),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": {
            "trigger_seen": ST["trigger_seen"],
            "trigger_entry": ST["trigger_entry"],
            "optional_accepts": ST["optional_accepts"],
            "card_choices_made": ST["card_choices_made"],
            "targets_submitted": ST["targets_submitted"],
            "free_casts": ST["free_casts"],
            "cast_oids": ST["cast_oids"],
            "mana_paid_during_free_cast": ST["mana_paid_during_free_cast"],
            "attack_turn": ST["attack_turn"],
            "first_cast_turn": ST["first_cast_turn"],
            "pre_exiled_spells": ST["pre_exiled_spells"],
            "pre_life_p1": ST["pre_life_p1"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "36x Lightning Bolt density is a test-harness convenience "
            "(engine accepts >4-of for custom games); needed so Jeleva's "
            "X=4 exile yields >=2 instants/sorceries.",
            "The driver never casts from hand and P0 never attacks twice, so "
            "every exile-cast is attributable to the single attack trigger.",
            "States are authoritative exports, restorable only via full game replay.",
        ],
        "setup_line": "P0: 8x Jeleva, Nephalia's Scourge + 36x Lightning Bolt "
                      "+ 6x Island + 5x Swamp + 5x Mountain; P1: 60x Island",
        "contract_line": "Each Jeleva attack trigger permits exactly ONE free "
                         "instant/sorcery cast from among cards exiled with her",
        "prior_runs": [],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)
    return verdict


# ------------------------------------------------------------- main
async def main():
    reset()
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    await p0.connect()
    await p0.create(deck(*P0_DECK), player_count=2)
    await p1.connect()
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
    last_mech_diag = 0.0

    while time.time() - t_start < SETUP_DEADLINE_S:
        await asyncio.sleep(0.15)
        now = time.time()
        for c, tick, tag, pid in ((p0, p0_tick, "P0", 0),
                                  (p1, p1_tick, "P1", 1)):
            st = c.latest
            if not st:
                continue
            same_rev = (c.revision == last.get(tag))
            stale = now - last_tick_at.get(tag, 0) > 5
            if same_rev and not stale:
                continue
            last[tag] = c.revision
            last_tick_at[tag] = now
            # ---- observation pass (TOCTOU-safe: only on client-view facts)
            try:
                # surface rejections/errors without disturbing the pump
                try:
                    while True:
                        t, data = c.inbox.get_nowait()
                        if t in ("ActionRejected", "Error"):
                            say(f"[{tag}] {t}: {json.dumps(data)[:300]}")
                            wire("rejection", {"who": tag, "type": t,
                                               "data": data})
                        elif t not in ("StateUpdate", "GameStarted"):
                            wire("inbox_misc", {"who": tag, "type": t})
                except asyncio.QueueEmpty:
                    pass
                note_etb(st["state"])
                # exports: host seat only (P1's export 403s)
                if ST["etb_done"] and not ST["etb_exported"] \
                        and c.name == "P0":
                    etb = await c.export_state()
                    with open(f"{EVDIR}/mid_etb.json", "w") as f:
                        f.write(etb)
                    ST["etb_exported"] = True
                    say("exported mid_etb.json")
                hit = jeleva_trigger_entry(st["state"])
                if hit and ST["attack_done"] and not ST["trigger_seen"]:
                    ST["trigger_seen"] = True
                    ST["trigger_entry"] = json.dumps(hit, default=str)[:2000]
                    say("JELEVA ATTACK TRIGGER observed on stack")
                    wire("jeleva_trigger_on_stack", hit)
                    if not ST["pre_exported"] and c.name == "P0":
                        await export_pre(c)
                note_bolt_on_stack(st["state"])
                n, oids = count_free_casts(st["state"])
                # mechanism diagnostic: what does the post-attack decision
                # surface look like? (throttled)
                if ST["attack_done"] and tag == "P0" \
                        and now - last_mech_diag > 15:
                    last_mech_diag = now
                    sxx = st["state"]
                    vxx = get_vi(st) or {}
                    opps = [(o.get("interactionId") or o.get("id"),
                             (o.get("response") or {}).get("type"))
                            for o in vxx.get("opportunities", []) or []]
                    axx = sorted({a["type"] for a in merged_actions(st)})
                    ex_cast = exile_cast_action(merged_actions(st), sxx)
                    wire("mech_diag", {
                        "waiting_for": (wf_of(sxx).get("type")),
                        "vi_opps": opps,
                        "action_types": axx,
                        "exile_cast_oid": ex_cast[1],
                        "exile_spells": len(exiled_spells(sxx)),
                        "free_casts": ST["free_casts"]})
                if n > ST["free_casts"]:
                    ST["free_casts"] = n
                    ST["cast_oids"] = oids
                    if ST["first_cast_turn"] is None:
                        ST["first_cast_turn"] = st["state"].get("turn_number")
                    ST["in_free_cast_window"] = False
                    say(f"FREE CAST #{n} completed (exiled Bolt -> graveyard); "
                        f"P1 life={life_of(st['state'], 1)}")
                    wire("free_cast_completed",
                         {"n": n, "oids": oids,
                          "p1_life": life_of(st["state"], 1)})
            except Exception as e:
                wire("observe_error", {"err": str(e)})
            try:
                await tick(st, merged_actions(st), st["state"], c)
            except Exception as e:
                say(f"tick error {tag}: {e}")
                wire("tick_error", {"who": tag, "err": str(e)})
        if p0.latest:
            s = p0.latest["state"]
            # second free cast from the same trigger -> bug demonstrated
            if ST["free_casts"] >= 2 and not ST["post_exported"]:
                say("SECOND free cast completed from the same trigger; "
                    "exporting post and finishing")
                try:
                    post = await p0.export_state()
                    with open(f"{EVDIR}/post.json", "w") as f:
                        f.write(post)
                    ST["post_exported"] = True
                except Exception as e:
                    ST["notes"].append(f"post export failed: {e}")
                await finish(p0)
                await p0.close()
                await p1.close()
                return
            # observation window closed with exactly one cast
            if ST["free_casts"] == 1 and not ST["post_exported"] \
                    and observation_window_closed(s):
                say("observation window closed (end of P0's next turn); "
                    "exporting post and finishing")
                try:
                    post = await p0.export_state()
                    with open(f"{EVDIR}/post.json", "w") as f:
                        f.write(post)
                    ST["post_exported"] = True
                except Exception as e:
                    ST["notes"].append(f"post export failed: {e}")
                await finish(p0)
                await p0.close()
                await p1.close()
                return
        if now - last_diag > 60 and p0.latest:
            last_diag = now
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"jeleva={bf_id(s, 0, JELEVA)} exile_spells={len(exiled_spells(s))} "
                f"life={[life_of(s, i) for i in (0, 1)]} "
                f"atk={ST['attack_done']} casts={ST['free_casts']} "
                f"accepts={ST['optional_accepts']}")
    ST["notes"].append(f"global deadline ({SETUP_DEADLINE_S}s) hit")
    await finish(p0)
    await p0.close()
    await p1.close()


if __name__ == "__main__":
    asyncio.run(main())
