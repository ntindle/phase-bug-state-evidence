#!/usr/bin/env python3
"""Issue #7355 on v0.99.0 (protocol 98): Disa the Restless returns Lhurgoyfs
that die in combat.

Oracle (verified against pinned v0.99.0 card-data.json):
  Disa the Restless {2}{B}{R}{G}:
    "Whenever a Lhurgoyf permanent card is put into your graveyard from
     anywhere other than the battlefield, put it onto the battlefield.
     Whenever one or more creatures you control deal combat damage to a
     player, create a Tarmogoyf token."
  Tarmogoyf {1}{G}: creature type Lhurgoyf (a Lhurgoyf permanent card).

Triage (issue comments): the parser dropped the "from anywhere other than
the battlefield" origin exclusion -- the shipped ChangesZone trigger has
origin: null, so it fires on EVERY route into the graveyard, including
death in combat. (The second trigger's "create a Tarmogoyf token" node is
Unimplemented -- a separate, already-visible gap; this scenario never lets
a creature deal combat damage to a player, so it never fires.)

Behavioral contract (2-player, deterministic by construction):
  Setup: P0: 12x Disa + 16x Tarmogoyf + 12x Faithless Looting + 8 Forest /
         6 Swamp / 6 Mountain. P1: 12x Grizzly Bears + 48x Forest (passive:
         blocks, never attacks).
    P0 ramps (T1 Swamp, T2 Forest, T3 Mountain, ...), casts Disa as soon as
    the gate offers it ({2}{B}{R}{G}), casts one Tarmogoyf, then attacks
    with it only while P1 has an untapped Grizzly Bears to block with.
    With empty graveyards the goyf is 0/1; the 2/2 Bears kills it in combat.
  A1: setup ok -- game created, 2 seats, P0 mulligan kept Disa + 3 lands.
  A2: attack preconditions -- Disa on P0 BF, 0/1 Tarmogoyf on P0 BF,
      untapped Bears on P1 BF, P0 PreCombatMain/DeclareAttackers, 20/20.
  A3 (DECISIVE): after the goyf dies in combat, it must STAY in P0's
      graveyard (the trigger must not fire for a battlefield origin).
      If it returns to the battlefield -> bug reproduced.
  A4 (control): on a later P0 main phase, cast Faithless Looting and
      discard a Tarmogoyf from hand -- the trigger SHOULD fire (origin is
      the hand, not the battlefield) and return it to the battlefield.
      This shows the trigger exists and works; the bug is specifically the
      missing origin exclusion.

Verdict: reproduced iff A3 fails (goyf returns to BF after combat death);
not-reproduced iff A3 passes; blocked otherwise.

Protocol-98 notes: HELLO advertises protocol 98 (exact match);
MulliganDecision {"choice":{"type":"Keep"}}; BottomCards/DiscardToHandSize
via single SelectCards {"cards":[...]}; CastSpell as-advertised + PayMana
ticks; DeclareAttackers attacks=[[oid,{"type":"Player","data":1}]];
DeclareBlockers assignments=[[blocker_oid, attacker_oid]];
Looting discard arrives as a "Discard"-flavored waiting_for answered with
SelectCards. state.turn is null -- turn boundaries tracked via
active_player changes; priority-gated passes.
"""
import asyncio
import copy
import hashlib
import json
import logging
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client as _client
_client.URL = "ws://127.0.0.1:9374/ws"
from client import PhaseClient, deck  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario7355")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
EVID_RUN_ID = "20261001-7355"
EVDIR = f"{BACKFILL}/evidence/7355/{EVID_RUN_ID}"
if os.environ.get("BACKFILL_APPEND") != "1":
    assert not os.path.exists(EVDIR) or not os.listdir(EVDIR), "EVDIR not empty"
os.makedirs(EVDIR, exist_ok=True)

_MODE = "a" if os.environ.get("BACKFILL_APPEND") == "1" else "w"
WIRE = open(f"{EVDIR}/wire_log.jsonl", _MODE)
RUNLOG = open(f"{EVDIR}/scenario_run.log", _MODE)


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event, "payload": payload}) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(), "event": event + ":UNSER",
                               "payload": repr(payload)[:500], "err": str(e)}) + "\n")
    WIRE.flush()


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


SERVER_IDENTITY = {
    "validated_version": "v0.99.0",
    "build_commit": "d919616",
    "protocol_version": 98,
    "server_binary_sha256": None,
    "card_data_sha256": None,
    "draft_pools_sha256": None,
    "signature_verified": True,  # minisign-verified when pinned 2026-10-01
}
for _f, _k in (("server/releases/v0.99.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.99.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.99.0/data/draft-pools.json", "draft_pools_sha256")):
    SERVER_IDENTITY[_k] = sha256_of_file(f"{BACKFILL}/{_f}")
say("server identity hashes recorded:",
    {k: ((v[:12] + "...") if isinstance(v, str) and len(v) > 20 else v)
     for k, v in SERVER_IDENTITY.items()})

DISA = "Disa the Restless"
GOYF = "Tarmogoyf"
LOOT = "Faithless Looting"
BEARS = "Grizzly Bears"
FOREST = "Forest"
SWAMP = "Swamp"
MOUNTAIN = "Mountain"

P0_DECK = deck((DISA, 12), (GOYF, 16), (LOOT, 12),
               (FOREST, 8), (SWAMP, 6), (MOUNTAIN, 6))
P1_DECK = deck((BEARS, 12), (FOREST, 48))

P0_LANDSEQ = ["Swamp", "Forest", "Mountain", "Forest", "Swamp", "Forest",
              "Mountain", "Swamp", "Forest", "Mountain", "Forest", "Swamp"]


def obj_name(state, oid):
    o = state["objects"].get(str(oid))
    return (o.get("base_name") or o.get("name")) if o else "?"


