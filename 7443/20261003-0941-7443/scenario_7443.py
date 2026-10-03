#!/usr/bin/env python3
"""Issue #7443: Stick Together -- "chooses a party from among creatures they
control" is unparsed; "sacrifices the rest" sacrifices nothing.

Reported (2026-08-15, internal-triage; status:confirmed, area:parser,
priority:p3-card-specific, classifier:unsupported-aspect; related #6857
tracked-set publish census, one of 33 cards whose tracked-set antecedent
clause never parses):

> Each player chooses a party from among creatures they control, then
> sacrifices the rest. (To choose a party, choose up to one each of Cleric,
> Rogue, Warrior, and Wizard.)

Pinned v0.100.0 parse (see data_evidence.json): abilities[0], kind "Spell":
  head = Unimplemented { name: "unparsed_verb_arguments",
                         description: "choose a party from among creatures
                                       they control" }
    sub_ability (Spell): Sacrifice { target: TrackedSet(0),
                                     count: Fixed(1) }
  player_scope: All
DISCREPANCY (recorded): the issue body quotes the head name as "choose";
the pinned v0.100.0 card-data.json names it "unparsed_verb_arguments".
The description string matches, and the structural defect (Unimplemented
head -> empty chain tracked set -> Sacrifice reads an empty set) is
identical either way.

The Unimplemented resolver is a runtime no-op (pushes no GameEvent), so the
publish authority allocates a FRESH EMPTY chain tracked set and the
Sacrifice sub-ability reads that empty set. Sacrifice is a filter-only
tracked-set consumer (measured in the issue: it reads the tracked set from
resolution-time state, never through resolved_targets, so there is no
parent-target rescue path) -- an empty publish makes it a true no-op.

SECOND, INDEPENDENT PARSE DEFECT (recorded, out of runtime scope): the sub
is Sacrifice { target: TrackedSet(0) } with no Not wrapper, i.e. it is
shaped to sacrifice the CHOSEN party rather than "the rest". Even a correct
publish would sacrifice the wrong population. The runtime test cannot reach
this defect because the antecedent choose clause never parsed, so no party
is ever chosen.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 4x Stick Together + 8x Savannah Lions + 48x Plains (60)
  P1: 12x Savannah Lions + 48x Plains (60)
Drive:
  1. Mulligans: P0 keeps iff Stick Together is in the opening hand
     (mulligans down, min hand 5); P1 keeps iff Savannah Lions is in hand
     (min hand 5).
  2. Both players play one Plains per turn and cast Savannah Lions when
     able. P0 casts Stick Together ({3}{W}{W}) as soon as it holds
     >=5 untapped Plains on its own main phase with an empty stack.
     Neither player attacks or blocks (empty declarations). After the
     decisive cast, no further spells/lands are played -- the game only
     passes priority so the resolution window stays clean.
  3. pre.json is exported INSIDE the cast submit path (guarded flag), never
     by a main-loop probe. mid_stack.json is exported when the Stick
     Together spell is first seen on the stack (the real pre-resolution
     snapshot). post.json once settled (spell resolved, stack empty,
     Priority, 8s idle) or on the GameOver/deadline finalize path.
  4. The window classifier records every prompt offered to either seat
     during resolution. A choose-a-party-shaped prompt (text naming
     "party", or a battlefield-creature select/sequence during the window)
     would be recorded as party_choice and answered minimally so the game
     can continue; under the bug none appears.

Expected (correct behavior): each player is prompted to choose a party
(up to one each of Cleric, Rogue, Warrior, Wizard from creatures they
control), then sacrifices all other creatures they control. With vanilla
Savannah Lions on both sides, a correct engine sacrifices every Lion.
Reported (bug): the choose clause never parsed (Unimplemented head,
runtime no-op), so no party prompt is offered, the chain tracked set is
allocated empty, and the Sacrifice sub-ability sacrifices nothing -- all
creatures remain on the battlefield.

Assertions:
  A1_data_level   pinned v0.100.0 card-data.json parses Stick Together as
                  the issue reports: abilities[0] kind Spell, head
                  Unimplemented (record exact head name) describing "choose
                  a party from among creatures they control"; sub Spell
                  Sacrifice { target TrackedSet(0), count Fixed(1) };
                  player_scope All; cost {3}{W}{W}.
  A2_setup_ok     pre.json: Stick Together in P0 hand, >=5 untapped Plains,
                  main phase, stack empty, >=1 creature on each
                  battlefield; mid_stack.json: the Stick Together spell on
                  the stack (source = the spell object).
  A3_cast_resolved
                  the cast was submitted, the spell was seen on the stack
                  and resolved; Stick Together is in the P0 graveyard at
                  post.
  A4_no_party_prompt
                  the Unimplemented choose head produced no party choice:
                  no party_choice prompt was offered to either seat during
                  the resolution window (party_prompt_seen false, 0
                  party_choice window prompts). EXPECTED TO PASS under the
                  bug (the no-op played out at runtime).
  A5_nothing_sacrificed
                  every creature on the battlefield at pre is still on the
                  battlefield at post (exact oid sets), no creature card
                  entered either graveyard between pre and post, life
                  20/20. EXPECTED TO PASS under the bug.
  A6_cleanup      post stack empty, game advanced past the cast turn, no
                  unrejected submissions lingering.

Verdict rule: reproduced iff A1..A6 all passed (the reported parse no-op
              played out at runtime: no party prompt, nothing sacrificed);
              not-reproduced iff a party prompt was offered or any
              creature was sacrificed;
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
from client import PhaseClient, URL  # noqa: E402
import websockets  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261003-0941-7443"
ISSUE = 7443
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
                     "(shared process started by an earlier 2026-10-03 run; "
                     "ServerHello 0.100.0/bc9ef56/protocol 101 re-verified "
                     "by this run's own raw Hello handshake)",
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

STICK = "stick together"
LION = "savannah lions"
PLAINS = "plains"

P0_DECK = [("Stick Together", 4), ("Savannah Lions", 8), ("Plains", 48)]
P1_DECK = [("Savannah Lions", 12), ("Plains", 48)]


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
        "stick_cast": False,
        "stick_oid": None,
        "stick_on_stack": False,
        "stick_resolved": False,
        "cast_turn": None,
        "pre_exported": False,
        "mid_exported": False,
        "post_exported": False,
        "cast_in_flight": None,
        "party_prompt_seen": False,
        "party_answered": False,
        "mana_needs": {"P0": {"W": 0, "generic": 0},
                       "P1": {"W": 0, "generic": 0}},
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
                                       "A4_no_party_prompt",
                                       "A5_nothing_sacrificed",
                                       "A6_cleanup")},
        "notes": [],
        "data_level_ok": False,
        "hello_ok": False,
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


def is_creature(o):
    return "creature" in [str(t).lower()
                          for t in (o.get("card_types") or {}).get("core_types", [])]


def bf_permanents(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def bf_creatures(state, pid):
    """{oid: lname} of creatures on pid's battlefield."""
    return {int(oid): obj_lname(state, oid)
            for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and is_creature(o)}


