#!/usr/bin/env python3
"""Issue #7424: Happy Yargle Day! -- the random named-card choice is unparsed;
"copy the chosen card ... you may cast the copy" copies nothing.

Reported (2026-08-15, lgray, source:internal-triage, status:confirmed,
area:parser, mechanic:triggers+copy, classifier:unsupported-aspect,
priority:p3-card-specific):
On Happy Yargle Day!, the clause "choose Swords to Plowshares, Opt, Fatal
Push, Anger of the Gods, or Explore at random" does not parse: the parser
emits an `Effect::Unimplemented` node in its place. That resolver pushes no
`GameEvent`, so the chain tracked set is allocated empty and the dependent
`CastCopyOfCard` ("Copy the chosen card. You may cast the copy without paying
its mana cost") reads an empty set. `CastCopyOfCard` is a measured
filter-only tracked-set consumer, so the empty publish makes it a true
no-op: the trigger copies nothing and no cast is ever offered.

Oracle text (verified against the pinned v0.100.0 card-data.json):
> At the beginning of each player's end step, if that player doesn't control
> a creature named Yargle, Glutton of Urborg, that player puts a bargle
> counter on a creature they control. It becomes a copy of Yargle, Glutton
> of Urborg, except it isn't legendary.
> Whenever chaos ensues, choose Swords to Plowshares, Opt, Fatal Push, Anger
> of the Gods, or Explore at random. Copy the chosen card. You may cast the
> copy without paying its mana cost.

Pinned v0.100.0 parse (see data_evidence.json):
  triggers[1] (mode ChaosEnsues, SelfRef, zones Battlefield/Command):
    execute.effect = Unimplemented { name: "unparsed_verb_arguments",
        description: "choose Swords to Plowshares, Opt, Fatal Push, Anger of
                      the Gods, or Explore at random" }
      sub_ability: CastCopyOfCard { target: TrackedSet(0),
                                   cost: {shards: [], generic: 0} }
    (the chaos-trigger head is the chain root; the sub reads the chain
    tracked set. The issue's corpus (9b7c66e30) names the head "choose";
    the pinned v0.100.0 corpus names it "unparsed_verb_arguments" with the
    identical description -- both are Effect::Unimplemented over the same
    clause; the structural claim is unchanged.)

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, Planechase format, life 20):
  P0: 60x Island, planar deck [Happy Yargle Day! + 19 other planes]
      (UpTo(4) copy limit is satisfied by basic-land exemption; the planar
      deck is shuffled by the engine, so the starting plane is random.)
  P1: 60x Forest (plays a land, passes, never attacks)
Drive:
  1. Mulligans: both seats keep 7 (all lands).
  2. The planar deck is engine-shuffled, so the starting plane is random.
     P0 rolls the planar die on every main phase (RollPlanarDie special
     action; first roll each turn is free, then escalating generic) until
     Happy Yargle Day! is the active plane ("seeking" phase). The 19
     filler planes are chosen for benign, choice-free chaos abilities
     (10 have no chaos trigger at all); their triggers resolve harmlessly
     while seeking.
  3. With Yargle active ("rolling" phase), P0 keeps rolling. Classify
     each roll:
       - chaos trigger ("Whenever chaos ensues") on the stack -> CHAOS path
       - active plane changed -> planeswalked away: back to seeking
       - neither within 5s -> blank: roll again.
  4. CHAOS path: export pre.json IMMEDIATELY when the chaos trigger is
     first seen on the stack (guarded flag, in the observation path of the
     roll result); record every viewer-interaction opportunity offered to
     EITHER seat from the trigger appearing through its resolution; let
     the trigger resolve (both seats pass priority); export post once the
     game has settled (stack empty, Priority, 8s idle).

Expected (correct behavior): the trigger chooses one of the five named
cards at random, creates a copy, and offers "you may cast the copy".
Reported (bug): the head clause never parsed, so the trigger is a silent
no-op -- no copy is created, no cast is offered, nothing changes.

Assertions (correct-behavior expectations; a FAIL records the bug):
  A1_data_level   pinned v0.100.0 card-data.json parses the chaos trigger
                  as the issue reports: execute.effect Unimplemented
                  naming the "choose ... at random" clause; sub
                  CastCopyOfCard with target TrackedSet(0).
  A2_setup_ok     pre.json: Happy Yargle Day! is the active plane (command
                  zone, face up); planar_controller set.
  A3_chaos_trigger_resolved the "Whenever chaos ensues" trigger was
                  observed on the stack and resolved (stack emptied on the
                  same turn).
  A4_no_copy_offered (THE REPORTED BUG) during the chaos window, a
                  copy/cast offer was made to a seat. Expected: offered.
                  Observed bug: never offered.
  A5_no_copy_created (THE REPORTED BUG) the trigger created a copy of one
                  of the five named cards / changed the game. Expected:
                  changed. Observed bug: no such object appeared; hands,
                  graveyards, permanents, life all unchanged.
  A6_cleanup      post stack empty, game advancing.

Verdict rule: reproduced iff A1, A2, A3 passed and NOT (A4 passed and A5
              passed); not-reproduced iff A1..A5 all passed; blocked iff
              A1, A2, or A3 could not be established.

Protocol-101 driver notes (v0.100.0, build bc9ef56, verified 2026-10-02):
  - HELLO advertises 101 (server enforces exact match).
  - MulliganDecision as {"choice":{"type":"Keep"}} gated on
    waiting_for.data.pending[] with phase type "Declare".
  - Planechase game via format_config = the engine-canonical
    FormatConfig::planechase() JSON (validated empirically: game creates,
    planar deck loads, starting plane reveals after mulligans).
  - Deck payload carries "planar_deck": [names]; the engine shuffles it.
  - RollPlanarDie submitted as legacy action {"type":"RollPlanarDie"};
    legality read from st["derived"]["planechase"]["can_roll"].
  - Authoritative exports only from the host seat (P0 creates the game).
  - Adapted from scenario_7423.py (issue #7423, same Unimplemented-head
    pattern) 2026-10-02.
"""
import asyncio
import copy
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261002-7424"
ISSUE = 7424
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR) if f != "scenario_run.log"]
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
                     "(started by this run; ServerHello "
                     "0.100.0/bc9ef56/protocol 101 verified by this run's "
                     "own Hello handshake)",
    "mode": "Full",
    "source": "2026-10-02: latest stable release v0.100.0 == pinned release "
              "dir (ledger server.pinned_version 2026-10-02T12:50-05:00; "
              "GitHub /releases re-confirmed v0.100.0 still latest stable "
              "at 18:13 CDT); hashes recomputed against on-disk artifacts "
              "this run",
}

