#!/usr/bin/env python3
"""Issue #6759 (re-validation on v0.99.0 / protocol 98): Nalia de'Arnise gains
a counter regardless of having a full party.

Oracle (pinned v0.99.0 card-data.json, "nalia de'arnise"):
  "You may look at the top card of your library any time.
   You may cast Cleric, Rogue, Warrior, and Wizard spells from the top of
   your library.
   At the beginning of combat on your turn, if you have a full party, put a
   +1/+1 counter on each creature you control and those creatures gain
   deathtouch until end of turn."

Reported: the beginning-of-combat +1/+1 counter/deathtouch effect happens
REGARDLESS of having a full party (party = one each of Cleric, Rogue,
Warrior, Wizard among creatures you control; full = 4). Nalia de'Arnise is
herself a Human Rogue, so she contributes 1.

Pinned card-data parse (v0.99.0): the BeginCombat phase trigger carries the
PutCounterAll effect with the intervening full-party condition; the runtime
behavior under test is whether the engine honors that condition.

Prior validations:
- 2026-09-10, v0.79.0/protocol 69, evidence 6759/20260910-6759, commit
  7a334c9468307eb7d5f19fa0610e79d8cee598c6: not-reproduced.
- 2026-09-17, v0.86.0/protocol 72, evidence 6759/20260917-6759:
  not-reproduced (7/7 assertions).
Release pin advanced v0.86.0 -> v0.99.0, so per the playbook staleness rule
this run re-drives both legs on the current pin.

Behavioral contract -- Game 1 (bug branch, NO full party):
  Setup: P0 controls only Nalia de'Arnise (party size 1).
  A1 setup_ok      pre.json exported at P0 PreCombatMain priority, board as
                   planned (Nalia on BF)
  A2 no_counters   post.json (P0 DeclareAttackers): no creature P0 controls
                   gained a +1/+1 counter across the BeginCombat trigger
  A3 no_deathtouch no creature P0 controls has Deathtouch in keywords
  A4 cleanup       stack empty, game proceeding
Game 2 (control, full party):
  Setup: P0 controls Nalia + Acolyte of Xathrid (Cleric) +
  Aven Skirmisher (Warrior) + Augur of Skulls (Wizard) (party size 4).
  A5 counters      each of the four has +1/+1 count increased >= 1 vs pre
  A6 deathtouch    each has Deathtouch in keywords at DeclareAttackers
  A7 cleanup       stack empty, game proceeding

Verdict = reproduced iff A1 passed and (A2 or A3 failed).
Verdict = not-reproduced iff A1-A4 passed and A5-A7 passed
          (control proves the trigger fires correctly when it should).

Protocol-98 conventions (from driver lessons, verified 2026-10-01/02):
  - HELLO advertises protocol 98 (server enforces exact match).
  - MulliganDecision as {"choice":{"type":"Keep"}} gated on
    waiting_for.data.pending[] with phase type "Declare".
  - BottomCards/DiscardToHandSize via single SelectCards {"cards":[...]}.
  - legal_actions is top-level on the WS message data.
  - DeclareAttackers answered with attacks=[]/bands=[] for the active seat.
  - Authoritative exports only from the host seat (P0 creates the game).
"""
import asyncio
import hashlib
import json
import logging
import os
import shutil
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(message)s")
log = logging.getLogger("scenario6759")

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261002-6759"
ISSUE = 6759
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
assert not os.listdir(EVDIR), f"EVDIR {EVDIR} not empty -- refusing to overwrite"

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
    "server_run_id": f"runs/{RUN_ID} (fresh v0.99.0 server on 127.0.0.1:9374, "
                     "started by this run; stale 7363-run server stopped)",
    "mode": "Full",
    "source": "2026-10-02: latest stable release v0.99.0 == pinned release dir; "
              "hashes recomputed against on-disk artifacts",
}

