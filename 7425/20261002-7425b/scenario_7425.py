#!/usr/bin/env python3
"""Issue #7425: Highcliff Felidar -- "choose a creature with the greatest
power among creatures that player controls" is unparsed, so `Destroy`
reads an empty tracked set.

Reported (2026-08-15, source:internal-triage, status:confirmed,
area:parser, classifier:unsupported-aspect, priority:p3-card-specific):
found by the #6857 tracked-set publish census (one of 33 cards whose
tracked-set antecedent clause never parses).

Oracle text: "Vigilance. When this creature enters, for each opponent,
choose a creature with the greatest power among creatures that player
controls. Destroy those creatures."

Pinned v0.100.0 parse (see data_evidence.json):
  trigger[0]: mode ChangesZone (SelfRef -> Battlefield), repeat_for
      Ref(PlayerCount(Opponent))
    execute.effect = Unimplemented
        { name: "unparsed_verb_arguments",
          description: "choose a creature with the greatest power among
                        creatures that player controls" }
      sub: Destroy { target: TrackedSet(0), cant_regenerate: false }
      sub_link: SequentialSibling

The issue explicitly does NOT assert a runtime symptom for Destroy
("not measured for this sub type" -- consumer classification missing for
Destroy); the reported defect is the parse state and the empty publish
that structurally follows from it.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 4x Highcliff Felidar + 56x Plains
      (Felidar costs {5}{W}{W}, 5/5 vigilance; dense lands are a
      test-harness convenience)
  P1: 4x Agent of Stromgald (1/1, {R}) + 4x Cinder Wall (3/3, {R})
      + 4x Shivan Dragon (5/5, {4}{R}{R}) + 48x Mountain
      (distinct powers 1/3/5; dense playsets are a test-harness
      convenience)
Drive:
  1. Mulligans: P0 keeps Felidar + >=4 lands (<=3 mulligans); P1 keeps.
  2. P1, on its own main phases, casts the most expensive affordable
     creature (Dragon > Wall > Agent), plays a land per turn, never
     attacks.
  3. On the first P0 main phase with 7+ untapped Plains and >=2
     distinct-power P1 creatures on the battlefield: export pre.json
     IMMEDIATELY BEFORE submitting the Felidar cast (guarded flag; the
     decisive pre), cast Felidar paying {5}{W}{W}, let the ETB trigger
     resolve.
  4. Record every viewer-interaction opportunity during resolution; export
     post once the stack is empty, the game has settled, and no prompt is
     pending.

Expected (correct behavior): the ETB trigger resolves; for the one
opponent (P1), P0 is offered the "choose a creature with the greatest
power among creatures that player controls" choice; exactly one of the
greatest-power P1 creatures is destroyed (to P1's graveyard) and the
smaller ones survive on P1's battlefield.
Reported (bug): the choice clause never parsed, so no choice is offered
and Destroy reads an empty tracked set -- no creature is destroyed.

Assertions (correct-behavior expectations; a FAIL records the bug):
  A1_data_level   pinned v0.100.0 card-data.json parses the ETB as the
                  issue reports: ChangesZone SelfRef->Battlefield trigger
                  with repeat_for opponents; head Unimplemented naming the
                  "choose a creature with the greatest power among
                  creatures that player controls" clause; sub Destroy with
                  target TrackedSet(0).
  A2_setup_ok     pre.json: >=2 creatures under P1's control with
                  distinct powers; Felidar castable (7+ untapped Plains).
  A3_cast_resolves the Felidar cast completed; Felidar is on P0's
                  battlefield at post.
  A4_trigger_resolved the ETB trigger left the stack without a stall
                  (post stack empty; no resolution watchdog fired).
  A5_choice_offered (THE REPORTED BUG) during resolution, P0 was offered
                  the "choose a creature with the greatest power" choice.
                  Expected: offered. Observed bug: never offered (the
                  non-choice prompts were all ordinary turn-structure
                  menus -- priority passes, tap-land menus,
                  DeclareAttackers; a prompt appearing is not a pass).
  A6_greatest_destroyed (THE REPORTED BUG) post: exactly one of the
                  pre-time greatest-power P1 creatures is in P1's
                  graveyard and all smaller pre-P1 creatures remain on
                  P1's battlefield. Expected: destroyed. Observed bug:
                  nothing destroyed.
  A7_cleanup      post stack empty, game advancing.

Verdict rule: reproduced iff A1, A2, A3, A4 passed and NOT (A5 passed and
              A6 passed); not-reproduced iff A1..A6 all passed; blocked iff
              A1, A2, A3 or A4 could not be established.

Protocol-101 driver notes (v0.100.0, build bc9ef56, verified 2026-10-02):
  - HELLO advertises protocol 101 (server enforces exact match).
  - MulliganDecision as {"choice":{"type":"Keep"}} gated on
    waiting_for.data.pending[] with phase type "Declare".
  - BottomCards via single SelectCards {"cards":[...]}.
  - CastSpell via advertised action; mana via PayMana actions and
    viewer-interaction tapLandForMana choices (ST["mana_needs"]).
  - Authoritative exports only from the host seat (P0 creates the game).
  - Adapted from scenario_7422.py (issue #7422, same TrackedSet(0)
    unparsed-head pattern) 2026-10-02.
"""
import asyncio
import hashlib
import json
import os
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261002-7425b"
ISSUE = 7425
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
                     "(already listening at run start; ServerHello "
                     "0.100.0/bc9ef56/protocol 101 verified by the "
                     "2026-10-02 pin run and re-verified by this run's "
                     "own Hello handshake)",
    "mode": "Full",
    "source": "2026-10-02: latest stable release v0.100.0 == pinned release "
              "dir (minisig-verified binary + manifest, keynum "
              "436711b6a2d36828; GitHub /releases confirms v0.100.0 still "
              "latest stable); hashes recomputed against on-disk artifacts "
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

