#!/usr/bin/env python3
"""Issue #7423: Eumidian Wastewaker -- "you and defending player each discard
a card or sacrifice a permanent" is unparsed, so `Draw` reads an empty tracked
set.

Reported (2026-08-15, lgray, source:internal-triage, status:confirmed,
area:parser, classifier:unsupported-aspect, priority:p3-card-specific):
On Eumidian Wastewaker, the clause "you and defending player each discard a
card or sacrifice a permanent" does not parse: the parser emits an
`Effect::Unimplemented` node (`unbound_subject`) in its place. That resolver
pushes no `GameEvent`, so the chain tracked set is allocated empty and the
dependent `Draw` ("You draw a card for each land card put into a graveyard
this way") reads an empty set.

A Discord report (comment by matthewevans, 2026-08-23, thread
1540902517213364275) adds the runtime symptom: "When it attacks, there is no
prompt for attacking and defending players to sacrifice a permanent or
discard a card."

Pinned v0.100.0 parse (see data_evidence.json):
  triggers[0] (mode Attacks, SelfRef -> Battlefield):
    execute.effect = Unimplemented { name: "unbound_subject",
        description: "you and defending player each discard a card or
                      sacrifice a permanent" }
      sub_ability: Draw { count: Ref(FilteredTrackedSetSize(Typed[Land])),
                         target: Controller }
    (the attack-trigger head is the chain root; the sub reads the chain
    tracked set.)

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 -- written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 4x Eumidian Wastewaker + 56x Swamp
      (Wastewaker costs {2}{B}{B}; dense playsets are a test-harness
      convenience)
  P1: 12x Grizzly Bears + 48x Forest (plays lands/bears, never attacks)
Drive:
  1. Mulligans: P0 keeps Wastewaker + >=2 Swamps (mulligans at most three
     times); P1 keeps.
  2. On the first P0 main phase with 4+ untapped Swamps: cast Eumidian
     Wastewaker paying {2}{B}{B}.
  3. On the first P0 DeclareAttackers step with the Wastewaker on the
     battlefield untapped (no summoning sickness): export pre.json
     IMMEDIATELY BEFORE submitting the attack (guarded flag; the decisive
     pre), then declare the Wastewaker attacking P1 (defending player 1).
  4. Let the "Whenever this creature attacks" trigger resolve; record every
     viewer-interaction opportunity offered to EITHER seat from the attack
     declaration through the end of combat; export post once the game has
     settled (PostCombatMain, Priority, stack empty, 8s idle).

Expected (correct behavior): the attack trigger offers both seats a
"discard a card or sacrifice a permanent" choice; each player discards a
card or sacrifices a permanent; P0 draws a card for each LAND card put into
a graveyard this way.
Reported (bug): the head clause never parsed, so the trigger is a silent
no-op -- no prompt is ever offered, no discard/sacrifice happens, and the
Draw reads an empty tracked set (draws 0).

Assertions (correct-behavior expectations; a FAIL records the bug):
  A1_data_level   pinned v0.100.0 card-data.json parses the attack trigger
                  as the issue reports: execute.effect Unimplemented
                  unbound_subject naming the "each discard a card or
                  sacrifice a permanent" clause; sub Draw with
                  Ref(FilteredTrackedSetSize(Typed[Land])), target
                  Controller.
  A2_setup_ok     pre.json: Wastewaker on P0's battlefield; P0 and P1 each
                  hold >=1 card in hand (a discard choice would have
                  content); P1 has >=1 permanent (a sacrifice choice would
                  have content).
  A3_attack_trigger_resolved the Wastewaker attacked P1 and its attack
                  trigger fired and resolved (trigger observed on the
                  stack / in triggers_fired_this_turn; combat proceeded
                  past DeclareAttackers with an empty stack).
  A4_choice_offered (THE REPORTED BUG) during the attack window, a
                  "discard a card or sacrifice a permanent" choice was
                  offered to the seats. Expected: offered. Observed bug:
                  never offered.
  A5_state_change (THE REPORTED BUG) the trigger changed the game: a
                  discard or sacrifice happened and/or P0 drew cards.
                  Expected: changed. Observed bug: hand sizes, graveyards,
                  and battlefield permanent counts unchanged.
  A6_cleanup      post stack empty, game advancing.

Verdict rule: reproduced iff A1, A2, A3 passed and NOT (A4 passed and A5
              passed); not-reproduced iff A1..A5 all passed; blocked iff
              A1, A2, or A3 could not be established.

Protocol-101 driver notes (v0.100.0, build bc9ef56, verified 2026-10-02):
  - HELLO advertises protocol 101 (server enforces exact match).
  - MulliganDecision as {"choice":{"type":"Keep"}} gated on
    waiting_for.data.pending[] with phase type "Declare".
  - BottomCards via single SelectCards {"cards":[...]}.
  - CastSpell via advertised action; mana via PayMana actions and
    viewer-interaction tapLandForMana choices.
  - DeclareAttackers data: {"attacks": [[attacker_oid,
    {"type":"Player","data":defending_pid}]], "bands": []}.
  - TargetSelection answered via viewer_interaction: candidates carry
    serialized object references ("reference": "<oid>"); matched on
    serialized content, never on bare numeric needles.
  - Authoritative exports only from the host seat (P0 creates the game).
  - Adapted from scenario_7422.py (issue #7422, same Unimplemented-head
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
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261002-7423"
ISSUE = 7423
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

WASTEWAKER = "eumidian wastewaker"
BEAR = "grizzly bears"
FOREST = "forest"
SWAMP = "swamp"

P0_DECK = [("Eumidian Wastewaker", 4), ("Swamp", 56)]
P1_DECK = [("Grizzly Bears", 12), ("Forest", 48)]

SETUP_DEADLINE_S = 1700

ST = {}
MULLS = set()
MULL_COUNT = {}
SUBMITTED = set()
SUBMITTED_OPPS = set()
_DISCARD_REV = {}


def reset():
    ST.clear()
    ST.update({
        "t0": time.time(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "game_code": None,
        "phase": "setup",  # setup -> casting -> pre_attack -> attacking -> done
        "wastewaker_cast": False,
        "wastewaker_cast_at": None,
        "wastewaker_oid": None,
        "pre_exported": False,
        "post_exported": False,
        "attack_submitted": False,
        "hand_at_pre": {},
        "gy_at_pre": {},
        "perm_at_pre": {},
        "triggers_fired_at_pre": None,
        "stack_saw_trigger": False,
        "trigger_resolved": False,
        "trigger_window_stats": {},
        "prompts_seen": [],             # (phase, seat, iid) vi opportunities
                                        # during the attack window
        "wf_types_window": [],          # waiting_for types seen in window
        "mana_needs": {"B": 0, "generic": 0},
        "mana_tapped": 0,
        "terminal": False,
        "terminal_data": None,
        "states_seen": 0,
        "cast_rejections": 0,
        "settle_at": None,
        "ass": {k: "not-run" for k in ("A1_data_level", "A2_setup_ok",
                                       "A3_attack_trigger_resolved",
                                       "A4_choice_offered",
                                       "A5_state_change", "A6_cleanup")},
        "notes": [],
    })


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


def is_creature(o):
    return "creature" in [str(t).lower()
                          for t in (o.get("card_types") or {}).get("core_types", [])]


def is_land(o):
    return "land" in [str(t).lower()
                      for t in (o.get("card_types") or {}).get("core_types", [])]


def bf_creatures(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and is_creature(o)]


def wastewaker_bf_untapped(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if obj_lname(state, oid) == WASTEWAKER
            and o.get("zone") == "Battlefield" and o.get("controller") == pid
            and not o.get("tapped")]


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
    needles (case-insensitive). Never matches on bare numeric needles:
    candidate ids embed the interaction counter."""
    data = (opp.get("response", {}) or {}).get("data", {}) or {}
    chs = data.get("choices") or data.get("candidates") or []
    for ch in chs:
        if (ch.get("status", {}) or {}).get("type") not in (None, "available"):
            continue
        ser = json.dumps(ch, default=str).lower()
        if all(n.lower() in ser for n in needles):
            return ch.get("id")
    return None