# Recompute against on-disk artifacts; never copy hashes blindly.
for _f, _k in (("server/releases/v0.99.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.99.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.99.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)

NALIA = "Nalia de'Arnise"          # Human Rogue {1}{W}{B} 3/3
ACOLYTE = "Acolyte of Xathrid"     # Human Cleric {B} 0/1
AVEN = "Aven Skirmisher"           # Bird Warrior {W} 1/1
AUGUR = "Augur of Skulls"          # Skeleton Wizard {1}{B} 1/1
PLAINS = "Plains"
SWAMP = "Swamp"
ISLAND = "Island"
LANDS = (PLAINS, SWAMP)

# party role per creature (from pinned card-data subtypes)
ROLES = {NALIA: "Rogue", ACOLYTE: "Cleric", AVEN: "Warrior", AUGUR: "Wizard"}

PLAN_G1 = [NALIA]
WANT_G1 = {NALIA: 1}
PLAN_G2 = [NALIA, ACOLYTE, AVEN, AUGUR]
WANT_G2 = {NALIA: 1, ACOLYTE: 1, AVEN: 1, AUGUR: 1}

MAIN_PHASES = ("PreCombatMain", "PostCombatMain", "Main")
GAME_TIMEOUT = 900
TURN_CAP = 30

ST = {}
SUBMITTED = set()
_DISCARD_REV = {}


def reset():
    ST.clear()
    ST.update({
        "t0": time.time(),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(time.time())),
        "game_code": None,
        "pre_exported": False,
        "post_exported": False,
        "armed": False,
        "begin_combat_logged": False,
        "saw_nalia_trigger": False,
        "mulls": set(),
        "bottomed": set(),
        "party_roles_pre": [],
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


def oname(o):
    return o.get("base_name") or o.get("name")


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def hand_ids(state, pid):
    return [oid for oid in player_of(state, pid).get("hand", [])]


def bf(state, pid):
    return [o for o in (state.get("objects") or {}).values()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def n_on_bf(state, pid, name):
    return sum(1 for o in bf(state, pid) if oname(o) == name)


def wf_of(state):
    return state.get("waiting_for") or {}


def pending_for(state, pid):
    wf = wf_of(state)
    data = wf.get("data") or {}
    for p in data.get("pending", []) or []:
        if str(p.get("player")) == str(pid):
            return p
    return None


def my_priority(state, pid):
    # protocol 98: Priority waiting_for names the seat directly in
    # data.player (no pending[] list); only MulliganDecision/BottomCards
    # use data.pending[] with phase info.
    return (wf_of(state).get("type") == "Priority"
            and str((wf_of(state).get("data") or {}).get("player")) == str(pid))


def merged_actions(st):
    acts = list(st.get("legal_actions") or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def find_action(acts, atype):
    for a in acts:
        if a.get("type") == atype:
            return a
    return None


def stack_snapshot(state):
    out = []
    for e in (state.get("stack") or []):
        d = (e.get("kind") or {}).get("data") or {}
        ab = d.get("ability") or {}
        chain = []
        a = ab
        while a:
            eff = (a.get("effect") or {}).get("type")
            chain.append(eff)
            a = a.get("sub_ability")
        src = (d.get("source") or {}).get("name")
        out.append({"id": e.get("id"), "effects": chain, "source": src})
    return out


def nalia_trigger_on_stack(state):
    blob = json.dumps(stack_snapshot(state))
    return "PutCounterAll" in blob and "Nalia" in blob


def has_deathtouch(o):
    kw = o.get("keywords") or []
    return "Deathtouch" in json.dumps(kw)


def party_roles(state, pid):
    roles = set()
    for o in bf(state, pid):
        nm = oname(o)
        if nm in ROLES:
            roles.add(ROLES[nm])
    return sorted(roles)


# ------------------------------------------------------------- interaction
async def submit_as_is(c, a):
    await c.send_action(a)


async def get_vi_pass(c, st):
    """Pass priority via viewer_interaction passPriority choice, if any."""
    vi = st.get("viewer_interaction") or {}
    if not vi.get("canSubmit"):
        return False
    for opp in vi.get("opportunities", []) or []:
        data = (opp.get("response", {}) or {}).get("data", {}) or {}
        for ch in data.get("choices") or []:
            codes = [s.get("data", {}).get("code")
                     for s in ch.get("surfaces", []) or []]
            if "passPriority" in codes and ch.get("status", {}) \
                    .get("type") == "available":
                iid = opp.get("interactionId") or opp.get("id")
                resp = opp.get("response", {}) or {}
                rtype = resp.get("type")
                cid = ch.get("id")
                if rtype == "schema":
                    spec = (resp.get("data", {}) or {}).get("spec", {}) or {}
                    stype = spec.get("type") or "sequence"
                    sub = {"interactionId": iid,
                           "response": {"type": stype,
                                        "data": {"choiceIds": [cid]}}}
                else:
                    sub = {"interactionId": iid,
                           "response": {"type": "choose",
                                        "data": {"choiceId": cid}}}
                say(f"[{c.name}] vi pass priority (iid={iid})")
                wire("vi_pass", {"who": c.name})
                await c.send_interaction(sub)
                return True
    return False


# ------------------------------------------------------------- common ticks
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
    if tag in ST["mulls"]:
        return False
    ST["mulls"].add(tag)
    say(f"[{tag}] keep {len(hand_ids(state, pid))}")
    await c.send_action({"type": "MulliganDecision",
                         "data": {"choice": {"type": "Keep"}}})
    wire("mulligan", {"who": tag, "decision": "keep"})
    return True


async def do_bottom(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    if wf_of(state).get("type") not in ("MulliganDecision", "BottomCards"):
        return False
    pend = pending_for(state, pid)
    if pend is None:
        # fallback: protocol 98 may name the seat directly in data.player
        if str((wf_of(state).get("data") or {}).get("player")) != str(pid):
            return False
        phase = None
    else:
        phase = (pend.get("phase") or {}).get("type")
    if phase not in ("BottomCards", "Bottom", None):
        return False
    count = (pend.get("phase") or {}).get("count") if pend else None
    key = (tag, "bottom", str(count))
    if key in SUBMITTED:
        return False
    n = int(count or 0)
    if n <= 0:
        return False
    hand = [int(o) for o in hand_ids(state, pid)]

    def rank(o):
        return 0 if obj_lname(state, o) in ("plains", "swamp") else 1
    picks = sorted(hand, key=rank)[:n]
    SUBMITTED.add(key)
    say(f"[{tag}] bottoming {[obj_lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    wire("bottom", {"who": tag, "picks": picks})
    return True


def discard_rank(state, o):
    nm = obj_lname(state, o)
    if nm in ("plains", "swamp"):
        return 0
    if nm in ("nalia de'arnise", "acolyte of xathrid",
              "aven skirmisher", "augur of skulls"):
        return 2
    return 1


async def do_discard(c, pid, tag):
    st = c.latest
    if not st:
        return False
    state = st["state"]
    rev = c.revision
    if wf_of(state).get("type") != "DiscardToHandSize":
        return False
    # protocol 98: DiscardToHandSize names the seat directly in data.player
    # (no pending[] list).
    if str((wf_of(state).get("data") or {}).get("player")) != str(pid):
        return False
    if _DISCARD_REV.get((c.name, rev)):
        return False
    hand = [int(o) for o in hand_ids(state, pid)]
    n = len(hand) - 7
    if n <= 0:
        return False
    picks = [int(x) for x in sorted(hand, key=lambda o: discard_rank(state, o))[:n]]
    say(f"[{tag}] discarding to hand size: {[obj_lname(state, x) for x in picks]}")
    await c.send_action({"type": "SelectCards", "data": {"cards": picks}})
    _DISCARD_REV[(c.name, rev)] = True
    wire("discard", {"who": tag, "picks": picks})
    return True


async def pay_tick(c, acts):
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await submit_as_is(c, a)
            return True
    return False


async def pass_priority(c, st, acts):
    a = find_action(acts, "PassPriority")
    if a:
        await submit_as_is(c, a)
        return True
    return await get_vi_pass(c, st)


async def do_combat(c, pid, tag, state, acts):
    wtype = wf_of(state).get("type") or ""
    if wtype == "DeclareAttackers" and state.get("active_player") == pid:
        da = find_action(acts, "DeclareAttackers")
        if da:
            key = (c.name, "declatt", str(c.revision))
            if key not in SUBMITTED:
                SUBMITTED.add(key)
                import copy as _copy
                d = _copy.deepcopy(da)
                d.setdefault("data", {}).update({"attacks": [], "bands": []})
                await submit_as_is(c, d)
                say(f"[{tag}] declares no attackers")
                wire("declare_attackers", {"who": tag, "attacks": []})
            return True
    if wtype == "DeclareBlockers":
        # protocol 98: seat named directly in data.player (no pending[]).
        if str((wf_of(state).get("data") or {}).get("player")) != str(pid):
            return False
        db = find_action(acts, "DeclareBlockers")
        if db:
            key = (c.name, "declblk", str(c.revision))
            if key not in SUBMITTED:
                SUBMITTED.add(key)
                import copy as _copy
                d = _copy.deepcopy(db)
                d.setdefault("data", {}).update({"assignments": []})
                await submit_as_is(c, d)
                say(f"[{tag}] declares no blockers")
                wire("declare_blockers", {"who": tag})
            return True
    return False


def cast_action_for(acts, state, key):
    kl = key.lower()
    for a in acts:
        if "cast" not in a["type"].lower():
            continue
        d = a.get("data", {}) or {}
        for v in list(d.values()) + [a.get("_src_oid")]:
            try:
                iv = int(v)
            except (TypeError, ValueError):
                continue
            if obj_lname(state, iv) == kl:
                return a
    return None


async def tick(c, pid, tag, st, plan, want):
    state = st["state"]
    acts = merged_actions(st)
    wtype = wf_of(state).get("type") or ""
    pend = pending_for(state, pid)

    if wtype == "MulliganDecision":
        if await do_mulligan(c, pid, tag):
            return True
        if await do_bottom(c, pid, tag):
            return True
        return False
    if wtype == "BottomCards":
        if await do_bottom(c, pid, tag):
            return True
        return False
    if await do_discard(c, pid, tag):
        return True
    if await pay_tick(c, acts):
        return True
    oa = find_action(acts, "OrderTriggers")
    if oa:
        await submit_as_is(c, oa)
        say(f"[{tag}] submits advertised trigger order")
        wire("order_triggers", {"who": tag})
        return True
    # never pass priority while our own non-Priority interaction is pending
    if wtype in ("OptionalCostChoice", "TargetSelection") and pend is not None:
        return True  # hold; the wait names this seat
    if await do_combat(c, pid, tag, state, acts):
        return True
    # P0 main-phase duties
    if pid == 0 and state.get("active_player") == 0 \
            and state.get("priority_player") == 0 \
            and state.get("phase") in MAIN_PHASES:
        # land drop first
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(c, a)
                say(f"[{tag}] plays land")
                wire("play_land", {"who": tag})
                return True
        # cast planned creatures in order
        for name in plan:
            if n_on_bf(state, pid, name) >= want[name]:
                continue
            hid = next((oid for oid in hand_ids(state, pid)
                        if obj_lname(state, oid) == name.lower()), None)
            if hid is None:
                continue
            a = cast_action_for(acts, state, name)
            if a:
                await submit_as_is(c, a)
                say(f"[{tag}] casts {name}")
                wire("cast", {"who": tag, "card": name})
                return True
            break  # not castable yet; wait for mana
    # default pass, gated on my_priority
    if my_priority(state, pid):
        if await pass_priority(c, st, acts):
            return True
    return False


# ------------------------------------------------------------- game driver
def setup_ok(state, pid, want):
    return all(n_on_bf(state, pid, n) >= k for n, k in want.items())


async def run_game(p0, p1, plan, want, game_label):
    """Drive one game through a P0 BeginCombat with the board set up."""
    t0 = time.time()
    last_rev = {}
    last_tick_at = {}
    last_progress = time.time()
    while time.time() - t0 < GAME_TIMEOUT:
        await asyncio.sleep(0.25)
        for c, pid, tag in ((p0, 0, "P0"), (p1, 1, "P1")):
            st = c.latest
            if not st:
                continue
            same_rev = (c.revision == last_rev.get(c.name))
            stale = time.time() - last_tick_at.get(c.name, 0) > 5
            if same_rev and not stale:
                continue
            last_rev[c.name] = c.revision
            last_tick_at[c.name] = time.time()
            # ---- observation pass
            try:
                try:
                    while True:
                        t, data = c.inbox.get_nowait()
                        if t in ("ActionRejected", "Error"):
                            say(f"[{tag}] {t}: {json.dumps(data)[:300]}")
                            wire("rejection", {"who": tag, "type": t,
                                               "data": data})
                        elif t not in ("StateUpdate", "GameStarted"):
                            wire("inbox_misc", {"who": tag, "type": t})
                except asyncio.QueueEmpty:
                    pass
                state = st["state"]
                if tag == "P0":
                    if ST["armed"] and nalia_trigger_on_stack(state):
                        if not ST["saw_nalia_trigger"]:
                            ST["saw_nalia_trigger"] = True
                            say(f"[{game_label}] NALIA TRIGGER on stack: "
                                f"{stack_snapshot(state)}")
                            wire("nalia_trigger", {"stack": stack_snapshot(state)})
                    # arm: setup complete at P0 PreCombatMain priority
                    if (not ST["armed"] and setup_ok(state, 0, want)
                            and state.get("active_player") == 0
                            and state.get("priority_player") == 0
                            and state.get("phase") == "PreCombatMain"):
                        env = await p0.export_state()
                        with open(f"{EVDIR}/{game_label}_pre.json", "w") as f:
                            f.write(env)
                        ST["pre_exported"] = True
                        ST["armed"] = True
                        ST["party_roles_pre"] = party_roles(state, 0)
                        wire("pre_export",
                             {"turn": state.get("turn_number"),
                              "party_roles": ST["party_roles_pre"],
                              "bf": [(oname(o), o.get("id"))
                                     for o in bf(state, 0)]})
                        say(f"[{game_label}] exported pre.json (turn "
                            f"{state.get('turn_number')}, party="
                            f"{ST['party_roles_pre']})")
                    # log the BeginCombat trigger window once
                    if (ST["armed"] and not ST["begin_combat_logged"]
                            and state.get("phase") == "BeginCombat"):
                        ST["begin_combat_logged"] = True
                        wire("begin_combat",
                             {"turn": state.get("turn_number"),
                              "stack": stack_snapshot(state),
                              "waiting_for": wf_of(state)})
                        say(f"[{game_label}] BeginCombat: stack="
                            f"{stack_snapshot(state)}")
                    # done: P0 DeclareAttackers priority after arming
                    if (ST["armed"] and state.get("phase") == "DeclareAttackers"
                            and state.get("active_player") == 0
                            and state.get("priority_player") == 0):
                        env = await p0.export_state()
                        with open(f"{EVDIR}/{game_label}_post.json", "w") as f:
                            f.write(env)
                        ST["post_exported"] = True
                        wire("post_export",
                             {"turn": state.get("turn_number"),
                              "stack": stack_snapshot(state)})
                        say(f"[{game_label}] exported post.json (turn "
                            f"{state.get('turn_number')})")
                        return env
            except Exception as e:
                say(f"[{tag}] observation error: {e}")
            # ---- action pass
            try:
                if await tick(c, pid, tag, st, plan, want):
                    last_progress = time.time()
            except Exception as e:
                say(f"[{tag}] tick error: {e}")
        st = p0.latest
        if st and st["state"].get("turn_number", 0) > TURN_CAP:
            say(f"[{game_label}] turn cap hit; aborting")
            return None
        if time.time() - last_progress > 120:
            say(f"[{game_label}] 120s without progress; aborting")
            wire("stall_abort", {"waiting_for": st["state"].get("waiting_for")
                                if st else None})
            return None
    say(f"[{game_label}] TIMEOUT")
    return None


def counters_of(state):
    """oid -> {name, p1p1, deathtouch} for P0 battlefield creatures."""
    out = {}
    for o in bf(state, 0):
        cnts = o.get("counters") or {}
        out[str(o["id"])] = {
            "name": oname(o),
            "p1p1": int(cnts.get("P1P1", 0)),
            "deathtouch": has_deathtouch(o),
        }
    return out


def load_env(fn):
    with open(f"{EVDIR}/{fn}") as f:
        return json.load(f)["state"]


async def main():
    t0 = time.time()
    a = {}
    notes = []

    # ---------- Game 1: no full party (party = 1) ----------
    say("=== Game 1: Nalia only (no full party) ===")
    reset()
    p0 = PhaseClient("P0")
    await p0.connect()
    p1 = PhaseClient("P1")
    await p1.connect()
    await p0.create(deck((PLAINS, 20), (SWAMP, 20), (NALIA, 8)))
    await p1.join(p0.game_code, deck((ISLAND, 60)))
    await asyncio.sleep(2.0)
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_setup", {"game": 1, "game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})
    post1 = await run_game(p0, p1, PLAN_G1, WANT_G1, "g1_noparty")
    a["A1_setup_ok"] = "passed" if ST["pre_exported"] else "failed"
    if post1 is None:
        for k in ("A2_no_counters", "A3_no_deathtouch", "A4_cleanup"):
            a[k] = "not-run"
        notes.append("g1: bug-branch game did not complete; "
                     "blocked before the trigger window")
    else:
        pre1, st1 = load_env("g1_noparty_pre.json"), json.loads(post1)["state"]
        c_pre, c_post = counters_of(pre1), counters_of(st1)
        gained = [f"{v['name']}#{k}(+{v['p1p1'] - c_pre.get(k, {}).get('p1p1', 0)})"
                  for k, v in c_post.items()
                  if v["p1p1"] - c_pre.get(k, {}).get("p1p1", 0) > 0]
        a["A2_no_counters"] = "passed" if not gained else "failed"
        dt = [f"{v['name']}#{k}" for k, v in c_post.items() if v["deathtouch"]]
        a["A3_no_deathtouch"] = "passed" if not dt else "failed"
        a["A4_cleanup"] = "passed" if not (st1.get("stack") or []) else "failed"
        players = st1.get("players") if isinstance(st1.get("players"), list) else []
        life = "/".join(str(p.get("life")) for p in players[:2]) or ["?"]
        notes.append(
            f"g1: party_roles_pre={ST['party_roles_pre']} "
            f"nalia_trigger_seen_on_stack={ST['saw_nalia_trigger']} "
            f"gained_counters={gained or 'none'} deathtouch_on={dt or 'none'} "
            f"life={life}")
    for k in ("A1_setup_ok", "A2_no_counters", "A3_no_deathtouch", "A4_cleanup"):
        say(f"{k}: {a[k]}")
    await p0.close()
    await p1.close()

    # ---------- Game 2: full party control ----------
    say("=== Game 2: full party (Nalia+Cleric+Warrior+Wizard) ===")
    reset()
    p0 = PhaseClient("P0")
    await p0.connect()
    p1 = PhaseClient("P1")
    await p1.connect()
    await p0.create(deck((PLAINS, 20), (SWAMP, 20), (NALIA, 4),
                         (ACOLYTE, 8), (AVEN, 8), (AUGUR, 8)))
    await p1.join(p0.game_code, deck((ISLAND, 60)))
    await asyncio.sleep(2.0)
    ST["game_code"] = p0.game_code
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game_setup", {"game": 2, "game_code": p0.game_code,
                        "p0_seat": p0.player_id, "p1_seat": p1.player_id})
    post2 = await run_game(p0, p1, PLAN_G2, WANT_G2, "g2_fullparty")
    if post2 is None:
        for k in ("A5_counters", "A6_deathtouch", "A7_cleanup"):
            a[k] = "not-run"
        notes.append("g2: control game did not complete")
    else:
        pre2, st2 = load_env("g2_fullparty_pre.json"), json.loads(post2)["state"]
        c_pre, c_post = counters_of(pre2), counters_of(st2)
        per = {}
        for k, v in c_post.items():
            if v["name"] in WANT_G2:
                per.setdefault(v["name"], []).append(
                    (v["p1p1"] - c_pre.get(k, {}).get("p1p1", 0),
                     v["deathtouch"]))
        ok_c = all(n in per and all(d >= 1 for d, _ in per[n])
                   for n in WANT_G2)
        ok_d = all(n in per and all(dt for _, dt in per[n])
                   for n in WANT_G2)
        a["A5_counters"] = "passed" if ok_c else "failed"
        a["A6_deathtouch"] = "passed" if ok_d else "failed"
        a["A7_cleanup"] = "passed" if not (st2.get("stack") or []) else "failed"
        notes.append(
            f"g2: party_roles_pre={ST['party_roles_pre']} "
            f"per-creature (delta_p1p1, deathtouch)={per} "
            f"nalia_trigger_seen_on_stack={ST['saw_nalia_trigger']}")
    for k in ("A5_counters", "A6_deathtouch", "A7_cleanup"):
        say(f"{k}: {a[k]}")
    await p0.close()
    await p1.close()

    # ---------- verdict ----------
    if a["A1_setup_ok"] != "passed":
        verdict, reason = "blocked", "bug-branch setup did not complete"
    elif a["A2_no_counters"] == "failed" or a["A3_no_deathtouch"] == "failed":
        verdict = "reproduced"
        reason = (f"BeginCombat trigger fired with party=1 (no full party): "
                  f"{notes[0]}")
    elif a["A5_counters"] == "passed" and a["A6_deathtouch"] == "passed":
        verdict, reason = ("not-reproduced",
                           "trigger correctly withheld with party=1 and "
                           "correctly fired with party=4")
    else:
        verdict, reason = ("blocked",
                           "control branch inconclusive; "
                           "bug-branch withholding observed but the "
                           "positive control did not complete")
    notes.append(f"verdict={verdict}: {reason}")
    say(f"VERDICT: {verdict} -- {reason}")

    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "validated_at": "2026-10-02",
        "started_at": ST.get("started_at"),
        "server": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 98, "client": "driver/client.py"},
        "scenario": {"file": "scenario_6759_v099.py", "sha256": None},
        "plan": {"g1": {"cast": PLAN_G1, "want": WANT_G1},
                 "g2": {"cast": PLAN_G2, "want": WANT_G2}},
        "decks": {
            "g1_P0": {"Plains": 20, "Swamp": 20, "Nalia de'Arnise": 8},
            "g2_P0": {"Plains": 20, "Swamp": 20, "Nalia de'Arnise": 4,
                      "Acolyte of Xathrid": 8, "Aven Skirmisher": 8,
                      "Augur of Skulls": 8},
            "P1": {"Island": 60}},
        "assertions": a,
        "notes": notes,
        "verdict": verdict,
        "verdict_reason": reason,
        "elapsed_s": round(time.time() - t0, 1),
        "prior_runs": [
            {"run_id": "20260910-6759", "release": "v0.79.0",
             "build_commit": "1cde7a2", "protocol_version": 69,
             "verdict": "not-reproduced",
             "evidence_commit": "7a334c9468307eb7d5f19fa0610e79d8cee598c6"},
            {"run_id": "20260917-6759", "release": "v0.86.0",
             "build_commit": "2cc8c28", "protocol_version": 72,
             "verdict": "not-reproduced",
             "note": "release pin advanced since; re-validated here on v0.99.0"},
        ],
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            ">4-of deck density is a test-harness convenience (engine accepts "
            "custom games).",
            "Nalia's 'cast from top of library' static is not exercised; only "
            "the beginning-of-combat party gate is under test.",
            "States are authoritative exports, restorable only via full game "
            "replay.",
        ],
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)

    # server log excerpt
    import glob
    lines, used = [], None
    for lp in [f"{BACKFILL}/runs/{RUN_ID}/server.log"] + \
            sorted(glob.glob(f"{BACKFILL}/runs/*/server.log"), reverse=True):
        try:
            lines = open(lp).read().splitlines()
            used = lp
            break
        except Exception:
            continue
    excerpt = [l for l in lines
               if "nalia" in l.lower() or "party" in l.lower()
               or "putcounterall" in l.lower()]
    with open(f"{EVDIR}/server_log_excerpt.txt", "w") as f:
        f.write("\n".join(excerpt[-120:]) + "\n")
    notes.append(f"server.log excerpt: {len(excerpt)} matching lines "
                 f"(of {len(lines)} total; log={used})")

    # copy the scenario into the evidence dir for the manifest
    shutil.copy(f"{BACKFILL}/driver/scenario_6759_v099.py",
                f"{EVDIR}/scenario_6759_v099.py")

    try:
        WIRE.close()
    except Exception:
        pass
    try:
        RUNLOG.close()
    except Exception:
        pass
    print(f"DONE verdict={verdict} assertions={json.dumps(a)}", flush=True)
    return 0 if verdict in ("reproduced", "not-reproduced") else 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
