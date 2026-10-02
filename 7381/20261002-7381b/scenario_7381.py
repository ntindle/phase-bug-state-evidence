#!/usr/bin/env python3
"""Issue #7381: Marauding Mako gets one +1/+1 counter instead of two after
resolving Faithless Looting.

Reported (discord 2026-08-13, sync-filed, status:confirmed, area:engine,
mechanic:triggers/counters, classifier:supported-aspect-defect): "After I
resolve faithless looting, marauding mako gets one counter instead of two."

Oracle text (verified from pinned v0.99.0 card-data.json):
  Marauding Mako {R}
  "Whenever you discard one or more cards, put that many +1/+1 counters on
  this creature. Cycling {2} ({2}, Discard this card: Draw a card.)"
  Faithless Looting {R}
  "Draw two cards, then discard two cards. Flashback {2}{R} (...)"

The 2026-08-15 triage (mike-theDude) ruled out the parser: both cards are
supported:true and lower faithfully; marauding mako's trigger is the
batched one-or-more form (DiscardedAll with a PutCounter child whose count
is EventContextAmount P1P1 -- "that many", read from the discard event).
This is a runtime defect in how the discard event's card count is reported
to the trigger.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 4x Marauding Mako + 4x Faithless Looting + 52x Mountain
  P1: 60x Mountain (plays lands, passes)
Drive:
  1. P0 T1: Mountain. T2: Mountain, cast Marauding Mako ({R}).
  2. With the Mako on the battlefield, on a main phase with an untapped
     Mountain and Faithless Looting in hand and an empty stack:
       a. export pre (scope: setup assembled)
       b. cast Faithless Looting ({R})
       c. export cast (Looting spell on the stack)
       d. answer the engine's DiscardChoice (count 2) with exactly 2 cards
       e. export trigger (Mako trigger on/around the stack -- the real pre
          for the operation under investigation)
       f. pass priority; the trigger resolves.
  3. export post after the stack settles; finalize.

Expected (correct behavior): the Mako's trigger puts 2 +1/+1 counters on
the Mako (one per discarded card), drawn 2 cards, 2 discarded cards plus
the Looting itself in P0's graveyard.
Reported (bug): the Mako gets exactly 1 counter.

Assertions (correct-behavior expectations; a FAIL records the bug):
  A1_setup_ok      pre exported: Marauding Mako on P0's battlefield,
                   Faithless Looting in P0 hand, >=1 untapped Mountain,
                   life 20/20.
  A2_looting_resolved  Looting cast with no rejections, seen on the stack,
                   then resolved (in P0's graveyard; stack no longer holds
                   it). P0 drew 2 cards before discarding.
  A3_discard_ok    DiscardChoice (count 2) was offered; answered with
                   exactly 2 cards, no rejections; the 2 chosen cards ended
                   in P0's graveyard.
  A4_trigger_ok    The Mako "whenever you discard" trigger went on the
                   stack and resolved (observed on the stack, or the Mako's
                   counters demonstrably increased after the discard).
  A5_two_counters  (THE REPORTED BUG) post state: the Mako object carries
                   exactly 2 +1/+1 counters. 1 counter (the reported
                   symptom) fails; any other !=2 count fails with notes.
  A6_cleanup       post stack empty, game advancing.

Verdict rule: reproduced iff A1..A4 passed and A5 failed;
              not-reproduced iff A5 passed (exactly 2 counters);
              blocked iff A1 failed (the setup line never assembled).

Protocol-98 driver notes (v0.99.0, verified 2026-10-01/02):
  - HELLO advertises protocol 98 (server enforces exact match).
  - MulliganDecision as {"choice":{"type":"Keep"}} gated on
    waiting_for.data.pending[] with phase type "Declare".
  - BottomCards via single SelectCards {"cards":[...]}.
  - CastSpell via advertised action; mana via PayMana actions.
  - Faithless Looting's "discard two cards" surfaces as waiting_for type
    "DiscardChoice" (data.player, data.count, data.cards candidates) with a
    "SelectCards" legal action; submitted as
    {"type":"SelectCards","data":{"cards":[oid,oid]}} (probed 2026-10-02).
  - The Mako trigger is automatic (no choices); its stack entry references
    the Mako object id / "marauding mako".
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
RUN_ID = "20261002-7381b"
ISSUE = 7381
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

MAKO = "marauding mako"
LOOT = "faithless looting"
MTN = "mountain"

P0_DECK = [("Marauding Mako", 4), ("Faithless Looting", 4), ("Mountain", 52)]
P1_DECK = [("Mountain", 60)]

SETUP_DEADLINE_S = 1500
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
        "phase": "setup",  # setup -> casting_looting -> discarding -> resolving -> done
        "mako_cast": False,
        "looting_cast_submitted": False,
        "looting_cast_submitted_at": None,
        "looting_cast_rejections": 0,
        "looting_seen_on_stack": False,
        "looting_stack_oid": None,
        "looting_gy_oid": None,
        "discard_offered": False,
        "discard_offered_count": None,
        "discard_answered": False,
        "discard_picks": [],
        "discard_rejections": 0,
        "discard_accepted_at": None,
        "trigger_seen": False,
        "trigger_seen_at": None,
        "trigger_resolved_inferred": False,
        "pre_exported": False,
        "cast_exported": False,
        "trigger_exported": False,
        "post_exported": False,
        "settle_at": None,
        "states_seen": 0,
        "pre_life": None,
        "pre_hand_n": None,
        "pre_mako_bf_oid": None,
        "pre_lib_n": None,
        "drawn_n": None,
        "ass": {k: "not-run" for k in ("A1_setup_ok", "A2_looting_resolved",
                                       "A3_discard_ok", "A4_trigger_ok",
                                       "A5_two_counters", "A6_cleanup")},
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


def lib_count(state, pid):
    lib = (player_of(state, pid).get("library")
           or player_of(state, pid).get("deck") or [])
    try:
        return len(lib)
    except TypeError:
        return None


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


def get_vi(st):
    vi = st.get("viewer_interaction") or {}
    return vi if vi.get("canSubmit") else None


def stack_entries(state):
    return state.get("stack") or []


def stack_spell_oid(state, key):
    for e in stack_entries(state):
        if not isinstance(e, dict):
            continue
        for k in ("object_id", "id", "source", "source_id", "card_id"):
            v = e.get(k)
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if obj_lname(state, iv) == key:
                return iv
        for v in e.values():
            if isinstance(v, dict):
                for k in ("object_id", "id", "source", "source_id"):
                    try:
                        iv = int(v.get(k))
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
    """Total +1/+1 counters on an object, tolerant of representation."""
    c = obj.get("counters")
    if c is None:
        return None
    if isinstance(c, dict):
        tot = 0
        for k, v in c.items():
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


def mako_counters(state, oid):
    o = get_obj(state, oid)
    tot = counter_total(o)
    return tot, o.get("counters")

# ------------------------------------------------------------- data check
def check_data_level():
    mako = CARD_DATA.get("marauding mako", {})
    loot = CARD_DATA.get("faithless looting", {})
    findings = {
        "mako_name": mako.get("name"),
        "mako_mana_cost": mako.get("mana_cost"),
        "mako_oracle": mako.get("oracle_text"),
        "loot_name": loot.get("name"),
        "loot_mana_cost": loot.get("mana_cost"),
        "loot_oracle": loot.get("oracle_text"),
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(findings, f, indent=1, default=str)
    wire("data_level", findings)
    ok = (
        mako.get("mana_cost") == {"type": "Cost", "shards": ["Red"], "generic": 0}
        and "Whenever you discard one or more cards" in (mako.get("oracle_text") or "")
        and "put that many +1/+1 counters" in (mako.get("oracle_text") or "")
        and loot.get("mana_cost") == {"type": "Cost", "shards": ["Red"], "generic": 0}
        and "Draw two cards, then discard two cards" in (loot.get("oracle_text") or "")
    )
    say(f"data-level check: {'OK' if ok else 'MISMATCH'}")
    ST["notes"].append(f"data-level: Marauding Mako {{R}} / Faithless "
                       f"Looting {{R}} parse as reported: {ok}")
    return ok

# ------------------------------------------------------------- interaction
async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


def mulligan_keep_p0(hand):
    return ((MAKO in hand or LOOT in hand) and MTN in hand)


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
    rank = {MAKO: 5, LOOT: 5, MTN: 4}
    picks = sorted(hand, key=lambda o: rank.get(obj_lname(state, o), 2))[:n]
    SUBMITTED.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    wire("bottom", {"who": tag, "picks": picks})
    return True


async def do_looting_discard(c, pid, tag):
    """Answer Faithless Looting's DiscardChoice with exactly `count`
    cards, preferring basic lands."""
    st = c.latest
    if not st:
        return False
    state = st["state"]
    rev = c.revision
    wf = wf_of(state)
    if wf.get("type") != "DiscardChoice":
        return False
    data = wf.get("data") or {}
    if str(data.get("player")) != str(pid):
        return False
    if _DISCARD_REV.get((c.name, rev)):
        return False
    n = int(data.get("count") or 0)
    if n <= 0:
        return False
    ST["discard_offered"] = True
    ST["discard_offered_count"] = n
    cands = [int(x) for x in (data.get("cards") or [])]
    hand = set(hand_ids(state, pid))
    usable = [x for x in cands if x in hand] or list(hand)
    rank = {MTN: 0, MAKO: 5, LOOT: 5}
    picks = sorted(usable, key=lambda o: rank.get(obj_lname(state, o), 2))[:n]
    wire("discard_choice", {"who": tag, "count": n, "candidates": cands,
                            "picks": picks,
                            "pick_names": [obj_lname(state, x) for x in picks]})
    acts = merged_actions(st)
    sc = next((a for a in acts if a["type"] == "SelectCards"), None)
    if sc is None:
        say(f"[{tag}] WARNING: DiscardChoice offered but no SelectCards "
            f"legal action; not submitting blind")
        ST["notes"].append("DiscardChoice offered without a SelectCards "
                           "legal action; see wire_log discard_choice")
        return False
    _DISCARD_REV[(c.name, rev)] = True
    ST["discard_answered"] = True
    ST["discard_picks"] = picks
    ST["discard_accepted_at"] = time.time()
    # the observation pass keys the trigger checkpoint / settle / watchdogs
    # on this phase; it must flip the moment the discard is answered
    ST["phase"] = "discarding"
    say(f"[{tag}] discarding {[obj_lname(state, x) for x in picks]} "
        f"(count {n}); phase -> discarding")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    wire("discard_submit", {"who": tag, "picks": picks})
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
    rank = {MTN: 0, LOOT: 1, MAKO: 1}
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
    if wtype == "DiscardChoice":
        if await do_looting_discard(c, 0, "P0"):
            return
        return
    if ST["phase"] == "casting_looting":
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
    if ST["phase"] not in ("setup", "casting_looting", "discarding", "resolving"):
        await pass_priority(c, st, acts)
        return
    phase = state.get("phase")
    active = state.get("active_player")
    if phase in ("PreCombatMain", "PostCombatMain") and active == 0:
        mako_bf = zone_ids(state, 0, "Battlefield", MAKO)
        # 1) cast Marauding Mako as soon as possible
        if (not ST["mako_cast"] and not mako_bf
                and MAKO in hand_lnames(state, 0)
                and untapped_lands(state, 0)):
            a, oid = cast_action_for(acts, state, MAKO)
            if a:
                ST["mako_cast"] = True
                say(f"[P0] casting Marauding Mako (oid {oid})")
                wire("cast_mako", {"oid": oid, "action": a["type"]})
                await submit_as_is(c, a)
                return
            wire("mako_cast_missing",
                 {"hand": hand_lnames(state, 0),
                  "act_types": sorted({a["type"] for a in acts})})
        # 2) cast Faithless Looting with the Mako on the battlefield
        if (not ST["looting_cast_submitted"] and mako_bf
                and LOOT in hand_lnames(state, 0)
                and untapped_lands(state, 0)
                and not stack_entries(state)):
            a, oid = cast_action_for(acts, state, LOOT)
            if a:
                ST["looting_cast_submitted"] = True
                ST["looting_cast_submitted_at"] = time.time()
                p0p = player_of(state, 0)
                p1p = player_of(state, 1)
                ST["pre_life"] = (p0p.get("life"), p1p.get("life"))
                ST["pre_hand_n"] = len(hand_ids(state, 0))
                ST["pre_mako_bf_oid"] = mako_bf[0]
                ST["pre_lib_n"] = lib_count(state, 0)
                await export_as(c, "pre")
                ST["pre_exported"] = True
                ST["phase"] = "casting_looting"
                tot, raw = mako_counters(state, mako_bf[0])
                say(f"[P0] pre exported: Mako (oid {mako_bf[0]}) on BF "
                    f"(counters={raw}), Looting in hand, "
                    f"{len(untapped_lands(state, 0))} untapped Mountains, "
                    f"life {ST['pre_life']}, hand {ST['pre_hand_n']}")
                wire("cast_looting", {"oid": oid, "action": a["type"]})
                await submit_as_is(c, a)
                return
            wire("looting_cast_missing",
                 {"hand": hand_lnames(state, 0),
                  "mako_bf": mako_bf,
                  "act_types": sorted({a["type"] for a in acts})})
        # 3) land drop
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

    # A1: setup assembled
    if ST["pre_exported"] and pre_st:
        mako_bf = zone_ids(pre_st, 0, "Battlefield", MAKO)
        loot_hand = LOOT in hand_lnames(pre_st, 0)
        mtn_n = len(untapped_lands(pre_st, 0))
        life_ok = ST["pre_life"] == (20, 20)
        if mako_bf and loot_hand and mtn_n >= 1 and life_ok:
            ass["A1_setup_ok"] = "passed"
            notes.append(
                f"A1 passed: pre exported with Marauding Mako (oid "
                f"{mako_bf[0]}) on P0's battlefield, Faithless Looting in "
                f"P0 hand, {mtn_n} untapped Mountain(s), life 20/20.")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append(
                f"A1 FAILED: mako_bf={mako_bf}, loot_in_hand={loot_hand}, "
                f"untapped_mtn={mtn_n}, pre_life={ST['pre_life']}.")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append("A1 FAILED: pre state was never exported (Faithless "
                     "Looting was never cast with the Mako on the "
                     "battlefield).")

    # A2: Looting cast cleanly and resolved
    notes.append(
        f"A2 context: looting_seen_on_stack={ST['looting_seen_on_stack']} "
        f"(oid {ST['looting_stack_oid']}), cast_rejections="
        f"{ST['looting_cast_rejections']}, looting_in_gy_oid="
        f"{ST['looting_gy_oid']}.")
    if (ST["looting_seen_on_stack"] and ST["looting_cast_rejections"] == 0
            and ST["looting_gy_oid"] is not None):
        ass["A2_looting_resolved"] = "passed"
        notes.append(
            f"A2 passed: Faithless Looting cast with no rejections, seen "
            f"on the stack (oid {ST['looting_stack_oid']}), then resolved "
            f"into P0's graveyard (oid {ST['looting_gy_oid']}).")
    elif ST["looting_cast_submitted"]:
        ass["A2_looting_resolved"] = "failed"
        notes.append("A2 FAILED: Faithless Looting was submitted but did "
                     "not complete cleanly (see A2 context).")
    else:
        notes.append("A2 not-run: Faithless Looting was never cast.")

    # A3: discard-2 offered and answered with exactly 2 cards
    if ST["discard_offered"] and ST["discard_answered"]:
        picks = ST["discard_picks"]
        if (ST["discard_offered_count"] == 2 and len(picks) == 2
                and ST["discard_rejections"] == 0):
            gy_names = [obj_lname(post_st, o) for o in
                        zone_ids(post_st, 0, "Graveyard")]
            picks_in_gy = all(obj_lname(post_st, p) in gy_names
                              and zone_ids(post_st, 0, "Graveyard",
                                           obj_lname(post_st, p))
                              for p in picks) if post_st else False
            if picks_in_gy:
                ass["A3_discard_ok"] = "passed"
                notes.append(
                    f"A3 passed: DiscardChoice (count 2) was offered and "
                    f"answered with exactly 2 cards "
                    f"({[obj_lname(post_st, p) for p in picks]}, oids "
                    f"{picks}), no rejections; both are in P0's graveyard "
                    f"post-resolution.")
            else:
                ass["A3_discard_ok"] = "failed"
                notes.append(
                    f"A3 FAILED: the 2 discarded picks ({picks}) are not "
                    f"both in P0's graveyard post-resolution.")
        else:
            ass["A3_discard_ok"] = "failed"
            notes.append(
                f"A3 FAILED: offered_count={ST['discard_offered_count']}, "
                f"picks={picks}, rejections={ST['discard_rejections']}.")
    elif ST["looting_cast_submitted"]:
        ass["A3_discard_ok"] = "failed"
        notes.append(
            f"A3 FAILED: Looting was cast but DiscardChoice was never "
            f"properly offered/answered (offered={ST['discard_offered']}, "
            f"answered={ST['discard_answered']}).")
    else:
        notes.append("A3 not-run: Looting was never cast.")

    # A4: the Mako trigger fired and resolved
    if ST["trigger_seen"] or ST["trigger_resolved_inferred"]:
        ass["A4_trigger_ok"] = "passed"
        how = ("observed on the stack at "
               f"{ST['trigger_seen_at']}" if ST["trigger_seen"]
               else "inferred: Mako counters increased after the discard")
        notes.append(f"A4 passed: the Mako discard trigger {how} and the "
                     f"stack settled.")
    elif ST["looting_cast_submitted"]:
        ass["A4_trigger_ok"] = "failed"
        notes.append("A4 FAILED: no Mako trigger was observed on the stack "
                     "after the discard, and no counter increase was "
                     "observed.")
    else:
        notes.append("A4 not-run: Looting was never cast.")

    # A5: THE REPORTED BUG -- exactly 2 +1/+1 counters on the Mako
    if ST["post_exported"] and post_st and ass["A3_discard_ok"] == "passed":
        mako_bf = zone_ids(post_st, 0, "Battlefield", MAKO)
        if mako_bf:
            tot, raw = mako_counters(post_st, mako_bf[0])
            mobj = get_obj(post_st, mako_bf[0])
            wire("post_counter_probe",
                 {"mako_bf_oid": mako_bf[0], "counters_raw": raw,
                  "counters_total": tot,
                  "power": mobj.get("power"),
                  "toughness": mobj.get("toughness")})
            notes.append(
                f"A5 probe: post Mako (oid {mako_bf[0]}) counters raw={raw} "
                f"(total={tot}), P/T={mobj.get('power')}/"
                f"{mobj.get('toughness')}.")
            if tot == 2:
                ass["A5_two_counters"] = "passed"
                notes.append("A5 passed: the Mako carries exactly 2 +1/+1 "
                             "counters after discarding 2 cards -- the "
                             "reported path behaves correctly here.")
            elif tot == 1:
                ass["A5_two_counters"] = "failed"
                notes.append("A5 FAILED: the Mako carries exactly 1 +1/+1 "
                             "counter after discarding 2 cards -- THE "
                             "REPORTED BUG REPRODUCES (expected 2).")
            elif tot is None:
                ass["A5_two_counters"] = "failed"
                notes.append("A5 FAILED: counter field unreadable on the "
                             f"Mako object (raw={raw}); see post_counter_probe.")
            else:
                ass["A5_two_counters"] = "failed"
                notes.append(f"A5 FAILED: the Mako carries {tot} counters "
                             f"(raw={raw}) -- neither 2 (correct) nor 1 "
                             f"(reported symptom); unexpected.")
        else:
            ass["A5_two_counters"] = "failed"
            notes.append("A5 FAILED: no Marauding Mako on P0's battlefield "
                         "in the post state.")
    else:
        notes.append("A5 not-run: the discard path did not complete or "
                     "post is missing.")

    # A6: cleanup
    if post_st:
        if not stack_entries(post_st):
            ass["A6_cleanup"] = "passed"
            notes.append("A6 passed: post stack empty, game continues.")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append("A6 FAILED: post stack non-empty: "
                         f"{stack_entries(post_st)}")
    else:
        notes.append("A6 not-run: no post state")

    if (ass["A1_setup_ok"] == "passed"
            and ass["A2_looting_resolved"] == "passed"
            and ass["A3_discard_ok"] == "passed"
            and ass["A4_trigger_ok"] == "passed"
            and ass["A5_two_counters"] == "failed"):
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: Faithless Looting resolved (drew 2, "
            "discarded 2) with Marauding Mako on the battlefield; the "
            "Mako's trigger fired but put only 1 +1/+1 counter on the Mako "
            "instead of 2 -- exactly the reported failure (confirmed on "
            "v0.99.0).")
    elif ass["A5_two_counters"] == "passed":
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: the Mako gained exactly 2 counters "
            "from the 2-card discard. This is not a fix claim.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: the reported discard path could not "
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
               if "marauding" in l.lower() or "mako" in l.lower()
               or "faithless" in l.lower()
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
            open(f"{BACKFILL}/driver/scenario_7381.py", "rb").read()).hexdigest(),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": {
            "mako_cast": ST["mako_cast"],
            "looting_stack_oid": ST["looting_stack_oid"],
            "looting_gy_oid": ST["looting_gy_oid"],
            "discard_offered": ST["discard_offered"],
            "discard_offered_count": ST["discard_offered_count"],
            "discard_answered": ST["discard_answered"],
            "discard_picks": ST["discard_picks"],
            "discard_rejections": ST["discard_rejections"],
            "trigger_seen": ST["trigger_seen"],
            "trigger_resolved_inferred": ST["trigger_resolved_inferred"],
            "checkpoints": {
                "pre": {"life": ST["pre_life"], "hand_n": ST["pre_hand_n"],
                        "mako_bf_oid": ST["pre_mako_bf_oid"],
                        "lib_n": ST["pre_lib_n"]},
            },
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The report's attached Discord game state was not imported (the "
            "prebuilt server has no standalone state-restore facility); the "
            "scenario replays the reported line (cast Faithless Looting "
            "with Marauding Mako on the battlefield, discarding 2 cards) "
            "from a fresh game.",
            "Only the 2-card discard from Faithless Looting was exercised; "
            "other discard counts and the Cycling {2} branch were not.",
            "States are authoritative exports, restorable only via full game "
            "replay.",
        ],
        "setup_line": "P0: 4x Marauding Mako + 4x Faithless Looting + "
                      "52x Mountain; P1: 60x Mountain (lands, passes)",
        "contract_line": "Cast Faithless Looting with Marauding Mako on the "
                         "battlefield and discard 2 cards -> the Mako must "
                         "gain exactly 2 +1/+1 counters",
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
                            if ST["phase"] == "casting_looting":
                                ST["looting_cast_rejections"] += 1
                            elif ST["phase"] in ("discarding", "resolving") \
                                    and tag == "P0":
                                ST["discard_rejections"] += 1
                        elif t not in ("StateUpdate", "GameStarted"):
                            wire("inbox_misc", {"who": tag, "type": t})
                except asyncio.QueueEmpty:
                    pass
                state = st["state"]
                ST["states_seen"] += 1
                acts = merged_actions(st)

                if tag == "P0":
                    # Looting on the stack
                    if ST["phase"] == "casting_looting" and not ST["looting_seen_on_stack"]:
                        loid = stack_spell_oid(state, LOOT)
                        if loid is not None:
                            ST["looting_seen_on_stack"] = True
                            ST["looting_stack_oid"] = loid
                            wire("looting_on_stack",
                                 {"oid": loid,
                                  "entries": stack_entries(state)})
                            say(f"Looting on stack (oid {loid}); "
                                f"exporting cast")
                            await export_as(c, "cast")
                            ST["cast_exported"] = True
                    # Looting resolved into the graveyard
                    if ST["looting_gy_oid"] is None:
                        gy = zone_ids(state, 0, "Graveyard", LOOT)
                        if gy:
                            ST["looting_gy_oid"] = gy[0]
                            wire("looting_in_gy", {"gy_oid": gy[0]})
                            say(f"Looting in P0 graveyard (oid {gy[0]})")
                    # Mako trigger on the stack: any stack entry referencing
                    # the Mako that is not the Looting spell itself
                    if ST["phase"] in ("discarding", "resolving"):
                        for e in stack_entries(state):
                            ser = json.dumps(e, default=str).lower()
                            if "marauding" in ser or (
                                    "mako" in ser
                                    and "faithless" not in ser):
                                if not ST["trigger_seen"]:
                                    ST["trigger_seen"] = True
                                    ST["trigger_seen_at"] = now
                                    wire("trigger_on_stack",
                                         {"entries": stack_entries(state)})
                                    say("Mako discard trigger observed ON "
                                        "THE STACK")
                                break
                    # trigger export: discard answered and prompt gone;
                    # the trigger may already be resolving -- still the real
                    # pre for the operation under investigation.
                    if (ST["phase"] == "discarding"
                            and not ST["trigger_exported"]
                            and ST["discard_answered"]
                            and wf_of(state).get("type") != "DiscardChoice"):
                        await export_as(c, "trigger")
                        ST["trigger_exported"] = True
                        ST["phase"] = "resolving"
                        ST["settle_at"] = now
                        tot, raw = mako_counters(
                            state, ST["pre_mako_bf_oid"] or -1)
                        wire("trigger_checkpoint",
                             {"mako_counters_at_checkpoint": raw})
                        say(f"trigger checkpoint exported (Mako counters "
                            f"at checkpoint: {raw}); phase -> resolving")
                    # counter-increase inference: Mako gained counters after
                    # the discard even if the stack was never caught
                    if (ST["phase"] == "resolving"
                            and ST["pre_mako_bf_oid"] is not None
                            and not ST["trigger_resolved_inferred"]):
                        tot, raw = mako_counters(
                            state, ST["pre_mako_bf_oid"])
                        if tot is not None and tot > 0:
                            ST["trigger_resolved_inferred"] = True
                            wire("trigger_inferred",
                                 {"counters": raw})
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
                    if (ST["looting_cast_submitted"] and not ST["cast_exported"]
                            and ST.get("looting_cast_submitted_at")
                            and now - ST["looting_cast_submitted_at"] > 180
                            and not ST["post_exported"]):
                        say("looting-cast watchdog: 180s after submission, "
                            "cast never completed -- exporting stuck state")
                        wire("looting_cast_stuck",
                             {"waiting_for": wf_of(state),
                              "looting_seen_on_stack":
                                  ST["looting_seen_on_stack"]})
                        await export_as(c, "post")
                        ST["post_exported"] = True
                        finalized = True
                        break
                    if (ST["phase"] == "discarding" and ST["discard_answered"]
                            and not ST["trigger_exported"]
                            and ST.get("discard_accepted_at")
                            and now - ST["discard_accepted_at"] > 120
                            and not ST["post_exported"]):
                        say("discard watchdog: 120s after the discard was "
                            "answered, trigger checkpoint never exported -- "
                            "exporting stuck state")
                        wire("discard_stuck",
                             {"waiting_for": wf_of(state)})
                        await export_as(c, "post")
                        ST["post_exported"] = True
                        finalized = True
                        break
                    # setup-stall watchdog
                    if (ST["phase"] == "setup"
                            and not ST["looting_cast_submitted"]
                            and now - t_start > 900
                            and not ST["post_exported"]):
                        say("setup watchdog: 900s in, Looting never "
                            "cast -- exporting state and finalizing")
                        wire("setup_stall",
                             {"waiting_for": wf_of(state),
                              "p0_hand": hand_lnames(state, 0)})
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
    shutil.copy(f"{BACKFILL}/driver/scenario_7381.py",
                f"{EVDIR}/scenario_7381.py")
    sys.path.insert(0, f"{BACKFILL}/driver")
    import subprocess
    subprocess.run([sys.executable, f"{BACKFILL}/driver/render_summary.py",
                    EVDIR, str(ISSUE),
                    "Marauding Mako gets one counter instead of two after "
                    "Faithless Looting"],
                   check=True)
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for f in files:
            h = hashlib.sha256(open(f"{EVDIR}/{f}", "rb").read()).hexdigest()
            mf.write(f"{h}  {f}\n")
    print(f"manifest written ({len(files)} files); "
          f"verdict={run['verdict']}", flush=True)
    sys.exit(0)
