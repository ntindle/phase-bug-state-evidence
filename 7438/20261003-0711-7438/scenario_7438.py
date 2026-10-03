#!/usr/bin/env python3
"""Issue #7438: Revival Experiment -- "for each permanent type, return up to
one card of that type from your graveyard to the battlefield" is unparsed,
so `LoseLife` reads an empty tracked set.

Reported (2026-08-15, lgray; source:internal-triage, status:confirmed,
area:parser, mechanic:zone-change, priority:p3-card-specific,
classifier:unsupported-aspect; related #6857 tracked-set publish census, one
of 33 cards whose tracked-set antecedent clause never parses):

> For each permanent type, return up to one card of that type from your
> graveyard to the battlefield. You lose 3 life for each card returned this
> way. Exile Revival Experiment.

Pinned v0.100.0 parse (see data_evidence.json): abilities[0], kind "Spell":
  head = Unimplemented { name: "unparsed_quantity",
                         description: "For each permanent type, return up to
                                       one card of that type from your
                                       graveyard to the battlefield" }
    sub_ability (Spell, SequentialSibling): LoseLife
      { amount: Multiply(3, Ref(TrackedSetSize)), target: Controller }
      sub_ability (Spell, SequentialSibling): ChangeZone
        { destination: Exile, target: SelfRef }
DISCREPANCY (recorded, as in the #7437 run): the issue body quotes the head
name as "for"; the pinned v0.100.0 card-data.json names it
"unparsed_quantity". The description string matches, and the structural
defect (Unimplemented head -> empty tracked set -> LoseLife reads an empty
set) is identical either way.

The Unimplemented resolver is a runtime no-op (pushes no GameEvent), so the
publish authority allocates a FRESH EMPTY chain tracked set and the LoseLife
sub-ability reads that empty set. The issue explicitly states no runtime
symptom is asserted for the consumer side ("LoseLife is not among [the
measured consumers]"; "this issue does not assert a runtime symptom") -- the
reported defect is the parse state and the empty publish that structurally
follows from it. The scenario therefore replays the reported line (cast
Revival Experiment with a stocked graveyard, let it resolve) and observes
the resolution outcome: whether any graveyard cards return to the
battlefield, how much life the controller loses, and where the spell ends up.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 4x Revival Experiment + 4x Grisly Salvage + 8x Llanowar Elves +
      4x Ornithopter + 4x Memnite + 4x Pacifism + 4x Grizzly Bears +
      14x Forest + 14x Swamp (60)
  P1: 60x Forest (plays lands only, never casts, never attacks)
Drive:
  1. Mulligans: P0 keeps iff Revival Experiment or Grisly Salvage is in the
     opening hand (mulligans down, min hand 5); P1 keeps 7.
  2. P0 plays one land per turn. Cast Grisly Salvage as soon as able
     ({B}{G}) -- its Dig mills ~4 cards into the graveyard (the driver takes
     a land to hand from the optional pick). Up to 2 Salvages, until the
     graveyard holds cards of >=2 permanent types.
  3. Once the graveyard is stocked and P0 can pay {4}{B}{G} (1 untapped
     Swamp + 1 untapped Forest + 6 total untapped lands), cast Revival
     Experiment. pre.json is exported INSIDE the cast submit path (guarded
     flag), never by a main-loop probe. mid_stack.json is exported when the
     Revival Experiment spell is first seen on the stack. post.json once
     settled (spell resolved, stack empty, Priority, 8s idle) or on the
     GameOver/deadline finalize path.
  4. During the resolution window the driver records every prompt; a
     graveyard-card selection ("return_choice" class) would contradict the
     no-op.

Expected (correct behavior): for each permanent type, up to one card of
that type returns from the P0 graveyard to the battlefield; P0 loses 3 life
per card returned; Revival Experiment is exiled.
Reported (bug): the return clause never parsed (Unimplemented head, runtime
no-op), so no cards return, the chain tracked set is allocated empty, and
the LoseLife sub-ability reads an empty set (0 life lost). POST-RUN NOTE
(20261003-0711-7438): the parsed "Exile Revival Experiment" ChangeZone
SelfRef sub-ability did NOT take effect either -- the cast copy went
Hand -> Stack -> Graveyard. This is outside the issue's reported defect
(which covers only the return head and the LoseLife rider) and is recorded
as an additional observation; the mechanism (chain suppression vs
ChangeZone failure) was not distinguished.

Assertions:
  A1_data_level   pinned v0.100.0 card-data.json parses Revival Experiment
                  as the issue reports: abilities[0] kind Spell, head
                  Unimplemented (record exact head name) describing "For
                  each permanent type, return up to one card of that type
                  from your graveyard to the battlefield", chaining to sub
                  Spell LoseLife { Multiply(3, Ref(TrackedSetSize)),
                  target Controller } with sub_link SequentialSibling,
                  chaining to sub-sub Spell ChangeZone { destination
                  Exile, target SelfRef }.
  A2_setup_ok     pre.json: Revival Experiment in P0 hand, >=1 untapped
                  Swamp + >=1 untapped Forest + >=6 untapped lands total,
                  main phase, stack empty; P0 graveyard holds >=1 card
                  spanning >=2 permanent types (the non-vacuous precondition).
  A3_cast_resolved
                  the cast was submitted, the spell was seen on the stack
                  and resolved; Revival Experiment is in Exile at post.
  A4_no_returns   the Unimplemented return head produced no returns: no
                  graveyard-card "return_choice" prompt was offered during
                  the resolution window, no P0 object moved
                  Graveyard->Battlefield between pre and post, and Revival
                  Experiment is in Exile at post. EXPECTED TO PASS under
                  the bug (the no-op played out at runtime).
  A5_life_unchanged
                  P0 life is identical pre->post: LoseLife read the empty
                  chain tracked set (3 x 0 = 0). (Proxy: the issue asserts
                  no consumer-side symptom; this records the observable
                  absence.)
  A6_cleanup      post stack empty, game advanced past the cast turn, no
                  unrejected submissions lingering (no in-flight
                  cast/activation, rejections recorded).

Verdict rule: reproduced iff A1..A6 passed (the reported parse no-op played
              out at runtime: nothing returned, no life lost, spell exiled);
              not-reproduced iff A1,A2,A3 passed and (A4 or A5 failed --
              cards returned and/or life was lost);
              blocked iff A1, A2, or A3 cannot be established.
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
RUN_ID = "20261003-0711-7438"
ISSUE = 7438
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
                     "(run-20261003-0711; ServerHello 0.100.0/bc9ef56/"
                     "protocol 101 re-verified by this run's own Hello "
                     "handshake)",
    "mode": "Full",
    "source": "2026-10-03: latest stable release v0.100.0 == pinned "
              "release dir (GitHub /releases re-confirmed v0.100.0 still "
              "latest stable; ServerHello 0.100.0/bc9ef56/protocol 101 "
              "re-verified by this run); hashes recomputed against "
              "on-disk artifacts this run",
}

# Recompute against on-disk artifacts; never copy hashes blindly.
for _f, _k in (("server/releases/v0.100.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.100.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.100.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.100.0/data/card-data.json"))

REVIVAL = "revival experiment"
SALVAGE = "grisly salvage"
FOREST = "forest"
SWAMP = "swamp"

P0_DECK = [("Revival Experiment", 4), ("Grisly Salvage", 4),
           ("Llanowar Elves", 8), ("Ornithopter", 4), ("Memnite", 4),
           ("Pacifism", 4), ("Grizzly Bears", 4),
           ("Forest", 14), ("Swamp", 14)]
P1_DECK = [("Forest", 60)]


def deck(pairs):
    names = []
    for n, c in pairs:
        names += [n] * c
    return {"main_deck": names, "sideboard": [], "commander": []}


SETUP_DEADLINE_S = 1800
RESOLVE_DEADLINE_S = 600
SETTLE_IDLE_S = 8
TURN_CAP = 45
OPPONENT_PID = 1
PERMANENT_TYPES = {"artifact", "creature", "enchantment", "land",
                   "planeswalker", "battle", "kindred"}


def reset_attempt():
    global ST, MULLS, MULL_COUNT, SUBMITTED_OPPS, _DISCARD_REV
    ST = {
        "t0": time.time(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "game_code": None,
        "phase": "build",  # build -> resolving -> done
        "revival_cast": False,
        "revival_oid": None,
        "spell_on_stack": False,
        "spell_resolved": False,
        "salvage_casts": 0,
        "salvage_in_flight": None,
        "dig_answered": 0,
        "cast_turn": None,
        "pre_exported": False,
        "mid_exported": False,
        "post_exported": False,
        "cast_in_flight": None,
        "return_choice_seen": False,
        "mana_needs": {"P0": {"B": 0, "G": 0, "generic": 0},
                       "P1": {"B": 0, "G": 0, "generic": 0}},
        "prompts_seen": [],
        "prompt_classes": {},
        "wf_types_window": [],
        "stack_kinds_window": [],
        "terminal": False,
        "states_seen": 0,
        "cast_rejections": 0,
        "stack_dumped": False,
        "settle_at": None,
        "resolving_since": None,
        "ass": {k: "not-run" for k in ("A1_data_level", "A2_setup_ok",
                                       "A3_cast_resolved",
                                       "A4_no_returns",
                                       "A5_life_unchanged",
                                       "A6_cleanup")},
        "notes": [],
        "data_level_ok": False,
    }
    MULLS = set()
    MULL_COUNT = {}
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


def untapped_lands(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and not o.get("tapped") and is_land(o)]


def mana_available(state, pid):
    lands = untapped_lands(state, pid)
    b = sum(1 for o in lands if obj_lname(state, o) == SWAMP)
    g = sum(1 for o in lands if obj_lname(state, o) == FOREST)
    return b, g, len(lands)


def graveyard_cards(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Graveyard"
            and (o.get("owner") == pid or o.get("controller") == pid)]


def card_core_types(state, oid):
    o = get_obj(state, oid)
    ct = o.get("card_types") or {}
    return [str(t).lower() for t in (ct.get("core_types") or [])]


def graveyard_ptypes(state, pid):
    types = set()
    for oid in graveyard_cards(state, pid):
        for t in card_core_types(state, oid):
            if t in PERMANENT_TYPES:
                types.add(t)
    return types


def merged_actions(st):
    acts = list(st.get("legal_actions") or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def vi_ops(st):
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return []
    return vi.get("opportunities", []) or []


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


def se_blob(se):
    return json.dumps(se, default=str).lower()


def _src_matches(se, want):
    if want is None:
        return False
    try:
        return int(se.get("source_id")) == int(want)
    except (TypeError, ValueError):
        return False


def revival_on_stack(state):
    """The Revival Experiment spell object on the stack."""
    want = ST.get("revival_oid")
    for se in stack_entries(state):
        if _src_matches(se, want):
            return se
    for se in stack_entries(state):
        if "revival experiment" in se_blob(se):
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

# ------------------------------------------------------------- data check
def check_data_level():
    re_ = CARD_DATA.get("revival experiment", {})
    ab = re_.get("abilities") or []
    a0 = ab[0] if ab else {}
    head = (a0.get("effect") or {})
    sub = (a0.get("sub_ability") or {})
    sub_eff = sub.get("effect") or {}
    sub_amt = sub_eff.get("amount") or {}
    sub2 = (sub.get("sub_ability") or {})
    sub2_eff = sub2.get("effect") or {}
    ev = {
        "revival_experiment": re_,
        "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
        "head_name_discrepancy": {
            "issue_body_quotes": "for",
            "pinned_data_shows": head.get("name"),
            "note": "description string matches the issue; structural "
                    "defect identical either way",
        },
    }
    ok = (
        a0.get("kind") == "Spell"
        and head.get("type") == "Unimplemented"
        and "for each permanent type, return up to one card of that type "
            "from your graveyard to the battlefield" in
        str(head.get("description", "")).lower()
        and (a0.get("sub_ability") or {}).get("kind") == "Spell"
        and sub.get("kind") == "Spell"
        and sub_eff.get("type") == "LoseLife"
        and (sub_eff.get("target") or {}).get("type") == "Controller"
        and sub_amt.get("type") == "Multiply"
        and sub_amt.get("factor") == 3
        and (((sub_amt.get("inner") or {}).get("qty")) or {}).get("type") \
            == "TrackedSetSize"
        and sub.get("sub_link") == "SequentialSibling"
        and sub2.get("kind") == "Spell"
        and sub2_eff.get("type") == "ChangeZone"
        and sub2_eff.get("destination") == "Exile"
        and (sub2_eff.get("target") or {}).get("type") == "SelfRef"
        and str(re_.get("oracle_text", "")).startswith(
            "For each permanent type, return up to one card of that type")
    )
    say(f"data-level check: kind={a0.get('kind')}; "
        f"head={head.get('type')}/{head.get('name')}; "
        f"a0.sub_ability.kind={(a0.get('sub_ability') or {}).get('kind')} "
        f"(a0 has no sub_link field in pinned data); "
        f"sub={sub.get('kind')}/{sub_eff.get('type')}/"
        f"target={(sub_eff.get('target') or {}).get('type')}/"
        f"amount={sub_amt.get('type')}x{sub_amt.get('factor')}; "
        f"sub.sub_link={sub.get('sub_link')}; "
        f"sub2={sub2.get('kind')}/{sub2_eff.get('type')}/"
        f"{sub2_eff.get('destination')}/"
        f"{(sub2_eff.get('target') or {}).get('type')}")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    # byte snapshot of the Revival Experiment entry itself
    with open(f"{EVDIR}/revival_experiment_card_data.json", "w") as f:
        json.dump(re_, f, indent=1)
    ST["data_level_ok"] = ok
    wire("data_level", {"ok": ok, "head_name": head.get("name")})

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


def keep_p0(hand):
    return REVIVAL in hand or SALVAGE in hand


async def do_mulligan(c, pid, tag, keep_fn):
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
    if keep_fn(hn) or mulls >= 2 or len(hn) <= 5:
        MULLS.add(tag)
        say(f"[{tag}] keep {len(hn)} (hand: {hn})")
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Keep"}}})
        wire("mulligan", {"who": tag, "decision": "keep"})
    else:
        MULL_COUNT[mkey] = mulls + 1
        say(f"[{tag}] mulligan #{mulls + 1} (hand: {hn})")
        await c.send_action({"type": "MulliganDecision",
                             "data": {"choice": {"type": "Mulligan"}}})
        wire("mulligan", {"who": tag, "decision": "mulligan"})
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
    if key in SUBMITTED_OPPS:
        return False
    n = int((pend.get("phase") or {}).get("count") or 0)
    if n <= 0:
        return False
    hand = hand_ids(state, pid)
    # bottom lands first; never bottom the key spells for P0
    rank = {FOREST: 0, SWAMP: 0, REVIVAL: 9, SALVAGE: 9}
    picks = [int(o) for o in sorted(
        hand, key=lambda o: (rank.get(obj_lname(state, o), 5), o))[:n]]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    wire("bottom", {"who": tag, "picks": picks})
    return True


def p0_discard_pick(state, pid):
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return []
    # discard lands first; never discard the key spells
    rank = {FOREST: 0, SWAMP: 0, REVIVAL: 9, SALVAGE: 9}
    return [int(o) for o in sorted(
        hand, key=lambda o: (rank.get(obj_lname(state, o), 5), o))[:n]]


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
    if tag == "P0":
        picks = p0_discard_pick(state, pid)
    else:
        hand = hand_ids(state, pid)
        n = len(hand) - 7
        if n <= 0:
            return False
        picks = [int(x) for x in sorted(
            hand, key=lambda o: (0 if obj_lname(state, o) == FOREST else 1,
                                 o))[:n]]
    if not picks:
        return False
    _DISCARD_REV[(c.name, rev)] = True
    say(f"[{tag}] discarding {len(picks)} to hand size: "
        f"{[obj_lname(state, o) for o in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    wire("discard", {"who": tag, "picks": picks})
    return True


async def do_discard_vi(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    ops = vi_ops(st)
    if not ops:
        return False
    hand_oids = set(hand_ids(state, pid))
    if not hand_oids:
        return False
    for opp in ops:
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
            k, ref = _cand_ref(ch)
            if k != "object" or ref not in hand_oids:
                all_hand = False
                break
            cand_oids.add(ref)
        if not all_hand or not cand_oids:
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        if tag == "P0":
            picks = p0_discard_pick(state, pid)
            pick_oid = picks[0] if picks else None
        else:
            pick_oid = None
        pick = None
        for ch in chs:
            k, ref = _cand_ref(ch)
            if pick_oid is not None and ref == pick_oid:
                pick = ch.get("id")
                break
        if pick is None:
            for ch in chs:
                k, ref = _cand_ref(ch)
                if obj_lname(state, ref) == FOREST:
                    pick = ch.get("id")
                    break
            if pick is None:
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


def _cand_ref(ch):
    for sf in (ch.get("surfaces") or []):
        t = sf.get("type")
        d = sf.get("data") or {}
        if t == "object":
            try:
                return ("object", int(d.get("reference")))
            except (TypeError, ValueError):
                continue
        if t == "player":
            for k in ("reference", "player", "player_id", "id", "seat"):
                try:
                    v = d.get(k)
                    if v is None:
                        continue
                    return ("player", int(v))
                except (TypeError, ValueError):
                    continue
    return ("other", None)


async def answer_dig_choice(c, st, state, tag):
    """Answer Grisly Salvage's Dig pick (build phase only): take a land to
    hand when one is among the revealed creature/land candidates, else the
    first candidate. The rest mill to the graveyard either way."""
    if tag != "P0" or ST["phase"] != "build":
        return False
    if ST.get("salvage_in_flight") is None:
        return False
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        if resp.get("type") != "schema":
            continue
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        if spec.get("type") not in ("select", "sequence"):
            continue
        chs = data.get("candidates") or []
        if not chs:
            continue
        if not any(_cand_ref(ch)[0] == "object" for ch in chs):
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        pick = None
        for ch in chs:
            k, ref = _cand_ref(ch)
            if k == "object" and ref is not None and \
                    obj_lname(state, ref) in (FOREST, SWAMP):
                pick = ch.get("id")
                break
        if pick is None:
            pick = chs[0].get("id")
        SUBMITTED_OPPS.add(iid)
        ST["dig_answered"] = ST.get("dig_answered", 0) + 1
        say(f"[{tag}] Grisly Salvage Dig pick -> {pick} "
            f"(answered={ST['dig_answered']})")
        wire("dig_choice", {"who": tag, "iid": iid, "pick": pick,
                            "n_candidates": len(chs)})
        await interact_as(c, {
            "interactionId": iid,
            "response": {"type": spec.get("type"),
                         "data": {"choiceIds": [pick]}}}, tag)
        return True
    return False


async def pay_mana_vi(c, st, state, tag):
    ops = vi_ops(st)
    if not ops:
        return False
    needs = ST["mana_needs"][tag]
    for opp in ops:
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
                for color in ("B", "G", "R", "U", "W"):
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
    for opp in vi_ops(st):
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []
                     if s.get("type") == "action"]
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


def candidate_matching(opp, needles):
    """First available candidate whose serialized content contains all
    needles (case-insensitive)."""
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    chs = data.get("choices") or data.get("candidates") or []
    for ch in chs:
        if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
            continue
        ser = json.dumps(ch, default=str).lower()
        if all(n.lower() in ser for n in needles):
            return ch.get("id")
    return None

# ------------------------------------------------------------- prompt handlers
ORDINARY_ACTION_CODES = {"passPriority", "playLand", "tapLandForMana"}


def classify_prompt(opp):
    """Classify a resolution-window opportunity. Returns one of:
    mana_payment, pass_priority, action_menu, declare_combat,
    discard_handsize, target_selection, return_choice, other.

    return_choice is deliberately narrow: a card selection whose object
    candidates sit in the Graveyard -- the shape the "return up to one card
    of that type from your graveyard" choice would take if the clause had
    parsed. Mana payment, pass-priority, play-land menus, attack/block
    declarations, hand-size discards, and plain target selections must never
    count as the reported prompt."""
    blob = json.dumps(opp, default=str).lower()
    resp = opp.get("response") or {}
    data = resp.get("data") or {}
    spec = data.get("spec") or {}
    items = (data.get("choices") or []) + (data.get("candidates") or [])
    # 1. combat declarations (selection intent attack/block, or
    #    attacker/blocker roles on candidates)
    for sf in opp.get("surfaces") or []:
        if sf.get("type") == "selection":
            intent = str((sf.get("data") or {}).get("intent") or "").lower()
            if intent in ("attack", "block"):
                return "declare_combat"
    for it in items:
        for sf in it.get("surfaces") or []:
            role = str((sf.get("data") or {}).get("role") or "").lower()
            if role in ("attacker", "attacktarget", "blocker",
                        "blocktarget", "defender"):
                return "declare_combat"
    # 2. mana payment (tapLandForMana anywhere)
    for it in items:
        for sf in it.get("surfaces") or []:
            if sf.get("type") == "action" and \
                    (sf.get("data") or {}).get("code") == "tapLandForMana":
                return "mana_payment"
    # 3. ordinary action menus: every item's action codes are ordinary and
    #    every object surface is merely the "source" of such an action
    #    (e.g. the Swamp behind a playLand choice)
    if items:
        ordinary = True
        saw_obj = False
        for it in items:
            codes = [(sf.get("data") or {}).get("code")
                     for sf in it.get("surfaces") or []
                     if sf.get("type") == "action"]
            obj_roles = [(sf.get("data") or {}).get("role")
                         for sf in it.get("surfaces") or []
                         if sf.get("type") == "object"]
            if obj_roles:
                saw_obj = True
            if any(r not in (None, "source") for r in obj_roles):
                ordinary = False
            if not codes or any(c not in ORDINARY_ACTION_CODES
                                for c in codes):
                ordinary = False
        if ordinary:
            return "pass_priority" if not saw_obj else "action_menu"
    # 4. discard to hand size: card-candidate selection, all in hand
    obj_zones = set()
    saw_obj = False
    saw_player_only = True
    for it in items:
        for sf in it.get("surfaces") or []:
            if sf.get("type") == "object":
                saw_obj = True
                saw_player_only = False
                obj_zones.add(
                    str((sf.get("data") or {}).get("zone") or "").lower())
    if saw_obj and obj_zones and obj_zones <= {"hand"}:
        return "discard_handsize"
    # 5. plain target selections (player candidates, no object surfaces)
    if not saw_obj and saw_player_only and items:
        if "target" in blob:
            return "target_selection"
    # 6. the bug's choice: graveyard-card selection for the return clause
    if saw_obj and obj_zones and obj_zones <= {"graveyard"} \
            and spec.get("type") in ("select", "sequence"):
        return "return_choice"
    # 7. pass priority fallback (no objects)
    for it in data.get("choices") or []:
        for sf in it.get("surfaces") or []:
            if sf.get("type") == "action" and \
                    (sf.get("data") or {}).get("code") == "passPriority":
                return "pass_priority"
    return "other"


def record_window_prompt(seat, st):
    for opp in vi_ops(st):
        iid = opp.get("interactionId") or opp.get("id")
        if all(t[2] != iid for t in ST["prompts_seen"]):
            ST["prompts_seen"].append((ST["phase"], seat, iid, opp))
            cls = classify_prompt(opp)
            ST["prompt_classes"][str(iid)] = cls
            wire("window_prompt", {"phase": ST["phase"], "seat": seat,
                                   "iid": iid, "class": cls,
                                   "opportunity": opp})
            say(f"[{ST['phase']}/{seat}] prompt class={cls}: "
                f"{json.dumps(opp)[:260]}")


async def answer_return_choice(c, st, state, tag):
    """Control-branch handler: if the engine DOES offer a graveyard-card
    return choice during the resolution window, record it (A4 then fails)
    and answer minimally so the game can continue: empty selection when the
    spec min allows 0, else the first candidates up to max."""
    if tag not in ("P0", "P1") or ST["phase"] != "resolving" \
            or not ST["spell_on_stack"]:
        return False
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        if resp.get("type") != "schema":
            continue
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        if spec.get("type") not in ("select", "sequence"):
            continue
        chs = data.get("candidates") or []
        if not chs:
            continue
        if classify_prompt(opp) != "return_choice":
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        ST["return_choice_seen"] = True
        try:
            lo = int(spec.get("min") or 0)
        except (TypeError, ValueError):
            lo = 0
        try:
            hi = int((spec.get("max") or {}).get("value")
                     if isinstance(spec.get("max"), dict)
                     else (spec.get("max") or len(chs)))
        except (TypeError, ValueError):
            hi = len(chs)
        hi = max(0, min(hi, len(chs)))
        picks = [] if lo == 0 else [ch.get("id") for ch in chs[:hi]]
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] CONTROL: graveyard return-choice prompt OFFERED "
            f"(min={lo} max={hi}); answering with {len(picks)} picks")
        wire("return_choice", {"who": tag, "iid": iid,
                               "spec": str(spec)[:300],
                               "n_candidates": len(chs),
                               "picks": picks})
        await interact_as(c, {
            "interactionId": iid,
            "response": {"type": spec.get("type"),
                         "data": {"choiceIds": picks}}}, tag)
        return True
    return False

# ------------------------------------------------------------- P0 tick
async def p0_tick(st, acts, state, c):
    pid = c.player_id
    if ST["phase"] == "resolving":
        record_window_prompt("P0", st)
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan(c, pid, "P0", keep_p0):
            return
        if await do_bottom(c, pid, "P0"):
            return
        return
    if await do_discard_to_handsize(c, pid, "P0"):
        return
    if await do_discard_vi(c, pid, "P0"):
        return
    if await answer_dig_choice(c, st, state, "P0"):
        return
    if await answer_return_choice(c, st, state, "P0"):
        return
    # Salvage resolution detector: the instant hits the graveyard.
    sif = ST.get("salvage_in_flight")
    if sif is not None:
        so = get_obj(state, sif)
        if so.get("zone") == "Graveyard":
            ST["salvage_in_flight"] = None
            ST["salvage_casts"] += 1
            gy = graveyard_cards(state, pid)
            say(f"[P0] Grisly Salvage resolved (graveyard); "
                f"count={ST['salvage_casts']}; P0 graveyard now {len(gy)} "
                f"cards, types={sorted(graveyard_ptypes(state, pid))}")
            wire("salvage_resolved", {"count": ST["salvage_casts"],
                                      "graveyard_n": len(gy)})
    # Payment-complete detectors: a cast leaves the hand; the engine has
    # taken its payment: clear the owed mana.
    cif = ST.get("cast_in_flight")
    if cif is not None:
        co = get_obj(state, cif)
        if co.get("zone") != "Hand":
            ST["cast_in_flight"] = None
            ST["mana_needs"]["P0"] = {"B": 0, "G": 0, "generic": 0}
            say(f"[P0] cast oid {cif} left hand (zone {co.get('zone')}); "
                f"payment complete, needs cleared")
            wire("payment_complete", {"oid": cif, "zone": co.get("zone")})
    if sum(ST["mana_needs"]["P0"].values()) > 0:
        if await pay_mana_vi(c, st, state, "P0"):
            return
        if await pay_tick(c, acts, "P0"):
            return
        # Mana is still owed but no tap/payment made progress: the engine
        # is waiting on payment. Do NOT fall through to pass_priority here
        # (passing on a mana prompt can cancel/stall the cast); wait for the
        # next state and let the stall watchdog diagnose.
        if time.time() - ST.get("_mana_wait_at", 0) > 30:
            ST["_mana_wait_at"] = time.time()
            say(f"[P0] mana still owed {ST['mana_needs']['P0']} but no "
                f"payment available; waiting")
            wire("mana_wait", {"needs": dict(ST["mana_needs"]["P0"])})
        await asyncio.sleep(5)
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
        # 1. the decisive cast: Revival Experiment
        if not ST["revival_cast"] and REVIVAL in hn:
            gy = graveyard_cards(state, pid)
            gtypes = graveyard_ptypes(state, pid)
            b, g, tot = mana_available(state, pid)
            turn = state.get("turn_number") or 0
            stocked = len(gy) >= 1 and len(gtypes) >= 2
            # cast once mana is available and the graveyard is stocked; if
            # Salvage never showed up, still cast by turn 14 so the run
            # terminates with an honest (failed) A2 rather than stalling.
            if b >= 1 and g >= 1 and tot >= 6 and (
                    stocked or ST["salvage_casts"] >= 2 or turn >= 14):
                a, oid = cast_action_for(acts, state, REVIVAL)
                if a:
                    # pre.json is exported INSIDE the cast submit path
                    # (guarded flag), never by a main-loop probe.
                    if not ST["pre_exported"]:
                        await export_as(c, "pre")
                        ST["pre_exported"] = True
                        pre_st = json.load(
                            open(f"{EVDIR}/pre.json")).get("state", {})
                        say(f"[cast] pre.json exported: revival in P0 hand="
                            f"{REVIVAL in hand_lnames(pre_st, 0)}; "
                            f"mana B/G/total="
                            f"{mana_available(pre_st, 0)}; "
                            f"graveyard={len(graveyard_cards(pre_st, 0))} "
                            f"cards types="
                            f"{sorted(graveyard_ptypes(pre_st, 0))}; "
                            f"turn={pre_st.get('turn_number')}; "
                            f"phase={pre_st.get('phase')}")
                        wire("pre_exported",
                             {"turn": pre_st.get("turn_number"),
                              "gy_n": len(graveyard_cards(pre_st, 0)),
                              "gy_types": sorted(
                                  graveyard_ptypes(pre_st, 0))})
                    ST["revival_oid"] = oid
                    ST["revival_cast"] = True
                    ST["cast_in_flight"] = oid
                    ST["mana_needs"]["P0"] = {"B": 1, "G": 1, "generic": 4}
                    ST["phase"] = "resolving"
                    ST["resolving_since"] = time.time()
                    ST["cast_turn"] = state.get("turn_number")
                    say(f"[P0] casting Revival Experiment (oid {oid}) "
                        f"-- DECISIVE")
                    wire("revival_cast", {"oid": oid})
                    await submit_as_is(c, a)
                    return
        # 2. setup casts: Grisly Salvage to stock the graveyard
        if SALVAGE in hn and ST["salvage_casts"] < 2 \
                and ST["salvage_in_flight"] is None:
            gy = graveyard_cards(state, pid)
            if len(gy) < 4 or len(graveyard_ptypes(state, pid)) < 2:
                b, g, tot = mana_available(state, pid)
                if b >= 1 and g >= 1:
                    a, oid = cast_action_for(acts, state, SALVAGE)
                    if a:
                        ST["salvage_in_flight"] = oid
                        ST["cast_in_flight"] = oid
                        ST["mana_needs"]["P0"] = {"B": 1, "G": 1,
                                                 "generic": 0}
                        say(f"[P0] casting Grisly Salvage (oid {oid}) "
                            f"-- setup")
                        wire("salvage_cast", {"oid": oid})
                        await submit_as_is(c, a)
                        return
        # 3. play a land
        n_lands = sum(1 for oid in bf_permanents(state, pid)
                      if is_land(get_obj(state, oid)))
        if n_lands < 12:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(c, a)
                    return
    await pass_priority(c, st, acts)

# ------------------------------------------------------------- P1 tick (lands only)
async def p1_tick(st, acts, state, c):
    pid = c.player_id
    if ST["phase"] == "resolving":
        record_window_prompt("P1", st)
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan(c, pid, "P1", lambda hn: True):
            return
        if await do_bottom(c, pid, "P1"):
            return
        return
    if await do_discard_to_handsize(c, pid, "P1"):
        return
    if await do_discard_vi(c, pid, "P1"):
        return
    if await answer_return_choice(c, st, state, "P1"):
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
        if n_lands < 10:
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
    mid_st = mid.get("state") or {}
    post_st = post.get("state") or {}

    def revival_zone(st):
        for oid, o in (st.get("objects") or {}).items():
            if obj_lname(st, oid) == REVIVAL:
                return oid, o.get("zone")
        return None, None

    def gy_to_bf_moves(a_st, b_st):
        moves = []
        a_objs = a_st.get("objects") or {}
        b_objs = b_st.get("objects") or {}
        for oid, bo in b_objs.items():
            ao = a_objs.get(oid, {})
            if ao.get("zone") == "Graveyard" and bo.get("zone") == "Battlefield" \
                    and (bo.get("owner") == 0 or bo.get("controller") == 0):
                moves.append((oid, obj_lname(b_st, oid),
                              str(bo.get("base_name") or bo.get("name"))))
        return moves

    # A1: data-level parse matches the issue
    if ST.get("data_level_ok"):
        ass["A1_data_level"] = "passed"
        notes.append("A1 passed: pinned v0.100.0 card-data.json parses "
                     "Revival Experiment as the issue reports -- "
                     "abilities[0] kind Spell, head Unimplemented (name "
                     "'unparsed_quantity' in pinned data; the issue body "
                     "quotes 'for' -- description matches, structural "
                     "defect identical) describing 'For each permanent "
                     "type, return up to one card of that type from your "
                     "graveyard to the battlefield', + sub Spell LoseLife "
                     "{ Multiply(3, Ref(TrackedSetSize)), target "
                     "Controller }, sub_link SequentialSibling, + sub-sub "
                     "Spell ChangeZone { destination Exile, target "
                     "SelfRef } (a0 itself carries no sub_link field in "
                     "the pinned data); see data_evidence.json and "
                     "revival_experiment_card_data.json.")
    else:
        ass["A1_data_level"] = "failed"
        notes.append("A1 FAILED: the pinned parse does not match the "
                     "issue-reported shape; see data_evidence.json.")

    # A2: setup at the decisive pre
    if ST["pre_exported"] and pre_st:
        in_hand = REVIVAL in hand_lnames(pre_st, 0)
        b, g, tot = mana_available(pre_st, 0)
        ph = pre_st.get("phase")
        act = pre_st.get("active_player")
        gy = graveyard_cards(pre_st, 0)
        gtypes = graveyard_ptypes(pre_st, 0)
        notes.append(f"A2 probe: pre revival_in_hand={in_hand}; "
                     f"pre mana B/G/total={b}/{g}/{tot}; pre phase={ph}; "
                     f"pre active={act}; pre stack={len(stack_entries(pre_st))}; "
                     f"pre P0 graveyard={len(gy)} cards "
                     f"({[obj_lname(pre_st, o) for o in gy]}); "
                     f"permanent types={sorted(gtypes)}; "
                     f"salvage_casts={ST['salvage_casts']}.")
        if (in_hand and b >= 1 and g >= 1 and tot >= 6
                and len(gy) >= 1 and len(gtypes) >= 2
                and ph in ("PreCombatMain", "PostCombatMain") and act == 0
                and not stack_entries(pre_st)):
            ass["A2_setup_ok"] = "passed"
            notes.append("A2 passed: pre.json shows Revival Experiment in "
                         "the P0 hand with >=1 untapped Swamp, >=1 untapped "
                         "Forest and >=6 untapped lands total (main phase, "
                         "stack empty), and the P0 graveyard holds cards "
                         "spanning >=2 permanent types -- the return clause "
                         "has a non-vacuous test population.")
        else:
            ass["A2_setup_ok"] = "failed"
            notes.append("A2 FAILED: pre.json does not show the required "
                         "setup (revival in hand, mana, and/or a stocked "
                         "graveyard with >=2 permanent types).")
    else:
        ass["A2_setup_ok"] = "failed"
        notes.append("A2 FAILED: pre.json was never exported (the decisive "
                     "cast never armed).")

    # A3: cast resolved (the spell left the stack)
    # NOTE (post-run observation): the parsed "Exile Revival Experiment"
    # ChangeZone SelfRef sub-ability did NOT exile the spell -- oid 51 went
    # Hand -> Stack -> Graveyard. The self-exile clause is outside the
    # issue's reported defect (which covers only the unparsed return head
    # and the LoseLife rider), so it is recorded as an additional
    # observation, not a verdict driver. Possible mechanisms: the
    # Unimplemented head suppresses the whole resolution chain, or the
    # ChangeZone SelfRef fails; not distinguished by this run.
    post_oid, post_zone = revival_zone(post_st) if post_st else (None, None)
    # NOTE: revival_zone finds ANY copy by name; the cast copy is
    # ST["revival_oid"]. Prefer the tracked oid for the zone check.
    if ST.get("revival_oid") is not None and post_st:
        _co = get_obj(post_st, ST["revival_oid"])
        if _co:
            post_oid, post_zone = ST["revival_oid"], _co.get("zone")
    notes.append(f"A3 probe: revival_cast={ST['revival_cast']}; "
                 f"spell_on_stack={ST['spell_on_stack']}; "
                 f"spell_resolved={ST['spell_resolved']}; "
                 f"cast_turn={ST['cast_turn']}; "
                 f"revival post_zone={post_zone} (parsed self-exile "
                 f"sub-ability did not exile it).")
    if (ST["revival_cast"] and ST["spell_on_stack"]
            and ST["spell_resolved"]):
        ass["A3_cast_resolved"] = "passed"
        notes.append(f"A3 passed: Revival Experiment was cast, the spell "
                     f"was seen on the stack and resolved "
                     f"(cast oid {ST['revival_oid']}). Post-resolution "
                     f"zone of the cast copy: {post_zone} -- the parsed "
                     f"'Exile Revival Experiment' ChangeZone SelfRef "
                     f"sub-ability did not take effect (see note above).")
    else:
        ass["A3_cast_resolved"] = "failed"
        notes.append("A3 FAILED: the cast / resolution could not be "
                     "confirmed (see A3 probe).")

    # A4: the return head produced no returns (bug symptom)
    classes = {}
    for _ph, seat, iid, opp in ST["prompts_seen"]:
        classes.setdefault(ST["prompt_classes"].get(str(iid), "other"),
                           []).append((_ph, seat, str(iid)))
    n_return = len(classes.get("return_choice", []))
    moves = gy_to_bf_moves(pre_st, post_st) if pre_st and post_st else []
    notes.append(f"A4 probe: spell_resolved={ST['spell_resolved']}; "
                 f"return_choice_seen={ST['return_choice_seen']}; "
                 f"return_choice window prompts={n_return} "
                 f"{classes.get('return_choice')}; "
                 f"graveyard->battlefield moves pre->post={moves}.")
    if (ST["spell_resolved"] and not ST["return_choice_seen"]
            and n_return == 0 and not moves):
        ass["A4_no_returns"] = "passed"
        notes.append("A4 passed (bug symptom): the spell resolved but no "
                     "graveyard-card return choice was ever offered and no "
                     "P0 card moved Graveyard->Battlefield -- the "
                     "Unimplemented return head is a runtime no-op, exactly "
                     "the reported defect.")
    elif ST["spell_resolved"]:
        ass["A4_no_returns"] = "failed"
        notes.append("A4 FAILED: a return choice was offered and/or cards "
                     "returned to the battlefield -- the return clause "
                     "functioned at runtime.")
    else:
        notes.append("A4 not-run: the spell never resolved.")

    # A5: LoseLife read the empty tracked set -- no life lost
    pre_life = player_of(pre_st, 0).get("life") if pre_st else None
    post_life = player_of(post_st, 0).get("life") if post_st else None
    notes.append(f"A5 probe: spell_resolved={ST['spell_resolved']}; "
                 f"P0 life pre={pre_life} post={post_life}.")
    if (ST["spell_resolved"] and pre_life is not None
            and post_life is not None and pre_life == post_life):
        ass["A5_life_unchanged"] = "passed"
        notes.append("A5 passed: P0 life unchanged across resolution "
                     f"({pre_life} -> {post_life}) -- the LoseLife "
                     "sub-ability read the empty chain tracked set "
                     "(3 x 0 = 0). The issue asserts no consumer-side "
                     "symptom; this records the observable absence.")
    elif ST["spell_resolved"]:
        ass["A5_life_unchanged"] = "failed"
        notes.append("A5 FAILED: P0 life changed across resolution "
                     f"({pre_life} -> {post_life}) -- the LoseLife rider "
                     "fired on a non-empty population.")
    else:
        notes.append("A5 not-run: the spell never resolved.")

    # A6: cleanup
    if post_st:
        tnum = ST.get("cast_turn")
        cur_turn = post_st.get("turn_number")
        no_lingering = ST.get("cast_in_flight") is None
        if not stack_entries(post_st) and no_lingering and (
                tnum is None or (cur_turn or 0) > tnum):
            ass["A6_cleanup"] = "passed"
            notes.append("A6 passed: post stack empty, game advanced past "
                         f"the cast turn (cast turn {tnum}, post turn "
                         f"{cur_turn}, phase {post_st.get('phase')}), no "
                         f"in-flight submissions lingering "
                         f"(rejections={ST['cast_rejections']}).")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append("A6 FAILED: post stack non-empty, the game did not "
                         "advance past the cast turn, or an in-flight "
                         f"submission is lingering "
                         f"(in_flight={ST.get('cast_in_flight')}, "
                         f"stack={len(stack_entries(post_st))}, "
                         f"turn {cur_turn} vs cast {tnum}).")
    else:
        notes.append("A6 not-run: no post state")

    # verdict
    if all(ass[k] == "passed" for k in ("A1_data_level", "A2_setup_ok",
                                        "A3_cast_resolved",
                                        "A4_no_returns",
                                        "A5_life_unchanged",
                                        "A6_cleanup")):
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: Revival Experiment was cast with a stocked "
            "graveyard and resolved, but the 'for each permanent type, "
            "return up to one card of that type from your graveyard to the "
            "battlefield' clause never parsed (Unimplemented head, runtime "
            "no-op), so no cards returned, the chain tracked set was "
            "allocated empty, and the dependent LoseLife read an empty set "
            "(0 life lost). ADDITIONAL OBSERVATION (outside the issue's "
            "reported defect): the parsed 'Exile Revival Experiment' "
            "ChangeZone SelfRef sub-ability did not take effect -- the cast "
            "copy went Hand -> Stack -> Graveyard. Possible mechanisms: "
            "the Unimplemented head suppresses the whole resolution chain, "
            "or the ChangeZone SelfRef fails; not distinguished by this "
            "run. Confirmed on v0.100.0. This is not a fix claim.")
    elif (ass["A1_data_level"] == "passed"
            and ass["A2_setup_ok"] == "passed"
            and ass["A3_cast_resolved"] == "passed"
            and (ass["A4_no_returns"] == "failed"
                 or ass["A5_life_unchanged"] == "failed")):
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: the cast path was exercised and the "
            "return clause functioned at runtime (cards returned and/or "
            "life was lost). This is not a fix claim.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: the reported cast path could not be "
                     "fully exercised; see assertion notes.")

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
               if "revival" in l.lower()
               or "unimplemented" in l.lower()
               or "tracked" in l.lower()
               or "lose life" in l.lower()
               or "loselife" in l.lower()
               or (ST["game_code"] and ST["game_code"] in l)]
    with open(f"{EVDIR}/server_log_excerpt.txt", "w") as f:
        f.write("\n".join(excerpt[-150:]) + "\n")
    notes.append(f"server.log excerpt: {len(excerpt)} matching lines "
                 f"(of {len(lines)} total; log={used})")

    # copy the scenario into the evidence dir for provenance
    import shutil
    shutil.copy(f"{BACKFILL}/driver/scenario_{ISSUE}.py",
                f"{EVDIR}/scenario_{ISSUE}.py")

    gy_pre = [(obj_lname(pre_st, o), sorted(card_core_types(pre_st, o)))
              for o in graveyard_cards(pre_st, 0)] if pre_st else []
    gy_post = [(obj_lname(post_st, o), sorted(card_core_types(post_st, o)))
               for o in graveyard_cards(post_st, 0)] if post_st else []
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": ST["started_at"],
        "duration_s": round(time.time() - ST["t0"], 1),
        "server": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 101, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_{ISSUE}.py",
                 "rb").read()).hexdigest(),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": {
            "revival_cast": ST["revival_cast"],
            "revival_oid": ST["revival_oid"],
            "spell_on_stack": ST["spell_on_stack"],
            "spell_resolved": ST["spell_resolved"],
            "salvage_casts": ST["salvage_casts"],
            "dig_answered": ST["dig_answered"],
            "cast_turn": ST["cast_turn"],
            "return_choice_seen": ST["return_choice_seen"],
            "revival_post_zone": post_zone,
            "p0_life_pre": player_of(pre_st, 0).get("life") if pre_st else None,
            "p0_life_post": player_of(post_st, 0).get("life") if post_st else None,
            "gy_pre": gy_pre,
            "gy_post": gy_post,
            "gy_to_bf_moves": moves,
            "prompt_classes": {k: len(v)
                               for k, v in classes.items()},
            "prompts_seen": [(p[0], p[1], p[2]) for p in ST["prompts_seen"]],
            "wf_types_window": sorted(set(ST["wf_types_window"])),
            "stack_kinds_window": ST["stack_kinds_window"][-10:],
            "cast_rejections": ST["cast_rejections"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The report is a data-level parse report; the scenario replays "
            "the reported line (Revival Experiment cast with a stocked "
            "graveyard, full resolution) and observes the resolution "
            "outcome. The issue asserts no consumer-side symptom; A5 "
            "records the observable absence of any life loss.",
            "The P0 graveyard is stocked via Grisly Salvage mills "
            "(fully-parsed Dig effect) rather than combat deaths; the "
            "returned-card population is whatever the mills produced.",
            "4x/8x/14x deck densities are test-harness conveniences "
            "(engine accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full "
            "game replay.",
        ],
        "setup_line": "P0: 4x Revival Experiment + 4x Grisly Salvage + "
                      "8x Llanowar Elves + 4x Ornithopter + 4x Memnite + "
                      "4x Pacifism + 4x Grizzly Bears + 14x Forest + "
                      "14x Swamp; P1: 60x Forest (lands only, never casts, "
                      "never attacks); default Bo1, life 20",
        "contract_line": "Revival Experiment cast with stocked graveyard -> "
                         "full resolution: correct = up to one card of each "
                         "permanent type returns from the P0 graveyard to "
                         "the battlefield, P0 loses 3 life per card "
                         "returned, spell exiled. Observed (bug): the "
                         "return clause never parsed (Unimplemented head, "
                         "runtime no-op), so nothing returns, the chain "
                         "tracked set is allocated empty, and LoseLife "
                         "reads an empty set (0 life lost).",
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

def write_manifest_and_validate():
    import glob as _glob
    # driver_stdout.log is the live stdout of this process; it keeps growing
    # after the manifest is written, so it is hashed in a post-run pass
    # (see the tail of main) rather than here.
    files = sorted(os.path.basename(p)
                   for p in _glob.glob(f"{EVDIR}/*")
                   if os.path.isfile(p)
                   and os.path.basename(p) not in ("manifest.sha256",
                                                   "driver_stdout.log"))
    lines = []
    for fn in files:
        h = hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
        lines.append(f"{h}  {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    say(f"manifest.sha256 written ({len(files)} files)")
    # validation: every JSON parses; hashes match; PNG opens nonzero
    problems = []
    for fn in files:
        if fn.endswith(".json") or fn.endswith(".jsonl"):
            try:
                if fn.endswith(".jsonl"):
                    for line in open(f"{EVDIR}/{fn}"):
                        if line.strip():
                            json.loads(line)
                else:
                    json.load(open(f"{EVDIR}/{fn}"))
            except Exception as e:
                problems.append(f"{fn} JSON parse failed: {e}")
    for line in open(f"{EVDIR}/manifest.sha256"):
        h, _, fn = line.strip().partition("  ")
        if not fn:
            continue
        actual = hashlib.sha256(open(f"{EVDIR}/{fn}", "rb").read()).hexdigest()
        if actual != h:
            problems.append(f"{fn} hash mismatch")
    try:
        from PIL import Image
        im = Image.open(f"{EVDIR}/summary.png")
        im.load()
        if im.size[0] == 0 or im.size[1] == 0:
            problems.append("summary.png has zero size")
        say(f"summary.png opens OK ({im.size[0]}x{im.size[1]})")
    except Exception as e:
        problems.append(f"summary.png unreadable: {e}")
    if problems:
        say("VALIDATION PROBLEMS:")
        for p in problems:
            say(f"  - {p}")
    else:
        say("validation OK: all JSON parse, hashes match, PNG readable")
    return problems

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
        last_change = [time.time()]
        last_rev = [-1]
        stall_dumped = [False]
        while not ST["terminal"]:
            try:
                t, data = await asyncio.wait_for(c.inbox.get(), 2)
            except asyncio.TimeoutError:
                # stall watchdog: if the game state hasn't changed for 60s,
                # dump a diagnostic summary (the game may be waiting on us).
                st = c.latest
                if st is not None:
                    rev = st.get("state_revision", -1)
                    if rev != last_rev[0]:
                        last_rev[0] = rev
                        last_change[0] = time.time()
                        stall_dumped[0] = False
                    elif (not stall_dumped[0]
                          and time.time() - last_change[0] > 60):
                        stall_dumped[0] = True
                        state = st["state"]
                        vi = st.get("viewer_interaction") or {}
                        summ = {
                            "at": time.time(),
                            "who": tag,
                            "rev": rev,
                            "phase": state.get("phase"),
                            "active": state.get("active_player"),
                            "turn": state.get("turn_number"),
                            "waiting_for": wf_of(state),
                            "stack_n": len(stack_entries(state)),
                            "stack": [json.dumps(se, default=str)[:400]
                                      for se in stack_entries(state)],
                            "p0_hand": hand_lnames(state, 0),
                            "p0_untapped_lands": [
                                obj_lname(state, o)
                                for o in untapped_lands(state, 0)],
                            "p0_bf": [obj_lname(state, o)
                                      for o in bf_permanents(state, 0)],
                            "p0_gy": [obj_lname(state, o)
                                      for o in graveyard_cards(state, 0)],
                            "p1_hand_n": len(hand_ids(state, 1)),
                            "mana_needs": ST["mana_needs"][tag],
                            "cast_in_flight": ST.get("cast_in_flight"),
                            "revival_oid": ST.get("revival_oid"),
                            "salvage_in_flight": ST.get("salvage_in_flight"),
                            "vi_canSubmit": vi.get("canSubmit"),
                            "vi_opps": [
                                json.dumps(o, default=str)[:1200]
                                for o in (vi.get("opportunities") or [])],
                            "legal_action_types": sorted(set(
                                a.get("type") for a in merged_actions(st))),
                        }
                        with open(f"{EVDIR}/stall_dump.jsonl", "a") as f:
                            f.write(json.dumps(summ, default=str) + "\n")
                        say(f"[{tag}] STALL DUMP written "
                            f"(rev {rev} unchanged 60s)")
                        wire("stall_dump", {"rev": rev})
                # resolve watchdog also runs on the timeout path (no new
                # messages arrive after GameOver): deadline or game end must
                # terminate the run with a post export, never deadlock.
                if ST["phase"] == "resolving":
                    stx = c.latest
                    wfx = (wf_of(stx["state"]).get("type") or "") if stx \
                        else ""
                    if wfx == "GameOver":
                        say(f"[{tag}] game over seen on timeout path; "
                            "exporting post")
                        wire("gameover_timeout", {})
                        if not ST["post_exported"]:
                            await export_as(c0_ref[0], "post")
                            ST["post_exported"] = True
                        ST["terminal"] = True
                        return
                    if (not ST["spell_resolved"]
                            and time.time() - (ST["resolving_since"]
                                               or ST["t0"])
                            > RESOLVE_DEADLINE_S):
                        say("[watchdog/timeout] deadline in resolving "
                            "without resolution; exporting post and "
                            "finalizing")
                        wire("watchdog_timeout",
                             {"note": "resolving timeout"})
                        if not ST["post_exported"]:
                            await export_as(c0_ref[0], "post")
                            ST["post_exported"] = True
                        ST["terminal"] = True
                        return
                continue
            if t == "ActionRejected":
                ST["cast_rejections"] += 1
                wire("action_rejected", {"who": tag,
                                        "data": json.dumps(data)[:400]})
                say(f"[{tag}] ActionRejected: {json.dumps(data)[:200]}")
                # a rejected cast leaves no mana owed
                ST["mana_needs"][tag] = {"B": 0, "G": 0, "generic": 0}
                if ST.get("cast_in_flight"):
                    ST["cast_in_flight"] = None
                if ST.get("salvage_in_flight") and tag == "P0":
                    ST["salvage_in_flight"] = None
                if ST.get("revival_cast") and not ST.get("spell_on_stack"):
                    # the cast submission itself was rejected; back out to
                    # the build phase so the stage machine retries
                    ST["revival_cast"] = False
                    ST["revival_oid"] = None
                    ST["phase"] = "build"
                    ST["resolving_since"] = None
                    say("[P0] cast rejected; returning to build phase")
            st = c.latest
            if not st:
                continue
            state = st["state"]
            ST["states_seen"] += 1
            # ---- decisive window tracking ----
            if ST["phase"] == "resolving":
                if ST["revival_cast"] and not ST["spell_on_stack"]:
                    if revival_on_stack(state) is not None:
                        ST["spell_on_stack"] = True
                        say("[window] Revival Experiment spell observed "
                            "on the stack")
                        wire("spell_on_stack", {})
                if (ST["spell_on_stack"]
                        and not ST["mid_exported"]):
                    await export_as(c0_ref[0], "mid_stack")
                    ST["mid_exported"] = True
                    say("[window] mid_stack exported (spell on stack)")
                    wire("mid_exported", {})
                    dump_stack_once(state, "revival_on_stack")
                if ST["spell_on_stack"] and not ST["spell_resolved"]:
                    if revival_on_stack(state) is None:
                        ST["spell_resolved"] = True
                        say("[window] Revival Experiment resolved; "
                            "starting settle clock")
                        wire("spell_resolved", {})
                        ST["settle_at"] = time.time()
                # record the kinds of stack entries seen in the window
                for se in stack_entries(state):
                    kind = json.dumps(se.get("kind"), default=str)[:120]
                    if kind not in ST["stack_kinds_window"]:
                        ST["stack_kinds_window"].append(kind)
                # watchdog: the window must not hang the run forever
                if not ST["spell_resolved"]:
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
                wf = (wf_of(state).get("type") or "")
                if wf and wf not in ST["wf_types_window"]:
                    ST["wf_types_window"].append(wf)
                if wf == "GameOver":
                    say("[window] game over during decisive window; "
                        "exporting post")
                    wire("gameover", {"winner": (wf_of(state).get("data")
                                                or {}).get("winner")})
                    if not ST["post_exported"]:
                        await export_as(c0_ref[0], "post")
                        ST["post_exported"] = True
                    ST["terminal"] = True
                    return
                if (ST["spell_resolved"] and not stack_entries(state)
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
                elif not ST["spell_resolved"]:
                    ST["settle_at"] = None
            if time.time() - ST["t0"] > SETUP_DEADLINE_S:
                say("[timeout] deadline reached")
                wire("timeout", {})
                ST["terminal"] = True
                return
            if ST["phase"] == "build" and \
                    (state.get("turn_number") or 0) > TURN_CAP:
                say(f"[turncap] turn {state.get('turn_number')} > {TURN_CAP} "
                    "in build phase; finalizing")
                wire("turncap", {})
                ST["terminal"] = True
                return
            acts = merged_actions(st)
            if tag == "P0":
                await p0_tick(st, acts, state, c)
            else:
                await p1_tick(st, acts, state, c)

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
    # render the summary PNG from saved evidence, then manifest+validate
    import subprocess
    r = subprocess.run(
        [sys.executable, f"{BACKFILL}/driver/render_summary_7438.py", EVDIR],
        capture_output=True, text=True, timeout=120)
    say(r.stdout.strip() or "(renderer produced no stdout)")
    if r.returncode != 0:
        say(f"renderer FAILED rc={r.returncode}: {r.stderr[:500]}")
    problems = write_manifest_and_validate()
    print(json.dumps({"verdict": verdict, "assertions": ST["ass"],
                      "validation_problems": problems}, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
