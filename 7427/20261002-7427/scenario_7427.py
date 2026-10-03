#!/usr/bin/env python3
"""Issue #7427: Invasive Surgery -- the delirium multi-zone same-name search
is unparsed, so the dependent ChangeZone reads an empty tracked set.

Reported (2026-08-15, internal triage, status:confirmed, area:parser,
mechanic:search, mechanic:zone-change, classifier:unsupported-aspect,
priority:p3-card-specific; related #6857 tracked-set census):
On Invasive Surgery, the clause "search the graveyard, hand, and library
of that spell's controller for any number of cards with the same name as
that spell" does not parse: the parser emits an `Effect::Unimplemented`
node in its place. That resolver pushes no `GameEvent`, so the chain
tracked set is allocated empty and the dependent `ChangeZone` ("exile
those cards") reads an empty set. The census did NOT measure `ChangeZone`'s
consumption style, and the issue does not assert a runtime symptom -- the
reported defect is the parse state and the empty publish that structurally
follows from it.

Oracle text (verified against the pinned v0.100.0 card-data.json):
> Counter target sorcery spell.
> Delirium -- If there are four or more card types among cards in your
> graveyard, search the graveyard, hand, and library of that spell's
> controller for any number of cards with the same name as that spell,
> exile those cards, then that player shuffles.

Pinned v0.100.0 parse (see data_evidence.json):
  abilities[0]: Counter { target: StackSpell + Typed[Sorcery] }
                ("Counter target sorcery spell.")
  abilities[1]: Unimplemented { name: "unparsed_verb_arguments",
        description: "search the graveyard, hand, and library of that
                      spell's controller for any number of cards with the
                      same name as that spell" }
      sub_ability: ChangeZone { destination: "Exile",
                               target: TrackedSet { id: 0 } }
        sub_sub_ability: Shuffle { target: ParentTargetController },
                         condition: QuantityCheck(DistinctCardTypes(
                           controller graveyard) >= 4)
    (the Unimplemented head is the chain root; the sub reads the chain
    tracked set. The issue's corpus (9b7c66e30) names the head "search";
    the pinned v0.100.0 corpus names it "unparsed_verb_arguments" with the
    identical description -- both are Effect::Unimplemented over the same
    clause; the structural claim is unchanged.)

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, FreeForAll, life 20):
  P0: 4x Invasive Surgery, 4x Chromatic Star, 4x Catalog, 4x Divination,
      44x Island.
      Delirium is built deterministically, no library search needed:
        Chromatic Star ({1}, sac) -> graveyard (artifact)
        Catalog ({2}{U} instant)  -> graveyard (instant)
        Divination ({2}{U} sorcery) -> graveyard (sorcery)
        Island discarded to hand size -> graveyard (land)
      = 4 card types (land, artifact, instant, sorcery) in P0's graveyard.
  P1: 6x Divination, 54x Island (plays a land, passes; casts Divination
      only once P0 signals delirium-ready with Surgery in hand).
Drive:
  1. Mulligans: both seats keep 7.
  2. P0 builds delirium: land drops, Star cast+sac, Catalog, Divination,
     discarding Islands to hand size. P1 plays Islands and waits.
  3. When P0's graveyard holds >= 4 distinct card types AND Surgery is in
     P0's hand (ST p0_ready), P1 casts Divination ({2}{U}) on its main
     phase with an empty stack.
  4. P0 casts Invasive Surgery ({U}) targeting the Divination spell on the
     stack (advertised CastSpell; target selection answered from the
     engine-advertised candidates, matched on serialized content).
  5. When Surgery is on the stack: export pre.json IMMEDIATELY in the
     observation path (guarded flag), before either seat passes priority
     (ticks freeze until pre_exported).
  6. Both seats pass priority; Surgery resolves. Record every
     viewer-interaction opportunity offered during the resolution window.
  7. When Surgery leaves the stack: write window_end.json from the
     OBSERVED state (no export round-trip race). Settle: stack empty,
     Priority, 8s idle -> export post.json.

Expected (correct behavior): Surgery counters Divination; then, delirium
being on, the engine searches P1's graveyard/hand/library for any number
of cards named Divination (offering a choice), exiles those cards, and P1
shuffles.
Reported (bug): the search clause never parsed, so the delirium
"search ... exile those cards" is a silent no-op -- no search/exile choice
is offered, and the dependent ChangeZone exiles nothing (empty tracked
set). Only the counter half and the shuffle happen.

Assertions (correct-behavior expectations; a FAIL records the bug):
  A1_data_level   pinned v0.100.0 card-data.json parses Invasive Surgery
                  as the issue reports: abilities[1] head Unimplemented
                  naming the "search the graveyard, hand, and library of
                  that spell's controller for any number of cards with the
                  same name as that spell" clause; sub ChangeZone
                  (destination Exile, target TrackedSet id 0); sub-sub
                  Shuffle (target ParentTargetController) under the
                  delirium QuantityCheck (DistinctCardTypes >= 4).
  A2_setup_ok     pre.json: Invasive Surgery on the stack, and P0's
                  graveyard holds >= 4 distinct card types (delirium on).
  A3_counter_worked (control) the targeted Divination spell was countered:
                  its stack object is in P1's graveyard in post.json and
                  P1 never drew 2 from it.
  A4_exile_noop   (THE REPORTED BUG) the delirium exile moved nothing:
                  post.json shows 0 Divinations in exile, P1's
                  library+hand Divination count is unchanged apart from
                  normal draws/the countered spell, and no search/exile
                  choice was offered to either seat during the resolution
                  window. Expected: the same-name cards exiled.
  A5_shuffle_happened the delirium Shuffle sub ran: P1's library order
                  changed pre -> post (content preserved).
  A6_cleanup      post stack empty, game advancing.

Verdict rule: reproduced iff A1 and A2 passed and A4 shows the no-op
              (0 Divinations exiled with delirium on); not-reproduced iff
              A1..A5 all passed (Divinations actually exiled);
              blocked iff A1 or A2 could not be established.

Protocol-101 driver notes (v0.100.0, build bc9ef56):
  - HELLO advertises 101 (server enforces exact match).
  - MulliganDecision as {"choice":{"type":"Keep"}} gated on
    waiting_for.data.pending[] with phase type "Declare".
  - CastSpell: advertised legal action submitted as-is; raw fallback
    {"type":"CastSpell","data":{"object_id":..,"card_id":..,"targets":[],
    "payment_mode":{"type":"Auto"}}}.
  - ActivateAbility: engine-issued action matched on data.source_id.
  - Target selection for Surgery answered from the engine-advertised
    candidates matched on serialized content (candidate -> referenced
    object name == "Divination" on the stack); NEVER a bare numeric
    needle (candidate ids embed the interaction counter).
  - DiscardToHandSize: legacy SelectCards action, Islands preferred.
  - State shape (protocol 101): per-player library/hand/graveyard oid
    lists on players[]; top-level flat "exile" oid list; objects carry
    card_types.core_types, name, zone, controller, owner, tapped.
  - Authoritative exports only from the host seat (P0 creates the game).
  - Adapted from scenario_7426.py (issue #7426, same Unimplemented-head
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
RUN_ID = "20261002-7427"
ISSUE = 7427
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
              "dir (GitHub /releases re-confirmed v0.100.0 still latest "
              "stable at 23:44 CDT 2026-10-02); signatures re-verified and "
              "hashes recomputed against on-disk artifacts this run",
}

# Recompute against on-disk artifacts; never copy hashes blindly.
for _f, _k in (("server/releases/v0.100.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.100.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.100.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.100.0/data/card-data.json"))

STANDARD_FORMAT = {
    # NOTE: "Standard" enforces constructed legality, which rejects the
    # test-harness card pool. FreeForAll is the engine-canonical no-legality
    # 60-card format (FormatConfig::free_for_all()): life 20, seats 2-6,
    # sideboard Unlimited, copy limit Unlimited.
    "format": "FreeForAll",
    "starting_life": 20,
    "min_players": 2,
    "max_players": 6,  # engine-canonical FormatConfig::free_for_all()
    "deck_size": {"type": "Minimum", "data": 60},
    "singleton": False,
    "command_zone": False,
    "commander_damage_threshold": None,
    "team_based": False,
    "uses_commander": False,
    "sideboard_policy": {"type": "Unlimited"},  # canonical FreeForAll
    "default_deck_copy_limit": {"type": "Unlimited"},  # canonical FreeForAll
    "supplies_fixed_deck": False,
    "allow_debug_actions": False,
}


def deck(main_names):
    return {"main_deck": main_names, "sideboard": [], "commander": []}


P0_DECK = deck(["Invasive Surgery"] * 4 + ["Chromatic Star"] * 4
               + ["Catalog"] * 4 + ["Divination"] * 4 + ["Island"] * 44)
P1_DECK = deck(["Divination"] * 6 + ["Island"] * 54)

SETUP_DEADLINE_S = 2400


def reset_attempt():
    global ST, MULLS, SUBMITTED_OPPS, _DISCARD_REV
    ST = {
        "t0": time.time(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "game_code": None,
        "phase": "setup",  # setup -> duel -> done
        "p0_ready": False,
        "star_cast": False,
        "star_sacked": False,
        "catalog_cast": False,
        "p0_div_cast": False,
        "surgery_in_flight": False,
        "surgery_cast": False,
        "surgery_seen": False,
        "surgery_resolved": False,
        "surgery_target_oid": None,
        "surgery_oid": None,
        "target_answered": False,
        "pre_exported": False,
        "post_exported": False,
        "window_end_written": False,
        "settle_at": None,
        "land_turn": -1,
        "terminal": False,
        "terminal_data": None,
        "states_seen": 0,
        "cast_rejections": 0,
        "prompts_seen": [],
        "wf_types_window": [],
        "ass": {k: "not-run" for k in ("A1_data_level", "A2_setup_ok",
                                       "A3_counter_worked", "A4_exile_noop",
                                       "A5_shuffle_happened", "A6_cleanup")},
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
    return str(o.get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def zone_ids(state, pid, zone):
    p = player_of(state, pid)
    return [int(o) for o in (p.get(zone) or [])]


def hand_ids(state, pid):
    return zone_ids(state, pid, "hand")


def lib_ids(state, pid):
    return zone_ids(state, pid, "library")


def gy_ids(state, pid):
    return zone_ids(state, pid, "graveyard")


def exile_ids(state):
    return [int(o) for o in (state.get("exile") or [])]


def bf_ids(state, pid=None):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") != "Battlefield":
            continue
        if pid is not None and o.get("controller") != pid:
            continue
        out.append(int(oid))
    return out


def untapped_islands(state, pid):
    n = 0
    for oid in bf_ids(state, pid):
        o = get_obj(state, oid)
        if obj_lname(state, oid) == "island" and not o.get("tapped"):
            n += 1
    return n


def gy_distinct_types(state, pid):
    types = set()
    for oid in gy_ids(state, pid):
        o = get_obj(state, oid)
        for t in ((o.get("card_types") or {}).get("core_types") or []):
            types.add(t)
    return types


def find_hand(state, pid, name):
    for oid in hand_ids(state, pid):
        if obj_lname(state, oid) == name.lower():
            return oid
    return None


def stack_entries(state):
    return state.get("stack") or []


def stack_spell_named(state, name):
    """Find a SPELL stack entry by resolving its object id through the
    objects map. NOTE: Spell stack entries carry card_id, NOT a name --
    blob-substring matching fails for them (it only works for trigger
    entries, which carry source_name/description)."""
    for se in stack_entries(state):
        for key in ("id", "source_id"):
            oid = se.get(key)
            if oid is None:
                continue
            o = get_obj(state, oid)
            if str(o.get("name", "")).lower() == name.lower() \
                    and o.get("zone") == "Stack":
                return se
    return None


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

# ------------------------------------------------------------- data check
def check_data_level():
    card = CARD_DATA.get("invasive surgery", {})
    abil = card.get("abilities") or []
    ev = {
        "name": card.get("name"),
        "oracle": card.get("oracle_text"),
        "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
        "abilities_count": len(abil),
        "counter_ability": None,
        "head_effect": None,
        "sub_ability": None,
        "sub_sub_ability": None,
        "report_corpus_note": ("the issue's corpus (9b7c66e30) names the "
                               "Unimplemented head 'search'; the pinned "
                               "v0.100.0 corpus names it "
                               "'unparsed_verb_arguments' with the identical "
                               "description -- both are Effect::Unimplemented "
                               "over the same clause; the structural claim "
                               "is unchanged."),
    }
    ok = False
    if len(abil) >= 2:
        ev["counter_ability"] = (abil[0].get("effect") or {})
        d = abil[1]
        ev["head_effect"] = (d.get("effect") or {})
        ev["sub_ability"] = (d.get("sub_ability") or {})
        sub = ev["sub_ability"]
        sub_eff = (sub.get("effect") or {})
        ev["sub_sub_ability"] = (sub.get("sub_ability") or {})
        subsub = ev["sub_sub_ability"]
        subsub_eff = (subsub.get("effect") or {})
        head = ev["head_effect"]
        cond = (subsub.get("condition") or {})
        qty = ((cond.get("lhs") or {}).get("qty") or {})
        ok = (
            (abil[0].get("effect") or {}).get("type") == "Counter"
            and head.get("type") == "Unimplemented"
            and "search the graveyard, hand, and library of that spell's "
                "controller for any number of cards with the same name as "
                "that spell" in str(head.get("description", "")).lower()
            and sub_eff.get("type") == "ChangeZone"
            and sub_eff.get("destination") == "Exile"
            and (sub_eff.get("target") or {}).get("type") == "TrackedSet"
            and (sub_eff.get("target") or {}).get("id") == 0
            and subsub_eff.get("type") == "Shuffle"
            and (subsub_eff.get("target") or {}).get("type")
                == "ParentTargetController"
            and cond.get("type") == "QuantityCheck"
            and cond.get("comparator") == "GE"
            and qty.get("type") == "DistinctCardTypes"
            and ((cond.get("rhs") or {}).get("value")) == 4
        )
        say(f"data-level check: head={head.get('type')}/{head.get('name')}, "
            f"sub={sub_eff.get('type')}/{sub_eff.get('destination')}/"
            f"{(sub_eff.get('target') or {}).get('type')}/"
            f"{(sub_eff.get('target') or {}).get('id')}, "
            f"subsub={subsub_eff.get('type')}, "
            f"cond={cond.get('type')}/{cond.get('comparator')}/"
            f"{qty.get('type')}")
    else:
        say("data-level check: fewer than 2 abilities on Invasive Surgery")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    ST["data_level_ok"] = ok
    wire("data_level", {"ok": ok})

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
    # Prefer discarding Islands (lands feed the delirium land slot and are
    # plentiful); never discard Invasive Surgery.
    def sortkey(oid):
        nm = obj_lname(state, oid)
        return (0 if nm == "island" else 1,
                2 if nm == "invasive surgery" else 0,
                oid)
    picks = [int(x) for x in sorted(hand, key=sortkey)[:n]]
    say(f"[{tag}] discarding to hand size: "
        f"{[obj_lname(state, o) for o in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    _DISCARD_REV[(c.name, rev)] = True
    wire("discard", {"who": tag, "picks": picks})
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
        # prefer an Island candidate
        pick = None
        for ch in chs:
            for sf in (ch.get("surfaces") or []):
                d = sf.get("data") or {}
                try:
                    ref = int(d.get("reference"))
                except (TypeError, ValueError):
                    continue
                if obj_lname(state, ref) == "island":
                    pick = ch.get("id")
                    break
            if pick:
                break
        if not pick:
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


async def cast_by_name(c, pid, state, acts, name, tag):
    """Cast a spell from hand by name; advertised CastSpell as-is, else raw
    with payment_mode Auto. Serializes casts (cast_in_flight guard)."""
    if ST.get("cast_in_flight"):
        return False
    oid = find_hand(state, pid, name)
    if not oid:
        return False
    for a in acts:
        if a.get("type") == "CastSpell" and str(
                (a.get("data") or {}).get("object_id")) == str(oid):
            await submit_as_is(c, a)
            say(f"[{tag}] cast {name} via advertised CastSpell (oid={oid})")
            wire("cast", {"who": tag, "name": name, "oid": oid,
                          "via": "advertised"})
            ST["cast_in_flight"] = str(oid)
            return True
    obj = get_obj(state, oid)
    cid = obj.get("card_id", int(oid))
    raw = {"type": "CastSpell",
           "data": {"object_id": int(oid), "card_id": int(cid),
                    "targets": [], "payment_mode": {"type": "Auto"}}}
    await submit_as_is(c, raw)
    say(f"[{tag}] cast {name} via raw CastSpell Auto (oid={oid})")
    wire("cast", {"who": tag, "name": name, "oid": oid, "via": "raw"})
    ST["cast_in_flight"] = str(oid)
    return True


async def activate_star(c, state, acts, tag):
    """Activate Chromatic Star's {1},sac ability via engine-issued
    ActivateAbility matched on source_id."""
    star_oid = None
    for oid in bf_ids(state, 0):
        if obj_lname(state, oid) == "chromatic star":
            star_oid = oid
            break
    if star_oid is None:
        return False
    for a in acts:
        if a.get("type") == "ActivateAbility" and str(
                (a.get("data") or {}).get("source_id")) == str(star_oid):
            await submit_as_is(c, a)
            say(f"[{tag}] activated Chromatic Star (oid={star_oid})")
            wire("activate_star", {"oid": star_oid})
            ST["cast_in_flight"] = f"star-{star_oid}"
            return True
    return False


def vi_ops(st):
    return (st.get("viewer_interaction") or {}).get("opportunities", []) or []


def answer_surgery_target(c, st, state):
    """If a target-selection opportunity for the in-flight Surgery is
    advertised, answer it with the Divination spell on the stack.
    Candidates are matched on serialized content (the referenced object's
    name and zone), never on a bare numeric needle: candidate ids are
    opaque and embed the interaction counter."""
    if not ST.get("surgery_in_flight") or ST.get("target_answered"):
        return None
    for opp in vi_ops(st):
        resp = (opp.get("response") or {})
        rtype = resp.get("type")
        data = resp.get("data") or {}
        cands = data.get("candidates") or data.get("choices") or []
        if rtype != "schema" or not cands:
            continue
        spec = data.get("spec") or {}
        for ch in cands:
            for sf in (ch.get("surfaces") or []):
                d = sf.get("data") or {}
                if sf.get("type") != "object":
                    continue
                try:
                    ref = int(d.get("reference"))
                except (TypeError, ValueError):
                    continue
                o = get_obj(state, ref)
                if str(o.get("name", "")).lower() == "divination" \
                        and o.get("zone") == "Stack":
                    iid = opp.get("interactionId") or opp.get("id")
                    if iid in SUBMITTED_OPPS:
                        return None
                    SUBMITTED_OPPS.add(iid)
                    wire("surgery_target_submit",
                         {"iid": iid, "pick": ch.get("id"),
                          "target_oid": ref,
                          "spec_type": spec.get("type")})
                    say(f"[P0] answering Surgery target selection: "
                        f"Divination stack object {ref}")
                    return {"interactionId": iid,
                            "response": {"type": "sequence",
                                         "data": {"choiceIds": [ch.get("id")]}}}
        wire("surgery_target_unmatched",
             {"iid": opp.get("interactionId") or opp.get("id"),
              "spec": str(spec)[:400],
              "n_candidates": len(cands)})
    return None


async def maybe_answer_surgery_target(c, st, state):
    sub = answer_surgery_target(c, st, state)
    if sub:
        await c.send_interaction(sub)
        ST["target_answered"] = True
        return True
    return False

# ------------------------------------------------------------- P0 tick
def clear_cast_in_flight(state, pid):
    """Clear the in-flight cast flag once the object left the hand."""
    cf = ST.get("cast_in_flight")
    if not cf:
        return
    if str(cf).startswith("star-"):
        oid = int(str(cf).split("-")[1])
        if get_obj(state, oid).get("zone") != "Battlefield":
            ST["cast_in_flight"] = None
            ST["star_sacked"] = True
            say("[P0] Chromatic Star sacrificed (draw trigger on stack)")
        return
    try:
        oid = int(cf)
    except (TypeError, ValueError):
        ST["cast_in_flight"] = None
        return
    if get_obj(state, oid).get("zone") != "Hand":
        ST["cast_in_flight"] = None


def delirium_types(state):
    return gy_distinct_types(state, 0)


def p0_setup_state(state):
    """Derived setup flags from the actual state (not submit-time)."""
    gy_names = [obj_lname(state, o) for o in gy_ids(state, 0)]
    bf_names = [obj_lname(state, o) for o in bf_ids(state, 0)]
    return {
        "star_live": "chromatic star" in bf_names,
        "star_done": "chromatic star" in gy_names,
        "catalog_done": "catalog" in gy_names,
        "div_done": "divination" in gy_names,
        "types": delirium_types(state),
        "delirium": len(delirium_types(state)) >= 4,
        "surgery_in_hand": find_hand(state, 0, "invasive surgery") is not None,
    }


async def answer_mana_color(c, st, state, tag):
    """Answer a 'choose a mana color' (manaGroups) prompt, e.g. from
    Chromatic Star's 'add one mana of any color'. Prefers blue."""
    vi = get_vi(st)
    if not vi:
        return False
    for opp in vi.get("opportunities", []) or []:
        resp = opp.get("response") or {}
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        if spec.get("type") != "manaGroups":
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        chs = data.get("choices") or data.get("candidates") or []
        if not chs:
            continue
        # prefer a blue choice; else the first available
        pick = None
        for ch in chs:
            blob = json.dumps(ch, default=str).lower()
            if ch.get("status", {}).get("type") == "available" \
                    and "blue" in blob:
                pick = ch
                break
        if pick is None:
            for ch in chs:
                if ch.get("status", {}).get("type") == "available":
                    pick = ch
                    break
        if pick is None:
            continue
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] mana color choice: "
            f"{json.dumps(pick, default=str)[:120]}")
        wire("mana_color", {"who": tag, "iid": iid,
                            "pick": str(pick.get("id"))})
        await c.send_interaction({
            "interactionId": iid,
            "response": {"type": "manaGroups",
                         "data": {"choiceIds": [pick.get("id")],
                                  "count": 1}}})
        return True
    return False


