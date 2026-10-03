#!/usr/bin/env python3
"""Issue #7435: Pick the Brain -- the delirium multi-zone same-name search is
unparsed, so `ChangeZone` reads an empty tracked set.

Reported (2026-08-15, source:internal-triage, status:confirmed,
area:parser, mechanic:search, mechanic:zone-change, priority:p3-card-specific,
classifier:unsupported-aspect; related #6857 tracked-set publish census, one
of 33 cards whose tracked-set antecedent clause never parses):

> Target opponent reveals their hand. You choose a nonland card from it and
> exile that card.
> Delirium -- If there are four or more card types among cards in your
> graveyard, search that player's graveyard, hand, and library for any number
> of cards with the same name as the exiled card, exile those cards, then that
> player shuffles.

Pinned v0.100.0 parse (see data_evidence.json): abilities[1] (Spell,
delirium QuantityCheck DistinctCardTypes >= 4):
  head = Unimplemented { name: "unparsed_verb_arguments",
                         description: "search that player's graveyard, hand,
                                       and library for any number of cards
                                       with the same name as the exiled card" }
    sub_ability (Spell): ChangeZone { destination Exile,
                                     target TrackedSet(0) }
      sub_sub_ability (Spell): Shuffle { target ParentTargetController },
                               same delirium condition.
abilities[0] (the reveal + "choose a nonland card" exile) is parsed:
RevealHand { target opponent } + sub ChangeZone ParentTarget -> Exile.

The Unimplemented resolver is a runtime no-op (pushes no GameEvent), so the
publish authority allocates a FRESH EMPTY tracked set and the ChangeZone
sub-ability exiles nothing.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 12x Pick the Brain + 4x Lightning Bolt + 4x Ornithopter + 20x Swamp
      + 20x Mountain (60)
  P1: 20x Lightning Bolt + 40x Mountain (60; plays lands, never casts,
      never attacks)
Drive (fully P0-driven setup; P1 only plays lands):
  1. Mulligans: both seats keep 7.
  2. Stage "thopter": P0 casts Ornithopter ({0}).
  3. Stage "bolt": P0 casts Lightning Bolt ({R}) targeting its own
     Ornithopter. Thopter dies -> P0 graveyard holds Instant + Artifact +
     Creature (3 types).
  4. Stage "brain1": P0 casts Pick the Brain ({2}{B}) targeting P1 (delirium
     NOT yet active: 3 types). P0 chooses a Lightning Bolt from P1's revealed
     hand; it is exiled. Brain resolves -> P0 graveyard gains Sorcery
     (4 types -> delirium active).
  5. Stage "brain2" (decisive): pre.json exported INSIDE the cast path
     (guarded flag), then P0 casts Pick the Brain #2 targeting P1. P0 again
     chooses a Lightning Bolt from the revealed hand (first clause works:
     the card is exiled). Delirium is active, so the search clause SHOULD
     find every other same-named card in P1's graveyard/hand/library and
     exile them -- under the bug the search head is an Unimplemented no-op,
     the tracked set is empty, and NOTHING further is exiled.
  6. Export mid_stack.json when Brain #2 is first seen on the stack; export
     post.json once settled (stack empty, Priority, no pending choice, 8s
     idle).

Expected (correct behavior): after Brain #2 resolves, every remaining
Lightning Bolt in P1's graveyard, hand, and library (same name as the
exiled chosen card) is exiled (>= 1 such card moves).
Reported (bug): the search clause never parsed (Unimplemented head, runtime
no-op), so 0 further cards are exiled -- the ChangeZone sub-ability reads
an empty tracked set.

Assertions:
  A1_data_level   pinned v0.100.0 card-data.json parses Pick the Brain as
                  the issue reports: abilities[1] head Unimplemented naming
                  the multi-zone search clause; sub Spell ChangeZone
                  { Exile, TrackedSet(0) }; sub-sub Shuffle with the
                  delirium QuantityCheck (DistinctCardTypes >= 4);
                  abilities[0] parsed (RevealHand + ChangeZone ParentTarget).
  A2_setup_ok     pre.json: P0 graveyard has >= 4 distinct card types
                  (expect Instant/Artifact/Creature/Sorcery); P1 has >= 1
                  Lightning Bolt across hand+library+graveyard; P0 main
                  phase, active P0, stack empty.
  A3_cast_resolved
                  Brain #2 was cast (mana paid), its spell object was seen
                  on the stack, and it left the stack (resolved).
  A4_search_exile count of P1-owned Lightning Bolt objects that moved
                  Library/Graveyard/Hand -> Exile in the decisive window,
                  EXCLUDING the card exiled by the first clause. Passes iff
                  >= 1 (correct behavior). EXPECTED TO FAIL (0) under the
                  bug -- the Unimplemented head pushed no GameEvent, so the
                  tracked set was allocated empty and the ChangeZone
                  sub-ability exiled nothing.
  A5_first_clause the card P0 chose from P1's revealed hand for Brain #2 is
                  in Exile at post (the parsed first clause works; only the
                  unparsed search fails).
  A6_cleanup      post stack empty, game advanced past the cast turn.

Verdict rule: reproduced iff A1, A2, A3, A5 passed and A4 failed (0 further
              exiled despite same-name copies available);
              not-reproduced iff A1..A6 all passed;
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
RUN_ID = "20261003-0541b-7435"  # run 4: run 3's brain2 cast + choices went
# through but brain_on_stack() never matched (v0.100.0 Spell stack entries
# carry source_id, not the card-name string), so settle/GameOver bookkeeping
# deadlocked and the game marched 100+ turns to GameOver. This run keys the
# window off ST["brain2_oid"]->source_id and finalizes on GameOver.
ISSUE = 7435
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
              "latest stable at ~02:45 CDT 2026-10-03); hashes recomputed "
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

BRAIN = "pick the brain"
BOLT = "lightning bolt"
THOPTER = "ornithopter"
BASICS = {"swamp", "mountain", "forest", "island", "plains"}

P0_DECK = [("Pick the Brain", 12), ("Lightning Bolt", 4), ("Ornithopter", 4),
           ("Swamp", 20), ("Mountain", 20)]
P1_DECK = [("Lightning Bolt", 20), ("Mountain", 40)]


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
        "stage": "thopter",  # thopter -> bolt -> brain1 -> delirium -> brain2
        "thopter_cast": False,
        "bolt_cast": False,
        "brain1_cast": False,
        "brain2_cast": False,
        "brain2_on_stack": False,
        "brain2_resolved": False,
        "brain2_turn": None,
        "pre_exported": False,
        "mid_exported": False,
        "post_exported": False,
        "pending_target_kind": None,
        "cast_in_flight": None,
        "choices": [],  # (brain_tag, oid, name)
        "search_prompt_seen": False,
        "search_choice_answered": False,
        "pre_p1_bolts": None,   # {zone: [oids]} at pre
        "pre_p0_gy_types": None,
        "chosen_oid": None,     # brain2's first-clause choice
        "mana_needs": {"P0": {"B": 0, "R": 0, "generic": 0},
                       "P1": {"B": 0, "R": 0, "generic": 0}},
        "prompts_seen": [],
        "wf_types_window": [],
        "terminal": False,
        "states_seen": 0,
        "cast_rejections": 0,
        "stack_dumped": False,
        "settle_at": None,
        "resolving_since": None,
        "ass": {k: "not-run" for k in ("A1_data_level", "A2_setup_ok",
                                       "A3_cast_resolved", "A4_search_exile",
                                       "A5_first_clause", "A6_cleanup")},
        "notes": [],
        "data_level_ok": False,
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


def gy_ids(state, pid):
    return [str(x) for x in player_of(state, pid).get("graveyard", [])]


def is_land(o):
    return "land" in [str(t).lower()
                      for t in (o.get("card_types") or {}).get("core_types", [])]


def core_types_of(o):
    return [str(t) for t in (o.get("card_types") or {}).get("core_types", [])]


def gy_types(state, pid):
    types = set()
    for oid in gy_ids(state, pid):
        for t in core_types_of(get_obj(state, oid)):
            types.add(t)
    return types


def bf_permanents(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def thopter_oid(state, pid=0):
    for oid in bf_permanents(state, pid):
        if obj_lname(state, oid) == THOPTER:
            return oid
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


def brain_on_stack(state):
    # v0.100.0 Spell stack entries carry source_id/card_id, not the card
    # name string, so string-sniffing never matched. Track the decisive
    # cast's own object id instead.
    want = ST.get("brain2_oid")
    for se in stack_entries(state):
        if want is not None and se.get("source_id") == want:
            return se
    return None


def p1_bolts_by_zone(state):
    zones = {"Hand": [], "Library": [], "Graveyard": [], "Exile": [],
             "Battlefield": [], "other": []}
    for oid, o in (state.get("objects") or {}).items():
        if o.get("owner") != 1:
            continue
        if str(o.get("base_name") or o.get("name") or "").lower() != BOLT:
            continue
        z = o.get("zone") or "other"
        zones.setdefault(z, zones["other"]).append(str(oid))
    return zones


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
    card = CARD_DATA.get(BRAIN, {})
    abilities = card.get("abilities") or []
    a0 = abilities[0] if len(abilities) > 0 else {}
    a1 = abilities[1] if len(abilities) > 1 else {}
    head = a1.get("effect") or {}
    sub = (a1.get("sub_ability") or {})
    sub_eff = sub.get("effect") or {}
    subsub = (sub.get("sub_ability") or {})
    subsub_eff = subsub.get("effect") or {}
    cond = a1.get("condition") or {}
    lhs = (cond.get("lhs") or {})
    qty = (lhs.get("qty") or {})
    a0eff = a0.get("effect") or {}
    a0sub = ((a0.get("sub_ability") or {}).get("effect") or {})
    ev = {
        "name": card.get("name"),
        "oracle": card.get("oracle_text"),
        "card_type": card.get("card_type"),
        "mana_cost": card.get("mana_cost"),
        "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
        "ability0": a0,
        "ability1": a1,
    }
    ok = (
        len(abilities) >= 2
        and head.get("type") == "Unimplemented"
        and "search that player's graveyard, hand, and library" in
        str(head.get("description", "")).lower()
        and "same name as the exiled card" in
        str(head.get("description", "")).lower()
        and (sub.get("kind") == "Spell")
        and sub_eff.get("type") == "ChangeZone"
        and sub_eff.get("destination") == "Exile"
        and (sub_eff.get("target") or {}).get("type") == "TrackedSet"
        and (sub_eff.get("target") or {}).get("id") == 0
        and subsub_eff.get("type") == "Shuffle"
        and (cond.get("type") == "QuantityCheck"
             and qty.get("type") == "DistinctCardTypes"
             and cond.get("comparator") == "GE"
             and (cond.get("rhs") or {}).get("value") == 4)
        and a0eff.get("type") == "RevealHand"
        and a0sub.get("type") == "ChangeZone"
        and a0sub.get("destination") == "Exile"
        and (card.get("mana_cost") or {}).get("generic") == 2
        and (card.get("mana_cost") or {}).get("shards") == ["Black"]
    )
    say(f"data-level check: a1 head={head.get('type')}/{head.get('name')}; "
        f"sub={sub_eff.get('type')}/{(sub_eff.get('target') or {})}; "
        f"subsub={subsub_eff.get('type')}; "
        f"cond=DistinctCardTypes GE {(cond.get('rhs') or {}).get('value')}; "
        f"a0={a0eff.get('type')}+{a0sub.get('type')}; "
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


def p0_discard_pick(state, pid):
    """Return oids to discard to reach 7, or []. Keep rules: lands while
    mana-light, 1 thopter until cast, 1 bolt until cast, 2 brains."""
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return []
    lands_bf = sum(1 for oid in bf_permanents(state, pid)
                   if is_land(get_obj(state, oid)))
    lands_hand = sum(1 for o in hand if is_land(get_obj(state, o)))
    brains = [o for o in hand if obj_lname(state, o) == BRAIN]
    bolts = [o for o in hand if obj_lname(state, o) == BOLT]
    thopters = [o for o in hand if obj_lname(state, o) == THOPTER]

    def rank(o):
        nm = obj_lname(state, o)
        if nm in BASICS and lands_bf + lands_hand > 9:
            return (0, o)
        if nm == BRAIN and len(brains) > 2:
            return (1, o)
        if nm == BOLT and (ST["bolt_cast"] or len(bolts) > 1):
            return (2, o)
        if nm == THOPTER and (ST["thopter_cast"] or len(thopters) > 1):
            return (3, o)
        if nm in BASICS:
            return (4, o)
        return (5, o)
    ranked = sorted((rank(o) for o in hand))
    return [int(o) for _, o in ranked[:n] if _ < 5]


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
        order = sorted(hand, key=lambda o: (
            0 if obj_lname(state, o) in BASICS
            else (1 if obj_lname(state, o) == BOLT else 2), o))
        picks = [int(x) for x in order[:n]]
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
            # P1: prefer discarding a basic land, else first
            for ch in chs:
                k, ref = _cand_ref(ch)
                if obj_lname(state, ref) in BASICS:
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

# ------------------------------------------------------------- prompt handlers
async def answer_pending_target(c, st, state, tag):
    """Answer the pending target-selection for an in-flight cast."""
    kind = ST.get("pending_target_kind")
    if not kind:
        return False
    # debug: rate-limited full dump of the prompt surface while we wait
    now = time.time()
    if now - ST.get("_tgt_dbg_at", 0) > 10:
        ST["_tgt_dbg_at"] = now
        vi = st.get("viewer_interaction") or {}
        dbg = {
            "kind": kind,
            "waiting_for": wf_of(state),
            "vi_canSubmit": vi.get("canSubmit"),
            "vi_keys": list(vi.keys()),
            "opportunities": vi.get("opportunities"),
            "legal_action_types": sorted(set(
                a.get("type") for a in merged_actions(st))),
        }
        with open(f"{EVDIR}/target_debug.jsonl", "a") as f:
            f.write(json.dumps(dbg, default=str)[:6000] + "\n")
        say(f"[{tag}] target-debug dumped for kind={kind} "
            f"(wf={wf_of(state).get('type')}, "
            f"n_opps={len(vi.get('opportunities') or [])})")
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        rtype = resp.get("type")
        data = resp.get("data") or {}
        spec = data.get("spec") or {}
        if rtype == "exactChoices":
            chs = data.get("choices") or []
            spec_type = "choose"
        elif rtype == "schema" and spec.get("type") in ("select", "sequence"):
            chs = data.get("candidates") or data.get("choices") or []
            spec_type = spec.get("type")
        else:
            continue
        if not chs:
            continue
        pick = None
        why = None
        want_player = kind in ("brain_target_p1", "brain2_target_p1")
        if kind == "bolt_own_thopter" and rtype == "schema":
            want = thopter_oid(state, 0)
            for ch in chs:
                k, ref = _cand_ref(ch)
                if k == "object" and ref == want:
                    pick, why = ch.get("id"), f"thopter {want}"
                    break
        elif want_player:
            for ch in chs:
                k, ref = _cand_ref(ch)
                if k == "player" and ref == 1:
                    pick, why = ch.get("id"), "player 1"
                    break
            if pick is None:
                for ch in chs:
                    blob = json.dumps(ch, default=str).lower()
                    if "player 1" in blob or '"player":1' in blob.replace(" ", "") \
                            or "opponent" in blob:
                        pick, why = ch.get("id"), "player 1 (blob)"
                        break
        if not pick:
            wire("target_unmatched", {"who": tag, "kind": kind,
                                      "iid": opp.get("interactionId") or opp.get("id"),
                                      "rtype": rtype,
                                      "spec": str(spec)[:200],
                                      "n_candidates": len(chs),
                                      "blob": json.dumps(chs, default=str)[:600]})
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        SUBMITTED_OPPS.add(iid)
        say(f"[{tag}] answering {kind} target: {why}")
        wire("target_submit", {"who": tag, "kind": kind, "iid": iid,
                               "why": why, "rtype": rtype,
                               "spec_type": spec_type})
        if rtype == "exactChoices":
            sub_resp = {"type": "choose", "data": {"choiceId": pick}}
        else:
            sub_resp = {"type": spec_type, "data": {"choiceIds": [pick]}}
        await interact_as(c, {
            "interactionId": iid,
            "response": sub_resp}, tag)
        ST["pending_target_kind"] = None
        return True
    return False


def _choice_is_hand_pick(opp, state):
    """Heuristic: opportunity offering P1-hand object candidates."""
    resp = opp.get("response") or {}
    if resp.get("type") != "schema":
        return False
    data = resp.get("data") or {}
    spec = data.get("spec") or {}
    if spec.get("type") not in ("select", "sequence"):
        return False
    chs = data.get("candidates") or []
    if not chs:
        return False
    refs = []
    for ch in chs:
        k, ref = _cand_ref(ch)
        if k != "object":
            return False
        refs.append(ref)
    # all candidates are objects; treat as the revealed-hand choice when
    # every candidate currently sits in P1's hand (authoritative zones).
    p1_hand = set(hand_ids(state, 1))
    if p1_hand and all(r in p1_hand for r in refs):
        return True
    # fallback: P0's live view may hide P1's hand; accept when none of the
    # candidates is on a battlefield / in a library / in exile.
    zones = set()
    for r in refs:
        o = get_obj(state, r)
        zones.add(o.get("zone"))
    if zones <= {"Hand", None, "Unknown"}:
        blob = json.dumps(opp, default=str).lower()
        if "hand" in blob or "reveal" in blob or "nonland" in blob:
            return True
    return False


async def answer_hand_choice(c, st, state, tag):
    """P0 answers 'choose a nonland card from the revealed hand' (Pick the
    Brain's first clause). Two shapes observed: schema select/sequence with
    object candidates, and exactChoices with selectCards action codes +
    object surfaces (waiting_for RevealChoice). Prefer a Lightning Bolt;
    never pick a land."""
    if tag != "P0":
        return False
    for opp in vi_ops(st):
        resp = opp.get("response") or {}
        rtype = resp.get("type")
        data = resp.get("data") or {}
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        pick, pick_nm, pick_ref, sub = None, None, None, None
        if rtype == "schema":
            if not _choice_is_hand_pick(opp, state):
                continue
            spec = data.get("spec") or {}
            chs = data.get("candidates") or []
            refs = []
            for ch in chs:
                k, ref = _cand_ref(ch)
                if k == "object":
                    refs.append((ch, ref))
            if not refs:
                continue
            for ch, ref in refs:  # prefer Lightning Bolt, never a land
                nm = obj_lname(state, ref)
                if nm == BOLT:
                    pick, pick_nm, pick_ref = ch.get("id"), nm, ref
                    break
            if pick is None:
                for ch, ref in refs:
                    nm = obj_lname(state, ref)
                    if nm not in BASICS and "land" not in nm:
                        pick, pick_nm, pick_ref = ch.get("id"), nm, ref
                        break
            if pick is None:
                wire("hand_choice_no_nonland",
                     {"who": tag, "iid": iid,
                      "candidates": [obj_lname(state, r)
                                     for _, r in refs]})
                continue
            sub = {"interactionId": iid,
                   "response": {"type": spec.get("type"),
                                "data": {"choiceIds": [pick]}}}
        elif rtype == "exactChoices":
            cands = []
            for ch in data.get("choices") or []:
                codes = [s.get("data", {}).get("code")
                         for s in ch.get("surfaces", []) or []
                         if s.get("type") == "action"]
                if "selectCards" not in codes:
                    continue
                for sf in ch.get("surfaces", []) or []:
                    if sf.get("type") != "object":
                        continue
                    d2 = sf.get("data") or {}
                    if d2.get("zone") == "hand" and \
                            str(d2.get("controller")) == "1":
                        try:
                            ref = int(d2.get("reference"))
                        except (TypeError, ValueError):
                            ref = None
                        cands.append((ch.get("id"),
                                      str(d2.get("name", "")).lower(), ref))
            if not cands:
                continue
            for cid, nm, ref in cands:
                if nm == BOLT:
                    pick, pick_nm, pick_ref = cid, nm, ref
                    break
            if pick is None:
                for cid, nm, ref in cands:
                    if "land" not in nm:
                        pick, pick_nm, pick_ref = cid, nm, ref
                        break
            if pick is None:
                wire("hand_choice_no_nonland",
                     {"who": tag, "iid": iid,
                      "candidates": [nm for _, nm, _ in cands]})
                continue
            sub = {"interactionId": iid,
                   "response": {"type": "choose",
                                "data": {"choiceId": pick}}}
        else:
            continue
        brain_tag = "brain2" if ST["brain2_cast"] else "brain1"
        SUBMITTED_OPPS.add(iid)
        ST["choices"].append((brain_tag, pick_ref, pick_nm))
        if brain_tag == "brain2":
            ST["chosen_oid"] = pick_ref
        say(f"[{tag}] {brain_tag} hand-choice ({rtype}): exiling {pick_nm} "
            f"(oid {pick_ref})")
        wire("hand_choice", {"who": tag, "brain": brain_tag, "iid": iid,
                             "rtype": rtype, "pick": pick, "name": pick_nm,
                             "oid": pick_ref})
        await interact_as(c, sub, tag)
        return True
    return False


async def answer_search_choice(c, st, state, tag):
    """Safety net for the fixed path: if the engine DOES present the
    delirium search selection (candidates spanning P1's zones), select all
    Lightning Bolt candidates. Under the bug this never fires."""
    if tag != "P0" or not ST["brain2_cast"]:
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
        zones = set()
        bolt_picks = []
        for ch in chs:
            k, ref = _cand_ref(ch)
            if k != "object":
                continue
            o = get_obj(state, ref)
            zones.add(o.get("zone"))
            if obj_lname(state, ref) == BOLT:
                bolt_picks.append(ch.get("id"))
        if not (zones & {"Library", "Graveyard"}):
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        ST["search_prompt_seen"] = True
        if not bolt_picks:
            wire("search_prompt_no_bolts", {"who": tag, "iid": iid,
                                            "zones": sorted(zones)})
            continue
        SUBMITTED_OPPS.add(iid)
        ST["search_choice_answered"] = True
        say(f"[{tag}] UNEXPECTED (fixed path?): search selection offering "
            f"{len(bolt_picks)} bolt candidates; selecting all")
        wire("search_choice", {"who": tag, "iid": iid,
                               "picks": bolt_picks})
        await interact_as(c, {
            "interactionId": iid,
            "response": {"type": spec.get("type"),
                         "data": {"choiceIds": bolt_picks}}}, tag)
        return True
    return False


def record_window_prompt(seat, st):
    for opp in vi_ops(st):
        iid = opp.get("interactionId") or opp.get("id")
        if all(t[2] != iid for t in ST["prompts_seen"]):
            ST["prompts_seen"].append((ST["phase"], seat, iid, opp))
            wire("window_prompt", {"phase": ST["phase"], "seat": seat,
                                   "iid": iid, "opportunity": opp})
            say(f"[{ST['phase']}/{seat}] prompt: "
                f"{json.dumps(opp)[:260]}")


def hand_choice_pending(st, state):
    for opp in vi_ops(st):
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        resp = opp.get("response") or {}
        rtype = resp.get("type")
        if rtype == "schema" and _choice_is_hand_pick(opp, state):
            return True
        if rtype == "exactChoices":
            data = resp.get("data") or {}
            for ch in data.get("choices") or []:
                codes = [s.get("data", {}).get("code")
                         for s in ch.get("surfaces", []) or []
                         if s.get("type") == "action"]
                if "selectCards" not in codes:
                    continue
                for sf in ch.get("surfaces", []) or []:
                    if sf.get("type") != "object":
                        continue
                    d2 = sf.get("data") or {}
                    if d2.get("zone") == "hand" and \
                            str(d2.get("controller")) == "1":
                        return True
    return False

# ------------------------------------------------------------- P0 tick
async def cast_brain(c, state, acts, tag, decisive):
    hn = hand_lnames(state, tag == "P1" and 1 or 0)
    if BRAIN not in hn:
        return False
    pool = mana_color_pool(state, 0)
    lands = len(untapped_lands(state, 0))
    if pool["B"] < 1 or lands < 3:
        return False
    a, oid = cast_action_for(acts, state, BRAIN)
    if not a:
        return False
    if decisive and not ST["pre_exported"]:
        await export_as(c, "pre")
        ST["pre_exported"] = True
        pre_st = json.load(open(f"{EVDIR}/pre.json")).get("state", {})
        ST["pre_p1_bolts"] = p1_bolts_by_zone(pre_st)
        ST["pre_p0_gy_types"] = sorted(gy_types(pre_st, 0))
        say(f"[arm] pre.json exported: P0 GY types={ST['pre_p0_gy_types']}; "
            f"P1 bolts by zone=" +
            json.dumps({k: len(v) for k, v in ST["pre_p1_bolts"].items()}))
        wire("pre_exported", {"gy_types": ST["pre_p0_gy_types"],
                              "p1_bolts": {k: len(v)
                                           for k, v in ST["pre_p1_bolts"].items()}})
    ST["pending_target_kind"] = None  # Brain has no cast-time target prompt:
    # "target opponent" is a Typed ability target resolved at resolution.
    ST["cast_in_flight"] = oid
    ST["mana_needs"]["P0"] = {"B": 1, "R": 0, "generic": 2}
    if decisive:
        ST["brain2_cast"] = True
        ST["brain2_turn"] = state.get("turn_number")
        ST["brain2_oid"] = oid  # track the spell object itself; the
        # string-sniffing brain_on_stack() never matched the v0.100.0
        # stack serialization, so window tracking keys off this oid's
        # zone (Hand -> Stack -> Graveyard) instead.
        ST["phase"] = "resolving"
        ST["resolving_since"] = time.time()
    else:
        ST["brain1_cast"] = True
    say(f"[P0] casting Pick the Brain ({'DECISIVE' if decisive else 'setup'}; "
        f"oid {oid})")
    wire("brain_cast", {"decisive": decisive, "oid": oid})
    await submit_as_is(c, a)
    return True


async def p0_tick(st, acts, state, c):
    pid = c.player_id
    if ST["phase"] == "resolving":
        record_window_prompt("P0", st)
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan(c, pid, "P0"):
            return
        return
    if await do_discard_to_handsize(c, pid, "P0"):
        return
    if await do_discard_vi(c, pid, "P0"):
        return
    if await answer_pending_target(c, st, state, "P0"):
        return
    if await answer_hand_choice(c, st, state, "P0"):
        return
    if await answer_search_choice(c, st, state, "P0"):
        return
    # Payment-complete detector (payment_mode Auto): the engine auto-taps
    # lands, so driver-side mana_needs can desync. Once the in-flight cast
    # has left the hand (on the stack or beyond), the engine has taken its
    # payment: clear the owed mana and any pending target kind.
    cif = ST.get("cast_in_flight")
    if cif is not None:
        co = get_obj(state, cif)
        if co.get("zone") != "Hand":
            ST["cast_in_flight"] = None
            ST["mana_needs"]["P0"] = {"B": 0, "R": 0, "generic": 0}
            ST["pending_target_kind"] = None
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
        gtypes = gy_types(state, pid)
        # stage machine: thopter -> bolt -> brain1 -> delirium -> brain2
        if ST["stage"] == "thopter":
            if thopter_oid(state, pid) is not None \
                    or "Artifact" in gtypes:
                ST["stage"] = "bolt"
                say("[P0] stage -> bolt")
            elif THOPTER in hn:
                a, oid = cast_action_for(acts, state, THOPTER)
                if a:
                    ST["thopter_cast"] = True
                    ST["cast_in_flight"] = oid
                    say(f"[P0] casting Ornithopter (oid {oid})")
                    wire("thopter_cast", {"oid": oid})
                    await submit_as_is(c, a)
                    return
        if ST["stage"] == "bolt":
            if "Instant" in gtypes:
                ST["stage"] = "brain1"
                ST["bolt_cast"] = True
                say("[P0] stage -> brain1")
            elif BOLT in hn and thopter_oid(state, pid) is not None:
                pool = mana_color_pool(state, pid)
                if pool["R"] >= 1:
                    a, oid = cast_action_for(acts, state, BOLT)
                    if a:
                        ST["pending_target_kind"] = "bolt_own_thopter"
                        ST["cast_in_flight"] = oid
                        ST["mana_needs"]["P0"] = {"B": 0, "R": 1,
                                                  "generic": 0}
                        say(f"[P0] casting Lightning Bolt at own "
                            f"Ornithopter (oid {oid})")
                        wire("bolt_cast", {"oid": oid,
                                           "target": "own thopter"})
                        await submit_as_is(c, a)
                        return
        if ST["stage"] == "brain1":
            if "Sorcery" in gtypes:
                ST["stage"] = "delirium"
                say("[P0] stage -> delirium")
            elif await cast_brain(c, state, acts, "P0", decisive=False):
                return
        if ST["stage"] == "delirium":
            if len(gtypes) >= 4:
                ST["stage"] = "brain2"
                say(f"[P0] delirium active ({sorted(gtypes)}); "
                    f"stage -> brain2")
            else:
                say(f"[P0] waiting for delirium: {sorted(gtypes)}")
        if ST["stage"] == "brain2":
            if await cast_brain(c, state, acts, "P0", decisive=True):
                return
        # play a land
        n_lands = sum(1 for oid in bf_permanents(state, pid)
                      if is_land(get_obj(state, oid)))
        if n_lands < 10:
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
    post_st = post.get("state") or {}

    # A1: data-level parse matches the issue
    if ST.get("data_level_ok"):
        ass["A1_data_level"] = "passed"
        notes.append("A1 passed: pinned v0.100.0 card-data.json parses "
                     "Pick the Brain as the issue reports -- abilities[1] "
                     "head Unimplemented ('unparsed_verb_arguments': "
                     "'search that player's graveyard, hand, and library "
                     "for any number of cards with the same name as the "
                     "exiled card') + sub Spell ChangeZone { Exile, "
                     "TrackedSet(0) } + sub-sub Shuffle, all under the "
                     "delirium QuantityCheck (DistinctCardTypes >= 4); "
                     "abilities[0] parsed (RevealHand + ChangeZone "
                     "ParentTarget -> Exile); mana {2}{B}; see "
                     "data_evidence.json.")
    else:
        ass["A1_data_level"] = "failed"
        notes.append("A1 FAILED: the pinned parse does not match the "
                     "issue-reported shape; see data_evidence.json.")

    # A2: setup at the decisive pre
    if ST["pre_exported"] and pre_st:
        gtypes = sorted(gy_types(pre_st, 0))
        bolts = p1_bolts_by_zone(pre_st)
        n_avail = sum(len(bolts[z]) for z in ("Hand", "Library", "Graveyard"))
        ph = pre_st.get("phase")
        act = pre_st.get("active_player")
        notes.append(f"A2 probe: pre P0 GY types={gtypes}; P1 bolts "
                     f"hand/lib/gy={n_avail} "
                     f"({ {k: len(v) for k, v in bolts.items()} }); "
                     f"phase={ph}; active={act}; stack="
                     f"{len(stack_entries(pre_st))}.")
        if len(gtypes) >= 4 and n_avail >= 1 \
                and ph in ("PreCombatMain", "PostCombatMain") and act == 0 \
                and not stack_entries(pre_st):
            ass["A2_setup_ok"] = "passed"
            notes.append("A2 passed: delirium live at the decisive pre "
                         f"({len(gtypes)} card types in P0's graveyard: "
                         f"{gtypes}), P1 holds >= 1 Lightning Bolt across "
                         "hand/library/graveyard, P0 main phase, stack "
                         "empty.")
        else:
            ass["A2_setup_ok"] = "failed"
            notes.append("A2 FAILED: the decisive pre does not show the "
                         "required setup (delirium and/or bolt availability "
                         "and/or phase).")
    else:
        ass["A2_setup_ok"] = "failed"
        notes.append("A2 FAILED: pre.json was never exported (the decisive "
                     "cast window never armed).")

    # A3: Brain #2 was cast and resolved
    if ST["brain2_cast"]:
        notes.append(f"A3 probe: brain2_cast={ST['brain2_cast']}; "
                     f"on_stack_seen={ST['brain2_on_stack']}; "
                     f"resolved={ST['brain2_resolved']}; "
                     f"turn={ST['brain2_turn']}; choices="
                     f"{ST['choices']}.")
        if ST["brain2_on_stack"] and ST["brain2_resolved"]:
            ass["A3_cast_resolved"] = "passed"
            notes.append("A3 passed: Pick the Brain #2 was cast, its spell "
                         "object was observed on the stack, and it left the "
                         "stack (resolved).")
        else:
            ass["A3_cast_resolved"] = "failed"
            notes.append("A3 FAILED: Brain #2 was submitted but its "
                         "stack presence / resolution could not be "
                         "confirmed.")
    else:
        ass["A3_cast_resolved"] = "failed"
        notes.append("A3 FAILED: Pick the Brain #2 was never cast.")

    # A4: did the delirium search exile any further same-name cards?
    search_exiled = 0
    search_exiled_names = []
    chosen = ST.get("chosen_oid")
    pre_bolts = ST.get("pre_p1_bolts") or {}
    avail_oids = set()
    for z in ("Hand", "Library", "Graveyard"):
        avail_oids.update(pre_bolts.get(z, []))
    if chosen and str(chosen) in avail_oids:
        avail_oids.discard(str(chosen))
    if post_st and avail_oids:
        for oid in avail_oids:
            o = (post_st.get("objects") or {}).get(str(oid), {})
            if o.get("zone") == "Exile":
                search_exiled += 1
                search_exiled_names.append(
                    str(o.get("base_name") or o.get("name") or "?"))
    ST["search_exiled"] = search_exiled
    notes.append(f"A4 probe: {search_exiled} of {len(avail_oids)} available "
                 f"P1 Lightning Bolts (excl. the first-clause choice) in "
                 f"Exile at post {search_exiled_names or ''}; "
                 f"search_prompt_seen={ST['search_prompt_seen']}; "
                 f"search_choice_answered={ST['search_choice_answered']}.")
    if search_exiled >= 1 and ST["brain2_resolved"]:
        ass["A4_search_exile"] = "passed"
        notes.append("A4 passed: the delirium search exiled >= 1 further "
                     "same-name card.")
    elif ST["brain2_resolved"]:
        ass["A4_search_exile"] = "failed"
        notes.append("A4 FAILED: 0 further P1 Lightning Bolts exiled by the "
                     "delirium search -- THE REPORTED BUG: the 'search that "
                     "player's graveyard, hand, and library for any number "
                     "of cards with the same name as the exiled card' clause "
                     "never parsed (Unimplemented head, a runtime no-op), so "
                     "the chain tracked set was allocated empty and the "
                     "ChangeZone sub-ability exiled nothing.")
    else:
        notes.append("A4 not-run: the decisive resolution never completed.")

    # A5: the first clause (parsed) still exiled the chosen card
    if chosen and post_st:
        co = (post_st.get("objects") or {}).get(str(chosen), {})
        if co.get("zone") == "Exile":
            ass["A5_first_clause"] = "passed"
            notes.append(f"A5 passed: the chosen card (oid {chosen}, "
                         f"{co.get('base_name')}) is in Exile at post -- "
                         "the parsed first clause works; only the unparsed "
                         "search fails.")
        else:
            ass["A5_first_clause"] = "failed"
            notes.append(f"A5 FAILED: the chosen card (oid {chosen}) is in "
                         f"zone {co.get('zone')} at post, not Exile.")
    else:
        notes.append("A5 not-run: no Brain #2 choice was recorded or no "
                     "post state.")

    # A6: cleanup
    if post_st:
        tnum = ST.get("brain2_turn")
        cur_turn = post_st.get("turn_number")
        if not stack_entries(post_st) and (
                tnum is None or (cur_turn or 0) > tnum):
            ass["A6_cleanup"] = "passed"
            notes.append("A6 passed: post stack empty, game advanced past "
                         f"the cast turn (cast turn {tnum}, post turn "
                         f"{cur_turn}, phase {post_st.get('phase')}).")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append("A6 FAILED: post stack non-empty or the game did "
                         "not advance past the cast turn.")
    else:
        notes.append("A6 not-run: no post state")

    # verdict
    if (ass["A1_data_level"] == "passed"
            and ass["A2_setup_ok"] == "passed"
            and ass["A3_cast_resolved"] == "passed"
            and ass["A5_first_clause"] == "passed"
            and ass["A4_search_exile"] == "failed"):
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: with delirium active, Pick the Brain #2 "
            "resolved and its parsed first clause exiled the chosen "
            "Lightning Bolt, but the delirium search exiled 0 further "
            "same-name cards although copies sat in P1's hand, library, "
            "and graveyard. The search clause never parsed (Unimplemented "
            "head, runtime no-op), so the chain tracked set was allocated "
            "empty and the ChangeZone sub-ability had nothing to exile -- "
            "the exact structural defect the issue reports, now measured "
            "at runtime. Confirmed on v0.100.0. This is not a fix claim.")
    elif all(ass[k] == "passed" for k in ("A1_data_level", "A2_setup_ok",
                                          "A3_cast_resolved",
                                          "A4_search_exile",
                                          "A5_first_clause",
                                          "A6_cleanup")):
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: the delirium search exiled further "
            "same-name cards. This is not a fix claim.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: the reported delirium path could not "
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
               if "pick the brain" in l.lower()
               or "unimplemented" in l.lower()
               or "tracked" in l.lower()
               or "delirium" in l.lower()
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
            open(f"{BACKFILL}/driver/scenario_7435.py", "rb").read()).hexdigest(),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": {
            "stage": ST["stage"],
            "thopter_cast": ST["thopter_cast"],
            "bolt_cast": ST["bolt_cast"],
            "brain1_cast": ST["brain1_cast"],
            "brain2_cast": ST["brain2_cast"],
            "brain2_on_stack": ST["brain2_on_stack"],
            "brain2_resolved": ST["brain2_resolved"],
            "brain2_turn": ST["brain2_turn"],
            "choices": ST["choices"],
            "chosen_oid": ST["chosen_oid"],
            "search_exiled": ST.get("search_exiled"),
            "search_prompt_seen": ST["search_prompt_seen"],
            "search_choice_answered": ST["search_choice_answered"],
            "pre_p0_gy_types": ST["pre_p0_gy_types"],
            "pre_p1_bolts": ({k: len(v)
                              for k, v in (ST["pre_p1_bolts"] or {}).items()}
                             if ST["pre_p1_bolts"] else None),
            "prompts_seen": [(p[0], p[1], p[2]) for p in ST["prompts_seen"]],
            "wf_types_window": sorted(set(ST["wf_types_window"])),
            "cast_rejections": ST["cast_rejections"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The report is a data-level parse report; the scenario replays "
            "the reported line (Pick the Brain cast with delirium active) "
            "and observes the resolution outcome.",
            "12x Pick the Brain / 20x Lightning Bolt / 4x Ornithopter deck "
            "densities are test-harness conveniences (engine accepts >4-of "
            "for custom games).",
            "States are authoritative exports, restorable only via full "
            "game replay.",
        ],
        "setup_line": "P0: 12x Pick the Brain + 4x Lightning Bolt + 4x "
                      "Ornithopter + 20x Swamp + 20x Mountain; P1: 20x "
                      "Lightning Bolt + 40x Mountain (lands only, never "
                      "casts, never attacks); default Bo1, life 20",
        "contract_line": "Pick the Brain cast with delirium active "
                         "(Instant/Artifact/Creature/Sorcery in P0's "
                         "graveyard): correct = every remaining same-name "
                         "card in the opponent's graveyard, hand, and "
                         "library is exiled. Observed (bug): the search "
                         "clause never parsed (Unimplemented head, runtime "
                         "no-op), so 0 further cards are exiled and the "
                         "ChangeZone sub-ability reads an empty tracked set.",
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
                            "p0_hand": hand_lnames(state, 0),
                            "p0_untapped_lands": [
                                obj_lname(state, o)
                                for o in untapped_lands(state, 0)],
                            "p0_bf": [obj_lname(state, o)
                                      for o in bf_permanents(state, 0)],
                            "p0_gy_types": sorted(gy_types(state, 0)),
                            "p1_hand_n": len(hand_ids(state, 1)),
                            "mana_needs": ST["mana_needs"][tag],
                            "pending_target_kind": ST.get("pending_target_kind"),
                            "stage": ST.get("stage"),
                            "vi_canSubmit": vi.get("canSubmit"),
                            "vi_opps": [
                                json.dumps(o, default=str)[:1200]
                                for o in (vi.get("opportunities") or [])],
                            "legal_action_types": sorted(set(
                                a.get("type") for a in merged_actions(st))),
                        }
                        with open(f"{EVDIR}/stall_dump.jsonl", "a") as f:
                            f.write(json.dumps(summ, default=str) + "\n")
                        # full (untruncated) copy of the pending opportunities
                        # for post-mortem: the 1200-char truncation above can
                        # hide the exact shape of a missed prompt.
                        try:
                            with open(f"{EVDIR}/stall_opps_full.jsonl",
                                       "a") as f:
                                f.write(json.dumps(
                                    {"at": time.time(), "who": tag,
                                     "rev": rev,
                                     "opportunities":
                                     vi.get("opportunities") or []},
                                    default=str) + "\n")
                        except Exception:
                            pass
                        say(f"[{tag}] STALL DUMP written "
                            f"(rev {rev} unchanged 60s)")
                        wire("stall_dump", {"rev": rev})
                        # stall recovery (run 2): the tick handlers may have
                        # missed an answerable prompt (e.g. a RevealChoice
                        # whose viewer_interaction was not present when the
                        # last snapshot was evaluated). Re-run ONLY the
                        # prompt-answer handlers against the latest snapshot
                        # -- never priority passing or the stage machine, so
                        # this cannot advance the game on its own.
                        try:
                            st2 = c.latest
                            if st2:
                                state2 = st2["state"]
                                pid2 = c.player_id
                                answered = False
                                if await do_mulligan(c, pid2, tag):
                                    answered = True
                                elif await do_discard_to_handsize(
                                        c, pid2, tag):
                                    answered = True
                                elif await do_discard_vi(c, pid2, tag):
                                    answered = True
                                elif await answer_pending_target(
                                        c, st2, state2, tag):
                                    answered = True
                                elif await answer_hand_choice(
                                        c, st2, state2, tag):
                                    answered = True
                                elif await answer_search_choice(
                                        c, st2, state2, tag):
                                    answered = True
                                say(f"[{tag}] stall re-answer done: "
                                    f"answered={answered}")
                                wire("stall_reanswer",
                                     {"rev": rev, "answered": answered})
                        except Exception as e:
                            say(f"[{tag}] stall re-answer failed: {e!r}")
                            wire("stall_reanswer_failed",
                                 {"error": repr(e)})
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
                    if (not ST["brain2_resolved"]
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
                if ST.get("pending_target_kind"):
                    ST["pending_target_kind"] = None
                # a rejected cast leaves no mana owed
                ST["mana_needs"][tag] = {"B": 0, "R": 0, "generic": 0}
            st = c.latest
            if not st:
                continue
            state = st["state"]
            ST["states_seen"] += 1
            # track Brain #2 on the stack
            if ST["brain2_cast"] and not ST["brain2_on_stack"]:
                if brain_on_stack(state) is not None:
                    ST["brain2_on_stack"] = True
                    ST["phase"] = "resolving"
                    ST["resolving_since"] = time.time()
                    say("[window] Pick the Brain #2 observed on the stack")
                    wire("brain2_on_stack", {})
                    if not ST["mid_exported"]:
                        await export_as(c0_ref[0], "mid_stack")
                        ST["mid_exported"] = True
                    dump_stack_once(state, "brain2_on_stack")
            # resolution: was on the stack, now gone
            if ST["brain2_on_stack"] and not ST["brain2_resolved"]:
                if brain_on_stack(state) is None:
                    ST["brain2_resolved"] = True
                    say("[window] Pick the Brain #2 resolved; starting "
                        "settle clock")
                    wire("brain2_resolved", {})
                    ST["settle_at"] = time.time()
            # watchdog: the window must not hang the run forever
            if ST["phase"] == "resolving" and not ST["brain2_resolved"]:
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
                if (ST["brain2_resolved"] and not stack_entries(state)
                        and wf == "Priority"
                        and not hand_choice_pending(st, state)):
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
                elif not ST["brain2_resolved"]:
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