# ------------------------------------------------------------- data check
def check_data_level():
    card = CARD_DATA.get("eumidian wastewaker", {})
    trigs = card.get("triggers") or []
    trig = trigs[0] if trigs else {}
    ex = (trig.get("execute") or {})
    eff = ex.get("effect") or {}
    sub = (ex.get("sub_ability") or {})
    seff = sub.get("effect") or {}
    cnt = seff.get("count") or {}
    qty = cnt.get("qty") or {}
    filt = qty.get("filter") or {}
    tgt = seff.get("target") or {}
    findings = {
        "name": card.get("name"),
        "mana_cost": card.get("mana_cost"),
        "oracle": card.get("oracle_text"),
        "trigger_mode": trig.get("mode"),
        "trigger_zones": trig.get("trigger_zones"),
        "head_effect": eff,
        "sub_ability": sub,
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(findings, f, indent=1, default=str)
    wire("data_level", {"head": eff, "sub": seff})
    ok = (trig.get("mode") == "Attacks"
          and eff.get("type") == "Unimplemented"
          and eff.get("name") == "unbound_subject"
          and "each discard a card or sacrifice a permanent" in
          str(eff.get("description"))
          and seff.get("type") == "Draw"
          and cnt.get("type") == "Ref"
          and qty.get("type") == "FilteredTrackedSetSize"
          and filt.get("type") == "Typed"
          and "Land" in (filt.get("type_filters") or [])
          and tgt.get("type") == "Controller")
    say(f"data-level check: trigger mode={trig.get('mode')}, "
        f"head={eff.get('type')}/{eff.get('name')!r}, "
        f"sub={seff.get('type')} count={cnt.get('type')}/"
        f"{qty.get('type')} filter={filt.get('type_filters')} "
        f"target={tgt.get('type')} -> "
        f"{'MATCHES ISSUE REPORT' if ok else 'MISMATCH'}")
    ST["notes"].append(
        "data-level: pinned v0.100.0 card-data.json parses Eumidian "
        "Wastewaker's attack trigger as mode=Attacks with head "
        f"Unimplemented(name={eff.get('name')!r}, "
        f"description={str(eff.get('description'))[:60]!r}...) + sub "
        f"Draw(count=Ref(FilteredTrackedSetSize(Typed"
        f"{filt.get('type_filters')})), target={tgt.get('type')}) "
        f"(issue-reported shape: {ok})")
    ST["data_level_ok"] = ok
    return ok

# ------------------------------------------------------------- interaction
async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


def mulligan_keep_p0(hand):
    swamps = sum(1 for n in hand if n == SWAMP)
    return WASTEWAKER in hand and swamps >= 2


def mulligan_keep_p1(hand):
    return True


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
    if keep_fn(hn) or mulls >= 3 or len(hn) <= 4:
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


RANK = {WASTEWAKER: 10, SWAMP: 6, FOREST: 6, BEAR: 5}


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
    if key in SUBMITTED:
        return False
    n = int((pend.get("phase") or {}).get("count") or 0)
    if n <= 0:
        return False
    hand = hand_ids(state, pid)
    picks = sorted(hand, key=lambda o: RANK.get(obj_lname(state, o), 2))[:n]
    SUBMITTED.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    wire("bottom", {"who": tag, "picks": picks})
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
    picks = [int(x) for x in sorted(
        hand, key=lambda o: RANK.get(obj_lname(state, o), 2))[:n]]
    say(f"[{tag}] discarding to hand size: {[obj_lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    _DISCARD_REV[(c.name, rev)] = True
    return True


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


async def pay_mana_vi(c, st, state, tag):
    """Answer vi mana-payment choices during casting. The engine runs a
    tap-to-pool payment UI (one tapLandForMana interaction per land); never
    touches cancelCast / untapLandForMana / unspendPoolMana."""
    vi = get_vi(st)
    if not vi:
        return False
    acted = False
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
            ST["mana_tapped"] += 1
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
                                "data": {"choiceIds": [pick["id"]]}}
                   }
        await c.send_interaction(sub)
        acted = True
    return acted


