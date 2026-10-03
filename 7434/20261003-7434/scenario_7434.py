#!/usr/bin/env python3
"""Issue #7434: Over the Top -- the variable-count reveal is unparsed, so
ChangeZoneAll reads an empty tracked set.

Reported (source:internal-triage, status:confirmed, area:parser,
mechanic:zone-change, priority:p3-card-specific,
classifier:unsupported-aspect; related #6857 census):

> Each player reveals a number of cards from the top of their library equal
> to the number of nonland permanents they control, puts all permanent cards
> they revealed this way onto the battlefield, and puts the rest into their
> graveyard.

Pinned v0.100.0 card-data.json parse (key "over the top", verified
2026-10-03; see data_evidence.json): abilities[0] (kind "Spell"):
  head = Unimplemented { name: "unparsed_quantity",
                         description: "reveal a number of cards from the top
                                       of their library equal to the number
                                       of nonland permanents they control" }
    sub_ability: ChangeZoneAll { origin null, destination Battlefield,
                                 target TrackedSetFiltered(0)
                                   filter Typed { type_filters [Permanent] } }
      sub_sub: ChangeZoneAll { origin Exile, destination Graveyard,
                               target TrackedSet(0) }
  player_scope All at every level. Cost {5}{R}{R} (card mana_cost), Sorcery.
NOTE: the issue body says the Unimplemented node's name is "reveal"; on
this pin it is "unparsed_quantity". Both are Effect::Unimplemented over the
same clause -- the structural claim is unchanged.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 12x Over the Top + 8x Memnite + 40x Mountain (60)
  P1: 8x Memnite + 52x Forest (60)
Drive:
  1. Mulligans: both seats keep 7.
  2. Build: both play a land per turn; both cast Memnite (0-cost) for free
     on their own main phase; discard-to-hand-size discards basic lands
     first (keeps Over the Top + Memnites). P1 passes priority otherwise.
  3. Cast: on P0's main phase with "Over the Top" in hand and >=7 untapped
     Mountains, export pre.json INSIDE the cast path (guarded flag), then
     submit the cast. mana_needs = {R:2, generic:5}; pay via vi land taps.
     From the cast on, ST["phase"] = "resolving": both seats only pass
     priority / answer required decisions (no more land plays or casts), so
     the pre->post window is clean.
  4. mid_stack.json at first sighting of Over the Top on the stack.
  5. post.json once the spell resolved (its object in P0's graveyard),
     stack empty, turn advanced past the cast turn, Priority, short idle.
     Record each player's nonland-permanent count at pre (would-be reveal
     counts).

Expected (correct behavior): each player reveals N cards (N = nonland
permanents they control), puts revealed permanents onto the battlefield,
puts the rest into their graveyard -- library->battlefield and
library->graveyard transitions in the window.
Reported (bug): the reveal clause never parsed (Unimplemented head, a
runtime no-op), so the publish authority allocates an empty tracked set;
both ChangeZoneAll sub-abilities read it and move nothing -- no reveal,
no battlefield moves, no graveyard moves.

Assertions:
  A1_data_level  pinned parse matches the pinned AST above (head
                 Unimplemented "unparsed_quantity" + reveal description;
                 sub ChangeZoneAll->Battlefield TrackedSetFiltered(0)
                 Typed[Permanent]; sub-sub ChangeZoneAll Exile->Graveyard
                 TrackedSet(0); All scopes; cost {5}{R}{R} Sorcery).
  A2_setup_ok    pre.json: P0 main phase, "Over the Top" in P0 hand,
                 >=7 untapped Mountains, each player controls >=1 Memnite
                 (nonland permanent), libraries full-ish (>=30).
  A3_cast_resolves
                 Over the Top was cast, its spell object was seen on the
                 stack (mid_stack.json), and it resolved -- the object
                 ended in P0's graveyard, stack later empty.
  A4_no_battlefield_moves
                 pre vs post: battlefield permanent counts per player
                 unchanged AND zero library->battlefield zone transitions
                 for either player's cards in the window. Passes under the
                 bug (ChangeZoneAll->Battlefield read an empty tracked
                 set); fails if the engine actually performed the reveal.
  A5_reveal_never_happened
                 top-of-library object identity undisturbed for both
                 players between pre and post (modulo natural draws, which
                 land in hand) AND zero library->graveyard transitions in
                 the window. Passes under the bug (the Exile->Graveyard
                 ChangeZoneAll also read the empty tracked set).
  A6_cleanup     stack empty at post, game advanced past the cast turn,
                 no submission rejections attributable to the spell's
                 resolution.

Verdict rule: reproduced iff A1, A2, A3, A4, A5 passed and A6 passed;
              not-reproduced iff A1..A3 passed and (A4 or A5) failed
              (cards actually moved -- the reveal happened);
              blocked iff A1, A2, or A3 could not be established (incl.
              engine stall with dumps).
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
RUN_ID = "20261003-7434"
ISSUE = 7434
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
                     "protocol 101 re-verified by this run: standalone "
                     "pre-check connection read ServerHello directly, and "
                     "this run's own Hello handshake advertises 101 which "
                     "the server enforces as an exact match)",
    "mode": "Full",
    "source": "2026-10-03: latest stable release v0.100.0 == pinned "
              "release dir; hashes recomputed against on-disk artifacts "
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

OTT = "over the top"
MEMNITE = "memnite"
BASICS = {"swamp", "mountain", "forest", "island", "plains"}

P0_DECK = [("Over the Top", 12), ("Memnite", 8), ("Mountain", 40)]
P1_DECK = [("Memnite", 8), ("Forest", 52)]


def deck(pairs):
    names = []
    for n, c in pairs:
        names += [n] * c
    return {"main_deck": names, "sideboard": [], "commander": []}


SETUP_DEADLINE_S = 900
RESOLVE_DEADLINE_S = 300
SETTLE_IDLE_S = 4
STALL_S = 75


def reset_attempt():
    global ST, MULLS, SUBMITTED_OPPS, _DISCARD_REV
    ST = {
        "t0": time.time(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "game_code": None,
        "phase": "build",  # build -> resolving -> done
        "ott_cast": False,
        "ott_cast_turn": None,
        "ott_oid": None,
        "ott_on_stack": False,
        "ott_resolved": False,
        "ott_resolved_at": None,
        "pre_exported": False,
        "mid_exported": False,
        "post_exported": False,
        "pre_nonland": {},
        "pre_lib": {"0": [], "1": []},
        "settle_post_at": None,
        "mana_needs": {"P0": {"R": 0, "generic": 0},
                       "P1": {"R": 0, "generic": 0}},
        "prompts_seen": [],
        "wf_types_window": [],
        "terminal": False,
        "states_seen": 0,
        "cast_rejections": 0,
        "stack_dumped": False,
        "last_rev": -1,
        "last_rev_at": time.time(),
        "stall_dumped": False,
        "ass": {k: "not-run" for k in ("A1_data_level", "A2_setup_ok",
                                       "A3_cast_resolves",
                                       "A4_no_battlefield_moves",
                                       "A5_reveal_never_happened",
                                       "A6_cleanup")},
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


def is_land(o):
    return "land" in [str(t).lower()
                      for t in (o.get("card_types") or {}).get("core_types", [])]


def bf_permanents(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def nonland_permanents(state, pid):
    return [oid for oid in bf_permanents(state, pid)
            if not is_land(get_obj(state, oid))]


def memnite_oid(state, pid):
    for oid in nonland_permanents(state, pid):
        if obj_lname(state, oid) == MEMNITE:
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


def ott_on_stack(state):
    """Match the Over the Top spell object on the stack.

    A cast spell's stack entry carries the ability's oracle-text description,
    not the card name, so match on source_id (the cast object oid) with the
    distinctive oracle phrase as fallback -- never a bare name blob.
    """
    for se in stack_entries(state):
        try:
            if int(se.get("source_id", -1)) == int(ST.get("ott_oid") or -1):
                return se
        except (TypeError, ValueError):
            pass
        blob = json.dumps(se, default=str).lower()
        if "reveals a number of cards from the top of their library" in blob:
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
    card = CARD_DATA.get(OTT, {})
    abilities = card.get("abilities") or []
    a0 = abilities[0] if abilities else {}
    head = a0.get("effect") or {}
    sub = a0.get("sub_ability") or {}
    sub_eff = sub.get("effect") or {}
    sub_tgt = sub_eff.get("target") or {}
    sub_filt = sub_tgt.get("filter") or {}
    sub2 = sub.get("sub_ability") or {}
    sub2_eff = sub2.get("effect") or {}
    sub2_tgt = sub2_eff.get("target") or {}
    mc = card.get("mana_cost") or {}
    ct = card.get("card_type") or {}
    ev = {
        "name": card.get("name"),
        "oracle": card.get("oracle_text"),
        "card_type": card.get("card_type"),
        "mana_cost": card.get("mana_cost"),
        "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
        "spell_ability": a0,
        "report_corpus_note": ("the issue body says the Unimplemented head "
                               "node is named 'reveal'; on the pinned "
                               "v0.100.0 corpus it is named "
                               "'unparsed_quantity' with the description "
                               "'reveal a number of cards from the top of "
                               "their library equal to the number of nonland "
                               "permanents they control' -- both are "
                               "Effect::Unimplemented over the same clause; "
                               "the structural claim is unchanged and this "
                               "difference does not change the verdict."),
    }
    ok = (len(abilities) >= 1
          and a0.get("kind") == "Spell"
          and head.get("type") == "Unimplemented"
          and head.get("name") == "unparsed_quantity"
          and "reveal a number of cards from the top of their library"
          in str(head.get("description", ""))
          and (a0.get("player_scope") or {}).get("type") == "All"
          and sub_eff.get("type") == "ChangeZoneAll"
          and sub_eff.get("origin") is None
          and sub_eff.get("destination") == "Battlefield"
          and sub_tgt.get("type") == "TrackedSetFiltered"
          and sub_tgt.get("id") == 0
          and sub_filt.get("type") == "Typed"
          and sub_filt.get("type_filters") == ["Permanent"]
          and (sub.get("player_scope") or {}).get("type") == "All"
          and sub2_eff.get("type") == "ChangeZoneAll"
          and sub2_eff.get("origin") == "Exile"
          and sub2_eff.get("destination") == "Graveyard"
          and sub2_tgt.get("type") == "TrackedSet"
          and sub2_tgt.get("id") == 0
          and (sub2.get("player_scope") or {}).get("type") == "All"
          and mc.get("generic") == 5
          and mc.get("shards") == ["Red", "Red"]
          and "Sorcery" in (ct.get("core_types") or []))
    say(f"data-level check: head={head.get('type')}/{head.get('name')}/"
        f"{str(head.get('description'))[:60]!r}; sub={sub_eff.get('type')}/"
        f"-> {sub_eff.get('destination')} target={json.dumps(sub_tgt)[:80]}; "
        f"sub2={sub2_eff.get('type')} {sub2_eff.get('origin')}->"
        f"{sub2_eff.get('destination')} target={json.dumps(sub2_tgt)}; "
        f"mana={json.dumps(mc)}; types={ct.get('core_types')}")
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


def discard_order_key(state, o, tag):
    nm = obj_lname(state, o)
    if tag == "P0":
        # keep Over the Top and Memnites; discard Mountains first
        return 0 if nm in BASICS else (1 if nm == MEMNITE else 2), o
    return 0 if nm in BASICS else 1, o


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
    order = sorted(hand, key=lambda o: discard_order_key(state, o, tag))
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
        # discard basics first, keep the test cards (7433 pattern refined
        # for the OTT/Memnite deck composition)
        def rank(ch):
            blob = json.dumps(ch.get("surfaces", []), default=str).lower()
            if "over the top" in blob or "memnite" in blob:
                return 1
            return 0
        chs_sorted = sorted(chs, key=rank)
        pick = chs_sorted[0].get("id")
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
    """Answer vi mana-payment choices during casting (7423/7433 pattern)."""
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

# ------------------------------------------------------------- P0 tick
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
    if (phase in ("PreCombatMain", "PostCombatMain") and active == pid
            and not stack_entries(state) and ST["phase"] == "build"):
        hn = hand_lnames(state, pid)
        # Cast Over the Top once {5}{R}{R} is affordable.
        if not ST["ott_cast"] and OTT in hn:
            pool = mana_color_pool(state, pid)
            untapped = len(untapped_lands(state, pid))
            if pool["R"] >= 2 and untapped >= 7:
                a, oid = cast_action_for(acts, state, OTT)
                if a:
                    # export pre INSIDE the cast path (guarded): the decisive
                    # pre-cast state, before the spell leaves the hand.
                    if not ST["pre_exported"]:
                        await export_as(c, "pre")
                        ST["pre_exported"] = True
                        ST["pre_nonland"] = {
                            0: len(nonland_permanents(state, 0)),
                            1: len(nonland_permanents(state, 1)),
                        }
                        ST["pre_lib"] = {
                            "0": library_oids(state, 0),
                            "1": library_oids(state, 1),
                        }
                        say(f"[arm] pre.json exported: P0 nonland="
                            f"{ST['pre_nonland'][0]} P1 nonland="
                            f"{ST['pre_nonland'][1]} (would-be reveal "
                            f"counts); libraries "
                            f"{len(ST['pre_lib']['0'])}/"
                            f"{len(ST['pre_lib']['1'])}")
                        wire("pre_exported",
                             {"nonland": ST["pre_nonland"],
                              "libs": {k: len(v)
                                       for k, v in ST["pre_lib"].items()}})
                    ST["ott_cast"] = True
                    ST["ott_cast_turn"] = state.get("turn_number")
                    ST["ott_oid"] = oid
                    ST["phase"] = "resolving"
                    ST["mana_needs"]["P0"] = {"R": 2, "generic": 5}
                    say(f"[P0] casting Over the Top (oid {oid})")
                    wire("ott_cast", {"oid": oid})
                    await submit_as_is(c, a)
                    return
        # Cast Memnite (0-cost) for free during own main phase.
        if MEMNITE in hn:
            a, oid = cast_action_for(acts, state, MEMNITE)
            if a:
                ST["mana_needs"]["P0"] = {"R": 0, "generic": 0}
                say(f"[P0] casting Memnite (oid {oid})")
                wire("memnite_cast", {"who": "P0", "oid": oid})
                await submit_as_is(c, a)
                return
        # play a land
        n_lands = sum(1 for oid in bf_permanents(state, pid)
                      if is_land(get_obj(state, oid)))
        if n_lands < 9:
            for a in acts:
                if a["type"] == "PlayLand":
                    await submit_as_is(c, a)
                    return
    await pass_priority(c, st, acts)


# ------------------------------------------------------------- P1 tick (build only, never attacks)
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
    if (phase in ("PreCombatMain", "PostCombatMain") and active == pid
            and not stack_entries(state) and ST["phase"] == "build"):
        hn = hand_lnames(state, pid)
        # Cast Memnite (0-cost) for free during own main phase.
        if MEMNITE in hn:
            a, oid = cast_action_for(acts, state, MEMNITE)
            if a:
                say(f"[P1] casting Memnite (oid {oid})")
                wire("memnite_cast", {"who": "P1", "oid": oid})
                await submit_as_is(c, a)
                return
        n_lands = sum(1 for oid in bf_permanents(state, pid)
                      if is_land(get_obj(state, oid)))
        if n_lands < 9:
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

    # A1: data-level parse matches the pinned AST
    if ST.get("data_level_ok"):
        ass["A1_data_level"] = "passed"
        notes.append("A1 passed: pinned v0.100.0 card-data.json parses "
                     "'Over the Top' as the issue reports -- abilities[0] "
                     "kind Spell, head Unimplemented ('unparsed_quantity': "
                     "'reveal a number of cards from the top of their "
                     "library equal to the number of nonland permanents "
                     "they control') + sub ChangeZoneAll ->Battlefield "
                     "target TrackedSetFiltered(0) Typed[Permanent] + "
                     "sub-sub ChangeZoneAll Exile->Graveyard target "
                     "TrackedSet(0); All scopes; cost {5}{R}{R} Sorcery. "
                     "Note: the issue body calls the head node 'reveal'; "
                     "the pin names it 'unparsed_quantity' -- same "
                     "Effect::Unimplemented over the same clause; see "
                     "data_evidence.json.")
    else:
        ass["A1_data_level"] = "failed"
        notes.append("A1 FAILED: the pinned parse does not match the "
                     "issue-reported shape; see data_evidence.json.")

    # A2: setup at the decisive pre
    if ST["pre_exported"] and pre_st:
        hn = hand_lnames(pre_st, 0)
        unt = len(untapped_lands(pre_st, 0))
        nl0 = len(nonland_permanents(pre_st, 0))
        nl1 = len(nonland_permanents(pre_st, 1))
        m0 = memnite_oid(pre_st, 0) is not None
        m1 = memnite_oid(pre_st, 1) is not None
        lib0, lib1 = len(library_oids(pre_st, 0)), len(library_oids(pre_st, 1))
        ph = pre_st.get("phase")
        act = pre_st.get("active_player")
        notes.append(f"A2 probe: pre phase={ph} active={act}; OTT in P0 hand="
                     f"{OTT in hn}; untapped P0 lands={unt}; nonland P0/P1="
                     f"{nl0}/{nl1}; Memnite on bf P0/P1={m0}/{m1}; "
                     f"libraries {lib0}/{lib1}.")
        if (ph in ("PreCombatMain", "PostCombatMain") and act == 0
                and OTT in hn and unt >= 7 and m0 and m1
                and lib0 >= 30 and lib1 >= 30):
            ass["A2_setup_ok"] = "passed"
            notes.append("A2 passed: P0 main phase, 'Over the Top' in P0 "
                         f"hand, {unt} untapped Mountains, each player "
                         f"controls a Memnite (nonland permanents "
                         f"{nl0}/{nl1} = would-be reveal counts), "
                         f"libraries {lib0}/{lib1}.")
        else:
            ass["A2_setup_ok"] = "failed"
            notes.append("A2 FAILED: the decisive pre does not show the "
                         "required setup.")
    else:
        ass["A2_setup_ok"] = "failed"
        notes.append("A2 FAILED: pre.json was never exported (the Over the "
                     "Top cast window never armed).")

    # A3: the spell was cast, seen on the stack, and resolved
    if ST["ott_cast"]:
        mid_seen = ott_on_stack(mid_st) is not None if mid_st else False
        # its object ended in P0's graveyard at post
        gy_ott = None
        for oid, o in (post_st.get("objects") or {}).items():
            if (str(o.get("base_name") or o.get("name") or "").lower() == OTT
                    and o.get("zone") == "Graveyard"
                    and str(o.get("owner", o.get("controller"))) == "0"):
                gy_ott = oid
                break
        notes.append(f"A3 probe: ott_cast={ST['ott_cast']} "
                     f"turn={ST['ott_cast_turn']}; on_stack_seen="
                     f"{ST['ott_on_stack']} (mid export={'yes' if mid_st else 'no'}); "
                     f"resolved={ST['ott_resolved']}; OTT object in P0 "
                     f"graveyard at post={gy_ott}.")
        if ST["ott_resolved"] and gy_ott is not None:
            ass["A3_cast_resolves"] = "passed"
            notes.append("A3 passed: Over the Top was cast, its spell "
                         "object was observed on the stack, and it "
                         f"resolved (object {gy_ott} in P0's graveyard).")
        else:
            ass["A3_cast_resolves"] = "failed"
            notes.append("A3 FAILED: the cast was submitted but "
                         "stack-observation or resolution could not be "
                         "confirmed.")
    else:
        ass["A3_cast_resolves"] = "failed"
        notes.append("A3 FAILED: Over the Top was never cast.")

    # zone-transition helpers over the pre->post window
    def zone_moves(pre_state, post_state, from_zone, to_zone):
        moves = []
        post_objs = post_state.get("objects") or {}
        for pid in (0, 1):
            for oid in library_oids(pre_state, pid) if from_zone == "Library" else []:
                o = post_objs.get(str(oid), {})
                if o.get("zone") == to_zone:
                    moves.append((pid, oid,
                                  str(o.get("base_name") or o.get("name"))))
        return moves

    bf_moves = []
    gy_moves = []
    bf_counts = {}
    if pre_st and post_st:
        bf_moves = zone_moves(pre_st, post_st, "Library", "Battlefield")
        gy_moves = zone_moves(pre_st, post_st, "Library", "Graveyard")
        for pid in (0, 1):
            bf_counts[pid] = (len(bf_permanents(pre_st, pid)),
                              len(bf_permanents(post_st, pid)))
    notes.append(f"A4 probe: bf permanent counts pre->post P0="
                 f"{bf_counts.get(0)} P1={bf_counts.get(1)}; "
                 f"library->battlefield transitions in window="
                 f"{[(p, n) for p, _, n in bf_moves] or 'none'}.")

    # A4: no battlefield moves in the window
    if pre_st and post_st and ST["ott_resolved"]:
        counts_same = all(bf_counts[p][0] == bf_counts[p][1] for p in (0, 1))
        if counts_same and not bf_moves:
            ass["A4_no_battlefield_moves"] = "passed"
            notes.append("A4 passed: battlefield permanent counts unchanged "
                         f"pre->post (P0 {bf_counts[0]}, P1 "
                         f"{bf_counts[1]}) and zero library->battlefield "
                         "zone transitions in the window -- THE REPORTED "
                         "BUG: the ChangeZoneAll->Battlefield sub-ability "
                         "read an empty tracked set.")
        else:
            ass["A4_no_battlefield_moves"] = "failed"
            notes.append("A4 FAILED: battlefield changed in the window -- "
                         "the reveal apparently happened (cards moved onto "
                         "the battlefield).")
    else:
        notes.append("A4 not-run: pre/post states or resolution missing.")

    # A5: library undisturbed (modulo natural draws) and no lib->gy moves
    a5_detail = {}
    if pre_st and post_st and ST["ott_resolved"]:
        ok = True
        for pid in (0, 1):
            pre_lib = library_oids(pre_st, pid)
            post_lib = library_oids(post_st, pid)
            post_objs = post_st.get("objects") or {}
            post_lib_set = set(post_lib)
            # relative order of the surviving library cards must be intact
            common_pre = [o for o in pre_lib if o in post_lib_set]
            order_ok = common_pre == post_lib
            # cards that left the library must be in hand (natural draws)
            # or on the battlefield (played lands -- not expected post-pre)
            post_hand = set(str(x)
                            for x in player_of(post_st, pid).get("hand", []))
            post_bf = set(str(x) for x in bf_permanents(post_st, pid))
            vanished = [o for o in pre_lib if o not in post_lib_set]
            vanished_ok = all(o in post_hand or o in post_bf
                              for o in vanished)
            gy_hit = [o for o in pre_lib
                      if post_objs.get(str(o), {}).get("zone") == "Graveyard"]
            a5_detail[pid] = {"pre_lib": len(pre_lib),
                              "post_lib": len(post_lib),
                              "order_ok": order_ok,
                              "vanished": len(vanished),
                              "vanished_ok": vanished_ok,
                              "lib_to_gy": len(gy_hit)}
            if not (order_ok and vanished_ok) or gy_hit:
                ok = False
        notes.append(f"A5 probe: per-player library detail={a5_detail}; "
                     f"library->graveyard transitions in window="
                     f"{[(p, n) for p, _, n in gy_moves] or 'none'}.")
        if ok and not gy_moves:
            ass["A5_reveal_never_happened"] = "passed"
            notes.append("A5 passed: top-of-library object identity "
                         "undisturbed for both players pre->post (order of "
                         "surviving cards intact; only natural draws left "
                         "the libraries) and zero library->graveyard "
                         "transitions -- THE REPORTED BUG: the "
                         "Exile->Graveyard ChangeZoneAll also read the "
                         "empty tracked set.")
        else:
            ass["A5_reveal_never_happened"] = "failed"
            notes.append("A5 FAILED: library order disturbed or cards moved "
                         "to graveyard in the window -- the reveal "
                         "apparently happened.")
    else:
        notes.append("A5 not-run: pre/post states or resolution missing.")

    # A6: cleanup
    if post_st:
        tnum = ST.get("ott_cast_turn")
        cur_turn = post_st.get("turn_number")
        rej = ST["cast_rejections"]
        notes.append(f"A6 probe: post stack entries="
                     f"{len(stack_entries(post_st))}; cast turn={tnum} "
                     f"post turn={cur_turn} phase={post_st.get('phase')}; "
                     f"cast_rejections={rej}.")
        if (not stack_entries(post_st)
                and (tnum is None or (cur_turn or 0) > tnum)):
            ass["A6_cleanup"] = "passed"
            notes.append("A6 passed: post stack empty, game advanced past "
                         f"the cast turn ({tnum} -> {cur_turn}).")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append("A6 FAILED: post stack non-empty or the game did "
                         "not advance past the cast turn.")
    else:
        notes.append("A6 not-run: no post state")

    # verdict
    if all(ass[k] == "passed" for k in
           ("A1_data_level", "A2_setup_ok", "A3_cast_resolves",
            "A4_no_battlefield_moves", "A5_reveal_never_happened",
            "A6_cleanup")):
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: Over the Top was cast on v0.100.0 with "
            f"P0/P1 nonland-permanent counts {ST['pre_nonland']} (the "
            "would-be reveal counts) and resolved with NO visible effect: "
            "battlefield permanent counts unchanged, zero "
            "library->battlefield transitions, library order undisturbed, "
            "zero library->graveyard transitions. The 'reveal a number of "
            "cards ... equal to the number of nonland permanents they "
            "control' clause never parsed (Unimplemented head "
            "'unparsed_quantity', a runtime no-op), so the publish "
            "authority allocated an empty tracked set and both "
            "ChangeZoneAll sub-abilities (->Battlefield, Exile->Graveyard) "
            "read it and moved nothing -- the exact structural defect the "
            "issue reports, now measured at runtime. Confirmed on v0.100.0. "
            "This is not a fix claim.")
    elif (ass["A1_data_level"] == "passed"
            and ass["A2_setup_ok"] == "passed"
            and ass["A3_cast_resolves"] == "passed"
            and (ass["A4_no_battlefield_moves"] == "failed"
                 or ass["A5_reveal_never_happened"] == "failed")):
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: cards actually moved in the window -- "
            "the engine performed the reveal. This is not a fix claim.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: the reported Over the Top path could "
                     "not be fully exercised; see assertion notes.")

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
               if "over the top" in l.lower()
               or "unparsed_quantity" in l.lower()
               or "unimplemented" in l.lower()
               or "tracked" in l.lower()
               or "changezone" in l.lower()
               or (ST["game_code"] and ST["game_code"] in l)]
    with open(f"{EVDIR}/server_log_excerpt.txt", "w") as f:
        f.write("\n".join(excerpt[-150:]) + "\n")
    notes.append(f"server.log excerpt: {len(excerpt)} matching lines "
                 f"(of {len(lines)} total; log={used})")

    # copy the scenario into the evidence dir (immutable record)
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
            open(f"{BACKFILL}/driver/scenario_{ISSUE}.py", "rb").read()).hexdigest(),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": {
            "ott_cast": ST["ott_cast"],
            "ott_cast_turn": ST["ott_cast_turn"],
            "ott_oid": ST["ott_oid"],
            "ott_on_stack": ST["ott_on_stack"],
            "ott_resolved": ST["ott_resolved"],
            "pre_nonland": ST["pre_nonland"],
            "pre_lib_sizes": {k: len(v) for k, v in ST["pre_lib"].items()},
            "bf_moves_window": [(p, n) for p, _, n in bf_moves],
            "gy_moves_window": [(p, n) for p, _, n in gy_moves],
            "bf_counts_pre_post": {str(k): v for k, v in bf_counts.items()},
            "a5_detail": a5_detail,
            "prompts_seen": [(p[0], p[1], p[2]) for p in ST["prompts_seen"]],
            "wf_types_window": sorted(set(ST["wf_types_window"])),
            "cast_rejections": ST["cast_rejections"],
            "stall_dumped": ST["stall_dumped"],
        },
        "verdict": verdict,
        "observed_symptom": ("Over the Top resolves with no visible effect: "
                             "no reveal, no permanents enter the battlefield, "
                             "no cards go to any graveyard; both ChangeZoneAll "
                             "sub-abilities read an empty tracked set."),
        "scope": ("Native engine via two human-client seats, default Bo1. "
                  "The scenario replays the reported line (cast Over the "
                  "Top with nonland permanents on both battlefields) and "
                  "measures the resolution outcome."),
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "12x Over the Top / 8x Memnite deck densities are test-harness "
            "conveniences (engine accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full "
            "game replay.",
            "Post is exported after the cast turn advances; natural draws "
            "between pre and post are accounted for (drawn cards verified "
            "in hand, library order of survivors intact).",
        ],
        "setup_line": "P0: 12x Over the Top + 8x Memnite + 40x Mountain; "
                      "P1: 8x Memnite + 52x Forest (passes priority except "
                      "required decisions); default Bo1, life 20",
        "contract_line": "Cast Over the Top with nonland permanents on both "
                         "battlefields: correct = each player reveals N "
                         "cards (N = nonland permanents they control), puts "
                         "revealed permanents onto the battlefield, puts "
                         "the rest into their graveyard. Observed (bug): "
                         "the reveal clause never parsed (Unimplemented "
                         "head, runtime no-op), so the chain tracked set is "
                         "allocated empty and both ChangeZoneAll "
                         "sub-abilities move nothing.",
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

    def check_stall(tag):
        # watchdog: no new state revision for STALL_S seconds while the game
        # is live => the engine is wedged; dump and end blocked.
        if ST["terminal"] or ST["stall_dumped"]:
            return
        if time.time() - ST["last_rev_at"] > STALL_S:
            ST["stall_dumped"] = True
            say(f"[watchdog] STALL: no new revision for {STALL_S}s "
                f"(tag={tag}); dumping and ending blocked")
            wire("stall", {"tag": tag, "last_rev": ST["last_rev"]})
            for nm, c in (("p0", p0), ("p1", p1)):
                st = c.latest
                if st:
                    with open(f"{EVDIR}/stall_{nm}_state.json", "w") as f:
                        json.dump(st, f, indent=1, default=str)
                    say(f"[watchdog] wrote stall_{nm}_state.json "
                        f"(rev {c.revision}, wf "
                        f"{(st['state'].get('waiting_for') or {}).get('type')})")
            ST["terminal"] = True

    async def pump(c, tag):
        while not ST["terminal"]:
            try:
                t, data = await asyncio.wait_for(c.inbox.get(), 2)
            except asyncio.TimeoutError:
                check_stall(tag)
                continue
            if t in ("Error", "ActionRejected"):
                ST["cast_rejections"] += 1
                say(f"[{tag}] {t}: {json.dumps(data)[:200]}")
                wire("rejection", {"who": tag, "type": t, "data": data})
            st = c.latest
            if not st:
                continue
            state = st["state"]
            rev = data.get("state_revision", st.get("state_revision", -1))
            if isinstance(rev, int) and rev > ST["last_rev"]:
                ST["last_rev"] = rev
                ST["last_rev_at"] = time.time()
            ST["states_seen"] += 1
            # track Over the Top on the stack
            if ST["ott_cast"] and not ST["ott_on_stack"]:
                if ott_on_stack(state) is not None:
                    ST["ott_on_stack"] = True
                    say("[window] Over the Top observed on the stack")
                    wire("ott_on_stack", {})
                    if not ST["mid_exported"]:
                        await export_as(c0_ref[0], "mid_stack")
                        ST["mid_exported"] = True
                    dump_stack_once(state, "ott_on_stack")
            # resolution: was on the stack, now gone
            if ST["ott_on_stack"] and not ST["ott_resolved"]:
                if ott_on_stack(state) is None:
                    ST["ott_resolved"] = True
                    ST["ott_resolved_at"] = time.time()
                    say("[window] Over the Top resolved; starting settle "
                        "clock")
                    wire("ott_resolved", {})
            # watchdog: the window must not hang the run forever
            if ST["phase"] == "resolving" and not ST["ott_resolved"]:
                if (time.time() - ST["t0"]
                        > SETUP_DEADLINE_S + RESOLVE_DEADLINE_S):
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
                if (ST["ott_resolved"] and not stack_entries(state)
                        and wf == "Priority"
                        and (state.get("turn_number") or 0)
                        > (ST["ott_cast_turn"] or 0)):
                    if ST["settle_post_at"] is None:
                        ST["settle_post_at"] = time.time()
                    elif time.time() - ST["settle_post_at"] >= SETTLE_IDLE_S:
                        say("[window] settle clock done; exporting post")
                        wire("post_export", {})
                        if not ST["post_exported"]:
                            await export_as(c0_ref[0], "post")
                            ST["post_exported"] = True
                        ST["terminal"] = True
                        return
                # backstop: resolved but the turn never cleanly advanced
                if (ST["ott_resolved"] and not ST["post_exported"]
                        and time.time() - (ST["ott_resolved_at"] or ST["t0"])
                        > 120):
                    say("[watchdog] resolved+120s without clean settle; "
                        "exporting post")
                    wire("post_export_backstop", {})
                    await export_as(c0_ref[0], "post")
                    ST["post_exported"] = True
                    ST["terminal"] = True
                    return
            if time.time() - ST["t0"] > SETUP_DEADLINE_S:
                say("[timeout] setup deadline reached")
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


def write_manifest_and_validate():
    import glob as _glob
    files = sorted(os.path.basename(p)
                   for p in _glob.glob(f"{EVDIR}/*")
                   if os.path.isfile(p) and os.path.basename(p) != "manifest.sha256")
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
        [sys.executable, f"{BACKFILL}/driver/render_summary_7434.py", EVDIR],
        capture_output=True, text=True, timeout=120)
    say(r.stdout.strip() or "(renderer produced no stdout)")
    if r.returncode != 0:
        say(f"renderer FAILED rc={r.returncode}: {r.stderr[:500]}")
    problems = write_manifest_and_validate()
    print(json.dumps({"verdict": verdict, "assertions": ST["ass"],
                      "validation_problems": problems}, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
