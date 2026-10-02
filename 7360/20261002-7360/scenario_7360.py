#!/usr/bin/env python3
"""Issue #7360: Prishe's Wanderings -- the reflexive "+1/+1 counter on target
creature you control" never triggers (no prompt, no counter).

First backfill run for this issue, on v0.99.0 / protocol 98.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (discord 2026-08-13, classifier: supported_aspect_defect,
status:confirmed): "I used Prishe's Wanderings, it worked fine to get the
land from the deck, but there wasn't a moment to put the +1/+1 counter on a
creature, it never triggered." The triage asked whether the reporter had any
creature on the battlefield (no legal target -> no prompt is correct); the
scenario therefore always fields a creature first.

Oracle text (pinned v0.99.0 card-data.json, "prishe's wanderings"):
  "Search your library for a basic land card or Town card, put it onto the
   battlefield tapped, then shuffle. When you search your library this way,
   put a +1/+1 counter on target creature you control."

Parse state (verified 2026-10-02 against pinned v0.99.0 card-data.json):
  The spell lowers to SearchLibrary -> ChangeZone (tapped) -> Shuffle ->
  CreateDelayedTrigger { condition: WhenNextEvent { trigger: SearchedLibrary,
  lifetime: Reflexive } } -> PutCounter. Matches the classifier's suspect
  exactly: the reflexive trigger "when you search your library this way" is
  lowered as a DELAYED trigger waiting for the NEXT SearchedLibrary event
  this turn. The search that satisfies it already happened inside this same
  resolution, so with no second search this turn the trigger can never fire --
  precisely the reported symptom (land found, counter prompt never comes).

Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 4x Prishe's Wanderings ({2}{G} sorcery) + 8x Grizzly Bears ({1}{G} 2/2)
      + 48x Forest.
  P1: 60x Forest dummy (never attacks, never blocks).

Drive:
  P0 mulligans (max 2) unless the opener holds a Bear or a Wanderings with
  >=3 lands. P0 ramps, casts a Grizzly Bear, then casts Prishe's Wanderings,
  chooses a Forest from the library search, and lets it resolve. The driver
  then watches for the reflexive counter prompt: a TargetSelection offering
  the Bear (which it would accept) or a Prishe's TriggeredAbility on the
  stack. Observation window 1 runs from the first cast through the end of
  the following turn with NO further searches -- a reflexive trigger must
  fire inside its own resolution, so this window is generous.
  Mechanism probe (window 2, notes only): if the counter never lands, P0
  casts a SECOND Wanderings on a later turn and watches one more turn. A
  prompt appearing only after the second search confirms the delayed
  (WhenNextEvent) lowering -- the first trigger firing on the second
  search's SearchedLibrary event -- rather than a reflexive trigger.

Assertions:
  A1_setup      pre.json (Wanderings on the stack, before resolution): Bear
                on P0 BF, >=1 Forest in P0 library pre-cast.
  A2_land       post-search: a Forest moved Library->Battlefield tapped
                (library shrank, BF Forest count grew, the new one tapped).
  A3_trigger    a PutCounter target prompt for the Bear (TargetSelection) or
                a Prishe's TriggeredAbility on the stack was observed, or A4
                passed. FAIL = the reported symptom (no prompt ever).
  A4_counter    post.json (end of window 1): the Bear carries a P1P1
                counter. FAIL = the reported outcome.
  A5_cleanup    stack empty and the game proceeding at post.

Verdict rule: reproduced iff A1 and A2 passed and (A3 failed or A4 failed);
              not-reproduced iff A1..A5 all passed;
              blocked iff A1 or A2 failed (the trigger test is moot when the
              setup or the land half did not complete).

Protocol-98 driver notes (v0.99.0, verified 2026-10-01/02):
  - HELLO advertises protocol 98 (server enforces exact match).
  - MulliganDecision as {"choice":{"type":"Keep"}} gated on
    waiting_for.data.pending[] with phase type "Declare".
  - BottomCards/DiscardToHandSize via single SelectCards {"cards":[...]}.
  - legal_actions is top-level on the WS message data.
  - SearchLibrary choice: card-choice opportunity whose candidates are
    library-zone objects; driver picks a Forest oid.
  - Target selection: advertised schema sequence response with the engine-
    issued candidate id (never synthesized from object ids).
  - Authoritative exports only from the host seat (P1's export 403s).
"""
import asyncio
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261002-7360"
ISSUE = 7360
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
    "server_run_id": "runs/20261002-7360 (live v0.99.0 server on 127.0.0.1:9374, "
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