def gy_creature_rows(state):
    """Sorted rows (lname, owner, controller) for creatures in graveyards."""
    rows = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") == "Graveyard" and is_creature(o):
            rows.append((obj_lname(state, oid), o.get("owner"),
                         o.get("controller")))
    return sorted(rows, key=lambda r: (str(r[0]), str(r[1]), str(r[2])))


def stick_in_gy(state, pid):
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") == "Graveyard" and obj_lname(state, oid) == STICK:
            return int(oid)
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


def stick_spell_on_stack(state):
    """The Stick Together spell object on the stack (source = spell oid)."""
    want = ST.get("stick_oid")
    if want is None:
        return None
    for se in stack_entries(state):
        if _src_matches(se, want):
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


async def verify_server_hello():
    ws = await websockets.connect(URL, max_size=1_000_000)
    raw = await asyncio.wait_for(ws.recv(), 10)
    await ws.close()
    hello = json.loads(raw)
    d = hello.get("data", {}) or {}
    ver = d.get("server_version") or d.get("version")
    build = d.get("build_commit") or d.get("build")
    proto = d.get("protocol_version") or d.get("protocol")
    say(f"ServerHello: version={ver} build={build} protocol={proto} "
        f"mode={d.get('mode')}")
    wire("server_hello", {"version": ver, "build": build, "protocol": proto})
    assert str(ver).startswith("0.100.0"), f"unexpected version {ver}"
    assert int(proto) == 101, f"unexpected protocol {proto}"
    assert str(build) == "bc9ef56", f"unexpected build {build}"
    ST["hello_ok"] = True