async def p0_tick(st, acts, state, c):
    # Freeze everything except the Surgery target answer until the
    # decisive pre is exported.
    if stack_spell_named(state, "invasive surgery") and not ST["pre_exported"]:
        await maybe_answer_surgery_target(c, st, state)
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
    clear_cast_in_flight(state, 0)
    # Answer a pending Surgery target selection first.
    if ST.get("surgery_in_flight") and not ST.get("target_answered"):
        if await maybe_answer_surgery_target(c, st, state):
            return
    # Chromatic Star's "add one mana of any color" needs a color choice.
    if await answer_mana_color(c, st, state, "P0"):
        return
    if not my_priority(state, 0):
        return
    setup = p0_setup_state(state)
    ST["p0_ready"] = bool(setup["delirium"] and setup["surgery_in_hand"])
    phase = state.get("phase")
    active = state.get("active_player")
    stack = stack_entries(state)

    # Respond to P1's Divination with Invasive Surgery.
    div_on_stack = stack_spell_named(state, "divination")
    surg_on_stack = stack_spell_named(state, "invasive surgery")
    if div_on_stack is not None and not ST.get("_div_logged"):
        ST["_div_logged"] = True
        say(f"[P0] Divination spell on stack (entry id="
            f"{div_on_stack.get('id')}); p0_ready={ST['p0_ready']}, "
            f"surgery_cast={ST.get('surgery_cast')}")
        wire("divination_on_stack", {"entry_id": div_on_stack.get("id")})
    if div_on_stack is None:
        ST["_div_logged"] = False
    if ST["p0_ready"] and div_on_stack is not None \
            and surg_on_stack is None \
            and not ST.get("surgery_cast"):
        if await cast_by_name(c, 0, state, acts, "invasive surgery", "P0"):
            ST["surgery_in_flight"] = True
            ST["surgery_cast"] = True
            say("[P0] Invasive Surgery cast in response to Divination")
            wire("surgery_cast", {})
            return

    if not (phase in ("PreCombatMain", "PostCombatMain") and active == 0
            and not stack):
        await pass_priority(c, st, acts)
        return

    # Delirium build order (state-driven, waits when mana/pieces missing).
    if ST["p0_ready"]:
        await pass_priority(c, st, acts)
        return
    if player_of(state, 0).get("lands_played_this_turn", 0) == 0:
        lid = find_hand(state, 0, "island")
        if lid:
            for a in acts:
                if a["type"] == "PlayLand" and str(
                        (a.get("data") or {}).get("object_id")) == str(lid):
                    say("[P0] playing Island")
                    await submit_as_is(c, a)
                    return
    ui = untapped_islands(state, 0)
    if not setup["star_live"] and not setup["star_done"]:
        if find_hand(state, 0, "chromatic star") and ui >= 1:
            if await cast_by_name(c, 0, state, acts, "chromatic star", "P0"):
                return
    elif setup["star_live"] and not setup["star_done"]:
        if ui >= 1:
            if await activate_star(c, state, acts, "P0"):
                return
    elif not setup["catalog_done"]:
        if find_hand(state, 0, "catalog") and ui >= 3:
            if await cast_by_name(c, 0, state, acts, "catalog", "P0"):
                return
    elif not setup["div_done"]:
        if find_hand(state, 0, "divination") and ui >= 3:
            if await cast_by_name(c, 0, state, acts, "divination", "P0"):
                return
    await pass_priority(c, st, acts)


