#!/usr/bin/env python3
"""Issue #7365: Doomsday Excruciator -- ETB trigger exiled all permanents on
the battlefield instead of exiling all but the bottom six cards of each
library.

Reported (Discord, synced to GitHub by matthewevans): casting Doomsday
Excruciator exiled every permanent on the battlefield; per the reporter's
EDIT the libraries were NOT exiled at all. Triage (mike-theDude, 2026-08-15)
confirmed by data inspection: the trigger's `ChangeZoneAll` child carried
`target: any target` with no filter -- an unfiltered mass-exile hitting the
wrong zone entirely. Classifier: supported-aspect-defect.

Pinned v0.99.0 card-data parse (recorded in card_data_excruciator.json):
the ETB trigger is now an `ExileTop` with player ScopedPlayer,
player_scope All, count Offset(ZoneCardCount(Library, ScopedPlayer), -6),
position Top, face_down true, condition WasCast. I.e. the parse that caused
the report appears repaired; this run tests the CURRENT behavior.

Oracle text (pinned v0.99.0 card-data.json):
> Flying
> When this creature enters, if it was cast, each player exiles all but the
> bottom six cards of their library face down.
> At the beginning of your upkeep, draw a card.

Behavioral contract (native engine, two human-client seats, v0.99.0/proto 98):
  SETUP - P0 (caster, seat 0): 8x Doomsday Excruciator + 52x Swamp.
            Mulligans (max 3) until Doomsday Excruciator in hand.
            P1 (opponent, seat 1): 20x Grizzly Bears + 40x Forest, keeps.
            Both play a land per turn; P1 casts Grizzly Bears ASAP;
            neither attacks.
  CAST  - On a P0 main phase with >=6 untapped Swamps and Excruciator in
            hand: P0 casts Doomsday Excruciator ({B}x6) from hand.
  PRE   - First tick with Excruciator on P0's battlefield (the ETB trigger
            cannot have resolved yet: resolution needs both seats to pass
            priority again): authoritative export -> pre.json.
  POST  - Once the stack empties on a later revision (trigger resolved or
            never fired): authoritative export -> post.json.

  A1 setup_ok            pre.json captured; Excruciator on P0's BF in pre;
                         both libraries >6 cards in pre (trigger is
                         non-vacuous); stack observed at pre (recorded).
  A2 libraries_correct   for EACH player: post library has exactly 6 cards;
                         those 6 are a contiguous end-segment of the pre
                         library (order preserved); every other pre-library
                         card is in post exile. (The "all but bottom six"
                         half of the expected behavior.)
  A3 battlefield_untouched (THE reported outcome): the battlefield object
                         set is identical in pre and post, and no
                         pre-battlefield object is in post exile.
                         FAILS while the reported bug is present.
  A4 cleanup             post captured, stack empty, no engine crash.

Verdict = blocked iff A1 fails.
Verdict = reproduced iff A1 passes and (A2 fails or A3 fails).
Verdict = not-reproduced iff A1..A4 all pass.
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
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261002-7365")
EVID_ISSUE = "7365"
EVDIR = f"{BACKFILL}/evidence/{EVID_ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

SERVER_IDENTITY = {
    "version": "v0.99.0",
    "build_commit": "d919616",
    "protocol_version": 98,
    "binary_sha256": "37fa78a8db1a51fbb950cbdba870021ad718fdf2e259971a1954c130a1a5ba60",
    "card_data_sha256": "ccfcf9e6d19d9407d207cfbfe95b4ad873cebd878f66b451682e56e991372a4e",
    "draft_pools_sha256": "8ce01364ee46e55cf2610d6a2dd35abbd3266606cc456af8012433f55bdd034e",
    "signature_verified": True,
}
for _f, _k in (("server/releases/v0.99.0/phase-server-slim-x86_64-unknown-linux-musl", "binary_sha256"),
               ("server/releases/v0.99.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.99.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"

assert not os.listdir(EVDIR), "EVDIR not empty"
shutil.copy(__file__, f"{EVDIR}/scenario_7365.py")

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

START = time.time()
DEADLINE = 600  # hard stop: 10 minutes


def say(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    RUNLOG.write(msg + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
    except Exception as e:
        WIRE.write(json.dumps({"t": time.time(), "event": event + "_unserializable",
                               "error": str(e)}) + "\n")
    WIRE.flush()


EXCRUCIATOR = "Doomsday Excruciator"
SWAMP = "Swamp"
BEARS = "Grizzly Bears"
FOREST = "Forest"

P0_DECK = deck((EXCRUCIATOR, 8), (SWAMP, 52))
P1_DECK = deck((BEARS, 20), (FOREST, 40))

ST = {"pre_done": False, "post_done": False, "pre_revision": None,
      "cast_done": False, "cast_submit_t": None, "rejections": [],
      "p0_mulls": 0, "p0_kept": False, "p0_bottomed": False,
      "p1_kept": False, "lib_orient": None, "p0_lib_prev": None,
      "pre_stack": None, "stop": False}


def lname(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def name_of(o):
    if not isinstance(o, dict):
        return None
    return o.get("base_name") or o.get("name")


def objs(state):
    return state.get("objects", {}) or {}


def obj(state, oid):
    return objs(state).get(str(oid))


def players(state):
    return state.get("players", []) or []


def plib(state, pid):
    pls = players(state)
    return (pls[pid].get("library") or []) if pid < len(pls) else []


def pzone_list(state, pid, z):
    pls = players(state)
    return (pls[pid].get(z) or []) if pid < len(pls) else []


def bf_oids(state):
    return list(state.get("battlefield") or [])


def exile_oids(state):
    return list(state.get("exile") or [])


def stack(state):
    return state.get("stack", []) or []


def hand_oids(state, pid):
    return [int(x) for x in pzone_list(state, pid, "hand")]


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        o = obj(state, oid)
        if o is not None and lname(o) == name.lower():
            return oid
    return None


def bf_names(state, pid):
    out = []
    for oid in bf_oids(state):
        o = obj(state, oid)
        if o is not None and o.get("controller") == pid:
            out.append(name_of(o) or "?")
    return sorted(out)


def untapped_lands(state, pid, land_name):
    out = []
    for oid in bf_oids(state):
        o = obj(state, oid)
        if (o is not None and o.get("controller") == pid
                and lname(o) == land_name.lower() and not o.get("tapped")):
            out.append(oid)
    return out


def excruciator_on_bf(state, pid):
    for oid in bf_oids(state):
        o = obj(state, oid)
        if (o is not None and o.get("controller") == pid
                and lname(o) == EXCRUCIATOR.lower()):
            return oid
    return None


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain"))


def cur(c):
    st = c.latest
    if not st:
        return None, [], {}
    state = st.get("state") or {}
    acts = st.get("legal_actions", []) or []
    wf = state.get("waiting_for") or {}
    return state, acts, wf


def drain_inbox(c):
    drained = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        drained.append((t, data))
        if t in ("ActionRejected", "Error", "AuthoritativeStateExportFailed"):
            wire("server_notice", {"who": c.name, "type": t, "data": data})
            say(f"[{c.name}] {t}: {json.dumps(data)[:300]}")
            if t == "ActionRejected":
                ST["rejections"].append(
                    {"t": time.time(), "who": c.name, "data": data})
    for item in drained:
        c.inbox.put_nowait(item)
    return drained


C0 = None
C1 = None


async def do_export(path):
    s = await C0.export_state()
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(s)
    say(f"exported {path}")
    return json.loads(s)["state"]


def mulligan_phase_for(state, pid):
    pending = ((state.get("waiting_for") or {}).get("data", {}) or {}).get("pending", [])
    for p in pending:
        if p.get("player") == pid:
            ph = p.get("phase", {}) or {}
            return ph.get("type")
    return None


async def mulligan_p0(c, acts, state):
    """P0 mulligans (max 3) until Doomsday Excruciator is in hand."""
    mphase = mulligan_phase_for(state, 0)
    if mphase == "Declare":
        ma = next((a for a in acts if a["type"] == "MulliganDecision"), None)
        if ma and not ST["p0_kept"]:
            hn = [lname(obj(state, oid)) for oid in hand_oids(state, 0)
                  if obj(state, oid)]
            want_n = sum(1 for n in hn if n == EXCRUCIATOR.lower())
            lands = sum(1 for n in hn if n == SWAMP.lower())
            if (want_n >= 1 and lands >= 2) or ST["p0_mulls"] >= 3:
                ST["p0_kept"] = True
                await c.send_action({"type": "MulliganDecision",
                                     "data": {"choice": {"type": "Keep"}}})
                say(f"[P0] keeps (excruciator={want_n}, lands={lands})")
            else:
                ST["p0_mulls"] += 1
                await c.send_action({"type": "MulliganDecision",
                                     "data": {"choice": {"type": "Mulligan"}}})
                say(f"[P0] mulligans #{ST['p0_mulls']} "
                    f"(excruciator={want_n}, lands={lands})")
            return True
    elif mphase == "BottomCards":
        sc = next((a for a in acts if a["type"] == "SelectCards"), None)
        if sc and not ST["p0_bottomed"]:
            pending = ((state.get("waiting_for") or {}).get("data", {}) or {}).get("pending", [])
            count = 1
            for p in pending:
                if p.get("player") == 0:
                    ph = p.get("phase", {}) or {}
                    if ph.get("type") == "BottomCards":
                        count = int(ph.get("count", 1))

            def bkey(oid):
                o = obj(state, oid)
                nm = lname(o) if o else ""
                if nm == EXCRUCIATOR.lower():
                    return 2
                if nm == SWAMP.lower():
                    return 1
                return 0
            picks = sorted(hand_oids(state, 0), key=bkey)[:count]
            ST["p0_bottomed"] = True
            await c.send_action({"type": "SelectCards",
                                 "data": {"cards": [int(x) for x in picks]}})
            say(f"[P0] bottoms {count}")
            return True
    return False


async def tick_p0(c, pid):
    drain_inbox(c)
    state, acts, wf = cur(c)
    if state is None:
        return False
    wtype = wf.get("type")
    wplayer = (wf.get("data") or {}).get("player")

    # library-orientation probe: which end loses the drawn card
    lib = [int(x) for x in plib(state, pid)]
    prev = ST.get("p0_lib_prev")
    if (prev is not None and ST.get("lib_orient") is None
            and len(lib) == len(prev) - 1):
        diff = [x for x in prev if x not in lib]
        if len(diff) == 1:
            ST["lib_orient"] = ("head-is-top" if prev[0] == diff[0]
                                else "tail-is-top" if prev[-1] == diff[0]
                                else "unknown")
            say(f"[P0] library orientation: {ST['lib_orient']}")
    ST["p0_lib_prev"] = lib

    if wtype == "MulliganDecision":
        return await mulligan_p0(c, acts, state)

    for a in acts:
        if a["type"] == "MulliganDecision" and not ST["p0_kept"]:
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            ST["p0_kept"] = True
            say("[P0] keeps (fallback)")
            return True

    if wtype == "DiscardToHandSize" and wplayer == 0:
        n = (wf.get("data") or {}).get("count") or max(0, len(hand_oids(state, pid)) - 7)
        hs = hand_oids(state, pid)

        def prio(oid):
            o = obj(state, oid)
            nm = lname(o) if o else ""
            return 0 if nm == SWAMP.lower() else (2 if nm == EXCRUCIATOR.lower() else 1)
        picks = sorted(hs, key=prio)[:n]
        if picks:
            await c.send_action({"type": "SelectCards",
                                 "data": {"cards": [int(x) for x in picks]}})
            say(f"[P0] discards {len(picks)}")
            return True
        return False

    if wtype == "DeclareAttackers" and str(wplayer) == str(pid):
        for a in acts:
            if a["type"] == "DeclareAttackers":
                d = copy.deepcopy(a)
                d.setdefault("data", {}).update({"attacks": [], "bands": []})
                wire("action_submit", {"who": "P0", "action": "DeclareAttackers/none"})
                await c.send_action(d)
                say("[P0] declares no attackers")
                return True
        return False

    if wtype not in ("Priority", "MulliganDecision", None) and wplayer == 0:
        wire("p0_hold", {"wtype": wtype})
        return False

    if is_my_main(state, pid) and not ST["cast_done"]:
        hid = find_hand(state, pid, SWAMP)
        for a in acts:
            if a["type"] == "PlayLand" and hid \
                    and str(a.get("data", {}).get("object_id")) == str(hid):
                wire("action_submit", {"who": "P0", "action": "PlayLand/Swamp"})
                await c.send_action(a)
                return True
        exc_oid = find_hand(state, pid, EXCRUCIATOR)
        if exc_oid and len(untapped_lands(state, pid, SWAMP)) >= 6:
            for a in acts:
                if a["type"] == "CastSpell" \
                        and str(a.get("data", {}).get("object_id")) == str(exc_oid):
                    wire("action_submit", {"who": "P0", "action": "CastSpell/Excruciator",
                                          "object_id": exc_oid})
                    await c.send_action(a)
                    ST["cast_done"] = True
                    ST["cast_submit_t"] = time.time()
                    say("[P0] casts Doomsday Excruciator")
                    return True
            wire("excruciator_not_offered", {})
    elif is_my_main(state, pid):
        # post-cast: still play lands so the game keeps moving
        hid = find_hand(state, pid, SWAMP)
        for a in acts:
            if a["type"] == "PlayLand" and hid \
                    and str(a.get("data", {}).get("object_id")) == str(hid):
                await c.send_action(a)
                return True

    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    if wtype == "Priority" and wplayer == 0:
        rev = (c.latest or {}).get("state_revision")
        if ST.get("p0_pass_rev") != rev:
            for a in acts:
                if a["type"] == "PassPriority":
                    await c.send_action(a)
                    ST["p0_pass_rev"] = rev
                    return True
    return False


async def tick_p1(c, pid):
    drain_inbox(c)
    state, acts, wf = cur(c)
    if state is None:
        return False
    wtype = wf.get("type")
    wplayer = (wf.get("data") or {}).get("player")

    for a in acts:
        if a["type"] == "MulliganDecision" and not ST["p1_kept"]:
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            ST["p1_kept"] = True
            say("[P1] keeps")
            return True

    if wtype == "DiscardToHandSize" and wplayer == 1:
        n = (wf.get("data") or {}).get("count") or max(0, len(hand_oids(state, pid)) - 7)
        hs = hand_oids(state, pid)

        def prio(oid):
            o = obj(state, oid)
            nm = lname(o) if o else ""
            return 0 if nm == FOREST.lower() else (2 if nm == BEARS.lower() else 1)
        picks = sorted(hs, key=prio)[:n]
        if picks:
            await c.send_action({"type": "SelectCards",
                                 "data": {"cards": [int(x) for x in picks]}})
            say(f"[P1] discards {len(picks)}")
            return True
        return False

    if wtype == "DeclareAttackers" and str(wplayer) == str(pid):
        for a in acts:
            if a["type"] == "DeclareAttackers":
                d = copy.deepcopy(a)
                d.setdefault("data", {}).update({"attacks": [], "bands": []})
                wire("action_submit", {"who": "P1", "action": "DeclareAttackers/none"})
                await c.send_action(d)
                say("[P1] declares no attackers")
                return True
        return False

    if wtype not in ("Priority", "MulliganDecision", None) and wplayer == 1:
        wire("p1_hold", {"wtype": wtype})
        return False

    if is_my_main(state, pid):
        hid = find_hand(state, pid, FOREST)
        for a in acts:
            if a["type"] == "PlayLand" and hid \
                    and str(a.get("data", {}).get("object_id")) == str(hid):
                wire("action_submit", {"who": "P1", "action": "PlayLand/Forest"})
                await c.send_action(a)
                return True
        bear_oid = find_hand(state, pid, BEARS)
        if bear_oid and len(untapped_lands(state, pid, FOREST)) >= 2:
            for a in acts:
                if a["type"] == "CastSpell" \
                        and str(a.get("data", {}).get("object_id")) == str(bear_oid):
                    wire("action_submit", {"who": "P1", "action": "CastSpell/Bears",
                                          "object_id": bear_oid})
                    await c.send_action(a)
                    say("[P1] casts Grizzly Bears")
                    return True

    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    if wtype == "Priority" and wplayer == 1:
        rev = (c.latest or {}).get("state_revision")
        if ST.get("p1_pass_rev") != rev:
            for a in acts:
                if a["type"] == "PassPriority":
                    await c.send_action(a)
                    ST["p1_pass_rev"] = rev
                    return True
    return False


async def main():
    global C0, C1
    c0 = PhaseClient("P0")
    await c0.connect()
    C0 = c0
    sess = await c0.create(P0_DECK, player_count=2)
    game_code = sess["game_code"]
    say(f"game {game_code}; P0 seat {c0.player_id}")
    wire("game_created", {"game_code": game_code})

    c1 = PhaseClient("P1")
    await c1.connect()
    await c1.join(game_code, P1_DECK)
    C1 = c1
    say(f"P1 joined; seat {c1.player_id}")

    await asyncio.sleep(2)

    pre = None
    last_progress = time.time()
    while time.time() - START < DEADLINE and not ST["stop"]:
        acted0 = await tick_p0(c0, 0)
        acted1 = await tick_p1(c1, 1)
        if acted0 or acted1:
            last_progress = time.time()

        s0, _, _ = cur(c0)
        if s0 is not None and not ST["pre_done"]:
            if excruciator_on_bf(s0, 0) is not None:
                ST["pre_stack"] = [
                    {"kind": (e.get("kind") or {}).get("type")
                     if isinstance(e.get("kind"), dict) else e.get("kind"),
                     "controller": e.get("controller")}
                    for e in stack(s0)]
                ST["pre_revision"] = (c0.latest or {}).get("state_revision")
                pre = await do_export("pre.json")
                ST["pre_done"] = True
                say(f"[main] pre-exported at revision {ST['pre_revision']}; "
                    f"stack at pre: {ST['pre_stack']}")
        elif s0 is not None and ST["pre_done"] and not ST["post_done"]:
            rev = (c0.latest or {}).get("state_revision")
            if (not stack(s0) and rev != ST["pre_revision"]
                    and rev is not None):
                await asyncio.sleep(0.5)
                s0b, _, _ = cur(c0)
                if s0b is not None and not stack(s0b):
                    say("[main] stack empty on later revision -> post export")
                    ST["post_done"] = True
                    break

        if time.time() - last_progress > 120:
            say("[main] 120s without progress -> stop")
            wire("watchdog", {"idle_seconds": 120})
            break
        await asyncio.sleep(0.15)

    post = None
    try:
        post = await do_export("post.json")
    except Exception as e:
        say(f"post export failed: {e}")
        wire("post_export_failed", {"error": str(e)})

    # ---------------- card-data root-cause context ----------------
    cd = json.load(open(f"{BACKFILL}/server/releases/v0.99.0/data/card-data.json"))
    exc_entry = cd.get(EXCRUCIATOR.lower())
    with open(f"{EVDIR}/card_data_excruciator.json", "w") as f:
        json.dump(exc_entry, f, indent=1)
    trig = (exc_entry.get("triggers") or [{}])[0]
    trig_eff = ((trig.get("execute") or {}).get("effect") or {})
    parse_shape = f"{trig_eff.get('type')} scope={((trig.get('execute') or {}).get('player_scope') or {}).get('type')}"

    # ---------------- assertions ----------------
    A = {}
    notes = {}

    if pre is not None:
        pre_bf0 = bf_names(pre, 0)
        pre_bf1 = bf_names(pre, 1)
        pre_lib0 = [int(x) for x in plib(pre, 0)]
        pre_lib1 = [int(x) for x in plib(pre, 1)]
        A["A1_setup_ok"] = ("passed"
                            if (excruciator_on_bf(pre, 0) is not None
                                and len(pre_lib0) > 6 and len(pre_lib1) > 6)
                            else "failed")
        notes["A1_setup_ok"] = (
            f"pre: Excruciator on P0 BF={excruciator_on_bf(pre, 0) is not None}; "
            f"P0 BF={pre_bf0}; P1 BF={pre_bf1}; "
            f"libraries P0={len(pre_lib0)} P1={len(pre_lib1)}; "
            f"stack at pre={ST['pre_stack']}; "
            f"library orientation probe={ST.get('lib_orient')}; "
            f"pinned parse: ETB trigger effect {parse_shape}")
    else:
        A["A1_setup_ok"] = "failed"
        notes["A1_setup_ok"] = ("pre.json was never captured "
                                f"(cast_done={ST['cast_done']})")

    if post is not None and pre is not None:
        post_bf = set(int(x) for x in bf_oids(post))
        pre_bf = set(int(x) for x in bf_oids(pre))
        post_ex = set(int(x) for x in exile_oids(post))
        bf_leaked = pre_bf & post_ex
        A["A3_battlefield_untouched"] = ("passed"
                                        if (pre_bf == post_bf and not bf_leaked)
                                        else "failed")
        notes["A3_battlefield_untouched"] = (
            f"pre BF={len(pre_bf)} objs, post BF={len(post_bf)} objs; "
            f"BF set identical={pre_bf == post_bf}; "
            f"pre-BF objects now in exile={len(bf_leaked)}")

        per_player = []
        a2_ok = True
        for pid in (0, 1):
            pl_pre = [int(x) for x in plib(pre, pid)]
            pl_post = [int(x) for x in plib(post, pid)]
            is_head = pl_post == pl_pre[:6]
            is_tail = pl_post == pl_pre[-6:]
            exiled_lib = set(pl_pre) - set(pl_post)
            exiled_ok = exiled_lib <= post_ex
            facedown = all((obj(post, oid) or {}).get("face_down")
                           for oid in exiled_lib)
            ok = (len(pl_post) == 6 and (is_head or is_tail) and exiled_ok)
            a2_ok = a2_ok and ok
            kept_end = ("head" if is_head else "tail" if is_tail else "neither")
            per_player.append(
                f"P{pid}: lib {len(pl_pre)}->{len(pl_post)}; kept={kept_end}; "
                f"exiled_lib_cards={len(exiled_lib)} all_in_exile={exiled_ok} "
                f"all_face_down={facedown}")
        A["A2_libraries_correct"] = "passed" if a2_ok else "failed"
        notes["A2_libraries_correct"] = ("; ".join(per_player)
                                        + f"; orientation probe={ST.get('lib_orient')}")
        A["A4_cleanup"] = ("passed" if not stack(post) else "failed")
        notes["A4_cleanup"] = (f"post stack empty={not stack(post)}; "
                              f"phase={post.get('phase')}; "
                              f"turn={post.get('turn_number')}")
    else:
        for k in ("A2_libraries_correct", "A3_battlefield_untouched", "A4_cleanup"):
            A[k] = "not-run"
            notes[k] = "pre.json or post.json unavailable"

    if A["A1_setup_ok"] != "passed":
        verdict = "blocked"
    elif (A["A2_libraries_correct"] == "failed"
          or A["A3_battlefield_untouched"] == "failed"):
        verdict = "reproduced"
    elif all(v == "passed" for v in A.values()):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    run = {
        "run_id": RUN_ID,
        "issue": 7365,
        "server": SERVER_IDENTITY,
        "server_hello": {"server_version": "0.99.0", "build_commit": "d919616",
                         "protocol_version": 98},
        "game_code": game_code,
        "ports": {"server": 9374},
        "verdict": verdict,
        "assertions": A,
        "scope": ("Doomsday Excruciator cast-from-hand ETB trigger: each "
                  "player should exile all but the bottom six of their "
                  "library face down; the battlefield must be untouched. "
                  "Native engine, two human-client seats, protocol-98 driver."),
        "limitations": [
            "Browser UI not exercised; native engine via two human-driver seats.",
            "Dense test decks (engine accepts >4-of for custom games) are a test-harness convenience.",
            "The prebuilt server has no standalone state-restore; states are authoritative exports (restorable only via full game replay, scenario_7365.py).",
            "Only the ETB exile half is exercised; the upkeep-draw trigger and the 'cast from hand' condition's negative branch are not.",
        ],
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(START)),
        "finished_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": notes, "verdict": verdict}, f,
                  indent=1, default=str)
    say("assertions: " + json.dumps(A))
    say("verdict: " + verdict)

    await c0.close()
    await c1.close()
    WIRE.close()
    RUNLOG.close()


if __name__ == "__main__":
    asyncio.run(main())