# ------------------------------------------------------------- data check
def check_data_level():
    stc = CARD_DATA.get("stick together", {})
    ab = (stc.get("abilities") or [{}])[0]
    head = ab.get("effect") or {}
    sub = ab.get("sub_ability") or {}
    sub_eff = sub.get("effect") or {}
    sub_tgt = sub_eff.get("target") or {}
    sub_cnt = sub_eff.get("count") or {}
    cost = stc.get("mana_cost") or {}
    ev = {
        "stick_together": stc,
        "card_data_sha256": SERVER_IDENTITY["card_data_sha256"],
        "head_name_discrepancy": {
            "issue_body_quotes": "choose",
            "pinned_data_shows": head.get("name"),
            "note": "description string matches the issue; structural "
                    "defect identical either way",
        },
        "second_parse_defect": {
            "note": "the sub is Sacrifice { target: TrackedSet(0), "
                    "count: Fixed(1) } with no Not wrapper -- shaped to "
                    "sacrifice the CHOSEN party rather than 'the rest'. "
                    "Even a correct publish would sacrifice the wrong "
                    "population. Out of runtime scope: the antecedent "
                    "choose clause never parsed, so no party is ever "
                    "chosen.",
            "sub_effect": sub_eff,
        },
    }
    ok = (
        ab.get("kind") == "Spell"
        and head.get("type") == "Unimplemented"
        and "choose a party from among creatures they control" in
        str(head.get("description", "")).lower()
        and sub_eff.get("type") == "Sacrifice"
        and sub_tgt.get("type") == "TrackedSet"
        and sub_tgt.get("id") == 0
        and sub_cnt.get("type") == "Fixed"
        and sub_cnt.get("value") == 1
        and cost.get("generic") == 3
        and cost.get("shards") == ["White", "White"]
        and (ab.get("player_scope") or {}).get("type") == "All"
    )
    say(f"data-level check: kind={ab.get('kind')}; "
        f"head={head.get('type')}/{head.get('name')}; "
        f"sub={sub_eff.get('type')}/{sub_tgt}/{sub_cnt}; "
        f"cost={cost.get('generic')}+{cost.get('shards')}; "
        f"scope={(ab.get('player_scope') or {}).get('type')}")
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(ev, f, indent=1)
    with open(f"{EVDIR}/stick_together_card_data.json", "w") as f:
        json.dump(stc, f, indent=1)
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
    # bottom lands first; never bottom the key spell/creature for the seat
    rank = {PLAINS: 0, LION: 5, STICK: 9}
    picks = [int(o) for o in sorted(
        hand, key=lambda o: (rank.get(obj_lname(state, o), 5), o))[:n]]
    SUBMITTED_OPPS.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    wire("bottom", {"who": tag, "picks": picks})
    return True


