#!/usr/bin/env python3
"""Issue #7417: Conqueror's Galleon -- "then return it to the battlefield
transformed" is hoisted out of the end-of-combat delayed trigger it depends on.

Reported (2026-08-15, lgray, source:internal-triage, status:confirmed,
area:parser, classifier:supported-aspect-defect, priority:p2-wrong-game-result):
Conqueror's Galleon's "then return it to the battlefield transformed under
your control" is parsed as a SIBLING of the CreateDelayedTrigger, not as part
of the delayed ability's body. The exile it depends on happens at end of
combat INSIDE the delayed trigger, so at the moment the return sub-ability
resolves nothing has been exiled and its TrackedSet binds nothing. The
Galleon never flips to Conqueror's Foothold. No runtime reproduction was run
for this card (the report is data-level).

Oracle text (verified in pinned v0.99.0 card-data.json):
  Conqueror's Galleon {4} 2/10 Artifact -- Vehicle
  "When this Vehicle attacks, exile it at end of combat, then return it to
  the battlefield transformed under your control.
  Crew 4 (Tap any number of creatures you control with total power 4 or
  more: This Vehicle becomes an artifact creature until end of turn.)"

Pinned v0.99.0 parse of the attack trigger (see data_evidence.json):
  head CreateDelayedTrigger {condition: AtNextPhase EndCombat,
        effect: ChangeZone(Exile, target ParentTarget), uses_tracked_set: false}
  sibling ChangeZone {destination: Battlefield, target: TrackedSet id 0,
        enter_transformed: true, enters_under: You}
  -- exactly the shape the issue reports.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Setup (native engine, two human-client seats, default Bo1, life 20):
  P0: 4x Conqueror's Galleon + 12x Grizzly Bears + 44x Forest
  P1: 60x Plains (plays lands, passes)
Drive:
  1. Mulligans: P0 keeps hands with >=3 lands (mulligans at most twice);
     P1 keeps.
  2. P0 plays a land each turn; casts Bears when castable; casts the Galleon
     when 4 untapped lands are available.
  3. On a P0 PreCombatMain strictly later than the turn the Galleon entered:
       a. export pre (Galleon + 2 untapped Bears on P0's BF, life 20/20)
       b. crew the Galleon (tap the two Bears; total power 4)
       c. export crewed (Galleon is now a creature)
  4. DeclareAttackers: attack P1 with the crewed Galleon.
       - the (non-optional) attack trigger goes on the stack; export attack
  5. Both seats pass; the trigger resolves: the head registers the delayed
     trigger and the misplaced return sibling resolves immediately (nothing
     has been exiled yet). Export post_trigger with the Galleon still on the
     battlefield and the stack empty -- the observable "nothing happened".
  6. P1 declares no blockers; combat damage; end of combat. The delayed
     trigger fires and resolves, exiling the Galleon. Export end_combat /
     post with the Galleon in exile.

Expected (correct behavior): at the delayed trigger's resolution the Galleon
returns to the battlefield transformed as Conqueror's Foothold.
Reported (bug): the return clause already resolved (as a no-op) at attack
time, so the Galleon is exiled at end of combat and never comes back --
Conqueror's Foothold never enters.

Assertions (correct-behavior expectations; a FAIL records the bug):
  A1_setup_ok       pre: Galleon + >=2 untapped Bears on P0's BF, life 20/20;
                    Galleon is a creature after crewing.
  A2_attack_declared Galleon declared as attacker against P1 (combat.attackers
                    carries the Galleon oid in the attack state).
  A3_trigger_resolved post_trigger: Galleon still on P0's BF, stack empty
                    (the misplaced sibling return changed nothing).
  A4_exiled_at_end_combat post: the Galleon object is in Exile.
  A5_never_transformed (THE REPORTED BUG) post: Conqueror's Foothold is NOT on
                    P0's battlefield. Absence fails -- the Galleon never
                    flips; presence (correct behavior) passes.
  A6_cleanup        post stack empty, game advancing.

Verdict rule: reproduced iff A1..A4 passed and A5 failed;
              not-reproduced iff A5 passed (Foothold on the battlefield);
              blocked iff A1/A2 could not be established (setup never
              assembled) or the attack trigger never resolved (A3/A4
              not established).

Protocol-98 driver notes (v0.99.0, verified 2026-10-01/02):
  - HELLO advertises protocol 98 (server enforces exact match).
  - MulliganDecision as {"choice":{"type":"Keep"}} gated on
    waiting_for.data.pending[] with phase type "Declare".
  - BottomCards via single SelectCards {"cards":[...]}.
  - CastSpell / PlayLand / ActivateAbility via advertised actions
    (ActivateAbility matched on data.source_id); PayMana* as advertised.
  - DeclareAttackers data.attacks = [[oid, {"type":"Player","data":<seat>}]].
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
RUN_ID = "20261002-7417d"
ISSUE = 7417
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

for _f, _k in (("server/releases/v0.99.0/phase-server-slim-x86_64-unknown-linux-musl", "server_binary_sha256"),
               ("server/releases/v0.99.0/data/card-data.json", "card_data_sha256"),
               ("server/releases/v0.99.0/data/draft-pools.json", "draft_pools_sha256")):
    _h = hashlib.sha256(open(f"{BACKFILL}/{_f}", "rb").read()).hexdigest()
    assert _h == SERVER_IDENTITY[_k], f"hash mismatch for {_f}: {_h}"
print("server identity hashes verified against on-disk pinned artifacts", flush=True)

CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.99.0/data/card-data.json"))

GAL = "conqueror's galleon"
FOOTHOLD = "conqueror's foothold"
BEAR = "grizzly bears"
FOR = "forest"

P0_DECK = [("Conqueror's Galleon", 4), ("Grizzly Bears", 12), ("Forest", 44)]
P1_DECK = [("Plains", 60)]

SETUP_DEADLINE_S = 1700

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
        "phase": "setup",  # setup -> crewed -> attacked -> trigger_wait
                           #        -> post_trigger -> end_combat_wait -> done
        "galleon_bf_oid": None,
        "galleon_entered_turn": None,
        "crewed": False,
        "crew_submitted": False,
        "crew_watchdog_at": None,
        "attack_submitted": False,
        "attack_turn": None,
        "trigger_seen": False,
        "trigger_seen_at": None,
        "trigger_exported": False,
        "post_trigger_exported": False,
        "end_combat_exported": False,
        "post_exported": False,
        "terminal": False,
        "terminal_data": None,
        "states_seen": 0,
        "pre_life": None,
        "cast_rejections": 0,
        "ass": {k: "not-run" for k in ("A1_setup_ok", "A2_attack_declared",
                                       "A3_trigger_resolved",
                                       "A4_exiled_at_end_combat",
                                       "A5_never_transformed",
                                       "A6_cleanup")},
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
        if o.get("zone") != zone:
            continue
        if pid is not None and o.get("controller") != pid:
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


def is_creature(state, oid):
    o = get_obj(state, oid)
    return "creature" in [str(t).lower()
                          for t in (o.get("card_types") or {}).get("core_types", [])]


def power_of(state, oid):
    o = get_obj(state, oid)
    p = o.get("power")
    if isinstance(p, dict):
        return int(p.get("value", 0))
    try:
        return int(p)
    except (TypeError, ValueError):
        return 0


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


def life_of(state, pid):
    return player_of(state, pid).get("life")


def attackers_of(state):
    return state.get("combat", {}).get("attackers", []) if isinstance(
        state.get("combat"), dict) else []

# ------------------------------------------------------------- data check
def check_data_level():
    gal = CARD_DATA.get("conqueror's galleon", {})
    trig = (gal.get("triggers") or [{}])[0]
    exe = trig.get("execute") or {}
    sub = exe.get("sub_ability") or {}
    findings = {
        "name": gal.get("name"),
        "mana_cost": gal.get("mana_cost"),
        "oracle": gal.get("oracle_text"),
        "attack_trigger_execute": exe,
        "attack_trigger_sibling_sub_ability": sub,
    }
    with open(f"{EVDIR}/data_evidence.json", "w") as f:
        json.dump(findings, f, indent=1, default=str)
    wire("data_level", findings)
    head = (exe.get("effect") or {}).get("type")
    head_cond = ((exe.get("effect") or {}).get("condition") or {}).get("type")
    head_exile = ((((exe.get("effect") or {}).get("effect") or {}).get("effect")
                    or {}).get("type"))
    subeff = (sub.get("effect") or {})
    ok = (head == "CreateDelayedTrigger"
          and head_cond == "AtNextPhase"
          and head_exile == "ChangeZone"
          and subeff.get("type") == "ChangeZone"
          and subeff.get("destination") == "Battlefield"
          and subeff.get("enter_transformed") is True
          and (subeff.get("target") or {}).get("type") == "TrackedSet")
    say(f"data-level check: head={head} (cond={head_cond}, inner={head_exile}), "
        f"sibling={subeff.get('type')}->"
        f"{subeff.get('destination')} transformed={subeff.get('enter_transformed')} "
        f"target={(subeff.get('target') or {}).get('type')} -> "
        f"{'MATCHES ISSUE REPORT' if ok else 'MISMATCH'}")
    ST["notes"].append(
        "data-level: pinned v0.99.0 card-data.json parses Conqueror's "
        "Galleon's attack trigger as CreateDelayedTrigger (exile at end of "
        "combat) head + a sibling ChangeZone(Battlefield, TrackedSet 0, "
        f"enter_transformed=true) (issue-reported shape: {ok})")
    return ok

# ------------------------------------------------------------- interaction
async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


def mulligan_keep_p0(hand):
    lands = sum(1 for n in hand if n == FOR)
    return lands >= 3


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
                             "data": {"choice": {"type": "Keep" if False else "Mulligan"}}})
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
    rank = {GAL: 5, BEAR: 4, FOR: 3}
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
    rank = {GAL: 5, BEAR: 4, FOR: 3}
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


def untapped_bears(state):
    return [int(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == 0
            and not o.get("tapped")
            and str(o.get("base_name") or o.get("name") or "").lower() == BEAR]


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
    if ST.get("casting"):
        if await pay_tick(c, acts, "P0"):
            return
    if await do_discard_to_handsize(c, 0, "P0"):
        return

    # crew interaction: if the crew activation opened a choice for P0,
    # answer it from the advertised candidates (prefer untapped bears).
    if ST["phase"] == "crewing" and wtype not in ("Priority",):
        pend = pending_for(state, 0)
        data = (wf_of(state).get("data") or {})
        say(f"[P0] crewing interaction: wtype={wtype} pending={json.dumps(pend, default=str)[:500]}")
        wire("crew_interaction", {"wtype": wtype, "data": data})
        done = await answer_crew_choice(c, state, acts)
        if done:
            return

    if wtype == "DeclareAttackers":
        if ST["attack_submitted"]:
            return
        if (ST["phase"] == "crewed" and ST["crewed"]):
            da = next((a for a in acts if a["type"] == "DeclareAttackers"), None)
            if da:
                import copy as _copy
                d = _copy.deepcopy(da)
                d.setdefault("data", {}).update(
                    {"attacks": [[ST["galleon_bf_oid"],
                                  {"type": "Player", "data": 1}]],
                     "bands": []})
                ST["attack_submitted"] = True
                ST["attack_turn"] = state.get("turn_number")
                ST["phase"] = "attacked"
                say(f"[P0] attacking P1 with Conqueror's Galleon "
                    f"(oid {ST['galleon_bf_oid']})")
                wire("attack", {"galleon_oid": ST["galleon_bf_oid"],
                                "submission": d})
                await submit_as_is(c, d)
                return
        # not our combat or already attacked: declare empty
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
        g_bf = zone_ids(state, 0, "Battlefield", GAL)
        bears = untapped_bears(state)
        # crew on a later turn than the Galleon's entry (summoning sickness)
        if (not ST["crew_submitted"] and ST["phase"] == "setup"
                and g_bf and len(bears) >= 2
                and ST["galleon_entered_turn"] is not None
                and state.get("turn_number") != ST["galleon_entered_turn"]):
            goid = g_bf[0]
            cands = [a for a in acts
                     if a["type"] in ("CrewVehicle", "ActivateAbility")
                     and (int((a.get("data") or {}).get("source_id", -1))
                          == goid
                          or int((a.get("data") or {}).get("vehicle_id", -1))
                          == goid
                          or int((a.get("data") or {}).get("object_id", -1))
                          == goid
                          or int(a.get("_src_oid") or -1) == goid
                          or a["type"] == "CrewVehicle")]
            wire("crew_candidates",
                 {"galleon_oid": goid,
                  "candidates": [{"type": a["type"], "data": a.get("data"),
                                  "_src_oid": a.get("_src_oid")}
                                 for a in cands],
                  "all_types": sorted({a["type"] for a in acts})})
            if cands:
                await export_as(c, "pre")
                ST["crew_submitted"] = True
                ST["phase"] = "crewing"
                ST["crew_watchdog_at"] = time.time()
                say(f"[P0] activating crew on Conqueror's Galleon "
                    f"(oid {goid})")
                await submit_as_is(c, cands[0])
                return
        # casting: Galleon first, then bears
        gal_in_hand = any(n == GAL for n in hand_lnames(state, 0))
        if (not g_bf and gal_in_hand
                and len(untapped_lands(state, 0)) >= 4):
            a, oid = cast_action_for(acts, state, GAL)
            if a:
                ST["casting"] = True
                ST["cast_was"] = "galleon"
                say(f"[P0] casting Conqueror's Galleon (oid {oid})")
                wire("cast_galleon", {"oid": oid})
                await submit_as_is(c, a)
                return
        n_bears = len(zone_ids(state, 0, "Battlefield", BEAR))
        if (BEAR in hand_lnames(state, 0) and n_bears < 3
                and len(untapped_lands(state, 0)) >= 2):
            a, oid = cast_action_for(acts, state, BEAR)
            if a:
                ST["casting"] = True
                ST["cast_was"] = "bear"
                ST["bears_at_cast"] = n_bears
                say(f"[P0] casting Grizzly Bears (oid {oid})")
                await submit_as_is(c, a)
                return
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(c, a)
                return
    await pass_priority(c, st, acts)


async def answer_crew_choice(c, state, acts):
    """Answer the crew cost/selection interaction.

    The engine's crew flow: submitting CrewVehicle as-is opens a CrewVehicle
    waiting state for P0; the decisive submission is the CrewVehicle action
    itself with data.creature_ids filled (untapped creatures, total power
    >= 4). Returns True if a submission was made.
    """
    wf = wf_of(state)
    wtype = wf.get("type") or ""
    bears = untapped_bears(state)
    chosen, total = [], 0
    for b in bears:
        if total < 4:
            chosen.append(b)
            total += power_of(state, b)
    if not chosen or total < 4:
        say(f"[P0] crew: cannot reach power 4 (bears={bears})")
        wire("crew_choice_no_bears", {"bears": bears})
        return False
    if wtype == "CrewVehicle":
        cv = next((a for a in acts if a["type"] == "CrewVehicle"), None)
        if cv:
            import copy as _copy
            d = _copy.deepcopy(cv)
            d.setdefault("data", {})["creature_ids"] = chosen
            say(f"[P0] crew: tapping "
                f"{[obj_lname(state, o) for o in chosen]} "
                f"(total power {total})")
            wire("crew_choice", {"chosen": chosen, "total_power": total,
                                 "submission": d})
            await submit_as_is(c, d)
            return True
    # unknown crew surface: log and do not guess
    data = wf.get("data") or {}
    say(f"[P0] crew: unrecognized waiting surface wtype={wtype}")
    wire("crew_unknown_surface", {"wtype": wtype, "data": data,
                                  "act_types": sorted({a["type"]
                                                       for a in acts})})
    return False


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
    crewed = load_env("crewed") or {}
    attack = load_env("attack") or {}
    post_trigger = load_env("post_trigger") or {}
    post = load_env("post") or {}
    pre_st = pre.get("state") or {}
    crewed_st = crewed.get("state") or {}
    attack_st = attack.get("state") or {}
    post_trigger_st = post_trigger.get("state") or {}
    post_st = post.get("state") or {}

    # A1: setup assembled (Galleon + 2 untapped bears, life 20/20; crewed)
    if pre_st:
        g_bf = zone_ids(pre_st, 0, "Battlefield", GAL)
        bears = [int(oid) for oid, o in (pre_st.get("objects") or {}).items()
                 if o.get("zone") == "Battlefield" and o.get("controller") == 0
                 and not o.get("tapped")
                 and str(o.get("base_name") or "").lower() == BEAR]
        life_ok = (life_of(pre_st, 0), life_of(pre_st, 1)) == (20, 20)
        crewed_ok = (is_creature(crewed_st, ST["galleon_bf_oid"])
                     if crewed_st and ST["galleon_bf_oid"] else False)
        notes.append(
            f"A1 probe: pre galleon_bf={g_bf}, untapped_bears={len(bears)}, "
            f"life={(life_of(pre_st, 0), life_of(pre_st, 1))}, "
            f"crewed_is_creature={crewed_ok}.")
        if g_bf and len(bears) >= 2 and life_ok and crewed_ok:
            ass["A1_setup_ok"] = "passed"
            notes.append("A1 passed: pre assembled (Galleon + >=2 untapped "
                         "Bears, life 20/20) and the crew activation made "
                         "the Galleon a creature.")
        else:
            ass["A1_setup_ok"] = "failed"
            notes.append("A1 FAILED: setup never fully assembled (see probe).")
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append("A1 FAILED: pre state never exported.")

    # A2: the Galleon was declared as an attacker
    if attack_st:
        atk = attackers_of(attack_st)
        ser = json.dumps(atk, default=str)
        hit = (str(ST["galleon_bf_oid"]) in ser) or (GAL in ser.lower())
        notes.append(f"A2 probe: combat.attackers={ser[:300]}")
        if hit:
            ass["A2_attack_declared"] = "passed"
            notes.append("A2 passed: Conqueror's Galleon declared as an "
                         "attacker.")
        else:
            ass["A2_attack_declared"] = "failed"
            notes.append("A2 FAILED: Galleon not among declared attackers.")
    else:
        notes.append("A2 not-run: attack state never exported.")

    # A3: the attack trigger resolved with the Galleon still on the BF
    if post_trigger_st:
        g_bf = zone_ids(post_trigger_st, 0, "Battlefield", GAL)
        stack_empty = not stack_entries(post_trigger_st)
        notes.append(
            f"A3 probe: post_trigger galleon_bf={g_bf}, "
            f"stack_empty={stack_empty}.")
        if g_bf and stack_empty:
            ass["A3_trigger_resolved"] = "passed"
            notes.append("A3 passed: attack trigger resolved; Galleon still "
                         "on the battlefield, stack empty -- the misplaced "
                         "return sibling changed nothing observable.")
        else:
            ass["A3_trigger_resolved"] = "failed"
            notes.append("A3 FAILED: unexpected post-trigger state.")
    else:
        notes.append("A3 not-run: post_trigger never exported.")

    # A4: the Galleon was exiled at end of combat
    if post_st:
        g_ex = zone_ids(post_st, None, "Exile", GAL)
        notes.append(f"A4 probe: post exile galleons={g_ex}.")
        if g_ex:
            ass["A4_exiled_at_end_combat"] = "passed"
            notes.append("A4 passed: the Galleon is in Exile after the end-"
                         "of-combat delayed trigger resolved.")
        else:
            ass["A4_exiled_at_end_combat"] = "failed"
            notes.append("A4 FAILED: Galleon not in exile in the post state.")
    else:
        notes.append("A4 not-run: no post state.")

    # A5: THE REPORTED BUG -- Conqueror's Foothold never enters
    if post_st:
        foothold = zone_ids(post_st, 0, "Battlefield", FOOTHOLD)
        g_bf = zone_ids(post_st, 0, "Battlefield", GAL)
        notes.append(f"A5 probe: post foothold_on_bf={foothold}, "
                     f"galleon_on_bf={g_bf}.")
        if foothold:
            ass["A5_never_transformed"] = "passed"
            notes.append("A5 passed: Conqueror's Foothold is on P0's "
                         "battlefield -- the Galleon returned transformed "
                         "at end of combat. This is not a fix claim.")
        else:
            ass["A5_never_transformed"] = "failed"
            notes.append("A5 FAILED: no Conqueror's Foothold on P0's "
                         "battlefield after the end-of-combat exile -- THE "
                         "REPORTED BUG REPRODUCES (the return clause resolved "
                         "as a no-op sibling before the exile existed).")
    else:
        notes.append("A5 not-run: no post state.")

    # A6: cleanup
    if post_st:
        if not stack_entries(post_st):
            ass["A6_cleanup"] = "passed"
            notes.append("A6 passed: post stack empty.")
        else:
            ass["A6_cleanup"] = "failed"
            notes.append("A6 FAILED: post stack non-empty: "
                         f"{stack_entries(post_st)[:2]}")
    else:
        notes.append("A6 not-run: no post state.")

    if (ass["A1_setup_ok"] == "passed"
            and ass["A2_attack_declared"] == "passed"
            and ass["A3_trigger_resolved"] == "passed"
            and ass["A4_exiled_at_end_combat"] == "passed"
            and ass["A5_never_transformed"] == "failed"):
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: the Galleon attacked, its attack trigger "
            "resolved (return sibling no-op), it was exiled at end of "
            "combat, and it never returned transformed -- Conqueror's "
            "Foothold never entered (confirmed on v0.99.0).")
    elif ass["A5_never_transformed"] == "passed":
        verdict = "not-reproduced"
        notes.append(
            "verdict=not-reproduced: Conqueror's Foothold entered the "
            "battlefield after the end-of-combat exile. Not a fix claim.")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: the reported attack/transform path "
                     "could not be fully exercised; see assertion notes.")

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
               if "galleon" in l.lower()
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
            open(f"{BACKFILL}/driver/scenario_7417.py", "rb").read()).hexdigest(),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "notes": notes,
        "observations": {
            "galleon_bf_oid": ST["galleon_bf_oid"],
            "galleon_entered_turn": ST["galleon_entered_turn"],
            "crewed": ST["crewed"],
            "attack_submitted": ST["attack_submitted"],
            "attack_turn": ST["attack_turn"],
            "trigger_seen": ST["trigger_seen"],
            "trigger_seen_at": ST["trigger_seen_at"],
            "cast_rejections": ST["cast_rejections"],
        },
        "verdict": verdict,
        "limitations": [
            "Browser UI not exercised; native engine via two human-client seats.",
            "The report includes no game state (data-level report); the "
            "scenario replays the reported line (Galleon attack + end of "
            "combat) from a fresh game.",
            "Only the P0-crewed attack path was exercised; the "
            "crew-by-opponent or non-crewed cases were not.",
            "States are authoritative exports, restorable only via full "
            "game replay.",
        ],
        "setup_line": "P0: 4x Conqueror's Galleon + 12x Grizzly Bears + 44x "
                      "Forest; P1: 60x Plains (lands, passes)",
        "contract_line": "Galleon attacks (crewed), end-of-combat delayed "
                         "trigger exiles it -> it must return to the "
                         "battlefield transformed as Conqueror's Foothold",
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
                            if ST["phase"] == "casting":
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
                    # Galleon on the battlefield
                    g_bf = zone_ids(state, 0, "Battlefield", GAL)
                    if g_bf and ST["galleon_bf_oid"] is None:
                        ST["galleon_bf_oid"] = g_bf[0]
                        ST["galleon_entered_turn"] = state.get("turn_number")
                        ST["casting"] = False
                        say(f"Conqueror's Galleon on P0 battlefield (oid "
                            f"{g_bf[0]}), turn {ST['galleon_entered_turn']}")
                    # a bear cast resolving clears the casting flag
                    if (ST.get("casting") and ST.get("cast_was") == "bear"
                            and len(zone_ids(state, 0, "Battlefield", BEAR))
                            > ST.get("bears_at_cast", -1)):
                        ST["casting"] = False
                        wire("bear_resolved", {})
                    # crew completed?
                    if (ST["phase"] == "crewing" and not ST["crewed"]
                            and ST["galleon_bf_oid"] is not None
                            and is_creature(state, ST["galleon_bf_oid"])):
                        ST["crewed"] = True
                        ST["phase"] = "crewed"
                        await export_as(c, "crewed")
                        say("Galleon is now a creature (crewed)")
                    # attack trigger on the stack: entry referencing galleon
                    if (ST["phase"] == "attacked"
                            and not ST["trigger_seen"]):
                        for e in stack_entries(state):
                            ser = json.dumps(e, default=str).lower()
                            if "galleon" in ser:
                                ST["trigger_seen"] = True
                                ST["trigger_seen_at"] = now
                                ST["phase"] = "trigger_wait"
                                ST["pre_life"] = (life_of(state, 0),
                                                  life_of(state, 1))
                                wire("attack_trigger_on_stack",
                                     {"entries": stack_entries(state)})
                                say("Galleon attack trigger observed ON THE "
                                    "STACK; exporting attack")
                                await export_as(c, "attack")
                                ST["trigger_exported"] = True
                                break
                    # trigger resolved: stack empty + Galleon still on BF
                    if (ST["phase"] == "trigger_wait"
                            and not ST["post_trigger_exported"]
                            and not stack_entries(state)
                            and zone_ids(state, 0, "Battlefield", GAL)):
                        await export_as(c, "post_trigger")
                        ST["post_trigger_exported"] = True
                        ST["phase"] = "post_trigger"
                        say("attack trigger resolved; Galleon still on BF; "
                            "post_trigger exported")
                        wire("trigger_resolved",
                             {"waiting_for": wf_of(state).get("type")})
                    # delayed trigger: end of combat, Galleon exiled
                    if (ST["phase"] == "post_trigger"
                            and not ST["end_combat_exported"]):
                        g_ex = zone_ids(state, None, "Exile", GAL)
                        for e in stack_entries(state):
                            ser = json.dumps(e, default=str).lower()
                            if ("galleon" in ser
                                    and state.get("phase") == "EndCombat"
                                    and not ST["end_combat_exported"]):
                                wire("delayed_trigger_on_stack",
                                     {"entries": stack_entries(state),
                                      "phase": state.get("phase")})
                                say("end-of-combat delayed trigger on the "
                                    "stack")
                                break
                        if g_ex:
                            await export_as(c, "end_combat")
                            ST["end_combat_exported"] = True
                            ST["phase"] = "end_combat"
                            say("Galleon in exile at/after end of combat; "
                                "end_combat exported")
                            wire("galleon_exiled",
                                 {"exile_oids": g_ex,
                                  "phase": state.get("phase")})
                    # post: end_combat exported, stack empty, settled
                    if (ST["phase"] == "end_combat"
                            and not ST["post_exported"]
                            and not stack_entries(state)
                            and (wf_of(state).get("type") or "") in
                            ("Priority", "DeclareAttackers")):
                        await export_as(c, "post")
                        ST["post_exported"] = True
                        say("post exported; finalizing")
                        finalized = True
                        break
                    # watchdogs
                    if (ST["phase"] == "crewing"
                            and ST["crew_watchdog_at"]
                            and now - ST["crew_watchdog_at"] > 240
                            and not ST["crewed"]):
                        say("crew watchdog: 240s after crew activation, "
                            "Galleon still not a creature -- exporting state")
                        wire("crew_stuck",
                             {"waiting_for": wf_of(state),
                              "galleon": get_obj(state, ST["galleon_bf_oid"])})
                        await export_as(c, "post")
                        ST["post_exported"] = True
                        finalized = True
                        break
                    if (ST["phase"] == "attacked"
                            and not ST["trigger_seen"]
                            and ST["attack_submitted"] and now - t_start > 900
                            and not ST["post_exported"]):
                        say("attack watchdog: attack submitted long ago, "
                            "trigger never seen -- exporting state")
                        wire("attack_stuck",
                             {"waiting_for": wf_of(state),
                              "stack": stack_entries(state)})
                        await export_as(c, "post")
                        ST["post_exported"] = True
                        finalized = True
                        break
                    if (ST["phase"] == "post_trigger"
                            and not ST["end_combat_exported"]
                            and now - t_start > 1400
                            and not ST["post_exported"]):
                        say("end-combat watchdog: trigger resolved but the "
                            "delayed trigger never exiled the Galleon -- "
                            "exporting state")
                        wire("end_combat_stuck",
                             {"waiting_for": wf_of(state),
                              "stack": stack_entries(state),
                              "phase": state.get("phase")})
                        await export_as(c, "post")
                        ST["post_exported"] = True
                        finalized = True
                        break
                    if (ST["phase"] == "setup"
                            and now - t_start > 1200
                            and ST["galleon_bf_oid"] is None
                            and not ST["post_exported"]):
                        say("setup watchdog: 1200s in, Galleon never reached "
                            "the battlefield -- exporting state")
                        wire("setup_stall",
                             {"waiting_for": wf_of(state),
                              "p0_hand": hand_lnames(state, 0)})
                        await export_as(c, "post")
                        ST["post_exported"] = True
                        finalized = True
                        break
                    # game-over detection
                    if state.get("winner") is not None or state.get("game_over"):
                        say(f"game over detected (winner="
                            f"{state.get('winner')}) -- finalizing")
                        wire("game_over_state",
                             {"winner": state.get("winner")})
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
    shutil.copy(f"{BACKFILL}/driver/scenario_7417.py",
                f"{EVDIR}/scenario_7417.py")
    sys.path.insert(0, f"{BACKFILL}/driver")
    import subprocess
    subprocess.run([sys.executable, f"{BACKFILL}/driver/render_summary.py",
                    EVDIR, str(ISSUE),
                    "Conqueror's Galleon never transforms (return clause "
                    "hoisted out of the delayed trigger)"],
                   check=True)
    files = sorted(f for f in os.listdir(EVDIR) if f != "manifest.sha256")
    with open(f"{EVDIR}/manifest.sha256", "w") as mf:
        for f in files:
            h = hashlib.sha256(open(f"{EVDIR}/{f}", "rb").read()).hexdigest()
            mf.write(f"{h}  {f}\n")
    print(f"manifest written ({len(files)} files); "
          f"verdict={run['verdict']}", flush=True)
    sys.exit(0)