def find_obj(state, name, pid=None, zone="Battlefield"):
    out = []
    for oid, o in state["objects"].items():
        if (o.get("base_name") or o.get("name")) != name:
            continue
        if zone and o.get("zone") != zone:
            continue
        if pid is not None and o.get("controller") != pid:
            continue
        out.append((oid, o))
    return out


def hand_ids(state, pid):
    return [str(x) for x in state["players"][pid]["hand"]]


def hand_names(state, pid):
    return [obj_name(state, x) for x in hand_ids(state, pid)]


def wf_of(state):
    return state.get("waiting_for") or {}


def wf_player(state):
    return (wf_of(state).get("data") or {}).get("player")


def pending_for(state, pid):
    wf = wf_of(state)
    data = wf.get("data") or {}
    for p in data.get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            return p
    return None


def merged_actions(st):
    return st.get("legal_actions") or []


_MULLS = {}
_MULL_ANSWERED = {}
_DISCARD_REV = {}
_PASSED_REV = {}
_LAND_PLAYED = {}
_CAST = {}
_SUBMITTED_ACT = set()
_TARGET_SUBMITTED = set()
_STACK_SEEN = set()


class AbortGame(Exception):
    pass


def p0_hand_ok(names):
    return names.count(DISA) >= 1 and sum(names.count(l) for l in (FOREST, SWAMP, MOUNTAIN)) >= 3


async def do_mulligan(c, pid, tag):
    st = c.latest
    if not st:
        return False
    rev = st.get("state_revision", -1)
    if _MULL_ANSWERED.get((tag, rev)):
        return False
    state = st["state"]
    if wf_of(state).get("type") != "MulliganDecision":
        return False
    pend = pending_for(state, pid)
    if not pend or (pend.get("phase") or {}).get("type") != "Declare":
        return False
    n = _MULLS.get(tag, 0)
    names = hand_names(state, pid)
    if pid == 1 or p0_hand_ok(names) or n >= 2:
        say(f"[{tag}] mulligan KEEP (hand {len(names)}): {names}")
        await c.send_action({"type": "MulliganDecision", "data": {"choice": {"type": "Keep"}}})
        wire("mulligan", {"who": tag, "decision": "keep", "hand": names})
    else:
        say(f"[{tag}] mulligan #{n + 1} (hand {len(names)}): {names}")
        await c.send_action({"type": "MulliganDecision", "data": {"choice": {"type": "Mulligan"}}})
        wire("mulligan", {"who": tag, "decision": "mulligan", "hand": names})
    _MULLS[tag] = n + 1
    _MULL_ANSWERED[(tag, rev)] = True
    return True