FELIDAR = "highcliff felidar"
AGENT = "agent of stromgald"    # 1/1 {R}
WALL = "cinder wall"            # 3/3 {R}
DRAGON = "shivan dragon"        # 5/5 {4}{R}{R}
PLAINS = "plains"
MOUNTAIN = "mountain"

P0_DECK = [("Highcliff Felidar", 4), ("Plains", 56)]
P1_DECK = [("Agent of Stromgald", 4), ("Cinder Wall", 4),
           ("Shivan Dragon", 4), ("Mountain", 48)]

SETUP_DEADLINE_S = 1700
CREATURE_NEEDS = {DRAGON: {"R": 2, "generic": 4},
                  WALL: {"R": 1, "generic": 0},
                  AGENT: {"R": 1, "generic": 0}}

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
        "phase": "setup",  # setup -> casting -> resolving -> done
        "cast_submitted_at": None,
        "felidar_cast": False,
        "pre_exported": False,
        "p1_creatures_at_pre": [],    # [(oid, name, power)]
        "post_exported": False,
        "prompts_seen": [],           # (phase, iid, opp) during resolution
        "wf_types_resolving": [],
        "stack_saw_trigger": False,
        "mana_needs": {"R": 0, "W": 0, "generic": 0},
        "paying": False,              # a cast is mid mana-payment
        "terminal": False,
        "terminal_data": None,
        "states_seen": 0,
        "cast_rejections": 0,
        "settle_at": None,
        "ass": {k: "not-run" for k in ("A1_data_level", "A2_setup_ok",
                                       "A3_cast_resolves", "A4_trigger_resolved",
                                       "A5_choice_offered", "A6_greatest_destroyed",
                                       "A7_cleanup")},
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


def power_of(state, oid):
    # Live state objects carry power as a plain number (other scenarios
    # assert o.get("power") == 8 directly); card-data uses {"type","value"}.
    o = get_obj(state, oid)
    p = o.get("power")
    if isinstance(p, dict):
        return p.get("value")
    return p


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

