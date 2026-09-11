#!/usr/bin/env python3
"""Issue #6767: Duskana, the Rage Mother doesn't trigger the +3/+3.

Reported (Discord): "[[duskana the rage mother]] should give +3/+3 to base
p/t 2/2 but does not."

Oracle (pinned v0.79.0 card-data.json, key "duskana, the rage mother",
name field "Duskana, the Rage Mother", 5/5 for {2}{R}{G}{W}):
  "When Duskana enters, draw a card for each creature you control with base
   power and toughness 2/2.
   Whenever a creature you control with base power and toughness 2/2 attacks,
   it gets +3/+3 until end of turn."

Parser state in pinned data: the ETB trigger is parsed as ChangesZone/SelfRef
with a FIXED draw count of 1; the attack trigger's mode is
{"Unknown": "Whenever a creature you control with base power and toughness
2/2 attacks"} with a Pump +3/+3, target TriggeringSource, UntilEndOfTurn.
The issue is labeled classifier:unsupported-aspect.

Behavioral contract (single game, two human-client seats, v0.79.0/protocol 69):
  RAMP   - P0 plays lands, casts 2x Grizzly Bears, then Duskana.
  ATTACK - at P0's DeclareAttackers with Duskana + an attack-ready Bear on
           the battlefield: export pre.json, attack P1 with exactly one Bear.
  OBSERVE- export mid_combat.json once combat is past DeclareAttackers;
           export post.json once the stack is empty post-combat; stop.

  A1 setup_ok      pre.json: Duskana on P0 BF, >=1 Bear on P0 BF, 20/20 life.
  A2 attack_declared  Bear declared as attacker (wire event + combat state).
  A3 trigger_fires    a Duskana attack trigger appears on the stack / in
                      triggers_fired_this_turn between declare and mid-combat.
  A4 pump_correct      attacking Bear is 5/5 at mid_combat (until end of turn).
  A5 cleanup           stack empty at post.json; game proceeding.

Verdict = blocked iff A1 fails (setup never assembled).
Verdict = reproduced iff A1 passes and (A3 fails or A4 fails).
Verdict = not-reproduced iff A1..A5 all pass.

The ETB draw count is recorded as an observation note only (not scored):
oracle says draw = # of other 2/2s controlled; the parsed trigger draws a
fixed 1.
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260911-6767"
EVID_ISSUE = "6767"
EVDIR = f"{BACKFILL}/evidence/{EVID_ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")


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


DUSKANA = "Duskana, the Rage Mother"
BEAR = "grizzly bears"  # compared against base_name lowercased
LANDS = {"forest", "taiga", "savannah", "plateau"}

P0_DECK = deck((DUSKANA, 4), ("Grizzly Bears", 12),
               ("Taiga", 16), ("Savannah", 14), ("Plateau", 14))
P1_DECK = deck(("Forest", 60))

ST = {"stage": "RAMP", "stop": False, "attacked": False,
      "mid_exported": False, "etb_seen": False, "etb_note": None}
WF_SEEN = []


def oname(o):
    return o.get("card_name") or o.get("name") or ""


def lname(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def objs(state):
    return state.get("objects", {}) or {}


def hand_oids(state, pid):
    return [str(oid) for oid, o in objs(state).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def bf_creatures(state, pid, key=None):
    out = []
    for oid, o in objs(state).items():
        if o.get("zone") != "Battlefield" or o.get("controller") != pid:
            continue
        ct = (o.get("base_card_types") or {}).get("core_types") or []
        if "Creature" not in ct:
            continue
        if key and lname(o) != key:
            continue
        out.append((int(oid), o))
    return out


def can_attack_now(o):
    if o.get("tapped"):
        return False
    if o.get("summoning_sick") or o.get("has_summoning_sickness"):
        kws = o.get("keywords") or []
        flat = []
        for k in kws:
            flat.append(k if isinstance(k, str) else str(k))
        if not any("aste" in k for k in flat):  # Haste
            return False
    return True


def untapped_land_count(state, pid):
    return sum(
        1 for oid, o in objs(state).items()
        if o.get("zone") == "Battlefield" and o.get("controller") == pid
        and "Land" in ((o.get("base_card_types") or {}).get("core_types") or [])
        and not o.get("tapped"))


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain", "PostCombatMain"))


def life_of(state, pid):
    players = state.get("players", [])
    if isinstance(players, dict):
        pp = players.get(str(pid), players.get(pid))
        return (pp or {}).get("life") if isinstance(pp, dict) else None
    for p in players or []:
        if isinstance(p, dict) and p.get("id") == pid:
            return p.get("life")
    return None


def find_hand(state, pid, name):
    for oid in hand_oids(state, pid):
        if lname(objs(state)[oid]) == name.lower():
            return oid
    return None


def find_action(acts, atype):
    for a in acts or []:
        if a.get("type") == atype:
            return a
    return None


def stack_desc(state):
    out = []
    for sid in state.get("stack", []) or []:
        o = objs(state).get(str(sid), {})
        out.append({"id": sid, "name": oname(o),
                    "zone": o.get("zone"),
                    "kind": str(o.get("kind", ""))[:60]})
    return out


def record_wf(state):
    wf = (state.get("waiting_for") or {}).get("type")
    if wf and (not WF_SEEN or WF_SEEN[-1] != wf):
        WF_SEEN.append(wf)
        wire("waiting_for", {"type": wf})


def export_now(path, state_str):
    with open(f"{EVDIR}/{path}", "w") as f:
        f.write(state_str)
    say(f"exported {path}")


C0 = None


async def do_export(path):
    s = await C0.export_state()
    export_now(path, s)
    return json.loads(s)["state"]


# ------------------------------------------------------------- tick (P0)

async def tick_p0(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    wf = state.get("waiting_for") or {}
    wtype = wf.get("type")
    wplayer = (wf.get("data") or {}).get("player")

    # mulligan: always keep
    for a in acts:
        if a["type"] == "MulliganDecision":
            wire("action_submit", {"who": "P0", "action": "MulliganDecision/Keep"})
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            say("[P0] keeps")
            return True

    # discard to hand size: lands, then extra Duskanas, then bears
    if wtype == "DiscardToHandSize" and wplayer == 0:
        n = (wf.get("data") or {}).get("count") or max(0, len(hand_oids(state, pid)) - 7)
        oids = hand_oids(state, pid)
        def rank(oid):
            nm = lname(objs(state)[oid])
            if nm in LANDS:
                return 0
            if nm == DUSKANA.lower():
                return 1
            return 2
        picks = sorted(oids, key=rank)[:n]
        if picks:
            wire("action_submit", {"who": "P0", "action": "SelectCards/discard",
                                   "picks": picks})
            await c.send_action({"type": "SelectCards",
                                 "data": {"cards": [int(x) for x in picks]}})
            say(f"[P0] discards {len(picks)}")
            return True
        return False

    # declare attackers
    da = find_action(acts, "DeclareAttackers")
    if da and state.get("active_player") == 0:
        dusk = bf_creatures(state, 0, DUSKANA.lower())
        ready = [oid for oid, o in bf_creatures(state, 0, BEAR) if can_attack_now(o)]
        if (not ST["attacked"] and dusk and ready and ST["stage"] == "RAMP"):
            pre = await do_export("pre.json")
            wire("pre_export", {"duskana": [o for o, _ in dusk],
                                "bears": [oid for oid, _ in bf_creatures(pre, 0, BEAR)],
                                "life": [life_of(pre, 0), life_of(pre, 1)]})
            sub = copy.deepcopy(da)
            sub["data"]["attacks"] = [[ready[0], {"type": "Player", "data": 1}]]
            sub["data"]["bands"] = []
            ST["attacker_oid"] = ready[0]
            ST["attack_turn"] = state.get("turn_number")
            wire("action_submit", {"who": "P0", "action": "DeclareAttackers",
                                   "attacks": sub["data"]["attacks"]})
            await c.send_action({"type": "DeclareAttackers", "data": sub["data"]})
            ST["attacked"] = True
            ST["stage"] = "OBSERVE"
            say(f"[P0] attacks P1 with Bear oid {ready[0]} "
                f"(turn {ST['attack_turn']})")
            return True
        sub = copy.deepcopy(da)
        sub["data"]["attacks"] = []
        sub["data"]["bands"] = []
        wire("action_submit", {"who": "P0", "action": "DeclareAttackers/empty"})
        await c.send_action({"type": "DeclareAttackers", "data": sub["data"]})
        return True

    # declare blockers: none
    db = find_action(acts, "DeclareBlockers")
    if db:
        sub = copy.deepcopy(db)
        sub["data"]["assignments"] = []
        wire("action_submit", {"who": "P0", "action": "DeclareBlockers/empty"})
        await c.send_action({"type": "DeclareBlockers", "data": sub["data"]})
        return True

    # mana payments: submit as-is (engine auto-taps on protocol 69)
    for a in acts:
        if a["type"] in ("PayManaAbilityMana", "PayMana"):
            wire("action_submit", {"who": "P0", "action": a["type"]})
            await c.send_action(a)
            return True

    # never pass priority while a P0 cast decision is pending
    if wtype in ("OptionalCostChoice", "TargetSelection", "ManaPayment") \
            and wplayer == 0:
        wire("hold_priority", {"wtype": wtype})
        return False

    if is_my_main(state, pid):
        # land drop (retry every tick; no kept-flags)
        hid = find_hand(state, pid, "forest") or find_hand(state, pid, "taiga") \
            or find_hand(state, pid, "savannah") or find_hand(state, pid, "plateau")
        for a in acts:
            if a["type"] == "PlayLand" and hid \
                    and str(a.get("data", {}).get("object_id")) == hid:
                wire("action_submit", {"who": "P0", "action": "PlayLand",
                                       "land": lname(objs(state)[hid])})
                await c.send_action(a)
                say(f"[P0] plays {lname(objs(state)[hid])}")
                return True
        # cast Duskana (needs 2RGW; attempt when 5+ untapped lands)
        duskana_bf = bf_creatures(state, 0, DUSKANA.lower())
        did = find_hand(state, pid, DUSKANA)
        if not duskana_bf and did and untapped_land_count(state, pid) >= 5:
            for a in acts:
                if a["type"] == "CastSpell" \
                        and str(a.get("data", {}).get("object_id")) == did:
                    wire("action_submit", {"who": "P0", "action": "CastSpell/Duskana"})
                    await c.send_action(a)
                    say("[P0] casts Duskana, the Rage Mother")
                    return True
        # cast bears until 2 on the battlefield
        bears = bf_creatures(state, 0, BEAR)
        bid = find_hand(state, pid, "Grizzly Bears")
        if len(bears) < 2 and bid and untapped_land_count(state, pid) >= 2:
            for a in acts:
                if a["type"] == "CastSpell" \
                        and str(a.get("data", {}).get("object_id")) == bid:
                    wire("action_submit", {"who": "P0", "action": "CastSpell/Bear"})
                    await c.send_action(a)
                    say("[P0] casts Grizzly Bears")
                    return True

    for a in acts:
        if a["type"] == "PassPriority":
            wire("action_submit", {"who": "P0", "action": "PassPriority"})
            await c.send_action(a)
            return True
    return False


# ------------------------------------------------------------- tick (P1)

async def tick_p1(c, pid):
    st = c.latest
    if not st:
        return False
    state, acts = st.get("state"), st.get("legal_actions", [])
    wf = state.get("waiting_for") or {}
    wtype = wf.get("type")
    wplayer = (wf.get("data") or {}).get("player")

    for a in acts:
        if a["type"] == "MulliganDecision":
            await c.send_action({"type": "MulliganDecision",
                                 "data": {"choice": {"type": "Keep"}}})
            return True
    if wtype == "DiscardToHandSize" and wplayer == 1:
        n = (wf.get("data") or {}).get("count") or max(0, len(hand_oids(state, pid)) - 7)
        picks = hand_oids(state, pid)[:n]
        if picks:
            await c.send_action({"type": "SelectCards",
                                 "data": {"cards": [int(x) for x in picks]}})
            return True
        return False
    da = find_action(acts, "DeclareAttackers")
    if da:
        sub = copy.deepcopy(da)
        sub["data"]["attacks"] = []
        sub["data"]["bands"] = []
        await c.send_action({"type": "DeclareAttackers", "data": sub["data"]})
        return True
    db = find_action(acts, "DeclareBlockers")
    if db:
        sub = copy.deepcopy(db)
        sub["data"]["assignments"] = []
        await c.send_action({"type": "DeclareBlockers", "data": sub["data"]})
        return True
    if is_my_main(state, pid):
        hid = find_hand(state, pid, "forest")
        for a in acts:
            if a["type"] == "PlayLand" and hid \
                    and str(a.get("data", {}).get("object_id")) == hid:
                await c.send_action(a)
                return True
    for a in acts:
        if a["type"] == "PayManaAbilityMana" or a["type"] == "PayMana":
            await c.send_action(a)
            return True
    for a in acts:
        if a["type"] == "PassPriority":
            await c.send_action(a)
            return True
    return False


# ------------------------------------------------------------------ main

async def main():
    t0 = time.time()
    obs = {"assert": {}, "notes": []}
    A = obs["assert"]

    p0 = PhaseClient("P0")
    await p0.connect()
    say("P0 creating game...")
    try:
        await p0.create(P0_DECK)
    except Exception as e:
        say(f"game creation failed: {e}")
        A["A1_setup_ok"] = "blocked"
        obs["notes"].append(f"deck/game creation failed: {e}")
        with open(f"{EVDIR}/assertions.json", "w") as f:
            json.dump({"assertions": A, "notes": obs["notes"],
                       "wf_sequence": WF_SEEN}, f, indent=2)
        await p0.close()
        return obs
    p1 = PhaseClient("P1")
    await p1.connect()
    say("P1 joining...")
    await p1.join(p0.game_code, P1_DECK)
    say(f"game {p0.game_code}; seats {p0.player_id}/{p1.player_id}")
    wire("game", {"code": p0.game_code, "p0": p0.player_id, "p1": p1.player_id})

    global C0
    C0 = p0

    last_rev = {}
    TIMEOUT = 1500
    last_stack_sig = None
    while time.time() - t0 < TIMEOUT and not ST["stop"]:
        await asyncio.sleep(0.25)
        for c, pid, tick in ((p0, p0.player_id, tick_p0),
                             (p1, p1.player_id, tick_p1)):
            if c.revision == last_rev.get(c.name):
                continue
            try:
                if await tick(c, pid):
                    last_rev[c.name] = c.revision
            except Exception as e:
                say(f"tick error {c.name}: {e}")
        st = p0.latest
        if not st:
            continue
        record_wf(st["state"])
        state = st["state"]

        # ETB observation: Duskana entering the battlefield
        if not ST["etb_seen"] and bf_creatures(state, 0, DUSKANA.lower()):
            ST["etb_seen"] = True
            bears = len(bf_creatures(state, 0, BEAR))
            drawn = (state.get("cards_drawn_this_turn") or {})
            p0drawn = drawn.get("0", drawn.get(0, "?")) if isinstance(drawn, dict) else drawn
            hand = len(hand_oids(state, 0))
            ST["etb_note"] = (f"Duskana ETB with {bears} other 2/2 Bears on BF; "
                              f"cards_drawn_this_turn(P0)={p0drawn}; P0 hand={hand} "
                              f"(oracle: draw {bears}; parsed trigger: fixed 1)")
            say("[note]", ST["etb_note"])
            wire("duskana_etb", {"bears_on_bf": bears, "p0_drawn": p0drawn,
                                 "p0_hand": hand})

        # stack watch during OBSERVE: log any trigger-shaped entries
        if ST["stage"] == "OBSERVE":
            sd = stack_desc(state)
            sig = json.dumps(sd, sort_keys=True, default=str)
            if sig != last_stack_sig:
                last_stack_sig = sig
                wire("stack", {"entries": sd,
                               "triggers_fired": state.get("triggers_fired_this_turn")})
                if sd:
                    say(f"[stack] {json.dumps(sd)[:300]}")

        # mid-combat export: first tick past DeclareAttackers in OBSERVE
        if ST["stage"] == "OBSERVE" and not ST["mid_exported"] \
                and (state.get("phase") or "") in (
                    "DeclareBlockers", "CombatDamage", "EndOfCombat",
                    "PostCombatMain"):
            mid = await do_export("mid_combat.json")
            ST["mid_exported"] = True
            atk = ST.get("attacker_oid")
            o = (mid.get("objects", {}) or {}).get(str(atk), {})
            wire("mid_combat", {"attacker_pt": (o.get("power"), o.get("toughness")),
                                "phase": mid.get("phase"),
                                "stack": stack_desc(mid),
                                "triggers_fired": mid.get("triggers_fired_this_turn")})
            say(f"[mid] attacker {atk} P/T={o.get('power')}/{o.get('toughness')} "
                f"phase={mid.get('phase')}")

        # post export: stack empty and (post-combat main or a later turn)
        if ST["mid_exported"]:
            stack_empty = not (state.get("stack") or [])
            post_phase = (state.get("phase") or "") in ("PostCombatMain", "EndStep",
                                                         "Cleanup")
            later_turn = (state.get("turn_number") or 0) > (ST.get("attack_turn") or 0)
            if stack_empty and (post_phase or later_turn):
                await asyncio.sleep(1.0)
                post = await do_export("post.json")
                atk = ST.get("attacker_oid")
                o = (post.get("objects", {}) or {}).get(str(atk), {})
                wire("post", {"attacker_pt": (o.get("power"), o.get("toughness")),
                              "phase": post.get("phase"),
                              "turn": post.get("turn_number"),
                              "triggers_fired": post.get("triggers_fired_this_turn")})
                say(f"[post] phase={post.get('phase')} turn={post.get('turn_number')} "
                    f"attacker P/T={o.get('power')}/{o.get('toughness')}")
                ST["stop"] = True

    # ------------------------------------------------------- evaluate
    def load_env(p):
        with open(f"{EVDIR}/{p}") as f:
            return json.load(f)

    def env_state(p):
        try:
            return load_env(p)["state"]
        except FileNotFoundError:
            return None

    pre = env_state("pre.json")
    mid = env_state("mid_combat.json")
    post = env_state("post.json")

    def pt(s, oid):
        o = (s.get("objects", {}) or {}).get(str(oid), {})
        return o.get("power"), o.get("toughness")

    def triggers_mention_duskana(s):
        hits = []
        for entry in s.get("triggers_fired_this_turn") or []:
            blob = json.dumps(entry, default=str)
            if "uskana" in blob or "ttack" in blob:
                hits.append(blob[:200])
        return hits

    # A1
    if pre is None:
        A["A1_setup_ok"] = "failed"
        obs["notes"].append("pre.json was never exported (attack never declared)")
    else:
        dusk = [o for o in (pre.get("objects", {}) or {}).values()
                if o.get("zone") == "Battlefield" and o.get("controller") == 0
                and lname(o) == DUSKANA.lower()]
        bears = [o for o in (pre.get("objects", {}) or {}).values()
                 if o.get("zone") == "Battlefield" and o.get("controller") == 0
                 and lname(o) == BEAR]
        ok = (len(dusk) >= 1 and len(bears) >= 1
              and life_of(pre, 0) == 20 and life_of(pre, 1) == 20)
        A["A1_setup_ok"] = "passed" if ok else "failed"
        obs["notes"].append(f"pre: duskana_bf={len(dusk)} bears_bf={len(bears)} "
                            f"life={[life_of(pre, 0), life_of(pre, 1)]}")

    # A2
    atk_declared = False
    for line in open(f"{EVDIR}/wire_log.jsonl"):
        d = json.loads(line)
        if d["event"] == "action_submit" and \
                d["payload"].get("action") == "DeclareAttackers" and \
                d["payload"].get("attacks"):
            atk_declared = True
    A["A2_attack_declared"] = "passed" if atk_declared else "failed"

    # A3
    trig_hits = []
    if mid is not None:
        trig_hits += triggers_mention_duskana(mid)
    if post is not None:
        for h in triggers_mention_duskana(post):
            if h not in trig_hits:
                trig_hits.append(h)
    # also scan wire stack snapshots for a Duskana trigger object
    for line in open(f"{EVDIR}/wire_log.jsonl"):
        d = json.loads(line)
        if d["event"] == "stack":
            for e in d["payload"].get("entries", []) or []:
                if "uskana" in str(e.get("name", "")):
                    trig_hits.append("stack:" + str(e.get("name")))
    A["A3_trigger_fires"] = "passed" if trig_hits else "failed"
    obs["notes"].append(f"trigger evidence hits: {trig_hits[:3] or 'none'}")

    # A4
    atk = ST.get("attacker_oid")
    if mid is not None and atk is not None:
        p, t = pt(mid, atk)
        A["A4_pump_correct"] = "passed" if (p == 5 and t == 5) else "failed"
        obs["notes"].append(f"mid_combat attacker {atk} P/T={p}/{t} "
                            f"(expected 5/5)")
        # control: non-attacking bear must stay 2/2
        others = [(oid, o) for oid, o in (mid.get("objects", {}) or {}).items()
                  if o.get("zone") == "Battlefield" and o.get("controller") == 0
                  and lname(o) == BEAR and str(oid) != str(atk)]
        obs["notes"].append("non-attacking bears mid_combat: " +
                            str([(o.get("power"), o.get("toughness")) for _, o in others]))
    else:
        A["A4_pump_correct"] = "not-run"
        obs["notes"].append("mid_combat.json missing; A4 not-run")

    # A5
    if post is not None:
        empty = not (post.get("stack") or [])
        A["A5_cleanup"] = "passed" if empty else "failed"
        obs["notes"].append(f"post: phase={post.get('phase')} "
                            f"turn={post.get('turn_number')} "
                            f"stack_empty={empty}")
    else:
        A["A5_cleanup"] = "not-run"
        obs["notes"].append("post.json missing; A5 not-run")

    if ST.get("etb_note"):
        obs["notes"].append("ETB observation: " + ST["etb_note"])

    for k in sorted(A):
        say(f"{k}: {A[k]}")
    say("WF sequence:", WF_SEEN)

    with open(f"{EVDIR}/assertions.json", "w") as f:
        json.dump({"assertions": A, "notes": obs["notes"],
                   "wf_sequence": WF_SEEN}, f, indent=2)

    # run.json
    scenario_src = open(__file__, "rb").read()
    run_meta = {
        "run_id": RUN_ID,
        "issue": 6767,
        "date": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "server": {
            "version": "v0.79.0",
            "build_commit": "1cde7a2",
            "protocol_version": 69,
            "binary_sha256": "46d89146bf3e051cf22591bd195b7d15d4806a8f6d226bb8792dbcfe479fef94",
            "card_data_sha256": "75cbfe139b220b8267c4d99b1d478b59d7298a86dfc3def83fbb31eaa970b5b3",
        },
        "scenario_sha256": hashlib.sha256(scenario_src).hexdigest(),
        "decks": {
            "P0": {"Duskana, the Rage Mother": 4, "Grizzly Bears": 12,
                   "Taiga": 16, "Savannah": 14, "Plateau": 14},
            "P1": {"Forest": 60},
        },
        "verdict": ("blocked" if A.get("A1_setup_ok") in ("failed", "blocked")
                    else "reproduced" if A.get("A3_trigger_fires") == "failed"
                    or A.get("A4_pump_correct") == "failed"
                    else "not-reproduced"
                    if all(v == "passed" for v in A.values())
                    else "blocked"),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run_meta, f, indent=2)
    say("verdict:", run_meta["verdict"])
    # copy scenario source into evidence
    with open(f"{EVDIR}/scenario_6767.py", "w") as f:
        f.write(scenario_src.decode())

    await p0.close()
    await p1.close()
    return obs


if __name__ == "__main__":
    obs = asyncio.run(main())
    print(json.dumps(obs["assert"], indent=2))
