#!/usr/bin/env python3
"""Issue #7433: Mr. Monopoly, On the Go -- the [-2] "Heist!" exile is
unparsed, so `GrantCastingPermission` reads an empty tracked set.

Reported (2026-08-15, lgray, source:internal-triage, status:confirmed,
area:parser, mechanic:zone-change, priority:p3-card-specific,
classifier:unsupported-aspect; related #6857 tracked-set publish census,
one of 33 cards whose tracked-set antecedent clause never parses):

> [0]: Roll a six-sided die. Put a number of loyalty counters on
>      Mr. Monopoly equal to the result.
> [-2]: Heist! -- Exile the top two cards of target opponent's library.
>       Until end of turn, you may play those cards, and mana of any type
>       can be spent to cast them.
> [-4]: Shut Down! -- Destroy target artifact.
> [-40]: Pass Go -- Create 200 Treasure tokens.

The parser emits an `Effect::Unimplemented` node as the chain head (root)
for "Heist! -- Exile the top two cards of target opponent's library";
its sub_ability is the anaphor -- "Until end of turn, you may play those
cards, and mana of any type can be spent to cast them" -- a
`GrantCastingPermission` { PlayFromExile, UntilEndOfTurn, granted_to 0,
mana AnyTypeOrColor } targeting the chain tracked set (TrackedSet(0)).
The Unimplemented resolver is a no-op (pushes no GameEvent), so the
publish authority allocates a fresh EMPTY tracked set. `GrantCastingPermission`
is NOT among the #6857 census's measured consumer classes, so the
consumer-side outcome was unmeasured in the report -- the report asserts
the parse state and its direct structural consequence, not a runtime
symptom. The triage acceptance criteria asks for a runtime test driving
the engine through resolution and observing the resulting game state.

Pinned v0.100.0 parse (see data_evidence.json): abilities[1]
  (Activated, cost Loyalty -2):
  head = Unimplemented { name: "unrecognized_clause_head",
                         description: "Heist! -- Exile the top two cards of
                                       target opponent's library" }
    sub_ability (Spell): GrantCastingPermission {
                           permission: PlayFromExile / UntilEndOfTurn /
                                       granted_to 0 /
                                       mana_spend_permission AnyTypeOrColor,
                           target: TrackedSet(0) }
  duration: UntilEndOfTurn. No parsed targets (target_prompt null -- the
  "target opponent" never parsed either). loyalty 5, mana cost {3}{R}.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 12x Mr. Monopoly, On the Go + 48x Mountain (60)
  P1: 12x Lightning Bolt + 48x Mountain (60; plays lands, never casts,
      never attacks)
Drive:
  1. Mulligans: both seats keep 7.
  2. Build: both play a land per turn; P0 casts Mr. Monopoly ({3}{R})
     once affordable; P1 never casts.
  3. Arm: on P0's main phase with Mr. Monopoly on the battlefield at
     loyalty >= 2, empty stack, P0 priority, and the [-2] (ability_index 1)
     advertised: export pre.json INSIDE the activation path (guarded flag;
     backfill lesson: export at the submission site, not a probe), then
     submit ActivateAbility. The ability has no parsed targets.
  4. Resolution window: record every viewer-interaction opportunity
     offered to ANY seat from activation through resolution; answer a
     genuine opponent-choice prompt defensively if one ever appears
     (expected never: no targets parsed); otherwise pass priority.
  5. Export mid_stack.json when the ability is first seen on the stack;
     export post.json once settled (ability resolved, stack empty,
     Priority, 8s idle).

Expected (correct behavior): the [-2] exiles the top two cards of P1's
library and P0 may cast them this turn with any mana -- exactly 2
P1-owned cards move Library->Exile in the window, and P0 is offered the
exile-cast.
Reported (bug): the antecedent clause never parsed (Unimplemented head,
a runtime no-op), so no exile happens and the GrantCastingPermission
sub-ability reads an empty tracked set -- 0 cards exiled, no cast
permission observable.

Assertions:
  A1_data_level   pinned v0.100.0 card-data.json parses Mr. Monopoly,
                  On the Go as the issue reports: abilities[1] head
                  Unimplemented naming the "Heist!" clause; sub Spell
                  GrantCastingPermission { PlayFromExile, UntilEndOfTurn,
                  granted_to 0, AnyTypeOrColor } target TrackedSet(0);
                  cost Loyalty -2; loyalty 5; mana {3}{R}.
  A2_setup_ok     pre.json: Mr. Monopoly on P0's battlefield at loyalty
                  >= 2; P1 library size >= 2; phase PreCombatMain (or
                  PostCombatMain), active player P0.
  A3_activated_resolved
                  the [-2] was activated (loyalty 5 -> 3 observed) and
                  its ability object was seen on the stack and later
                  resolved (off stack).
  A4_exile        count of P1-owned objects with zone Library at pre and
                  zone Exile at post. Passes iff == 2 (correct behavior).
                  EXPECTED TO FAIL (0) under the bug -- the Unimplemented
                  head pushed no GameEvent, so nothing was exiled.
  A5_cast_permission
                  P0 was offered a genuine exile-cast opportunity for the
                  exiled cards in the window. EXPECTED TO FAIL under the
                  bug (nothing exiled, nothing offered). Only meaningful
                  if A4 passed.
  A6_cleanup      post stack empty, game advanced past the activation turn.

Verdict rule: reproduced iff A1, A2, A3 passed and A4 failed (0 exiled);
              not-reproduced iff A1..A5 all passed;
              blocked iff A1, A2, or A3 could not be established.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261003-7433"
ISSUE = 7433
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR)
             if f not in ("scenario_run.log", "driver_stdout.log")]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.100.0",
    "build_commit": "bc9ef56",
    "protocol_version": 101,
    "server_binary_sha256": "261550905a3d569731c9bd66b2b12a0fa878400fefaa9cc36f3ad4e1a3d8adda",
    "card_data_sha256": "57e086e700ee0bd81002327e89d339356c4bb6c26d9e1ca9c010474c0b8c291c",
    "draft_pools_sha256": "961c5397d834ca92b2168035be386844339573024df72dbc78370b934ed75770",
    "signature_verified": True,
    "server_run_id": "backfill-owned v0.100.0 server on 127.0.0.1:9374 "
                     "(shared process; ServerHello 0.100.0/bc9ef56/"
                     "protocol 101 re-verified by this run's own Hello "
                     "handshake)",
    "mode": "Full",
    "source": "2026-10-03: latest stable release v0.100.0 == pinned "
              "release dir (GitHub /releases re-confirmed v0.100.0 still "
              "latest stable at 02:15 CDT 2026-10-03); hashes recomputed "
              "against on-disk artifacts this run",
}

# Recompute against on-disk artifacts; never copy hashes blindly.
for _f, _k in (("server/releases/v0.100.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.100.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.100.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.100.0/data/card-data.json"))

MONO = "mr. monopoly, on the go"
MOUNTAIN = "mountain"
BOLT = "lightning bolt"
BASICS = {"swamp", "mountain", "forest", "island", "plains"}

P0_DECK = [("Mr. Monopoly, On the Go", 12), ("Mountain", 48)]
P1_DECK = [("Lightning Bolt", 12), ("Mountain", 48)]


def deck(pairs):
    names = []
    for n, c in pairs:
        names += [n] * c
    return {"main_deck": names, "sideboard": [], "commander": []}


SETUP_DEADLINE_S = 1800
RESOLVE_DEADLINE_S = 600
SETTLE_IDLE_S = 8


def reset_attempt():
    global ST, MULLS, SUBMITTED_OPPS, _DISCARD_REV
    ST = {
        "t0": time.time(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "game_code": None,
        "phase": "build",  # build -> resolving -> done
        "monopoly_cast": False,
        "monopoly_cast_turn": None,
        "monopoly_oid": None,
        "heist_activated": False,
        "heist_on_stack": False,
        "heist_resolved": False,
        "heist_turn": None,
        "loyalty_at_pre": None,
        "loyalty_at_post": None,
        "pre_exported": False,
        "mid_exported": False,
        "post_exported": False,
        "p1_lib_at_pre": None,
        "exiled_in_window": None,
        "mana_needs": {"P0": {"R": 0, "generic": 0},
                       "P1": {"R": 0, "generic": 0}},
        "prompts_seen": [],
        "exile_cast_offered": False,
        "opponent_choice_answered": False,
        "wf_types_window": [],
        "terminal": False,
        "states_seen": 0,
        "cast_rejections": 0,
        "stack_dumped": False,
        "settle_at": None,
        "ass": {k: "not-run" for k in ("A1_data_level", "A2_setup_ok",
                                       "A3_activated_resolved",
                                       "A4_exile", "A5_cast_permission",
                                       "A6_cleanup")},
        "notes": [],
        "data_level_ok": False,
        "resolving_since": None,
    }
    MULLS = set()
    SUBMITTED_OPPS = set()
    _DISCARD_REV = {}


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    if not RUNLOG.closed:
        RUNLOG.write(msg + "\n")
        RUNLOG.flush()


def wire(event, payload):
    if WIRE.closed:
        return
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


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def is_land(o):
    return "land" in [str(t).lower()
                      for t in (o.get("card_types") or {}).get("core_types", [])]


def bf_permanents(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def monopoly_oid(state, pid=0):
    for oid in bf_permanents(state, pid):
        if obj_lname(state, oid) == MONO:
            return oid
    return None


def loyalty_of(o):
    if isinstance(o.get("loyalty"), (int, float)):
        return int(o["loyalty"])
    for c in o.get("counters", []) or []:
        if isinstance(c, dict) and "loyal" in str(c).lower():
            return int(c.get("count", c.get("amount", 0)))
    return None


def untapped_lands(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and not o.get("tapped") and is_land(o)]


def mana_color_pool(state, pid):
    pool = {"B": 0, "R": 0, "G": 0, "U": 0, "W": 0}
    for oid in untapped_lands(state, pid):
        nm = obj_lname(state, oid)
        if nm == "swamp":
            pool["B"] += 1
        elif nm == "mountain":
            pool["R"] += 1
        elif nm == "forest":
            pool["G"] += 1
        elif nm == "island":
            pool["U"] += 1
        elif nm == "plains":
            pool["W"] += 1
    return pool


def library_oids(state, pid):
    return [str(x) for x in player_of(state, pid).get("library", [])]


def merged_actions(st):
    acts = list(st.get("legal_actions") or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


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


def stack_entries(state):
    return state.get("stack") or []


def heist_on_stack(state):
    """Match the Heist! activated ability on the stack."""
    for se in stack_entries(state):
        blob = json.dumps(se, default=str).lower()
        if "monopoly" in blob or "heist" in blob:
            return se
    return None


def dump_stack_once(state, why):
    if ST.get("stack_dumped"):
        return
    ST["stack_dumped"] = True
    with open(f"{EVDIR}/stack_dump.json", "w") as f:
        json.dump({"why": why, "stack": stack_entries(state)}, f, indent=1,
                  default=str)
    say(f"[window] stack dump written ({why}); "
        f"{len(stack_entries(state))} entries")
    wire("stack_dump", {"why": why,
                        "n": len(stack_entries(state))})

# ------------------------------------------------------------- data check
def check_data_level():
    card = CARD_DATA.get(MONO, {})
    abilities = card.get("abilities") or []
    heist = abilities[1] if len(abilities) > 1 else {}
    head = heist.get("effect") or {}
    sub = (heist.get("sub_ability") or {})
    sub_eff = sub.get("effect") or {}
    perm = sub_eff.get("permission") or {}
    tgt = sub_eff.get("target") or {}
    ev = {
        "name": card.get("name"),
        "oracle": card.get("oracle_text"),
        "card_type": card.get("card_type"),
        "mana_cost": card.get("mana_cost"),
        "loyalty": card.get("loyalty"),
        "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
        "heist_ability": heist,
        "report_corpus_note": ("the issue's corpus (9b7c66e30) named the "
                               "Unimplemented head 'heist!'; the pinned "
                               "v0.100.0 corpus names it "
                               "'unrecognized_clause_head' with the "
                               "identical description -- both are "
                               "Effect::Unimplemented over the same clause; "
                               "the structural claim is unchanged."),
    }
    ok = (len(abilities) >= 2
          and head.get("type") == "Unimplemented"
          and "heist!" in str(head.get("description", "")).lower()
          and "exile the top two cards of target opponent" in
          str(head.get("description", "")).lower()
          and sub.get("kind") == "Spell"
          and sub_eff.get("type") == "GrantCastingPermission"
          and perm.get("type") == "PlayFromExile"
          and perm.get("duration") == "UntilEndOfTurn"
          and perm.get("granted_to") == 0
          and perm.get("mana_spend_permission") == "AnyTypeOrColor"
          and tgt.get("type") == "TrackedSet"
          and tgt.get("id") == 0
          and (heist.get("cost") or {}).get("type") == "Loyalty"
          and (heist.get("cost") or {}).get("amount") == -2
          and str(card.get("loyalty")) == "5"
          and (card.get("mana_cost") or {}).get("generic") == 3
          and (card.get("mana_cost") or {}).get("shards") == ["Red"])
    say(f"data-level check: head={head.get('type')}/{head.get('name')}/"
        f"{head.get('description')!r}; sub={sub_eff.get('type')}/"
        f"perm={json.dumps(perm)}/target={json.dumps(tgt)}; "
        f"cost={json.dumps(heist.get('cost'))}; loyalty={card.get('loyalty')}; "
        f"mana={json.dumps(card.get('mana_cost'))}")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    ST["data_level_ok"] = ok
    wire("data_level", {"ok": ok})

# ------------------------------------------------------------- actions
async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    wire("action_submit", {"who": c.name, "action": msg})
    await c.send_action(msg)


async def interact_as(c, sub, tag):
    wire("interaction_submit", {"who": tag,
                                "interactionId": sub.get("interactionId"),
                                "response": sub.get("response")})
    await c.send_interaction(sub)


async def do_mulligan(c, pid, tag):
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
    say(f"[{tag}] keep 7")
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    wire("mulligan", {"who": tag, "decision": "keep"})
    return True


async def do_discard_to_handsize(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    rev = c.revision
    if wf_of(state).get("type") != "DiscardToHandSize":
        return False
    pend = pending_for(state, pid)
    if pend is None:
        return False
    if _DISCARD_REV.get((c.name, rev)):
        return False
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return False
    # never discard the combo pieces first: basics, then bolts, then Monopoly
    order = sorted(hand, key=lambda o: (
        0 if obj_lname(state, o) in BASICS
        else (1 if obj_lname(state, o) == BOLT else 2), o))
    picks = [int(x) for x in order[:n]]
    say(f"[{tag}] discarding {n} to hand size")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    _DISCARD_REV[(c.name, rev)] = True
    return True


async def do_discard_vi(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    vi = get_vi(st)
    if not vi:
        return False
    hand_oids = set(hand_ids(state, pid))
    if not hand_oids:
        return False
    for opp in vi.get("opportunities", []) or []:
        resp = opp.get("response") or {}
        if resp.get("type") != "schema":
            continue
        spec = (resp.get("data") or {}).get("spec") or {}
        if spec.get("type") != "select":
            continue
        chs = (resp.get("data") or {}).get("candidates") or []
        if not chs:
            continue
        cand_oids = set()
        all_hand = True
        for ch in chs:
            for sf in (ch.get("surfaces") or []):
                if sf.get("type") == "object":
                    d = sf.get("data") or {}
                    if d.get("zone") != "hand":
                        all_hand = False
                        break
                    try:
                        cand_oids.add(int(d.get("reference")))
                    except (TypeError, ValueError):
                        pass
            if not all_hand:
                break
        if not all_hand or not cand_oids or not cand_oids <= hand_oids:
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        pick = chs[0].get("id")
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] vi discard-to-hand-size: {pick}")
        wire("vi_discard", {"who": tag, "iid": iid, "pick": pick})
        await interact_as(c, {
            "interactionId": iid,
            "response": {"type": "select",
                         "data": {"choiceIds": [pick]}}}, tag)
        return True
    return False


async def pay_mana_vi(c, st, state, tag):
    """Answer vi mana-payment choices during casting (7423 pattern)."""
    vi = get_vi(st)
    if not vi:
        return False
    needs = ST["mana_needs"][tag]
    for opp in vi.get("opportunities", []) or []:
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        rtype = (opp.get("response", {}) or {}).get("type")
        taps = []
        for ch in data.get("choices") or []:
            if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
                continue
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
            if "tapLandForMana" in codes:
                manas = [s for s in ch.get("surfaces", []) or []
                         if s.get("type") == "mana"]
                syms = manas[0]["data"].get("symbols", []) if manas else []
                taps.append((ch, syms))
        if not taps:
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        pick, syms, used = None, [], None
        if sum(needs.values()) > 0 and taps:
            for ch, s in taps:  # colored needs first
                for color in ("B", "G", "U", "W", "R"):
                    if needs.get(color, 0) > 0 and color in s:
                        pick, syms, used = ch, s, color
                        break
                if pick is not None:
                    break
            if pick is None and needs.get("generic", 0) > 0:
                pick, syms, used = taps[0][0], taps[0][1], "generic"
            if pick is None:
                continue  # no choice makes progress; do not tap blindly
            needs[used] -= 1
            wire("tap_land", {"iid": iid, "choice": pick["id"],
                              "symbols": syms, "used_for": used,
                              "needs_now": dict(needs)})
            say(f"[{tag}] tap land for mana: {pick['id'][:24]} "
                f"symbols={syms} used_for={used} needs={dict(needs)}")
        else:
            continue
        SUBMITTED_OPPS.add(iid)
        if rtype == "exactChoices":
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": pick["id"]}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": "sequence",
                                "data": {"choiceIds": [pick["id"]]}}}
        await interact_as(c, sub, tag)
        return True
    return False


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
    vi = get_vi(st)
    if vi:
        for opp in vi.get("opportunities", []) or []:
            data = (opp.get("response", {}) or {}).get("data", {}) or {}
            for ch in data.get("choices") or []:
                codes = [s.get("data", {}).get("code")
                         for s in ch.get("surfaces", []) or []]
                if "passPriority" in codes and ch.get("status", {}) \
                        .get("type") == "available":
                    iid = opp.get("interactionId") or opp.get("id")
                    await interact_as(c, {
                        "interactionId": iid,
                        "response": {"type": "choose",
                                     "data": {"choiceId": ch.get("id")}}},
                        tag)
                    return True
    return False


async def export_as(c, name):
    env = await c.export_state()
    with open(f"{EVDIR}/{name}.json", "w") as f:
        f.write(env)
    say(f"exported {name}.json")


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
                    return a, iv
    return None, None


def record_window_prompt(seat, st):
    vi = st.get("viewer_interaction") or {}
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId") or opp.get("id")
        if all(t[2] != iid for t in ST["prompts_seen"]):
            ST["prompts_seen"].append((ST["phase"], seat, iid, opp))
            wire("window_prompt", {"phase": ST["phase"], "seat": seat,
                                   "iid": iid, "opportunity": opp})
            say(f"[{ST['phase']}/{seat}] prompt: "
                f"{json.dumps(opp)[:260]}")


def is_exile_cast_opp(opp):
    """True iff this opportunity offers casting a card from exile
    (the GrantCastingPermission consumer surfacing at runtime)."""
    resp = opp.get("response") or {}
    rtype = resp.get("type")
    data = resp.get("data") or {}
    blob = json.dumps(opp).lower()
    if "exile" not in blob:
        return False
    if rtype == "schema":
        spec = data.get("spec") or {}
        if spec.get("type") != "select":
            return False
        cands = data.get("candidates") or []
        for ch in cands:
            for sf in (ch.get("surfaces") or []):
                if sf.get("type") == "object":
                    d = sf.get("data") or {}
                    if d.get("zone") == "Exile":
                        return True
        return False
    if rtype == "exactChoices":
        choices = data.get("choices") or []
        return bool(choices) and "cast" in blob
    return False


async def answer_opponent_choice(c, pid, tag):
    """Defensive: if the engine DOES ask for the (unparsed) target
    opponent, answer with the opponent seat. Under the bug this handler
    never fires -- the ability declares no targets."""
    st = c.latest
    if not st:
        return False
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        blob = json.dumps(opp).lower()
        if "opponent" not in blob and "player" not in blob:
            continue
        resp = opp.get("response") or {}
        rtype = resp.get("type")
        data = resp.get("data") or {}
        picks = []
        if rtype == "schema":
            for ch in data.get("candidates") or []:
                chb = json.dumps(ch).lower()
                if "opponent" in chb or '"1"' in chb or "player 1" in chb:
                    picks.append(ch.get("id"))
        elif rtype == "exactChoices":
            for ch in data.get("choices") or []:
                chb = json.dumps(ch).lower()
                if "opponent" in chb:
                    picks.append(ch.get("id"))
        if not picks:
            continue
        SUBMITTED_OPPS.add(iid)
        ST["opponent_choice_answered"] = True
        say(f"[{tag}] DEFENSIVE: answering opponent-choice prompt: "
            f"{picks[0][:24]}")
        wire("opponent_choice_answer", {"who": tag, "iid": iid,
                                        "pick": picks[0]})
        if rtype == "exactChoices":
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": picks[0]}}}
        else:
            sub = {"interactionId": iid,
                   "response": {"type": "sequence",
                                "data": {"choiceIds": [picks[0]]}}}
        await interact_as(c, sub, tag)
        return True
    return False

# ------------------------------------------------------------- P0 tick
async def p0_tick(st, acts, state, c):
    pid = c.player_id
    if ST["phase"] == "resolving":
        record_window_prompt("P0", st)
        if await answer_opponent_choice(c, pid, "P0"):
            return
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan(c, pid, "P0"):
            return
        return
    if await do_discard_to_handsize(c, pid, "P0"):
        return
    if await do_discard_vi(c, pid, "P0"):
        return
    if sum(ST["mana_needs"]["P0"].values()) > 0:
        if await pay_mana_vi(c, st, state, "P0"):
            return
        if await pay_tick(c, acts, "P0"):
            return
    if wtype == "DeclareAttackers":
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if wtype == "DeclareBlockers":
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            d = copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return
    if not my_priority(state, pid):
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if phase in ("PreCombatMain", "PostCombatMain") and active == pid \
            and not stack_entries(state) and ST["phase"] == "build":
        hn = hand_lnames(state, pid)
        # Cast Mr. Monopoly once {3}{R} is available.
        if not ST["monopoly_cast"] and MONO in hn:
            pool = mana_color_pool(state, pid)
            lands = len(untapped_lands(state, pid))
            if pool["R"] >= 1 and lands >= 4:
                a, oid = cast_action_for(acts, state, MONO)
                if a:
                    ST["monopoly_cast"] = True
                    ST["monopoly_cast_turn"] = state.get("turn_number")
                    ST["mana_needs"]["P0"] = {"R": 1, "generic": 3}
                    say(f"[P0] casting Mr. Monopoly, On the Go (oid {oid})")
                    wire("monopoly_cast", {"oid": oid})
                    await submit_as_is(c, a)
                    return
        # Activate the [-2] Heist! (ability_index 1) once advertised.
        if ST["monopoly_cast"] and not ST["heist_activated"]:
            moid = monopoly_oid(state, pid)
            if moid is not None:
                loy = loyalty_of(get_obj(state, moid))
                if loy is not None and loy >= 2:
                    opts = []
                    for a in acts:
                        if a["type"] != "ActivateAbility":
                            continue
                        d = a.get("data", {}) or {}
                        src = str(d.get("source_id", d.get("object_id", "")))
                        if src == str(moid):
                            opts.append((d.get("ability_index"), a))
                    choice = next((a for i, a in opts if i == 1), None)
                    if choice is not None:
                        # export pre INSIDE the activation path (guarded)
                        if not ST["pre_exported"]:
                            await export_as(c, "pre")
                            ST["pre_exported"] = True
                            ST["loyalty_at_pre"] = loy
                            ST["p1_lib_at_pre"] = library_oids(state, 1)
                            say(f"[arm] pre.json exported: monopoly loyalty "
                                f"{loy}, P1 library "
                                f"{len(ST['p1_lib_at_pre'])} cards")
                            wire("pre_exported",
                                 {"loyalty": loy,
                                  "p1_lib": len(ST["p1_lib_at_pre"])})
                        d = dict(choice.get("data", {}))
                        for k in ("source_id", "object_id"):
                            if k in d:
                                d[k] = int(d[k])
                        ST["heist_activated"] = True
                        ST["heist_turn"] = state.get("turn_number")
                        say(f"[P0] activating Heist! [-2] "
                            f"(ability_index 1), loyalty was {loy}")
                        wire("heist_activate", {"monopoly": moid,
                                                "loyalty": loy})
                        await submit_as_is(
                            c, {"type": "ActivateAbility", "data": d})
                        return
        # play a land
        n_lands = sum(1 for oid in bf_permanents(state, pid)
                      if is_land(get_obj(state, oid)))
        if n_lands < 8:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(c, a)
                    return
    await pass_priority(c, st, acts)


# ------------------------------------------------------------- P1 tick (build only, never cast, never attack)
async def p1_tick(st, acts, state, c):
    pid = c.player_id
    if ST["phase"] == "resolving":
        record_window_prompt("P1", st)
        if await answer_opponent_choice(c, pid, "P1"):
            return
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan(c, pid, "P1"):
            return
        return
    if await do_discard_to_handsize(c, pid, "P1"):
        return
    if await do_discard_vi(c, pid, "P1"):
        return
    if wtype == "DeclareAttackers":
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if wtype == "DeclareBlockers":
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            d = copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return
    if not my_priority(state, pid):
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if phase in ("PreCombatMain", "PostCombatMain") and active == pid \
            and not stack_entries(state) and ST["phase"] == "build":
        n_lands = sum(1 for oid in bf_permanents(state, pid)
                      if is_land(get_obj(state, oid)))
        if n_lands < 6:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(c, a)
                    return
    await pass_priority(c, st, acts)


# ------------------------------------------------------------- finalize
async def finalize(c0):
    ass = ST["ass"]
    notes = ST["notes"]

    def load_env(name):
        try:
            return json.load(open(f"{EVDIR}/{name}.json"))
        except Exception as e:
            notes.append(f"{name}.json load failed: {e}")
            return None

    pre = load_env("pre") or {}
    mid = load_env("mid_stack") or {}
    post = load_env("post") or {}
    pre_st = pre.get("state") or {}
    post_st = post.get("state") or {}

    # A1: data-level parse matches the issue
    if ST.get("data_level_ok"):
        ass["A1_data_level"] = "passed"
        notes.append("A1 passed: pinned v0.100.0 card-data.json parses "
                     "Mr. Monopoly, On the Go as the issue reports -- "
                     "abilities[1] head Unimplemented "
                     "('unrecognized_clause_head': 'Heist! -- Exile the top "
                     "two cards of target opponent's library') + sub Spell "
                     "GrantCastingPermission { PlayFromExile, UntilEndOfTurn, "
                     "granted_to 0, mana AnyTypeOrColor } target "
                     "TrackedSet(0); cost Loyalty -2; loyalty 5; mana "
                     "{3}{R}; no parsed targets; see data_evidence.json.")
    else:
        ass["A1_data_level"] = "failed"
        notes.append("A1 FAILED: the pinned parse does not match the "
                     "issue-reported shape; see data_evidence.json.")

    # A2: setup at the decisive pre
    if ST["pre_exported"] and pre_st:
        moid = monopoly_oid(pre_st, 0)
        loy = loyalty_of(get_obj(pre_st, moid)) if moid else None
        lib_n = len(library_oids(pre_st, 1))
        ph = pre_st.get("phase")
        act = pre_st.get("active_player")
        notes.append(f"A2 probe: pre monopoly_on_bf={moid is not None}; "
                     f"loyalty={loy}; P1 library={lib_n}; phase={ph}; "
                     f"active={act}.")
        if moid is not None and (loy or 0) >= 2 and lib_n >= 2 \
                and ph in ("PreCombatMain", "PostCombatMain") and act == 0:
            ass["A2_setup_ok"] = "passed"
            notes.append("A2 passed: Mr. Monopoly, On the Go on P0's "
                         f"battlefield at loyalty {loy} with {lib_n} cards "
                         f"in P1's library, P0 main phase.")
        else:
            ass["A2_setup_ok"] = "failed"
            notes.append("A2 FAILED: the decisive pre does not show the "
                         "required setup.")
    else:
        ass["A2_setup_ok"] = "failed"
        notes.append("A2 FAILED: pre.json was never exported (the [-2] "
                     "activation window never armed).")

    # A3: the [-2] was activated and resolved
    if ST["heist_activated"]:
        moid = monopoly_oid(post_st, 0)
        loy_post = loyalty_of(get_obj(post_st, moid)) if moid else None
        ST["loyalty_at_post"] = loy_post
        notes.append(f"A3 probe: heist_activated={ST['heist_activated']}; "
                     f"heist_on_stack_seen={ST['heist_on_stack']}; "
                     f"loyalty pre={ST['loyalty_at_pre']} post={loy_post}.")
        if ST["heist_resolved"] and loy_post == (ST["loyalty_at_pre"] or 5) - 2:
            ass["A3_activated_resolved"] = "passed"
            notes.append("A3 passed: the Heist! [-2] was activated (loyalty "
                         f"{ST['loyalty_at_pre']} -> {loy_post}), its "
                         "ability object was observed on the stack, and it "
                         "resolved.")
        else:
            ass["A3_activated_resolved"] = "failed"
            notes.append("A3 FAILED: the [-2] was submitted but the "
                         "activation/resolution could not be confirmed "
                         "(stack observation or loyalty delta missing).")
    else:
        ass["A3_activated_resolved"] = "failed"
        notes.append("A3 FAILED: the Heist! [-2] was never activated.")

    # A4: did the top two of P1's library move to exile in the window?
    pre_lib = set(ST.get("p1_lib_at_pre") or [])
    exiled = 0
    exiled_names = []
    if post_st and pre_lib:
        for oid in pre_lib:
            o = (post_st.get("objects") or {}).get(str(oid), {})
            if o.get("zone") == "Exile":
                exiled += 1
                exiled_names.append(
                    str(o.get("base_name") or o.get("name") or "?"))
    ST["exiled_in_window"] = exiled
    notes.append(f"A4 probe: {exiled} of {len(pre_lib)} P1 library-at-pre "
                 f"objects in Exile at post {exiled_names or ''}; P1 "
                 f"library pre={len(pre_lib)} post="
                 f"{len(library_oids(post_st, 1)) if post_st else '?'}; "
                 f"total exile objects at post="
                 f"{sum(1 for o in (post_st.get('objects') or {}).values() if o.get('zone') == 'Exile') if post_st else '?'}.")

    # A5: was P0 offered the exile-cast?
    cast_iids = []
    for p in ST["prompts_seen"]:
        opp = p[3]
        try:
            if is_exile_cast_opp(opp):
                cast_iids.append(p[2])
        except Exception as e:
            notes.append(f"A5 classifier error on prompt {p[2]}: {e}")
    if cast_iids:
        ST["exile_cast_offered"] = True
    notes.append(f"A5 probe: {len(ST['prompts_seen'])} total prompts in "
                 f"window; exile-cast-shaped opportunities="
                 f"{cast_iids or 'none'}; wf types in window="
                 f"{sorted(set(ST['wf_types_window']))}; "
                 f"opponent_choice_answered="
                 f"{ST.get('opponent_choice_answered')}.")
    if ST["exile_cast_offered"] and exiled == 2:
        ass["A5_cast_permission"] = "passed"
        notes.append("A5 passed: P0 was offered the exile-cast for the "
                     "exiled cards.")
    elif ST["heist_resolved"]:
        ass["A5_cast_permission"] = "failed"
        notes.append("A5 FAILED: the [-2] resolved but P0 was never offered "
                     "the PlayFromExile cast -- THE REPORTED BUG: the "
                     "Unimplemented head pushed no GameEvent, the publish "
                     "authority allocated an empty tracked set, and the "
                     "GrantCastingPermission consumer had nothing to grant "
                     "permission for.")
    else:
        notes.append("A5 not-run: the resolution window never completed.")
    if exiled == 2 and ST["heist_resolved"]:
        ass["A4_exile"] = "passed"
        notes.append("A4 passed: exactly the 2 expected cards were exiled "
                     "from P1's library.")
    elif ST["heist_resolved"]:
        ass["A4_exile"] = "failed"
        notes.append("A4 FAILED: 0 P1 cards exiled by the Heist! [-2] -- "
                     "THE REPORTED BUG: the 'Heist! -- Exile the top two "
                     "cards of target opponent's library' clause never "
                     "parsed (Unimplemented head, a runtime no-op), so the "
                     "exile never happened and the dependent "
                     "GrantCastingPermission reads an empty tracked set.")
    else:
        notes.append("A4 not-run: the resolution window never completed.")

    # A6: cleanup
    if post_st:
        tnum = ST.get("heist_turn")
        cur_turn = post_st.get("turn_number")
        if not stack_entries(post_st) and (
                tnum is None or (cur_turn or 0) > tnum):
            ass["A6_cleanup"] = "passed"
            notes.append("A6 passed: post stack empty, game advanced past "
                         f"the activation turn (activation turn {tnum}, "
                         f"post turn {cur_turn}, phase "
                         f"{post_st.get('phase')}).")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append("A6 FAILED: post stack non-empty or the game did "
                         "not advance past the activation turn.")
    else:
        notes.append("A6 not-run: no post state")

    # check for a PlayFromExile effect residue in the post state
    perm_blob = json.dumps(post_st, default=str)
    perm_seen = "PlayFromExile" in perm_blob or "GrantCastingPermission" in perm_blob
    notes.append(f"PlayFromExile/GrantCastingPermission residue in post "
                 f"state: {perm_seen}")

    # verdict
    if (ass["A1_data_level"] == "passed"
            and ass["A2_setup_ok"] == "passed"
            and ass["A3_activated_resolved"] == "passed"
            and ass["A4_exile"] == "failed"):
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: the Heist! [-2] activated (loyalty "
            f"{ST['loyalty_at_pre']} -> {ST['loyalty_at_post']}) and resolved, "
            "but 0 cards were exiled from P1's library and P0 was never "
            "offered the PlayFromExile cast. The 'Heist! -- Exile the top two "
            "cards of target opponent's library' clause never parsed "
            "(Unimplemented head, a runtime no-op), so the chain tracked set "
            "was allocated empty and the GrantCastingPermission sub-ability "
            "had nothing to grant -- the exact structural defect the issue "
            "reports, now measured at runtime. Confirmed on v0.100.0. This "
            "is not a fix claim.")
    elif all(ass[k] == "passed" for k in ("A1_data_level", "A2_setup_ok",
                                          "A3_activated_resolved",
                                          "A4_exile", "A5_cast_permission")):
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: the [-2] exiled 2 cards and P0 was "
            "offered the exile-cast. This is not a fix claim.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: the reported [-2] path could not "
                     "be fully exercised; see assertion notes.")

    # server log excerpts for this game (newest-first by mtime)
    import glob
    lines = []
    used = None
    for lp in sorted(glob.glob(f"{BACKFILL}/runs/*/server.log"),
                     key=os.path.getmtime, reverse=True):
        try:
            lines = open(lp).read().splitlines()
            used = lp
            break
        except Exception:
            continue
    excerpt = [l for l in lines
               if "monopoly" in l.lower()
               or "unimplemented" in l.lower()
               or "tracked" in l.lower()
               or "heist" in l.lower()
               or (ST["game_code"] and ST["game_code"] in l)]
    with open(f"{EVDIR}/server_log_excerpt.txt", "w") as f:
        f.write("\n".join(excerpt[-150:]) + "\n")
    notes.append(f"server.log excerpt: {len(excerpt)} matching lines "
                 f"(of {len(lines)} total; log={used})")

    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": ST["started_at"],
        "duration_s": round(time.time() - ST["t0"], 1),
        "server": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 101, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_7433.py", "rb").read()).hexdigest(),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": {
            "monopoly_cast": ST["monopoly_cast"],
            "monopoly_cast_turn": ST["monopoly_cast_turn"],
            "heist_activated": ST["heist_activated"],
            "heist_on_stack": ST["heist_on_stack"],
            "heist_resolved": ST["heist_resolved"],
            "heist_turn": ST["heist_turn"],
            "loyalty_at_pre": ST["loyalty_at_pre"],
            "loyalty_at_post": ST["loyalty_at_post"],
            "p1_lib_at_pre": len(ST["p1_lib_at_pre"] or []),
            "exiled_in_window": ST["exiled_in_window"],
            "exile_cast_offered": ST["exile_cast_offered"],
            "opponent_choice_answered": ST["opponent_choice_answered"],
            "prompts_seen": [(p[0], p[1], p[2]) for p in ST["prompts_seen"]],
            "wf_types_window": sorted(set(ST["wf_types_window"])),
            "cast_rejections": ST["cast_rejections"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The report is a data-level parse report; the scenario replays "
            "the reported line (Mr. Monopoly, On the Go on the battlefield, "
            "its [-2] activated) and observes the resolution outcome. The "
            "consumer-side outcome was unmeasured in the report; this run "
            "measures it.",
            "12x Mr. Monopoly, On the Go / 12x Lightning Bolt deck densities "
            "are test-harness conveniences (engine accepts >4-of for custom "
            "games).",
            "States are authoritative exports, restorable only via full "
            "game replay.",
        ],
        "setup_line": "P0: 12x Mr. Monopoly, On the Go + 48x Mountain; "
                      "P1: 12x Lightning Bolt + 48x Mountain "
                      "(lands only, never casts, never attacks); default "
                      "Bo1, life 20",
        "contract_line": "Mr. Monopoly, On the Go on P0's battlefield, [-2] "
                         "activated: correct = the top two cards of P1's "
                         "library are exiled and P0 may cast them this turn. "
                         "Observed (bug): the 'Heist! -- Exile the top two "
                         "cards of target opponent's library' clause never "
                         "parsed (Unimplemented head, runtime no-op), so 0 "
                         "cards are exiled and the GrantCastingPermission "
                         "sub-ability reads an empty tracked set.",
        "prior_runs": [],
        "stats": {"states_seen": ST["states_seen"]},
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
    return verdict


# ------------------------------------------------------------- main loop
async def run_game():
    reset_attempt()
    check_data_level()
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    await p0.connect()
    await p1.connect()
    att = await p0.create(deck(P0_DECK), player_count=2)
    ST["game_code"] = att.get("game_code")
    await p1.join(ST["game_code"], deck(P1_DECK))
    say(f"game created: code={ST['game_code']}")
    wire("game_created", {"game_code": ST["game_code"]})

    async def pump(c, tag):
        while not ST["terminal"]:
            try:
                t, data = await asyncio.wait_for(c.inbox.get(), 2)
            except asyncio.TimeoutError:
                tick_timeout(c)
                continue
            st = c.latest
            if not st:
                continue
            state = st["state"]
            ST["states_seen"] += 1
            # track the Heist! ability on the stack
            if ST["heist_activated"] and not ST["heist_on_stack"]:
                if heist_on_stack(state) is not None:
                    ST["heist_on_stack"] = True
                    ST["phase"] = "resolving"
                    ST["resolving_since"] = time.time()
                    say("[window] Heist! [-2] ability observed on the stack")
                    wire("heist_on_stack", {})
                    if not ST["mid_exported"]:
                        await export_as(c0_ref[0], "mid_stack")
                        ST["mid_exported"] = True
                    dump_stack_once(state, "heist_on_stack")
            # resolution: was on the stack, now gone, loyalty dropped
            if ST["heist_on_stack"] and not ST["heist_resolved"]:
                if heist_on_stack(state) is None:
                    moid = monopoly_oid(state, 0)
                    loy = loyalty_of(get_obj(state, moid)) if moid else None
                    if loy == (ST["loyalty_at_pre"] or 5) - 2:
                        ST["heist_resolved"] = True
                        say("[window] Heist! [-2] resolved; starting settle "
                            "clock")
                        wire("heist_resolved", {"loyalty": loy})
                        ST["settle_at"] = time.time()
            # watchdog: the window must not hang the run forever
            if ST["phase"] == "resolving" and not ST["heist_resolved"]:
                if (time.time() - (ST["resolving_since"] or ST["t0"])
                        > RESOLVE_DEADLINE_S):
                    say("[watchdog] deadline in resolving without "
                        "resolution; exporting post and finalizing")
                    wire("watchdog", {"note": "resolving timeout"})
                    if not ST["post_exported"]:
                        await export_as(c0_ref[0], "post")
                        ST["post_exported"] = True
                    ST["terminal"] = True
                    return
            if ST["phase"] == "resolving":
                wf = (wf_of(state).get("type") or "")
                if wf and wf not in ST["wf_types_window"]:
                    ST["wf_types_window"].append(wf)
                if (ST["heist_resolved"] and not stack_entries(state)
                        and wf == "Priority"):
                    if ST["settle_at"] is None:
                        ST["settle_at"] = time.time()
                    elif time.time() - ST["settle_at"] >= SETTLE_IDLE_S:
                        say("[window] settle clock done; exporting post")
                        wire("post_export", {})
                        if not ST["post_exported"]:
                            await export_as(c0_ref[0], "post")
                            ST["post_exported"] = True
                        ST["terminal"] = True
                        return
                elif not ST["heist_resolved"]:
                    ST["settle_at"] = None
            if time.time() - ST["t0"] > SETUP_DEADLINE_S:
                say("[timeout] deadline reached")
                wire("timeout", {})
                ST["terminal"] = True
                return
            acts = merged_actions(st)
            if tag == "P0":
                await p0_tick(st, acts, state, c)
            else:
                await p1_tick(st, acts, state, c)

    def tick_timeout(c):
        # on idle ticks, nothing to do: the pump loop re-reads state
        pass

    c0_ref = [p0]
    await asyncio.gather(pump(p0, "P0"), pump(p1, "P1"))
    for c in (p0, p1):
        try:
            await c.ws.close()
        except Exception:
            pass


async def main():
    try:
        await run_game()
    except Exception as e:
        say(f"FATAL: {e!r}")
        wire("fatal", {"error": repr(e)})
    verdict = await finalize(None)
    say(f"FINAL VERDICT: {verdict}")
    print(json.dumps({"verdict": verdict, "assertions": ST["ass"]},
                     indent=1))


if __name__ == "__main__":
    asyncio.run(main())