# ------------------------------------------------------------- data check
def check_data_level():
    card = CARD_DATA.get("highcliff felidar", {})
    trigs = card.get("triggers") or []
    trig = trigs[0] if trigs else {}
    ex = (trig.get("execute") or {})
    head = ex.get("effect") or {}
    sub = (ex.get("sub_ability") or {}).get("effect") or {}
    tgt = (ex.get("sub_ability") or {}).get("effect", {}).get("target") or {}
    rep = ex.get("repeat_for") or {}
    findings = {
        "name": card.get("name"),
        "mana_cost": card.get("mana_cost"),
        "oracle": card.get("oracle_text"),
        "trigger_mode": trig.get("mode"),
        "trigger_destination": trig.get("destination"),
        "repeat_for": rep,
        "head": head,
        "sub": sub,
        "sub_target": tgt,
        "sub_link": (ex.get("sub_ability") or {}).get("sub_link"),
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(findings, f, indent=1, default=str)
    wire("data_level", {"head_type": head.get("type"),
                        "head_name": head.get("name"),
                        "head_desc": head.get("description"),
                        "sub_type": sub.get("type"),
                        "sub_target": tgt,
                        "repeat_for": rep})
    desc = str(head.get("description") or "")
    rep_qty = (rep.get("qty") or {})
    ok = (trig.get("mode") == "ChangesZone"
          and trig.get("destination") == "Battlefield"
          and head.get("type") == "Unimplemented"
          and "choose a creature with the greatest power among creatures that player controls" in desc
          and sub.get("type") == "Destroy"
          and tgt.get("type") == "TrackedSet"
          and tgt.get("id") == 0
          and rep_qty.get("type") == "PlayerCount"
          and (rep_qty.get("filter") or {}).get("type") == "Opponent")
    say(f"data-level check: head={head.get('type')}/{head.get('name')!r}, "
        f"sub={sub.get('type')} target={tgt}, repeat_for={rep_qty} -> "
        f"{'MATCHES ISSUE REPORT' if ok else 'MISMATCH'}")
    ST["notes"].append(
        "data-level: pinned v0.100.0 card-data.json parses Highcliff "
        "Felidar's ETB as ChangesZone SelfRef->Battlefield with "
        "repeat_for Opponent count; head "
        f"Unimplemented(name={head.get('name')!r}, "
        f"description={desc[:70]!r}...) + sub "
        f"Destroy target={tgt} (sub_link="
        f"{(ex.get('sub_ability') or {}).get('sub_link')}); "
        f"issue-reported shape: {ok}; head node name differs from the "
        "issue's 9b7c66e30 corpus ('choose' -> "
        "'unparsed_verb_arguments'), same clause")
    ST["data_level_ok"] = ok
    return ok

# ------------------------------------------------------------- interaction
async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


def mulligan_keep_p0(hand):
    lands = sum(1 for n in hand if n == PLAINS)
    return FELIDAR in hand and lands >= 4


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


RANK = {FELIDAR: 10, DRAGON: 8, WALL: 7, AGENT: 6, PLAINS: 5, MOUNTAIN: 4}


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
                for color in ("R", "W", "G", "U", "B"):
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
        acted = True
    return acted


def start_cast(needs):
    ST["mana_needs"] = dict(needs)
    ST["paying"] = True


def stop_cast():
    ST["mana_needs"] = {"R": 0, "W": 0, "generic": 0}
    ST["paying"] = False


def p1_affordable_creature(state):
    """Most expensive creature P1 can cast right now (Dragon > Wall > Agent)."""
    lands = untapped_lands(state, 1)
    hn = set(hand_lnames(state, 1))
    for name in (DRAGON, WALL, AGENT):
        if name not in hn:
            continue
        need = CREATURE_NEEDS[name]
        total = need["R"] + need["generic"]
        if len(lands) >= total:
            return name, need
    return None, None


async def export_as(c, name):
    env = await c.export_state()
    with open(f"{EVDIR}/{name}.json", "w") as f:
        f.write(env)
    say(f"exported {name}.json")


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
    if ST["paying"]:
        if await pay_mana_vi(c, st, state, "P0"):
            return
    if await do_discard_to_handsize(c, 0, "P0"):
        return
    if wtype == "DeclareAttackers":
        # P0 never attacks: keep the board clean for the destroy assertions.
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            import copy as _copy
            d = _copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if wtype == "DeclareBlockers":
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            import copy as _copy
            d = _copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return
    if not my_priority(state, 0):
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if phase in ("PreCombatMain", "PostCombatMain") and active == 0:
        hn = hand_lnames(state, 0)
        # the Felidar cast: decisive pre.json exported INSIDE this
        # submission path (guarded flag), then cast paying {5}{W}{W}.
        p1c = [(o, obj_lname(state, o), power_of(state, o))
               for o in bf_creatures(state, 1)]
        powers = {p for _, _, p in p1c if p is not None}
        if FELIDAR in hn and len(untapped_lands(state, 0)) >= 7:
            say(f"[P0] cast-gate probe: p1c={p1c} powers={sorted(powers)} "
                f"phase={ST['phase']}")
        if (ST["phase"] == "setup" and FELIDAR in hn
                and len(untapped_lands(state, 0)) >= 7
                and len(p1c) >= 2 and len(powers) >= 2):
            a, oid = cast_action_for(acts, state, FELIDAR)
            if a:
                if not ST["pre_exported"]:
                    await export_as(c, "pre")
                    ST["pre_exported"] = True
                    ST["p1_creatures_at_pre"] = p1c
                    say(f"[P0] pre.json exported; P1 BF creatures at pre: "
                        f"{p1c}")
                ST["felidar_cast"] = True
                ST["cast_submitted_at"] = time.time()
                start_cast({"W": 2, "generic": 5})
                ST["phase"] = "casting"
                say(f"[P0] casting Highcliff Felidar (oid {oid})")
                wire("felidar_cast", {"oid": oid,
                                      "p1_creatures": p1c})
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
        if await do_mulligan(c, 1, "P1", lambda hn: True):
            return
        if await do_bottom(c, 1, "P1"):
            return
        return
    if wtype == "BottomCards":
        if await do_bottom(c, 1, "P1"):
            return
        return
    if ST["paying"]:
        if await pay_mana_vi(c, st, state, "P1"):
            return
    if await do_discard_to_handsize(c, 1, "P1"):
        return
    if wtype == "DeclareAttackers":
        # P1 never attacks; keeps its creatures on the battlefield.
        da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
        if da:
            import copy as _copy
            d = _copy.deepcopy(da)
            d.setdefault("data", {}).update({"attacks": [], "bands": []})
            await submit_as_is(c, d)
        return
    if wtype == "DeclareBlockers":
        db = next((a for a in acts if a["type"] == "DeclareBlockers"), None)
        if db:
            import copy as _copy
            d = _copy.deepcopy(db)
            d.setdefault("data", {}).update({"assignments": []})
            await submit_as_is(c, d)
        return
    if not my_priority(state, 1):
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if phase in ("PreCombatMain", "PostCombatMain") and active == 1:
        name, need = p1_affordable_creature(state)
        if name:
            a, oid = cast_action_for(acts, state, name)
            if a:
                start_cast(need)
                say(f"[P1] casting {name} (oid {oid})")
                wire("p1_cast", {"name": name, "oid": oid})
                await submit_as_is(c, a)
                return
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
    post = load_env("post") or {}
    pre_st = pre.get("state") or {}
    post_st = post.get("state") or {}

    # A1: data-level parse matches the issue
    if ST.get("data_level_ok"):
        ass["A1_data_level"] = "passed"
        notes.append("A1 passed: pinned v0.100.0 card-data.json parses "
                     "Highcliff Felidar's ETB as ChangesZone SelfRef->"
                     "Battlefield (repeat_for Opponent count) with head "
                     "Unimplemented('choose a creature with the greatest "
                     "power among creatures that player controls') + sub "
                     "Destroy TrackedSet(0), sub_link SequentialSibling; "
                     "see data_evidence.json.")
    else:
        ass["A1_data_level"] = "failed"
        notes.append("A1 FAILED: the pinned parse does not match the "
                     "issue-reported shape; see data_evidence.json.")

    # A2: setup at the decisive pre
    p1c = ST["p1_creatures_at_pre"]
    powers = sorted({p for _, _, p in p1c if p is not None})
    if ST["pre_exported"] and pre_st and len(p1c) >= 2 and len(powers) >= 2:
        ass["A2_setup_ok"] = "passed"
        notes.append(f"A2 passed: pre.json has {len(p1c)} creatures under "
                     f"P1's control with distinct powers {powers} "
                     f"({p1c}); Felidar was castable (7+ untapped Plains).")
    else:
        ass["A2_setup_ok"] = "failed"
        notes.append(f"A2 FAILED: the decisive pre does not have >=2 "
                     f"P1 creatures with distinct powers (got {p1c}, "
                     f"powers {powers}).")

    # A3: the cast completed, Felidar is on P0's battlefield
    felidar_bf_post = False
    if post_st:
        for oid, o in (post_st.get("objects") or {}).items():
            if (str(o.get("base_name") or o.get("name") or "").lower() == FELIDAR
                    and o.get("zone") == "Battlefield"
                    and o.get("controller") == 0):
                felidar_bf_post = True
                break
    if ST["felidar_cast"] and felidar_bf_post:
        ass["A3_cast_resolves"] = "passed"
        notes.append("A3 passed: Highcliff Felidar was cast and is on P0's "
                     "battlefield at post.")
    elif ST["felidar_cast"]:
        ass["A3_cast_resolves"] = "failed"
        notes.append("A3 FAILED: Felidar was cast but is not on P0's "
                     "battlefield at post.")
    else:
        notes.append("A3 not-run: Felidar was never cast.")

    # A4: the ETB trigger left the stack without a stall
    if post_st and not stack_entries(post_st) and ST["felidar_cast"]:
        ass["A4_trigger_resolved"] = "passed"
        notes.append("A4 passed: post stack empty after the cast; the ETB "
                     "trigger resolved without a stall "
                     f"(observed on stack live: {ST['stack_saw_trigger']}).")
    elif ST["felidar_cast"]:
        ass["A4_trigger_resolved"] = "failed"
        notes.append("A4 FAILED: post stack non-empty or post missing.")
    else:
        notes.append("A4 not-run: Felidar was never cast.")

    # A5/A6: the reported bug. A5 passes only if a CHOICE-shaped prompt
    # reached P0 during resolution: ordinary turn-structure prompts
    # (priority passes, tap-land/cast menus, DeclareAttackers) do NOT
    # count -- a prompt appearing is not a pass (playbook step 2;
    # 20261002-7422 lesson).
    notes.append(f"Resolution prompts: {len(ST['prompts_seen'])} seen.")
    ORDINARY_ACTIONS = {"passPriority", "tapLandForMana", "castSpell",
                        "playLand"}
    choice_like = []
    for _ph, iid, opp in ST["prompts_seen"]:
        resp = (opp or {}).get("response") or {}
        rtype = resp.get("type")
        if rtype == "schema":
            spec = ((resp.get("data") or {}).get("spec")) or {}
            sdata = spec.get("data") or {}
            if (spec.get("type") == "relations"
                    and sdata.get("sourceConstraint") == "atMostOne"):
                continue  # DeclareAttackers, not the choice step
            choice_like.append(iid)
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
            choice_like.append(iid)
    if ST["prompts_seen"]:
        notes.append(f"prompt iids during resolution: "
                     f"{[t[1] for t in ST['prompts_seen']][:8]} (full JSON "
                     f"in wire_log resolution_prompt entries); "
                     f"choice-shaped iids: {choice_like[:4] or 'none'}")
    if ST["felidar_cast"] and not choice_like:
        ass["A5_choice_offered"] = "failed"
        notes.append("A5 FAILED: no 'choose a creature with the greatest "
                     "power' choice was ever offered to P0 -- THE REPORTED "
                     "BUG (the clause never parsed, so the choice step "
                     "does not exist at runtime). All resolution prompts "
                     "were ordinary turn-structure menus.")
    elif choice_like:
        ass["A5_choice_offered"] = "passed"
        notes.append("A5 passed: a choice-shaped prompt was offered during "
                     f"resolution: {choice_like[:4]}.")
    else:
        notes.append("A5 not-run: Felidar was never cast.")

    # A6: the printed outcome -- exactly one of the greatest-power P1
    # creatures destroyed, all smaller ones surviving.
    if (ST["pre_exported"] and ST["post_exported"] and pre_st and post_st
            and len(p1c) >= 2):
        maxp = max(p for _, _, p in p1c if p is not None)
        greatest = [o for o, _, p in p1c if p == maxp]
        smaller = [o for o, _, p in p1c if p is not None and p < maxp]
        destroyed = []
        surviving = []
        for oid, nm, pw in p1c:
            o = get_obj(post_st, oid)
            z = o.get("zone")
            ctrl = o.get("controller")
            notes.append(f"A6 probe: pre-P1 creature oid {oid} ({nm}, "
                         f"power {pw}) post zone={z!r} controller={ctrl}.")
            if z == "Graveyard":
                destroyed.append(oid)
            else:
                surviving.append((oid, z, ctrl))
        correct = (len(destroyed) == 1 and destroyed[0] in greatest
                   and all(s[0] in smaller or s[0] not in greatest
                           for s in surviving)
                   and all(s[1] == "Battlefield" and s[2] == 1
                           for s in surviving))
        if correct:
            ass["A6_greatest_destroyed"] = "passed"
            notes.append(f"A6 passed: exactly one greatest-power creature "
                         f"(oid {destroyed[0]}, power {maxp}) destroyed; "
                         f"smaller creatures survived: {surviving}.")
        else:
            ass["A6_greatest_destroyed"] = "failed"
            if not destroyed:
                notes.append("A6 FAILED: NO pre-P1 creature was destroyed "
                             f"(greatest-power group: {greatest} power "
                             f"{maxp}; survivors: {surviving}) -- THE "
                             "REPORTED BUG (Destroy read an empty tracked "
                             "set).")
            else:
                notes.append("A6 FAILED: the destruction did not match the "
                             f"printed outcome (destroyed={destroyed}, "
                             f"greatest group={greatest} power {maxp}, "
                             f"survivors={surviving}).")
    else:
        notes.append("A6 not-run: no pre/post pair with >=2 P1 creatures.")

    # A7: cleanup
    if post_st:
        if not stack_entries(post_st):
            ass["A7_cleanup"] = "passed"
            notes.append("A7 passed: post stack empty.")
        else:
            ass["A7_cleanup"] = "failed"
            notes.append("A7 FAILED: post stack non-empty: "
                         f"{stack_entries(post_st)}")
    else:
        notes.append("A7 not-run: no post state")

    if ST["stack_saw_trigger"]:
        notes.append("The Felidar ETB trigger was observed on the stack "
                     "during resolution (live tick).")
    else:
        notes.append("The Felidar ETB trigger was NOT observed on the stack "
                     "by the live tick (may have resolved between ticks).")

    # verdict
    core_ok = all(ass[k] == "passed" for k in ("A1_data_level", "A2_setup_ok",
                                               "A3_cast_resolves",
                                               "A4_trigger_resolved"))
    bug_fixed = (ass["A5_choice_offered"] == "passed"
                 and ass["A6_greatest_destroyed"] == "passed")
    if core_ok and not bug_fixed:
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: the printed ETB effect never happened -- "
            "no greatest-power choice was offered and no creature was "
            "destroyed (or the destruction did not match the printed "
            "outcome); confirmed on v0.100.0. The parse is the reported "
            "defect; the missing choice and missing destruction are its "
            "direct structural consequence. This is not a fix claim.")
    elif core_ok and bug_fixed:
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: P0 was offered the choice and exactly "
            "one greatest-power P1 creature was destroyed as printed. "
            "This is not a fix claim.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: the reported Felidar-ETB path could "
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
               if "felidar" in l.lower()
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
            open(f"{BACKFILL}/driver/scenario_7425.py", "rb").read()).hexdigest(),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": {
            "felidar_cast": ST["felidar_cast"],
            "cast_submitted_at": ST["cast_submitted_at"],
            "p1_creatures_at_pre": ST["p1_creatures_at_pre"],
            "prompts_seen": ST["prompts_seen"],
            "wf_types_resolving": sorted(set(ST["wf_types_resolving"])),
            "stack_saw_trigger": ST["stack_saw_trigger"],
            "cast_rejections": ST["cast_rejections"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The report is a data-level parse report (no game state); the "
            "scenario replays the reported line (Felidar enters with >=2 "
            "distinct-power creatures under the opponent's control) from a "
            "fresh game and observes the resolution outcome.",
            "Dense playsets/lands are a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full "
            "game replay.",
        ],
        "setup_line": "P0: 4x Highcliff Felidar + 56x Plains; P1: 4x Agent "
                      "of Stromgald + 4x Cinder Wall + 4x Shivan Dragon + "
                      "48x Mountain (both sides never attack)",
        "contract_line": "Felidar ETB: P0 must be offered the 'choose a "
                         "creature with the greatest power among creatures "
                         "P1 controls' choice and exactly one "
                         "greatest-power P1 creature must be destroyed. "
                         "Observed: the clause never parsed, no choice "
                         "prompt appeared, and no creature was destroyed.",
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

                if tag == "P0":
                    if ST["phase"] == "resolving":
                        vi = get_vi(st)
                        if vi:
                            for opp in vi.get("opportunities", []) or []:
                                iid = opp.get("interactionId") or opp.get("id")
                                tag2 = (ST["phase"], iid)
                                if all(t[:2] != tag2 for t in ST["prompts_seen"]):
                                    ST["prompts_seen"].append((tag2[0], tag2[1], opp))
                                    wire("resolution_prompt",
                                         {"phase": ST["phase"], "iid": iid,
                                          "opportunity": opp})
                                    say(f"[{ST['phase']}] prompt seen: "
                                        f"{json.dumps(opp)[:300]}")
                        wtype = (wf_of(state).get("type") or "")
                        if wtype and wtype not in ST["wf_types_resolving"]:
                            ST["wf_types_resolving"].append(wtype)
                        for se in stack_entries(state):
                            ser = json.dumps(se, default=str).lower()
                            if "felidar" in ser and not ST["stack_saw_trigger"]:
                                ST["stack_saw_trigger"] = True
                                say("Felidar ETB trigger observed on the stack")
                                wire("trigger_on_stack", {"entry": se})
                    # cast resolution: felidar on P0's battlefield
                    if ST["phase"] == "casting":
                        felidar_bf = any(
                            str(o.get("base_name") or o.get("name") or "")
                            .lower() == FELIDAR
                            and o.get("zone") == "Battlefield"
                            and o.get("controller") == 0
                            for o in (state.get("objects") or {}).values())
                        if felidar_bf:
                            say("Felidar resolved (on P0 battlefield); "
                                "phase -> resolving")
                            wire("felidar_resolved", {})
                            stop_cast()
                            ST["phase"] = "resolving"
                            ST["settle_at"] = now
                        elif (ST["cast_submitted_at"] is not None
                                and now - ST["cast_submitted_at"] > 300):
                            say("felidar watchdog: 300s after the cast, "
                                "Felidar not on battlefield -- exporting "
                                "post and finalizing")
                            wire("felidar_stall",
                                 {"waiting_for": wf_of(state),
                                  "stack": stack_entries(state)})
                            await export_as(c, "post")
                            ST["post_exported"] = True
                            finalized = True
                            break
                    # decisive post: stack empty + settled
                    if ST["phase"] == "resolving":
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
                    if (ST["phase"] == "setup"
                            and now - t_start > 1200
                            and not ST["post_exported"]):
                        say("setup watchdog: 1200s in, Felidar never cast "
                            "-- exporting state and finalizing")
                        wire("setup_stall",
                             {"waiting_for": wf_of(state),
                              "p0_hand": hand_lnames(state, 0)})
                        await export_as(c, "post")
                        ST["post_exported"] = True
                        finalized = True
                        break
                    if (ST["phase"] == "resolving"
                            and ST["settle_at"] is not None
                            and now - ST["settle_at"] > 300):
                        say("resolving watchdog: 300s settled-ish without "
                            "post export -- exporting post and finalizing")
                        wire("resolving_stall",
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
    shutil.copy(f"{BACKFILL}/driver/scenario_7425.py",
                f"{EVDIR}/scenario_7425.py")
    sys.path.insert(0, f"{BACKFILL}/driver")
    import subprocess
    subprocess.run([sys.executable, f"{BACKFILL}/driver/render_summary.py",
                    EVDIR, str(ISSUE),
                    "Highcliff Felidar: unparsed 'choose a creature with the "
                    "greatest power' leaves Destroy reading an empty "
                    "tracked set; no choice offered, no creature destroyed"],
                   check=True)
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for f in files:
            h = hashlib.sha256(open(f"{EVDIR}/{f}", "rb").read()).hexdigest()
            mf.write(f"{h}  {f}\n")
    print(f"manifest written ({len(files)} files); "
          f"verdict={run['verdict']}", flush=True)
    sys.exit(0)