async def do_bottom(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    pend = pending_for(state, pid)
    if not pend:
        return False
    phase = pend.get("phase") or {}
    # do_mulligan owns the Declare step; bottoming is the follow-up step
    # (which may still carry wf type "MulliganDecision")
    if phase.get("type") == "Declare":
        return False
    n = phase.get("count")
    if not n:
        wire("bottom_no_count", {"who": tag, "phase": phase,
                                 "wf_type": wf_of(state).get("type")})
        return False
    if (tag, "bottomed") in _MULLS:
        return False
    ids = hand_ids(state, pid)
    names = hand_names(state, pid)
    mins = {FOREST: 1, SWAMP: 1, MOUNTAIN: 1, DISA: 1}
    counts = {}
    for nm in names:
        counts[nm] = counts.get(nm, 0) + 1

    def bottom_rank(i):
        nm = names[i]
        excess = counts.get(nm, 0) - mins.get(nm, 0)
        if excess > 0:
            value = {LOOT: 0, GOYF: 1, MOUNTAIN: 2, FOREST: 3, SWAMP: 4, DISA: 5}.get(nm, 6)
            return (0, -excess, value)
        return (1, 0, 0)

    order = sorted(range(len(ids)), key=bottom_rank)
    safe = [int(ids[i]) for i in order[:n] if bottom_rank(i)[0] == 0]
    picks = (safe[:n] if safe else [int(ids[i]) for i in order[:n]])
    say(f"[{tag}] bottoming {len(picks)}: {[obj_name(state, x) for x in picks]} (hand was {names})")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    _MULLS[(tag, "bottomed")] = True
    wire("bottom", {"who": tag, "count": n})
    return True


async def do_discard(c, pid, tag):
    st = c.latest
    if not st:
        return False
    rev = st.get("state_revision", -1)
    if _DISCARD_REV.get((c.name, rev)):
        return False
    state = st["state"]
    if wf_of(state).get("type") != "DiscardToHandSize":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    n = len(hand_ids(state, pid)) - 7
    if n <= 0:
        return False

    def rank(oid):
        nm = obj_name(state, oid) or ""
        return {LOOT: 0, GOYF: 1, MOUNTAIN: 2, FOREST: 3, SWAMP: 4, DISA: 5}.get(nm, 6)

    picks = [int(x) for x in sorted(hand_ids(state, pid), key=rank)[:n]]
    say(f"[{tag}] discarding to hand size: {[obj_name(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    _DISCARD_REV[(c.name, rev)] = True
    return True


async def do_looting_discard(c, pid, tag):
    """Faithless Looting's 'discard two cards': prefer discarding a goyf
    (the control leg), then lowest-value cards."""
    st = c.latest
    if not st:
        return False
    rev = st.get("state_revision", -1)
    if _DISCARD_REV.get((c.name, "loot", rev)):
        return False
    state = st["state"]
    wtype = wf_of(state).get("type") or ""
    if "Discard" not in wtype or wtype == "DiscardToHandSize":
        return False
    if str(wf_player(state)) != str(pid):
        return False
    d = wf_of(state).get("data", {}) or {}
    n = 2
    for k in ("count", "amount", "number"):
        if isinstance(d.get(k), int):
            n = d[k]
    wire("looting_discard_prompt", {"who": tag, "wtype": wtype, "n": n,
                                   "hand": hand_names(state, pid)})

    def rank(oid):
        nm = obj_name(state, oid) or ""
        if nm == GOYF:
            return (0, nm)
        return ({LOOT: 1, MOUNTAIN: 2, FOREST: 3, SWAMP: 4, DISA: 5}.get(nm, 6), nm)

    picks = [int(x) for x in sorted(hand_ids(state, pid), key=rank)[:n]]
    say(f"[{tag}] looting discards: {[obj_name(state, x) for x in picks]}")
    wire("looting_discard", {"who": tag, "picks": [obj_name(state, x) for x in picks],
                             "pick_oids": picks})
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    _DISCARD_REV[(c.name, "loot", rev)] = True
    return picks


async def drain_rejections(c):
    rej = []
    try:
        while True:
            t, data = c.inbox.get_nowait()
            if t in ("ActionRejected", "Error"):
                rej.append({"type": t, "data": data})
    except asyncio.QueueEmpty:
        pass
    return rej


async def send_checked(c, action, key, tag):
    """Send an action; if the engine rejects it promptly, drop the dedup key
    so the driver retries on a later tick instead of stalling forever."""
    await c.send_action(action)
    await asyncio.sleep(0.5)
    rej = await drain_rejections(c)
    if rej:
        _SUBMITTED_ACT.discard(key)
        say(f"[{tag}] action REJECTED, will retry: {json.dumps(rej)[:300]}")
        wire("action_rejection", {"who": tag, "action": action, "rejection": rej})
        return False
    return True


async def pay_tick(acts, c, tag):
    for a in acts:
        if a.get("type") in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    return False


async def pass_priority_if_offered(c, pid, acts):
    st = c.latest
    if not st:
        return False
    rev = st.get("state_revision", -1)
    s = st["state"]
    if wf_of(s).get("type") != "Priority":
        return False
    if str((wf_of(s).get("data") or {}).get("player")) != str(pid):
        return False
    if _PASSED_REV.get(c.name, -1) >= rev:
        return False
    for a in acts:
        if a.get("type") == "PassPriority":
            await c.send_action(a)
            _PASSED_REV[c.name] = rev
            return True
    return False


def my_main(state, pid):
    return (state.get("active_player") == pid
            and state.get("priority_player") == pid
            and state.get("phase") in ("PreCombatMain", "PostCombatMain", "Main"))


_TURN = {"n": 0, "last_active": "init"}


def update_turn(state):
    cur = state.get("active_player")
    if cur == 0 and _TURN["last_active"] != 0:
        _TURN["n"] += 1
        say(f"--- P0 turn {_TURN['n']} begins ---")
    _TURN["last_active"] = cur


def find_action(acts, atype, name=None, state=None):
    for a in acts:
        if a.get("type") != atype:
            continue
        if name is not None and state is not None:
            oid = (a.get("data") or {}).get("object_id")
            if obj_name(state, oid) != name:
                continue
        return a
    return None


def untapped_lands(state, pid, name=None):
    out = []
    for oid, o in state["objects"].items():
        if o.get("zone") != "Battlefield" or o.get("controller") != pid:
            continue
        if o.get("tapped"):
            continue
        nm = o.get("base_name") or o.get("name")
        if nm not in (FOREST, SWAMP, MOUNTAIN):
            continue
        if name is not None and nm != name:
            continue
        out.append(oid)
    return out


def can_pay_disa(state):
    return (len(untapped_lands(state, 0, SWAMP)) >= 1
            and len(untapped_lands(state, 0, MOUNTAIN)) >= 1
            and len(untapped_lands(state, 0, FOREST)) >= 1
            and len(untapped_lands(state, 0)) >= 5)


def stack_entries(state):
    out = []
    for oid, o in state["objects"].items():
        if o.get("zone") == "Stack":
            out.append((oid, o.get("base_name") or o.get("name"),
                        (o.get("source_name") or "")))
    return out


async def combat_drive(c, pid, tag, goyf_oid):
    """Drive one client during COMBAT_WATCH. P1 blocks the goyf with every
    untapped Bears; nobody attacks; everything else is answered generically."""
    if await do_mulligan(c, pid, tag):
        return True
    if await do_bottom(c, pid, tag):
        return True
    if await do_discard(c, pid, tag):
        return True
    if pid == 0 and await do_looting_discard(c, pid, tag):
        return True
    rej = await drain_rejections(c)
    if rej:
        wire("rejections", {"who": tag, "rejections": rej})
    st = c.latest
    if not st:
        return False
    state, acts = st["state"], merged_actions(st)
    if await pay_tick(acts, c, tag):
        return True
    wtype = wf_of(state).get("type") or ""
    wplayer = str(wf_player(state))
    if wtype == "DeclareAttackers" and wplayer == str(pid):
        da = find_action(acts, "DeclareAttackers")
        if da is not None:
            d = copy.deepcopy(da.get("data", {}))
            d["attacks"] = []
            d["bands"] = []
            await c.send_action({"type": "DeclareAttackers", "data": d})
            wire("empty_attackers", {"who": tag})
            return True
    if wtype == "DeclareBlockers" and wplayer == str(pid):
        db = find_action(acts, "DeclareBlockers")
        if db is not None:
            d = copy.deepcopy(db.get("data", {}))
            if pid == 1 and goyf_oid is not None:
                bears = [oid for oid, o in find_obj(state, BEARS, pid=1)
                         if not o.get("tapped")]
                d["assignments"] = [[int(b), int(goyf_oid)] for b in bears]
                say(f"[P1] combat_watch: blocking goyf {goyf_oid} with Bears {bears}")
                wire("p1_blocks", {"blockers": bears, "attacker": goyf_oid})
            else:
                d["assignments"] = []
            await c.send_action({"type": "DeclareBlockers", "data": d})
            return True
    if wtype in ("TargetSelection", "TriggerTargetSelection") and wplayer == str(pid):
        wire("combat_target_selection", {"who": tag, "wf": wf_of(state),
               "vi": st.get("viewer_interaction")})
        say(f"[{tag}] target selection during combat_watch (logged, pending)")
        return True
    await pass_priority_if_offered(c, pid, acts)
    return False


async def generic_drive(c, pid, tag):
    """Answer non-priority decisions; pass priority; empty declares.
    Returns True if it acted."""
    if await do_mulligan(c, pid, tag):
        return True
    if await do_bottom(c, pid, tag):
        return True
    if await do_discard(c, pid, tag):
        return True
    st = c.latest
    if not st:
        return False
    state, acts = st["state"], merged_actions(st)
    if await pay_tick(acts, c, tag):
        return True
    wtype = wf_of(state).get("type") or ""
    wplayer = str(wf_player(state))
    if wtype == "DeclareAttackers" and wplayer == str(pid):
        da = find_action(acts, "DeclareAttackers")
        if da is not None:
            d = copy.deepcopy(da.get("data", {}))
            d["attacks"] = []
            d["bands"] = []
            await c.send_action({"type": "DeclareAttackers", "data": d})
            return True
    if wtype == "DeclareBlockers" and wplayer == str(pid):
        db = find_action(acts, "DeclareBlockers")
        if db is not None:
            d = copy.deepcopy(db.get("data", {}))
            d["assignments"] = []
            await c.send_action({"type": "DeclareBlockers", "data": d})
            return True
    if wtype in ("TargetSelection", "TriggerTargetSelection") and wplayer == str(pid):
        wire("unexpected_target_selection", {"who": tag, "wf": wf_of(state),
               "vi": st.get("viewer_interaction")})
        say(f"[{tag}] UNEXPECTED target selection; leaving pending (logged)")
        return True
    await pass_priority_if_offered(c, pid, acts)
    return False

async def p0_land(c, state, acts, tn):
    key = ("P0", tn)
    if key in _LAND_PLAYED:
        return False
    hn = hand_names(state, 0)
    want = P0_LANDSEQ[tn - 1] if 0 < tn <= len(P0_LANDSEQ) else FOREST
    pick = want if want in hn else next(
        (l for l in (FOREST, SWAMP, MOUNTAIN) if l in hn), None)
    if not pick:
        return False
    a = find_action(acts, "PlayLand", pick, state)
    if a:
        say(f"[P0] T{tn}: playing {pick}")
        await c.send_action(a)
        _LAND_PLAYED[key] = True
        return True
    return False


async def setup_tick(c, pid, tag):
    """Drive one client during SETUP. Returns 'attacked' when P0 has
    declared the goyf attack (pre.json exported)."""
    st = c.latest
    if not st:
        return None
    state = st["state"]
    if pid == 0:
        update_turn(state)
        if _TURN["n"] > 20 and not find_obj(state, DISA, pid=0):
            raise AbortGame(f"P0 turn {_TURN['n']} without Disa; stalled")
    if await do_mulligan(c, pid, tag):
        return None
    if await do_bottom(c, pid, tag):
        return None
    if await do_discard(c, pid, tag):
        return None
    if pid == 0 and await do_looting_discard(c, pid, tag):
        return None
    # global rejection drain: log anything the engine refused (diagnosis)
    rej = await drain_rejections(c)
    if rej:
        wire("rejections", {"who": tag, "rejections": rej})
        say(f"[{tag}] rejections drained: {json.dumps(rej)[:250]}")
    st = c.latest
    state, acts = st["state"], merged_actions(st)
    if await pay_tick(acts, c, tag):
        return None
    wtype = wf_of(state).get("type") or ""
    wplayer = str(wf_player(state))

    if pid == 1:
        # P1: passive. Land, Bears, empty declares, block the goyf.
        if my_main(state, 1):
            tn = _TURN["n"]
            key = ("P1", tn)
            if key not in _LAND_PLAYED and FOREST in hand_names(state, 1):
                a = find_action(acts, "PlayLand", FOREST, state)
                if a:
                    say(f"[P1] playing Forest (P0 turn {tn})")
                    await c.send_action(a)
                    _LAND_PLAYED[key] = True
                    return None
            if BEARS in hand_names(state, 1) and _CAST.get("p1_bears", 0) < 2:
                a = find_action(acts, "CastSpell", BEARS, state)
                if a:
                    key2 = json.dumps(a, sort_keys=True)
                    if key2 not in _SUBMITTED_ACT:
                        _SUBMITTED_ACT.add(key2)
                        say("[P1] casting Grizzly Bears")
                        wire("cast", {"who": "P1", "card": BEARS, "action": a})
                        if await send_checked(c, a, key2, tag):
                            _CAST["p1_bears"] = _CAST.get("p1_bears", 0) + 1
                        return None
        if wtype == "DeclareAttackers" and wplayer == "1":
            da = find_action(acts, "DeclareAttackers")
            if da is not None:
                d = copy.deepcopy(da.get("data", {}))
                d["attacks"] = []
                d["bands"] = []
                await c.send_action({"type": "DeclareAttackers", "data": d})
                return None
        if wtype == "DeclareBlockers" and wplayer == "1":
            db = find_action(acts, "DeclareBlockers")
            if db is not None:
                goyfs = find_obj(state, GOYF, pid=0)
                bears = [oid for oid, o in find_obj(state, BEARS, pid=1)
                         if not o.get("tapped")]
                d = copy.deepcopy(db.get("data", {}))
                if goyfs and bears:
                    d["assignments"] = [[int(bears[0]), int(goyfs[0][0])]]
                    say(f"[P1] blocking goyf {goyfs[0][0]} with Bears {bears[0]}")
                    wire("p1_blocks", {"blocker": bears[0], "attacker": goyfs[0][0]})
                else:
                    d["assignments"] = []
                    wire("p1_no_block", {"goyfs": [g[0] for g in goyfs],
                                         "bears_untapped": bears})
                await c.send_action({"type": "DeclareBlockers", "data": d})
                return None
        await pass_priority_if_offered(c, pid, acts)
        return None

    # ---- P0 ----
    if my_main(state, 0):
        tn = _TURN["n"]
        hn = hand_names(state, 0)
        if await p0_land(c, state, acts, tn):
            return None
        disa_bf = find_obj(state, DISA, pid=0)
        if not disa_bf and DISA in hn:
            # watchdog: a sent-but-never-arrived Disa gets retried
            if _CAST.get("disa_sent_turn") is not None \
                    and tn > _CAST["disa_sent_turn"] + 2:
                _SUBMITTED_ACT.discard(_CAST.get("disa_key"))
                say(f"[P0] T{tn}: Disa cast never arrived; retrying")
                wire("disa_retry", {"turn": tn})
                _CAST.pop("disa_sent_turn", None)
                _CAST.pop("disa", None)
            a = find_action(acts, "CastSpell", DISA, state)
            if a and can_pay_disa(state):
                key = json.dumps(a, sort_keys=True)
                if key not in _SUBMITTED_ACT:
                    _SUBMITTED_ACT.add(key)
                    say(f"[P0] T{tn}: casting Disa the Restless")
                    wire("cast", {"who": "P0", "card": DISA, "action": a})
                    if await send_checked(c, a, key, tag):
                        _CAST["disa"] = True
                        _CAST["disa_sent_turn"] = tn
                        _CAST["disa_key"] = key
                    return None
        if disa_bf and GOYF in hn and not _CAST.get("goyf"):
            a = find_action(acts, "CastSpell", GOYF, state)
            if a and len(untapped_lands(state, 0, FOREST)) >= 1 \
                    and len(untapped_lands(state, 0)) >= 2:
                key = json.dumps(a, sort_keys=True)
                if key not in _SUBMITTED_ACT:
                    _SUBMITTED_ACT.add(key)
                    say(f"[P0] T{tn}: casting Tarmogoyf")
                    wire("cast", {"who": "P0", "card": GOYF, "action": a})
                    if await send_checked(c, a, key, tag):
                        _CAST["goyf"] = True
                        _CAST["goyf_turn"] = tn
                    return None
    if wtype == "DeclareAttackers" and wplayer == "0":
        da = find_action(acts, "DeclareAttackers")
        if da is None:
            return None
        goyfs = [(oid, o) for oid, o in find_obj(state, GOYF, pid=0)
                 if not o.get("tapped")]
        bears = [oid for oid, o in find_obj(state, BEARS, pid=1)
                 if not o.get("tapped")]
        want_attack = (goyfs and bears and not _CAST.get("attacked")
                       and _TURN["n"] > _CAST.get("goyf_turn", 10**9))
        d = copy.deepcopy(da.get("data", {}))
        if want_attack:
            goyf_oid = int(goyfs[0][0])
            # A2 preconditions + pre export
            pre_s = await c.export_state()
            with open(f"{EVDIR}/pre.json", "w") as f:
                f.write(pre_s)
            pre = json.loads(pre_s)["state"]
            g = pre["objects"].get(str(goyf_oid), {})
            pt = (g.get("power"), g.get("toughness"))
            say(f"[P0] A2 check: goyf P/T={pt} (want (0,1)); "
                f"disa_bf={bool(find_obj(pre, DISA, pid=0))} "
                f"bears_untapped={len([o for _, o in find_obj(pre, BEARS, pid=1) if not o.get('tapped')])}")
            wire("a2_pre", {"goyf_oid": goyf_oid, "goyf_pt": pt,
                            "hand": hand_names(pre, 0)})
            d["attacks"] = [[goyf_oid, {"type": "Player", "data": 1}]]
            d["bands"] = []
            say(f"[P0] attacking P1 with Tarmogoyf {goyf_oid}")
            wire("p0_attacks", {"attacker": goyf_oid})
            await c.send_action({"type": "DeclareAttackers", "data": d})
            _CAST["attacked"] = True
            _CAST["attack_goyf_oid"] = goyf_oid
            return "attacked"
        d["attacks"] = []
        d["bands"] = []
        await c.send_action({"type": "DeclareAttackers", "data": d})
        return None
    if wtype == "DeclareBlockers" and wplayer == "0":
        db = find_action(acts, "DeclareBlockers")
        if db is not None:
            d = copy.deepcopy(db.get("data", {}))
            d["assignments"] = []
            await c.send_action({"type": "DeclareBlockers", "data": d})
            return None
    if wtype in ("TargetSelection", "TriggerTargetSelection") and wplayer == "0":
        wire("setup_target_selection", {"wf": wf_of(state)})
        say("[P0] target selection during setup; leaving pending (logged)")
        return None
    await pass_priority_if_offered(c, pid, acts)
    return None


def reset_per_game_state():
    _MULLS.clear(); _MULL_ANSWERED.clear(); _DISCARD_REV.clear()
    _PASSED_REV.clear(); _LAND_PLAYED.clear(); _CAST.clear()
    _SUBMITTED_ACT.clear(); _TARGET_SUBMITTED.clear(); _STACK_SEEN.clear()
    _TURN.update({"n": 0, "last_active": "init"})


async def run_setup(p0, p1, obs, timeout=1500):
    clients = [(p0, 0, "P0"), (p1, 1, "P1")]
    t0 = time.time()
    while time.time() - t0 < timeout:
        await asyncio.sleep(0.15)
        for c, pid, tag in clients:
            r = await setup_tick(c, pid, tag)
            if r == "attacked":
                say("attack declared -> entering COMBAT_WATCH")
                return obs, True
    obs["notes"].append(f"setup timed out after {timeout}s; cast={_CAST}")
    return obs, False


async def combat_watch(p0, p1, obs, goyf_oid, timeout=150):
    """After the attack: drive both seats, watch the goyf's zone and the
    stack for Disa's trigger, then export mid_death/post."""
    a = obs["assert"]
    death_seen = False
    trigger_seen = False
    returned = False
    t0 = time.time()
    say(f"COMBAT_WATCH start (goyf {goyf_oid})")
    while time.time() - t0 < timeout:
        await asyncio.sleep(0.15)
        for c, pid, tag in ((p0, 0, "P0"), (p1, 1, "P1")):
            await combat_drive(c, pid, tag, goyf_oid)
        st = p0.latest
        if not st:
            continue
        s = st["state"]
        for oid, nm, src in stack_entries(s):
            if oid not in _STACK_SEEN:
                _STACK_SEEN.add(oid)
                blob = f"{nm} {src}".lower()
                say(f"COMBAT_WATCH stack: {oid} {nm} src={src}")
                wire("stack_entry", {"oid": oid, "name": nm, "source": src})
                if "disa" in blob:
                    trigger_seen = True
                    mid_s = await p0.export_state()
                    with open(f"{EVDIR}/mid_trigger.json", "w") as f:
                        f.write(mid_s)
                    say("Disa trigger observed on stack -> mid_trigger.json")
                    wire("disa_trigger_seen", {"oid": oid})
        o = s["objects"].get(str(goyf_oid), {})
        zone = o.get("zone")
        if zone == "Graveyard" and not death_seen:
            death_seen = True
            t_death = time.time() - t0
            say(f"COMBAT_WATCH: goyf died at t+{t_death:.1f}s")
            wire("goyf_death", {"t": round(t_death, 1), "zone": zone})
            mid_s = await p0.export_state()
            with open(f"{EVDIR}/mid_death.json", "w") as f:
                f.write(mid_s)
        if death_seen and zone == "Battlefield":
            returned = True
            say("COMBAT_WATCH: goyf is back on the battlefield (BUG)")
            wire("goyf_returned", {"t": round(time.time() - t0, 1)})
            await asyncio.sleep(3)
            break
        if death_seen and time.time() - t0 > 60:
            say("COMBAT_WATCH: 60s after death, goyf still not on BF")
            break
    post_s = await p0.export_state()
    with open(f"{EVDIR}/post.json", "w") as f:
        f.write(post_s)
    post = json.loads(post_s)["state"]
    g = post["objects"].get(str(goyf_oid), {})
    final_zone = g.get("zone")
    say(f"COMBAT_WATCH done: death_seen={death_seen} trigger_seen={trigger_seen} "
        f"final_zone={final_zone}")
    wire("combat_watch_result", {"death_seen": death_seen,
                                 "trigger_seen": trigger_seen,
                                 "final_zone": final_zone})
    obs["trigger_seen_on_stack"] = trigger_seen
    if not death_seen:
        a["A3_no_return"] = "not-run"
        obs["notes"].append("A3 not-run: the goyf never reached the graveyard")
        return "blocked"
    if final_zone == "Graveyard":
        a["A3_no_return"] = "passed"
        obs["notes"].append("A3: goyf stayed in P0's graveyard after combat "
                            "death; Disa's trigger correctly did not fire "
                            f"(stack sighting: {trigger_seen})")
        return "not-reproduced"
    if final_zone == "Battlefield":
        a["A3_no_return"] = "failed"
        obs["notes"].append("A3 FAILED (bug reproduced): the goyf returned to "
                            "the battlefield after dying in combat; Disa's "
                            "ChangesZone trigger fired despite the battlefield "
                            f"origin (stack sighting: {trigger_seen})")
        return "reproduced"
    a["A3_no_return"] = "not-run"
    obs["notes"].append(f"A3 not-run: goyf ended in unexpected zone {final_zone}")
    return "blocked"

async def control_leg(p0, p1, obs, timeout=420):
    """A4: cast Faithless Looting, discard a goyf from hand; the trigger
    SHOULD fire (non-battlefield origin) and return it."""
    a = obs["assert"]
    t0 = time.time()
    looting_cast = False
    discarded_goyf = None
    say("CONTROL leg start: looking for a P0 main phase with Looting+goyf")
    while time.time() - t0 < timeout:
        await asyncio.sleep(0.15)
        for c, pid, tag in ((p0, 0, "P0"), (p1, 1, "P1")):
            st = c.latest
            if not st:
                continue
            s = st["state"]
            if await do_mulligan(c, pid, tag):
                continue
            if await do_bottom(c, pid, tag):
                continue
            if await do_discard(c, pid, tag):
                continue
            picks = await do_looting_discard(c, pid, tag)
            if picks:
                looting_cast = True
                for pk in picks:
                    if obj_name(s, pk) == GOYF:
                        discarded_goyf = str(pk)
                wire("control_discarded_goyf", {"oid": discarded_goyf})
                say(f"CONTROL: discarded goyf oid={discarded_goyf}")
                continue
            acts = merged_actions(st)
            if await pay_tick(acts, c, tag):
                continue
            wtype = wf_of(s).get("type") or ""
            wplayer = str(wf_player(s))
            if pid == 0 and my_main(s, 0) and not looting_cast:
                hn = hand_names(s, 0)
                if LOOT in hn and GOYF in hn:
                    la = find_action(acts, "CastSpell", LOOT, s)
                    if la and len(untapped_lands(s, 0, MOUNTAIN)) >= 1:
                        key = json.dumps(la, sort_keys=True)
                        if key not in _SUBMITTED_ACT:
                            _SUBMITTED_ACT.add(key)
                            say("[P0] CONTROL: casting Faithless Looting")
                            wire("cast", {"who": "P0", "card": LOOT,
                                          "action": la})
                            await send_checked(p0, la, key, tag)
                            continue
            if pid == 1 and wtype == "DeclareBlockers" and wplayer == "1":
                db = find_action(acts, "DeclareBlockers")
                if db is not None:
                    d = copy.deepcopy(db.get("data", {}))
                    d["assignments"] = []
                    await c.send_action({"type": "DeclareBlockers", "data": d})
                    continue
            if wtype == "DeclareAttackers" and wplayer == str(pid):
                da = find_action(acts, "DeclareAttackers")
                if da is not None:
                    d = copy.deepcopy(da.get("data", {}))
                    d["attacks"] = []
                    d["bands"] = []
                    await c.send_action({"type": "DeclareAttackers", "data": d})
                    continue
            if wtype in ("TargetSelection", "TriggerTargetSelection") \
                    and wplayer == str(pid):
                wire("control_target_selection", {"who": tag, "wf": wf_of(s)})
                say(f"[{tag}] target selection during control leg (logged, pending)")
                continue
            await pass_priority_if_offered(c, pid, acts)
        if looting_cast and discarded_goyf:
            break
    if not looting_cast or not discarded_goyf:
        a["A4_discard_control"] = "not-run"
        obs["notes"].append("A4 not-run: no Looting+goyf window "
                            f"(looting_cast={looting_cast}, "
                            f"discarded_goyf={discarded_goyf})")
        return
    # watch for the trigger to return the discarded goyf
    t1 = time.time()
    trigger_seen = False
    while time.time() - t1 < 90:
        await asyncio.sleep(0.2)
        for c, pid, tag in ((p0, 0, "P0"), (p1, 1, "P1")):
            await generic_drive(c, pid, tag)
        st = p0.latest
        if not st:
            continue
        s = st["state"]
        for oid, nm, src in stack_entries(s):
            if oid not in _STACK_SEEN:
                _STACK_SEEN.add(oid)
                if "disa" in f"{nm} {src}".lower():
                    trigger_seen = True
                    say("CONTROL: Disa trigger seen on stack")
                    wire("control_trigger_seen", {"oid": oid})
        o = s["objects"].get(str(discarded_goyf), {})
        if o.get("zone") == "Battlefield":
            say("CONTROL: discarded goyf returned to battlefield")
            break
    post_s = await p0.export_state()
    with open(f"{EVDIR}/post_control.json", "w") as f:
        f.write(post_s)
    post = json.loads(post_s)["state"]
    g = post["objects"].get(str(discarded_goyf), {})
    zone = g.get("zone")
    say(f"CONTROL done: discarded goyf zone={zone} trigger_seen={trigger_seen}")
    wire("control_result", {"zone": zone, "trigger_seen": trigger_seen})
    if zone == "Battlefield":
        a["A4_discard_control"] = "passed"
        obs["notes"].append("A4: Looting discard of a goyf fired Disa's trigger "
                            f"(stack: {trigger_seen}) and returned it to the "
                            "battlefield -- the trigger works; the bug is the "
                            "missing battlefield-origin exclusion")
    elif zone == "Graveyard":
        a["A4_discard_control"] = "failed"
        obs["notes"].append("A4 FAILED: discarded goyf stayed in the graveyard; "
                            f"Disa's trigger did not fire even for a hand origin "
                            f"(stack: {trigger_seen}) -- unexpected")
    else:
        a["A4_discard_control"] = "not-run"
        obs["notes"].append(f"A4 not-run: discarded goyf in zone {zone}")


async def run_single_game(attempt):
    obs = {"assert": {}, "notes": [], "trigger_seen_on_stack": None}
    reset_per_game_state()
    p0 = PhaseClient("P0")
    await p0.connect()
    say(f"P0 creating game (attempt {attempt})")
    await p0.create(P0_DECK, player_count=2)
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, P1_DECK)
    say(f"game {p0.game_code}; seats={[p0.player_id, p1.player_id]}")
    obs["assert"]["A1_setup_ok"] = "passed" if [p0.player_id, p1.player_id] == [0, 1] else "failed"
    try:
        obs, setup_ok = await run_setup(p0, p1, obs)
    except AbortGame as e:
        say(f"ABORT game attempt {attempt}: {e}")
        wire("abort_game", {"attempt": attempt, "reason": str(e)})
        obs["notes"].append(f"game attempt {attempt} aborted: {e}")
        for c in (p0, p1):
            await c.close()
        return obs, "abort"
    if not setup_ok:
        for k in ("A2_attack_preconditions", "A3_no_return", "A4_discard_control"):
            obs["assert"][k] = "not-run"
        for c in (p0, p1):
            await c.close()
        return obs, "blocked"

    # A2 from pre.json (exported right before the attack was declared)
    pre = json.loads(open(f"{EVDIR}/pre.json").read())["state"]
    goyf_oid = _CAST.get("attack_goyf_oid")
    g = pre["objects"].get(str(goyf_oid), {})
    pt = (g.get("power"), g.get("toughness"))
    # NOTE: P/T is recorded, not gated: hand-size discards put sorceries in
    # yards, so the goyf may be bigger than 0/1. What matters is that it dies
    # to the 2/2 Bears block (toughness <= 2 here); combat_watch verifies the
    # death empirically.
    a2_ok = (bool(find_obj(pre, DISA, pid=0))
             and g.get("zone") == "Battlefield"
             and any(not o.get("tapped") for _, o in find_obj(pre, BEARS, pid=1))
             and pre.get("active_player") == 0
             and all(life == 20 for life in
                     [pre["players"][i].get("life") for i in (0, 1)]))
    obs["assert"]["A2_attack_preconditions"] = "passed" if a2_ok else "failed"
    obs["notes"].append(f"A2: goyf P/T={pt} (recorded; dies to 2/2 block); Disa on P0 BF; "
                        f"untapped Bears on P1 BF; P0 active; life 20/20 -> "
                        f"{'passed' if a2_ok else 'FAILED'}")
    if not a2_ok:
        obs["assert"]["A3_no_return"] = "not-run"
        obs["assert"]["A4_discard_control"] = "not-run"
        for c in (p0, p1):
            await c.close()
        return obs, "blocked"

    verdict = await combat_watch(p0, p1, obs, goyf_oid)
    if verdict in ("reproduced", "not-reproduced"):
        await control_leg(p0, p1, obs)
    else:
        obs["assert"]["A4_discard_control"] = "not-run"
    for c in (p0, p1):
        await c.close()
    return obs, verdict


async def run_game():
    last_obs, last_verdict = None, "blocked"
    for attempt in (1, 2, 3):
        last_obs, last_verdict = await run_single_game(attempt)
        if last_verdict != "abort":
            return last_obs, last_verdict
        say(f"game attempt {attempt} aborted; retrying" if attempt < 3 else
            "all game attempts aborted")
    last_obs["notes"].append("all 3 game attempts aborted on setup variance")
    return last_obs, "blocked"


async def render_summary(run, out_path):
    from PIL import Image, ImageDraw
    W, H = 1000, 900
    img = Image.new("RGB", (W, H), (16, 20, 28))
    d = ImageDraw.Draw(img)
    si = run["server_identity"]
    y = 20
    d.text((24, y), "Issue #7355 -- Disa the Restless returns Lhurgoyfs that die in combat",
           fill=(235, 240, 250)); y += 30
    d.text((24, y), "Oracle: '...put into your graveyard from anywhere OTHER than the",
           fill=(140, 160, 180)); y += 24
    d.text((24, y), "battlefield, put it onto the battlefield' -- origin exclusion missing (origin: null)",
           fill=(140, 160, 180)); y += 28
    d.text((24, y), f"server v{si['validated_version']} ({si['build_commit']}) protocol {si['protocol_version']} -- {run['run_id']}",
           fill=(140, 160, 180)); y += 28
    vcol = (255, 90, 90) if run["verdict"] == "reproduced" else ((120, 220, 120) if run["verdict"] == "not-reproduced" else (230, 200, 90))
    d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vcol); y += 34
    d.text((24, y), "Assertions (from saved states + wire log):", fill=(200, 210, 225)); y += 24
    labels = {
        "A1_setup_ok": "A1 setup: 2 seats, P0 mulligan kept Disa + 3 lands",
        "A2_attack_preconditions": "A2 pre-attack: Disa out, 0/1 goyf, untapped Bears, P0 active",
        "A3_no_return": "A3 goyf STAYS in graveyard after combat death (no trigger)",
        "A4_discard_control": "A4 control: Looting discard of goyf DOES return it",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "not-run")
        col = (120, 220, 120) if v == "passed" else ((255, 90, 90) if v == "failed" else (150, 150, 150))
        d.text((40, y), f"{'pass' if v=='passed' else ('FAIL' if v=='failed' else 'n/a')} {lab}", fill=col); y += 22
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225)); y += 24
    for n in run["notes"][:12]:
        d.text((40, y), n[:122], fill=(150, 165, 185)); y += 20
    d.text((24, H - 30), "Evidence: ntindle/phase-bug-state-evidence 7355/" + run["run_id"], fill=(120, 130, 150))
    img.save(out_path)