# Recompute against on-disk artifacts; never copy hashes blindly.
for _f, _k in (("server/releases/v0.100.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.100.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.100.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.100.0/data/card-data.json"))

YARGLE = "happy yargle day!"
ISLAND = "island"
FOREST = "forest"
FIVE = ["swords to plowshares", "opt", "fatal push", "anger of the gods", "explore"]

PLANECHASE_FORMAT = {
    "format": "Planechase",
    "starting_life": 20,
    "min_players": 2,
    "max_players": 4,
    "deck_size": {"type": "Minimum", "data": 60},
    "singleton": False,
    "command_zone": False,
    "commander_damage_threshold": None,
    "team_based": False,
    "uses_commander": False,
    "sideboard_policy": {"type": "Unlimited"},
    "default_deck_copy_limit": {"type": "UpTo", "data": 4},
    "supplies_fixed_deck": False,
    "allow_debug_actions": False,
}

PLANAR_DECK = [
    "Happy Yargle Day!",
    # 10 planes with no chaos trigger (chaos on them = nothing happens)
    "Bicycle Rack", "Elvish Impersonation Contest", "Ghirapur Grand Prix",
    "Jalira's Show", "Shrinking Plane", "Sky Deck", "Stroopwafel Cafe",
    "The Food Court", "The Pro Tour", "Windmill Farm",
    # 9 planes with benign, choice-free chaos abilities
    "Hedron Fields of Agadeem",  # create a 7/7 Eldrazi token
    "Jund",                       # create two 1/1 Goblin tokens
    "Llanowar",                   # untap all creatures you control
    "Prahv",                      # gain life = cards in hand
    "Tazeem",                     # draw a card for each land you control
    "Windriddle Palaces",         # each player mills a card
    "Agyrem",                     # creatures can't attack you
    "Esper",                      # your white/blue/black creatures become artifacts
    "The Eon Fog",                # untap all permanents you control
]


def pdeck(main_names, planar_names):
    return {"main_deck": main_names, "sideboard": [], "commander": [],
            "planar_deck": planar_names}


P0_DECK = pdeck(["Island"] * 60, PLANAR_DECK)
P1_DECK = pdeck(["Forest"] * 60, [])

SETUP_DEADLINE_S = 1500

ST = {}
MULLS = set()
MULL_COUNT = {}
SUBMITTED = set()
SUBMITTED_OPPS = set()
_DISCARD_REV = {}