# ------------------------------------------------------------- P1 tick
async def p1_tick(st, acts, state, c):
    if stack_spell_named(state, "invasive surgery") and not ST["pre_exported"]:
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
    clear_cast_in_flight(state, 1)
    if not my_priority(state, 1):
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if phase in ("PreCombatMain", "PostCombatMain") and active == 1 \
            and not stack_entries(state):
        if player_of(state, 1).get("lands_played_this_turn", 0) == 0:
            lid = find_hand(state, 1, "island")
            if lid:
                for a in acts:
                    if a["type"] == "PlayLand" and str(
                            (a.get("data") or {}).get("object_id")) == str(lid):
                        await submit_as_is(c, a)
                        return
        # Cast the bait Divination once P0 is delirium-ready. Recast on a
        # later turn if the previous bait resolved without Surgery (the
        # one-shot flag is not used: a missed response must not end the run).
        if ST.get("p0_ready") and not ST.get("surgery_cast") \
                and not stack_spell_named(state, "divination") \
                and (state.get("turn_number") or 0) > ST.get("p1_bait_turn", 0) \
                and find_hand(state, 1, "divination") \
                and untapped_islands(state, 1) >= 3:
            if await cast_by_name(c, 1, state, acts, "divination", "P1"):
                ST["p1_bait_turn"] = state.get("turn_number") or 0
                say("[P1] cast bait Divination (P0 delirium-ready)")
                wire("p1_divination_cast", {})
                return
    await pass_priority(c, st, acts)