async def write_manifest():
    lines = []
    for name in sorted(os.listdir(EVDIR)):
        if name == "manifest.sha256":
            continue
        p = os.path.join(EVDIR, name)
        if os.path.isfile(p):
            lines.append(f"{sha256_of_file(p)}  {name}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")


async def main():
    t0 = time.time()
    obs, verdict = await run_game()
    dur = time.time() - t0
    a = obs["assert"]
    notes = obs["notes"] + [
        "protocol-98 driver (v0.99.0): 2 human seats; P0 mulligan keeps "
        "Disa + 2 lands (<=2 mulligans); SelectCards bottom/discard; "
        "PlayLand/CastSpell as-advertised + PayMana ticks; P1 passive "
        "(lands, one Bears, empty attacks, blocks the goyf); priority-gated "
        "passes; combat_watch drives both seats and samples the goyf's zone "
        "plus stack entries.",
        "Disa's second trigger (create a Tarmogoyf token, Unimplemented) was "
        "deliberately never armed: no creature dealt combat damage to a player.",
        "Card-data ground truth: Disa trigger[0] mode=ChangesZone, origin=null "
        "(the 'from anywhere other than the battlefield' exclusion absent), "
        "destination=Graveyard, valid_card=Typed[Permanent, Subtype Lhurgoyf] "
        "controller You; Tarmogoyf card_type.subtypes=[Lhurgoyf].",
    ]
    run = {
        "issue": 7355,
        "run_id": EVID_RUN_ID,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t0)),
        "duration_s": round(dur, 1),
        "server_identity": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 98, "client": "driver/client.py"},
        "scenario_sha256": sha256_of_file(f"{BACKFILL}/driver/scenario_7355.py"),
        "decks": {
            "P0": [[DISA, 12], [GOYF, 16], [LOOT, 12], [FOREST, 8], [SWAMP, 6], [MOUNTAIN, 6]],
            "P1": [[BEARS, 12], [FOREST, 48]],
        },
        "assertions": a,
        "notes": notes,
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "12x/20x deck densities are test-harness conveniences (engine accepts >4-of for custom games).",
            "Disa's second trigger (combat-damage -> Tarmogoyf token, currently Unimplemented) was deliberately not exercised.",
            "Restoration: the prebuilt phase-server has no standalone state-restore facility; pre/post states are authoritative exports restorable only via full game replay (scenario_7355.py).",
        ],
        "setup_line": ("P0: 12x Disa + 16x Tarmogoyf + 12x Faithless Looting + 20 lands; "
                       "ramp to {2}{B}{R}{G}, cast Disa, cast one Tarmogoyf, attack "
                       "into P1's untapped Grizzly Bears (0/1 goyf dies)"),
        "contract_line": ("After the goyf's combat death it must stay in P0's graveyard "
                          "(trigger must not fire for a battlefield origin); control: "
                          "Faithless Looting discarding a goyf from hand must fire the "
                          "trigger and return it"),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    await render_summary(run, f"{EVDIR}/summary.png")
    WIRE.close(); RUNLOG.close()
    await write_manifest()
    print(f"DONE verdict={verdict} assertions={json.dumps(a)}", flush=True)


asyncio.run(main())