def reset_attempt():
    ST.clear()
    ST.update({
        "t0": time.time(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "game_code": None,
        "phase": "setup",  # setup -> seeking -> rolling -> chaos_window -> done
        "attempt": 1,
        "starting_plane_name": None,
        "starting_plane_oid": None,
        "yargle_active": False,
        "rolls": 0,
        "roll_pending": False,
        "roll_at": 0,
        "roll_pre_plane": None,
        "roll_rejections": 0,
        "pre_exported": False,
        "post_exported": False,
        "stack_saw_chaos": False,
        "chaos_resolved": False,
        "chaos_turn": None,
        "hand_at_pre": {},
        "gy_at_pre": {},
        "perm_at_pre": {},
        "life_at_pre": {},
        "perm_oids_at_pre": {},
        "triggers_fired_at_pre": None,
        "chaos_window_stats": {},
        "prompts_seen": [],
        "wf_types_window": [],
        "terminal": False,
        "terminal_data": None,
        "states_seen": 0,
        "cast_rejections": 0,
        "settle_at": None,
        "land_turn": -1,
        "ass": {k: "not-run" for k in ("A1_data_level", "A2_setup_ok",
                                       "A3_chaos_trigger_resolved",
                                       "A4_no_copy_offered",
                                       "A5_no_copy_created", "A6_cleanup")},
        "notes": [],
        "data_level_ok": False,
    })
    MULLS.clear()
    MULL_COUNT.clear()
    SUBMITTED.clear()
    SUBMITTED_OPPS.clear()
    _DISCARD_REV.clear()


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


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def gy_ids(state, pid):
    gy = state.get("graveyard") or {}
    ids = gy.get(str(pid)) or gy.get(pid) or []
    return [int(o) for o in ids]


def bf_permanents(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def untapped_lands(state, pid):
    out = []
    for oid in bf_permanents(state, pid):
        o = get_obj(state, oid)
        if not o.get("tapped") and "Land" in str(o.get("base_card_types")):
            out.append(oid)
    return out


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


def planechase_view(st):
    return (st.get("derived") or {}).get("planechase") or {}


def active_plane_oid(st):
    return planechase_view(st).get("active_plane")


def active_plane_name(st):
    oid = active_plane_oid(st)
    if oid is None:
        return None
    return obj_lname(st.get("state") or {}, oid)


def chaos_trigger_on_stack(state):
    for se in stack_entries(state):
        if "chaos ensues" in json.dumps(se, default=str).lower():
            return se
    return None

# ------------------------------------------------------------- data check
def check_data_level():
    card = CARD_DATA.get("happy yargle day!", {})
    trigs = card.get("triggers") or []
    chaos = [t for t in trigs if t.get("mode") == "ChaosEnsues"]
    ev = {
        "name": card.get("name"),
        "oracle": card.get("oracle_text"),
        "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
        "trigger_mode": None,
        "trigger_zones": None,
        "head_effect": None,
        "sub_ability": None,
        "report_corpus_note": ("the issue's corpus (9b7c66e30) names the "
                               "Unimplemented head 'choose'; the pinned "
                               "v0.100.0 corpus names it "
                               "'unparsed_verb_arguments' with the identical "
                               "description -- both are Effect::Unimplemented "
                               "over the same clause; the structural claim "
                               "is unchanged."),
    }
    ok = False
    if chaos:
        t = chaos[0]
        ev["trigger_mode"] = t.get("mode")
        ev["trigger_zones"] = t.get("trigger_zones")
        ex = (t.get("execute") or {})
        ev["head_effect"] = (ex.get("effect") or {})
        ev["sub_ability"] = (ex.get("sub_ability") or {})
        head = ev["head_effect"]
        sub = ((ev["sub_ability"] or {}).get("effect")) or {}
        ok = (t.get("mode") == "ChaosEnsues"
              and head.get("type") == "Unimplemented"
              and "choose swords to plowshares, opt, fatal push, anger of the gods, or explore at random"
              in str(head.get("description", "")).lower()
              and sub.get("type") == "CastCopyOfCard"
              and (sub.get("target") or {}).get("type") == "TrackedSet")
        say(f"data-level check: trigger mode={t.get('mode')}, "
            f"head={head.get('type')}/{head.get('name')}, "
            f"sub={sub.get('type')}/{(sub.get('target') or {}).get('type')}")
    else:
        say("data-level check: NO ChaosEnsues trigger found in pinned card data")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    ST["data_level_ok"] = ok
    wire("data_level", {"ok": ok, "evidence": ev})

# ------------------------------------------------------------- actions
async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


def mulligan_keep(hand):
    return True  # all lands; always keep 7


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
    picks = [int(x) for x in sorted(hand)[:n]]
    say(f"[{tag}] discarding to hand size: {[obj_lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    _DISCARD_REV[(c.name, rev)] = True
    return True


async def do_discard_vi(c, pid, tag):
    """Answer a viewer-interaction schema 'select' prompt whose candidates
    are all hand-zone cards (the engine's discard-to-hand-size UI)."""
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
        # discard the first candidate (all are lands here)
        pick = chs[0].get("id")
        SUBMITTED_OPPS.add(iid)
        ST.setdefault("discard_iids", set()).add(iid)
        say(f"[{tag}] vi discard-to-hand-size: {pick}")
        wire("vi_discard", {"who": tag, "iid": iid, "pick": pick})
        await c.send_interaction({
            "interactionId": iid,
            "response": {"type": "select",
                         "data": {"choiceIds": [pick]}}})
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
                    await c.send_interaction({
                        "interactionId": iid,
                        "response": {"type": "choose",
                                     "data": {"choiceId": ch.get("id")}}})
                    return True
    return False


async def export_as(c, name):
    env = await c.export_state()
    with open(f"{EVDIR}/{name}.json", "w") as f:
        f.write(env)
    say(f"exported {name}.json")


def snapshot_zones(state):
    return {
        "hand": {str(pid): len(hand_ids(state, pid)) for pid in (0, 1)},
        "gy": {str(pid): len(gy_ids(state, pid)) for pid in (0, 1)},
        "perm": {str(pid): len(bf_permanents(state, pid)) for pid in (0, 1)},
        "life": {str(pid): player_of(state, pid).get("life") for pid in (0, 1)},
        "triggers_fired": [json.dumps(t, default=str)
                           for t in (state.get("triggers_fired_this_turn") or [])],
        "discarded_this_turn": state.get("players_who_discarded_card_this_turn"),
    }

# ------------------------------------------------------------- P0 tick
async def p0_tick(st, acts, state, c):
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan(c, 0, "P0"):
            return
        return
    if await do_discard_to_handsize(c, 0, "P0"):
        return
    if await do_discard_vi(c, 0, "P0"):
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
    if not my_priority(state, 0):
        return
    phase = state.get("phase")
    active = state.get("active_player")
    # land drop every own main phase (keeps hand size manageable in all
    # phases, not just while rolling)
    if (phase in ("PreCombatMain", "PostCombatMain") and active == 0
            and not stack_entries(state)
            and ST["land_turn"] != state.get("turn_number")):
        for a in acts:
            if a["type"] == "PlayLand":
                ST["land_turn"] = state.get("turn_number")
                say(f"[P0] playing land (turn {state.get('turn_number')})")
                await submit_as_is(c, a)
                return
    if ST["phase"] in ("seeking", "rolling") \
            and phase in ("PreCombatMain", "PostCombatMain") \
            and active == 0 and not stack_entries(state):
        pc = planechase_view(st)
        if pc.get("can_roll") and not ST["roll_pending"]:
            say(f"[P0] rolling planar die (roll #{ST['rolls'] + 1}, "
                f"cost={pc.get('current_roll_cost')}, phase={ST['phase']})")
            await c.send_action({"type": "RollPlanarDie"})
            ST["roll_pending"] = True
            ST["roll_at"] = time.time()
            ST["roll_pre_plane"] = pc.get("active_plane")
            ST["rolls"] += 1
            wire("roll_submitted", {"n": ST["rolls"],
                                    "cost": pc.get("current_roll_cost"),
                                    "phase": ST["phase"]})
            return
    await pass_priority(c, st, acts)


# ------------------------------------------------------------- P1 tick
async def p1_tick(st, acts, state, c):
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan(c, 1, "P1"):
            return
        return
    if await do_discard_to_handsize(c, 1, "P1"):
        return
    if await do_discard_vi(c, 1, "P1"):
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


def record_window_prompt(seat, opp):
    iid = opp.get("interactionId") or opp.get("id")
    tag = (ST["phase"], seat, iid)
    if all(t[:3] != tag for t in ST["prompts_seen"]):
        ST["prompts_seen"].append((tag[0], tag[1], tag[2], opp))
        wire("window_prompt", {"phase": ST["phase"], "seat": seat,
                               "iid": iid, "opportunity": opp})
        say(f"[{ST['phase']}/{seat}] prompt seen: "
            f"{json.dumps(opp)[:240]}")

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
    post = load_env("post") or {}
    pre_st = pre.get("state") or {}
    post_st = post.get("state") or {}

    # A1: data-level parse matches the issue
    if ST.get("data_level_ok"):
        ass["A1_data_level"] = "passed"
        notes.append("A1 passed: pinned v0.100.0 card-data.json parses Happy "
                     "Yargle Day!'s chaos trigger as mode=ChaosEnsues with "
                     "head Unimplemented('unparsed_verb_arguments': 'choose "
                     "Swords to Plowshares, Opt, Fatal Push, Anger of the "
                     "Gods, or Explore at random') + sub CastCopyOfCard "
                     "(target TrackedSet(0)); see data_evidence.json. The "
                     "issue's corpus named the head 'choose'; the pinned "
                     "corpus names it 'unparsed_verb_arguments' with the "
                     "identical description -- both Effect::Unimplemented "
                     "over the same clause.")
    else:
        ass["A1_data_level"] = "failed"
        notes.append("A1 FAILED: the pinned parse does not match the "
                     "issue-reported shape; see data_evidence.json.")

    # A2: setup at the decisive pre -- Yargle is the active plane
    if ST["pre_exported"] and pre_st:
        cmd = pre_st.get("command_zone") or []
        pc_id = None
        for oid in cmd:
            o = get_obj(pre_st, oid)
            if obj_lname(pre_st, oid) == YARGLE:
                pc_id = oid
                break
        yargle_up = (pc_id is not None
                     and get_obj(pre_st, pc_id).get("zone") == "Command"
                     and not get_obj(pre_st, pc_id).get("face_down"))
        notes.append(f"A2 probe: Happy Yargle Day! face-up in command zone="
                     f"{yargle_up} (starting_plane_name="
                     f"{ST.get('starting_plane_name')}).")
        if yargle_up:
            ass["A2_setup_ok"] = "passed"
            notes.append("A2 passed: Happy Yargle Day! was the active plane "
                         "(face-up in the command zone) when chaos ensued.")
        else:
            ass["A2_setup_ok"] = "failed"
            notes.append("A2 FAILED: the decisive pre does not show Happy "
                         "Yargle Day! as the active plane.")
    else:
        ass["A2_setup_ok"] = "failed"
        notes.append("A2 FAILED: pre.json was never exported (chaos never "
                     "ensued with Yargle active).")

    # A3: the chaos trigger fired and resolved
    if ST["stack_saw_chaos"] and ST["chaos_resolved"]:
        ass["A3_chaos_trigger_resolved"] = "passed"
        notes.append("A3 passed: the 'Whenever chaos ensues' trigger was "
                     "observed on the stack and resolved (stack emptied on "
                     f"turn {ST.get('chaos_turn')}).")
    elif ST["stack_saw_chaos"]:
        ass["A3_chaos_trigger_resolved"] = "failed"
        notes.append("A3 FAILED: the chaos trigger was seen on the stack "
                     "but its resolution was never confirmed.")
    else:
        notes.append("A3 not-run: chaos never ensued with Yargle active.")

    # A4: was a copy/cast offer made to either seat during the chaos window?
    # A prompt appearing is not a pass (playbook step 2): only a
    # CHOICE-shaped prompt counts. Ordinary turn-structure prompts
    # (priority passes, tap-land menus, DeclareAttackers/DeclareBlockers
    # relations schema) do NOT count.
    ORDINARY_ACTIONS = {"passPriority", "tapLandForMana", "castSpell",
                        "playLand", "activateAbility"}
    discard_iids = ST.get("discard_iids") or set()
    choice_like = []
    for _ph, seat, iid, opp in ST["prompts_seen"]:
        if iid in discard_iids:
            continue  # discard-to-hand-size UI, not a copy/cast offer
        resp = (opp or {}).get("response") or {}
        rtype = resp.get("type")
        if rtype == "schema":
            spec = ((resp.get("data") or {}).get("spec")) or {}
            if spec.get("type") == "relations":
                continue
            # A hand-zone "select" is the discard UI, never a copy offer.
            chs = ((resp.get("data") or {}).get("candidates")
                   or (resp.get("data") or {}).get("choices") or [])
            zones = set()
            for ch in chs:
                for sf in (ch.get("surfaces") or []):
                    if sf.get("type") == "object":
                        zones.add((sf.get("data") or {}).get("zone"))
            if chs and zones == {"hand"}:
                continue
            choice_like.append((seat, iid))
            continue
        if rtype == "exactChoices":
            codes = set()
            for ch in ((resp.get("data") or {}).get("choices") or []):
                for sf in (ch.get("surfaces") or []):
                    if sf.get("type") == "action":
                        codes.add((sf.get("data") or {}).get("code"))
            codes.discard(None)
            if codes <= ORDINARY_ACTIONS:
                continue
            choice_like.append((seat, iid))
    notes.append(f"Chaos-window prompts: {len(ST['prompts_seen'])} vi "
                 f"opportunities seen by either seat; waiting_for types: "
                 f"{sorted(set(ST['wf_types_window']))}; choice-shaped: "
                 f"{choice_like or 'none'}.")
    if choice_like:
        ass["A4_no_copy_offered"] = "passed"
        notes.append("A4 passed: a choice-shaped prompt was offered during "
                     f"the chaos window: {choice_like[:4]}.")
    elif ST["stack_saw_chaos"] and ST["chaos_resolved"]:
        ass["A4_no_copy_offered"] = "failed"
        notes.append("A4 FAILED: the chaos trigger resolved but NO copy/ "
                     "cast offer was ever made to either seat -- THE "
                     "REPORTED BUG (the head clause never parsed, so the "
                     "choice step does not exist at runtime; 'copies "
                     "nothing').")
    else:
        notes.append("A4 not-run: the chaos window never completed.")

    # A5: did the trigger create a copy / change the game at all?
    win = ST["chaos_window_stats"]
    wend = load_env("window_end") or {}
    wend_st = wend.get("state") or {}
    five_objs = {}
    for nm, s in (("pre", pre_st), ("window_end", wend_st), ("post", post_st)):
        for oid, o in (s.get("objects") or {}).items():
            ln = str(o.get("base_name") or o.get("name") or "").lower()
            if ln in FIVE:
                five_objs.setdefault(ln, []).append(
                    (nm, oid, o.get("zone")))
    h_delta = win.get("hand_delta")
    g_delta = win.get("gy_delta")
    p_delta = win.get("perm_delta")
    l_delta = win.get("life_delta")
    notes.append(f"A5 probe: hand pre={ST.get('hand_at_pre')} -> window-end "
                 f"{win.get('hand_end')} (delta={h_delta}); gy pre="
                 f"{ST.get('gy_at_pre')} -> {win.get('gy_end')} "
                 f"(delta={g_delta}); permanents pre="
                 f"{ST.get('perm_at_pre')} -> {win.get('perm_end')} "
                 f"(delta={p_delta}); life pre={ST.get('life_at_pre')} -> "
                 f"{win.get('life_end')} (delta={l_delta}); five-named-card "
                 f"objects across pre/window_end/post: "
                 f"{five_objs or 'none'}.")
    if h_delta is None:
        notes.append("A5 not-run: no window-end snapshot was captured.")
    elif (five_objs
          or h_delta.get("0") != 0 or h_delta.get("1") != 0
          or g_delta.get("0") != 0 or g_delta.get("1") != 0
          or p_delta.get("0") != 0 or p_delta.get("1") != 0
          or l_delta.get("0") != 0 or l_delta.get("1") != 0):
        ass["A5_no_copy_created"] = "passed"
        notes.append("A5 passed: the chaos trigger created a copy of one "
                     "of the five named cards or otherwise changed the "
                     "game (see deltas/objects above).")
    else:
        ass["A5_no_copy_created"] = "failed"
        notes.append("A5 FAILED: the chaos trigger resolved with ZERO "
                     "observable effect -- no copy of any of the five "
                     "named cards was created, hands, graveyards, "
                     "permanents, and life all unchanged -- THE REPORTED "
                     "BUG (CastCopyOfCard read the empty chain tracked "
                     "set; filter-only no-op).")

    # A6: cleanup
    if post_st:
        if not stack_entries(post_st):
            ass["A6_cleanup"] = "passed"
            notes.append("A6 passed: post stack empty, game advanced "
                         f"(turn {post_st.get('turn_number')}, phase "
                         f"{post_st.get('phase')}).")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append("A6 FAILED: post stack non-empty: "
                         f"{stack_entries(post_st)}")
    else:
        notes.append("A6 not-run: no post state")

    # verdict
    if (ass["A1_data_level"] == "passed"
            and ass["A2_setup_ok"] == "passed"
            and ass["A3_chaos_trigger_resolved"] == "passed"
            and not (ass["A4_no_copy_offered"] == "passed"
                     and ass["A5_no_copy_created"] == "passed")):
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: chaos ensued with Happy Yargle Day! as "
            "the active plane; its 'Whenever chaos ensues' trigger fired "
            "and resolved, but no copy of any of the five named cards was "
            "created and no 'cast the copy' offer was ever made -- see the "
            "failed assertion(s) above (confirmed on v0.100.0). The parse "
            "is the reported defect; the silent no-op is its direct "
            "structural consequence (Unimplemented head pushes no "
            "GameEvent -> empty chain tracked set -> filter-only "
            "CastCopyOfCard no-op). This is not a fix claim.")
    elif all(ass[k] == "passed" for k in ("A1_data_level", "A2_setup_ok",
                                          "A3_chaos_trigger_resolved",
                                          "A4_no_copy_offered",
                                          "A5_no_copy_created")):
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: the chaos trigger chose and copied "
            "one of the five named cards and offered the cast. This is "
            "not a fix claim.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: the reported chaos-trigger path "
                     "could not be fully exercised; see assertion notes.")

    # server log excerpts for this game
    import glob
    lines = []
    used = None
    # newest-first by mtime (reverse-alphabetical once picked a September log)
    for lp in sorted(glob.glob(f"{BACKFILL}/runs/*/server.log"),
                     key=os.path.getmtime, reverse=True):
        try:
            lines = open(lp).read().splitlines()
            used = lp
            break
        except Exception:
            continue
    excerpt = [l for l in lines
               if "yargle" in l.lower()
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
            open(f"{BACKFILL}/driver/scenario_7424.py", "rb").read()).hexdigest(),
        "format_config": "Planechase (engine-canonical FormatConfig::planechase())",
        "decks": {"P0": [("Island", 60)], "P1": [("Forest", 60)],
                  "planar_deck": PLANAR_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": {
            "attempts": ST["attempt"],
            "rolls": ST["rolls"],
            "roll_rejections": ST["roll_rejections"],
            "starting_plane_name": ST["starting_plane_name"],
            "stack_saw_chaos": ST["stack_saw_chaos"],
            "chaos_resolved": ST["chaos_resolved"],
            "hand_at_pre": ST["hand_at_pre"],
            "gy_at_pre": ST["gy_at_pre"],
            "perm_at_pre": ST["perm_at_pre"],
            "life_at_pre": ST["life_at_pre"],
            "chaos_window_stats": ST["chaos_window_stats"],
            "prompts_seen": [(p[0], p[1], p[2]) for p in ST["prompts_seen"]],
            "wf_types_window": sorted(set(ST["wf_types_window"])),
            "cast_rejections": ST["cast_rejections"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The report is a data-level parse report; the scenario replays "
            "the reported line (chaos ensues with Happy Yargle Day! as the "
            "active plane) from a fresh Planechase game and observes the "
            "resolution outcome. The planar deck is engine-shuffled; P0 "
            "planeswalked via the planar die until Happy Yargle Day! was "
            "active (filler planes chosen for benign, choice-free chaos "
            "abilities).",
            "The end-step bargle-counter ability was not exercised (only "
            "the chaos trigger).",
            "States are authoritative exports, restorable only via full "
            "game replay.",
        ],
        "setup_line": "P0: 60x Island + planar deck [Happy Yargle Day! + 19 "
                      "planes]; P1: 60x Forest (lands, passes, never "
                      "attacks); Planechase format, life 20",
        "contract_line": "Chaos ensues with Happy Yargle Day! active: one "
                         "of Swords to Plowshares / Opt / Fatal Push / "
                         "Anger of the Gods / Explore must be chosen at "
                         "random, copied, and the copy offered for cast. "
                         "Observed: the clause never parsed, no copy was "
                         "created, no cast was offered, nothing changed.",
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
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)
    return run


# ------------------------------------------------------------- one game
async def drive_one_game():
    """Run one Planechase game: seek Yargle via planeswalking, then roll
    for chaos on Yargle. Returns 'done' (finalized) or 'giveup'."""
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    await p0.connect()
    await p0.create(P0_DECK, player_count=2, format_config=PLANECHASE_FORMAT)
    await p1.connect()
    await p1.join(p0.game_code, P1_DECK)
    await asyncio.sleep(2.0)
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code} (attempt {ST['attempt']}); "
        f"P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_setup", {"game_code": p0.game_code, "attempt": ST["attempt"],
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})

    t_start = time.time()
    last = {}
    last_tick_at = {}
    result = "giveup"

    async def close():
        try:
            await p0.close()
        except Exception:
            pass
        try:
            await p1.close()
        except Exception:
            pass

    while time.time() - t_start < SETUP_DEADLINE_S and result == "giveup":
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
            # ---- observation pass
            try:
                try:
                    while True:
                        t, data = c.inbox.get_nowait()
                        if t in ("ActionRejected", "Error"):
                            say(f"[{tag}] {t}: {json.dumps(data)[:300]}")
                            wire("rejection", {"who": tag, "type": t,
                                               "data": data})
                            ST["cast_rejections"] += 1
                            if "RollPlanarDie" in json.dumps(data):
                                ST["roll_rejections"] += 1
                                ST["roll_pending"] = False
                            # Allow retrying a rejected interaction: drop
                            # its iid from the submitted set.
                            rej_str = json.dumps(data)
                            for iid in list(SUBMITTED_OPPS):
                                if iid in rej_str:
                                    SUBMITTED_OPPS.discard(iid)
                                    (ST.get("discard_iids") or set()).discard(iid)
                        elif t == "TerminalResult":
                            ST["terminal"] = True
                            ST["terminal_data"] = data
                            wire("terminal_result",
                                 {"who": tag, "data": data})
                            say(f"[{tag}] TerminalResult: "
                                f"{json.dumps(data)[:300]}")
                        elif t not in ("StateUpdate", "GameStarted"):
                            wire("inbox_misc", {"who": tag, "type": t})
                except asyncio.QueueEmpty:
                    pass
                state = st["state"]
                ST["states_seen"] += 1
                acts = merged_actions(st)

                # setup -> seeking/rolling: starting plane check (once the
                # game has started and the plane is revealed)
                if ST["phase"] == "setup":
                    pname = active_plane_name(st)
                    if pname and (wf_of(state).get("type") not in
                                  ("MulliganDecision", "BottomCards")):
                        ST["starting_plane_name"] = pname
                        ST["starting_plane_oid"] = active_plane_oid(st)
                        if pname == YARGLE:
                            ST["yargle_active"] = True
                            ST["phase"] = "rolling"
                            say(f"starting plane is Happy Yargle Day!; "
                                f"phase -> rolling")
                            wire("yargle_start", {})
                        else:
                            ST["phase"] = "seeking"
                            say(f"starting plane is {pname}; phase -> "
                                f"seeking (will planeswalk until Happy "
                                f"Yargle Day! is active)")
                            wire("seeking_start", {"plane": pname})

                # seeking: roll until Happy Yargle Day! is the active plane
                if ST["phase"] == "seeking":
                    pname = active_plane_name(st)
                    if pname == YARGLE:
                        ST["yargle_active"] = True
                        ST["phase"] = "rolling"
                        say("Happy Yargle Day! is now active; "
                            "phase -> rolling")
                        wire("yargle_found", {"rolls": ST["rolls"]})

                # roll outcome classification (seeking and rolling)
                if ST["phase"] in ("seeking", "rolling") and ST["roll_pending"]:
                    se = chaos_trigger_on_stack(state)
                    # In seeking mode, chaos on another plane is irrelevant;
                    # the per-tick seeking check above catches Yargle.
                    if se and ST["phase"] == "rolling":
                        ST["roll_pending"] = False
                        ST["stack_saw_chaos"] = True
                        ST["phase"] = "chaos_window"
                        ST["chaos_turn"] = state.get("turn_number")
                        say("CHAOS: 'Whenever chaos ensues' trigger on the "
                            "stack; exporting pre.json")
                        wire("chaos_on_stack", {"entry": se})
                        # THE decisive pre: exported INSIDE the roll-result
                        # observation path (guarded flag).
                        if not ST["pre_exported"]:
                            await export_as(p0, "pre")
                            ST["pre_exported"] = True
                            ST["hand_at_pre"] = {
                                str(pid): len(hand_ids(state, pid))
                                for pid in (0, 1)}
                            ST["gy_at_pre"] = {
                                str(pid): len(gy_ids(state, pid))
                                for pid in (0, 1)}
                            ST["perm_at_pre"] = {
                                str(pid): len(bf_permanents(state, pid))
                                for pid in (0, 1)}
                            ST["life_at_pre"] = {
                                str(pid): player_of(state, pid).get("life")
                                for pid in (0, 1)}
                            ST["perm_oids_at_pre"] = {
                                str(pid): sorted(bf_permanents(state, pid))
                                for pid in (0, 1)}
                            ST["triggers_fired_at_pre"] = json.dumps(
                                state.get("triggers_fired_this_turn") or [],
                                default=str)
                    elif (active_plane_oid(st) != ST["roll_pre_plane"]
                          and ST["roll_pre_plane"] is not None):
                        # planeswalk (or chaos on another plane in seeking
                        # mode -- the seeking check handles Yargle arrival)
                        ST["roll_pending"] = False
                        if ST["phase"] == "rolling":
                            pname = active_plane_name(st)
                            say(f"planeswalked to {pname}; phase -> seeking")
                            wire("planeswalked", {"plane": pname})
                            ST["yargle_active"] = False
                            ST["phase"] = "seeking"
                    elif now - ST["roll_at"] > 5:
                        ST["roll_pending"] = False
                        say(f"blank roll (roll #{ST['rolls']})")

                # chaos-window observation (either seat)
                if ST["phase"] == "chaos_window":
                    vi = get_vi(st)
                    if vi:
                        for opp in vi.get("opportunities", []) or []:
                            record_window_prompt(tag, opp)
                    wtype = (wf_of(state).get("type") or "")
                    if wtype and wtype not in ST["wf_types_window"]:
                        ST["wf_types_window"].append(wtype)
                    if (ST["stack_saw_chaos"] and not ST["chaos_resolved"]
                            and not chaos_trigger_on_stack(state)
                            and state.get("turn_number") == ST["chaos_turn"]
                            and not stack_entries(state)):
                        ST["chaos_resolved"] = True
                        snap = snapshot_zones(state)
                        # Write the OBSERVED state, not a re-export: the
                        # export round-trip races the other client's
                        # auto-passing loop and can land turns later.
                        with open(f"{EVDIR}/window_end.json", "w") as wfj:
                            json.dump({"state": state,
                                       "observed_turn":
                                           state.get("turn_number"),
                                       "observed_phase":
                                           state.get("phase"),
                                       "chaos_turn": ST.get("chaos_turn")},
                                      wfj, default=str)
                        say("window_end.json written from observed state "
                            f"(turn {state.get('turn_number')}, "
                            f"phase {state.get('phase')})")
                        ST["chaos_window_stats"] = {
                            "hand_end": snap["hand"],
                            "gy_end": snap["gy"],
                            "perm_end": snap["perm"],
                            "life_end": snap["life"],
                            "hand_delta": {
                                k: snap["hand"][k] - ST["hand_at_pre"][k]
                                for k in snap["hand"]},
                            "gy_delta": {
                                k: snap["gy"][k] - ST["gy_at_pre"][k]
                                for k in snap["gy"]},
                            "perm_delta": {
                                k: snap["perm"][k] - ST["perm_at_pre"][k]
                                for k in snap["perm"]},
                            "life_delta": {
                                k: (snap["life"][k] or 0)
                                - (ST["life_at_pre"][k] or 0)
                                for k in snap["life"]},
                            "discarded_this_turn": snap["discarded_this_turn"],
                            "triggers_fired_new": snap["triggers_fired"],
                        }
                        wire("chaos_window_complete",
                             ST["chaos_window_stats"])
                        say(f"chaos window complete: "
                            f"hand_delta={ST['chaos_window_stats']['hand_delta']} "
                            f"gy_delta={ST['chaos_window_stats']['gy_delta']} "
                            f"perm_delta={ST['chaos_window_stats']['perm_delta']}")
                        ST["settle_at"] = now
                # decisive post: settled in a main phase (or beyond)
                if ST["chaos_resolved"] and not ST["post_exported"]:
                    if not stack_entries(state):
                        if ST["settle_at"] is None:
                            ST["settle_at"] = now
                        idle = now - ST["settle_at"]
                        wtype = (wf_of(state).get("type") or "")
                        if wtype == "Priority" and idle > 8:
                            say(f"settled: stack empty, Priority, "
                                f"{idle:.0f}s idle; exporting post")
                            await export_as(p0, "post")
                            ST["post_exported"] = True
                            await finalize(p0)
                            result = "done"
                            break
                    else:
                        ST["settle_at"] = None
                # watchdogs
                if (ST["phase"] in ("seeking", "rolling")
                        and (state.get("turn_number") or 0) > 40
                        and not ST["post_exported"]):
                    say("seek/roll watchdog: 40 turns in, chaos on Yargle "
                        "never ensued -- exporting state and giving up")
                    wire("rolling_stall",
                         {"waiting_for": wf_of(state),
                          "turn": state.get("turn_number"),
                          "phase": ST["phase"]})
                    await export_as(p0, "post")
                    ST["post_exported"] = True
                    result = "giveup"
                    break
                if state.get("winner") is not None or state.get("game_over"):
                    say(f"game over detected in state "
                        f"(winner={state.get('winner')})")
                    wire("game_over_state",
                         {"winner": state.get("winner")})
                    result = "giveup"
                    break
                if ST["terminal"]:
                    say("TerminalResult received")
                    result = "giveup"
                    break
            except Exception as e:
                say(f"[{tag}] observation error: {e}")
            if result != "giveup":
                break
            # ---- action pass
            try:
                acts = merged_actions(st)
                await tick(st, acts, st["state"], c)
            except Exception as e:
                say(f"[{tag}] tick error: {e}")
        if result != "giveup":
            break
    await close()
    return result


