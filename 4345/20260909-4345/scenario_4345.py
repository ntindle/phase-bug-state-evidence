#!/usr/bin/env python3
"""Issue #4345: Stuck decision: CombatTaxPayment.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (github issue, build v0.6.0 399601b, 2026-06-26):
  "What happened" - EMPTY (no description at all).
  Diagnostic: Waiting for: CombatTaxPayment | Stuck players: 0
  Maintainer comment (2026-07-19, mike-theDude): asked for the attackers, the
  card imposing the combat tax, available mana, and whether the stall occurred
  before choosing to pay / while paying / after declining; a saved game state
  would make it actionable. NO reply from the reporter. No attachment.

There is NO testable premise: no board state, no tax card, no mana situation,
no indication of which branch (pay vs decline) stalled. Per PLAYBOOK step 2, a
native engine test would require inventing the premise. This run is therefore
a blocked ATTEMPT: drive a fresh native-engine game through the
CombatTaxPayment decision - once paying the tax (Ghostly Prison {2} per
attacker), once declining to pay - and record whether the decision ever leaves
the acting player with no available submission (the reported softlock
signature: Waiting for: CombatTaxPayment with Stuck players: 0).

Oracle/expected (generic engine behavior):
  E1: attacking a player who controls Ghostly Prison opens a CombatTaxPayment
      decision for the attacking player.
  E2: paying {2} per attacker lets the attack proceed (unblocked 2/2 -> P1 18).
  E3: declining to pay does not strand the game on CombatTaxPayment; the game
      proceeds (attack not made or removed, no orphaned decision).
  E4: every CombatTaxPayment wait offers the acting player an actionable
      submission.

Assertions:
  A1_setup_ok        P1's Ghostly Prison on BF; P0 Bear attack-ready;
                     pre.json exported at the first taxed DeclareAttackers.
  A2_tax_prompted    >=1 CombatTaxPayment wait observed, acting player = P0,
                     with >=1 actionable submission offered.
  A3_pay_completes   paid {2} (2 Forests tapped via PayMana flow); P1 life
                     20 -> 18 from the unblocked Bear; game left the decision.
  A4_decline_resolves on the second attack P0 declines; no damage from that
                     attack and the game advanced past CombatTaxPayment.
  A5_no_softlock     no CombatTaxPayment wait lacked an actionable submission;
                     the 60s watchdog never fired.
  A6_cleanup         post.json: stack empty, game proceeding.

Verdict rule: the reported bug has no testable premise (missing setup
information that materially changes the test), so the verdict is `blocked`
regardless of the generic attempt outcome. The generic attempt only becomes
`reproduced` if the softlock signature itself is observed (a CombatTaxPayment
wait with no submission available to the acting player, or the decision
orphaned after decline); it can never be `not-reproduced` because the reported
path was never identified.

Evidence: evidence/4345/<run-id>/pre.json (first taxed DeclareAttackers),
mid_tax.json (first CombatTaxPayment wait), post.json (after both combats),
run.json, manifest.sha256, summary.png, scenario_4345.py, wire_log.jsonl,
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
client.URL = "ws://localhost:9375/ws"
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260909-4345"
EVDIR = f"{BACKFILL}/evidence/4345/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

BEAR = "Grizzly Bears"
FOREST = "Forest"
PLAINS = "Plains"
PRISON = "Ghostly Prison"

P0_DECK = [(BEAR, 12), (FOREST, 48)]
P1_DECK = [(PRISON, 8), (PLAINS, 52)]

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
              "fresh isolated server on 127.0.0.1:9375 for run 20260909-4345",
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


def can_attack_now(state, oid):
    o = get_obj(state, oid)
    if o.get("tapped"):
        return False
    if o.get("summoning_sick"):
        kws = o.get("keywords") or []
        if "Haste" not in [str(k) for k in kws]:
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
    p0_bears_cast = 0
    p1_prison_cast = False
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
        "pay_mode": True,
    }
    tax_wait_start = {"P0": None, "P1": None}
    declare_wait_start = {"P0": None, "P1": None}

    async def mulligan_tick(c, pid, acts, state, tag, want):
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get(tag):
            hn = hand_lnames(state, pid)
            want_n = sum(1 for n in hn if n == want.lower())
            lands = sum(1 for n in hn if n in (FOREST.lower(), PLAINS.lower()))
            mulls = kept.get(f"{tag}_mulls", 0)
            if (want_n >= 1 and lands >= 2) or mulls >= 3:
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
                    return 0 if nm in (FOREST.lower(), PLAINS.lower()) else (2 if nm == want.lower() else 1)
                picks = sorted(hand_ids, key=bottom_key)[:count]
                kept[f"{tag}_bottomed"] = True
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": [int(x) for x in picks]}})
                say(f"{tag} bottoms {count}: {[lname(state, x) for x in picks]}")
                return True
        return False

    async def handle_tax(c, pid, tag, acts, state):
        """Record + act on a CombatTaxPayment wait for the acting player."""
        nonlocal mid_tax_exported, decline_mode
        wf = state.get("waiting_for") or {}
        vi = (state.get("viewer_interaction") or {})
        mode = "decline" if decline_mode else "pay"
        rec = {
            "turn": state.get("turn_number"),
            "phase": state.get("phase"),
            "who": tag,
            "wf_type": wf.get("type"),
            "wf_data_keys": list((wf.get("data") or {}).keys()),
            "action_types": sorted({a["type"] for a in acts}),
            "n_actions": len(acts),
            "vi_present": bool(vi),
            "vi_type": (vi.get("opportunity") or {}).get("type") if isinstance(vi, dict) else None,
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
        # avoid duplicate submissions on stale re-ticks; allow retry after 30s
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
            # PAY mode: after accept=true the engine drives mana payment
            # through the normal PayMana flow (auto-handled by the tick).
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
        # DECLINE fallback
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
        st_latest = c.latest
        state = st_latest["state"]
        acts = merged_actions(st_latest)
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
        nonlocal pre_exported, p0_bears_cast, post_exported, decline_mode, decline_attack_done
        pid, tag, c = 0, "P0", p0
        wtype = (state.get("waiting_for") or {}).get("type")
        if await mulligan_tick(c, pid, acts, state, tag, BEAR):
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
            prison = battlefield_ids(state, 1, PRISON)
            ready = [o for o in battlefield_ids(state, 0, BEAR) if can_attack_now(state, o)]
            if prison and ready and not pre_exported:
                say("P0 DeclareAttackers vs Ghostly Prison with attack-ready Bear; exporting PRE")
                pre = await c.export_state()
                with open(f"{EVDIR}/pre.json", "w") as f:
                    f.write(pre)
                pre_st = json.loads(pre)["state"]
                obs["life_pre"] = life_of(pre_st, 1)
                ass["A1_setup_ok"] = "passed"
                notes.append(f"pre: P1 prison={len(prison)}, P0 ready bears={len(ready)}, "
                             f"P1 life={obs['life_pre']}")
                pre_exported = True
            if prison and ready:
                # exactly one attacker keeps the tax at {2}; only attack with
                # >=2 untapped Forests so the engine offers both accept=true
                # (pay) and accept=false (decline). In decline mode attack only
                # once, then declare empty so the game advances.
                mana_ok = len(untapped_lands(state, 0, FOREST)) >= 2
                if decline_mode and decline_attack_done:
                    await declare_attackers(c, pid, tag, [])
                    say("P0 declares no attackers (decline test complete)")
                elif mana_ok:
                    await declare_attackers(c, pid, tag, ready[:1])
                    if decline_mode:
                        decline_attack_done = True
                else:
                    await declare_attackers(c, pid, tag, [])
                    say(f"P0 holds attack: only "
                        f"{len(untapped_lands(state, 0, FOREST))} untapped Forests")
            else:
                await declare_attackers(c, pid, tag, [])
                if not prison:
                    say("P0 holds attack: Prison not yet on P1 BF")
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
        if (phase in ("PreCombatMain", "PostCombatMain")
                and BEAR.lower() in hand_lnames(state, 0)
                and len(untapped_lands(state, 0, FOREST)) >= 2
                and p0_bears_cast < 2):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" and lname(state, d.get("object_id")) == BEAR.lower():
                    say("P0 casts Grizzly Bears")
                    wire("cast_p0_bear", a)
                    await submit_as_is(c, a)
                    p0_bears_cast += 1
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
        nonlocal p1_prison_cast, post_exported
        pid, tag, c = 1, "P1", p1
        wtype = (state.get("waiting_for") or {}).get("type")
        if await mulligan_tick(c, pid, acts, state, tag, PRISON):
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
            # P1 is never the acting player (waiting_for.data.player is the
            # attacker); the decision is visible to both seats but only P0
            # has submissions. Stay passive.
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
        if (not p1_prison_cast and phase in ("PreCombatMain", "PostCombatMain")
                and PRISON.lower() in hand_lnames(state, 1)
                and len(untapped_lands(state, 1, PLAINS)) >= 3):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" and lname(state, d.get("object_id")) == PRISON.lower():
                    say("P1 casts Ghostly Prison")
                    wire("cast_p1_prison", a)
                    await submit_as_is(c, a)
                    p1_prison_cast = True
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
                         f"(e.g. {actionable[0]['action_types'][:8]})")
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
            # A3: paid attack dealt 2
            pay_waits = [w for w in p0_tax if w["mode"] == "pay"]
            if obs["life_after_pay"] is not None and obs["life_pre"] is not None \
                    and obs["life_after_pay"] == obs["life_pre"] - 2 and pay_waits:
                ass["A3_pay_completes"] = "passed"
                notes.append(f"paid attack: P1 life {obs['life_pre']} -> "
                             f"{obs['life_after_pay']} (unblocked 2/2, tax {{2}} paid)")
            else:
                ass["A3_pay_completes"] = "failed"
                notes.append(f"A3 failed: life_pre={obs['life_pre']} "
                             f"life_after_pay={obs['life_after_pay']}")
            # A4: decline resolved without damage and without stranding
            if obs["decline_turn"] is not None and not obs["stall_observed"]:
                post_turn = post_st.get("turn_number") or 0
                post_phase = post_st.get("phase")
                progressed = (post_turn > obs["decline_turn"]
                              or (post_turn == obs["decline_turn"]
                                  and post_phase in ("PostCombatMain", "End", "Cleanup")))
                no_damage = obs["life_post"] == (obs["life_after_pay"]
                                                 if obs["life_after_pay"] is not None
                                                 else obs["life_pre"])
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
            notes.append("verdict reproduced: CombatTaxPayment softlock signature "
                         "observed on v0.78.0 (a related failure, not the reporter's "
                         "v0.6.0 board)")
        else:
            verdict = "blocked"
            notes.append("verdict blocked: report has no testable premise "
                         "(no board, tax card, mana situation, or branch; reporter "
                         "never answered the maintainer's 2026-07-19 questions). "
                         "Generic CombatTaxPayment path on v0.78.0 exercised.")
        run = {
            "issue": 4345,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "server_port": 9375,
            "driver": {"protocol_advertised": 68, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_4345.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": obs,
            "verdict": verdict,
            "limitations": [
                "Browser UI not exercised; native engine via two human-client seats.",
                "12x Grizzly Bears / 8x Ghostly Prison deck density is a test-harness "
                "convenience (engine accepts >4-of for custom games).",
                "The report names no tax card; Ghostly Prison ({2} per attacker) is "
                "the driver's chosen representative.",
                "Not tested on the original 2026-06-26 build v0.6.0; verdict is scoped "
                "to v0.78.0, not a fix claim.",
                "The prebuilt server has no standalone state-restore; states are "
                "authoritative exports (restorable only via full game replay).",
            ],
            "setup_line": "P0: 12x Grizzly Bears + 48x Forest (mulligan to Bear+2 lands, "
                          "cast Bears, attack P1); P1: 8x Ghostly Prison + 52x Plains "
                          "(mulligan to Prison+3 Plains, cast Prison, never attacks)",
            "contract_line": "CombatTaxPayment surfaces when attacking a Prison "
                             "controller; paying {2} lets the attack proceed; declining "
                             "does not strand the game; every wait offers an actionable "
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
    while time.time() - t0 < 1500:
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
        # detect transitions out of tax waits to record outcomes
        s0 = p0.latest["state"] if p0.latest else {}
        wf0 = (s0.get("waiting_for") or {}).get("type")
        if wf0 != "CombatTaxPayment":
            tax_wait_start = {"P0": None, "P1": None}
        # paid-attack outcome: latch P1 life only AFTER combat damage for the
        # pay-mode attack turn has resolved (turn advanced, or same turn at
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
                f"pp={s.get('priority_player')} P0bears={len(battlefield_ids(s, 0, BEAR))} "
                f"P1prison={len(battlefield_ids(s, 1, PRISON))} life0={life_of(s, 0)} "
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
    notes.append("global timeout (1500s) hit before assertions resolved")
    await finish()


asyncio.run(main())
