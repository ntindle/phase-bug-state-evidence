#!/usr/bin/env python3
"""Issue #4836: The Mindskinner taps on attack but does not mill.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (discord, 2026-07-01, triage-confirmed, area:engine/area:parser,
mechanic:replacement-effects): "The Mindskinner taps on attack, but its
damage-prevention/mill replacement does not mill cards."

Oracle (verified against pinned v0.78.0 card-data.json, key 'the mindskinner'):
  "The Mindskinner can't be blocked.
   If a source you control would deal damage to an opponent, prevent that
   damage and each opponent mills that many cards."
  10/1, {U}{U}{U}. Parsed in the dataset as a DamageDone replacement with
  Prevention(All) shield + Mill { count: EventContextAmount, player_scope:
  Opponent } -- so the parse looks complete; any failure is engine-side.

Expected:
  E1: The Mindskinner attacks as a 10/1 and taps as an attacker.
  E2: Its 10 combat damage to the defending opponent is prevented: the
      defending player's life is unchanged (20 -> 20).
  E3: EACH opponent mills 10 (the prevented amount): library -10 / graveyard
      +10 for both P1 and P2.
  E4: A non-combat source P0 controls (Lightning Bolt, 3 damage) is likewise
      prevented: defending player life unchanged, each opponent mills 3.

Assertions:
  A1_setup_ok       P0 DeclareAttackers with an attack-ready Mindskinner;
                    pre.json exported; P1 life 20.
  A2_attack_taps    declared Mindskinner is tapped=true and listed in
                    state.combat.attackers (the reported "taps on attack").
  A3_damage_prevented  P1 life unchanged 20 -> 20 across combat.
  A4_mill_combat    P1 and P2 each mill exactly 10 (lib -10, gy +10).
  A5_bolt_branch    Lightning Bolt at P1: life unchanged, P1 and P2 each
                    mill exactly 3 (lib -3, gy +3).
  A6_cleanup        post.json: stack empty, game proceeding.

Verdict rule: A3 failing (damage went through) or A4 failing (no mill after
the attack) -> reproduced on v0.78.0 (the reported symptom). A5 failing ->
reproduced for the non-combat branch. All of A2-A5 passing -> not-reproduced
(scoped to v0.78.0; the report's build is not tested here; not a fix claim).

Evidence: evidence/4836/<run-id>/pre.json (P0 DeclareAttackers),
mid_attack.json (attackers declared), mid_combat.json (P0 PostCombatMain after
combat), mid_bolt.json (after Bolt resolution), post.json, run.json,
manifest.sha256, summary.png, scenario_4836.py, wire_log.jsonl,
scenario_run.log
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
import client  # noqa: E402
client.URL = "ws://localhost:9374/ws"
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260909-4836"
EVDIR = f"{BACKFILL}/evidence/4836/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

MIND = "The Mindskinner"
BOLT = "Lightning Bolt"
ISLAND = "Island"
MOUNTAIN = "Mountain"

P0_DECK = [(MIND, 8), (BOLT, 12), (ISLAND, 22), (MOUNTAIN, 18)]
P1_DECK = [("Forest", 60)]
P2_DECK = [("Plains", 60)]

SERVER_IDENTITY = {
    "server_version": "0.78.0",
    "build_commit": "4de7224",
    "protocol_version": 68,
    "mode": "Full",
    "binary_sha256": "02c40235ea1e9d9f0b30f4d7e47e68f8787dd6830fa6f0cfba64dfd20dffc3e0",
    "card_data_sha256": "038a49c554606eadcb25c23ce98c944c5385424e0970facc7d9d0fb0a6954df6",
    "draft_pools_sha256": "70e323a23cbd1d0b6bf72fc18cf9fe66ccbc090c4c42699207c1e892483c7ab0",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "observed_at": "2026-09-09",
    "source": "ServerHello + digests re-verified against pinned v0.78.0 release "
              "artifacts (binary+manifest match GitHub asset digests; "
              "data files match the signed manifest); fresh isolated server "
              "on 127.0.0.1:9374 for run 20260909-4836. Latest stable release "
              "re-checked this run: v0.78.0 still newest; no pin advance.",
}


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    RUNLOG.write(m + "\n")
    RUNLOG.flush()


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def obj_name(state, oid):
    o = get_obj(state, oid)
    return o.get("base_name") or o.get("name") or "?"


def lname(state, oid):
    return str(obj_name(state, oid)).lower()


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def life_of(state, pid):
    return player_of(state, pid).get("life")


def lib_count(state, pid):
    return len(player_of(state, pid).get("library", []) or [])


def gy_count(state, pid):
    return len(player_of(state, pid).get("graveyard", []) or [])


def hand_oids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", []) or []]


def hand_lnames(state, pid):
    return [lname(state, o) for o in hand_oids(state, pid)]


def battlefield_ids(state, pid, name):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and lname(state, oid) == name.lower()]


def untapped_lands(state, pid, name):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and lname(state, oid) == name.lower() and not o.get("tapped")]


def land_names(state, pid):
    return sum(1 for n in hand_lnames(state, pid)
               if n in (ISLAND.lower(), MOUNTAIN.lower()))


def can_attack_now(state, oid):
    o = get_obj(state, oid)
    return not o.get("tapped") and not o.get("summoning_sick")


def merged_actions(st):
    acts = list(st.get("legal_actions", []) or [])
    for _oid, payloads in (st.get("legal_actions_by_object") or {}).items():
        for p in payloads or []:
            a = {"type": p.get("type"), "data": p.get("data", {})}
            a["_src_oid"] = _oid
            acts.append(a)
    return acts


def find_action(acts, atype):
    return next((a for a in acts if a["type"] == atype), None)


async def submit_as_is(c, a):
    msg = {"type": a["type"]}
    if "data" in a:
        msg["data"] = a["data"]
    await c.send_action(msg)


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_attack_taps", "A3_damage_prevented",
            "A4_mill_combat", "A5_bolt_branch", "A6_cleanup")}
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK), player_count=3)
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    p2 = PhaseClient("P2")
    await p2.connect()
    await p2.join(p0.game_code, deck(*P2_DECK))
    say(f"game {p0.game_code}; seats P0={p0.player_id} P1={p1.player_id} "
        f"P2={p2.player_id}")

    kept = {}
    mind_cast = False
    bolt_done = False
    pre_exported = mid_attack_exported = mid_combat_exported = False
    mid_bolt_exported = post_exported = False

    obs = {
        "attack_turn": None,
        "mindskinner_oid": None,
        "rejections": [],
        "vi_shapes_seen": [],
        "bolt_target_prompts": 0,
        "life_pre": None, "lib_pre": {}, "gy_pre": {},
        "life_midc": None, "lib_midc": {}, "gy_midc": {},
        "life_midb": None, "lib_midb": {}, "gy_midb": {},
    }

    async def mulligan_tick(c, pid, acts, state, tag, want, want_lands):
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get(tag):
            hn = hand_lnames(state, pid)
            want_n = sum(1 for n in hn if n == want.lower())
            lands = sum(1 for n in hn
                        if n in (ISLAND.lower(), MOUNTAIN.lower()))
            if want:
                ok = want_n >= 1 and lands >= want_lands
            else:
                ok = lands >= want_lands
            mulls = kept.get(f"{tag}_mulls", 0)
            if ok or mulls >= 3:
                kept[tag] = True
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Keep"}}})
                say(f"{tag} keeps ({want or 'any'}={want_n}, lands={lands})")
            else:
                kept[f"{tag}_mulls"] = mulls + 1
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Mulligan"}}})
                say(f"{tag} mulligans #{mulls + 1} ({want or 'any'}={want_n}, "
                    f"lands={lands})")
            return True
        wtype = (state.get("waiting_for") or {}).get("type")
        if wtype == "MulliganDecision":
            sc = find_action(acts, "SelectCards")
            if sc and not kept.get(f"{tag}_bottomed"):
                pending = ((state.get("waiting_for") or {}).get("data", {})
                           or {}).get("pending", [])
                count = 1
                for p in pending:
                    if p.get("player") == pid:
                        ph = p.get("phase", {}) or {}
                        if ph.get("type") == "BottomCards":
                            count = int(ph.get("count", 1))
                hand_ids = hand_oids(state, pid)

                def bottom_key(oid):
                    nm = lname(state, oid)
                    if want and nm == want.lower():
                        return 3
                    if nm in (ISLAND.lower(), MOUNTAIN.lower()):
                        return 2
                    return 1
                picks = sorted(hand_ids, key=bottom_key)[:count]
                kept[f"{tag}_bottomed"] = True
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": [int(x) for x in picks]}})
                say(f"{tag} bottoms {count}: {[lname(state, x) for x in picks]}")
                return True
        return False

    def resolve_player_candidate(opp, seat):
        """Pick a player-seat candidate from a schema target opportunity."""
        data = (opp.get("response") or {}).get("data", {}) or {}
        cands = data.get("candidates") or data.get("choices") or []
        for ch in cands:
            if (ch.get("status") or {}).get("type") != "available":
                continue
            for s in ch.get("surfaces", []) or []:
                d = s.get("data") if isinstance(s.get("data"), dict) else {}
                if d.get("seat") == seat:
                    return ch.get("id"), ch
        return None, None

    async def handle_target_prompt(c, st, state):
        """Answer P0's pending Bolt target prompt with P1. Returns True if
        an opportunity was answered."""
        vi = st.get("viewer_interaction") or {}
        if not vi.get("canSubmit"):
            return False
        answered = False
        for opp in vi.get("opportunities", []) or []:
            rtype = (opp.get("response") or {}).get("type")
            spec = ((opp.get("response") or {}).get("data") or {}).get("spec") or {}
            if rtype != "schema" or spec.get("type") not in (
                    "sequence", "select", "target"):
                continue
            iid = opp.get("interactionId")
            if iid in answered_iids:
                continue
            shape = json.dumps(opp)[:1200]
            if shape not in obs["vi_shapes_seen"]:
                obs["vi_shapes_seen"].append(shape)
                wire("bolt_target_opp_shape", opp)
                say(f"target opportunity shape: {shape[:400]}")
            cid, ch = resolve_player_candidate(opp, 1)
            if cid is None:
                notes.append("Bolt target prompt offered no P1 candidate; "
                             "not answering blindly")
                wire("bolt_target_no_match", opp)
                return False
            sub = {"interactionId": iid,
                   "response": {"type": "sequence",
                                "data": {"choiceIds": [cid]}}}
            wire("bolt_target_submit", sub)
            await c.send_interaction(sub)
            answered_iids.add(iid)
            obs["bolt_target_prompts"] += 1
            say(f"P0 answers Bolt target prompt -> P1 (choice {cid})")
            answered = True
        return answered

    answered_iids = set()

    async def declare_attackers(c, pid, tag, attack_oids, defend_pid):
        state = c.latest["state"]
        acts = merged_actions(c.latest)
        da = find_action(acts, "DeclareAttackers")
        if da:
            sub = copy.deepcopy(da)
            sub["data"]["attacks"] = [
                [o, {"type": "Player", "data": defend_pid}] for o in attack_oids]
            sub["data"]["bands"] = []
            wire(f"{tag}_declare_attackers_submit", sub["data"])
            await c.send_action({"type": "DeclareAttackers", "data": sub["data"]})
            say(f"{tag} submits DeclareAttackers attacks={attack_oids} "
                f"vs P{defend_pid}")
            return True
        say(f"{tag} DeclareAttackers WAIT: no action offered")
        return False

    async def p0_tick(st, acts, state):
        nonlocal mind_cast, bolt_done
        nonlocal pre_exported, mid_attack_exported, mid_combat_exported
        nonlocal mid_bolt_exported
        pid, tag, c = 0, "P0", p0
        wtype = (state.get("waiting_for") or {}).get("type")
        if await mulligan_tick(c, pid, acts, state, tag, MIND, 2):
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(c, a)
                return
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(c, oa)
            return
        if await handle_target_prompt(c, st, state):
            return
        if wtype == "AssignCombatDamage":
            ad = find_action(acts, "AssignCombatDamage")
            if ad:
                await submit_as_is(c, ad)
                say("P0 submits advertised AssignCombatDamage")
            return
        # mid-attack checkpoint: attackers declared, Mindskinner in the list
        attackers = (state.get("combat") or {}).get("attackers") or []
        mid_oid = next((a.get("object_id") for a in attackers
                        if lname(state, a.get("object_id")) == MIND.lower()), None)
        if mid_oid and not mid_attack_exported:
            obs["mindskinner_oid"] = int(mid_oid)
            try:
                mid = await c.export_state()
                with open(f"{EVDIR}/mid_attack.json", "w") as f:
                    f.write(mid)
                mid_attack_exported = True
                say(f"exported mid_attack.json (attacker oid {mid_oid}, "
                    f"tapped={get_obj(state, int(mid_oid)).get('tapped')})")
            except Exception as e:
                notes.append(f"mid_attack export failed: {e}")
        if wtype == "DeclareAttackers" and state.get("active_player") == 0:
            ready = [o for o in battlefield_ids(state, 0, MIND)
                     if can_attack_now(state, o)]
            if ready and not pre_exported:
                say("P0 DeclareAttackers with attack-ready Mindskinner; "
                    "exporting PRE")
                try:
                    pre = await c.export_state()
                    with open(f"{EVDIR}/pre.json", "w") as f:
                        f.write(pre)
                    pre_st = json.loads(pre)["state"]
                    obs["life_pre"] = life_of(pre_st, 1)
                    obs["lib_pre"] = {i: lib_count(pre_st, i) for i in (1, 2)}
                    obs["gy_pre"] = {i: gy_count(pre_st, i) for i in (1, 2)}
                    ass["A1_setup_ok"] = "passed"
                    notes.append(f"pre: P0 Mindskinner={len(ready)} ready, "
                                 f"P1 life={obs['life_pre']}, "
                                 f"P1 lib={obs['lib_pre'][1]}, "
                                 f"P2 lib={obs['lib_pre'][2]}")
                    pre_exported = True
                except Exception as e:
                    notes.append(f"pre export failed: {e}")
            if ready:
                if obs["attack_turn"] is None:
                    obs["attack_turn"] = state.get("turn_number")
                    obs["mindskinner_oid"] = int(ready[0])
                await declare_attackers(c, pid, tag, [ready[0]], 1)
            else:
                await declare_attackers(c, pid, tag, [], 1)
            return
        if wtype == "DeclareBlockers" and state.get("active_player") != 0:
            db = find_action(acts, "DeclareBlockers")
            if db:
                sub = copy.deepcopy(db)
                sub["data"]["assignments"] = []
                await c.send_action({"type": "DeclareBlockers",
                                     "data": sub["data"]})
            return
        if wtype != "Priority" or state.get("priority_player") != 0:
            return
        # ---- P0 priority ----
        phase = state.get("phase")
        turn = state.get("turn_number")
        # mid-combat checkpoint at first PostCombatMain priority of attack turn
        if (pre_exported and not mid_combat_exported
                and phase == "PostCombatMain"
                and obs["attack_turn"] is not None
                and turn == obs["attack_turn"]):
            try:
                mid = await c.export_state()
                with open(f"{EVDIR}/mid_combat.json", "w") as f:
                    f.write(mid)
                mc = json.loads(mid)["state"]
                obs["life_midc"] = life_of(mc, 1)
                obs["lib_midc"] = {i: lib_count(mc, i) for i in (1, 2)}
                obs["gy_midc"] = {i: gy_count(mc, i) for i in (1, 2)}
                mid_combat_exported = True
                say(f"exported mid_combat.json: P1 life={obs['life_midc']}, "
                    f"P1 lib={obs['lib_midc'][1]} gy={obs['gy_midc'][1]}, "
                    f"P2 lib={obs['lib_midc'][2]} gy={obs['gy_midc'][2]}")
            except Exception as e:
                notes.append(f"mid_combat export failed: {e}")
            return
        # bolt resolution checkpoint: Bolt in P0 graveyard, stack quiet.
        # (Checked BEFORE the cast branch so we never cast a second Bolt.)
        if (obs.get("bolt_cast") and not mid_bolt_exported
                and not (state.get("stack") or [])):
            p0_gy = [lname(state, o) for o in
                     player_of(state, 0).get("graveyard", []) or []]
            if BOLT.lower() in p0_gy:
                try:
                    mid = await c.export_state()
                    with open(f"{EVDIR}/mid_bolt.json", "w") as f:
                        f.write(mid)
                    mb = json.loads(mid)["state"]
                    obs["life_midb"] = life_of(mb, 1)
                    obs["lib_midb"] = {i: lib_count(mb, i) for i in (1, 2)}
                    obs["gy_midb"] = {i: gy_count(mb, i) for i in (1, 2)}
                    mid_bolt_exported = True
                    bolt_done = True
                    say(f"exported mid_bolt.json: P1 life={obs['life_midb']}, "
                        f"P1 lib={obs['lib_midb'][1]} gy={obs['gy_midb'][1]}, "
                        f"P2 lib={obs['lib_midb'][2]} gy={obs['gy_midb'][2]}")
                except Exception as e:
                    notes.append(f"mid_bolt export failed: {e}")
                return
        # bolt branch: mid-combat captured, Bolt + red mana available, and no
        # Bolt cast yet this run (exactly one Bolt for a clean mill delta)
        if (mid_combat_exported and not bolt_done and not obs.get("bolt_cast")
                and phase in ("PreCombatMain", "PostCombatMain")
                and BOLT.lower() in hand_lnames(state, 0)
                and untapped_lands(state, 0, MOUNTAIN)):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" and lname(
                        state, d.get("object_id")) == BOLT.lower():
                    say("P0 casts Lightning Bolt (non-combat damage branch)")
                    wire("cast_p0_bolt", a)
                    await submit_as_is(c, a)
                    obs["bolt_cast"] = True
                    return
        # cast the Mindskinner
        if (not mind_cast and phase in ("PreCombatMain", "PostCombatMain")
                and MIND.lower() in hand_lnames(state, 0)
                and len(untapped_lands(state, 0, ISLAND)) >= 3
                and not battlefield_ids(state, 0, MIND)):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" and lname(
                        state, d.get("object_id")) == MIND.lower():
                    say("P0 casts The Mindskinner")
                    wire("cast_p0_mind", a)
                    await submit_as_is(c, a)
                    mind_cast = True
                    return
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(c, a)
                return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(c, a)
                return

    async def passive_tick(c, pid, tag):
        st = c.latest
        acts = merged_actions(st)
        state = st["state"]
        wtype = (state.get("waiting_for") or {}).get("type")
        want = "" if pid else None
        if await mulligan_tick(c, pid, acts, state, tag, want, 2):
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(c, a)
                return
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(c, oa)
            return
        if wtype == "AssignCombatDamage":
            ad = find_action(acts, "AssignCombatDamage")
            if ad:
                await submit_as_is(c, ad)
            return
        if wtype == "DeclareAttackers" and state.get("active_player") == pid:
            await declare_attackers(c, pid, tag, [], 0)
            return
        if wtype == "DeclareBlockers" and state.get("active_player") == 0:
            db = find_action(acts, "DeclareBlockers")
            if db:
                sub = copy.deepcopy(db)
                sub["data"]["assignments"] = []
                await c.send_action({"type": "DeclareBlockers",
                                     "data": sub["data"]})
                say(f"{tag} declares no blockers (Mindskinner can't be "
                    f"blocked anyway)")
            return
        if wtype != "Priority" or state.get("priority_player") != pid:
            return
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(c, a)
                return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(c, a)
                return

    async def p1_tick(st, acts, state):
        await passive_tick(p1, 1, "P1")

    async def p2_tick(st, acts, state):
        await passive_tick(p2, 2, "P2")

    async def finish():
        nonlocal post_exported
        dur = time.time() - t_start
        if not post_exported:
            try:
                post = await p0.export_state()
                with open(f"{EVDIR}/post.json", "w") as f:
                    f.write(post)
                post_exported = True
            except Exception as e:
                notes.append(f"post export failed: {e}")

        def load_env(name):
            p = f"{EVDIR}/{name}.json"
            if not os.path.exists(p):
                return None
            try:
                return json.loads(open(p).read())["state"]
            except Exception as e:
                notes.append(f"{name}.json reload failed: {e}")
                return None

        pre_st = load_env("pre")
        ma_st = load_env("mid_attack")
        mc_st = load_env("mid_combat")
        mb_st = load_env("mid_bolt")
        post_st = load_env("post")

        # A2: the declared Mindskinner tapped (the reported observation)
        if ma_st is not None and obs["mindskinner_oid"]:
            oid = obs["mindskinner_oid"]
            attackers = (ma_st.get("combat") or {}).get("attackers") or []
            listed = any(a.get("object_id") == oid for a in attackers)
            tapped = bool(get_obj(ma_st, oid).get("tapped"))
            power = get_obj(ma_st, oid).get("power")
            if listed and tapped:
                ass["A2_attack_taps"] = "passed"
                notes.append(f"A2 passed: Mindskinner oid {oid} in "
                             f"combat.attackers, tapped={tapped}, power={power}")
            else:
                ass["A2_attack_taps"] = "failed"
                notes.append(f"A2 failed: oid {oid} listed={listed} "
                             f"tapped={tapped} power={power}")
        else:
            ass["A2_attack_taps"] = "failed"
            notes.append("A2 failed: no mid_attack checkpoint with the "
                         "Mindskinner as an attacker")

        # A3: damage prevented (P1 life unchanged across combat)
        if obs["life_pre"] is not None and obs["life_midc"] is not None:
            if obs["life_midc"] == obs["life_pre"]:
                ass["A3_damage_prevented"] = "passed"
                notes.append(f"A3 passed: P1 life {obs['life_pre']} -> "
                             f"{obs['life_midc']} (10 combat damage prevented)")
            else:
                ass["A3_damage_prevented"] = "failed"
                notes.append(f"A3 FAILED: P1 life {obs['life_pre']} -> "
                             f"{obs['life_midc']}; combat damage was NOT "
                             f"prevented")
        else:
            notes.append("A3 not evaluated (missing pre/mid_combat life)")

        # A4: each opponent mills exactly 10 from the combat replacement
        if obs["lib_pre"] and obs["lib_midc"]:
            parts = []
            ok = True
            for i in (1, 2):
                milled = obs["lib_pre"][i] - obs["lib_midc"][i]
                gyed = obs["gy_midc"][i] - obs["gy_pre"][i]
                parts.append(f"P{i} lib-{milled}/gy+{gyed}")
                if milled != 10 or gyed != 10:
                    ok = False
            if ok:
                ass["A4_mill_combat"] = "passed"
            else:
                ass["A4_mill_combat"] = "failed"
            notes.append(f"A4 {'passed' if ok else 'FAILED'}: "
                         + ", ".join(parts) + " (expected lib-10/gy+10 each)")
        else:
            notes.append("A4 not evaluated (missing pre/mid_combat counts)")

        # A5: Bolt branch -- life unchanged, each opponent mills exactly 3
        if obs["lib_midc"] and obs["lib_midb"]:
            parts = []
            ok = True
            life_ok = obs["life_midb"] == obs["life_midc"]
            for i in (1, 2):
                milled = obs["lib_midc"][i] - obs["lib_midb"][i]
                gyed = obs["gy_midb"][i] - obs["gy_midc"][i]
                parts.append(f"P{i} lib-{milled}/gy+{gyed}")
                if milled != 3 or gyed != 3:
                    ok = False
            if ok and life_ok:
                ass["A5_bolt_branch"] = "passed"
            else:
                ass["A5_bolt_branch"] = "failed"
            notes.append(f"A5 {'passed' if (ok and life_ok) else 'FAILED'}: "
                         + ", ".join(parts)
                         + f"; P1 life {obs['life_midc']} -> {obs['life_midb']} "
                         + "(expected lib-3/gy+3 each, life unchanged)")
        else:
            notes.append("A5 not evaluated (bolt branch did not complete; "
                         f"bolt_cast={obs.get('bolt_cast')}, "
                         f"mid_bolt_exported={mid_bolt_exported})")

        # A6: cleanup
        if post_st is not None:
            slen = len(post_st.get("stack", []) or [])
            wf = (post_st.get("waiting_for") or {}).get("type")
            if slen == 0 and wf in ("Priority",):
                ass["A6_cleanup"] = "passed"
                notes.append(f"A6 passed: post.json stack empty, wf={wf}, "
                             f"turn={post_st.get('turn_number')}, "
                             f"phase={post_st.get('phase')}")
            else:
                ass["A6_cleanup"] = "failed"
                notes.append(f"A6 failed: post stack={slen}, wf={wf}")
        else:
            notes.append("A6 not evaluated (no post.json)")

        # Verdict
        failed_core = [k for k in ("A3_damage_prevented", "A4_mill_combat",
                                   "A5_bolt_branch")
                       if ass[k] == "failed"]
        passed_core = all(ass[k] == "passed" for k in
                          ("A2_attack_taps", "A3_damage_prevented",
                           "A4_mill_combat", "A5_bolt_branch"))
        if failed_core:
            verdict = "reproduced"
            notes.append("verdict reproduced on v0.78.0: failing assertions: "
                         + ", ".join(failed_core))
        elif passed_core:
            verdict = "not-reproduced"
            notes.append("verdict not-reproduced on v0.78.0: the full reported "
                         "path completes (combat damage prevented, each "
                         "opponent mills 10; Bolt damage prevented, each "
                         "opponent mills 3). Not tested on the report's "
                         "original build; not a fix claim.")
        else:
            verdict = "blocked"
            notes.append("verdict blocked: scenario did not reach the "
                         "evaluation checkpoints: "
                         + json.dumps(ass))
        run = {
            "issue": 4836,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "server_port": 9374,
            "driver": {"protocol_advertised": 68, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_4836.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK, "P2": P2_DECK},
            "issue_title": "The Mindskinner taps on attack but does not mill",
            "assertions": ass,
            "notes": notes,
            "observations": obs,
            "verdict": verdict,
            "limitations": [
                "Browser UI not exercised; native engine via three human-client seats.",
                "8x Mindskinner / 12x Lightning Bolt deck density is a "
                "test-harness convenience (engine accepts >4-of for custom games).",
                "Not tested on the report's original 2026-07-01 build; verdict "
                "is scoped to v0.78.0, not a fix claim.",
                "The prebuilt server has no standalone state-restore; states are "
                "authoritative exports (restorable only via full game replay).",
            ],
            "setup_line": "P0: 8x The Mindskinner + 12x Lightning Bolt + 22x Island "
                          "+ 18x Mountain (mulligan to Mindskinner + lands, cast "
                          "Mindskinner {U}{U}{U}, attack P1); P1/P2: 60x "
                          "land each, passive (never attack)",
            "contract_line": "The Mindskinner attacks as a 10/1: it taps, its 10 "
                             "combat damage to P1 is prevented, and EACH opponent "
                             "(P1, P2) mills 10; then Lightning Bolt at P1 is "
                             "prevented and each opponent mills 3",
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

    t0 = time.time()
    last = {}
    last_diag = 0.0
    last_tick_at = {}
    clients = ((p0, p0_tick, "P0"), (p1, p1_tick, "P1"), (p2, p2_tick, "P2"))
    while time.time() - t0 < 1800:
        await asyncio.sleep(0.15)
        for c, tick, tag in clients:
            st = c.latest
            if not st:
                continue
            rev = c.revision
            same_rev = (rev == last.get(c.name))
            stale = time.time() - last_tick_at.get(c.name, 0) > 5
            if same_rev and not stale:
                continue
            last[c.name] = rev
            last_tick_at[c.name] = time.time()
            try:
                await tick(st, merged_actions(st), st["state"])
            except Exception as e:
                say(f"tick error {tag}: {e}")
                wire("tick_error", {"who": tag, "err": str(e)})
        # post-export trigger: bolt branch done and game advanced a turn
        if (mid_bolt_exported and not post_exported and p0.latest
                and obs["attack_turn"] is not None):
            s = p0.latest["state"]
            if (s.get("turn_number") or 0) > obs["attack_turn"]:
                try:
                    post = await p0.export_state()
                    with open(f"{EVDIR}/post.json", "w") as f:
                        f.write(post)
                    post_exported = True
                    say("exported POST")
                except Exception as e:
                    notes.append(f"post export failed: {e}")
        if post_exported:
            say("post exported; finishing")
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} P0mind={len(battlefield_ids(s, 0, MIND))} "
                f"life1={life_of(s, 1)} P1lib={lib_count(s, 1)} P2lib={lib_count(s, 2)} "
                f"mind_cast={mind_cast} atk_turn={obs['attack_turn']} "
                f"bolt_cast={obs.get('bolt_cast')} bolt_done={bolt_done}")
    notes.append("global timeout (1800s) hit before assertions resolved")
    await finish()


asyncio.run(main())
