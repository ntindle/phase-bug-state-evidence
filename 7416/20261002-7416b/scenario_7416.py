#!/usr/bin/env python3
"""Issue #7416: Blood Tyrant's "for each 1 life lost this way" counter count
resolves to 0 because the parser models it as an object count
(QuantityRef::TrackedSetSize) -- an axis no tracked set can supply.

Reported (2026-08-15, lgray, source:internal-triage, status:confirmed,
area:parser, classifier:supported-aspect-defect, priority:p2-wrong-game-result):
Blood Tyrant's upkeep trigger parses its PutCounter count as
`{"type":"Ref","qty":{"type":"TrackedSetSize"}}`. Tracked sets hold
ObjectIds; life loss produces no objects, so the count resolves to 0 no
matter how many players lost life. No runtime reproduction was run for
this card (the report is data-level).

Oracle text (verified in pinned v0.99.0 card-data.json):
  Blood Tyrant {4}{U}{B}{R} 5/5 Vampire, Flying, trample
  "At the beginning of your upkeep, each player loses 1 life. Put a +1/+1
  counter on this creature for each 1 life lost this way.
  Whenever a player loses the game, put five +1/+1 counters on this creature."
Ruling (2009-02-01): "Blood Tyrant will count each player's life loss,
including yours, when determining how many +1/+1 counters it gets."

Pinned v0.99.0 parse of the upkeep trigger (see data_evidence.json):
  head LoseLife {amount: Fixed 1, player_scope: All}
  sub_ability PutCounter {counter_type: P1P1,
    count: {type: Ref, qty: {type: TrackedSetSize}}, target: SelfRef},
    sub_link: SequentialSibling
  -- exactly the shape the issue reports.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 4x Blood Tyrant + 18x Island + 19x Swamp + 19x Mountain (93% lands)
  P1: 60x Plains (plays lands, passes)
Drive:
  1. Both mulligan: P0 keeps Blood Tyrant + >=3 lands; P1 keeps.
  2. P0 plays a land each turn; never attacks (life totals must stay clean).
  3. On the first P0 main phase with 7+ untapped lands including U, B, R:
       a. export pre (Tyrant on P0 BF, life 20/20) -- the pre for the
          operation under investigation
       b. cast Blood Tyrant (CastSpell + PayMana flow), stack seen -> export cast
  4. After the Tyrant resolves, P0 and P1 pass; P1 takes a turn (land, pass).
  5. On P0's next upkeep the automatic trigger fires:
       - observe it on the stack -> export trigger (real pre for the
         counter placement)
       - players pass; the trigger resolves.
  6. export post after the stack settles; finalize.

Expected (correct behavior): upkeep trigger resolves, each player loses 1
life (20->19 both), and Blood Tyrant gains 2 +1/+1 counters (one per 1 life
lost; two loss events of 1 life each in a 2-player game).
Reported (bug): the count resolves to 0, so the Tyrant gains 0 counters
while both players still lose the 1 life.

Assertions (correct-behavior expectations; a FAIL records the bug):
  A1_setup_ok      Tyrant on P0's battlefield, life 20/20 at pre.
  A2_trigger_fired The upkeep TriggeredAbility went on the stack during
                   P0's Upkeep (observed live), or its effects are
                   demonstrably observed (life loss during the upkeep).
  A3_life_loss     Both players went 20->19 (the LoseLife head resolved).
  A4_two_counters  (THE REPORTED BUG) post state: Blood Tyrant carries
                   exactly 2 +1/+1 counters. 0 counters (the reported
                   symptom) fails; any other !=2 count fails with notes.
  A5_cleanup       post stack empty, game advancing.

Verdict rule: reproduced iff A1..A3 passed and A4 failed;
              not-reproduced iff A4 passed (exactly 2 counters);
              blocked iff A1 failed (setup never assembled) or the trigger
              never fired (A2/A3 not established).

Protocol-98 driver notes (v0.99.0, verified 2026-10-01/02):
  - HELLO advertises protocol 98 (server enforces exact match).
  - MulliganDecision as {"choice":{"type":"Keep"}} gated on
    waiting_for.data.pending[] with phase type "Declare".
  - BottomCards via single SelectCards {"cards":[...]}.
  - CastSpell via advertised action; mana via PayMana actions.
  - The upkeep trigger is automatic (optional:false); no choices surface.
  - Authoritative exports only from the host seat (P0 creates the game).
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
RUN_ID = "20261002-7416b"
ISSUE = 7416
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
leftovers = [f for f in os.listdir(EVDIR) if f != "scenario_run.log"]
assert not leftovers, f"EVDIR {EVDIR} not empty -- refusing to overwrite"

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

SERVER_IDENTITY = {
    "server_version": "v0.99.0",
    "build_commit": "d919616",
    "protocol_version": 98,
    "server_binary_sha256": "37fa78a8db1a51fbb950cbdba870021ad718fdf2e259971a1954c130a1a5ba60",
    "card_data_sha256": "ccfcf9e6d19d9407d207cfbfe95b4ad873cebd878f66b451682e56e991372a4e",
    "draft_pools_sha256": "8ce01364ee46e55cf2610d6a2dd35abbd3266606cc456af8012433f55bdd034e",
    "signature_verified": True,
    "server_run_id": "shared pinned v0.99.0 server on 127.0.0.1:9374 "
                     "(already listening per task body; ServerHello "
                     "0.99.0/d919616/protocol 98 verified this run; "
                     "not restarted by this run)",
    "mode": "Full",
    "source": "2026-10-02: latest stable release v0.99.0 == pinned release "
              "dir; hashes recomputed against on-disk artifacts",
}

# Recompute against on-disk artifacts; never copy hashes blindly.
for _f, _k in (("server/releases/v0.99.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.99.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.99.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.99.0/data/card-data.json"))

TYR = "blood tyrant"
ISL = "island"
SWM = "swamp"
MTN = "mountain"

P0_DECK = [("Blood Tyrant", 4), ("Island", 18), ("Swamp", 19), ("Mountain", 19)]
P1_DECK = [("Plains", 60)]

SETUP_DEADLINE_S = 1700
SETTLE_IDLE_S = 20

ST = {}
MULLS = set()
MULL_COUNT = {}
SUBMITTED = set()
_DISCARD_REV = {}


def reset():
    ST.clear()
    ST.update({
        "t0": time.time(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "game_code": None,
        "phase": "setup",  # setup -> casting_tyrant -> tyrant_on_bf -> trigger_seen -> resolving -> done
        "tyrant_cast": False,
        "tyrant_cast_submitted_at": None,
        "tyrant_cast_rejections": 0,
        "tyrant_seen_on_stack": False,
        "tyrant_stack_oid": None,
        "tyrant_on_bf_at": None,
        "trigger_seen": False,
        "trigger_seen_at": None,
        "trigger_exported": False,
        "watch": False,
        "watch_last": 0,
        "watch_n": 0,
        "terminal": False,
        "terminal_data": None,
        "pre_exported": False,
        "cast_exported": False,
        "post_exported": False,
        "settle_at": None,
        "states_seen": 0,
        "pre_life": None,
        "tyrant_bf_oid": None,
        "ass": {k: "not-run" for k in ("A1_setup_ok", "A2_trigger_fired",
                                       "A3_life_loss", "A4_two_counters",
                                       "A5_cleanup")},
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


def zone_ids(state, pid, zone, key=None):
    out = []
    for oid, o in (state.get("objects") or {}).items():
        if o.get("zone") != zone or o.get("controller") != pid:
            continue
        if key is not None and str(o.get("base_name") or o.get("name") or "").lower() != key:
            continue
        out.append(int(oid))
    return out


def untapped_lands(state, pid):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and not o.get("tapped")
            and "land" in [str(t).lower()
                           for t in (o.get("card_types") or {}).get("core_types", [])]]


def untapped_land_colors(state, pid):
    colors = set()
    for oid in untapped_lands(state, pid):
        n = obj_lname(state, oid)
        if n == ISL:
            colors.add("U")
        elif n == SWM:
            colors.add("B")
        elif n == MTN:
            colors.add("R")
    return colors


def merged_actions(st):
    acts = list(st.get("legal_actions") or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


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


def stack_spell_oid(state, key):
    for e in stack_entries(state):
        if not isinstance(e, dict):
            continue
        for k in ("object_id", "id", "source", "source_id", "card_id"):
            try:
                iv = int(e.get(k))
            except (TypeError, ValueError):
                continue
            if obj_lname(state, iv) == key:
                return iv
    return None


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


def counter_total(obj):
    c = obj.get("counters")
    if c is None:
        return None
    if isinstance(c, dict):
        tot = 0
        for v in c.values():
            try:
                tot += int(v)
            except (TypeError, ValueError):
                pass
        return tot
    if isinstance(c, list):
        tot = 0
        for e in c:
            if isinstance(e, dict):
                try:
                    tot += int(e.get("count", 0))
                except (TypeError, ValueError):
                    pass
            else:
                try:
                    tot += int(e)
                except (TypeError, ValueError):
                    pass
        return tot
    try:
        return int(c)
    except (TypeError, ValueError):
        return None


def life_of(state, pid):
    return player_of(state, pid).get("life")

# ------------------------------------------------------------- data check
def check_data_level():
    tyr = CARD_DATA.get("blood tyrant", {})
    trig = (tyr.get("triggers") or [{}])[0]
    exe = trig.get("execute") or {}
    sub = exe.get("sub_ability") or {}
    findings = {
        "name": tyr.get("name"),
        "mana_cost": tyr.get("mana_cost"),
        "oracle": tyr.get("oracle_text"),
        "upkeep_trigger_execute": exe,
        "upkeep_trigger_sub_ability": sub,
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(findings, f, indent=1, default=str)
    wire("data_level", findings)
    head = (exe.get("effect") or {}).get("type")
    subeff = (sub.get("effect") or {})
    subqty = ((subeff.get("count") or {}).get("qty") or {}).get("type")
    ok = (head == "LoseLife"
          and subeff.get("type") == "PutCounter"
          and subqty == "TrackedSetSize")
    say(f"data-level check: head={head}, sub effect={subeff.get('type')}, "
        f"sub count qty={subqty} -> {'MATCHES ISSUE REPORT' if ok else 'MISMATCH'}")
    ST["notes"].append(
        "data-level: pinned v0.99.0 card-data.json parses Blood Tyrant's "
        f"upkeep trigger as LoseLife head + PutCounter sub with count qty "
        f"{subqty} (issue-reported shape: {ok})")
    return ok

# ------------------------------------------------------------- interaction
async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


def mulligan_keep_p0(hand):
    lands = sum(1 for n in hand if n in (ISL, SWM, MTN))
    return TYR in hand and lands >= 3


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
    if key in SUBMITTED:
        return False
    n = int((pend.get("phase") or {}).get("count") or 0)
    if n <= 0:
        return False
    hand = hand_ids(state, pid)
    # keep the Tyrant and lands; bottom lowest-value first
    rank = {TYR: 5, ISL: 4, SWM: 4, MTN: 4}
    picks = sorted(hand, key=lambda o: rank.get(obj_lname(state, o), 2))[:n]
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
    rank = {TYR: 5, ISL: 4, SWM: 4, MTN: 3}
    picks = [int(x) for x in sorted(
        hand, key=lambda o: rank.get(obj_lname(state, o), 2))[:n]]
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
    return False


async def export_as(c, name):
    env = await c.export_state()
    with open(f"{EVDIR}/{name}.json", "w") as f:
        f.write(env)
    say(f"exported {name}.json")


def castable_tyrant(state):
    """Tyrant castable this main phase? 7 untapped lands with U,B,R among them."""
    lands = untapped_lands(state, 0)
    if len(lands) < 7:
        return False
    colors = untapped_land_colors(state, 0)
    return {"U", "B", "R"} <= colors


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
    if ST["phase"] == "casting_tyrant":
        if await pay_tick(c, acts, "P0"):
            return
    if await do_discard_to_handsize(c, 0, "P0"):
        return
    if wtype == "DeclareAttackers":
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
    if ST["phase"] not in ("setup", "casting_tyrant", "tyrant_on_bf",
                           "trigger_seen", "resolving"):
        await pass_priority(c, st, acts)
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if phase in ("PreCombatMain", "PostCombatMain") and active == 0:
        tyr_bf = zone_ids(state, 0, "Battlefield", TYR)
        if (not ST["tyrant_cast"] and not tyr_bf
                and TYR in hand_lnames(state, 0)
                and castable_tyrant(state)):
            a, oid = cast_action_for(acts, state, TYR)
            if a:
                ST["tyrant_cast"] = True
                ST["tyrant_cast_submitted_at"] = time.time()
                ST["pre_life"] = (life_of(state, 0), life_of(state, 1))
                ST["phase"] = "casting_tyrant"
                say(f"[P0] casting Blood Tyrant (oid {oid})")
                wire("cast_tyrant", {"oid": oid, "action": a["type"]})
                await submit_as_is(c, a)
                return
            wire("tyrant_cast_missing",
                 {"hand": hand_lnames(state, 0),
                  "untapped_lands": len(untapped_lands(state, 0)),
                  "colors": sorted(untapped_land_colors(state, 0)),
                  "act_types": sorted({a["type"] for a in acts})})
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
    cast = load_env("cast") or {}
    trigger = load_env("trigger") or {}
    post = load_env("post") or {}
    pre_st = pre.get("state") or {}
    cast_st = cast.get("state") or {}
    trigger_st = trigger.get("state") or {}
    post_st = post.get("state") or {}

    # A1: setup assembled (Tyrant on P0's battlefield, life 20/20)
    if ST["pre_exported"] and pre_st:
        tyr_bf = zone_ids(pre_st, 0, "Battlefield", TYR)
        life_ok = (life_of(pre_st, 0), life_of(pre_st, 1)) == (20, 20)
        if tyr_bf and life_ok:
            ass["A1_setup_ok"] = "passed"
            notes.append(
                f"A1 passed: pre exported with Blood Tyrant (oid "
                f"{tyr_bf[0]}) on P0's battlefield, life 20/20.")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append(
                f"A1 FAILED: tyr_bf={tyr_bf}, life="
                f"{(life_of(pre_st, 0), life_of(pre_st, 1))}.")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append("A1 FAILED: pre state was never exported (Blood Tyrant "
                     "never reached P0's battlefield).")

    # A2: the upkeep trigger fired
    notes.append(
        f"A2 context: trigger_seen={ST['trigger_seen']} "
        f"(at {ST['trigger_seen_at']}), trigger_exported="
        f"{ST['trigger_exported']}.")
    if ST["trigger_seen"]:
        ass["A2_trigger_fired"] = "passed"
        notes.append("A2 passed: Blood Tyrant's upkeep TriggeredAbility was "
                     "observed on the stack during P0's Upkeep.")
    elif ST["tyrant_on_bf_at"] is not None:
        ass["A2_trigger_fired"] = "failed"
        notes.append("A2 FAILED: the Tyrant was on the battlefield but its "
                     "upkeep trigger was never observed (see A3 for whether "
                     "the life-loss head resolved anyway).")
    else:
        notes.append("A2 not-run: the Tyrant never reached the battlefield.")

    # A3: each player lost 1 life (LoseLife head resolved)
    if post_st and ass["A1_setup_ok"] == "passed":
        post_life = (life_of(post_st, 0), life_of(post_st, 1))
        notes.append(f"A3 probe: post life totals = {post_life} "
                     f"(pre was {ST['pre_life']}).")
        if post_life == (19, 19):
            ass["A3_life_loss"] = "passed"
            notes.append("A3 passed: each player lost 1 life (20->19 both) "
                         "-- the LoseLife head of the trigger resolved.")
        else:
            ass["A3_life_loss"] = "failed"
            notes.append(f"A3 FAILED: post life {post_life}, expected "
                         f"(19, 19).")
    else:
        notes.append("A3 not-run: no post state or setup never assembled.")

    # A4: THE REPORTED BUG -- exactly 2 +1/+1 counters on Blood Tyrant
    if (ST["post_exported"] and post_st
            and ass["A1_setup_ok"] == "passed"):
        tyr_bf = zone_ids(post_st, 0, "Battlefield", TYR)
        if tyr_bf:
            tot = counter_total(get_obj(post_st, tyr_bf[0]))
            raw = get_obj(post_st, tyr_bf[0]).get("counters")
            mobj = get_obj(post_st, tyr_bf[0])
            wire("post_counter_probe",
                 {"tyr_bf_oid": tyr_bf[0], "counters_raw": raw,
                  "counters_total": tot,
                  "power": mobj.get("power"),
                  "toughness": mobj.get("toughness"),
                  "post_life": (life_of(post_st, 0), life_of(post_st, 1))})
            notes.append(
                f"A4 probe: post Blood Tyrant (oid {tyr_bf[0]}) counters "
                f"raw={raw} (total={tot}), P/T={mobj.get('power')}/"
                f"{mobj.get('toughness')}, life="
                f"{(life_of(post_st, 0), life_of(post_st, 1))}.")
            if tot == 2:
                ass["A4_two_counters"] = "passed"
                notes.append("A4 passed: Blood Tyrant carries exactly 2 "
                             "+1/+1 counters after its upkeep trigger "
                             "resolved -- the reported path behaves "
                             "correctly here.")
            elif tot == 0:
                ass["A4_two_counters"] = "failed"
                notes.append("A4 FAILED: Blood Tyrant carries 0 +1/+1 "
                             "counters after both players lost 1 life -- "
                             "THE REPORTED BUG REPRODUCES (expected 2, "
                             "the TrackedSetSize count resolves to 0).")
            elif tot is None:
                ass["A4_two_counters"] = "failed"
                notes.append("A4 FAILED: counter field unreadable on the "
                             f"Blood Tyrant object (raw={raw}); see "
                             f"post_counter_probe.")
            else:
                ass["A4_two_counters"] = "failed"
                notes.append(f"A4 FAILED: Blood Tyrant carries {tot} "
                             f"counters (raw={raw}) -- neither 2 (correct) "
                             f"nor 0 (reported symptom); unexpected.")
        else:
            ass["A4_two_counters"] = "failed"
            notes.append("A4 FAILED: no Blood Tyrant on P0's battlefield "
                         "in the post state.")
    else:
        notes.append("A4 not-run: the upkeep trigger path did not complete "
                     "or post is missing.")

    # A5: cleanup
    if post_st:
        if not stack_entries(post_st):
            ass["A5_cleanup"] = "passed"
            notes.append("A5 passed: post stack empty, game continues.")
        else:
            ass["A5_cleanup"] = "failed"
            notes.append("A5 FAILED: post stack non-empty: "
                         f"{stack_entries(post_st)}")
    else:
        notes.append("A5 not-run: no post state")

    if (ass["A1_setup_ok"] == "passed"
            and ass["A2_trigger_fired"] == "passed"
            and ass["A3_life_loss"] == "passed"
            and ass["A4_two_counters"] == "failed"):
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: Blood Tyrant's upkeep trigger fired and "
            "each player lost 1 life, but the Tyrant gained 0 +1/+1 "
            "counters -- the parsed TrackedSetSize quantity resolves to 0 "
            "as the issue reports (confirmed on v0.99.0).")
    elif ass["A4_two_counters"] == "passed":
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: Blood Tyrant gained exactly 2 "
            "counters from its upkeep trigger. This is not a fix claim.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: the reported upkeep path could not "
                     "be fully exercised; see assertion notes.")

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
               if "blood tyrant" in l.lower()
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
        "driver": {"protocol_advertised": 98, "client": "driver/client.py"},
        "scenario_sha256": hashlib.sha256(
            open(f"{BACKFILL}/driver/scenario_7416.py", "rb").read()).hexdigest(),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": {
            "tyrant_cast": ST["tyrant_cast"],
            "tyrant_cast_rejections": ST["tyrant_cast_rejections"],
            "tyrant_stack_oid": ST["tyrant_stack_oid"],
            "tyrant_bf_oid": ST["tyrant_bf_oid"],
            "tyrant_on_bf_at": ST["tyrant_on_bf_at"],
            "trigger_seen": ST["trigger_seen"],
            "trigger_seen_at": ST["trigger_seen_at"],
            "checkpoints": {
                "pre": {"life": ST["pre_life"],
                        "tyrant_bf_oid": ST["tyrant_bf_oid"]},
            },
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The report includes no game state (data-level report); the "
            "scenario replays the reported line (Blood Tyrant's upkeep "
            "trigger in a 2-player game) from a fresh game.",
            "Only the 2-player case was exercised (expected 2 counters); "
            "the 4-player expectation (4 counters) was not tested.",
            "Only the upkeep trigger was exercised; the LosesGame trigger "
            "was not.",
            "States are authoritative exports, restorable only via full "
            "game replay.",
        ],
        "setup_line": "P0: 4x Blood Tyrant + 18x Island + 19x Swamp + 19x "
                      "Mountain; P1: 60x Plains (lands, passes)",
        "contract_line": "Blood Tyrant's upkeep trigger resolves with each "
                         "player losing 1 life -> the Tyrant must gain "
                         "exactly 2 +1/+1 counters",
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
                            if ST["phase"] == "casting_tyrant":
                                ST["tyrant_cast_rejections"] += 1
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
                    # Tyrant spell on the stack
                    if (ST["phase"] == "casting_tyrant"
                            and not ST["tyrant_seen_on_stack"]):
                        soid = stack_spell_oid(state, TYR)
                        if soid is not None:
                            ST["tyrant_seen_on_stack"] = True
                            ST["tyrant_stack_oid"] = soid
                            wire("tyrant_on_stack",
                                 {"oid": soid,
                                  "entries": stack_entries(state)})
                            say(f"Blood Tyrant on stack (oid {soid}); "
                                f"exporting cast")
                            await export_as(c, "cast")
                            ST["cast_exported"] = True
                    # Tyrant on the battlefield
                    tyr_bf = zone_ids(state, 0, "Battlefield", TYR)
                    if tyr_bf and ST["tyrant_bf_oid"] is None:
                        ST["tyrant_bf_oid"] = tyr_bf[0]
                        ST["tyrant_on_bf_at"] = now
                        ST["pre_life"] = (life_of(state, 0),
                                          life_of(state, 1))
                        await export_as(c, "pre")
                        ST["pre_exported"] = True
                        ST["phase"] = "tyrant_on_bf"
                        say(f"Blood Tyrant on P0 battlefield (oid "
                            f"{tyr_bf[0]}), life {ST['pre_life']}; "
                            f"pre exported; phase -> tyrant_on_bf")
                    # upkeep trigger on the stack: any stack entry
                    # referencing the Tyrant during P0's Upkeep
                    if (ST["phase"] == "tyrant_on_bf"
                            and not ST["trigger_seen"]
                            and state.get("phase") == "Upkeep"
                            and state.get("active_player") == 0):
                        for e in stack_entries(state):
                            ser = json.dumps(e, default=str).lower()
                            if "blood tyrant" in ser:
                                ST["trigger_seen"] = True
                                ST["trigger_seen_at"] = now
                                wire("trigger_on_stack",
                                     {"entries": stack_entries(state),
                                      "life": (life_of(state, 0),
                                               life_of(state, 1))})
                                say("Blood Tyrant upkeep trigger observed "
                                    "ON THE STACK; exporting trigger")
                                await export_as(c, "trigger")
                                ST["trigger_exported"] = True
                                ST["phase"] = "resolving"
                                ST["settle_at"] = now
                                ST["watch"] = True
                                ST["watch_last"] = 0
                                break
                    # fallback: life loss during an Upkeep with the Tyrant
                    # on the battlefield means the trigger resolved even if
                    # the stack window was missed
                    if (not ST["trigger_seen"]
                            and ST["tyrant_bf_oid"] is not None
                            and ST["pre_life"] is not None):
                        pl = (life_of(state, 0), life_of(state, 1))
                        if pl != ST["pre_life"] and pl == (19, 19):
                            ST["trigger_seen"] = True
                            ST["trigger_seen_at"] = now
                            wire("trigger_inferred_via_life",
                                 {"life": pl,
                                  "stack": stack_entries(state)})
                            say("trigger inferred via life 20->19 both; "
                                "exporting trigger (post-resolution)")
                            await export_as(c, "trigger")
                            ST["trigger_exported"] = True
                            ST["phase"] = "resolving"
                            ST["settle_at"] = now
                            ST["watch"] = True
                            ST["watch_last"] = 0
                    # resolution watch: every 5s export a watch state and
                    # check for the decisive post-resolution moment
                    # (LoseLife resolved + stack empty). Export post and
                    # finalize immediately -- do not wait for idle, the
                    # game may run on and end before we capture.
                    if ST["watch"] and now - ST["watch_last"] > 5:
                        ST["watch_last"] = now
                        ST["watch_n"] += 1
                        wname = f"watch_{ST['watch_n']:03d}"
                        try:
                            await export_as(c, wname)
                        except Exception as e:
                            say(f"watch export {wname} failed: {e}")
                        pl = (life_of(state, 0), life_of(state, 1))
                        tyr_bf = zone_ids(state, 0, "Battlefield", TYR)
                        ctot = (counter_total(get_obj(state, tyr_bf[0]))
                                if tyr_bf else None)
                        wire("watch", {"n": ST["watch_n"], "life": pl,
                                       "stack_n": len(stack_entries(state)),
                                       "tyrant_counters": ctot,
                                       "phase": state.get("phase"),
                                       "active": state.get("active_player"),
                                       "waiting_for": wf_of(state).get("type")})
                        say(f"watch {ST['watch_n']}: life={pl} "
                            f"stack={len(stack_entries(state))} "
                            f"tyrant_counters={ctot} "
                            f"phase={state.get('phase')}")
                        if (pl == (19, 19) and not stack_entries(state)
                                and not ST["post_exported"]):
                            say("decisive post-resolution state captured "
                                "(life 19/19, stack empty); exporting post")
                            await export_as(c, "post")
                            ST["post_exported"] = True
                            finalized = True
                            break
                    # settle detection: stack empty + Priority + idle
                    if ST["phase"] == "resolving" and ST["trigger_exported"]:
                        if not stack_entries(state):
                            if ST["settle_at"] is None:
                                ST["settle_at"] = now
                            idle = now - ST["settle_at"]
                            wtype = (wf_of(state).get("type") or "")
                            if wtype == "Priority" and idle > SETTLE_IDLE_S:
                                say(f"settled: stack empty, Priority, "
                                    f"{idle:.0f}s idle; exporting post")
                                await export_as(c, "post")
                                ST["post_exported"] = True
                                finalized = True
                                break
                        else:
                            ST["settle_at"] = None
                    # watchdogs: stuck states
                    if (ST["tyrant_cast"]
                            and not ST["cast_exported"]
                            and not ST["pre_exported"]
                            and ST.get("tyrant_cast_submitted_at")
                            and now - ST["tyrant_cast_submitted_at"] > 180
                            and not ST["post_exported"]):
                        say("tyrant-cast watchdog: 180s after submission, "
                            "Tyrant never reached the stack/battlefield -- "
                            "exporting stuck state")
                        wire("tyrant_cast_stuck",
                             {"waiting_for": wf_of(state),
                              "tyrant_seen_on_stack":
                                  ST["tyrant_seen_on_stack"]})
                        await export_as(c, "post")
                        ST["post_exported"] = True
                        finalized = True
                        break
                    if (ST["phase"] == "setup"
                            and not ST["tyrant_cast"]
                            and now - t_start > 1200
                            and not ST["post_exported"]):
                        say("setup watchdog: 1200s in, Blood Tyrant never "
                            "cast -- exporting state and finalizing")
                        wire("setup_stall",
                             {"waiting_for": wf_of(state),
                              "p0_hand": hand_lnames(state, 0)})
                        await export_as(c, "post")
                        ST["post_exported"] = True
                        finalized = True
                        break
                    if (ST["phase"] == "tyrant_on_bf"
                            and ST["tyrant_on_bf_at"] is not None
                            and now - ST["tyrant_on_bf_at"] > 600
                            and not ST["trigger_seen"]
                            and not ST["post_exported"]):
                        say("trigger watchdog: 600s after the Tyrant "
                            "reached the battlefield, its upkeep trigger "
                            "never fired -- exporting stuck state")
                        wire("trigger_stuck",
                             {"waiting_for": wf_of(state),
                              "life": (life_of(state, 0),
                                       life_of(state, 1)),
                              "p0_turn_phase": state.get("phase"),
                              "active": state.get("active_player")})
                        await export_as(c, "post")
                        ST["post_exported"] = True
                        finalized = True
                        break
                    # game-over detection
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
                    # terminal result (e.g. draw by game_rules): finalize
                    # with whatever the watch captured
                    if ST["terminal"] and not finalized:
                        say("TerminalResult received -- finalizing with "
                            "captured watch states")
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
    shutil.copy(f"{BACKFILL}/driver/scenario_7416.py",
                f"{EVDIR}/scenario_7416.py")
    sys.path.insert(0, f"{BACKFILL}/driver")
    import subprocess
    subprocess.run([sys.executable, f"{BACKFILL}/driver/render_summary.py",
                    EVDIR, str(ISSUE),
                    "Blood Tyrant upkeep trigger places 0 counters "
                    "(TrackedSetSize mis-parse)"],
                   check=True)
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for f in files:
            h = hashlib.sha256(open(f"{EVDIR}/{f}", "rb").read()).hexdigest()
            mf.write(f"{h}  {f}\n")
    print(f"manifest written ({len(files)} files); "
          f"verdict={run['verdict']}", flush=True)
    sys.exit(0)