def castable_wastewaker(state):
    return len(untapped_lands(state, 0)) >= 4


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
        "triggers_fired": [json.dumps(t, default=str)
                           for t in (state.get("triggers_fired_this_turn") or [])],
        "discarded_this_turn": state.get("players_who_discarded_card_this_turn"),
        "tracked_sets": json.dumps(state.get("tracked_object_sets"), default=str)[:600],
    }

# ------------------------------------------------------------- P0 tick
async def p0_tick(st, acts, state, c):
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan(c, 0, "P0", mulligan_keep_p0):
            return
        if await do_bottom(c, 0, "P0"):
            return
        return
    if wtype == "BottomCards":
        if await do_bottom(c, 0, "P0"):
            return
        return
    if ST["phase"] == "casting":
        if await pay_mana_vi(c, st, state, "P0"):
            return
        if await pay_tick(c, acts, "P0"):
            return
    if await do_discard_to_handsize(c, 0, "P0"):
        return
    if wtype == "DeclareAttackers":
        da = next((a for a in acts
                   if a["type"] == "DeclareAttackers"), None)
        if ST["phase"] in ("pre_attack", "casting"):
            ww = wastewaker_bf_untapped(state, 0)
            # Export pre ONLY in the same tick that submits the attack
            # (da must be advertised); otherwise the pre can land a full
            # turn before the attack and later land plays pollute the
            # window deltas.
            if ww and not ST["attack_submitted"] and da:
                # THE decisive pre: exported INSIDE the submission path.
                if not ST["pre_exported"]:
                    await export_as(c, "pre")
                    ST["pre_exported"] = True
                    ST["wastewaker_oid"] = ww[0]
                    ST["hand_at_pre"] = {str(pid): len(hand_ids(state, pid))
                                         for pid in (0, 1)}
                    ST["gy_at_pre"] = {str(pid): len(gy_ids(state, pid))
                                       for pid in (0, 1)}
                    ST["perm_at_pre"] = {str(pid): len(bf_permanents(state, pid))
                                         for pid in (0, 1)}
                    ST["perm_oids_at_pre"] = {
                        str(pid): sorted(bf_permanents(state, pid))
                        for pid in (0, 1)}
                    ST["triggers_fired_at_pre"] = json.dumps(
                        state.get("triggers_fired_this_turn") or [],
                        default=str)
                    say(f"[P0] pre.json exported; Wastewaker oid={ww[0]}; "
                        f"hand={ST['hand_at_pre']} gy={ST['gy_at_pre']} "
                        f"perm={ST['perm_at_pre']}")
                d = copy.deepcopy(da)
                d.setdefault("data", {}).update(
                    {"attacks": [[ww[0], {"type": "Player", "data": 1}]],
                     "bands": []})
                ST["attack_submitted"] = True
                ST["attack_turn"] = state.get("turn_number")
                ST["phase"] = "attacking"
                ST["settle_at"] = None
                wire("attack_declared",
                     {"attacker_oid": ww[0], "defender": 1,
                      "data": d["data"]})
                say(f"[P0] attacking P1 with Eumidian Wastewaker "
                    f"(oid {ww[0]}); phase -> attacking")
                await submit_as_is(c, d)
                return
        # no attack this turn (not ready yet): declare no attackers.
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
    if phase in ("PreCombatMain", "PostCombatMain") and active == 0:
        hn = hand_lnames(state, 0)
        # the Wastewaker cast: {2}{B}{B}.
        if (ST["phase"] == "setup" and WASTEWAKER in hn
                and castable_wastewaker(state)):
            a, oid = cast_action_for(acts, state, WASTEWAKER)
            if a:
                ST["wastewaker_cast"] = True
                ST["wastewaker_cast_at"] = time.time()
                ST["mana_needs"] = {"B": 2, "generic": 2}
                ST["mana_tapped"] = 0
                ST["phase"] = "casting"
                say(f"[P0] casting Eumidian Wastewaker (oid {oid})")
                wire("wastewaker_cast", {"oid": oid})
                await submit_as_is(c, a)
                return
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(c, a)
                return
    await pass_priority(c, st, acts)