def discard_pick(state, pid, protect):
    hand = hand_ids(state, pid)
    n = len(hand) - 7
    if n <= 0:
        return []
    rank = {PLAINS: 0, LION: 5, STICK: 9}
    for p in protect:
        rank[p] = 9
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
    picks = discard_pick(state, pid, [STICK] if tag == "P0" else [LION])
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
        picks = discard_pick(state, pid, [STICK] if tag == "P0" else [LION])
        pick_oid = picks[0] if picks else None
        pick = None
        for ch in chs:
            k, ref = _cand_ref(ch)
            if pick_oid is not None and ref == pick_oid:
                pick = ch.get("id")
                break
        if pick is None:
            for ch in chs:
                k, ref = _cand_ref(ch)
                if obj_lname(state, ref) == PLAINS:
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
                for color in ("W", "U", "B", "R", "G"):
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
    discard_handsize, target_selection, party_choice, other.

    party_choice is deliberately narrow: the prompt text names "party", or
    it is a battlefield-creature select/sequence offered during the
    resolving window (the shape a choose-a-party prompt would take).
    Mana payment, pass-priority, play-land menus, attack/block
    declarations, hand-size discards, and plain target selections must
    never count as the reported prompt."""
    blob = json.dumps(opp, default=str).lower()
    resp = opp.get("response") or {}
    data = resp.get("data") or {}
    items = (data.get("choices") or []) + (data.get("candidates") or [])
    # 1. combat declarations
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
    # 3. ordinary action menus
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
    # 6. the bug's choice: choose-a-party creature selection
    if "party" in blob:
        return "party_choice"
    if saw_obj and obj_zones <= {"battlefield"} \
            and resp.get("type") == "schema":
        spec = (data.get("spec") or {}).get("type")
        if spec in ("select", "sequence") \
                and ST.get("phase") == "resolving":
            return "party_choice"
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


async def answer_party_choice(c, st, state, tag):
    """Control-branch handler: if the engine DOES offer a choose-a-party
    prompt during the resolution window, record it (A4 then fails) and
    answer minimally so the game can continue: empty selection when the
    spec min allows 0, else the first candidates up to max."""
    if ST["phase"] != "resolving" or not ST["stick_on_stack"]:
        return False
    if (wf_of(state).get("type") or "") == "TargetSelection":
        return False  # Stick Together targets nothing; not the party prompt
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
        if classify_prompt(opp) != "party_choice":
            continue
        iid = opp.get("interactionId") or opp.get("id")
        if iid in SUBMITTED_OPPS:
            continue
        ST["party_prompt_seen"] = True
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
        ST["party_answered"] = True
        say(f"[{tag}] CONTROL: choose-a-party prompt OFFERED "
            f"(min={lo} max={hi}); answering with {len(picks)} picks")
        wire("party_choice_answered", {"who": tag, "iid": iid,
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
        if await do_mulligan(c, pid, "P0", lambda hn: STICK in hn):
            return
        if await do_bottom(c, pid, "P0"):
            return
        return
    if await do_discard_to_handsize(c, pid, "P0"):
        return
    if await do_discard_vi(c, pid, "P0"):
        return
    if await answer_party_choice(c, st, state, "P0"):
        return
    # Payment-complete detector: a cast leaves the hand; the engine has
    # taken its payment: clear the owed mana.
    cif = ST.get("cast_in_flight")
    if cif is not None:
        co = get_obj(state, cif)
        if co.get("zone") != "Hand":
            ST["cast_in_flight"] = None
            ST["mana_needs"]["P0"] = {"W": 0, "generic": 0}
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
    if ST["phase"] != "build":
        # resolving: only pass priority; no more spells or lands, so the
        # pre/post battlefield comparison stays clean.
        await pass_priority(c, st, acts)
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if phase in ("PreCombatMain", "PostCombatMain") and active == pid \
            and not stack_entries(state):
        hn = hand_lnames(state, pid)
        n_plains = len(untapped_lands(state, pid))
        if STICK in hn and n_plains >= 5:
            a, oid = cast_action_for(acts, state, STICK)
            if a:
                # pre.json is exported INSIDE the cast submit path
                # (guarded flag), never by a main-loop probe.
                if not ST["pre_exported"]:
                    await export_as(c, "pre")
                    ST["pre_exported"] = True
                    pre_st = json.load(
                        open(f"{EVDIR}/pre.json")).get("state", {})
                    say(f"[cast] pre.json exported: stick in P0 hand="
                        f"{STICK in hand_lnames(pre_st, 0)}; "
                        f"untapped plains={len(untapped_lands(pre_st, 0))}; "
                        f"P0 creatures={bf_creatures(pre_st, 0)}; "
                        f"P1 creatures={bf_creatures(pre_st, 1)}; "
                        f"turn={pre_st.get('turn_number')}; "
                        f"phase={pre_st.get('phase')}")
                    wire("pre_exported",
                         {"turn": pre_st.get("turn_number"),
                          "p0_creatures": len(bf_creatures(pre_st, 0)),
                          "p1_creatures": len(bf_creatures(pre_st, 1))})
                ST["stick_oid"] = oid
                ST["stick_cast"] = True
                ST["cast_in_flight"] = oid
                ST["mana_needs"]["P0"] = {"W": 2, "generic": 3}
                ST["phase"] = "resolving"
                ST["resolving_since"] = time.time()
                ST["cast_turn"] = state.get("turn_number")
                say(f"[P0] casting Stick Together (oid {oid}) -- DECISIVE")
                wire("stick_cast", {"oid": oid})
                await submit_as_is(c, a)
                return
        if LION in hn and n_plains >= 1:
            a, oid = cast_action_for(acts, state, LION)
            if a:
                ST["cast_in_flight"] = oid
                ST["mana_needs"]["P0"] = {"W": 1, "generic": 0}
                say(f"[P0] casting Savannah Lions (oid {oid})")
                wire("lion_cast", {"who": "P0", "oid": oid})
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

# ------------------------------------------------------------- P1 tick
async def p1_tick(st, acts, state, c):
    pid = c.player_id
    if ST["phase"] == "resolving":
        record_window_prompt("P1", st)
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan(c, pid, "P1", lambda hn: LION in hn):
            return
        if await do_bottom(c, pid, "P1"):
            return
        return
    if await do_discard_to_handsize(c, pid, "P1"):
        return
    if await do_discard_vi(c, pid, "P1"):
        return
    if await answer_party_choice(c, st, state, "P1"):
        return
    cif = ST.get("cast_in_flight")
    if cif is not None and ST["phase"] == "build":
        co = get_obj(state, cif)
        if co.get("zone") != "Hand":
            ST["cast_in_flight"] = None
            ST["mana_needs"]["P1"] = {"W": 0, "generic": 0}
            say(f"[P1] cast oid {cif} left hand (zone {co.get('zone')}); "
                f"payment complete, needs cleared")
            wire("payment_complete", {"oid": cif, "zone": co.get("zone")})
    if sum(ST["mana_needs"]["P1"].values()) > 0:
        if await pay_mana_vi(c, st, state, "P1"):
            return
        if await pay_tick(c, acts, "P1"):
            return
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
    if ST["phase"] != "build":
        await pass_priority(c, st, acts)
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if phase in ("PreCombatMain", "PostCombatMain") and active == pid \
            and not stack_entries(state):
        hn = hand_lnames(state, pid)
        n_plains = len(untapped_lands(state, pid))
        if LION in hn and n_plains >= 1:
            a, oid = cast_action_for(acts, state, LION)
            if a:
                ST["cast_in_flight"] = oid
                ST["mana_needs"]["P1"] = {"W": 1, "generic": 0}
                say(f"[P1] casting Savannah Lions (oid {oid})")
                wire("lion_cast", {"who": "P1", "oid": oid})
                await submit_as_is(c, a)
                return
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

    def life(st, pid):
        return player_of(st, pid).get("life")

    # A1: data-level parse matches the issue
    if ST.get("data_level_ok"):
        ass["A1_data_level"] = "passed"
        notes.append("A1 passed: pinned v0.100.0 card-data.json parses "
                     "Stick Together as the issue reports -- abilities[0] "
                     "kind Spell, head Unimplemented (name "
                     "'unparsed_verb_arguments' in pinned data; the issue "
                     "body quotes 'choose' -- description matches, "
                     "structural defect identical) describing 'choose a "
                     "party from among creatures they control', + sub Spell "
                     "Sacrifice { target TrackedSet(0), count Fixed(1) }, "
                     "player_scope All, cost {3}{W}{W}; see "
                     "data_evidence.json and stick_together_card_data.json.")
    else:
        ass["A1_data_level"] = "failed"
        notes.append("A1 FAILED: the pinned parse does not match the "
                     "issue-reported shape; see data_evidence.json.")

    # A2: setup at the decisive pre + spell on the stack at mid
    mid_entry = stick_spell_on_stack(mid_st) if mid_st else None
    mid_txt = se_blob(mid_entry)[:300] if mid_entry else ""
    if ST["pre_exported"] and pre_st and ST["mid_exported"] and mid_st:
        stick_in_hand = STICK in hand_lnames(pre_st, 0)
        n_pl = len(untapped_lands(pre_st, 0))
        ph = pre_st.get("phase")
        act = pre_st.get("active_player")
        p0c = bf_creatures(pre_st, 0)
        p1c = bf_creatures(pre_st, 1)
        notes.append(f"A2 probe: pre stick_in_hand={stick_in_hand}; "
                     f"pre untapped_plains={n_pl}; pre phase={ph}; "
                     f"pre active={act}; pre stack={len(stack_entries(pre_st))}; "
                     f"pre P0 creatures={p0c}; pre P1 creatures={p1c}; "
                     f"mid spell on stack={mid_entry is not None}; "
                     f"mid entry text={mid_txt!r}.")
        if (stick_in_hand and n_pl >= 5 and ph in ("PreCombatMain",
                                                  "PostCombatMain")
                and act == 0 and not stack_entries(pre_st)
                and len(p0c) >= 1 and len(p1c) >= 1
                and mid_entry is not None):
            ass["A2_setup_ok"] = "passed"
            notes.append("A2 passed: pre.json shows Stick Together in the "
                         "P0 hand with >=5 untapped Plains (main phase, "
                         "stack empty, creatures on both battlefields), "
                         "and mid_stack.json shows the Stick Together "
                         "spell on the stack.")
        else:
            ass["A2_setup_ok"] = "failed"
            notes.append("A2 FAILED: pre/mid do not show the required "
                         "setup (stick in hand and/or spell on stack).")
    else:
        ass["A2_setup_ok"] = "failed"
        notes.append("A2 FAILED: pre.json and/or mid_stack.json was never "
                     "exported (the decisive window never armed).")

    # A3: cast resolved, Stick Together in the P0 graveyard
    notes.append(f"A3 probe: stick_cast={ST['stick_cast']}; "
                 f"stick_on_stack={ST['stick_on_stack']}; "
                 f"stick_resolved={ST['stick_resolved']}; "
                 f"cast_turn={ST['cast_turn']}.")
    stick_gy = stick_in_gy(post_st, 0) if post_st else None
    if (ST["stick_cast"] and ST["stick_on_stack"]
            and ST["stick_resolved"] and stick_gy is not None):
        ass["A3_cast_resolved"] = "passed"
        notes.append(f"A3 passed: Stick Together was cast, seen on the "
                     f"stack, and resolved; the card is now in the P0 "
                     f"graveyard (oid {stick_gy}).")
    else:
        ass["A3_cast_resolved"] = "failed"
        notes.append("A3 FAILED: the cast / spell resolution could not be "
                     "confirmed (see A3 probe).")

    # A4: the choose head produced no party prompt (bug symptom)
    classes = {}
    for _ph, seat, iid, opp in ST["prompts_seen"]:
        classes.setdefault(ST["prompt_classes"].get(str(iid), "other"),
                           []).append((_ph, seat, str(iid)))
    n_party = len(classes.get("party_choice", []))
    notes.append(f"A4 probe: stick_resolved={ST['stick_resolved']}; "
                 f"party_prompt_seen={ST['party_prompt_seen']}; "
                 f"party_choice window prompts={n_party} "
                 f"{classes.get('party_choice')}; "
                 f"all window prompt classes="
                 f"{ {k: len(v) for k, v in classes.items()} }.")
    if (ST["stick_resolved"] and not ST["party_prompt_seen"]
            and n_party == 0):
        ass["A4_no_party_prompt"] = "passed"
        notes.append("A4 passed (bug symptom): the spell resolved but no "
                     "choose-a-party prompt was ever offered to either "
                     "seat -- the Unimplemented choose head is a runtime "
                     "no-op, exactly the reported defect.")
    elif ST["stick_resolved"]:
        ass["A4_no_party_prompt"] = "failed"
        notes.append("A4 FAILED: a choose-a-party prompt was offered -- "
                     "the choose clause functioned at runtime.")
    else:
        notes.append("A4 not-run: the spell never resolved.")

    # A5: nothing sacrificed -- exact battlefield creature conservation
    if pre_st and post_st:
        pre_p0 = bf_creatures(pre_st, 0)
        pre_p1 = bf_creatures(pre_st, 1)
        post_p0 = bf_creatures(post_st, 0)
        post_p1 = bf_creatures(post_st, 1)
        lost_p0 = {o: pre_p0[o] for o in pre_p0 if o not in post_p0}
        lost_p1 = {o: pre_p1[o] for o in pre_p1 if o not in post_p1}
        pre_gy = gy_creature_rows(pre_st)
        post_gy = gy_creature_rows(post_st)
        new_gy = [r for r in post_gy if r not in pre_gy]
        notes.append(f"A5 probe: pre P0 creatures={pre_p0}; "
                     f"post P0 creatures={post_p0}; lost P0={lost_p0}; "
                     f"pre P1 creatures={pre_p1}; "
                     f"post P1 creatures={post_p1}; lost P1={lost_p1}; "
                     f"new graveyard creature rows={new_gy}; "
                     f"life pre={life(pre_st, 0)}/{life(pre_st, 1)} "
                     f"post={life(post_st, 0)}/{life(post_st, 1)}.")
        if (not lost_p0 and not lost_p1 and not new_gy
                and life(pre_st, 0) == life(post_st, 0) == 20
                and life(pre_st, 1) == life(post_st, 1) == 20
                and len(post_p0) >= 1 and len(post_p1) >= 1):
            ass["A5_nothing_sacrificed"] = "passed"
            notes.append("A5 passed (bug symptom): every creature on the "
                         "battlefield at pre is still there at post, no "
                         "creature card entered either graveyard, life "
                         "20/20 -- 'sacrifices the rest' sacrificed "
                         "nothing.")
        else:
            ass["A5_nothing_sacrificed"] = "failed"
            notes.append("A5 FAILED: creatures left the battlefield and/or "
                         "entered a graveyard -- the sacrifice clause "
                         "functioned at runtime.")
    else:
        notes.append("A5 not-run: pre.json and/or post.json missing.")

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
                                        "A4_no_party_prompt",
                                        "A5_nothing_sacrificed",
                                        "A6_cleanup")):
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: Stick Together was cast and resolved, but "
            "the 'chooses a party from among creatures they control' "
            "clause never parsed (Unimplemented head, runtime no-op), so "
            "no party prompt was offered to either player, the chain "
            "tracked set was allocated empty, and the Sacrifice "
            "sub-ability sacrificed nothing -- every creature stayed on "
            "the battlefield. The exact structural defect the issue "
            "reports, now measured at runtime. Confirmed on v0.100.0. "
            "This is not a fix claim.")
    elif (ass["A1_data_level"] == "passed"
            and ass["A2_setup_ok"] == "passed"
            and ass["A3_cast_resolved"] == "passed"
            and (ass["A4_no_party_prompt"] == "failed"
                 or ass["A5_nothing_sacrificed"] == "failed")):
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: the spell-resolution path was "
            "exercised and the choose/sacrifice clauses functioned at "
            "runtime (a party prompt was offered and/or creatures were "
            "sacrificed). This is not a fix claim.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: the reported spell-resolution path "
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
               if "stick together" in l.lower()
               or "party" in l.lower()
               or "sacrifice" in l.lower()
               or "tracked" in l.lower()
               or "unimplemented" in l.lower()
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
            "stick_cast": ST["stick_cast"],
            "stick_oid": ST["stick_oid"],
            "stick_on_stack": ST["stick_on_stack"],
            "stick_resolved": ST["stick_resolved"],
            "cast_turn": ST["cast_turn"],
            "party_prompt_seen": ST["party_prompt_seen"],
            "party_answered": ST["party_answered"],
            "pre_creatures": {
                "P0": bf_creatures(pre_st, 0) if pre_st else {},
                "P1": bf_creatures(pre_st, 1) if pre_st else {},
            },
            "post_creatures": {
                "P0": bf_creatures(post_st, 0) if post_st else {},
                "P1": bf_creatures(post_st, 1) if post_st else {},
            },
            "prompt_classes": {k: len(v)
                               for k, v in classes.items()},
            "prompts_seen": [(p[0], p[1], p[2]) for p in ST["prompts_seen"]],
            "wf_types_window": sorted(set(ST["wf_types_window"])),
            "stack_kinds_window": ST["stack_kinds_window"][-10:],
            "cast_rejections": ST["cast_rejections"],
            "hello_ok": ST["hello_ok"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The report is a data-level parse report; the scenario replays "
            "the reported line (Stick Together cast with creatures on both "
            "battlefields) and observes the resolution outcome.",
            "4x Stick Together / 8x Savannah Lions / 48x Plains (P0) and "
            "12x Savannah Lions / 48x Plains (P1) deck densities are "
            "test-harness conveniences (engine accepts >4-of for custom "
            "games). No party-typed creatures were used; this is "
            "immaterial because the choose clause is unparsed -- the "
            "engine never reaches creature-type filtering.",
            "Second independent parse defect (recorded, out of runtime "
            "scope): the Sacrifice sub targets TrackedSet(0) with no Not "
            "wrapper, i.e. it is shaped to sacrifice the chosen party "
            "rather than 'the rest'. The runtime test cannot reach this "
            "defect because the antecedent choose clause never parsed.",
            "States are authoritative exports, restorable only via full "
            "game replay.",
        ],
        "setup_line": "P0: 4x Stick Together + 8x Savannah Lions + 48x "
                      "Plains; P1: 12x Savannah Lions + 48x Plains; no "
                      "attacks; default Bo1, life 20",
        "contract_line": "Stick Together cast -> resolution: correct = "
                         "each player is prompted to choose a party, then "
                         "sacrifices all other creatures. Observed (bug): "
                         "no party prompt is offered (Unimplemented head, "
                         "runtime no-op), the chain tracked set is "
                         "allocated empty, and the Sacrifice sub-ability "
                         "sacrifices nothing.",
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

# ------------------------------------------------------------- main loop
async def run_game():
    reset_attempt()
    await verify_server_hello()
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
                            "stick_oid": ST.get("stick_oid"),
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
                    if (not ST["stick_resolved"]
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
                ST["mana_needs"][tag] = {"W": 0, "generic": 0}
                if ST.get("cast_in_flight"):
                    ST["cast_in_flight"] = None
                if ST.get("stick_cast") and not ST.get("stick_on_stack"):
                    # the cast submission itself was rejected; back out to
                    # the build phase so the stage machine retries
                    ST["stick_cast"] = False
                    ST["stick_oid"] = None
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
                if ST["stick_cast"] and not ST["stick_on_stack"]:
                    if stick_spell_on_stack(state) is not None:
                        ST["stick_on_stack"] = True
                        say("[window] Stick Together spell observed on "
                            "the stack")
                        wire("stick_on_stack", {})
                if ST["stick_on_stack"] and not ST["mid_exported"]:
                    await export_as(c0_ref[0], "mid_stack")
                    ST["mid_exported"] = True
                    say("[window] mid_stack exported (spell on stack)")
                    wire("mid_exported", {})
                    dump_stack_once(state, "spell_on_stack")
                if ST["stick_on_stack"] and not ST["stick_resolved"]:
                    if stick_spell_on_stack(state) is None:
                        ST["stick_resolved"] = True
                        say("[window] Stick Together resolved; "
                            "starting settle clock")
                        wire("stick_resolved", {})
                        ST["settle_at"] = time.time()
                # record the kinds of stack entries seen in the window
                for se in stack_entries(state):
                    kind = json.dumps(se.get("kind"), default=str)[:120]
                    if kind not in ST["stack_kinds_window"]:
                        ST["stack_kinds_window"].append(kind)
                # watchdog: the window must not hang the run forever
                if not ST["stick_resolved"]:
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
                if (ST["stick_resolved"] and not stack_entries(state)
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
                elif not ST["stick_resolved"]:
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
        [sys.executable, f"{BACKFILL}/driver/render_summary_7443.py", EVDIR],
        capture_output=True, text=True, timeout=120)
    say(r.stdout.strip() or "(renderer produced no stdout)")
    if r.returncode != 0:
        say(f"renderer FAILED rc={r.returncode}: {r.stderr[:500]}")
    problems = write_manifest_and_validate()
    print(json.dumps({"verdict": verdict, "assertions": ST["ass"],
                      "validation_problems": problems}, indent=1))


if __name__ == "__main__":
    asyncio.run(main())
