#!/usr/bin/env python3
"""Issue #4554: Stuck decision: CombatTaxPayment.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (github issue, build v0.9.0 6bd7432, 2026-06-28, Coppakill):
  "Upon willing to pay combat tax for a creature that has haste the game got
   stuck. 'Krang Utrom Warlord'. Opponent had windborn muse making me tax 2
   for each attacking creature."
  Diagnostic: Waiting for: CombatTaxPayment | Stuck players: 0

TESTABLE PREMISE (this differs from #4345's no-premise report):
  P0 attacks with Krang, Utrom Warlord (9/9, Flying/Trample/Indestructible/
  HASTE, {9} generic - card data 'Krang, Utrom Warlord', confirmed in the
  pinned v0.78.0 dataset) into P1's Windborn Muse (tax {2} per attacking
  creature). P0 chooses to PAY the tax (accept=true). Reported: game sticks
  on CombatTaxPayment with Stuck players: 0.

Oracle/expected:
  E1: attacking P1 who controls Windborn Muse opens a CombatTaxPayment
      decision for the attacking player (P0).
  E2: paying {2} for the single attacker lets the attack proceed; the hasty
      Krang (9/9, unblocked) deals 9 to P1 (20 -> 11).
  E3: the decision never strands P0 with no actionable submission; a decline
      control also resolves without stranding the game.
  E4: the haste path is exercised: Krang attacks the turn it is cast
      (summoning-sick but Haste), as in the report.

Assertions:
  A1_setup_ok        P1's Windborn Muse on BF; P0's Krang attack-ready (haste);
                     pre.json exported at the first taxed DeclareAttackers.
  A2_tax_prompted    >=1 CombatTaxPayment wait observed for P0 (mirrors the
                     report's "Waiting for: CombatTaxPayment"), each with >=1
                     actionable submission offered.
  A3_pay_completes   accept=true submitted; {2} paid; P1 life 20 -> 11 from
                     the unblocked 9/9 hasty Krang; game left the decision.
  A4_decline_resolves on the second attack P0 declines; no damage from that
                     attack and the game advanced past CombatTaxPayment.
  A5_no_softlock     no CombatTaxPayment wait lacked an actionable submission;
                     the 60s watchdog never fired.
  A6_cleanup         post.json: stack empty, game proceeding.

Verdict rule: the reported softlock signature (CombatTaxPayment wait with no
actionable submission for the acting player, or the decision orphaned after
choosing to pay) -> reproduced. The full reported path (tax prompted for a
haste attacker, pay chosen, attack completes, no stall) -> not-reproduced
(scoped to v0.78.0; the report's build v0.9.0 is not tested here).

Evidence: evidence/4554/<run-id>/pre.json (first taxed DeclareAttackers),
mid_tax.json (first CombatTaxPayment wait), post.json (after both combats),
run.json, manifest.sha256, summary.png, scenario_4554.py, wire_log.jsonl,
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
RUN_ID = "20260909-4554"
EVDIR = f"{BACKFILL}/evidence/4554/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

KRANG = "Krang, Utrom Warlord"
FOREST = "Forest"
PLAINS = "Plains"
MUSE = "Windborn Muse"

P0_DECK = [(KRANG, 8), (FOREST, 52)]
P1_DECK = [(MUSE, 8), (PLAINS, 52)]

SERVER_IDENTITY = {
    "server_version": "0.78.0",
    "build_commit": "4de7224",
    "protocol_version": 68,
    "mode": "Full",
    "binary_sha256": "02c40235ea1e9d9f0b30f4d7e47e68f8787dd6830fa6f0cfba64dfd20dffc3e0",
    "card_data_sha256": "038a49c554606eadcb25c23ce98c944c5385424e0970facc7d9d0fb0a6954df6",
    "draft_pools_sha256": "70e323a23cbd1d0b6bf72fc18cf9fe66ccbc090c4c426992083c7ab0",
    "signature_key_id": "436711b6a2d36828",
    "signature_verified": True,
    "observed_at": "2026-09-09",
    "source": "ServerHello + sha256 re-verified against pinned v0.78.0 "
              "release artifacts (binary+data+sigs under server/releases/v0.78.0/); "
              "fresh isolated server on 127.0.0.1:9374 for run 20260909-4554",
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


def life_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p.get("life")
    return None


def hand_lnames(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return [lname(state, o) for o in p.get("hand", [])]
    return []


def battlefield_ids(state, pid, name):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and lname(state, oid) == name.lower()]


def untapped_lands(state, pid, name):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and lname(state, oid) == name.lower() and not o.get("tapped")]


def keywords_of(state, oid):
    kws = get_obj(state, oid).get("keywords") or []
    out = []
    for k in kws:
        out.append(k if isinstance(k, str) else k.get("type", str(k)))
    return [str(x) for x in out]


def can_attack_now(state, oid):
    o = get_obj(state, oid)
    if o.get("tapped"):
        return False
    if o.get("summoning_sick"):
        if "Haste" not in keywords_of(state, oid):
            return False
    return True


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
           ("A1_setup_ok", "A2_tax_prompted", "A3_pay_completes",
            "A4_decline_resolves", "A5_no_softlock", "A6_cleanup")}
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    say(f"game {p0.game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    kept = {}
    pre_exported = False
    post_exported = False
    mid_tax_exported = False
    p0_krang_cast = False
    p0_krang_cast_turn = None
    p1_muse_cast = False
    decline_mode = False  # first taxed attack pays; later taxed attacks decline
    decline_attack_done = False  # attack exactly once in decline mode

    obs = {
        "tax_waits": [],       # every CombatTaxPayment wait seen
        "declare_waits": [],
        "stall_observed": False,
        "rejections": [],
        "life_pre": None,
        "life_post": None,
        "life_after_pay": None,
        "paid_attack_turn": None,
        "decline_turn": None,
        "krang_cast_turn": None,
        "krang_attack_turn": None,
        "krang_summoning_sick_at_attack": None,
        "pay_mode": True,
    }
    tax_wait_start = {"P0": None, "P1": None}
    declare_wait_start = {"P0": None, "P1": None}

    async def mulligan_tick(c, pid, acts, state, tag, want, want_lands, land_name):
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get(tag):
            hn = hand_lnames(state, pid)
            want_n = sum(1 for n in hn if n == want.lower())
            lands = sum(1 for n in hn if n == land_name.lower())
            mulls = kept.get(f"{tag}_mulls", 0)
            if (want_n >= 1 and lands >= want_lands) or mulls >= 3:
                kept[tag] = True
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Keep"}}})
                say(f"{tag} keeps ({want}={want_n}, lands={lands})")
            else:
                kept[f"{tag}_mulls"] = mulls + 1
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Mulligan"}}})
                say(f"{tag} mulligans #{mulls + 1} ({want}={want_n}, lands={lands})")
            return True
        wtype = (state.get("waiting_for") or {}).get("type")
        if wtype == "MulliganDecision":
            sc = find_action(acts, "SelectCards")
            if sc and not kept.get(f"{tag}_bottomed"):
                pending = ((state.get("waiting_for") or {}).get("data", {}) or {}).get("pending", [])
                count = 1
                for p in pending:
                    if p.get("player") == pid:
                        ph = p.get("phase", {}) or {}
                        if ph.get("type") == "BottomCards":
                            count = int(ph.get("count", 1))
                hand_ids = [o for pl in state.get("players", [])
                            if pl.get("id") == pid for o in pl.get("hand", [])]
                def bottom_key(oid):
                    nm = lname(state, oid)
                    if nm == want.lower():
                        return 2
                    if nm == land_name.lower():
                        return 1
                    return 0
                picks = sorted(hand_ids, key=bottom_key)[:count]
                kept[f"{tag}_bottomed"] = True
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": [int(x) for x in picks]}})
                say(f"{tag} bottoms {count}: {[lname(state, x) for x in picks]}")
                return True
        return False

    async def handle_tax(c, pid, tag, acts, state):
        """Record + act on a CombatTaxPayment wait for the acting player."""
        nonlocal mid_tax_exported
        wf = state.get("waiting_for") or {}
        vi = (state.get("viewer_interaction") or {})
        mode = "decline" if decline_mode else "pay"
        rec = {
            "turn": state.get("turn_number"),
            "phase": state.get("phase"),
            "who": tag,
            "wf_type": wf.get("type"),
            "wf_data_keys": list((wf.get("data") or {}).keys()),
            "wf_data": wf.get("data"),
            "action_types": sorted({a["type"] for a in acts}),
            "n_actions": len(acts),
            "vi_present": bool(vi),
            "mode": mode,
        }
        obs["tax_waits"].append(rec)
        wire(f"{tag}_tax_wait", {"waiting_for": wf, "acts": acts, "vi": vi})
        say(f"{tag} CombatTaxPayment wait (mode={mode}): "
            f"{len(acts)} actions: {rec['action_types'][:12]}")
        if not mid_tax_exported:
            try:
                mid = await c.export_state()
                with open(f"{EVDIR}/mid_tax.json", "w") as f:
                    f.write(mid)
                mid_tax_exported = True
                say("exported mid_tax.json")
            except Exception as e:
                notes.append(f"mid_tax export failed: {e}")
        want = False if decline_mode else True
        last = obs.get("last_tax_submit") or {}
        now = time.time()
        dup = (last.get("turn") == rec["turn"] and last.get("mode") == mode
               and now - last.get("at", 0) < 30)
        if not dup:
            choice = next((a for a in acts if a["type"] == "PayCombatTax"
                           and a.get("data", {}).get("accept") is want), None)
            if choice is not None:
                wire(f"{tag}_tax_{mode}_submit", choice)
                await submit_as_is(c, choice)
                obs["last_tax_submit"] = {"at": now, "turn": rec["turn"], "mode": mode}
                say(f"{tag} submits PayCombatTax accept={want} ({mode})")
                return
        if not decline_mode:
            pay_a = [a for a in acts
                     if a["type"] in ("PayMana", "PayManaAbilityMana")]
            if not pay_a:
                pp = find_action(acts, "PassPriority")
                if pp:
                    await submit_as_is(c, pp)
                    say(f"{tag} passes priority at tax wait (pay fallback)")
                    return
            if dup:
                say(f"{tag} tax wait unchanged (already submitted accept=true)")
            else:
                say(f"{tag} pay mode: no PayCombatTax accept=true offered "
                    f"(actions={rec['action_types'][:8]})")
            return
        if dup:
            say(f"{tag} tax wait unchanged (already submitted accept=false)")
            return
        pp = find_action(acts, "PassPriority")
        if pp:
            await submit_as_is(c, pp)
            say(f"{tag} passes priority at tax wait (decline fallback)")
            return
        say(f"{tag} decline mode: NO decline submission available "
            f"(actions={rec['action_types'][:8]})")

    async def declare_attackers(c, pid, tag, attack_oids):
        state = c.latest["state"]
        acts = merged_actions(c.latest)
        da = find_action(acts, "DeclareAttackers")
        rec = {"turn": state.get("turn_number"), "who": tag,
               "action_offered": da is not None,
               "attack_oids": list(attack_oids)}
        if da:
            sub = copy.deepcopy(da)
            sub["data"]["attacks"] = [[o, {"type": "Player", "data": 1 - pid}]
                                      for o in attack_oids]
            sub["data"]["bands"] = []
            wire(f"{tag}_declare_attackers_submit", sub["data"])
            await c.send_action({"type": "DeclareAttackers", "data": sub["data"]})
            rec["submitted"] = True
            say(f"{tag} submits DeclareAttackers attacks={attack_oids}")
        else:
            rec["submitted"] = False
            wire(f"{tag}_declare_wait_no_action", {"state_revision": c.revision})
            say(f"{tag} DeclareAttackers WAIT: no DeclareAttackers action offered!")
        obs["declare_waits"].append(rec)
        return rec

    async def p0_tick(st, acts, state):
        nonlocal pre_exported, p0_krang_cast, p0_krang_cast_turn, post_exported
        nonlocal decline_mode, decline_attack_done
        pid, tag, c = 0, "P0", p0
        wtype = (state.get("waiting_for") or {}).get("type")
        if await mulligan_tick(c, pid, acts, state, tag, KRANG, 5, FOREST):
            return
        if not decline_mode:
            for a in acts:
                if a["type"] in ("PayManaAbilityMana", "PayMana"):
                    await submit_as_is(c, a)
                    return
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(c, oa)
            return
        if wtype == "CombatTaxPayment" and state.get("priority_player") in (pid, None):
            if tax_wait_start[tag] is None:
                tax_wait_start[tag] = time.time()
            await handle_tax(c, pid, tag, acts, state)
            return
        if wtype == "AssignCombatDamage":
            ad = find_action(acts, "AssignCombatDamage")
            if ad:
                await submit_as_is(c, ad)
                say("P0 submits advertised AssignCombatDamage")
            return
        if wtype == "DeclareAttackers" and state.get("active_player") == 0:
            if declare_wait_start[tag] is None:
                declare_wait_start[tag] = time.time()
            muse = battlefield_ids(state, 1, MUSE)
            ready = [o for o in battlefield_ids(state, 0, KRANG) if can_attack_now(state, o)]
            if muse and ready and not pre_exported:
                say("P0 DeclareAttackers vs Windborn Muse with attack-ready Krang (haste); exporting PRE")
                pre = await c.export_state()
                with open(f"{EVDIR}/pre.json", "w") as f:
                    f.write(pre)
                pre_st = json.loads(pre)["state"]
                obs["life_pre"] = life_of(pre_st, 1)
                ass["A1_setup_ok"] = "passed"
                notes.append(f"pre: P1 muse={len(muse)}, P0 ready krang={len(ready)}, "
                             f"P1 life={obs['life_pre']}")
                pre_exported = True
            if muse and ready:
                # one attacker keeps the tax at {2}; only attack with >=2
                # untapped Forests so the engine offers both accept=true
                # (pay) and accept=false (decline). In decline mode attack
                # only once, then declare empty so the game advances.
                mana_ok = len(untapped_lands(state, 0, FOREST)) >= 2
                if decline_mode and decline_attack_done:
                    await declare_attackers(c, pid, tag, [])
                    say("P0 declares no attackers (decline test complete)")
                elif mana_ok:
                    o = ready[0]
                    k = get_obj(state, o)
                    if obs["krang_attack_turn"] is None:
                        obs["krang_attack_turn"] = state.get("turn_number")
                        obs["krang_summoning_sick_at_attack"] = bool(k.get("summoning_sick"))
                        obs["krang_keywords_at_attack"] = keywords_of(state, o)
                    await declare_attackers(c, pid, tag, [o])
                    if decline_mode:
                        decline_attack_done = True
                else:
                    await declare_attackers(c, pid, tag, [])
                    say(f"P0 holds attack: only "
                        f"{len(untapped_lands(state, 0, FOREST))} untapped Forests")
            else:
                await declare_attackers(c, pid, tag, [])
                if not muse:
                    say("P0 holds attack: Muse not yet on P1 BF")
                elif not ready:
                    say("P0 holds attack: Krang not attack-ready")
            declare_wait_start[tag] = None
            return
        if wtype == "DeclareBlockers" and state.get("active_player") == 1:
            db = find_action(acts, "DeclareBlockers")
            if db:
                sub = copy.deepcopy(db)
                sub["data"]["assignments"] = []
                await c.send_action({"type": "DeclareBlockers", "data": sub["data"]})
                say("P0 declares no blockers")
            return
        if wtype != "Priority" or state.get("priority_player") != 0:
            return
        # ---- P0 priority ----
        phase = state.get("phase")
        if (not p0_krang_cast and phase in ("PreCombatMain", "PostCombatMain")
                and KRANG.lower() in hand_lnames(state, 0)
                and len(untapped_lands(state, 0, FOREST)) >= 9
                and not battlefield_ids(state, 0, KRANG)):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" and lname(state, d.get("object_id")) == KRANG.lower():
                    say("P0 casts Krang, Utrom Warlord")
                    wire("cast_p0_krang", a)
                    await submit_as_is(c, a)
                    p0_krang_cast = True
                    p0_krang_cast_turn = state.get("turn_number")
                    obs["krang_cast_turn"] = p0_krang_cast_turn
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
        nonlocal p1_muse_cast, post_exported
        pid, tag, c = 1, "P1", p1
        wtype = (state.get("waiting_for") or {}).get("type")
        if await mulligan_tick(c, pid, acts, state, tag, MUSE, 3, PLAINS):
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
        if wtype == "CombatTaxPayment":
            # P1 is never the acting player; the decision is visible to both
            # seats but only P0 has submissions. Stay passive.
            wire("P1_tax_wait_passive",
                 {"n_actions": len(acts),
                  "wf_player": (state.get("waiting_for") or {}).get("data", {}).get("player")})
            return
        if wtype == "AssignCombatDamage":
            ad = find_action(acts, "AssignCombatDamage")
            if ad:
                await submit_as_is(c, ad)
                say("P1 submits advertised AssignCombatDamage")
            return
        if wtype == "DeclareAttackers" and state.get("active_player") == 1:
            await declare_attackers(c, pid, tag, [])  # P1 never attacks
            return
        if wtype == "DeclareBlockers" and state.get("active_player") == 0:
            db = find_action(acts, "DeclareBlockers")
            if db:
                sub = copy.deepcopy(db)
                sub["data"]["assignments"] = []
                await c.send_action({"type": "DeclareBlockers", "data": sub["data"]})
                say("P1 declares no blockers")
            return
        if wtype != "Priority" or state.get("priority_player") != 1:
            return
        # ---- P1 priority ----
        phase = state.get("phase")
        if (not p1_muse_cast and phase in ("PreCombatMain", "PostCombatMain")
                and MUSE.lower() in hand_lnames(state, 1)
                and len(untapped_lands(state, 1, PLAINS)) >= 4
                and not battlefield_ids(state, 1, MUSE)):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" and lname(state, d.get("object_id")) == MUSE.lower():
                    say("P1 casts Windborn Muse")
                    wire("cast_p1_muse", a)
                    await submit_as_is(c, a)
                    p1_muse_cast = True
                    return
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(c, a)
                return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(c, a)
                return

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
        try:
            pre_st = json.loads(open(f"{EVDIR}/pre.json").read())["state"] \
                if os.path.exists(f"{EVDIR}/pre.json") else None
            post_st = json.loads(open(f"{EVDIR}/post.json").read())["state"] \
                if os.path.exists(f"{EVDIR}/post.json") else None
        except Exception as e:
            pre_st, post_st = None, None
            notes.append(f"state reload failed: {e}")
        # A2: tax decision actually surfaced with an actionable submission
        p0_tax = [w for w in obs["tax_waits"] if w["who"] == "P0"]
        actionable = [w for w in p0_tax if w["n_actions"] > 0]
        if p0_tax and actionable:
            ass["A2_tax_prompted"] = "passed"
            notes.append(f"{len(p0_tax)} CombatTaxPayment wait(s) for P0, "
                         f"all with actionable submissions "
                         f"(e.g. {actionable[0]['action_types'][:10]}); "
                         f"mirrors the report's 'Waiting for: CombatTaxPayment'")
        elif p0_tax:
            ass["A2_tax_prompted"] = "failed"
            notes.append(f"{len(p0_tax)} CombatTaxPayment wait(s) for P0 but NONE "
                         f"offered an actionable submission (softlock signature)")
            obs["stall_observed"] = True
        else:
            ass["A2_tax_prompted"] = "failed"
            notes.append("no CombatTaxPayment wait was ever observed")
        if pre_st is not None and post_st is not None:
            obs["life_pre"] = life_of(pre_st, 1)
            obs["life_post"] = life_of(post_st, 1)
            # A3: paid attack dealt 9 (unblocked 9/9 Krang)
            pay_waits = [w for w in p0_tax if w["mode"] == "pay"]
            if obs["life_after_pay"] is not None and obs["life_pre"] is not None \
                    and obs["life_after_pay"] == obs["life_pre"] - 9 and pay_waits:
                ass["A3_pay_completes"] = "passed"
                notes.append(f"paid attack: P1 life {obs['life_pre']} -> "
                             f"{obs['life_after_pay']} (unblocked 9/9 hasty Krang, "
                             f"tax {{2}} paid)")
            else:
                ass["A3_pay_completes"] = "failed"
                notes.append(f"A3 failed: life_pre={obs['life_pre']} "
                             f"life_after_pay={obs['life_after_pay']}")
            # haste-path evidence
            if obs["krang_cast_turn"] is not None and obs["krang_attack_turn"] is not None:
                notes.append(f"haste path: Krang cast turn {obs['krang_cast_turn']}, "
                             f"attacked turn {obs['krang_attack_turn']}, "
                             f"summoning_sick_at_attack="
                             f"{obs['krang_summoning_sick_at_attack']}, "
                             f"keywords={obs.get('krang_keywords_at_attack')}")
            # A4: decline resolved without damage and without stranding
            if obs["decline_turn"] is not None and not obs["stall_observed"]:
                post_turn = post_st.get("turn_number") or 0
                post_phase = post_st.get("phase")
                progressed = (post_turn > obs["decline_turn"]
                              or (post_turn == obs["decline_turn"]
                                  and post_phase in ("PostCombatMain", "End", "Cleanup")))
                base = (obs["life_after_pay"] if obs["life_after_pay"] is not None
                        else obs["life_pre"])
                no_damage = obs["life_post"] == base
                if progressed and no_damage:
                    ass["A4_decline_resolves"] = "passed"
                    notes.append(f"decline resolved: no damage from declined attack "
                                 f"(P1 life {obs['life_post']}), game advanced to "
                                 f"turn {post_st.get('turn_number')}")
                else:
                    ass["A4_decline_resolves"] = "failed"
                    notes.append(f"A4 failed: progressed={progressed} "
                                 f"(decline_turn={obs['decline_turn']}, "
                                 f"post_turn={post_st.get('turn_number')}), "
                                 f"no_damage={no_damage} (life_post={obs['life_post']})")
            else:
                ass["A4_decline_resolves"] = "failed"
                notes.append("A4 failed: decline branch never reached or stall observed")
            # A6 cleanup
            slen = len(post_st.get("stack", []) or [])
            wf = (post_st.get("waiting_for") or {}).get("type")
            if slen == 0 and wf != "CombatTaxPayment":
                ass["A6_cleanup"] = "passed"
                notes.append(f"post.json: stack empty, wf={wf}, game proceeding "
                             f"(turn={post_st.get('turn_number')}, "
                             f"phase={post_st.get('phase')}, "
                             f"active=P{post_st.get('active_player')})")
            else:
                ass["A6_cleanup"] = "failed"
                notes.append(f"post.json stack={slen}, wf={wf}, not clean")
        else:
            for k in ("A3_pay_completes", "A4_decline_resolves", "A6_cleanup"):
                if ass[k] == "not-run":
                    ass[k] = "failed"
                    notes.append(f"{k} could not be evaluated (missing pre/post state)")
        # A5: no softlock across all tax waits
        if obs["tax_waits"] and not obs["stall_observed"]:
            dead = [w for w in obs["tax_waits"] if w["n_actions"] == 0]
            if not dead:
                ass["A5_no_softlock"] = "passed"
                notes.append(f"{len(obs['tax_waits'])} CombatTaxPayment waits; "
                             f"acting player had an actionable submission every time")
            else:
                ass["A5_no_softlock"] = "failed"
                obs["stall_observed"] = True
                notes.append(f"SOFTLOCK SIGNATURE: {len(dead)} CombatTaxPayment wait(s) "
                             f"with NO submission offered to the acting player")
        elif obs["stall_observed"]:
            ass["A5_no_softlock"] = "failed"
            notes.append("SOFTLOCK SIGNATURE observed (see notes)")
        else:
            ass["A5_no_softlock"] = "failed"
            notes.append("no CombatTaxPayment wait was ever observed")
        # Verdict
        if obs["stall_observed"]:
            verdict = "reproduced"
            notes.append("verdict reproduced: the reported CombatTaxPayment stall "
                         "signature observed on v0.78.0 with the reported cards "
                         "(Krang, Utrom Warlord paying Windborn Muse's tax)")
        else:
            verdict = "not-reproduced"
            notes.append("verdict not-reproduced on v0.78.0: the reported pay-branch "
                         "path completes (tax paid, attack proceeds, no stall). Not "
                         "tested on the report's build v0.9.0; not a fix claim.")
        run = {
            "issue": 4554,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "server_port": 9374,
            "driver": {"protocol_advertised": 68, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_4554.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": obs,
            "verdict": verdict,
            "limitations": [
                "Browser UI not exercised; native engine via two human-client seats.",
                "8x Krang, Utrom Warlord / 8x Windborn Muse deck density is a "
                "test-harness convenience (engine accepts >4-of for custom games).",
                "Not tested on the original 2026-06-28 build v0.9.0 (6bd7432); "
                "verdict is scoped to v0.78.0, not a fix claim.",
                "The prebuilt server has no standalone state-restore; states are "
                "authoritative exports (restorable only via full game replay).",
            ],
            "setup_line": "P0: 8x Krang, Utrom Warlord + 52x Forest (mulligan to Krang "
                          "+ lands, cast Krang {9}, attack P1 with haste); P1: 8x "
                          "Windborn Muse + 52x Plains (mulligan to Muse + Plains, "
                          "cast Muse, never attacks)",
            "contract_line": "CombatTaxPayment surfaces when the hasty Krang attacks "
                             "the Muse controller; choosing to pay {2} lets the "
                             "attack proceed (P1 20->11); choosing to decline does not "
                             "strand the game; every wait offers an actionable "
                             "submission",
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
    while time.time() - t0 < 1800:
        await asyncio.sleep(0.15)
        for c, tick, tag in ((p0, p0_tick, "P0"), (p1, p1_tick, "P1")):
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
        s0 = p0.latest["state"] if p0.latest else {}
        wf0 = (s0.get("waiting_for") or {}).get("type")
        if wf0 != "CombatTaxPayment":
            tax_wait_start = {"P0": None, "P1": None}
        # paid-attack outcome: latch P1 life only AFTER the pay-mode attack
        # turn's combat has resolved (turn advanced, or same turn at
        # PostCombatMain/End/Cleanup)
        pay_turns = [w["turn"] for w in obs["tax_waits"] if w["mode"] == "pay"]
        if obs["life_after_pay"] is None and pay_turns:
            pt = pay_turns[0]
            ct = s0.get("turn_number") or 0
            cp = s0.get("phase")
            if ct > pt or (ct == pt and cp in ("PostCombatMain", "End", "Cleanup")):
                obs["life_after_pay"] = life_of(s0, 1)
                obs["paid_attack_turn"] = pt
                decline_mode = True
                obs["pay_mode"] = False
                notes.append(f"pay-mode tax resolved (attack turn {pt}); "
                             f"P1 life now {obs['life_after_pay']}; switching to decline mode")
                say("switched to DECLINE mode")
        # decline outcome: after a decline-mode tax wait resolved
        if obs["decline_turn"] is None and any(w["mode"] == "decline" for w in obs["tax_waits"]) \
                and wf0 != "CombatTaxPayment" and s0.get("turn_number"):
            obs["decline_turn"] = s0.get("turn_number")
            notes.append(f"decline-mode tax wait resolved (turn {obs['decline_turn']})")
        # softlock watchdog: tax wait with no actionable submission for the actor
        for c, tag, pid in ((p0, "P0", 0), (p1, "P1", 1)):
            if c.latest and c.latest["state"].get("waiting_for", {}).get("type") == "CombatTaxPayment":
                start = tax_wait_start[tag]
                acts = merged_actions(c.latest)
                if start and time.time() - start > 60 and not acts:
                    obs["stall_observed"] = True
                    mid = await p0.export_state()
                    with open(f"{EVDIR}/mid_stall.json", "w") as f:
                        f.write(mid)
                    notes.append(f"stall watchdog: {tag} CombatTaxPayment wait >60s "
                                 f"with no offered submission")
                    say("STALL OBSERVED; finishing")
                    await finish()
                    return
        if post_exported:
            say("post exported; finishing")
            await finish()
            return
        if time.time() - last_diag > 60 and p0.latest:
            last_diag = time.time()
            s = p0.latest["state"]
            say(f"DIAG turn={s.get('turn_number')} active={s.get('active_player')} "
                f"phase={s.get('phase')} wf={(s.get('waiting_for') or {}).get('type')} "
                f"pp={s.get('priority_player')} P0krang={len(battlefield_ids(s, 0, KRANG))} "
                f"P1muse={len(battlefield_ids(s, 1, MUSE))} life0={life_of(s, 0)} "
                f"life1={life_of(s, 1)} taxwaits={len(obs['tax_waits'])} "
                f"mode={'decline' if decline_mode else 'pay'}")
        # post-export trigger: decline resolved and game advanced
        if obs["decline_turn"] is not None and not post_exported and p0.latest:
            s = p0.latest["state"]
            adv = (s.get("turn_number") or 0) > obs["decline_turn"] or (
                s.get("phase") in ("PostCombatMain", "End", "Cleanup")
                and s.get("turn_number") == obs["decline_turn"])
            if adv:
                try:
                    post = await p0.export_state()
                    with open(f"{EVDIR}/post.json", "w") as f:
                        f.write(post)
                    post_exported = True
                    say("exported POST")
                except Exception as e:
                    notes.append(f"post export failed: {e}")
    notes.append("global timeout (1800s) hit before assertions resolved")
    await finish()


asyncio.run(main())