WAND = "prishe's wanderings"
BEAR = "grizzly bears"
FOREST = "forest"

P0_DECK = [("Prishe's Wanderings", 4), ("Grizzly Bears", 8), ("Forest", 48)]
P1_DECK = [("Forest", 60)]

SETUP_DEADLINE_S = 1500

ST = {}
MULLS = set()
MULL_COUNT = {}
SUBMITTED = set()
_DISCARD_REV = {}


def reset():
    ST.clear()
    ST.update({
        "t0": time.time(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "game_code": None,
        "bear_cast": False,
        "wand_casts": 0,            # Wanderings casts submitted
        "wand_cast_turns": [],      # turn_number of each cast
        "pre_exported": False,
        "mid_exported": False,
        "post_exported": False,
        "window1_done": False,
        "probe_armed": False,
        "trigger_seen": False,      # TriggeredAbility entry referencing prishe
        "target_prompt_seen": False,# TargetSelection offering the Bear
        "target_answers": 0,
        "search_answers": 0,
        "bear_oid": None,
        "pre_lib_size": None,
        "pre_bf_forests": None,
        "probe_prompt_seen": False, # target prompt observed only in window 2
        "ass": {k: "not-run" for k in ("A1_setup", "A2_land", "A3_trigger",
                                       "A4_counter", "A5_cleanup")},
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
def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    o = get_obj(state, oid)
    return str(o.get("base_name") or o.get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in player_of(state, pid).get("hand", [])]


def lib_size(state, pid):
    return len(player_of(state, pid).get("library", []))


def bf_ids(state, pid, key):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and str(o.get("base_name") or o.get("name") or "").lower() == key]


def untapped_lands(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and not o.get("tapped")
            and str(o.get("base_name") or o.get("name") or "").lower() == FOREST]


def counters_of(state, oid):
    return get_obj(state, oid).get("counters") or {}


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
                if obj_lname(state, iv) == key:
                    return a
    return None


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


def _surface_object(ch):
    """First surface with type 'object' -> its data dict (or {})."""
    for s in ch.get("surfaces", []) or []:
        if s.get("type") == "object":
            return s.get("data") or {}
    return {}


def find_search_forest_choice(vi, state):
    """SearchLibrary card-choice opportunity whose candidates include a
    library-zone Forest -> (iid, candidate_id, resp_kind). Candidate ids
    are opaque (e.g. '3Z0JWR.0.84.s0'); the object identity lives in
    surfaces[].data.reference."""
    for opp in vi.get("opportunities", []) or []:
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        cands = list(data.get("candidates", []) or []) \
            + list(data.get("choices", []) or [])
        for ch in cands:
            d = _surface_object(ch)
            if str(d.get("zone", "")).lower() == "library" \
                    and str(d.get("name", "")).lower() == FOREST:
                return (opp.get("interactionId") or opp.get("id"),
                        ch.get("id"), resp.get("type"))
    return None, None, None


def find_confirm(vi):
    """An explicit-confirm opportunity (select schema with confirm step)
    -> (iid, choice, resp_kind)."""
    for opp in vi.get("opportunities", []) or []:
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        cands = list(data.get("candidates", []) or []) \
            + list(data.get("choices", []) or [])
        for ch in cands:
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []]
            if "confirm" in codes:
                return (opp.get("interactionId") or opp.get("id"),
                        ch, resp.get("type"))
    return None, None, None


def find_target_bear(vi, state, bear_oid):
    """TargetSelection opportunity offering the Bear -> (iid, target, kind).
    Candidate identity is in surfaces[].data.reference."""
    for opp in vi.get("opportunities", []) or []:
        resp = opp.get("response", {}) or {}
        data = resp.get("data", {}) or {}
        cands = list(data.get("candidates", []) or []) \
            + list(data.get("choices", []) or [])
        for ch in cands:
            d = _surface_object(ch)
            if str(d.get("reference", "")) == str(bear_oid):
                return (opp.get("interactionId") or opp.get("id"),
                        ch.get("id"), resp.get("type"))
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
    lands = [n for n in hand if n == FOREST]
    return len(lands) >= 3 and (WAND in hand or BEAR in hand)


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
        say(f"[P0] mulligan #{mulls + 1} (hand: {hn})")
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
        wire("mulligan", {"who": tag, "decision": "mulligan"})
    else:
        MULLS.add(tag)
        say(f"[P0] keep {len(hn)}")
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


async def do_bottom(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") not in ("MulliganDecision", "BottomCards"):
        return False
    pend = pending_for(state, pid)
    if not pend:
        return False
    phase = (pend.get("phase") or {}).get("type")
    if phase not in ("BottomCards", "Bottom"):
        return False
    key = (tag, "bottom", str((pend.get("phase") or {}).get("count")))
    if key in SUBMITTED:
        return False
    n = int((pend.get("phase") or {}).get("count") or 0)
    if n <= 0:
        return False
    hand = [int(o) for o in player_of(state, pid).get("hand", [])]
    rank = {WAND: 2, BEAR: 1, FOREST: 0}
    picks = sorted(hand, key=lambda o: rank.get(obj_lname(state, o), 0))[:n]
    SUBMITTED.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    wire("bottom", {"who": tag, "picks": picks})
    return True


def discard_rank(state, o):
    nm = obj_lname(state, o)
    if nm == FOREST:
        return 0
    if nm == BEAR:
        return 1
    return 2


async def do_discard(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    rev = c.revision
    if wf_of(state).get("type") != "DiscardToHandSize":
        return False
    if _DISCARD_REV.get((c.name, rev)):
        return False
    hand = [int(o) for o in player_of(state, pid).get("hand", [])]
    n = len(hand) - 7
    if n <= 0:
        return False
    picks = [int(x) for x in sorted(hand, key=lambda o: discard_rank(state, o))[:n]]
    say(f"[{tag}] discarding to hand size: {[obj_lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    _DISCARD_REV[(c.name, rev)] = True
    return True


async def pay_tick(c, acts, tag):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pass_priority(c, st, acts):
    for a in acts:
        if a["type"] == "PassPriority":
            await submit_as_is(c, a)
            return True
    return await vi_pass_fallback(c, st)

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
            import copy as _copy
            d = _copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return
    if wtype == "DeclareAttackers":
        da = find_action(acts, "DeclareAttackers")
        if da:
            import copy as _copy
            d = _copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return

    # ---- search choice (SearchLibrary) + explicit confirm ----
    vi = get_vi(st)
    if vi and "SearchChoice" in wtype:
        for opp in vi.get("opportunities", []) or []:
            resp = opp.get("response", {}) or {}
            data = resp.get("data", {}) or {}
            for ch in list(data.get("candidates", []) or []) \
                    + list(data.get("choices", []) or []):
                d = _surface_object(ch)
                if str(d.get("zone", "")).lower() == "library" \
                        and str(d.get("name", "")).lower() == FOREST:
                    if await answer_vi(c, opp, ch, "search"):
                        ST["search_answers"] += 1
                        say(f"[P0] search choice #{ST['search_answers']}: "
                            f"Forest ({ch.get('id')})")
                        wire("search_choice",
                             {"iid": opp.get("interactionId") or opp.get("id"),
                              "choice": ch.get("id")})
                        return True
        # maybe the selection needs an explicit confirm now
        ciid, ch, crtype = find_confirm(vi)
        if ch is not None:
            if await answer_vi(c, {"interactionId": ciid,
                                   "response": {"type": crtype},
                                   "id": ciid}, ch, "search-confirm"):
                say("[P0] search confirm submitted")
                wire("search_confirm", {"iid": ciid})
                return True

    # ---- target prompt for the counter: answer with the Bear ----
    if vi and "TargetSelection" in wtype and ST["bear_oid"] is not None:
        iid, tid, rtype = find_target_bear(vi, state, ST["bear_oid"])
        if tid is not None:
            key = (c.name, "target", str(iid))
            if key not in SUBMITTED:
                SUBMITTED.add(key)
                ST["target_answers"] += 1
                ST["target_prompt_seen"] = True
                if ST["window1_done"]:
                    ST["probe_prompt_seen"] = True
                say(f"[P0] counter target #{ST['target_answers']}: Bear "
                    f"(oid {tid}) -- prompt observed")
                wire("counter_target", {"iid": iid, "bear_oid": tid,
                                        "window": 2 if ST["window1_done"] else 1})
                await c.send_interaction(submit_interaction(c, iid, tid, rtype))
                return True

    if not my_priority(state, 0):
        return
    phase = state.get("phase")
    active = state.get("active_player")
    in_flight = any((e.get("kind") or {}).get("type") == "Spell"
                    and e.get("controller") == 0
                    for e in stack_entries(state))

    # 1) cast a Grizzly Bear first (need a creature on the battlefield)
    if (not ST["bear_cast"]
            and not bf_ids(state, 0, BEAR)
            and not in_flight
            and phase in ("PreCombatMain", "PostCombatMain") and active == 0
            and BEAR in hand_lnames(state, 0)):
        if len(untapped_lands(state, 0)) >= 2:
            a = cast_action_for(acts, state, BEAR)
            if a:
                say("[P0] casting Grizzly Bears")
                wire("cast_bear", {"action": a["type"]})
                await submit_as_is(c, a)
                ST["bear_cast"] = True
                return

    # 2) cast Prishe's Wanderings (up to 2: the 2nd is the mechanism probe)
    max_casts = 1 if not ST["window1_done"] else 2
    if (ST["wand_casts"] < max_casts
            and bf_ids(state, 0, BEAR)
            and not in_flight
            and phase in ("PreCombatMain", "PostCombatMain") and active == 0
            and WAND in hand_lnames(state, 0)):
        if len(untapped_lands(state, 0)) >= 3:
            a = cast_action_for(acts, state, WAND)
            if a:
                ST["wand_casts"] += 1
                ST["wand_cast_turns"].append(state.get("turn_number"))
                say(f"[P0] casting Prishe's Wanderings "
                    f"(#{ST['wand_casts']}, turn {state.get('turn_number')})")
                wire("cast_wanderings", {"n": ST["wand_casts"],
                                        "turn": state.get("turn_number")})
                await submit_as_is(c, a)
                # export pre immediately: the spell can resolve (search is
                # part of resolution) before the observation pass ever sees
                # it on the stack.
                if not ST["pre_exported"]:
                    ST["pre_lib_size"] = lib_size(state, 0)
                    ST["pre_bf_forests"] = len(bf_ids(state, 0, FOREST))
                    await export_pre(c)
                return

    # 3) normal play: land drop, then pass
    if phase in ("PreCombatMain", "PostCombatMain") and active == 0:
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
            import copy as _copy
            d = _copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if wtype == "DeclareBlockers":
        db = find_action(acts, "DeclareBlockers")
        if db:
            import copy as _copy
            d = _copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return
    if not my_priority(state, 1):
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if phase in ("PreCombatMain", "PostCombatMain") and active == 1:
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(c, a)
                return
    await pass_priority(c, st, acts)


# ------------------------------------------------------------- observation
async def export_pre(c):
    env = await c.export_state()
    with open(f"{EVDIR}/pre.json", "w") as f:
        f.write(env)
    ST["pre_exported"] = True
    say("exported pre.json")


async def export_post(c):
    env = await c.export_state()
    with open(f"{EVDIR}/post.json", "w") as f:
        f.write(env)
    ST["post_exported"] = True
    say("exported post.json")


def note_trigger_on_stack(state):
    if ST["trigger_seen"]:
        return
    for e in stack_entries(state):
        low = json.dumps(e, default=str).lower()
        if "prishe" in low and "triggered" in low:
            ST["trigger_seen"] = True
            say("PRISHE TRIGGERED ABILITY observed on stack")
            wire("prishe_trigger_on_stack", e)
            return


def window1_over(state):
    return (ST["wand_casts"] >= 1
            and (state.get("turn_number") or 0) > ST["wand_cast_turns"][0] + 1)


def window2_over(state):
    return (ST["wand_casts"] >= 2
            and (state.get("turn_number") or 0) > ST["wand_cast_turns"][1] + 1)


# ------------------------------------------------------------- finalization
async def finalize(c0):
    ass = ST["ass"]
    notes = ST["notes"]
    pre = post = None
    try:
        pre = json.load(open(f"{EVDIR}/pre.json"))
        post = json.load(open(f"{EVDIR}/post.json"))
    except Exception as e:
        notes.append(f"state load failed: {e}")
    pre_st = (pre or {}).get("state") or {}
    post_st = (post or {}).get("state") or {}

    # A1: setup
    if pre_st:
        bear_ok = bool(bf_ids(pre_st, 0, BEAR))
        lib_ok = (ST["pre_lib_size"] or 0) > 0
        # pre is exported at cast-submit time, so the Wanderings may still
        # be in hand in the client view; wand_casts>=1 is the ground truth
        wand_ok = (WAND in [obj_lname(pre_st, o)
                           for o in player_of(pre_st, 0).get("hand", [])]
                   or any((e.get("kind") or {}).get("type") == "Spell"
                          and "prishe" in json.dumps(e, default=str).lower()
                          for e in stack_entries(pre_st))
                   or ST["wand_casts"] >= 1)
        if bear_ok and lib_ok and wand_ok:
            ass["A1_setup"] = "passed"
            notes.append(f"A1 passed: Bear on P0 BF, library={ST['pre_lib_size']}, "
                         f"Wanderings cast #{ST['wand_casts']}.")
        else:
            ass["A1_setup"] = "failed"
            notes.append(f"A1 FAILED: bear={bear_ok} lib={lib_ok} "
                         f"wanderings={wand_ok}")
    else:
        notes.append("A1 not-run: no pre state")

    # A2: land moved Library -> BF tapped. Compare pre against mid_search
    # (captured right after the first search resolved): comparing against
    # post is confounded by the mechanism-probe's second search and by the
    # untap steps of the intervening turns.
    a2_st = None
    try:
        a2_st = (json.load(open(f"{EVDIR}/mid_search.json")) or {}).get("state")
    except Exception:
        a2_st = None
    a2_label = "mid_search"
    if not a2_st:
        a2_st = post_st
        a2_label = "post"
    if pre_st and a2_st and ass["A1_setup"] == "passed":
        pre_forests = ST["pre_bf_forests"] or 0
        a2_forests = len(bf_ids(a2_st, 0, FOREST))
        lib_delta = (ST["pre_lib_size"] or 0) - lib_size(a2_st, 0)
        new_tapped = [oid for oid in bf_ids(a2_st, 0, FOREST)
                      if get_obj(a2_st, oid).get("tapped")
                      and oid not in bf_ids(pre_st, 0, FOREST)]
        if a2_forests > pre_forests and lib_delta >= 1 and new_tapped:
            ass["A2_land"] = "passed"
            notes.append(f"A2 passed ({a2_label}): library "
                         f"{ST['pre_lib_size']}->{lib_size(a2_st, 0)}, BF "
                         f"Forests {pre_forests}->{a2_forests} (new one "
                         f"tapped: {new_tapped}).")
        else:
            ass["A2_land"] = "failed"
            notes.append(f"A2 FAILED ({a2_label}): BF Forests "
                         f"{pre_forests}->{a2_forests}, library "
                         f"delta={lib_delta}, new tapped={new_tapped}")
    else:
        notes.append("A2 not-run: A1 failed or missing states")

    # A3: the trigger prompt was observed (window 1)
    if ass["A1_setup"] == "passed" and ass["A2_land"] == "passed":
        if ST["trigger_seen"] or ST["target_prompt_seen"]:
            ass["A3_trigger"] = "passed"
            notes.append("A3 passed: counter trigger observed "
                         f"(trigger_on_stack={ST['trigger_seen']}, "
                         f"target_prompt={ST['target_prompt_seen']}).")
        else:
            ass["A3_trigger"] = "failed"
            notes.append("A3 FAILED (reported symptom): no counter trigger "
                         "and no target prompt observed through window 1.")
    else:
        notes.append("A3 not-run: setup/land half incomplete")

    # A4: the Bear carries a +1/+1 counter at post (end of window 1)
    if post_st and ass["A2_land"] == "passed":
        bids = bf_ids(post_st, 0, BEAR)
        n = max([counters_of(post_st, b).get("P1P1", 0) for b in bids]
                + [0]) if bids else 0
        if bids and n >= 1:
            ass["A4_counter"] = "passed"
            notes.append(f"A4 passed: Bear has {n} +1/+1 counter(s).")
        else:
            ass["A4_counter"] = "failed"
            notes.append(f"A4 FAILED (reported outcome): Bear counters at post "
                         f"= {[counters_of(post_st, b) for b in bids]} "
                         f"(bears on BF: {len(bids)}).")
    else:
        notes.append("A4 not-run: A2 failed or no post state")

    # A5: cleanup
    if post_st:
        empty = not stack_entries(post_st)
        wf = (post_st.get("waiting_for") or {}).get("type")
        if empty and wf in ("Priority", None):
            ass["A5_cleanup"] = "passed"
            notes.append(f"A5 passed: stack empty, waiting_for={wf}, "
                         f"game at turn {post_st.get('turn_number')}.")
        else:
            ass["A5_cleanup"] = "failed"
            notes.append(f"A5 FAILED: stack={len(stack_entries(post_st))}, "
                         f"waiting_for={wf}")
    else:
        notes.append("A5 not-run: no post state")

    # mechanism probe note (window 2)
    if ST["probe_armed"]:
        if ST["probe_prompt_seen"]:
            notes.append("MECHANISM PROBE: a counter target prompt appeared "
                         "only after the SECOND Wanderings' search resolved "
                         "-- consistent with the lowered "
                         "CreateDelayedTrigger{WhenNextEvent SearchedLibrary} "
                         "(first trigger fired on the second search's event), "
                         "not a reflexive trigger.")
        else:
            notes.append("MECHANISM PROBE: no counter prompt even after a "
                         "second Wanderings search this game.")

    if ass["A1_setup"] != "passed" or ass["A2_land"] != "passed":
        verdict = "blocked"
        notes.append("verdict=blocked: setup or land half incomplete")
    elif ass["A3_trigger"] == "failed" or ass["A4_counter"] == "failed":
        verdict = "reproduced"
        notes.append("verdict=reproduced: the land was found and entered "
                     "tapped, but the reflexive counter trigger never fired "
                     "and the Bear has no counter.")
    elif all(ass[k] == "passed" for k in ass):
        verdict = "not-reproduced"
        notes.append("verdict=not-reproduced: the counter trigger fired and "
                     "the Bear got its counter.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: inconclusive assertion set")

    import glob
    log_paths = [f"{BACKFILL}/runs/20261002-7359/server.log",
                 f"{BACKFILL}/runs/{RUN_ID}/server.log"]
    lines = []
    used = None
    for lp in log_paths + sorted(glob.glob(f"{BACKFILL}/runs/*/server.log"),
                                 reverse=True):
        try:
            lines = open(lp).read().splitlines()
            used = lp
            break
        except Exception:
            continue
    excerpt = [l for l in lines
               if "prishe" in l.lower() or "putcounter" in l.lower()
               or "searchedlibrary" in l.lower() or "trigger" in l.lower()]
    with open(f"{EVDIR}/server_log_excerpt.txt", "w") as f:
        f.write("\n".join(excerpt[-120:]) + "\n")
    notes.append(f"server.log excerpt: {len(excerpt)} matching lines "
                 f"(of {len(lines)} total; log={used})")

    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": ST["started_at"],
        "duration_s": round(time.time() - ST["t0"], 1),
        "server": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 98, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_7360.py", "rb").read()).hexdigest(),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": {
            "wand_casts": ST["wand_casts"],
            "wand_cast_turns": ST["wand_cast_turns"],
            "trigger_seen": ST["trigger_seen"],
            "target_prompt_seen": ST["target_prompt_seen"],
            "target_answers": ST["target_answers"],
            "search_answers": ST["search_answers"],
            "probe_prompt_seen": ST["probe_prompt_seen"],
            "pre_lib_size": ST["pre_lib_size"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "8x Grizzly Bears density is a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "The report's second item ('Inverted selection') never made it "
            "across from Discord; only the counter item is tested here.",
            "The triage's open question (did the reporter have a creature on "
            "the battlefield?) is answered affirmatively in this fixture: a "
            "Bear is always fielded before the Wanderings cast.",
            "States are authoritative exports, restorable only via full game replay.",
        ],
        "setup_line": "P0: 4x Prishe's Wanderings + 8x Grizzly Bears + 48x Forest; "
                      "P1: 60x Forest",
        "contract_line": "Casting Prishe's Wanderings with a creature on the "
                         "battlefield prompts for the +1/+1 counter target "
                         "and places the counter",
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
    finalized = False

    while time.time() - t_start < SETUP_DEADLINE_S and not finalized:
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
                state = st["state"]
                if tag == "P0":
                    bids = bf_ids(state, 0, BEAR)
                    if bids and ST["bear_oid"] is None:
                        ST["bear_oid"] = bids[0]
                        say(f"[P0] Bear on BF (oid {bids[0]})")
                    note_trigger_on_stack(state)
                    # pre export: Wanderings on the stack
                    if ST["wand_casts"] >= 1 and not ST["pre_exported"]:
                        on_stack = any(
                            (e.get("kind") or {}).get("type") == "Spell"
                            and "prishe" in json.dumps(e, default=str).lower()
                            for e in stack_entries(state))
                        if on_stack:
                            ST["pre_lib_size"] = lib_size(state, 0)
                            ST["pre_bf_forests"] = len(bf_ids(state, 0, FOREST))
                            await export_pre(c)
                    # mid export: search answered
                    if (ST["search_answers"] >= 1 and not ST["mid_exported"]
                            and not stack_entries(state)):
                        env = await c.export_state()
                        with open(f"{EVDIR}/mid_search.json", "w") as f:
                            f.write(env)
                        ST["mid_exported"] = True
                        say("exported mid_search.json")
                    # window 1 close: one full turn after the first cast
                    if (not ST["window1_done"] and ST["wand_casts"] >= 1
                            and window1_over(state)):
                        ST["window1_done"] = True
                        await export_post(c)
                        say(f"window 1 closed at turn {state.get('turn_number')}")
                        wire("window1_close", {"turn": state.get("turn_number"),
                                              "trigger_seen": ST["trigger_seen"],
                                              "target_prompt_seen":
                                              ST["target_prompt_seen"]})
                        # if the counter landed, we're done: not-reproduced
                        bids2 = bf_ids(state, 0, BEAR)
                        n = max([counters_of(state, b).get("P1P1", 0)
                                 for b in bids2] + [0]) if bids2 else 0
                        if n >= 1:
                            say("counter observed in window 1 -- finishing")
                            finalized = True
                            break
                        ST["probe_armed"] = True
                        say("arming mechanism probe: casting a 2nd Wanderings")
                    # window 2 close
                    if (ST["probe_armed"] and ST["wand_casts"] >= 2
                            and window2_over(state)):
                        say(f"window 2 closed at turn {state.get('turn_number')}")
                        finalized = True
                        break
            except Exception as e:
                say(f"[{tag}] observation error: {e}")
            # ---- action pass
            try:
                acts = merged_actions(st)
                await tick(st, acts, st["state"], c)
            except Exception as e:
                say(f"[{tag}] tick error: {e}")
        # deadline watchdog: if window1 never closed (cast never happened),
        # finalize as blocked once time runs out
    say("finalizing")
    verdict = await finalize(p0)
    await p0.close()
    await p1.close()
    return verdict


if __name__ == "__main__":
    v = asyncio.run(main())
    # copy the scenario into the evidence dir for the manifest
    shutil.copy(f"{BACKFILL}/driver/scenario_7360.py",
                f"{EVDIR}/scenario_7360.py")
    sys.exit(0)
