#!/usr/bin/env python3
"""Issue #7436: Princess Luna -- "choose up to six cards you own from
outside the game" is unparsed, so `ChangeZone` reads an empty tracked set.

Reported (2026-08-15, lgray; source:internal-triage, status:confirmed,
area:parser, mechanic:zone-change, priority:p3-card-specific,
classifier:unsupported-aspect; related #6857 tracked-set publish census, one
of 33 cards whose tracked-set antecedent clause never parses):

> Flying
> When this creature transforms into Princess Luna, choose up to six cards
> you own from outside the game with a moon in their art, then exile those
> cards. As long as those cards remain exiled, you may cast them, and your
> friends may cast them with your permission. (Gifts are appreciated.)

Pinned v0.100.0 parse (see data_evidence.json): Princess Luna triggers[0],
mode "Transformed":
  head = Unimplemented { name: "unparsed_verb_arguments",
                         description: "choose cards you own from outside the
                                       game with a moon in their art" }
    sub_ability (Spell): ChangeZone { destination Exile,
                                     target TrackedSet(0) }
      sub_sub_ability (Spell): Unimplemented
                               { name: "unrecognized_clause_head",
                                 description: "As long as those cards
                                               remain exiled" }
        sub_sub_sub (Spell): CastFromZone { target ParentTarget,
                                           optional true, mode Cast }
DISCREPANCY (recorded, as in the #7434 run): the issue body quotes the head
name as "choose"; the pinned v0.100.0 card-data.json names it
"unparsed_verb_arguments". The description string matches, and the
structural defect (Unimplemented head -> empty tracked set -> ChangeZone
exiles nothing) is identical either way.
The front face "Nightmare Moon" ({4}{B}{B}, flying) has one Activated
ability: {6}: Transform Nightmare Moon, Transform target SelfRef.

The Unimplemented resolver is a runtime no-op (pushes no GameEvent), so the
publish authority allocates a FRESH EMPTY tracked set and the ChangeZone
sub-ability exiles nothing. No choice prompt for the outside-the-game
selection is ever offered.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 4x Nightmare Moon + 56x Swamp (60)
  P1: 60x Swamp (plays lands only, never casts, never attacks)
Drive:
  1. Mulligans: P0 keeps iff Nightmare Moon is in the opening hand
     (mulligans down, min hand 5); P1 keeps 7.
  2. P0 plays one Swamp per turn, casts Nightmare Moon as soon as able
     ({4}{B}{B} = 6 untapped Swamps), then activates the {6} transform
     ability as soon as able (6 more untapped lands), passing priority
     otherwise. P1 plays lands and passes. Turn cap ~45.
  3. pre.json is exported INSIDE the activation submit path (guarded flag),
     never by a main-loop probe. mid_stack.json is exported when the
     transform trigger is first seen on the stack; post.json once settled
     (trigger resolved, stack empty, Priority, 8s idle) or on the
     GameOver/deadline finalize path.

Expected (correct behavior): the transform trigger resolves by offering a
choice of up to six cards the player owns from outside the game, then exiles
the chosen cards (>= 1 such card would be exiled in a real game with a
sideboard; in this harness there is no outside-the-game pool, so the
decisive observable is whether the choice prompt is EVER offered).
Reported (bug): the choose clause never parsed (Unimplemented head, runtime
no-op), so the choice prompt is never offered, the chain tracked set is
allocated empty, and the ChangeZone sub-ability exiles 0 objects.

Assertions:
  A1_data_level   pinned v0.100.0 card-data.json parses Princess Luna as the
                  issue reports: triggers[0] mode Transformed, head
                  Unimplemented (record exact head name) describing the
                  outside-the-game choose; sub Spell ChangeZone { Exile,
                  TrackedSet(0) }; sub-sub Unimplemented naming the "As long
                  as those cards remain exiled" clause; sub-sub-sub
                  CastFromZone { ParentTarget, optional true, Cast }; front
                  face Nightmare Moon has the {6} Transform SelfRef
                  activated ability.
  A2_setup_ok     pre.json: Nightmare Moon on the P0 battlefield before
                  activation; mid_stack.json: the transform trigger on the
                  stack (source = the Moon object, "princess luna" in the
                  stack entry text).
  A3_cast_resolved
                  the transform activation was submitted, its ability object
                  was seen on the stack and left it (resolved); the creature
                  is now "Princess Luna" on the P0 battlefield; the transform
                  trigger left the stack (resolved).
  A4_no_exile_window
                  count of objects moving into Exile in the resolution window
                  from pre.json to post.json, tracking per-object zone
                  transitions (natural deaths cannot produce Exile moves, so
                  no exclusion is needed). EXPECTED TO FAIL-PASS: under the
                  bug the Unimplemented head is a runtime no-op, the tracked
                  set publishes empty, and the ChangeZone exiles nothing --
                  so A4 passes as the bug symptom (0 exiled). A4 passed iff 0
                  objects were exiled by the resolution window AND the choice
                  prompt was never offered (ST["choice_prompt_seen"] false).
  A5_no_choice_prompt
                  no outside-the-game / choice-shaped prompt for cards was
                  ever offered during the trigger resolution window
                  (classifier: choice-shaped only -- schema/exactChoices
                  opportunities with object (card) candidates, or text
                  mentioning "outside the game"; excludes mana-payment taps,
                  pass-priority choices, and ordinary Priority /
                  DeclareAttackers prompts).
  A6_cleanup      post stack empty, game advanced past the resolution turn,
                  no unrejected submissions lingering (no in-flight
                  cast/activation, rejections recorded).

Verdict rule: reproduced iff A1, A2, A3, A5, A6 passed and A4 confirmed 0
              exiled with no prompt (i.e., the reported parse no-op played
              out at runtime);
              not-reproduced iff a choice prompt was offered or some cards
              were actually chosen and exiled;
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
RUN_ID = "20261003-0611-7436"
ISSUE = 7436
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

MOON = "nightmare moon"
PRINCESS = "princess luna"
SWAMP = "swamp"

P0_DECK = [("Nightmare Moon", 4), ("Swamp", 56)]
P1_DECK = [("Swamp", 60)]


def deck(pairs):
    names = []
    for n, c in pairs:
        names += [n] * c
    return {"main_deck": names, "sideboard": [], "commander": []}


SETUP_DEADLINE_S = 1800
RESOLVE_DEADLINE_S = 600
SETTLE_IDLE_S = 8
TURN_CAP = 45


def reset_attempt():
    global ST, MULLS, MULL_COUNT, SUBMITTED_OPPS, _DISCARD_REV
    ST = {
        "t0": time.time(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "game_code": None,
        "phase": "build",  # build -> resolving -> done
        "moon_cast": False,
        "moon_oid": None,
        "transform_activated": False,
        "activated_on_stack": False,
        "activation_resolved": False,
        "trigger_on_stack": False,
        "trigger_resolved": False,
        "activation_turn": None,
        "pre_exported": False,
        "mid_exported": False,
        "post_exported": False,
        "cast_in_flight": None,
        "choice_prompt_seen": False,
        "choice_answered": False,
        "exile_window": None,  # {moved: n, transitions: [...]}
        "mana_needs": {"P0": {"B": 0, "R": 0, "generic": 0},
                       "P1": {"B": 0, "R": 0, "generic": 0}},
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
                                       "A4_no_exile_window",
                                       "A5_no_choice_prompt",
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


def moon_oid(state, pid=0):
    for oid in bf_permanents(state, pid):
        if obj_lname(state, oid) == MOON:
            return oid
    return None


def princess_oid(state, pid=0):
    for oid in bf_permanents(state, pid):
        if obj_lname(state, oid) == PRINCESS:
            return oid
    return None


def untapped_lands(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and not o.get("tapped") and is_land(o)]


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


def activated_ability_on_stack(state):
    """The {6}: Transform activated ability: source is the Moon object,
    text mentions 'transform' but NOT 'princess luna' (the trigger text
    names Princess Luna; the ability text does not)."""
    want = ST.get("moon_oid")
    if want is None:
        want = princess_oid(state)
    for se in stack_entries(state):
        b = se_blob(se)
        if _src_matches(se, want) and "transform" in b \
                and PRINCESS not in b:
            return se
    return None


def transform_trigger_on_stack(state):
    """The 'When this creature transforms into Princess Luna' trigger:
    source is the Moon object and the entry text names Princess Luna."""
    want = ST.get("moon_oid")
    if want is None:
        want = princess_oid(state)
    for se in stack_entries(state):
        b = se_blob(se)
        if _src_matches(se, want) and PRINCESS in b:
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
    luna = CARD_DATA.get("princess luna", {})
    moon = CARD_DATA.get("nightmare moon", {})
    triggers = luna.get("triggers") or []
    t0 = triggers[0] if triggers else {}
    head = (t0.get("execute") or {}).get("effect") or {}
    sub = ((t0.get("execute") or {}).get("sub_ability") or {})
    sub_eff = sub.get("effect") or {}
    subsub = (sub.get("sub_ability") or {})
    subsub_eff = subsub.get("effect") or {}
    subsubsub = (subsub.get("sub_ability") or {})
    subsubsub_eff = subsubsub.get("effect") or {}
    moon_acts = moon.get("abilities") or []
    mact = moon_acts[0] if moon_acts else {}
    mact_eff = mact.get("effect") or {}
    mact_cost = mact.get("cost") or {}
    ev = {
        "princess_luna": luna,
        "nightmare_moon": moon,
        "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
        "head_name_discrepancy": {
            "issue_body_quotes": "choose",
            "pinned_data_shows": head.get("name"),
            "note": "description string matches the issue; structural "
                    "defect identical either way",
        },
    }
    ok = (
        t0.get("mode") == "Transformed"
        and head.get("type") == "Unimplemented"
        and "choose cards you own from outside the game with a moon in "
            "their art" in str(head.get("description", "")).lower()
        and sub_eff.get("type") == "ChangeZone"
        and sub_eff.get("destination") == "Exile"
        and (sub_eff.get("target") or {}).get("type") == "TrackedSet"
        and (sub_eff.get("target") or {}).get("id") == 0
        and subsub_eff.get("type") == "Unimplemented"
        and "as long as those cards remain exiled" in
        str(subsub_eff.get("description", "")).lower()
        and subsubsub_eff.get("type") == "CastFromZone"
        and (subsubsub_eff.get("target") or {}).get("type") == "ParentTarget"
        and subsubsub.get("optional") is True
        and mact.get("kind") == "Activated"
        and mact_eff.get("type") == "Transform"
        and (mact_eff.get("target") or {}).get("type") == "SelfRef"
        and (mact_cost.get("cost") or {}).get("generic") == 6
    )
    say(f"data-level check: trigger mode={t0.get('mode')}; "
        f"head={head.get('type')}/{head.get('name')}; "
        f"sub={sub_eff.get('type')}/{(sub_eff.get('target') or {})}; "
        f"subsub={subsub_eff.get('type')}/{subsub_eff.get('name')}; "
        f"subsubsub={subsubsub_eff.get('type')}/"
        f"{(subsubsub_eff.get('target') or {}).get('type')}/"
        f"optional={subsubsub_eff.get('optional')}; "
        f"moon ability={mact.get('kind')}/{mact_eff.get('type')}/"
        f"cost={(mact_cost.get('cost') or {}).get('generic')}")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    # byte snapshot of the Princess Luna entry itself
    with open(f"{EVDIR}/princess_luna_card_data.json", "w") as f:
        json.dump(luna, f, indent=1)
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
    return MOON in hand


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
    # bottom lands first; never bottom Nightmare Moon for P0
    rank = {SWAMP: 0, MOON: 9}
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
    # discard lands first; never discard Nightmare Moon
    rank = {SWAMP: 0, MOON: 9}
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
            hand, key=lambda o: (0 if obj_lname(state, o) == SWAMP else 1,
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
                if obj_lname(state, ref) == SWAMP:
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
                for color in ("B", "R", "G", "U", "W"):
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

# ------------------------------------------------------------- prompt handlers
ORDINARY_ACTION_CODES = {"passPriority", "playLand", "tapLandForMana"}


def classify_prompt(opp):
    """Classify a resolution-window opportunity. Returns one of:
    mana_payment, pass_priority, action_menu, declare_combat,
    discard_handsize, choice_shaped, other.

    choice_shaped is deliberately narrow: only a card-candidate selection
    that is NOT an ordinary game prompt (mana payment, pass/play-land
    menu, attack/block declaration, hand-size discard) and that names
    outside-the-game cards or offers non-hand, non-battlefield object
    candidates. The bug's choice is specifically "cards you own from
    outside the game", so hand-size discards and play-land menus must
    never count as the reported prompt."""
    blob = json.dumps(opp, default=str).lower()
    resp = opp.get("response") or {}
    data = resp.get("data") or {}
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
    for it in items:
        for sf in it.get("surfaces") or []:
            if sf.get("type") == "object":
                saw_obj = True
                obj_zones.add(
                    str((sf.get("data") or {}).get("zone") or "").lower())
    if saw_obj and obj_zones and obj_zones <= {"hand"}:
        return "discard_handsize"
    # 5. the bug's choice: outside-the-game card selection
    if saw_obj or "outside the game" in blob or "choose up to six" in blob:
        return "choice_shaped"
    # 6. pass priority fallback (no objects)
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


async def answer_outside_choice(c, st, state, tag):
    """Control-branch handler: if the engine DOES offer an outside-the-game
    card choice during the trigger window, record it (A5 then fails) and
    answer minimally so the game can continue: empty selection when the
    spec min allows 0, else the first candidates up to max."""
    if tag != "P0" or ST["phase"] != "resolving" \
            or not ST["trigger_on_stack"]:
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
        has_obj = False
        for ch in chs:
            k, _ = _cand_ref(ch)
            if k == "object":
                has_obj = True
                break
        if not has_obj:
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        ST["choice_prompt_seen"] = True
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
        ST["choice_answered"] = True
        say(f"[{tag}] CONTROL: outside-the-game choice prompt OFFERED "
            f"(min={lo} max={hi}); answering with {len(picks)} picks")
        wire("outside_choice", {"who": tag, "iid": iid,
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
async def activate_transform(c, state, acts, tag):
    """P0 activates Nightmare Moon's {6}: Transform. pre.json is exported
    INSIDE this submit path (guarded flag), never by a main-loop probe."""
    mid = moon_oid(state, 0)
    if mid is None:
        return False
    for a in acts:
        if a.get("type") == "ActivateAbility" and str(
                (a.get("data") or {}).get("source_id")) == str(mid):
            if not ST["pre_exported"]:
                await export_as(c, "pre")
                ST["pre_exported"] = True
                pre_st = json.load(
                    open(f"{EVDIR}/pre.json")).get("state", {})
                say(f"[arm] pre.json exported: moon on P0 battlefield oid="
                    f"{moon_oid(pre_st, 0)}; turn={pre_st.get('turn_number')}; "
                    f"phase={pre_st.get('phase')}")
                wire("pre_exported",
                     {"moon_oid": moon_oid(pre_st, 0),
                      "turn": pre_st.get("turn_number")})
            ST["moon_oid"] = mid
            ST["transform_activated"] = True
            ST["activation_turn"] = state.get("turn_number")
            ST["cast_in_flight"] = f"activate-{mid}"
            ST["mana_needs"]["P0"] = {"B": 0, "R": 0, "generic": 6}
            ST["phase"] = "resolving"
            ST["resolving_since"] = time.time()
            say(f"[P0] activating {{6}}: Transform Nightmare Moon "
                f"(DECISIVE; oid {mid})")
            wire("transform_activated", {"oid": mid})
            await submit_as_is(c, a)
            return True
    return False


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
    if await answer_outside_choice(c, st, state, "P0"):
        return
    # Payment-complete detectors: a cast leaves the hand; an activation
    # lands on the stack. Either way the engine has taken its payment:
    # clear the owed mana.
    cif = ST.get("cast_in_flight")
    if cif is not None:
        if isinstance(cif, str) and cif.startswith("activate-"):
            if activated_ability_on_stack(state) is not None:
                ST["cast_in_flight"] = None
                ST["mana_needs"]["P0"] = {"B": 0, "R": 0, "generic": 0}
                say("[P0] transform ability on stack; payment complete, "
                    "needs cleared")
                wire("payment_complete", {"kind": "activation"})
        else:
            co = get_obj(state, cif)
            if co.get("zone") != "Hand":
                ST["cast_in_flight"] = None
                ST["mana_needs"]["P0"] = {"B": 0, "R": 0, "generic": 0}
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
        mid = moon_oid(state, pid)
        if mid is not None:
            ST["moon_oid"] = mid
        if princess_oid(state, pid) is not None:
            pass  # already transformed; nothing left to drive
        elif mid is not None and not ST["transform_activated"]:
            if len(untapped_lands(state, pid)) >= 6:
                if await activate_transform(c, state, acts, "P0"):
                    return
        elif mid is None and not ST["moon_cast"] and MOON in hn:
            if len(untapped_lands(state, pid)) >= 6:
                a, oid = cast_action_for(acts, state, MOON)
                if a:
                    ST["moon_cast"] = True
                    ST["cast_in_flight"] = oid
                    ST["mana_needs"]["P0"] = {"B": 0, "R": 0, "generic": 6}
                    say(f"[P0] casting Nightmare Moon (oid {oid})")
                    wire("moon_cast", {"oid": oid})
                    await submit_as_is(c, a)
                    return
        # play a land
        n_lands = sum(1 for oid in bf_permanents(state, pid)
                      if is_land(get_obj(state, oid)))
        if n_lands < 20:
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
        if n_lands < 8:
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

    # A1: data-level parse matches the issue
    if ST.get("data_level_ok"):
        ass["A1_data_level"] = "passed"
        notes.append("A1 passed: pinned v0.100.0 card-data.json parses "
                     "Princess Luna as the issue reports -- triggers[0] "
                     "mode Transformed, head Unimplemented (name "
                     "'unparsed_verb_arguments' in pinned data; the issue "
                     "body quotes 'choose' -- description matches, "
                     "structural defect identical) describing 'choose cards "
                     "you own from outside the game with a moon in their "
                     "art', + sub Spell ChangeZone { Exile, TrackedSet(0) } "
                     "+ sub-sub Unimplemented ('unrecognized_clause_head': "
                     "'As long as those cards remain exiled') + sub-sub-sub "
                     "CastFromZone { ParentTarget, optional true, Cast }; "
                     "front face Nightmare Moon has the {6} Transform "
                     "SelfRef activated ability; see data_evidence.json "
                     "and princess_luna_card_data.json.")
    else:
        ass["A1_data_level"] = "failed"
        notes.append("A1 FAILED: the pinned parse does not match the "
                     "issue-reported shape; see data_evidence.json.")

    # A2: setup at the decisive pre + trigger seen on the stack
    if ST["pre_exported"] and pre_st and ST["mid_exported"] and mid_st:
        moon_pre = moon_oid(pre_st, 0)
        trig_mid = transform_trigger_on_stack(mid_st)
        ph = pre_st.get("phase")
        act = pre_st.get("active_player")
        notes.append(f"A2 probe: pre moon_oid={moon_pre} (P0 battlefield); "
                     f"mid trigger on stack={trig_mid is not None}; "
                     f"pre phase={ph}; pre active={act}; "
                     f"pre stack={len(stack_entries(pre_st))}.")
        if moon_pre is not None and trig_mid is not None \
                and ph in ("PreCombatMain", "PostCombatMain") and act == 0 \
                and not stack_entries(pre_st):
            ass["A2_setup_ok"] = "passed"
            notes.append("A2 passed: pre.json shows Nightmare Moon on the P0 "
                         "battlefield before activation (main phase, stack "
                         "empty), and mid_stack.json shows the transform "
                         "trigger on the stack.")
        else:
            ass["A2_setup_ok"] = "failed"
            notes.append("A2 FAILED: pre/mid do not show the required "
                         "setup (moon on battlefield and/or trigger on "
                         "stack).")
    else:
        ass["A2_setup_ok"] = "failed"
        notes.append("A2 FAILED: pre.json and/or mid_stack.json was never "
                     "exported (the decisive window never armed).")

    # A3: activation resolved, Princess Luna on the battlefield, trigger gone
    notes.append(f"A3 probe: transform_activated={ST['transform_activated']}; "
                 f"activated_on_stack={ST['activated_on_stack']}; "
                 f"activation_resolved={ST['activation_resolved']}; "
                 f"trigger_on_stack={ST['trigger_on_stack']}; "
                 f"trigger_resolved={ST['trigger_resolved']}; "
                 f"activation_turn={ST['activation_turn']}.")
    princess_post = princess_oid(post_st, 0) if post_st else None
    if (ST["transform_activated"] and ST["activation_resolved"]
            and ST["trigger_resolved"] and princess_post is not None):
        ass["A3_cast_resolved"] = "passed"
        notes.append(f"A3 passed: the {{6}} transform activation was "
                     f"submitted, its ability object was seen on the stack "
                     f"and resolved; the creature is now Princess Luna "
                     f"(oid {princess_post}) on the P0 battlefield; the "
                     f"transform trigger left the stack (resolved).")
    else:
        ass["A3_cast_resolved"] = "failed"
        notes.append("A3 FAILED: the transform activation / trigger "
                     "resolution could not be confirmed (see A3 probe).")

    # A4: per-object zone transitions pre -> post; count moves into Exile
    exiled_oids = []
    transitions = []
    pre_objs = pre_st.get("objects") or {}
    post_objs = post_st.get("objects") or {}
    for oid, o in pre_objs.items():
        pz = o.get("zone")
        if pz == "Exile":
            continue
        po = post_objs.get(str(oid))
        if po is None:
            transitions.append((oid, pz, "MISSING"))
            continue
        qz = po.get("zone")
        if qz != pz:
            transitions.append(
                (oid, pz, qz,
                 str(o.get("base_name") or o.get("name") or "?")))
        if qz == "Exile":
            exiled_oids.append(oid)
    ST["exile_window"] = {"moved": len(exiled_oids),
                          "transitions": transitions}
    notes.append(f"A4 probe: {len(exiled_oids)} objects moved into Exile in "
                 f"the pre->post window {exiled_oids}; zone transitions: "
                 f"{transitions[:20]}; "
                 f"choice_prompt_seen={ST['choice_prompt_seen']}.")
    if ST["trigger_resolved"] and len(exiled_oids) == 0 \
            and not ST["choice_prompt_seen"]:
        ass["A4_no_exile_window"] = "passed"
        notes.append("A4 passed (bug symptom): 0 objects were exiled by the "
                     "resolution window and no outside-the-game choice "
                     "prompt was ever offered -- the Unimplemented head is "
                     "a runtime no-op, the tracked set published empty, and "
                     "the ChangeZone sub-ability exiled nothing, exactly "
                     "the reported defect.")
    elif ST["trigger_resolved"]:
        ass["A4_no_exile_window"] = "failed"
        notes.append("A4 FAILED: objects were exiled in the window and/or "
                     "a choice prompt was offered -- the choose clause "
                     "appears to have functioned.")
    else:
        notes.append("A4 not-run: the trigger never resolved.")

    # A5: no choice-shaped prompt in the resolution window
    classes = {}
    for _ph, seat, iid, opp in ST["prompts_seen"]:
        classes.setdefault(ST["prompt_classes"].get(str(iid), "other"),
                           []).append((_ph, seat, str(iid)))
    notes.append("A5 probe: window prompt classes="
                 + json.dumps({k: len(v) for k, v in classes.items()}))
    for cls, items in classes.items():
        notes.append(f"  class {cls}: {len(items)} prompts "
                     f"{[(p, s) for p, s, _ in items][:8]}")
    n_choice = len(classes.get("choice_shaped", []))
    if n_choice == 0 and ST["trigger_resolved"]:
        ass["A5_no_choice_prompt"] = "passed"
        notes.append("A5 passed: no outside-the-game / choice-shaped card "
                     "prompt was offered during the trigger resolution "
                     "window (mana-payment and pass-priority prompts "
                     "excluded by the classifier).")
    elif ST["trigger_resolved"]:
        ass["A5_no_choice_prompt"] = "failed"
        notes.append(f"A5 FAILED: {n_choice} choice-shaped prompt(s) were "
                     f"offered in the window "
                     f"{classes.get('choice_shaped')}; the choose clause "
                     f"functioned at runtime.")
    else:
        notes.append("A5 not-run: the trigger never resolved.")

    # A6: cleanup
    if post_st:
        tnum = ST.get("activation_turn")
        cur_turn = post_st.get("turn_number")
        no_lingering = ST.get("cast_in_flight") is None
        if not stack_entries(post_st) and no_lingering and (
                tnum is None or (cur_turn or 0) > tnum):
            ass["A6_cleanup"] = "passed"
            notes.append("A6 passed: post stack empty, game advanced past "
                         f"the activation turn (activation turn {tnum}, "
                         f"post turn {cur_turn}, phase "
                         f"{post_st.get('phase')}), no in-flight "
                         f"submissions lingering "
                         f"(rejections={ST['cast_rejections']}).")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append("A6 FAILED: post stack non-empty, the game did not "
                         "advance past the activation turn, or an "
                         "in-flight submission is lingering "
                         f"(in_flight={ST.get('cast_in_flight')}, "
                         f"stack={len(stack_entries(post_st))}, "
                         f"turn {cur_turn} vs activation {tnum}).")
    else:
        notes.append("A6 not-run: no post state")

    # verdict
    if all(ass[k] == "passed" for k in ("A1_data_level", "A2_setup_ok",
                                        "A3_cast_resolved",
                                        "A4_no_exile_window",
                                        "A5_no_choice_prompt",
                                        "A6_cleanup")):
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: Nightmare Moon transformed into Princess "
            "Luna and the Transformed trigger resolved, but the 'choose up "
            "to six cards you own from outside the game' clause never "
            "parsed (Unimplemented head, runtime no-op), so no choice "
            "prompt was offered, the chain tracked set was allocated empty, "
            "and the ChangeZone sub-ability exiled 0 objects -- the exact "
            "structural defect the issue reports, now measured at runtime. "
            "Confirmed on v0.100.0. This is not a fix claim.")
    elif (ass["A1_data_level"] == "passed"
            and ass["A2_setup_ok"] == "passed"
            and ass["A3_cast_resolved"] == "passed"
            and (ass["A4_no_exile_window"] == "failed"
                 or ass["A5_no_choice_prompt"] == "failed")):
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: the transform trigger path was "
            "exercised and the choose clause functioned at runtime (a "
            "choice prompt was offered and/or cards were exiled). This is "
            "not a fix claim.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: the reported transform-trigger path "
                     "could not be fully exercised; see assertion notes.")

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
               if "nightmare moon" in l.lower()
               or "princess luna" in l.lower()
               or "unimplemented" in l.lower()
               or "tracked" in l.lower()
               or "outside the game" in l.lower()
               or (ST["game_code"] and ST["game_code"] in l)]
    with open(f"{EVDIR}/server_log_excerpt.txt", "w") as f:
        f.write("\n".join(excerpt[-150:]) + "\n")
    notes.append(f"server.log excerpt: {len(excerpt)} matching lines "
                 f"(of {len(lines)} total; log={used})")

    # copy the scenario into the evidence dir for provenance
    import shutil
    shutil.copy(f"{BACKFILL}/driver/scenario_{ISSUE}.py",
                f"{EVDIR}/scenario_{ISSUE}.py")

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
            "moon_cast": ST["moon_cast"],
            "moon_oid": ST["moon_oid"],
            "transform_activated": ST["transform_activated"],
            "activated_on_stack": ST["activated_on_stack"],
            "activation_resolved": ST["activation_resolved"],
            "trigger_on_stack": ST["trigger_on_stack"],
            "trigger_resolved": ST["trigger_resolved"],
            "activation_turn": ST["activation_turn"],
            "choice_prompt_seen": ST["choice_prompt_seen"],
            "choice_answered": ST["choice_answered"],
            "exile_window": ST["exile_window"],
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
            "the reported line (Nightmare Moon transform activation, "
            "Princess Luna Transformed trigger resolution) and observes "
            "the resolution outcome.",
            "4x Nightmare Moon / 56x Swamp deck densities are test-harness "
            "conveniences (engine accepts >4-of for custom games).",
            "The harness holds no outside-the-game card pool; the decisive "
            "observable is whether the choice prompt is ever offered, not "
            "the number of cards chosen.",
            "States are authoritative exports, restorable only via full "
            "game replay.",
        ],
        "setup_line": "P0: 4x Nightmare Moon + 56x Swamp; P1: 60x Swamp "
                      "(lands only, never casts, never attacks); default "
                      "Bo1, life 20",
        "contract_line": "Nightmare Moon {6} transform activation -> "
                         "Princess Luna Transformed trigger resolution: "
                         "correct = an outside-the-game choice is offered "
                         "and chosen cards are exiled. Observed (bug): the "
                         "choose clause never parsed (Unimplemented head, "
                         "runtime no-op), so no choice prompt is offered "
                         "and the ChangeZone sub-ability reads an empty "
                         "tracked set, exiling 0 objects.",
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
                            "p1_hand_n": len(hand_ids(state, 1)),
                            "mana_needs": ST["mana_needs"][tag],
                            "cast_in_flight": ST.get("cast_in_flight"),
                            "moon_oid": ST.get("moon_oid"),
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
                    if (not ST["trigger_resolved"]
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
                # a rejected activation/cast leaves no mana owed
                ST["mana_needs"][tag] = {"B": 0, "R": 0, "generic": 0}
                if ST.get("cast_in_flight"):
                    ST["cast_in_flight"] = None
                if ST.get("transform_activated") and \
                        not ST.get("activated_on_stack"):
                    # the activation submission itself was rejected; back
                    # out to the build phase so the stage machine retries
                    ST["transform_activated"] = False
                    ST["phase"] = "build"
                    ST["resolving_since"] = None
                    say("[P0] activation rejected; returning to build phase")
            st = c.latest
            if not st:
                continue
            state = st["state"]
            ST["states_seen"] += 1
            # ---- decisive window tracking ----
            if ST["phase"] == "resolving":
                if (ST["transform_activated"]
                        and not ST["activated_on_stack"]):
                    if activated_ability_on_stack(state) is not None:
                        ST["activated_on_stack"] = True
                        say("[window] transform activation observed on the "
                            "stack")
                        wire("activated_on_stack", {})
                if (ST["activated_on_stack"]
                        and not ST["activation_resolved"]):
                    if activated_ability_on_stack(state) is None:
                        ST["activation_resolved"] = True
                        say("[window] transform activation resolved")
                        wire("activation_resolved", {})
                        dump_stack_once(state, "activation_resolved")
                if (ST["activation_resolved"]
                        and not ST["trigger_on_stack"]):
                    if transform_trigger_on_stack(state) is not None:
                        ST["trigger_on_stack"] = True
                        say("[window] Princess Luna transform trigger "
                            "observed on the stack; exporting mid_stack")
                        wire("trigger_on_stack", {})
                        if not ST["mid_exported"]:
                            await export_as(c0_ref[0], "mid_stack")
                            ST["mid_exported"] = True
                        dump_stack_once(state, "trigger_on_stack")
                if ST["trigger_on_stack"] and not ST["trigger_resolved"]:
                    if transform_trigger_on_stack(state) is None:
                        ST["trigger_resolved"] = True
                        say("[window] Princess Luna trigger resolved; "
                            "starting settle clock")
                        wire("trigger_resolved", {})
                        ST["settle_at"] = time.time()
                # record the kinds of stack entries seen in the window
                for se in stack_entries(state):
                    kind = json.dumps(se.get("kind"), default=str)[:120]
                    if kind not in ST["stack_kinds_window"]:
                        ST["stack_kinds_window"].append(kind)
                # watchdog: the window must not hang the run forever
                if not ST["trigger_resolved"]:
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
                if (ST["trigger_resolved"] and not stack_entries(state)
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
                elif not ST["trigger_resolved"]:
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
    print(json.dumps({"verdict": verdict, "assertions": ST["ass"]},
                     indent=1))


if __name__ == "__main__":
    asyncio.run(main())