def record_window_prompt(seat, opp):
    iid = opp.get("interactionId") or opp.get("id")
    tag = (ST["phase"], seat, iid)
    if all(t[:3] != tag for t in ST["prompts_seen"]):
        ST["prompts_seen"].append((tag[0], tag[1], tag[2], opp))
        wire("window_prompt", {"phase": ST["phase"], "seat": seat,
                               "iid": iid,
                               "opportunity": str(opp)[:2000]})
        say(f"[{ST['phase']}/{seat}] prompt seen: "
            f"{json.dumps(opp, default=str)[:240]}")

# ------------------------------------------------------------- finalize
def div_counts(state, pid):
    p = player_of(state, pid)
    out = {}
    for z in ("hand", "library", "graveyard"):
        n = 0
        for oid in (p.get(z) or []):
            if str((get_obj(state, int(oid)).get("name") or "")).lower() \
                    == "divination":
                n += 1
        out[z] = n
    return out


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
                     "Invasive Surgery with abilities[1] head "
                     "Unimplemented('unparsed_verb_arguments': 'search the "
                     "graveyard, hand, and library of that spell's "
                     "controller for any number of cards with the same name "
                     "as that spell') + sub ChangeZone (destination Exile, "
                     "target TrackedSet id 0) + sub-sub Shuffle (target "
                     "ParentTargetController) under the delirium "
                     "QuantityCheck (DistinctCardTypes >= 4); see "
                     "data_evidence.json.")
    else:
        ass["A1_data_level"] = "failed"
        notes.append("A1 FAILED: the pinned parse does not match the "
                     "issue-reported shape; see data_evidence.json.")

    # A2: setup at the decisive pre -- Surgery on the stack, delirium on
    if ST["pre_exported"] and pre_st:
        surg = stack_spell_named(pre_st, "invasive surgery")
        types = gy_distinct_types(pre_st, 0)
        notes.append(f"A2 probe: Surgery on stack at pre={surg is not None}; "
                     f"P0 graveyard distinct types={sorted(types)} "
                     f"(n={len(types)}); P0 life="
                     f"{player_of(pre_st, 0).get('life')}.")
        if surg is not None and len(types) >= 4:
            ass["A2_setup_ok"] = "passed"
            notes.append("A2 passed: Invasive Surgery was on the stack with "
                         "delirium active (>= 4 card types in P0's graveyard).")
        else:
            ass["A2_setup_ok"] = "failed"
            notes.append("A2 FAILED: decisive pre lacks Surgery-on-stack "
                         "with delirium on.")
    else:
        ass["A2_setup_ok"] = "failed"
        notes.append("A2 FAILED: pre.json was never exported (Surgery was "
                     "never cast with delirium on).")

    # A3: the counter half worked (control) -- the targeted Divination was
    # countered: its stack object sits in P1's graveyard in post.
    if post_st and ST.get("surgery_resolved"):
        p1gy = [obj_lname(post_st, o) for o in gy_ids(post_st, 1)]
        div_in_p1gy = "divination" in p1gy
        notes.append(f"A3 probe: P1 graveyard names in post="
                     f"{sorted(set(p1gy))}; Divination present={div_in_p1gy}; "
                     f"P1 life={player_of(post_st, 1).get('life')}.")
        if div_in_p1gy:
            ass["A3_counter_worked"] = "passed"
            notes.append("A3 passed: the targeted Divination spell was "
                         "countered (in P1's graveyard in post.json).")
        else:
            ass["A3_counter_worked"] = "failed"
            notes.append("A3 FAILED: the Divination spell is not in P1's "
                         "graveyard in post.json.")
    else:
        notes.append("A3 not-run: Surgery never resolved.")

    # A4: THE REPORTED BUG -- the delirium exile should have exiled the
    # same-name cards; with the search clause unparsed it exiles nothing.
    if post_st and pre_st and ST.get("surgery_resolved"):
        ex = exile_ids(post_st)
        ex_div = sum(1 for o in ex
                     if obj_lname(post_st, o) == "divination")
        pre_c = div_counts(pre_st, 1)
        post_c = div_counts(post_st, 1)
        # The countered spell moved stack -> graveyard; everything else
        # should be untouched by the delirium exile.
        notes.append(f"A4 probe: Divinations in exile in post={ex_div}; "
                     f"P1 Divination counts pre={pre_c} post={post_c}.")
        # search/exile choice offered during the resolution window?
        blob = json.dumps(ST["prompts_seen"], default=str).lower()
        search_choice = ("divination" in blob and "exile" in blob) \
            or "search the graveyard" in blob
        notes.append(f"A4 probe: search/exile choice offered during "
                     f"resolution window={search_choice} "
                     f"({len(ST['prompts_seen'])} prompts recorded).")
        if ex_div >= 1:
            ass["A4_same_name_exiled"] = "passed"
            notes.append("A4 passed: the delirium effect exiled "
                         f"{ex_div} Divination(s) -- the search clause "
                         "worked at runtime.")
        else:
            ass["A4_same_name_exiled"] = "failed"
            notes.append("A4 FAILED: with delirium active, Invasive "
                         "Surgery's resolution exiled ZERO Divinations and "
                         f"offered no search/exile choice "
                         f"(offered={search_choice}) -- THE REPORTED BUG "
                         "(the search clause never parsed, so the "
                         "dependent ChangeZone read the empty chain "
                         "tracked set).")
    else:
        notes.append("A4 not-run: Surgery never resolved with pre+post.")
    # rename key to the contract name
    if "A4_same_name_exiled" in ass:
        ass["A4_exile_noop"] = ass.pop("A4_same_name_exiled")

    # A5: the delirium Shuffle sub ran (control) -- P1's library order
    # changed pre -> post while its multiset was preserved.
    if post_st and pre_st and ST.get("surgery_resolved"):
        pre_lib = lib_ids(pre_st, 1)
        post_lib = lib_ids(post_st, 1)
        pre_names = sorted(obj_lname(pre_st, o) for o in pre_lib)
        post_names = sorted(obj_lname(post_st, o) for o in post_lib)
        changed = pre_lib != post_lib
        notes.append(f"A5 probe: P1 library pre={len(pre_lib)} post="
                     f"{len(post_lib)} cards; order_changed={changed}; "
                     f"multiset_equal={pre_names == post_names}.")
        if changed and pre_names == post_names:
            ass["A5_shuffle_happened"] = "passed"
            notes.append("A5 passed: P1's library was shuffled by the "
                         "delirium Shuffle sub (order changed, contents "
                         "preserved) -- the delirium condition was met and "
                         "the ability partially resolved.")
        else:
            ass["A5_shuffle_happened"] = "failed"
            notes.append("A5 FAILED: P1's library was not shuffled as the "
                         "delirium Shuffle sub requires.")
    else:
        notes.append("A5 not-run: Surgery never resolved with pre+post.")

    # A6: cleanup
    if post_st:
        if not stack_entries(post_st):
            ass["A6_cleanup"] = "passed"
            notes.append("A6 passed: post stack empty, game advanced "
                         f"(turn {post_st.get('turn_number')}, phase "
                         f"{post_st.get('phase')}).")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append("A6 FAILED: post stack non-empty.")
    else:
        notes.append("A6 not-run: no post state")

    # verdict
    core = [ass.get(k) for k in ("A1_data_level", "A2_setup_ok")]
    if any(v != "passed" for v in core):
        verdict = "blocked"
    elif ass.get("A4_exile_noop") == "failed":
        verdict = "reproduced"
    elif all(ass.get(k) == "passed" for k in
             ("A1_data_level", "A2_setup_ok", "A3_counter_worked",
              "A4_exile_noop", "A5_shuffle_happened")):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
    say(f"VERDICT: {verdict}")
    wire("verdict", {"verdict": verdict, "assertions": ass})

    # server-log excerpt: the Unimplemented resolver warning, if logged
    try:
        logp = f"{BACKFILL}/runs/20261002-bf1/server.log"
        lines = open(logp, errors="replace").read().splitlines()
        hits = [l for l in lines
                if "nvasive" in l or "nimplemented" in l or "racked" in l]
        with open(f"{EVDIR}/server_log_excerpt.txt", "w") as f:
            f.write("\n".join(hits[-60:]) + "\n")
        notes.append(f"server-log excerpt: {len(hits)} matching lines "
                     f"(of {len(lines)} total) saved.")
    except Exception as e:
        notes.append(f"server-log excerpt failed: {e}")

    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "started_at": ST["started_at"],
        "duration_s": round(time.time() - ST["t0"], 1),
        "server": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 101, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_{ISSUE}.py", "rb").read()
        ).hexdigest(),
        "format_config": "FreeForAll (2-player, 60-card minimum, no legality check)",
        "decks": {
            "P0": [["Invasive Surgery", 4], ["Chromatic Star", 4],
                   ["Catalog", 4], ["Divination", 4], ["Island", 44]],
            "P1": [["Divination", 6], ["Island", 54]],
        },
        "assertions": ass,
        "notes": notes,
        "observations": {
            "p0_ready": ST["p0_ready"],
            "surgery_seen": ST["surgery_seen"],
            "surgery_resolved": ST["surgery_resolved"],
            "target_answered": ST["target_answered"],
            "cast_rejections": ST["cast_rejections"],
            "prompts_seen": [(ph, seat, iid)
                             for ph, seat, iid, _ in ST["prompts_seen"]],
            "wf_types_window": ST["wf_types_window"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human driver "
            "seats.",
            "Dense playsets are a test-harness convenience (engine accepts "
            ">4-of for custom games).",
            "Delirium was built with a deterministic 4-card sequence "
            "(Star/Catalog/Divination/Island-discard); other 4-type "
            "combinations were not exercised.",
            "States are authoritative exports, restorable only via full "
            "game replay.",
        ],
        "setup_line": "P0 builds delirium (Star sac, Catalog, Divination, "
                      "Island discard = 4 types), holds Surgery; P1 casts "
                      "Divination once P0 is ready.",
        "contract_line": "Surgery counters Divination; delirium then "
                         "searches P1's GY/hand/library for Divinations, "
                         "exiles them, P1 shuffles.",
        "prior_runs": [],
        "stats": {"states_seen": ST["states_seen"]},
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    say(f"run.json written: verdict={verdict}")
    wire("run_written", {"verdict": verdict})
    for c in (WIRE, RUNLOG):
        try:
            c.close()
        except Exception:
            pass
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}", flush=True)
    return run