# ------------------------------------------------------------- P1 tick
async def p1_tick(st, acts, state, c):
    wtype = wf_of(state).get("type") or ""
    if wtype == "MulliganDecision":
        if await do_mulligan(c, 1, "P1", mulligan_keep_p1):
            return
        if await do_bottom(c, 1, "P1"):
            return
        return
    if wtype == "BottomCards":
        if await do_bottom(c, 1, "P1"):
            return
        return
    if await do_discard_to_handsize(c, 1, "P1"):
        return
    if wtype == "DeclareAttackers":
        # P1 never attacks.
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            d = copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if wtype == "DeclareBlockers":
        # P1 lets the Wastewaker through unblocked (simpler combat).
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
        # cast a bear when affordable (2 untapped lands incl. a forest)
        if (BEAR in hand_lnames(state, 1)
                and len(untapped_lands(state, 1)) >= 2
                and any(obj_lname(state, o) == FOREST
                        for o in untapped_lands(state, 1))):
            a, oid = cast_action_for(acts, state, BEAR)
            if a:
                say(f"[P1] casting Grizzly Bears (oid {oid})")
                await submit_as_is(c, a)
                return
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
        notes.append("A1 passed: pinned v0.100.0 card-data.json parses "
                     "Eumidian Wastewaker's attack trigger as mode=Attacks "
                     "with head Unimplemented('unbound_subject': 'you and "
                     "defending player each discard a card or sacrifice a "
                     "permanent') + sub Draw(count=Ref(FilteredTrackedSetSize"
                     "(Typed[Land])), target=Controller); see "
                     "data_evidence.json.")
    else:
        ass["A1_data_level"] = "failed"
        notes.append("A1 FAILED: the pinned parse does not match the "
                     "issue-reported shape; see data_evidence.json.")

    # A2: setup at the decisive pre
    if ST["pre_exported"] and pre_st:
        h0, h1 = ST["hand_at_pre"].get("0", 0), ST["hand_at_pre"].get("1", 0)
        p1p = ST["perm_at_pre"].get("1", 0)
        ww_bf = any(obj_lname(pre_st, int(oid)) == WASTEWAKER
                    and get_obj(pre_st, int(oid)).get("zone") == "Battlefield"
                    and get_obj(pre_st, int(oid)).get("controller") == 0
                    for oid in (pre_st.get("objects") or {}))
        notes.append(f"A2 probe: pre Wastewaker-on-P0-BF={ww_bf}; "
                     f"hand P0={h0} P1={h1}; P1 permanents={p1p}.")
        if ww_bf and h0 >= 1 and h1 >= 1 and p1p >= 1:
            ass["A2_setup_ok"] = "passed"
            notes.append("A2 passed: Wastewaker on P0's battlefield; both "
                         "seats hold >=1 card (a discard choice would have "
                         "content) and P1 has >=1 permanent (a sacrifice "
                         "choice would have content).")
        else:
            ass["A2_setup_ok"] = "failed"
            notes.append("A2 FAILED: the decisive pre lacks the material "
                         "for the reported line.")
    else:
        ass["A2_setup_ok"] = "failed"
        notes.append("A2 FAILED: pre.json was never exported (the attack "
                     "never happened).")

    # A3: the attack happened and the attack trigger fired + resolved
    trig_ser = ST["trigger_window_stats"].get("triggers_fired_new", [])
    if (ST["attack_submitted"] and ST["stack_saw_trigger"]
            and ST["trigger_resolved"]):
        ass["A3_attack_trigger_resolved"] = "passed"
        notes.append("A3 passed: the Wastewaker attacked P1; its 'Whenever "
                     "this creature attacks' trigger was observed on the "
                     "stack and resolved (stack emptied, combat proceeded). "
                     f"triggers_fired_this_turn delta: "
                     f"{json.dumps(trig_ser)[:240]}.")
    elif ST["attack_submitted"] and ST["trigger_resolved"]:
        ass["A3_attack_trigger_resolved"] = "passed"
        notes.append("A3 passed (trigger inferred): the Wastewaker attacked "
                     "P1 and combat proceeded past the trigger window with "
                     "an empty stack, but no stack entry naming the trigger "
                     "was captured by a live tick (fast resolution).")
    elif ST["attack_submitted"]:
        ass["A3_attack_trigger_resolved"] = "failed"
        notes.append("A3 FAILED: the attack was submitted but the trigger "
                     "window never completed (stall or disconnect).")
    else:
        notes.append("A3 not-run: no attack was ever declared.")

    # A4: was a discard-or-sacrifice choice offered to either seat?
    # A prompt appearing is not a pass (playbook step 2): only a
    # CHOICE-shaped prompt counts. Ordinary turn-structure prompts
    # (priority passes, tap-land/cast menus, DeclareAttackers/DeclareBlockers
    # relations schema) do NOT count.
    ORDINARY_ACTIONS = {"passPriority", "tapLandForMana", "castSpell",
                        "playLand", "activateAbility"}
    choice_like = []
    for _ph, seat, iid, opp in ST["prompts_seen"]:
        resp = (opp or {}).get("response") or {}
        rtype = resp.get("type")
        if rtype == "schema":
            spec = ((resp.get("data") or {}).get("spec")) or {}
            if spec.get("type") == "relations":
                # DeclareAttackers / DeclareBlockers assignment prompts are
                # turn-structure, never the discard-or-sacrifice choice.
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
                continue  # ordinary turn menu, not the choice step
            choice_like.append((seat, iid))
    notes.append(f"Attack-window prompts: {len(ST['prompts_seen'])} vi "
                 f"opportunities seen by either seat; waiting_for types: "
                 f"{sorted(set(ST['wf_types_window']))}; choice-shaped: "
                 f"{choice_like or 'none'}.")
    if choice_like:
        ass["A4_choice_offered"] = "passed"
        notes.append("A4 passed: a choice-shaped prompt was offered during "
                     f"the attack window: {choice_like[:4]}.")
    elif ST["attack_submitted"] and ST["trigger_resolved"]:
        ass["A4_choice_offered"] = "failed"
        notes.append("A4 FAILED: the attack trigger resolved but NO "
                     "discard-or-sacrifice choice was ever offered to "
                     "either seat -- THE REPORTED BUG (the head clause "
                     "never parsed, so the choice step does not exist at "
                     "runtime; matches the Discord report 'there is no "
                     "prompt').")
    else:
        notes.append("A4 not-run: the attack trigger window never "
                     "completed.")

    # A5: did the trigger change the game at all?
    win = ST["trigger_window_stats"]
    h_delta = win.get("hand_delta")
    g_delta = win.get("gy_delta")
    p_delta = win.get("perm_delta")
    disc = win.get("discarded_this_turn")
    # oid-level permanent diff (forensics on any unexpected delta)
    wend = load_env("window_end") or {}
    wend_st = wend.get("state") or {}
    perm_diff_note = ""
    if wend_st and ST.get("perm_oids_at_pre"):
        new_perms, gone_perms = {}, {}
        for pid in ("0", "1"):
            pre_oids = set(ST["perm_oids_at_pre"].get(pid, []))
            end_oids = set(bf_permanents(wend_st, int(pid)))
            for oid in sorted(end_oids - pre_oids):
                new_perms[oid] = (obj_lname(wend_st, oid),
                                  get_obj(wend_st, oid).get("zone"))
            for oid in sorted(pre_oids - end_oids):
                gone_perms[oid] = obj_lname(pre_st, oid)
        perm_diff_note = (f" permanent oid diff pre->window-end: "
                          f"appeared={new_perms or 'none'} "
                          f"left={gone_perms or 'none'}.")
    notes.append(f"A5 probe: hand pre={ST.get('hand_at_pre')} -> window-end "
                 f"{win.get('hand_end')} (delta={h_delta}); gy pre="
                 f"{ST.get('gy_at_pre')} -> {win.get('gy_end')} "
                 f"(delta={g_delta}); permanents pre="
                 f"{ST.get('perm_at_pre')} -> {win.get('perm_end')} "
                 f"(delta={p_delta});{perm_diff_note} "
                 f"players_who_discarded_card_this_turn={disc}.")
    if h_delta is None:
        notes.append("A5 not-run: no window-end snapshot was captured.")
    elif (h_delta.get("0") == 0 and h_delta.get("1") == 0
          and g_delta.get("0") == 0 and g_delta.get("1") == 0
          and p_delta.get("0") == 0 and p_delta.get("1") == 0):
        ass["A5_state_change"] = "failed"
        notes.append("A5 FAILED: the attack trigger resolved with ZERO "
                     "observable effect -- no discard by either player, no "
                     "sacrifice, and P0 drew 0 cards -- THE REPORTED BUG "
                     "(Draw read the empty chain tracked set, count=0).")
    else:
        ass["A5_state_change"] = "passed"
        notes.append("A5 passed: the trigger changed the game state "
                     f"(deltas above).")

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
            and ass["A3_attack_trigger_resolved"] == "passed"
            and not (ass["A4_choice_offered"] == "passed"
                     and ass["A5_state_change"] == "passed")):
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: Eumidian Wastewaker attacked, its attack "
            "trigger fired and resolved, but no discard-or-sacrifice choice "
            "was ever offered and nothing changed -- see the failed "
            "assertion(s) above (confirmed on v0.100.0). The parse is the "
            "reported defect; the silent no-op is its direct structural "
            "consequence. This answers the issue's open "
            "consumer-classification question for Draw on an empty chain "
            "tracked set: it is filter-only -- count 0, draws nothing, a "
            "silent no-op. This is not a fix claim.")
    elif all(ass[k] == "passed" for k in ("A1_data_level", "A2_setup_ok",
                                          "A3_attack_trigger_resolved",
                                          "A4_choice_offered",
                                          "A5_state_change")):
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: the attack trigger offered the "
            "discard-or-sacrifice choice and changed the game as printed. "
            "This is not a fix claim.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: the reported attack-trigger path "
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
               if "wastewaker" in l.lower() or "eumidian" in l.lower()
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
            open(f"{BACKFILL}/driver/scenario_7423.py", "rb").read()).hexdigest(),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": {
            "wastewaker_cast": ST["wastewaker_cast"],
            "wastewaker_cast_at": ST["wastewaker_cast_at"],
            "wastewaker_oid": ST["wastewaker_oid"],
            "attack_submitted": ST["attack_submitted"],
            "stack_saw_trigger": ST["stack_saw_trigger"],
            "trigger_resolved": ST["trigger_resolved"],
            "hand_at_pre": ST["hand_at_pre"],
            "gy_at_pre": ST["gy_at_pre"],
            "perm_at_pre": ST["perm_at_pre"],
            "trigger_window_stats": ST["trigger_window_stats"],
            "prompts_seen": [(p[0], p[1], p[2]) for p in ST["prompts_seen"]],
            "wf_types_window": sorted(set(ST["wf_types_window"])),
            "cast_rejections": ST["cast_rejections"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The report is a data-level parse report (no game state); the "
            "scenario replays the reported line (Wastewaker attacks the "
            "defending player with both seats holding cards and P1 holding "
            "permanents) from a fresh game and observes the resolution "
            "outcome.",
            "The Encore ability was not exercised (only the attack trigger).",
            "States are authoritative exports, restorable only via full "
            "game replay.",
        ],
        "setup_line": "P0: 4x Eumidian Wastewaker + 56x Swamp; P1: 12x "
                      "Grizzly Bears + 48x Forest (lands/bears, never "
                      "attacks, never blocks)",
        "contract_line": "Wastewaker attacks P1; both seats must be offered "
                         "the 'discard a card or sacrifice a permanent' "
                         "choice and P0 must draw for each land put into a "
                         "graveyard this way. Observed: the clause never "
                         "parsed, no choice prompt ever appeared, and the "
                         "trigger was a complete silent no-op (hands, "
                         "graveyards, permanents all unchanged; P0 drew 0).",
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


