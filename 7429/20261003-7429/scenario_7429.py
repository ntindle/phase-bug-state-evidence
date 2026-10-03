#!/usr/bin/env python3
"""Issue #7429: Kharasha Foothills -- the per-opponent attacking token
copies are unparsed, so `CreateDelayedTrigger` reads an empty tracked set.

Reported (2026-08-15, lgray, source:internal-triage, status:confirmed,
area:parser, mechanic:triggers/tokens/copy/combat,
classifier:unsupported-aspect, priority:p3-card-specific;
related #6857 tracked-set census):
Kharasha Foothills (a Plane in the pinned dataset) reads:
> Whenever a creature you control attacks a player, for each other
> opponent, you may create a token that's a copy of that creature, tapped
> and attacking that opponent. Exile those tokens at the beginning of the
> next end step.
> Whenever chaos ensues, you may sacrifice any number of creatures. If you
> do, Kharasha Foothills deals that much damage to target creature.
The "for each other opponent, you may create a token ..." clause does not
parse: the parser emits an `Effect::Unimplemented` node as the chain head
(root). Its sub_ability is the anaphor -- "Exile those tokens at the
beginning of the next end step" -- a `CreateDelayedTrigger`
(AtNextPhase End, uses_tracked_set=false) whose inner effect is
`ChangeZone -> Exile` targeting the `TrackedSet(0)` sentinel. The
Unimplemented resolver is a no-op (logs a warning, pushes no GameEvent),
so the chain tracked set is allocated empty; the inner sentinel is
resolved when the delayed trigger fires, against whatever tracked set
exists then (the #6857 census records the same shape on Tears of Rage).
The triage comment did NOT measure the consumer-side runtime outcome for
this card -- the reported defect is the unparsed antecedent, plus the
structural consequence that the exile reads an empty set.

Pinned v0.100.0 parse (see data_evidence.json): triggers[0] mode
"Attacks", valid_card Typed Creature controller You, attack_target_filter
"Player", trigger_zones [Battlefield, Command]:
  execute.effect = Unimplemented { name: "unparsed_quantity",
      description: "for each other opponent, you may create a token that's
                    a copy of that creature, tapped and attacking that
                    opponent" }
    sub_ability (sub_link SequentialSibling):
      CreateDelayedTrigger { condition: AtNextPhase/End,
                             uses_tracked_set: false,
        effect: ChangeZone { destination: Exile,
                             target: TrackedSet { id: 0 } } }
(the issue's corpus 9b7c66e30 names the Unimplemented head "for"; the
pinned v0.100.0 corpus names it "unparsed_quantity" with the identical
description -- both are Effect::Unimplemented over the same clause.)

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, three human-client seats, Planechase, life 20):
  P0: 4x Grizzly Bears + 56x Forest; planar deck [Kharasha Foothills +
      19 benign planes] (planar deck enforces singleton; engine shuffles)
  P1: 60x Forest (plays lands, passes, never attacks)
  P2: 60x Forest (plays lands, passes, never attacks)
Drive:
  1. Mulligans: all seats keep 7.
  2. Seek: P0 rolls the planar die on every own main phase (RollPlanarDie
     legacy action; can_roll from derived.planechase) until Kharasha
     Foothills is the active plane. Filler planes are benign/choice-free;
     their chaos triggers resolve via ordinary priority passes.
  3. Build: P0 plays a land per turn and casts Grizzly Bears ({1}{G},
     tap-to-pool vi payment) once affordable.
  4. Attack: on the first P0 DeclareAttackers with an untapped Bears on
     the battlefield and Kharasha active: export pre.json INSIDE the
     submission path (guarded flag), then declare the Bears attacking P1.
     P1/P2 declare no blockers (P1 has none).
  5. Attack-trigger window: record every viewer-interaction opportunity
     offered to ANY seat from the attack declaration through the trigger
     leaving the stack; both seats pass priority to resolve. Track P0's
     battlefield token objects and the exile zone.
  6. End step: the CreateDelayedTrigger (AtNextPhase End) should fire;
     record any stack appearance and the exile delta. Export post.json
     once settled (stack empty, Priority, 8s idle).

Expected (correct behavior): the attack trigger offers the optional "you
may create a token copy for each other opponent, tapped and attacking";
accepting creates the copies; at the next end step the delayed trigger
exiles those tokens.
Reported (bug): the antecedent clause never parsed, so the trigger is a
silent no-op -- no tokens are ever created -- and the dependent
CreateDelayedTrigger exiles the empty tracked set (nothing).

Assertions (correct-behavior expectations; a FAIL records the bug):
  A1_data_level   pinned v0.100.0 card-data.json parses the Attacks
                  trigger as the issue reports: Typed-Creature/You,
                  attack_target_filter Player; head Unimplemented naming
                  the token-copy clause; sub CreateDelayedTrigger
                  (AtNextPhase End, uses_tracked_set false) with inner
                  ChangeZone->Exile target TrackedSet(0).
  A2_setup_ok     pre.json: Kharasha Foothills face-up in the command zone
                  as the active plane; an untapped Grizzly Bears on P0's
                  battlefield; P1/P2 alive.
  A3_attack_trigger_resolved the Kharasha attack trigger was observed on
                  the stack after the attack declaration and resolved
                  (left the stack).
  A4_no_tokens    (THE REPORTED BUG) the attack trigger's resolution
                  created no token copies: P0's battlefield token-object
                  count at window_end == at pre, and no new object named
                  "grizzly bears" appeared on P0's battlefield.
  A5_exile_noop    the end-step exile moved nothing: exile-zone object
                  counts unchanged pre -> post (the delayed trigger read
                  the empty tracked set); whether the delayed trigger was
                  visibly on the stack is recorded as an observation.
  A6_cleanup      post stack empty, game advanced past the attack turn.

Verdict rule: reproduced iff A1, A2, A3 passed and NOT (A4 passed and A5
              passed); not-reproduced iff A1..A5 all passed; blocked iff
              A1, A2, or A3 could not be established.

Protocol-101 driver notes (v0.100.0, build bc9ef56):
  - HELLO advertises 101 (server enforces exact match).
  - MulliganDecision as {"choice":{"type":"Keep"}} gated on
    waiting_for.data.pending[] with phase type "Declare" (7426 pattern).
  - Planechase game via engine-canonical FormatConfig::planechase().
  - Deck payload carries "planar_deck": [names]; the engine shuffles it;
    minimum 20 cards, singleton enforced (7426 pattern).
  - RollPlanarDie submitted as legacy action {"type":"RollPlanarDie"};
    legality read from st["derived"]["planechase"]["can_roll"].
  - state["planar_deck"] is the ordered oid list (index 0 = top);
    planar cards live in the Command zone, face-down except the active
    plane.
  - CastSpell via cast_action_for + tap-to-pool vi mana payment
    (pay_mana_vi/pay_tick, 7423 pattern); needs {G:1, generic:1}.
  - DeclareAttackers data: {"attacks": [[oid, {"type":"Player","data":1}]],
    "bands": []} (7423 pattern).
  - Authoritative exports only from the host seat (P0 creates the game).
  - wire() guards WIRE.closed (7426 post-run fix).
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
RUN_ID = "20261003-7429"
ISSUE = 7429
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
                     "(shared process started 2026-10-02, run 20261002-bf1; "
                     "ServerHello 0.100.0/bc9ef56/protocol 101 re-verified "
                     "by this run's own Hello handshake)",
    "mode": "Full",
    "source": "2026-10-03: latest stable release v0.100.0 == pinned release "
              "dir (GitHub /releases re-confirmed v0.100.0 still latest "
              "stable at 00:20 CDT 2026-10-03); hashes recomputed against "
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

KHARASHA = "kharasha foothills"
BEAR = "grizzly bears"
FOREST = "forest"

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
    "Kharasha Foothills",
    # 10 planes with no chaos trigger (chaos on them = nothing happens)
    "Bicycle Rack", "Elvish Impersonation Contest", "Ghirapur Grand Prix",
    "Jalira's Show", "Shrinking Plane", "Sky Deck", "Stroopwafel Cafe",
    "The Food Court", "The Pro Tour", "Windmill Farm",
    # 9 planes with benign, choice-free chaos abilities (7426 list)
    "Hedron Fields of Agadeem",  # create a 7/7 Eldrazi token
    "Jund",                       # create two 1/1 Goblin tokens
    "Llanowar",                   # untap all creatures you control
    "Prahv",                      # gain life = cards in hand
    "Tazeem",                     # draw a card for each land you control
    "Windriddle Palaces",         # each player mills a card
    "Agyrem",                     # creatures can't attack you
    "Esper",                      # your white/blue/black creatures become artifacts
    "The Eon Fog",                # untap all permanents you control
    # 10 more benign, choice-free planes (3-player minimum is 30)
    "Gavony",                     # all creatures have vigilance
    "Aretopolis",                 # scroll counters, life/draw, all automatic
    "Astral Arena",               # max 1 attacker/blocker per combat
    "Academy at Tolaria West",    # end step draw 7 if empty hand; chaos discards
    "Edge of Malacol",            # untap-step creatures get counters instead
    "Aplan Mortarium",            # upkeep life loss; chaos creates 2 Alien Angels
    "Sokenzan",                   # creatures +1/+1 and haste
    "Otaria",                     # graveyard flashback; chaos = extra turn
    "Megaflora Jungle",           # MV<=2 creatures get +2/+2
    "Panopticon",                 # extra draws, all automatic
]


def pdeck(main_names, planar_names):
    return {"main_deck": main_names, "sideboard": [], "commander": [],
            "planar_deck": planar_names}


P0_DECK = pdeck([BEAR.title()] * 4 + [FOREST.title()] * 56, PLANAR_DECK)
P1_DECK = pdeck([FOREST.title()] * 60, [])
P2_DECK = pdeck([FOREST.title()] * 60, [])

SETUP_DEADLINE_S = 2400
SEEK_WATCHDOG_TURNS = 200  # 30-card planar deck: expect ~15 planeswalks to
                           # reach Kharasha (~6 rolls each); 200 is safe


def reset_attempt():
    global ST, MULLS, SUBMITTED_OPPS, _DISCARD_REV
    ST = {
        "t0": time.time(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "game_code": None,
        "phase": "setup",  # setup -> seek -> build -> pre_attack -> attacking -> done
        "starting_plane_name": None,
        "rolls": 0,
        "roll_pending": False,
        "roll_at": 0,
        "roll_pre_plane": None,
        "roll_rejections": 0,
        "bears_cast": False,
        "mana_needs": {"G": 0, "generic": 0},
        "attack_submitted": False,
        "attack_turn": None,
        "attacker_oid": None,
        "pre_exported": False,
        "post_exported": False,
        "window_end_written": False,
        "trigger_seen": False,
        "trigger_resolved": False,
        "trigger_turn": None,
        "endstep_trigger_seen": False,
        "endstep_exile_delta": None,
        "tokens_at_pre": None,
        "tokens_at_window_end": None,
        "exile_at_pre": None,
        "exile_at_post": None,
        "prompts_seen": [],
        "wf_types_window": [],
        "terminal": False,
        "terminal_data": None,
        "states_seen": 0,
        "cast_rejections": 0,
        "settle_at": None,
        "token_marker_logged": False,
        "ass": {k: "not-run" for k in ("A1_data_level", "A2_setup_ok",
                                       "A3_attack_trigger_resolved",
                                       "A4_no_tokens", "A5_exile_noop",
                                       "A6_cleanup")},
        "notes": [],
        "data_level_ok": False,
        "discard_iids": set(),
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


def bf_permanents(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def is_creature(o):
    return "creature" in [str(t).lower()
                          for t in (o.get("card_types") or {}).get("core_types", [])]


def is_land(o):
    return "land" in [str(t).lower()
                      for t in (o.get("card_types") or {}).get("core_types", [])]


def untapped_lands(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and not o.get("tapped") and is_land(o)]


def bears_bf_untapped(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if obj_lname(state, oid) == BEAR
            and o.get("zone") == "Battlefield" and o.get("controller") == pid
            and not o.get("tapped")]


def bf_token_oids(state, pid):
    """Battlefield objects controlled by pid that look like tokens."""
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") != "Battlefield" or o.get("controller") != pid:
            continue
        blob = json.dumps(o, default=str).lower()
        if any(k in blob for k in ('"is_token": true', '"istoken": true',
                                   '"token": true', "token copy",
                                   '"is_token":true')):
            out.append(int(oid))
    return out


def exile_count(state):
    return sum(1 for o in (state.get("objects") or {}).values()
               if o.get("zone") == "Exile")


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


def kharasha_active(st):
    return active_plane_name(st) == KHARASHA


def kharasha_on_stack(state):
    for se in stack_entries(state):
        if "kharasha" in json.dumps(se, default=str).lower():
            return se
    return None


def kharasha_face_up(state):
    """(oid or None, face_up bool) for Kharasha Foothills in command zone."""
    for oid, o in (state.get("objects") or {}).items():
        if str(o.get("base_name") or o.get("name") or "").lower() == KHARASHA:
            if o.get("zone") == "Command":
                return int(oid), not o.get("face_down")
    return None, False

# ------------------------------------------------------------- data check
def check_data_level():
    card = CARD_DATA.get(KHARASHA, {})
    trigs = card.get("triggers") or []
    atk = [t for t in trigs if t.get("mode") == "Attacks"]
    ev = {
        "name": card.get("name"),
        "oracle": card.get("oracle_text"),
        "card_type": card.get("card_type"),
        "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
        "trigger_mode": None,
        "valid_card": None,
        "attack_target_filter": None,
        "trigger_zones": None,
        "trigger_optional": None,
        "head_effect": None,
        "sub_ability": None,
        "report_corpus_note": ("the issue's corpus (9b7c66e30) names the "
                               "Unimplemented head 'for'; the pinned "
                               "v0.100.0 corpus names it "
                               "'unparsed_quantity' with the identical "
                               "description -- both are Effect::Unimplemented "
                               "over the same clause; the structural claim "
                               "is unchanged."),
    }
    ok = False
    if atk:
        t = atk[0]
        ev["trigger_mode"] = t.get("mode")
        ev["valid_card"] = t.get("valid_card")
        ev["attack_target_filter"] = t.get("attack_target_filter")
        ev["trigger_zones"] = t.get("trigger_zones")
        ev["trigger_optional"] = t.get("optional")
        ex = (t.get("execute") or {})
        ev["head_effect"] = (ex.get("effect") or {})
        ev["sub_ability"] = (ex.get("sub_ability") or {})
        sub = ev["sub_ability"]
        sub_eff = (sub.get("effect") or {})
        # CreateDelayedTrigger nests the delayed ability one more level:
        # effect = {"kind": "Spell", "effect": {"type": "ChangeZone", ...}}
        inner = ((sub_eff.get("effect") or {}).get("effect")) or {}
        head = ev["head_effect"]
        ok = (t.get("mode") == "Attacks"
              and (t.get("valid_card") or {}).get("type") == "Typed"
              and "Creature" in (t.get("valid_card") or {}).get("type_filters", [])
              and (t.get("valid_card") or {}).get("controller") == "You"
              and t.get("attack_target_filter") == "Player"
              and head.get("type") == "Unimplemented"
              and "for each other opponent" in str(head.get("description", "")).lower()
              and "create a token that's a copy of that creature"
              in str(head.get("description", "")).lower()
              and sub_eff.get("type") == "CreateDelayedTrigger"
              and (sub_eff.get("condition") or {}).get("type") == "AtNextPhase"
              and (sub_eff.get("condition") or {}).get("phase") == "End"
              and sub_eff.get("uses_tracked_set") is False
              and inner.get("type") == "ChangeZone"
              and inner.get("destination") == "Exile"
              and (inner.get("target") or {}).get("type") == "TrackedSet"
              and (inner.get("target") or {}).get("id") == 0)
        say(f"data-level check: mode={t.get('mode')}, "
            f"valid_card={json.dumps(ev['valid_card'], default=str)[:120]}, "
            f"target_filter={t.get('attack_target_filter')}, "
            f"head={head.get('type')}/{head.get('name')}, "
            f"sub={sub_eff.get('type')}/{sub_eff.get('uses_tracked_set')}, "
            f"inner={inner.get('type')}/{inner.get('destination')}/"
            f"{(inner.get('target') or {}).get('type')}")
    else:
        say("data-level check: NO Attacks trigger found in pinned card data")
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
    say(f"[{tag}] discarding to hand size")
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
        ST["discard_iids"].add(iid)
        say(f"[{tag}] vi discard-to-hand-size: {pick}")
        wire("vi_discard", {"who": tag, "iid": iid, "pick": pick})
        await c.send_interaction({
            "interactionId": iid,
            "response": {"type": "select",
                         "data": {"choiceIds": [pick]}}})
        return True
    return False


async def pay_mana_vi(c, st, state, tag):
    """Answer vi mana-payment choices during casting (7423 pattern):
    one tapLandForMana interaction per land; never touches cancelCast."""
    vi = get_vi(st)
    if not vi:
        return False
    needs = ST["mana_needs"]
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
        await c.send_interaction(sub)
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


# ------------------------------------------------------------- P0 tick
async def p0_tick(st, acts, state, c):
    pid = c.player_id
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan(c, pid, "P0"):
            return
        return
    if await do_discard_to_handsize(c, pid, "P0"):
        return
    if await do_discard_vi(c, pid, "P0"):
        return
    if sum(ST["mana_needs"].values()) > 0:
        if await pay_mana_vi(c, st, state, "P0"):
            return
        if await pay_tick(c, acts, "P0"):
            return
    if wtype == "DeclareAttackers":
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if ST["phase"] == "pre_attack" and kharasha_active(st):
            bb = bears_bf_untapped(state, pid)
            # THE decisive pre: exported INSIDE the submission path
            # (guarded flag), before the attack is declared.
            if bb and not ST["attack_submitted"] and da:
                if not ST["pre_exported"]:
                    await export_as(c, "pre")
                    ST["pre_exported"] = True
                    ST["attacker_oid"] = bb[0]
                    ST["tokens_at_pre"] = sorted(bf_token_oids(state, pid))
                    ST["exile_at_pre"] = exile_count(state)
                    ST["life_at_pre"] = {
                        str(p.get("id")): p.get("life")
                        for p in state.get("players", [])}
                    say(f"[P0] pre.json exported; attacker oid={bb[0]}; "
                        f"tokens_at_pre={ST['tokens_at_pre']}; "
                        f"exile_at_pre={ST['exile_at_pre']}; "
                        f"life={ST['life_at_pre']}")
                    wire("pre_exported",
                         {"attacker": bb[0],
                          "tokens": ST["tokens_at_pre"],
                          "exile": ST["exile_at_pre"]})
                d = copy.deepcopy(da)
                d.setdefault("data", {}).update(
                    {"attacks": [[bb[0], {"type": "Player", "data": 1}]],
                     "bands": []})
                ST["attack_submitted"] = True
                ST["attack_turn"] = state.get("turn_number")
                ST["phase"] = "attacking"
                ST["settle_at"] = None
                wire("attack_declared",
                     {"attacker_oid": bb[0], "defender": 1,
                      "data": d["data"]})
                say(f"[P0] attacking P1 with Grizzly Bears (oid {bb[0]}); "
                    f"phase -> attacking")
                await submit_as_is(c, d)
                return
        # not ready (or not our attack turn): declare no attackers.
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
    if ST["phase"] == "seek" and phase in ("PreCombatMain", "PostCombatMain") \
            and active == pid and not stack_entries(state):
        pc = planechase_view(st)
        if pc.get("can_roll") and not ST["roll_pending"]:
            say(f"[P0] rolling planar die (roll #{ST['rolls'] + 1}, "
                f"cost={pc.get('current_roll_cost')})")
            await c.send_action({"type": "RollPlanarDie"})
            ST["roll_pending"] = True
            ST["roll_at"] = time.time()
            ST["roll_pre_plane"] = pc.get("active_plane")
            ST["rolls"] += 1
            wire("roll_submitted", {"n": ST["rolls"],
                                    "cost": pc.get("current_roll_cost")})
            return
    if phase in ("PreCombatMain", "PostCombatMain") and active == pid:
        hn = hand_lnames(state, pid)
        # cast Grizzly Bears {1}{G} once Kharasha is active.
        if (ST["phase"] in ("build", "pre_attack") and not ST["bears_cast"]
                and BEAR in hn and len(untapped_lands(state, pid)) >= 2):
            a, oid = cast_action_for(acts, state, BEAR)
            if a:
                ST["bears_cast"] = True
                ST["mana_needs"] = {"G": 1, "generic": 1}
                say(f"[P0] casting Grizzly Bears (oid {oid})")
                wire("bears_cast", {"oid": oid})
                await submit_as_is(c, a)
                return
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(c, a)
                return
    # ready to attack: move to pre_attack once the bears is on board.
    if ST["phase"] == "build" and bears_bf_untapped(state, pid):
        ST["phase"] = "pre_attack"
        say("[P0] bears on battlefield untapped; phase -> pre_attack")
        wire("phase", {"to": "pre_attack"})
    await pass_priority(c, st, acts)


# ------------------------------------------------------------- P1/P2 tick (never attack)
async def px_tick(st, acts, state, c, tag):
    pid = c.player_id
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan(c, pid, tag):
            return
        return
    if await do_discard_to_handsize(c, pid, tag):
        return
    if await do_discard_vi(c, pid, tag):
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
    if phase in ("PreCombatMain", "PostCombatMain") and active == pid:
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
    we = load_env("window_end") or {}
    we_st = we.get("state") or {}

    # A1: data-level parse matches the issue
    if ST.get("data_level_ok"):
        ass["A1_data_level"] = "passed"
        notes.append("A1 passed: pinned v0.100.0 card-data.json parses "
                     "Kharasha Foothills' Attacks trigger as the issue "
                     "reports -- Typed Creature controller You, "
                     "attack_target_filter Player, head "
                     "Unimplemented('unparsed_quantity': 'for each other "
                     "opponent, you may create a token that's a copy of "
                     "that creature, tapped and attacking that opponent') "
                     "+ sub CreateDelayedTrigger (AtNextPhase End, "
                     "uses_tracked_set=false) with inner ChangeZone->Exile "
                     "target TrackedSet(0); see data_evidence.json.")
    else:
        ass["A1_data_level"] = "failed"
        notes.append("A1 FAILED: the pinned parse does not match the "
                     "issue-reported shape; see data_evidence.json.")

    # A2: setup at the decisive pre
    if ST["pre_exported"] and pre_st:
        oid, up = kharasha_face_up(pre_st)
        bb = bears_bf_untapped(pre_st, 0)
        alive = [p.get("id") for p in pre_st.get("players", [])
                 if p.get("id") in (1, 2)]
        ap = None
        for o in (pre_st.get("objects") or {}).values():
            if str(o.get("base_name") or "").lower() == KHARASHA \
                    and o.get("zone") == "Command" and not o.get("face_down"):
                ap = True
        notes.append(f"A2 probe: Kharasha face-up in command zone={up} "
                     f"(oid={oid}); untapped P0 bears={bb}; P1/P2 "
                     f"present={alive}; life={ST.get('life_at_pre')}.")
        if up and bb:
            ass["A2_setup_ok"] = "passed"
            notes.append("A2 passed: Kharasha Foothills was the active "
                         "plane (face-up in the command zone) and P0 had "
                         "an untapped Grizzly Bears when the attack was "
                         "declared.")
        else:
            ass["A2_setup_ok"] = "failed"
            notes.append("A2 FAILED: the decisive pre does not show the "
                         "required setup.")
    else:
        ass["A2_setup_ok"] = "failed"
        notes.append("A2 FAILED: pre.json was never exported (the attack "
                     "was never declared).")

    # A3: the attack trigger fired and resolved
    if ST["trigger_seen"] and ST["trigger_resolved"]:
        ass["A3_attack_trigger_resolved"] = "passed"
        notes.append("A3 passed: the Kharasha Foothills attack trigger was "
                     "observed on the stack after P0's attack declaration "
                     f"(turn {ST.get('trigger_turn')}) and resolved (left "
                     "the stack).")
    elif ST["trigger_seen"]:
        ass["A3_attack_trigger_resolved"] = "failed"
        notes.append("A3 FAILED: the attack trigger was seen on the stack "
                     "but its resolution was never confirmed.")
    else:
        notes.append("A3 not-run: the attack trigger was never observed on "
                     "the stack.")

    # A4: did the trigger's resolution create token copies?
    toks_pre = ST.get("tokens_at_pre") or []
    toks_we = ST.get("tokens_at_window_end") or []
    new_bears = []
    if we_st:
        pre_oids = set()
        if pre_st:
            pre_oids = {int(oid) for oid, o in
                        (pre_st.get("objects") or {}).items()
                        if o.get("zone") == "Battlefield"
                        and o.get("controller") == 0}
        for oid, o in (we_st.get("objects") or {}).items():
            if (o.get("zone") == "Battlefield" and o.get("controller") == 0
                    and int(oid) not in pre_oids
                    and obj_lname(we_st, oid) == BEAR):
                new_bears.append(int(oid))
    notes.append(f"A4 probe: P0 token oids at pre={toks_pre}, at "
                 f"window_end={toks_we}; new '{BEAR}' objects on P0 "
                 f"battlefield at window_end={new_bears or 'none'}.")
    if ST["trigger_seen"] and ST["trigger_resolved"]:
        if not toks_we and not new_bears and toks_pre == toks_we:
            ass["A4_no_tokens"] = "failed"
            notes.append("A4 FAILED: the attack trigger resolved but "
                         "created NO token copies of the attacking creature "
                         "-- THE REPORTED BUG (the 'for each other "
                         "opponent, you may create a token ...' clause "
                         "never parsed; the Unimplemented head is a "
                         "runtime no-op).")
        else:
            ass["A4_no_tokens"] = "passed"
            notes.append("A4 passed: token copies were created on "
                         "resolution.")
    else:
        notes.append("A4 not-run: the attack window never completed.")

    # A5: the end-step exile moved nothing
    ex_pre = ST.get("exile_at_pre")
    ex_post = ST.get("exile_at_post")
    notes.append(f"A5 probe: exile objects pre={ex_pre} post={ex_post}; "
                 f"delayed-trigger visibly on stack at end step="
                 f"{ST.get('endstep_trigger_seen')}.")
    if ex_pre is not None and ex_post is not None:
        if ex_pre == ex_post:
            ass["A5_exile_noop"] = "passed"
            notes.append("A5 passed: the end-step exile moved no objects "
                         "(exile zone unchanged pre -> post) -- the "
                         "CreateDelayedTrigger read the empty tracked set, "
                         "as the issue's structural analysis predicts. "
                         "Delayed trigger visibly on stack at end step: "
                         f"{ST.get('endstep_trigger_seen')}.")
        else:
            ass["A5_exile_noop"] = "failed"
            notes.append("A5 FAILED: the exile zone changed pre -> post "
                         f"({ex_pre} -> {ex_post}); investigate which "
                         "objects moved -- the deferred-sentinel "
                         "resolution may have exiled something unexpected.")
    else:
        notes.append("A5 not-run: exile counts unavailable.")

    # A6: cleanup
    if post_st:
        atk_turn = ST.get("attack_turn")
        cur_turn = post_st.get("turn_number")
        if not stack_entries(post_st) and (
                atk_turn is None or (cur_turn or 0) > atk_turn):
            ass["A6_cleanup"] = "passed"
            notes.append("A6 passed: post stack empty, game advanced past "
                         f"the attack turn (attack turn {atk_turn}, post "
                         f"turn {cur_turn}, phase {post_st.get('phase')}).")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append("A6 FAILED: post stack non-empty or the game did "
                         "not advance past the attack turn: "
                         f"stack={stack_entries(post_st)} "
                         f"turn={cur_turn} (attack turn {atk_turn})")
    else:
        notes.append("A6 not-run: no post state")

    # verdict
    if (ass["A1_data_level"] == "passed"
            and ass["A2_setup_ok"] == "passed"
            and ass["A3_attack_trigger_resolved"] == "passed"
            and not (ass["A4_no_tokens"] == "passed"
                     and ass["A5_exile_noop"] == "passed")):
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: Kharasha Foothills was the active plane; "
            "P0's Grizzly Bears attacked P1; the attack trigger fired and "
            "resolved, but created NO token copies (the antecedent clause "
            "never parsed -- Unimplemented head is a runtime no-op), and "
            "the end-step CreateDelayedTrigger exiled nothing (empty "
            "tracked set). Confirmed on v0.100.0. The parse is the "
            "reported defect; the silent no-op + empty exile are its "
            "direct structural consequences. This is not a fix claim.")
    elif all(ass[k] == "passed" for k in ("A1_data_level", "A2_setup_ok",
                                          "A3_attack_trigger_resolved",
                                          "A4_no_tokens",
                                          "A5_exile_noop")):
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: the attack trigger created the token "
            "copies and the end step exiled them. This is not a fix claim.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: the reported attack path could not "
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
               if "kharasha" in l.lower()
               or "delayed" in l.lower()
               or "unimplemented" in l.lower()
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
            open(f"{BACKFILL}/driver/scenario_7429.py", "rb").read()).hexdigest(),
        "format_config": "Planechase (engine-canonical FormatConfig::planechase())",
        "decks": {"P0": [("Grizzly Bears", 4), ("Forest", 56)],
                  "P1": [("Forest", 60)], "P2": [("Forest", 60)],
                  "planar_deck": PLANAR_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": {
            "rolls": ST["rolls"],
            "roll_rejections": ST["roll_rejections"],
            "starting_plane_name": ST["starting_plane_name"],
            "bears_cast": ST["bears_cast"],
            "attack_submitted": ST["attack_submitted"],
            "attack_turn": ST["attack_turn"],
            "attacker_oid": ST["attacker_oid"],
            "trigger_seen": ST["trigger_seen"],
            "trigger_resolved": ST["trigger_resolved"],
            "endstep_trigger_seen": ST["endstep_trigger_seen"],
            "tokens_at_pre": ST["tokens_at_pre"],
            "tokens_at_window_end": ST["tokens_at_window_end"],
            "exile_at_pre": ST["exile_at_pre"],
            "exile_at_post": ST["exile_at_post"],
            "prompts_seen": [(p[0], p[1], p[2]) for p in ST["prompts_seen"]],
            "wf_types_window": sorted(set(ST["wf_types_window"])),
            "cast_rejections": ST["cast_rejections"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via three human-client seats.",
            "The report is a data-level parse report; the scenario replays "
            "the reported line (Kharasha Foothills active, a creature you "
            "control attacks a player) and observes the resolution "
            "outcome. The planar deck is engine-shuffled; P0 planeswalked "
            "via the planar die until Kharasha Foothills was the active "
            "plane (filler planes chosen for benign, choice-free chaos "
            "abilities; planar deck singleton enforced by the engine).",
            "4x Grizzly Bears deck density is a test-harness convenience "
            "(engine accepts >4-of for custom games).",
            "The 'Whenever chaos ensues' ability was never exercised (no "
            "chaos roll occurred while Kharasha Foothills was active).",
            "States are authoritative exports, restorable only via full "
            "game replay.",
        ],
        "setup_line": "P0: 4x Grizzly Bears + 56x Forest + planar deck "
                      "[Kharasha Foothills + 19 benign planes]; P1/P2: 60x "
                      "Forest (lands, passes, never attack); Planechase, "
                      "life 20",
        "contract_line": "Kharasha Foothills active; P0's Grizzly Bears "
                         "attacks P1: for each other opponent you may "
                         "create a tapped-and-attacking token copy; exile "
                         "those tokens at the next end step. Observed "
                         "(bug): the clause never parsed -- no tokens "
                         "created, end-step exile moves nothing.",
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
    """Seek Kharasha Foothills, cast a Bears, attack P1, capture the
    trigger window and the end-step exile. Returns 'done' or 'giveup'."""
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    p2 = PhaseClient("P2")
    await p0.connect()
    await p0.create(P0_DECK, player_count=3, format_config=PLANECHASE_FORMAT)
    await p1.connect()
    await p1.join(p0.game_code, P1_DECK)
    await p2.connect()
    await p2.join(p0.game_code, P2_DECK)
    await asyncio.sleep(2.0)
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} "
        f"P1 seat={p1.player_id} P2 seat={p2.player_id}")
    wire("game_setup", {"game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id,
                        "p2_seat": p2.player_id})

    t_start = time.time()
    last = {}
    last_tick_at = {}
    result = "giveup"

    async def close():
        for c in (p0, p1, p2):
            try:
                await c.close()
            except Exception:
                pass

    clients = ((p0, p0_tick, "P0"), (p1, None, "P1"), (p2, None, "P2"))
    while time.time() - t_start < SETUP_DEADLINE_S and result == "giveup":
        await asyncio.sleep(0.15)
        now = time.time()
        for c, tickfn, tag in clients:
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
                            rej_str = json.dumps(data)
                            for iid in list(SUBMITTED_OPPS):
                                if iid in rej_str:
                                    SUBMITTED_OPPS.discard(iid)
                                    ST["discard_iids"].discard(iid)
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

                # setup -> seek: starting plane check
                if ST["phase"] == "setup":
                    pname = active_plane_name(st)
                    if pname and (wf_of(state).get("type") not in
                                  ("MulliganDecision", "BottomCards")):
                        ST["starting_plane_name"] = pname
                        if pname == KHARASHA:
                            ST["phase"] = "build"
                            say(f"starting plane is Kharasha Foothills; "
                                f"phase -> build")
                        else:
                            ST["phase"] = "seek"
                            say(f"starting plane is {pname}; phase -> seek")
                        wire("seek_start", {"plane": pname,
                                            "phase": ST["phase"]})

                # seek: roll-outcome classification
                if ST["phase"] == "seek" and ST["roll_pending"]:
                    if kharasha_on_stack(state):
                        pass  # phenomena only; Kharasha is a plane
                    if kharasha_active(st):
                        ST["roll_pending"] = False
                        ST["phase"] = "build"
                        say("Kharasha Foothills is now the active plane; "
                            "phase -> build")
                        wire("kharasha_active",
                             {"rolls": ST["rolls"]})
                    elif (active_plane_oid(st) != ST["roll_pre_plane"]
                          and ST["roll_pre_plane"] is not None):
                        ST["roll_pending"] = False
                        pname = active_plane_name(st)
                        say(f"planeswalked to {pname}; keep seeking")
                        wire("planeswalked", {"plane": pname})
                    elif now - ST["roll_at"] > 5:
                        ST["roll_pending"] = False
                        say(f"blank roll (roll #{ST['rolls']})")
                        wire("blank_roll", {"n": ST["rolls"]})

                # attack-trigger window observation (any seat)
                if ST["phase"] == "attacking":
                    vi = get_vi(st)
                    if vi:
                        for opp in vi.get("opportunities", []) or []:
                            record_window_prompt(tag, opp)
                    wtype = (wf_of(state).get("type") or "")
                    if wtype and wtype not in ST["wf_types_window"]:
                        ST["wf_types_window"].append(wtype)
                    se = kharasha_on_stack(state)
                    if se and not ST["trigger_seen"]:
                        ST["trigger_seen"] = True
                        ST["trigger_turn"] = state.get("turn_number")
                        say("TRIGGER: Kharasha Foothills attack trigger on "
                            "the stack")
                        wire("trigger_on_stack", {"entry": se,
                                                  "turn": ST["trigger_turn"]})
                    if (ST["trigger_seen"] and not ST["trigger_resolved"]
                            and not kharasha_on_stack(state)):
                        ST["trigger_resolved"] = True
                        ST["tokens_at_window_end"] = sorted(
                            bf_token_oids(state, 0))
                        # Write the OBSERVED state (7426 pattern: the
                        # export round-trip races the auto-passing loop).
                        with open(f"{EVDIR}/window_end.json", "w") as wfj:
                            json.dump({"state": state,
                                       "observed_turn":
                                           state.get("turn_number"),
                                       "observed_phase":
                                           state.get("phase"),
                                       "trigger_turn": ST.get("trigger_turn"),
                                       "tokens_at_window_end":
                                           ST["tokens_at_window_end"],
                                       "exile": exile_count(state)},
                                      wfj, default=str)
                        say("window_end.json written from observed state "
                            f"(turn {state.get('turn_number')}, "
                            f"phase {state.get('phase')}, "
                            f"tokens={ST['tokens_at_window_end']})")
                        wire("trigger_window_complete",
                             {"tokens": ST["tokens_at_window_end"],
                              "exile": exile_count(state)})
                        ST["settle_at"] = now
                    # end-step delayed-trigger watch
                    ph = str(state.get("phase") or "").lower()
                    if (ST["trigger_resolved"]
                            and state.get("active_player") == 0
                            and ST.get("attack_turn") is not None
                            and state.get("turn_number") == ST["attack_turn"]
                            and "end" in ph):
                        if kharasha_on_stack(state):
                            ST["endstep_trigger_seen"] = True
                            say("ENDSTEP: delayed trigger on the stack")
                            wire("endstep_trigger",
                                 {"entry": kharasha_on_stack(state)})
                # decisive post: settled past the attack turn
                if ST["trigger_resolved"] and not ST["post_exported"]:
                    if not stack_entries(state):
                        if ST["settle_at"] is None:
                            ST["settle_at"] = now
                        idle = now - ST["settle_at"]
                        wtype = (wf_of(state).get("type") or "")
                        atk_turn = ST.get("attack_turn") or 0
                        if (wtype == "Priority" and idle > 8
                                and (state.get("turn_number") or 0)
                                > atk_turn):
                            ST["exile_at_post"] = exile_count(state)
                            say(f"settled: stack empty, Priority, "
                                f"{idle:.0f}s idle, turn advanced past "
                                f"attack turn; exporting post")
                            await export_as(p0, "post")
                            ST["post_exported"] = True
                            await finalize(p0)
                            result = "done"
                            break
                    else:
                        ST["settle_at"] = None
                # watchdogs
                if (ST["phase"] == "seek"
                        and (state.get("turn_number") or 0)
                        > SEEK_WATCHDOG_TURNS
                        and not ST["post_exported"]):
                    say(f"seek watchdog: {SEEK_WATCHDOG_TURNS} turns in, "
                        f"Kharasha was never the active plane -- exporting "
                        f"state and giving up")
                    wire("seek_stall",
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
                if tickfn is not None:
                    await tickfn(st, acts, st["state"], c)
                else:
                    await px_tick(st, acts, st["state"], c, tag)
            except Exception as e:
                say(f"[{tag}] tick error: {e}")
        if result != "giveup":
            break
    await close()
    return result


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
        # No attack run completed: finalize a blocked run from whatever
        # we have (data-level evidence stands on its own).
        say("no successful attack attempt; finalizing blocked run")
        try:
            await finalize(None)
        except Exception as e:
            say(f"finalize failed: {e}")


if __name__ == "__main__":
    run = asyncio.run(main())
    # copy the scenario into the evidence dir, render the PNG, and write
    # the SHA-256 manifest over everything except the manifest itself.
    shutil.copy(f"{BACKFILL}/driver/scenario_7429.py",
                f"{EVDIR}/scenario_7429.py")
    import subprocess
    if os.path.exists(f"{EVDIR}/run.json") and os.path.exists(f"{EVDIR}/pre.json") \
            and os.path.exists(f"{EVDIR}/post.json"):
        subprocess.run([sys.executable, f"{BACKFILL}/driver/render_summary.py",
                        EVDIR, str(ISSUE),
                        "Kharasha Foothills: unparsed 'for each other "
                        "opponent, you may create a token copy' -- attack "
                        "trigger is a silent no-op; end-step exile reads "
                        "an empty tracked set"],
                       check=True)
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for f in files:
            h = hashlib.sha256(open(f"{EVDIR}/{f}", "rb").read()).hexdigest()
            mf.write(f"{h}  {f}\n")
    print(f"manifest written ({len(files)} files)", flush=True)
    sys.exit(0)