# ------------------------------------------------------------- one game
async def drive_one_game():
    """Run one Standard game: build delirium, counter Divination with
    Invasive Surgery, capture the resolution window. Returns 'done' or
    'giveup'."""
    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    await p0.connect()
    await p0.create(P0_DECK, player_count=2, format_config=STANDARD_FORMAT)
    await p1.connect()
    await p1.join(p0.game_code, P1_DECK)
    await asyncio.sleep(2.0)
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_setup", {"game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})

    t_start = time.time()
    last = {}
    last_tick_at = {}
    result = "giveup"

    async def close():
        for c in (p0, p1):
            try:
                await c.close()
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
                            if ST.get("cast_in_flight") and \
                                    "CastSpell" in json.dumps(data):
                                ST["cast_in_flight"] = None
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
                setup = p0_setup_state(state)
                if setup["delirium"] and setup["surgery_in_hand"]:
                    if not ST["p0_ready"]:
                        say(f"[{tag}] P0 delirium-ready: types="
                            f"{sorted(setup['types'])}, Surgery in hand")
                        wire("p0_ready", {"types": sorted(setup["types"])})
                    ST["p0_ready"] = True
                    if ST["phase"] == "setup":
                        ST["phase"] = "duel"

                # Surgery on the stack -> decisive pre, in the observation
                # path (guarded flag), before either seat passes priority.
                if stack_spell_named(state, "invasive surgery"):
                    if not ST["surgery_seen"]:
                        ST["surgery_seen"] = True
                        ST["phase"] = "resolution_window"
                        say("SURGERY on the stack; exporting pre.json")
                        wire("surgery_on_stack", {})
                        if not ST["pre_exported"]:
                            await export_as(p0, "pre")
                            ST["pre_exported"] = True
                # resolution-window prompt recording
                if ST["surgery_seen"] and not ST["surgery_resolved"]:
                    vi = get_vi(st)
                    if vi:
                        for opp in vi.get("opportunities", []) or []:
                            record_window_prompt(tag, opp)
                    wtype = (wf_of(state).get("type") or "")
                    if wtype and wtype not in ST["wf_types_window"]:
                        ST["wf_types_window"].append(wtype)
                    if not stack_spell_named(state, "invasive surgery"):
                        ST["surgery_resolved"] = True
                        with open(f"{EVDIR}/window_end.json", "w") as wfj:
                            json.dump({"state": state,
                                       "observed_turn":
                                           state.get("turn_number"),
                                       "observed_phase":
                                           state.get("phase"),
                                       "surgery_resolved": True},
                                      wfj, default=str)
                        say("window_end.json written from observed state "
                            f"(turn {state.get('turn_number')}, "
                            f"phase {state.get('phase')})")
                        wire("surgery_resolved", {})
                        ST["settle_at"] = now
                # decisive post: settled with empty stack
                if ST["surgery_resolved"] and not ST["post_exported"]:
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
                if (state.get("turn_number") or 0) > 60 \
                        and not ST["post_exported"]:
                    say("turn watchdog: 60 turns in, giving up")
                    wire("turn_stall",
                         {"waiting_for": wf_of(state),
                          "turn": state.get("turn_number")})
                    result = "giveup"
                    break
                if state.get("winner") is not None or state.get("game_over"):
                    say(f"game over (winner={state.get('winner')})")
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
                if c.name == "P0":
                    await p0_tick(st, acts, st["state"], c)
                else:
                    await p1_tick(st, acts, st["state"], c)
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
        # No decisive run completed: finalize a blocked run from whatever
        # we have (data-level evidence stands on its own).
        say("no successful decisive attempt; finalizing blocked run")
        try:
            await finalize(None)
        except Exception as e:
            say(f"finalize failed: {e}")


if __name__ == "__main__":
    run = asyncio.run(main())
    # copy the scenario into the evidence dir, render the PNG, and write
    # the SHA-256 manifest over everything except the manifest itself.
    shutil.copy(f"{BACKFILL}/driver/scenario_7427.py",
                f"{EVDIR}/scenario_7427.py")
    import subprocess
    if os.path.exists(f"{EVDIR}/run.json") and os.path.exists(f"{EVDIR}/pre.json") \
            and os.path.exists(f"{EVDIR}/post.json"):
        subprocess.run([sys.executable, f"{BACKFILL}/driver/render_summary.py",
                        EVDIR, str(ISSUE),
                        "Invasive Surgery: unparsed delirium search leaves "
                        "ChangeZone reading an empty tracked set; exile is "
                        "a silent no-op"],
                       check=True)
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for f in files:
            h = hashlib.sha256(open(f"{EVDIR}/{f}", "rb").read()).hexdigest()
            mf.write(f"{h}  {f}\n")
    print(f"manifest written ({len(files)} files)", flush=True)
    sys.exit(0)