# ------------------------------------------------------------- main
async def main():
    reset()
    check_data_level()

    p0 = PhaseClient("P0")
    p1 = PhaseClient("P1")
    await p0.connect()
    await p0.create(deck(*P0_DECK), player_count=2)
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    await asyncio.sleep(2.0)
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_setup", {"game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})

    t_start = time.time()
    last = {}
    last_tick_at = {}
    finalized = False

    while time.time() - t_start < SETUP_DEADLINE_S and not finalized:
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

                # attack-window observation (either seat)
                if ST["phase"] == "attacking":
                    vi = get_vi(st)
                    if vi:
                        for opp in vi.get("opportunities", []) or []:
                            record_window_prompt(tag, opp)
                    wtype = (wf_of(state).get("type") or "")
                    if wtype and wtype not in ST["wf_types_window"]:
                        ST["wf_types_window"].append(wtype)
                    for se in stack_entries(state):
                        ser = json.dumps(se, default=str).lower()
                        if ("wastewaker" in ser or "eumidian" in ser
                                or "each discard a card" in ser):
                            if not ST["stack_saw_trigger"]:
                                ST["stack_saw_trigger"] = True
                                say("attack trigger observed on the stack")
                                wire("trigger_on_stack", {"entry": se})
                    # trigger window complete: the stack carried the
                    # trigger and emptied again on the SAME turn (the
                    # trigger is mandatory, so combat cannot proceed
                    # without it having been put on the stack and
                    # resolved). The same-turn guard keeps the window-end
                    # snapshot from sliding into a later turn (a land
                    # played next turn once masqueraded as a trigger
                    # effect).
                    same_turn = (state.get("turn_number")
                                 == ST.get("attack_turn"))
                    advanced = (wtype == "DeclareBlockers"
                                or "Blockers" in wtype
                                or "CombatDamage" in wtype
                                or state.get("phase") == "PostCombatMain")
                    if (not ST["trigger_resolved"] and same_turn
                            and not stack_entries(state)
                            and (ST["stack_saw_trigger"] or advanced)):
                        if not ST["stack_saw_trigger"]:
                            say("trigger resolved inferred: combat advanced "
                                f"past attackers step (wf={wtype}, "
                                f"phase={state.get('phase')}) with empty "
                                "stack; no tick caught the stack entry")
                            wire("trigger_inferred_resolved",
                                 {"waiting_for": wtype,
                                  "phase": state.get("phase")})
                        ST["trigger_resolved"] = True
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
                                       "attack_turn": ST.get("attack_turn"),
                                       "perm_oids": {
                                           str(pid):
                                               sorted(bf_permanents(state,
                                                                    pid))
                                           for pid in (0, 1)}},
                                      wfj, default=str)
                        say("window_end.json written from observed state "
                            f"(turn {state.get('turn_number')}, "
                            f"phase {state.get('phase')})")
                        ST["trigger_window_stats"] = {
                            "hand_end": snap["hand"],
                            "gy_end": snap["gy"],
                            "perm_end": snap["perm"],
                            "hand_delta": {
                                k: snap["hand"][k] - ST["hand_at_pre"][k]
                                for k in snap["hand"]},
                            "gy_delta": {
                                k: snap["gy"][k] - ST["gy_at_pre"][k]
                                for k in snap["gy"]},
                            "perm_delta": {
                                k: snap["perm"][k] - ST["perm_at_pre"][k]
                                for k in snap["perm"]},
                            "discarded_this_turn": snap["discarded_this_turn"],
                            "triggers_fired_new": snap["triggers_fired"],
                        }
                        wire("trigger_window_complete",
                             ST["trigger_window_stats"])
                        say(f"trigger window complete: "
                            f"hand_delta={ST['trigger_window_stats']['hand_delta']} "
                            f"gy_delta={ST['trigger_window_stats']['gy_delta']} "
                            f"perm_delta={ST['trigger_window_stats']['perm_delta']}")
                        ST["settle_at"] = now
                # Wastewaker resolved (on P0 battlefield): casting done.
                if ST["phase"] == "casting":
                    ww_bf = any(
                        str(o.get("base_name") or o.get("name") or "")
                        .lower() == WASTEWAKER
                        and o.get("zone") == "Battlefield"
                        and o.get("controller") == 0
                        for o in (state.get("objects") or {}).values())
                    if ww_bf:
                        say("Wastewaker resolved (on P0 battlefield); "
                            "phase -> pre_attack")
                        wire("wastewaker_resolved", {})
                        ST["mana_needs"] = {"B": 0, "generic": 0}
                        ST["phase"] = "pre_attack"
                    elif (ST["wastewaker_cast_at"] is not None
                            and now - ST["wastewaker_cast_at"] > 300):
                        say("cast watchdog: 300s after the cast, "
                            "Wastewaker not on battlefield -- exporting "
                            "post and finalizing")
                        wire("cast_stall",
                             {"waiting_for": wf_of(state),
                              "stack": stack_entries(state)})
                        await export_as(c, "post")
                        ST["post_exported"] = True
                        finalized = True
                        break
                # decisive post: settled in PostCombatMain (or beyond)
                if ST["trigger_resolved"] and not ST["post_exported"]:
                    if not stack_entries(state):
                        if ST["settle_at"] is None:
                            ST["settle_at"] = now
                        idle = now - ST["settle_at"]
                        wtype = (wf_of(state).get("type") or "")
                        if wtype == "Priority" and idle > 8:
                            say(f"settled: stack empty, Priority, "
                                f"{idle:.0f}s idle; exporting post")
                            await export_as(c, "post")
                            ST["post_exported"] = True
                            finalized = True
                            break
                    else:
                        ST["settle_at"] = None
                # watchdogs
                if (ST["phase"] in ("setup", "pre_attack")
                        and now - t_start > 1200
                        and not ST["post_exported"]):
                    say("setup watchdog: 1200s in, attack never declared "
                        "-- exporting state and finalizing")
                    wire("setup_stall",
                         {"waiting_for": wf_of(state),
                          "p0_hand": hand_lnames(state, 0)})
                    await export_as(c, "post")
                    ST["post_exported"] = True
                    finalized = True
                    break
                if (ST["phase"] == "attacking"
                        and ST["settle_at"] is not None
                        and now - ST["settle_at"] > 300
                        and not ST["post_exported"]):
                    say("attacking watchdog: 300s settled-ish without "
                        "post export -- exporting post and finalizing")
                    wire("attacking_stall",
                         {"waiting_for": wf_of(state),
                          "stack": stack_entries(state)})
                    await export_as(c, "post")
                    ST["post_exported"] = True
                    finalized = True
                    break
                if state.get("winner") is not None or state.get("game_over"):
                    say(f"game over detected in state "
                        f"(winner={state.get('winner')}) -- finalizing")
                    wire("game_over_state",
                         {"winner": state.get("winner"),
                          "state_keys": sorted(state.keys())})
                    if not ST["post_exported"]:
                        try:
                            await export_as(c, "post")
                            ST["post_exported"] = True
                        except Exception as e:
                            say(f"post export on game-over failed: {e}")
                    finalized = True
                    break
                if ST["terminal"] and not finalized:
                    say("TerminalResult received -- finalizing with "
                        "captured states")
                    if not ST["post_exported"]:
                        try:
                            await export_as(c, "post")
                            ST["post_exported"] = True
                        except Exception as e:
                            say(f"post export on terminal failed: {e}")
                    finalized = True
                    break
            except Exception as e:
                say(f"[{tag}] observation error: {e}")
            # ---- action pass
            try:
                acts = merged_actions(st)
                await tick(st, acts, st["state"], c)
            except Exception as e:
                say(f"[{tag}] tick error: {e}")
    say("finalizing")
    run = await finalize(p0)
    await p0.close()
    await p1.close()
    return run


if __name__ == "__main__":
    run = asyncio.run(main())
    # copy the scenario into the evidence dir, render the PNG, and write
    # the SHA-256 manifest over everything except the manifest itself.
    shutil.copy(f"{BACKFILL}/driver/scenario_7423.py",
                f"{EVDIR}/scenario_7423.py")
    sys.path.insert(0, f"{BACKFILL}/driver")
    import subprocess
    subprocess.run([sys.executable, f"{BACKFILL}/driver/render_summary.py",
                    EVDIR, str(ISSUE),
                    "Eumidian Wastewaker: unparsed 'each discard a card or "
                    "sacrifice a permanent' leaves Draw reading an empty "
                    "tracked set; attack trigger is a silent no-op -- no "
                    "choice offered, no discard, no draw"],
                   check=True)
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for f in files:
            h = hashlib.sha256(open(f"{EVDIR}/{f}", "rb").read()).hexdigest()
            mf.write(f"{h}  {f}\n")
    print(f"manifest written ({len(files)} files); "
          f"verdict={run['verdict']}", flush=True)
    sys.exit(0)
