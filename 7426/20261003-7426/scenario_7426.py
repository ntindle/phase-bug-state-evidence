#!/usr/bin/env python3
"""Issue #7426: Interplanar Tunnel -- "reveal cards from the top of your
planar deck until you reveal five plane cards" is unparsed, so the
dependent ChangeZoneAll reads an empty tracked set.

Reported (2026-08-15, lgray, source:internal-triage, status:confirmed,
area:parser, mechanic:zone-change, classifier:unsupported-aspect,
priority:p3-card-specific; related #6857 tracked-set census):
On Interplanar Tunnel, the clause "reveal cards from the top of your planar
deck until you reveal five plane cards" does not parse: the parser emits an
`Effect::Unimplemented` node in its place. That resolver pushes no
`GameEvent`, so the chain tracked set is allocated empty and the dependent
`ChangeZoneAll` ("put the rest of the revealed cards on the bottom in a
random order") reads an empty set. The census did NOT measure
`ChangeZoneAll`'s consumption style, and the issue does not assert a
runtime symptom -- the reported defect is the parse state and the empty
publish that structurally follows from it. The triage comment asks for a
runtime test driving the engine through resolution.

Oracle text (verified against the pinned v0.100.0 card-data.json):
> When you encounter Interplanar Tunnel, reveal cards from the top of your
> planar deck until you reveal five plane cards. Put a plane card from among
> them on top of your planar deck, then put the rest of the revealed cards
> on the bottom in a random order. (Then planeswalk away from this
> phenomenon.)

Pinned v0.100.0 parse (see data_evidence.json):
  trigger mode {"Planeswalked": {"role": "To"}} ("When you encounter"),
  zones [Battlefield, Command]:
    execute.effect = Unimplemented { name: "unparsed_verb_arguments",
        description: "reveal cards from the top of your planar deck until
                      you reveal five plane cards" }
      sub_ability: ChangeZoneAll { destination: "Library",
                                   target: TrackedSetFiltered { id: 0,
                                     filter: Any } }
        sub_sub_ability: Unimplemented { name: "unparsed_verb_arguments",
            description: "put the rest of the revealed cards on the bottom
                          in a random order" }
    (the encounter-trigger head is the chain root; the sub reads the chain
    tracked set. The issue's corpus (9b7c66e30) names the head "reveal";
    the pinned v0.100.0 corpus names it "unparsed_verb_arguments" with the
    identical description -- both are Effect::Unimplemented over the same
    clause; the structural claim is unchanged. Note the pinned parse also
    shows the "put the rest ..." clause as a second Unimplemented node,
    matching the triage comment's two-unsupported-nodes finding.)

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, Planechase format, life 20):
  P0: 60x Island, planar deck [Interplanar Tunnel + 19 benign planes]
      (planar deck enforces singleton -- verified by probe; the engine
      shuffles it, so the Tunnel's position is random.)
  P1: 60x Forest (plays a land, passes, never attacks, never rolls)
Drive:
  1. Mulligans: both seats keep 7 (all lands).
  2. Seeking: P0 rolls the planar die on every main phase (RollPlanarDie
     legacy action; first roll each turn free, then escalating generic)
     until Interplanar Tunnel is encountered (planeswalk reveals it).
     Filler planes are benign/choice-free; their chaos triggers resolve
     via ordinary priority passes.
  3. When the encounter trigger ("When you encounter Interplanar Tunnel")
     is first seen on the stack: export pre.json IMMEDIATELY, in the
     observation path (guarded flag), before either seat passes priority.
  4. Encounter window: record every viewer-interaction opportunity offered
     to EITHER seat from the trigger appearing through its resolution;
     both seats pass priority to resolve. Track revealed_cards /
     public_revealed_cards and the planar_deck order each tick.
  5. When the trigger leaves the stack: write window_end.json from the
     OBSERVED state (no export round-trip race). Then let the engine's
     phenomenon-departure planeswalk settle; export post.json once the
     stack is empty, Priority, 8s idle.

Expected (correct behavior): the encounter reveals cards from the top of
the planar deck until five plane cards are revealed, offers "put a plane
card from among them on top of your planar deck" as a choice, then puts
the rest on the bottom in a random order.
Reported (bug): the head clause never parsed, so the encounter ability is
a silent no-op -- no reveal happens, no on-top choice is offered, and the
dependent ChangeZoneAll moves nothing (empty tracked set). The only
planar-deck motion is the engine's own phenomenon-departure procedure.

Assertions (correct-behavior expectations; a FAIL records the bug):
  A1_data_level   pinned v0.100.0 card-data.json parses the encounter
                  trigger as the issue reports: Planeswalked-To trigger
                  with head Unimplemented naming the "reveal cards from
                  the top of your planar deck until you reveal five plane
                  cards" clause; sub ChangeZoneAll with target
                  TrackedSetFiltered; sub-sub Unimplemented naming "put
                  the rest of the revealed cards on the bottom in a
                  random order".
  A2_setup_ok     pre.json: Interplanar Tunnel face-up in the command
                  zone (encountered), planar_controller set.
  A3_encounter_trigger_resolved the "When you encounter Interplanar
                  Tunnel" trigger was observed on the stack and resolved
                  (left the stack).
  A4_reveal_choice_offered (THE REPORTED BUG) during the encounter
                  window, a choice-shaped prompt to put a plane card on
                  top of the planar deck (or any reveal-related choice)
                  was offered to a seat. Expected: offered. Observed
                  bug: never offered.
  A5_no_ability_effect (THE REPORTED BUG) the encounter ability's
                  resolution moved no planar-deck cards and revealed
                  nothing: planar_deck (ordered oids) at pre vs at
                  window_end (trigger resolved, departure not yet run)
                  identical, and revealed_cards/public_revealed_cards
                  stayed empty through the resolution. Expected: the
                  reveal-five + put-on-top + rest-to-bottom. Observed
                  bug: zero ability-driven effect.
  A6_cleanup      post stack empty, game advancing.

Verdict rule: reproduced iff A1, A2, A3 passed and NOT (A4 passed and A5
              passed); not-reproduced iff A1..A5 all passed; blocked iff
              A1, A2, or A3 could not be established.

Protocol-101 driver notes (v0.100.0, build bc9ef56):
  - HELLO advertises 101 (server enforces exact match).
  - MulliganDecision as {"choice":{"type":"Keep"}} gated on
    waiting_for.data.pending[] with phase type "Declare".
  - Planechase game via engine-canonical FormatConfig::planechase().
  - Deck payload carries "planar_deck": [names]; the engine shuffles it;
    minimum 20 cards, singleton enforced.
  - RollPlanarDie submitted as legacy action {"type":"RollPlanarDie"};
    legality read from st["derived"]["planechase"]["can_roll"].
  - state["planar_deck"] is the ordered oid list (index 0 = top);
    planar cards live in the Command zone, face-down except the active
    plane (and an encountered phenomenon).
  - Authoritative exports only from the host seat (P0 creates the game).
  - Adapted from scenario_7424.py (issue #7424, same Unimplemented-head
    pattern) 2026-10-03.
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
RUN_ID = "20261003-7426"
ISSUE = 7426
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
    "source": "2026-10-03: latest stable release v0.100.0 == pinned release "
              "dir (GitHub /releases re-confirmed v0.100.0 still latest "
              "stable at 23:13 CDT 2026-10-02); hashes recomputed against "
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

TUNNEL = "interplanar tunnel"

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
    "Interplanar Tunnel",
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

SETUP_DEADLINE_S = 2400
SEEK_WATCHDOG_TURNS = 80


def reset_attempt():
    global ST, MULLS, MULL_COUNT, SUBMITTED, SUBMITTED_OPPS, _DISCARD_REV
    ST = {
        "t0": time.time(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "game_code": None,
        "phase": "setup",  # setup -> seeking -> encounter_window -> done
        "attempt": 1,
        "starting_plane_name": None,
        "starting_plane_oid": None,
        "rolls": 0,
        "roll_pending": False,
        "roll_at": 0,
        "roll_pre_plane": None,
        "roll_rejections": 0,
        "pre_exported": False,
        "post_exported": False,
        "window_end_written": False,
        "encounter_seen": False,
        "encounter_resolved": False,
        "encounter_turn": None,
        "tunnel_oid": None,
        "planar_deck_at_pre": None,
        "revealed_seen_window": False,
        "revealed_max_window": 0,
        "departure_by_window_end": None,
        "prompts_seen": [],
        "wf_types_window": [],
        "terminal": False,
        "terminal_data": None,
        "states_seen": 0,
        "cast_rejections": 0,
        "settle_at": None,
        "land_turn": -1,
        "ass": {k: "not-run" for k in ("A1_data_level", "A2_setup_ok",
                                       "A3_encounter_trigger_resolved",
                                       "A4_reveal_choice_offered",
                                       "A5_no_ability_effect", "A6_cleanup")},
        "notes": [],
        "data_level_ok": False,
        "discard_iids": set(),
    }
    MULLS = set()
    MULL_COUNT = {}
    SUBMITTED = set()
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


def gy_ids(state, pid):
    gy = state.get("graveyard") or {}
    ids = gy.get(str(pid)) or gy.get(pid) or []
    return [int(o) for o in ids]


def bf_permanents(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


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


def encounter_trigger_on_stack(state):
    for se in stack_entries(state):
        if TUNNEL in json.dumps(se, default=str).lower():
            return se
    return None


def tunnel_face_up(state):
    """(oid or None, face_up bool) for Interplanar Tunnel in command zone."""
    for oid, o in (state.get("objects") or {}).items():
        if str(o.get("base_name") or o.get("name") or "").lower() == TUNNEL:
            if o.get("zone") == "Command":
                return int(oid), not o.get("face_down")
    return None, False


def planar_deck_oids(state):
    pd = state.get("planar_deck") or []
    return [int(x) for x in pd]


def revealed_count(state):
    rc = state.get("revealed_cards") or []
    prc = state.get("public_revealed_cards") or []
    return len(rc) + len(prc)

# ------------------------------------------------------------- data check
def check_data_level():
    card = CARD_DATA.get(TUNNEL, {})
    trigs = card.get("triggers") or []
    pw = [t for t in trigs
          if t.get("mode") == {"Planeswalked": {"role": "To"}}]
    ev = {
        "name": card.get("name"),
        "oracle": card.get("oracle_text"),
        "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
        "trigger_mode": None,
        "trigger_zones": None,
        "head_effect": None,
        "sub_ability": None,
        "sub_sub_ability": None,
        "report_corpus_note": ("the issue's corpus (9b7c66e30) names the "
                               "Unimplemented head 'reveal'; the pinned "
                               "v0.100.0 corpus names it "
                               "'unparsed_verb_arguments' with the identical "
                               "description -- both are Effect::Unimplemented "
                               "over the same clause; the structural claim "
                               "is unchanged. The pinned parse also shows "
                               "the 'put the rest ...' clause as a second "
                               "Unimplemented node, matching the triage "
                               "comment."),
    }
    ok = False
    if pw:
        t = pw[0]
        ev["trigger_mode"] = t.get("mode")
        ev["trigger_zones"] = t.get("trigger_zones")
        ex = (t.get("execute") or {})
        ev["head_effect"] = (ex.get("effect") or {})
        ev["sub_ability"] = (ex.get("sub_ability") or {})
        sub = ev["sub_ability"]
        sub_eff = (sub.get("effect") or {})
        ev["sub_sub_ability"] = (sub.get("sub_ability") or {})
        head = ev["head_effect"]
        subsub = ((ev["sub_sub_ability"] or {}).get("effect")) or {}
        ok = (head.get("type") == "Unimplemented"
              and "reveal cards from the top of your planar deck until "
                  "you reveal five plane cards"
              in str(head.get("description", "")).lower()
              and sub_eff.get("type") == "ChangeZoneAll"
              and (sub_eff.get("target") or {}).get("type")
              == "TrackedSetFiltered"
              and subsub.get("type") == "Unimplemented"
              and "put the rest of the revealed cards on the bottom in a "
                  "random order" in str(subsub.get("description", "")).lower())
        say(f"data-level check: trigger mode={t.get('mode')}, "
            f"head={head.get('type')}/{head.get('name')}, "
            f"sub={sub_eff.get('type')}/{sub_eff.get('destination')}/"
            f"{(sub_eff.get('target') or {}).get('type')}, "
            f"subsub={subsub.get('type')}/{subsub.get('name')}")
    else:
        say("data-level check: NO Planeswalked-To trigger found in pinned "
            "card data")
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

# ------------------------------------------------------------- P0 tick
async def p0_tick(st, acts, state, c):
    # Freeze all actions until the decisive pre is exported: the encounter
    # trigger resolves as soon as both seats pass priority (it offers no
    # choices), so no priority pass may happen before pre is captured.
    if ST["phase"] == "encounter_window" and not ST["pre_exported"]:
        return
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
    if (phase in ("PreCombatMain", "PostCombatMain") and active == 0
            and not stack_entries(state)
            and ST["land_turn"] != state.get("turn_number")):
        for a in acts:
            if a["type"] == "PlayLand":
                ST["land_turn"] = state.get("turn_number")
                say(f"[P0] playing land (turn {state.get('turn_number')})")
                await submit_as_is(c, a)
                return
    if ST["phase"] == "seeking" \
            and phase in ("PreCombatMain", "PostCombatMain") \
            and active == 0 and not stack_entries(state):
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
    await pass_priority(c, st, acts)


# ------------------------------------------------------------- P1 tick
async def p1_tick(st, acts, state, c):
    if ST["phase"] == "encounter_window" and not ST["pre_exported"]:
        return
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
    we = load_env("window_end") or {}
    we_st = we.get("state") or {}

    # A1: data-level parse matches the issue
    if ST.get("data_level_ok"):
        ass["A1_data_level"] = "passed"
        notes.append("A1 passed: pinned v0.100.0 card-data.json parses "
                     "Interplanar Tunnel's encounter trigger as mode "
                     "{'Planeswalked': {'role': 'To'}} with head "
                     "Unimplemented('unparsed_verb_arguments': 'reveal cards "
                     "from the top of your planar deck until you reveal "
                     "five plane cards') + sub ChangeZoneAll "
                     "(destination Library, target TrackedSetFiltered id 0) "
                     "+ sub-sub Unimplemented('unparsed_verb_arguments': "
                     "'put the rest of the revealed cards on the bottom in "
                     "a random order'); see data_evidence.json.")
    else:
        ass["A1_data_level"] = "failed"
        notes.append("A1 FAILED: the pinned parse does not match the "
                     "issue-reported shape; see data_evidence.json.")

    # A2: setup at the decisive pre -- Tunnel encountered (face-up)
    if ST["pre_exported"] and pre_st:
        oid, up = tunnel_face_up(pre_st)
        ST["tunnel_oid"] = oid
        notes.append(f"A2 probe: Interplanar Tunnel face-up in command zone="
                     f"{up} (oid={oid}); planar_controller="
                     f"{pre_st.get('planar_controller')}; planar_deck "
                     f"count={len(planar_deck_oids(pre_st))}.")
        if up:
            ass["A2_setup_ok"] = "passed"
            notes.append("A2 passed: Interplanar Tunnel was encountered "
                         "(face-up in the command zone) when its trigger "
                         "was on the stack.")
        else:
            ass["A2_setup_ok"] = "failed"
            notes.append("A2 FAILED: the decisive pre does not show "
                         "Interplanar Tunnel face-up in the command zone.")
    else:
        ass["A2_setup_ok"] = "failed"
        notes.append("A2 FAILED: pre.json was never exported (the Tunnel "
                     "was never encountered).")

    # A3: the encounter trigger fired and resolved
    if ST["encounter_seen"] and ST["encounter_resolved"]:
        ass["A3_encounter_trigger_resolved"] = "passed"
        notes.append("A3 passed: the 'When you encounter Interplanar "
                     "Tunnel' trigger was observed on the stack and "
                     f"resolved (turn {ST.get('encounter_turn')}).")
    elif ST["encounter_seen"]:
        ass["A3_encounter_trigger_resolved"] = "failed"
        notes.append("A3 FAILED: the encounter trigger was seen on the "
                     "stack but its resolution was never confirmed.")
    else:
        notes.append("A3 not-run: the Tunnel was never encountered.")

    # A4: was a reveal/on-top choice offered during the encounter window?
    # A prompt appearing is not a pass (playbook step 2): only a
    # CHOICE-shaped prompt counts, and it must be reveal-related (the
    # correct behavior offers "put a plane card from among them on top of
    # your planar deck"). Ordinary turn-structure prompts (priority
    # passes, tap-land menus, DeclareAttackers/DeclareBlockers relations
    # schema, hand-zone discard UI) do NOT count.
    ORDINARY_ACTIONS = {"passPriority", "tapLandForMana", "castSpell",
                        "playLand", "activateAbility", "rollPlanarDie"}
    discard_iids = ST.get("discard_iids") or set()
    choice_like = []
    reveal_related = []
    for _ph, seat, iid, opp in ST["prompts_seen"]:
        if iid in discard_iids:
            continue
        resp = (opp or {}).get("response") or {}
        rtype = resp.get("type")
        blob = json.dumps(opp, default=str).lower()
        is_reveal = ("plane card" in blob and "top" in blob) \
            or "planar deck" in blob or "reveal" in blob
        if rtype == "schema":
            spec = ((resp.get("data") or {}).get("spec")) or {}
            if spec.get("type") == "relations":
                continue
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
            if is_reveal:
                reveal_related.append((seat, iid))
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
            if is_reveal:
                reveal_related.append((seat, iid))
    notes.append(f"Encounter-window prompts: {len(ST['prompts_seen'])} vi "
                 f"opportunities seen by either seat; waiting_for types: "
                 f"{sorted(set(ST['wf_types_window']))}; choice-shaped: "
                 f"{choice_like or 'none'}; reveal-related: "
                 f"{reveal_related or 'none'}.")
    if reveal_related:
        ass["A4_reveal_choice_offered"] = "passed"
        notes.append("A4 passed: a reveal-related choice-shaped prompt was "
                     f"offered during the encounter window: "
                     f"{reveal_related[:4]}.")
    elif ST["encounter_seen"] and ST["encounter_resolved"]:
        ass["A4_reveal_choice_offered"] = "failed"
        notes.append("A4 FAILED: the encounter trigger resolved but NO "
                     "reveal/on-top choice was ever offered to either seat "
                     "-- THE REPORTED BUG (the head clause never parsed, "
                     "so the reveal step does not exist at runtime).")
    else:
        notes.append("A4 not-run: the encounter window never completed.")

    # A5: did the ability move/reveal any planar-deck cards?
    pd_pre = planar_deck_oids(pre_st) if pre_st else []
    pd_we = planar_deck_oids(we_st) if we_st else []
    rev_pre = revealed_count(pre_st)
    rev_we = revealed_count(we_st)
    dep = ST.get("departure_by_window_end")
    notes.append(f"A5 probe: planar_deck pre={len(pd_pre)} cards, "
                 f"window_end={len(pd_we)} cards, order_changed="
                 f"{pd_pre != pd_we if pd_pre and pd_we else 'n/a'}; "
                 f"revealed_cards+public pre={rev_pre} window_end={rev_we}; "
                 f"max revealed during window={ST.get('revealed_max_window')}; "
                 f"departure_already_run_by_window_end={dep}.")
    ability_moved = (ST.get("revealed_seen_window")
                     or (pd_pre and pd_we and pd_pre != pd_we
                         and not dep))
    if ability_moved:
        ass["A5_no_ability_effect"] = "passed"
        notes.append("A5 passed: the encounter ability's resolution "
                     "revealed and/or moved planar-deck cards (see "
                     "probe above).")
    elif ST["encounter_seen"] and ST["encounter_resolved"]:
        ass["A5_no_ability_effect"] = "failed"
        notes.append("A5 FAILED: the encounter trigger resolved with ZERO "
                     "ability-driven planar-deck effect -- no reveal "
                     "recorded, no cards moved by the ability -- THE "
                     "REPORTED BUG (ChangeZoneAll read the empty chain "
                     "tracked set; silent no-op). Any planar-deck motion "
                     "in post is the engine's own phenomenon-departure "
                     "procedure, not the ability.")
    else:
        notes.append("A5 not-run: the encounter window never completed.")

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
            and ass["A3_encounter_trigger_resolved"] == "passed"
            and not (ass["A4_reveal_choice_offered"] == "passed"
                     and ass["A5_no_ability_effect"] == "passed")):
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: Interplanar Tunnel was encountered; its "
            "'When you encounter' trigger fired and resolved, but no "
            "reveal happened, no 'put a plane card on top' choice was "
            "offered, and the ability moved no planar-deck cards -- see "
            "the failed assertion(s) above (confirmed on v0.100.0). The "
            "parse is the reported defect; the silent no-op is its direct "
            "structural consequence (Unimplemented head pushes no "
            "GameEvent -> empty chain tracked set -> ChangeZoneAll no-op). "
            "This is not a fix claim.")
    elif all(ass[k] == "passed" for k in ("A1_data_level", "A2_setup_ok",
                                          "A3_encounter_trigger_resolved",
                                          "A4_reveal_choice_offered",
                                          "A5_no_ability_effect")):
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: the encounter revealed cards until "
            "five plane cards were revealed, offered the on-top choice, "
            "and moved the planar-deck cards. This is not a fix claim.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: the reported encounter path could "
                     "not be fully exercised; see assertion notes.")

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
               if "tunnel" in l.lower()
               or "phenomenon" in l.lower()
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
            open(f"{BACKFILL}/driver/scenario_7426.py", "rb").read()).hexdigest(),
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
            "encounter_seen": ST["encounter_seen"],
            "encounter_resolved": ST["encounter_resolved"],
            "tunnel_oid": ST["tunnel_oid"],
            "planar_deck_at_pre": ST["planar_deck_at_pre"],
            "revealed_max_window": ST["revealed_max_window"],
            "departure_by_window_end": ST["departure_by_window_end"],
            "prompts_seen": [(p[0], p[1], p[2]) for p in ST["prompts_seen"]],
            "wf_types_window": sorted(set(ST["wf_types_window"])),
            "cast_rejections": ST["cast_rejections"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The report is a data-level parse report; the scenario replays "
            "the reported line (encounter Interplanar Tunnel in a fresh "
            "Planechase game) and observes the resolution outcome. The "
            "planar deck is engine-shuffled; P0 planeswalked via the planar "
            "die until the Tunnel was encountered (filler planes chosen "
            "for benign, choice-free chaos abilities; planar deck "
            "singleton enforced by the engine).",
            "The engine's phenomenon-departure planeswalk (reveal until a "
            "plane, rest to the bottom) runs after the encounter ability "
            "resolves; planar-deck motion from the departure is not the "
            "ability's effect.",
            "States are authoritative exports, restorable only via full "
            "game replay.",
        ],
        "setup_line": "P0: 60x Island + planar deck [Interplanar Tunnel + "
                      "19 benign planes]; P1: 60x Forest (lands, passes, "
                      "never attacks); Planechase format, life 20",
        "contract_line": "Encounter Interplanar Tunnel: reveal cards from "
                         "the top of the planar deck until five plane "
                         "cards are revealed, put a plane card from among "
                         "them on top, rest on the bottom in random order. "
                         "Observed (bug): the clause never parsed -- no "
                         "reveal, no on-top choice, no planar-deck motion "
                         "from the ability.",
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
    """Run one Planechase game: seek the Tunnel via planeswalking, capture
    the encounter window. Returns 'done' (finalized) or 'giveup'."""
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

                # setup -> seeking: starting plane check
                if ST["phase"] == "setup":
                    pname = active_plane_name(st)
                    if pname and (wf_of(state).get("type") not in
                                  ("MulliganDecision", "BottomCards")):
                        ST["starting_plane_name"] = pname
                        ST["starting_plane_oid"] = active_plane_oid(st)
                        ST["phase"] = "seeking"
                        say(f"starting plane is {pname}; phase -> seeking "
                            f"(will planeswalk until Interplanar Tunnel is "
                            f"encountered)")
                        wire("seeking_start", {"plane": pname})

                # seeking: roll-outcome classification
                if ST["phase"] == "seeking" and ST["roll_pending"]:
                    se = encounter_trigger_on_stack(state)
                    if se:
                        ST["roll_pending"] = False
                        ST["encounter_seen"] = True
                        ST["phase"] = "encounter_window"
                        ST["encounter_turn"] = state.get("turn_number")
                        say("ENCOUNTER: 'When you encounter Interplanar "
                            "Tunnel' trigger on the stack; exporting "
                            "pre.json")
                        wire("encounter_on_stack", {"entry": se})
                        # THE decisive pre: exported INSIDE the
                        # observation path (guarded flag), before either
                        # seat passes priority (ticks are frozen until
                        # pre_exported).
                        if not ST["pre_exported"]:
                            await export_as(p0, "pre")
                            ST["pre_exported"] = True
                            ST["planar_deck_at_pre"] = planar_deck_oids(state)
                            wire("pre_exported",
                                 {"planar_deck": ST["planar_deck_at_pre"]})
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

                # encounter-window observation (either seat)
                if ST["phase"] == "encounter_window":
                    vi = get_vi(st)
                    if vi:
                        for opp in vi.get("opportunities", []) or []:
                            record_window_prompt(tag, opp)
                    wtype = (wf_of(state).get("type") or "")
                    if wtype and wtype not in ST["wf_types_window"]:
                        ST["wf_types_window"].append(wtype)
                    rc = revealed_count(state)
                    if rc > 0:
                        ST["revealed_seen_window"] = True
                    ST["revealed_max_window"] = max(
                        ST["revealed_max_window"], rc)
                    if (ST["encounter_seen"] and not ST["encounter_resolved"]
                            and not encounter_trigger_on_stack(state)):
                        ST["encounter_resolved"] = True
                        _oid, _up = tunnel_face_up(state)
                        ST["departure_by_window_end"] = (not _up)
                        # Write the OBSERVED state, not a re-export: the
                        # export round-trip races the other client's
                        # auto-passing loop and can land turns later.
                        with open(f"{EVDIR}/window_end.json", "w") as wfj:
                            json.dump({"state": state,
                                       "observed_turn":
                                           state.get("turn_number"),
                                       "observed_phase":
                                           state.get("phase"),
                                       "encounter_turn": ST.get("encounter_turn"),
                                       "tunnel_face_up": _up,
                                       "active_plane": active_plane_name(st),
                                       "planar_deck": planar_deck_oids(state),
                                       "revealed": rc},
                                      wfj, default=str)
                        say("window_end.json written from observed state "
                            f"(turn {state.get('turn_number')}, "
                            f"phase {state.get('phase')}, tunnel_face_up="
                            f"{_up})")
                        wire("encounter_window_complete",
                             {"tunnel_face_up": _up,
                              "planar_deck": planar_deck_oids(state),
                              "revealed": rc})
                        ST["settle_at"] = now
                # decisive post: settled in a main phase (or beyond)
                if ST["encounter_resolved"] and not ST["post_exported"]:
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
                if (ST["phase"] == "seeking"
                        and (state.get("turn_number") or 0)
                        > SEEK_WATCHDOG_TURNS
                        and not ST["post_exported"]):
                    say(f"seek watchdog: {SEEK_WATCHDOG_TURNS} turns in, "
                        f"the Tunnel was never encountered -- exporting "
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
        # No encounter run completed: finalize a blocked run from whatever
        # we have (data-level evidence stands on its own).
        say("no successful encounter attempt; finalizing blocked run")
        try:
            await finalize(None)
        except Exception as e:
            say(f"finalize failed: {e}")


if __name__ == "__main__":
    run = asyncio.run(main())
    # copy the scenario into the evidence dir, render the PNG, and write
    # the SHA-256 manifest over everything except the manifest itself.
    shutil.copy(f"{BACKFILL}/driver/scenario_7426.py",
                f"{EVDIR}/scenario_7426.py")
    import subprocess
    if os.path.exists(f"{EVDIR}/run.json") and os.path.exists(f"{EVDIR}/pre.json") \
            and os.path.exists(f"{EVDIR}/post.json"):
        subprocess.run([sys.executable, f"{BACKFILL}/driver/render_summary.py",
                        EVDIR, str(ISSUE),
                        "Interplanar Tunnel: unparsed 'reveal ... until five "
                        "plane cards' leaves ChangeZoneAll reading an empty "
                        "tracked set; encounter is a silent no-op"],
                       check=True)
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for f in files:
            h = hashlib.sha256(open(f"{EVDIR}/{f}", "rb").read()).hexdigest()
            mf.write(f"{h}  {f}\n")
    print(f"manifest written ({len(files)} files)", flush=True)
    sys.exit(0)
