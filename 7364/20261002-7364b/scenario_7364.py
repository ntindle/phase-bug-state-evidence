#!/usr/bin/env python3
"""Issue #7364: Supper for Spiders -- "Does not put creatures from my opponents
graveyard to the battlefield."

Reported (Discord, synced to GitHub by matthewevans): casting Supper for
Spiders does nothing -- no creatures move from opponents' graveyards to the
battlefield. Triage (mike-theDude, 2026-08-15) confirmed by data inspection:
the card's spell effect is Unimplemented in the published card data
(`unparsed_verb_arguments` head + `unbound_subject` child), i.e. the parser
cannot build either clause: the this-turn zone-history filter ("...put there
from the battlefield this turn") or the mass Food-artifact type-replacing
rider. Classifier: unsupported-aspect (known gap, not a hidden defect).

Oracle text (pinned v0.99.0 card-data.json):
> Put onto the battlefield under your control all creature cards in your
> opponents' graveyards that were put there from the battlefield this turn.
> They are Food artifacts with "{2}, {T}, Sacrifice this artifact: You gain
> 3 life." (They lose all other types and subtypes.)

Behavioral contract (native engine, two human-client seats, v0.99.0/proto 98):
  SETUP - P0 (opponent, seat 0): 20x Grizzly Bears + 40x Forest.
            P1 (caster, seat 1): 12x Supper for Spiders + 12x Infest + 36x Swamp.
            Both keep. P0 plays Forest, casts Grizzly Bears ASAP.
  KILL  - On a P1 main phase with >=5 untapped Swamps, Bears on the
            battlefield under P0, and both Infest and Supper for Spiders in
            hand: P1 casts Infest (all creatures get -2/-2; Bears die).
  PRE   - Once Infest resolved and >=1 Bears is in P0's graveyard with an
            empty stack: authoritative export -> pre.json.
  CAST  - Same turn, P1 casts Supper for Spiders (instant, {1}{B}).
  POST  - After the cast resolves (or is refused / never offered): export
            post.json.

  A1 setup_ok            pre.json shows >=1 Grizzly Bears in P0's graveyard
                         and Supper for Spiders in P1's hand.
  A2 cast_attempted      a CastSpell submission for Supper for Spiders was
                         sent (accepted or rejected -- recorded in notes).
  A3 expected_return     >=1 of the dead Bears is on the battlefield under
                         P1's control in post.json (FAILS while the bug is
                         present: nothing moves).
  A4 expected_food       >=1 Food-artifact permanent under P1's control in
                         post.json (FAILS while the bug is present).
  A5 card_unimplemented  pinned card-data marks the spell effect
                         Unimplemented (root-cause confirmation).
  A6 cleanup             post captured, stack empty, no engine crash.

Verdict = blocked iff A1 fails.
Verdict = reproduced iff A1 passes and (A2 fails or A3 fails or A4 fails).
Verdict = not-reproduced iff A1..A6 all pass.
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
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20261002-7364b")
EVID_ISSUE = "7364"
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
shutil.copy(__file__, f"{EVDIR}/scenario_7364.py")

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


BEARS = "Grizzly Bears"
FOREST = "Forest"
SUPPER = "Supper for Spiders"
INFEST = "Infest"
SWAMP = "Swamp"

P0_DECK = deck((BEARS, 20), (FOREST, 40))
P1_DECK = deck((SUPPER, 12), (INFEST, 12), (SWAMP, 36))

ST = {"infest_cast": False, "pre_done": False, "supper_cast": False,
      "supper_done": False, "supper_miss": 0, "supper_never_offered": False,
      "supper_rejected": None, "stop": False, "pre_bears_gy": [],
      "supper_post_zone": None}


def lname(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def name_of(o):
    if not isinstance(o, dict):
        return None
    return o.get("base_name") or o.get("name")


def objs(state):
    return state.get("objects", {}) or {}


def zone_objs(state, pid, zone):
    return [(oid, o) for oid, o in objs(state).items()
            if o.get("zone") == zone and o.get("controller") == pid]


def zone_names(state, pid, zone):
    return sorted([name_of(o) or "?" for _, o in zone_objs(state, pid, zone)])


def hand(state, pid):
    return [oid for oid, o in zone_objs(state, pid, "Hand")]


def stack(state):
    return state.get("stack", []) or []


def find_hand(state, pid, name):
    for oid in hand(state, pid):
        if lname(objs(state)[oid]) == name.lower():
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
    """Non-blocking drain; wire-log rejections/errors. Returns list of
    (type, data) drained (re-queued at the tail so waiters still see them)."""
    drained = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        drained.append((t, data))
        if t in ("ActionRejected", "Error", "AuthoritativeStateExportFailed"):
            wire("server_notice", {"who": c.name, "type": t,
                                   "data": data})
            say(f"[{c.name}] {t}: {json.dumps(data)[:300]}")
            if t == "ActionRejected" and ST.get("supper_cast") and \
                    ST.get("supper_rejected") is None:
                ST["supper_rejected"] = data
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


# ------------------------------------------------------------- tick (P0: opponent)

async def tick_p0(c, pid):
    drain_inbox(c)
    state, acts, wf = cur(c)
    if state is None:
        return False
    wtype = wf.get("type")
    wplayer = (wf.get("data") or {}).get("player")

    for a in acts:
        if a["type"] == "MulliganDecision":
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            say("[P0] keeps")
            return True

    if wtype == "DiscardToHandSize" and wplayer == 0:
        n = (wf.get("data") or {}).get("count") or max(0, len(hand(state, pid)) - 7)
        # discard lands first
        hs = hand(state, pid)
        lands = [oid for oid in hs if lname(objs(state)[oid]) == FOREST.lower()]
        picks = (lands + [oid for oid in hs if oid not in lands])[:n]
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
                wire("action_submit", {"who": "P0",
                                      "action": "DeclareAttackers/none"})
                await c.send_action(d)
                say("[P0] declares no attackers")
                return True
        return False

    if wtype not in ("Priority", None) and wplayer == 0:
        wire("p0_hold", {"wtype": wtype})
        return False

    if is_my_main(state, pid):
        hid = find_hand(state, pid, FOREST)
        for a in acts:
            if a["type"] == "PlayLand" and hid \
                    and str(a.get("data", {}).get("object_id")) == hid:
                wire("action_submit", {"who": "P0", "action": "PlayLand/Forest"})
                await c.send_action(a)
                return True
        oid = find_hand(state, pid, BEARS)
        if oid:
            for a in acts:
                if a["type"] == "CastSpell" \
                        and str(a.get("data", {}).get("object_id")) == oid:
                    wire("action_submit", {"who": "P0", "action": "CastSpell/Bears",
                                          "object_id": oid})
                    await c.send_action(a)
                    say("[P0] casts Grizzly Bears")
                    return True

    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    if wtype == "Priority" and wplayer == 0:
        for a in acts:
            if a["type"] == "PassPriority":
                await c.send_action(a)
                return True
    return False


# ------------------------------------------------------------- tick (P1: caster)

async def tick_p1(c, pid):
    drain_inbox(c)
    state, acts, wf = cur(c)
    if state is None:
        return False
    wtype = wf.get("type")
    wplayer = (wf.get("data") or {}).get("player")

    for a in acts:
        if a["type"] == "MulliganDecision":
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            say("[P1] keeps")
            return True

    if wtype == "DiscardToHandSize" and wplayer == 1:
        n = (wf.get("data") or {}).get("count") or max(0, len(hand(state, pid)) - 7)
        hs = hand(state, pid)

        def prio(oid):
            nm = lname(objs(state)[oid])
            if nm == SWAMP.lower():
                return 0
            if nm == INFEST.lower():
                return 1
            if nm == SUPPER.lower():
                return 2
            return 3
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
                wire("action_submit", {"who": "P1",
                                      "action": "DeclareAttackers/none"})
                await c.send_action(d)
                say("[P1] declares no attackers")
                return True
        return False

    if wtype not in ("Priority", None) and wplayer == 1:
        wire("p1_hold", {"wtype": wtype})
        return False

    if is_my_main(state, pid):
        hid = find_hand(state, pid, SWAMP)
        for a in acts:
            if a["type"] == "PlayLand" and hid \
                    and str(a.get("data", {}).get("object_id")) == hid:
                wire("action_submit", {"who": "P1", "action": "PlayLand/Swamp"})
                await c.send_action(a)
                return True

        untapped_swamps = [oid for oid, o in zone_objs(state, pid, "Battlefield")
                           if lname(o) == SWAMP.lower() and not o.get("tapped")]
        bears_bf = [oid for oid, o in zone_objs(state, 0, "Battlefield")
                    if lname(o) == BEARS.lower()]
        infest_oid = find_hand(state, pid, INFEST)
        supper_oid = find_hand(state, pid, SUPPER)

        # Phase A: kill the Bears and set up the same-turn Supper cast.
        if (not ST["infest_cast"] and bears_bf and infest_oid and supper_oid
                and len(untapped_swamps) >= 5):
            for a in acts:
                if a["type"] == "CastSpell" \
                        and str(a.get("data", {}).get("object_id")) == infest_oid:
                    wire("action_submit", {"who": "P1", "action": "CastSpell/Infest",
                                          "object_id": infest_oid})
                    await c.send_action(a)
                    ST["infest_cast"] = True
                    say("[P1] casts Infest (Bears should die this turn)")
                    return True

        # Phase B: pre-export once Infest resolved and Bears are in P0's gy.
        if ST["infest_cast"] and not ST["pre_done"] and not stack(state):
            gy = zone_names(state, 0, "Graveyard")
            if BEARS in gy:
                pre = await do_export("pre.json")
                ST["pre_bears_gy"] = [n for n in zone_names(pre, 0, "Graveyard")
                                     if n == BEARS]
                ST["pre_done"] = True
                say(f"[P1] pre-exported; P0 gy Bears: {ST['pre_bears_gy']}")
                return True

        # Phase C: cast Supper for Spiders (instant) on the same turn.
        if ST["pre_done"] and not ST["supper_done"] and supper_oid:
            for a in acts:
                if a["type"] == "CastSpell" \
                        and str(a.get("data", {}).get("object_id")) == supper_oid:
                    wire("action_submit", {"who": "P1", "action": "CastSpell/Supper",
                                          "object_id": supper_oid})
                    await c.send_action(a)
                    ST["supper_cast"] = True
                    ST["supper_done"] = True
                    say("[P1] casts Supper for Spiders")
                    return True
            ST["supper_miss"] += 1
            wire("supper_not_offered", {"miss": ST["supper_miss"]})
            if ST["supper_miss"] >= 8:
                ST["supper_never_offered"] = True
                ST["supper_done"] = True
                say("[P1] Supper for Spiders CastSpell never offered "
                    f"({ST['supper_miss']} main-phase checks)")
                return True

    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            await c.send_action(a)
            return True
    if wtype == "Priority" and wplayer == 1:
        for a in acts:
            if a["type"] == "PassPriority":
                await c.send_action(a)
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

    last_progress = time.time()
    while time.time() - START < DEADLINE and not ST["stop"]:
        acted0 = await tick_p0(c0, 0)
        acted1 = await tick_p1(c1, 1)
        if acted0 or acted1:
            last_progress = time.time()
        # completion: Supper attempt concluded and game settled
        if ST["supper_done"]:
            s1, _, wf1 = cur(c1)
            if s1 is not None and wf1.get("type") in ("Priority", None) \
                    and not stack(s1):
                await asyncio.sleep(1)
                s1b, _, wf1b = cur(c1)
                if s1b is not None and not stack(s1b) \
                        and wf1b.get("type") in ("Priority", None):
                    say("[main] Supper attempt concluded, stack empty -> post export")
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

    # ---------------- card-data root-cause check ----------------
    cd = json.load(open(f"{BACKFILL}/server/releases/v0.99.0/data/card-data.json"))
    supper_entry = cd.get(SUPPER.lower())
    with open(f"{EVDIR}/card_data_supper.json", "w") as f:
        json.dump(supper_entry, f, indent=1)
    eff = ((supper_entry.get("abilities") or [{}])[0].get("effect") or {})
    sub = ((supper_entry.get("abilities") or [{}])[0].get("sub_ability") or {})
    sub_eff = (sub.get("effect") or {})
    card_unimplemented = (eff.get("type") == "Unimplemented"
                          and eff.get("name") == "unparsed_verb_arguments"
                          and sub_eff.get("type") == "Unimplemented"
                          and sub_eff.get("name") == "unbound_subject")

    # ---------------- assertions ----------------
    A = {}
    notes = {}
    pre = None
    if os.path.exists(f"{EVDIR}/pre.json"):
        pre = json.loads(open(f"{EVDIR}/pre.json").read())["state"]

    if pre is not None:
        pre_gy0 = zone_names(pre, 0, "Graveyard")
        pre_hand1 = zone_names(pre, 1, "Hand")
        A["A1_setup_ok"] = ("passed" if (BEARS in pre_gy0 and SUPPER in pre_hand1)
                            else "failed")
        notes["A1_setup_ok"] = (f"pre: P0 gy={pre_gy0}; "
                               f"SUPPER in P1 hand={SUPPER in pre_hand1}")
    else:
        A["A1_setup_ok"] = "failed"
        notes["A1_setup_ok"] = "pre.json was never captured (Infest did not resolve with Bears in P0 gy)"

    A["A2_cast_attempted"] = ("passed" if ST["supper_cast"] else "failed")
    notes["A2_cast_attempted"] = (
        f"CastSpell(Supper) submitted={ST['supper_cast']}; "
        f"never_offered={ST['supper_never_offered']}; "
        f"rejected={ST['supper_rejected'] is not None}"
        + (f" rejection={json.dumps(ST['supper_rejected'])[:200]}"
           if ST["supper_rejected"] else ""))

    if post is not None:
        post_bf1 = zone_objs(post, 1, "Battlefield")
        returned_bears = [oid for oid, o in post_bf1
                          if lname(o) == BEARS.lower()]
        food_perms = [oid for oid, o in post_bf1
                      if "food" in str(o.get("subtypes") or []).lower()
                      or "food" in str(o.get("type_line") or "").lower()]
        supper_zones = [(o.get("zone")) for oid, o in objs(post).items()
                        if lname(o) == SUPPER.lower() and o.get("controller") == 1]
        ST["supper_post_zone"] = supper_zones[0] if supper_zones else None
        post_gy0 = zone_names(post, 0, "Graveyard")
        A["A3_expected_return"] = "passed" if returned_bears else "failed"
        notes["A3_expected_return"] = (
            f"bears on BF under P1={len(returned_bears)}; "
            f"P0 gy still holds bears={BEARS in post_gy0}; "
            f"supper post zone={ST['supper_post_zone']}")
        A["A4_expected_food"] = "passed" if food_perms else "failed"
        notes["A4_expected_food"] = f"Food permanents under P1={len(food_perms)}"
        A["A6_cleanup"] = ("passed" if not stack(post) else "failed")
        notes["A6_cleanup"] = (f"post stack empty={not stack(post)}; "
                              f"phase={post.get('phase')}")
    else:
        for k in ("A3_expected_return", "A4_expected_food", "A6_cleanup"):
            A[k] = "not-run"
            notes[k] = "post.json unavailable"

    A["A5_card_unimplemented"] = "passed" if card_unimplemented else "failed"
    notes["A5_card_unimplemented"] = (
        "pinned card-data: spell effect Unimplemented/"
        f"{eff.get('name')}, child {sub_eff.get('name')}"
        if card_unimplemented else
        f"UNEXPECTED: effect parsed as {eff.get('type')}/{eff.get('name')}")

    if A["A1_setup_ok"] != "passed":
        verdict = "blocked"
    elif (A["A2_cast_attempted"] == "failed"
          or A["A3_expected_return"] == "failed"
          or A["A4_expected_food"] == "failed"):
        verdict = "reproduced"
    elif all(v == "passed" for v in A.values()):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    run = {
        "run_id": RUN_ID,
        "issue": 7364,
        "server": SERVER_IDENTITY,
        "server_hello": {"server_version": "0.99.0", "build_commit": "d919616",
                         "protocol_version": 98},
        "game_code": game_code,
        "ports": {"server": 9374},
        "verdict": verdict,
        "assertions": A,
        "scope": ("Supper for Spiders accepted/attempted-cast path: P0's "
                  "Grizzly Bears killed by Infest the same turn, then P1 "
                  "casts Supper for Spiders; native engine, two human-client "
                  "seats, protocol-98 driver."),
        "limitations": [
            "Browser UI not exercised; native engine via two human-driver seats.",
            "Dense test decks (engine accepts >4-of for custom games) are a test-harness convenience.",
            "The prebuilt server has no standalone state-restore; states are authoritative exports (restorable only via full game replay, scenario_7364.py).",
            "Single-turn scenario: only the this-turn graveyard path is exercised; multi-opponent and non-creature-card cases are not.",
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