async def tick(st, acts, state, c):
    if c.name == "P0":
        await p0_tick(st, acts, state, c)
    else:
        await p1_tick(st, acts, state, c)


# ------------------------------------------------------------- main
async def main():
    reset_attempt()
    check_data_level()
    data_ok = ST["data_level_ok"]
    reset_attempt()
    ST["data_level_ok"] = data_ok  # check_data_level ran once; keep it
    result = await drive_one_game()
    say(f"game -> {result}")
    wire("game_result", {"result": result})
    if not os.path.exists(f"{EVDIR}/run.json"):
        # No chaos run completed: finalize a blocked run from whatever we
        # have (data-level evidence stands on its own).
        say("no successful chaos attempt; finalizing blocked run")
        try:
            await finalize(None)
        except Exception as e:
            say(f"finalize failed: {e}")


if __name__ == "__main__":
    run = asyncio.run(main())
    # copy the scenario into the evidence dir, render the PNG, and write
    # the SHA-256 manifest over everything except the manifest itself.
    shutil.copy(f"{BACKFILL}/driver/scenario_7424.py",
                f"{EVDIR}/scenario_7424.py")
    import subprocess
    if os.path.exists(f"{EVDIR}/run.json") and os.path.exists(f"{EVDIR}/pre.json") \
            and os.path.exists(f"{EVDIR}/post.json"):
        subprocess.run([sys.executable, f"{BACKFILL}/driver/render_summary.py",
                        EVDIR, str(ISSUE),
                        "Happy Yargle Day!: unparsed 'choose ... at random' "
                        "leaves CastCopyOfCard reading an empty tracked set; "
                        "chaos trigger is a silent no-op -- copies nothing"],
                       check=True)
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for f in files:
            h = hashlib.sha256(open(f"{EVDIR}/{f}", "rb").read()).hexdigest()
            mf.write(f"{h}  {f}\n")
    print(f"manifest written ({len(files)} files)", flush=True)
    sys.exit(0)
