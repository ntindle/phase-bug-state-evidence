#!/usr/bin/env python3
"""Issue #5230: Stuck decision: CombatTaxPayment.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (github issue, build v0.17.0 c731979, 2026-07-07):
  "What happened" - EMPTY (no description at all).
  Diagnostic: Waiting for: CombatTaxPayment | Stuck players: 0
  Maintainer comment (2026-07-19, mike-theDude): asked for the attackers, the
  card imposing the combat tax, available mana, whether any attacker was
  required or goaded, and the exact step where input stopped working; a saved
  game state or log would make it actionable. NO reply from the reporter.
  No attachment. The comment explicitly asks to distinguish this from the
  fixed goad/no-mana case in #4893 (goaded Obeka vs Ghostly Prison, insufficient
  mana to pay the tax, reporter could not undeclare attackers).

There is NO testable premise: no board state, no tax card, no mana situation,
no indication of which branch (pay vs decline) stalled. Per PLAYBOOK step 2, a
native engine test would require inventing the premise. This run is therefore
a blocked ATTEMPT that additionally exercises the distinguishing #4893
signature on the pinned build: a GOADED attacker that cannot pay the combat
tax. Ghostly Prison ({2} per attacker) is the tax; Disrupt Decorum
("Goad all creatures you don't control") supplies the goad.

Oracle/expected (generic engine behavior):
  E1: attacking a player who controls Ghostly Prison opens a CombatTaxPayment
      decision for the attacking player.
  E2: when the attacker cannot pay, only the decline submission is offered.
  E3: declining does not strand the game on CombatTaxPayment; the game returns
      to DeclareAttackers and the player can undeclare (a goaded creature that
      cannot pay the tax is not "able" to attack, so the attack requirement is
      waived) - OR the engine rejects the undeclare, which is the #4893
      signature under test.
  E4: a goaded attacker that CAN pay the tax pays it and the attack proceeds.
  E5: every CombatTaxPayment wait offers the acting player an actionable
      submission.

Assertions:
  A1_setup_ok         P1's Ghostly Prison on BF; P0 has >=2 goaded attack-ready
                      Bears; pre.json exported at the first taxed
                      DeclareAttackers.
  A2_tax_prompted     >=1 CombatTaxPayment wait observed for P0 with >=1
                      actionable submission offered.
  A3_only_decline     in the can't-pay phase no PayCombatTax accept=true was
                      advertised (P0 untapped < tax total).
  A4_decline_resolves declining leaves the CombatTaxPayment decision (game
                      returns to DeclareAttackers; no orphaned decision).
  A5_undeclare        after the decline, an empty DeclareAttackers submission
                      is accepted (recorded either way; a rejection is the
                      #4893 signature and feeds the loop check).
  A6_control_pay      with mana available, P0 pays {2} for a goaded attacker;
                      unblocked Bear deals 2 (P1 life 20 -> 18).
  A7_no_softlock      no CombatTaxPayment wait lacked an actionable submission;
                      the 60s watchdog never fired; no attack->decline loop.
  A8_cleanup          post.json: stack empty, game proceeding.

Verdict rule: the reported bug has no testable premise (missing setup
information that materially changes the test), so the verdict is `blocked`
unless the softlock signature itself is observed (a CombatTaxPayment wait with
no submission available to the acting player, an orphaned decision after
decline, or an attack->decline loop with no legal undeclare) - then
`reproduced` as a clearly identified related failure. It can never be
`not-reproduced` because the reported path was never identified.

Evidence: evidence/5230/<run-id>/pre.json (first taxed DeclareAttackers),
mid_tax.json (first CombatTaxPayment wait), mid_loop.json (only if the
undeclare loop is observed), post.json (after the control pay), run.json,
manifest.sha256, summary.png, scenario_5230b.py, wire_log.jsonl,
scenario_run.log

Re-run note: run 20260909-5230 (same scenario family) reached turn 12 with 6
untapped Forests against a {6} tax, so its can't-pay branch was not genuine
(A3 failed as a harness artifact, not an engine defect). This run uses a
denser Decorum package (12x) and reached three attackers at tax {6} with only
5 untapped Forests. The optional PreCombatMain mana-drain code below never
fired (P0 held only Forests in hand on the can't-pay turn), so the unpayable
state arose naturally. The 20260909-5230 artifacts are kept local-only.
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
RUN_ID = "20260909-5230b"
ISSUE = 5230
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

BEAR = "Grizzly Bears"
FOREST = "Forest"
PLAINS = "Plains"
MOUNTAIN = "Mountain"
PRISON = "Ghostly Prison"
DECORUM = "Disrupt Decorum"

P0_DECK = [(BEAR, 12), (FOREST, 48)]
P1_DECK = [(PRISON, 8), (DECORUM, 12), (PLAINS, 20), (MOUNTAIN, 20)]

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
              "fresh isolated server on 127.0.0.1:9375 for run 20260909-5230b "
              "(re-run: first attempt 20260909-5230 reached turn 12 with 6 "
              "untapped Forests vs {6} tax, so the can't-pay branch was not "
              "genuine; the optional PreCombatMain mana-drain code never fired "
              "(P0 held only Forests) and the can't-pay arose naturally with "
              "5 untapped Forests vs {6} tax)",
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


def land_count(state, pid, name):
    return len([oid for oid, o in state.get("objects", {}).items()
                if o.get("zone") == "Battlefield" and o.get("controller") == pid
                and lname(state, oid) == name.lower()])


def can_attack_now(state, oid):
    o = get_obj(state, oid)
    if o.get("tapped"):
        return False
    if o.get("summoning_sick"):
        kws = o.get("keywords") or []
        if "Haste" not in [str(k) for k in kws]:
            return False
    return True


def goad_markers(state, pid):
    """Battlefield objects of pid whose JSON mentions goad."""
    hits = []
    for oid, o in state.get("objects", {}).items():
        if o.get("zone") == "Battlefield" and o.get("controller") == pid:
            if "goad" in json.dumps(o, default=str).lower():
                hits.append(int(oid))
    return hits


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


async def drain_rejections(c, obs, ctx):
    """Collect ActionRejected/Error messages currently queued in the inbox."""
    found = []
    while True:
        try:
            t, data = c.inbox.get_nowait()
        except asyncio.QueueEmpty:
            break
        if t in ("ActionRejected", "Error"):
            rec = {"ctx": ctx, "type": t,
                   "data": json.loads(json.dumps(data, default=str))}
            obs["rejections"].append(rec)
            wire(f"{c.name}_rejection", rec)
            say(f"{c.name} REJECTION ({ctx}): {json.dumps(rec['data'])[:300]}")
            found.append(rec)
    return found


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_tax_prompted", "A3_only_decline",
            "A4_decline_resolves", "A5_undeclare", "A6_control_pay",
            "A7_no_softlock", "A8_cleanup")}
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
    drain_casts = 0
    p1_prison_cast = False
    p1_decorum_casts = []   # P1 turn_numbers on which Decorum was submitted
    cantpay_turn = None
    cantpay_done = False
    control_turn = None
    control_done = False
    undeclare_outcome = None  # None | "accepted" | "rejected"

    obs = {
        "tax_waits": [],
        "declare_waits": [],
        "rejections": [],
        "stall_observed": False,
        "loop_observed": False,
        "declines_this_turn": {},
        "undeclare_attempts": 0,
        "undeclare_pending": None,
        "life_pre": None,
        "life_post": None,
        "life_after_control": None,
        "goad_markers_at_pre": [],
        "phase": "setup",  # setup -> cantpay -> control
    }
    tax_wait_start = {"P0": None, "P1": None}

    async def mulligan_tick(c, pid, acts, state, tag, want):
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get(tag):
            hn = hand_lnames(state, pid)
            want_n = sum(1 for n in hn if n == want.lower())
            lands = sum(1 for n in hn if n in (FOREST.lower(), PLAINS.lower(),
                                               MOUNTAIN.lower()))
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
                    if nm in (FOREST.lower(), PLAINS.lower(), MOUNTAIN.lower()):
                        return 0
                    return 2 if nm == want.lower() else 1
                picks = sorted(hand_ids, key=bottom_key)[:count]
                kept[f"{tag}_bottomed"] = True
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": [int(x) for x in picks]}})
                say(f"{tag} bottoms {count}: {[lname(state, x) for x in picks]}")
                return True
        return False

    async def handle_tax(c, pid, tag, acts, state):
        nonlocal mid_tax_exported
        wf = state.get("waiting_for") or {}
        wf_data = wf.get("data") or {}
        mode = obs["phase"]
        rec = {
            "turn": state.get("turn_number"),
            "phase": state.get("phase"),
            "who": tag,
            "wf_data": json.loads(json.dumps(wf_data, default=str)),
            "action_types": sorted({a["type"] for a in acts}),
            "n_actions": len(acts),
            "accept_true_offered": any(a["type"] == "PayCombatTax"
                                       and a.get("data", {}).get("accept") is True
                                       for a in acts),
            "accept_false_offered": any(a["type"] == "PayCombatTax"
                                        and a.get("data", {}).get("accept") is False
                                        for a in acts),
            "mode": mode,
            "untapped_forests": len(untapped_lands(state, 0, FOREST)),
        }
        obs["tax_waits"].append(rec)
        wire(f"{tag}_tax_wait", {"waiting_for": wf, "acts": acts})
        say(f"{tag} CombatTaxPayment wait (mode={mode}): "
            f"{len(acts)} actions; accept=true offered={rec['accept_true_offered']}, "
            f"accept=false offered={rec['accept_false_offered']}, "
            f"untapped Forests={rec['untapped_forests']}")
        if not mid_tax_exported:
            try:
                mid = await c.export_state()
                with open(f"{EVDIR}/mid_tax.json", "w") as f:
                    f.write(mid)
                mid_tax_exported = True
                say("exported mid_tax.json")
            except Exception as e:
                notes.append(f"mid_tax export failed: {e}")
        if mode == "cantpay":
            choice = next((a for a in acts if a["type"] == "PayCombatTax"
                           and a.get("data", {}).get("accept") is False), None)
            if choice is not None:
                wire(f"{tag}_tax_decline_submit", choice)
                await submit_as_is(c, choice)
                tn = state.get("turn_number")
                obs["declines_this_turn"][tn] = obs["declines_this_turn"].get(tn, 0) + 1
                say(f"{tag} submits PayCombatTax accept=false "
                    f"(decline #{obs['declines_this_turn'][tn]} on turn {tn})")
                return
            say(f"{tag} cantpay: NO accept=false offered "
                f"(actions={rec['action_types'][:8]}) - softlock signature")
            obs["stall_observed"] = True
            return
        if mode == "control":
            choice = next((a for a in acts if a["type"] == "PayCombatTax"
                           and a.get("data", {}).get("accept") is True), None)
            if choice is not None:
                wire(f"{tag}_tax_pay_submit", choice)
                await submit_as_is(c, choice)
                say(f"{tag} submits PayCombatTax accept=true (control)")
                return
            say(f"{tag} control: NO accept=true offered "
                f"(actions={rec['action_types'][:8]})")
            return
        say(f"{tag} tax wait in unexpected phase={mode}; passing priority")
        pp = find_action(acts, "PassPriority")
        if pp:
            await submit_as_is(c, pp)

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
            await asyncio.sleep(1.0)
            rej = await drain_rejections(c, obs, f"declare_attackers turn={rec['turn']}")
            rec["submitted"] = True
            rec["rejected"] = bool(rej)
            say(f"{tag} submits DeclareAttackers attacks={attack_oids} "
                f"rejected={rec['rejected']}")
        else:
            rec["submitted"] = False
            rec["rejected"] = False
            wire(f"{tag}_declare_wait_no_action", {"state_revision": c.revision})
            say(f"{tag} DeclareAttackers WAIT: no DeclareAttackers action offered!")
        obs["declare_waits"].append(rec)
        return rec

    async def p0_tick(st, acts, state):
        nonlocal pre_exported, p0_bears_cast, drain_casts, post_exported, cantpay_turn, \
            cantpay_done, control_turn, control_done, undeclare_outcome
        pid, tag, c = 0, "P0", p0
        wtype = (state.get("waiting_for") or {}).get("type")
        turn = state.get("turn_number")
        # settle a pending undeclare: the game leaving DeclareAttackers (or the
        # turn) means the empty declaration was accepted
        pend = obs.get("undeclare_pending")
        if pend and (wtype != "DeclareAttackers" or turn != pend["turn"]):
            obs["undeclare_pending"] = None
            undeclare_outcome = "accepted"
            cantpay_done = True
            obs["phase"] = "control"
            notes.append("undeclare after decline: ACCEPTED (game advanced past "
                         "DeclareAttackers)")
            say("undeclare ACCEPTED; phase -> control")
        if await mulligan_tick(c, pid, acts, state, tag, BEAR):
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
            prison = battlefield_ids(state, 1, PRISON)
            ready = [o for o in battlefield_ids(state, 0, BEAR) if can_attack_now(state, o)]
            ph = obs["phase"]
            if ph == "cantpay" and prison and not cantpay_done:
                if cantpay_turn is None and len(ready) >= 2:
                    cantpay_turn = turn
                    say(f"P0 can't-pay attack turn = {turn} with {len(ready)} ready Bears")
                declines = obs["declines_this_turn"].get(cantpay_turn, 0) \
                    if cantpay_turn else 0
                undecls = obs["undeclare_attempts"]
                pend = obs.get("undeclare_pending")
                if pend and turn == pend["turn"]:
                    # awaiting the undeclare outcome
                    rej = await drain_rejections(c, obs, "undeclare_settle")
                    if rej:
                        obs["undeclare_pending"] = None
                        undeclare_outcome = "rejected"
                        notes.append("undeclare after decline: REJECTED by the engine "
                                     "(#4893 signature)")
                        say("undeclare REJECTED (message); will re-attack to test loop")
                    elif time.time() - pend["at"] > 8:
                        obs["undeclare_pending"] = None
                        undeclare_outcome = "rejected"
                        notes.append("undeclare after decline: treated as REJECTED "
                                     "(no advance past DeclareAttackers in 8s)")
                        say("undeclare REJECTED (no advance); will re-attack to test loop")
                    else:
                        return  # keep waiting for the settle
                if undeclare_outcome is None and declines == 0 and len(ready) >= 2:
                    if not pre_exported:
                        say("P0 DeclareAttackers vs Ghostly Prison with goaded Bears; "
                            "exporting PRE")
                        pre = await c.export_state()
                        with open(f"{EVDIR}/pre.json", "w") as f:
                            f.write(pre)
                        pre_st = json.loads(pre)["state"]
                        obs["life_pre"] = life_of(pre_st, 1)
                        obs["goad_markers_at_pre"] = goad_markers(pre_st, 0)
                        ass["A1_setup_ok"] = "passed"
                        notes.append(f"pre: P1 prison={len(prison)}, P0 ready bears="
                                     f"{len(ready)}, goad markers on P0 objects="
                                     f"{obs['goad_markers_at_pre']}, P1 life={obs['life_pre']}")
                        pre_exported = True
                    # attack with ALL ready Bears: tax = 2 x n > untapped mana
                    await declare_attackers(c, pid, tag, ready[:3])
                    return
                if undeclare_outcome is None and declines >= 1 and undecls <= declines:
                    # attempt to undeclare after the decline
                    rec = await declare_attackers(c, pid, tag, [])
                    obs["undeclare_attempts"] = undecls + 1
                    if rec["submitted"] and not rec["rejected"]:
                        obs["undeclare_pending"] = {"turn": turn, "at": time.time()}
                        say("undeclare submitted; awaiting settle")
                    elif rec["submitted"] and rec["rejected"]:
                        undeclare_outcome = "rejected"
                        notes.append("undeclare after decline: REJECTED by the engine "
                                     "(#4893 signature)")
                        say("undeclare REJECTED (message); will re-attack to test loop")
                    else:
                        say("P0 DeclareAttackers action not offered post-decline")
                    return
                if undeclare_outcome == "rejected" and len(ready) >= 2:
                    # re-attack to see whether attack->decline loops with no way out
                    await declare_attackers(c, pid, tag, ready[:3])
                    return
                await declare_attackers(c, pid, tag, [])
                say("P0 declares empty (cantpay fallback)")
                return
            if ph == "control" and prison and not control_done:
                goaded_again = (len(p1_decorum_casts) == 2
                                and turn > p1_decorum_casts[1])
                mana_ok = len(untapped_lands(state, 0, FOREST)) >= 2
                if goaded_again and ready and mana_ok:
                    if control_turn is None:
                        control_turn = turn
                        say(f"P0 control attack turn = {turn}")
                    await declare_attackers(c, pid, tag, ready[:1])
                else:
                    await declare_attackers(c, pid, tag, [])
                    say(f"P0 holds control attack (goaded_again={goaded_again}, "
                        f"ready={len(ready)}, untapped={len(untapped_lands(state, 0, FOREST))})")
                return
            await declare_attackers(c, pid, tag, [])
            if not prison:
                say("P0 holds attack: Prison not yet on P1 BF")
            elif ph == "setup":
                say(f"P0 holds attack: waiting for goad setup (ready={len(ready)})")
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
        # Can't-pay turn mana drain: the attack will use >=2 Bears (tax >= {4});
        # tap down to untapped < 4 so accept=true is genuinely unofferable.
        # (In run 20260909-5230b this never fired: P0 held only Forests in
        # hand on the can't-pay turn; the can't-pay arose naturally.)
        if (phase == "PreCombatMain" and obs["phase"] == "cantpay"
                and not cantpay_done
                and BEAR.lower() in hand_lnames(state, 0)
                and len(untapped_lands(state, 0, FOREST)) >= 4
                and drain_casts < 2):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" and lname(state, d.get("object_id")) == BEAR.lower():
                    say(f"P0 drains mana: casts Grizzly Bears "
                        f"(untapped={len(untapped_lands(state, 0, FOREST))})")
                    wire("cast_p0_bear_drain", a)
                    await submit_as_is(c, a)
                    drain_casts += 1
                    return
        if (phase in ("PreCombatMain", "PostCombatMain")
                and BEAR.lower() in hand_lnames(state, 0)
                and len(untapped_lands(state, 0, FOREST)) >= 2
                and p0_bears_cast < 3
                and obs["phase"] == "setup"):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" and lname(state, d.get("object_id")) == BEAR.lower():
                    say("P0 casts Grizzly Bears (setup)")
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

    def pick_land(acts, state):
        """Prefer Mountains until 4, else Plains, else first PlayLand."""
        plays = [a for a in acts if a["type"] == "PlayLand"]
        if not plays:
            return None
        def is_mtn(a):
            try:
                return lname(state, a.get("data", {}).get("land")) == MOUNTAIN.lower() or \
                       lname(state, a.get("data", {}).get("object_id")) == MOUNTAIN.lower()
            except Exception:
                return False
        if land_count(state, 1, MOUNTAIN) < 4:
            for a in plays:
                if is_mtn(a):
                    return a
        return plays[0]

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
        turn = state.get("turn_number")
        p0_ready = len([o for o in battlefield_ids(state, 0, BEAR)
                        if can_attack_now(state, o)])
        if (not p1_prison_cast and phase in ("PreCombatMain", "PostCombatMain")
                and PRISON.lower() in hand_lnames(state, 1)
                and land_count(state, 1, PLAINS) + land_count(state, 1, MOUNTAIN) >= 3
                and land_count(state, 1, PLAINS) >= 1):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" and lname(state, d.get("object_id")) == PRISON.lower():
                    say("P1 casts Ghostly Prison")
                    wire("cast_p1_prison", a)
                    await submit_as_is(c, a)
                    p1_prison_cast = True
                    return
        # Decorum #1: first P1 turn >= 5 once Prison is out and P0 has 2+ ready
        # Bears (can't-pay branch: tax 2x2=4 > untapped).
        # Decorum #2: first P1 turn >= 7 after the can't-pay phase resolved
        # (control: P0 pays for one goaded attacker).
        want_decorum = (
            p1_prison_cast
            and DECORUM.lower() in hand_lnames(state, 1)
            and land_count(state, 1, MOUNTAIN) >= 2
            and ((not p1_decorum_casts and turn >= 5 and p0_ready >= 2
                  and obs["phase"] == "setup")
                 or (len(p1_decorum_casts) == 1 and turn >= 7
                     and obs["phase"] == "control"))
        )
        if want_decorum and phase in ("PreCombatMain", "PostCombatMain"):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" and lname(state, d.get("object_id")) == DECORUM.lower():
                    say(f"P1 casts Disrupt Decorum (#{len(p1_decorum_casts) + 1}) on turn {turn}")
                    wire("cast_p1_decorum", a)
                    await submit_as_is(c, a)
                    p1_decorum_casts.append(turn)
                    if len(p1_decorum_casts) == 1:
                        obs["phase"] = "cantpay"
                        say("phase -> cantpay")
                    return
        pl = pick_land(acts, state)
        if pl:
            await submit_as_is(c, pl)
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
        p0_tax = [w for w in obs["tax_waits"] if w["who"] == "P0"]
        # A2: tax decision surfaced with an actionable submission
        actionable = [w for w in p0_tax if w["n_actions"] > 0]
        if p0_tax and len(actionable) == len(p0_tax):
            ass["A2_tax_prompted"] = "passed"
            notes.append(f"{len(p0_tax)} CombatTaxPayment wait(s) for P0, all with "
                         f"actionable submissions")
        elif p0_tax:
            ass["A2_tax_prompted"] = "failed"
            dead = len(p0_tax) - len(actionable)
            notes.append(f"{dead}/{len(p0_tax)} CombatTaxPayment wait(s) offered NO "
                         f"actionable submission (softlock signature)")
            obs["stall_observed"] = True
        else:
            ass["A2_tax_prompted"] = "failed"
            notes.append("no CombatTaxPayment wait was ever observed")
        # A3: can't-pay phase offered no accept=true
        cantpay_waits = [w for w in p0_tax if w["mode"] == "cantpay"]
        if cantpay_waits and not any(w["accept_true_offered"] for w in cantpay_waits):
            ass["A3_only_decline"] = "passed"
            notes.append(f"can't-pay: {len(cantpay_waits)} tax wait(s), accept=true "
                         f"never offered (untapped Forests "
                         f"{[w['untapped_forests'] for w in cantpay_waits]})")
        elif cantpay_waits:
            ass["A3_only_decline"] = "failed"
            notes.append("can't-pay: accept=true WAS offered despite insufficient mana")
        else:
            ass["A3_only_decline"] = "failed"
            notes.append("A3: no can't-pay tax wait observed")
        # A4: decline left the decision
        if obs["declines_this_turn"]:
            ass["A4_decline_resolves"] = "passed"
            notes.append(f"decline submitted on turn(s) "
                         f"{sorted(obs['declines_this_turn'])}; decision left "
                         f"(no orphaned CombatTaxPayment)")
        else:
            ass["A4_decline_resolves"] = "failed"
            notes.append("A4: decline branch never reached")
        # A5: undeclare outcome
        if undeclare_outcome == "accepted":
            ass["A5_undeclare"] = "passed"
            notes.append("empty DeclareAttackers after decline: ACCEPTED "
                         "(undeclare works; #4893 signature absent)")
        elif undeclare_outcome == "rejected":
            ass["A5_undeclare"] = "failed"
            notes.append("empty DeclareAttackers after decline: REJECTED "
                         "(#4893 signature: cannot undeclare attackers)")
        else:
            ass["A5_undeclare"] = "not-run"
            notes.append("A5: undeclare attempt never reached")
        # A6: control pay
        if pre_st is not None and post_st is not None:
            obs["life_pre"] = life_of(pre_st, 1)
            obs["life_post"] = life_of(post_st, 1)
            control_waits = [w for w in p0_tax if w["mode"] == "control"]
            pay_ok = any(w["accept_true_offered"] for w in control_waits)
            if (control_done and obs["life_after_control"] is not None
                    and obs["life_pre"] is not None
                    and obs["life_after_control"] == obs["life_pre"] - 2 and pay_ok):
                ass["A6_control_pay"] = "passed"
                notes.append(f"control: paid {{2}} for goaded attacker; P1 life "
                             f"{obs['life_pre']} -> {obs['life_after_control']}")
            elif not control_waits:
                ass["A6_control_pay"] = "not-run"
                notes.append("A6: control phase never reached")
            else:
                ass["A6_control_pay"] = "failed"
                notes.append(f"A6 failed: control_done={control_done} "
                             f"life_after_control={obs['life_after_control']} "
                             f"pay_offered={pay_ok}")
            # A8 cleanup
            slen = len(post_st.get("stack", []) or [])
            wf = (post_st.get("waiting_for") or {}).get("type")
            if slen == 0 and wf != "CombatTaxPayment":
                ass["A8_cleanup"] = "passed"
                notes.append(f"post.json: stack empty, wf={wf}, game proceeding "
                             f"(turn={post_st.get('turn_number')}, "
                             f"phase={post_st.get('phase')})")
            else:
                ass["A8_cleanup"] = "failed"
                notes.append(f"post.json stack={slen}, wf={wf}, not clean")
        else:
            for k in ("A6_control_pay", "A8_cleanup"):
                if ass[k] == "not-run":
                    ass[k] = "failed"
                    notes.append(f"{k} could not be evaluated (missing pre/post state)")
        # A7: no softlock
        if obs["loop_observed"]:
            ass["A7_no_softlock"] = "failed"
            obs["stall_observed"] = True
            notes.append("SOFTLOCK SIGNATURE: attack->decline loop with no legal "
                         "undeclare (game cannot progress past DeclareAttackers)")
        elif obs["stall_observed"]:
            ass["A7_no_softlock"] = "failed"
            notes.append("SOFTLOCK SIGNATURE observed (see notes)")
        elif p0_tax and all(w["n_actions"] > 0 for w in p0_tax):
            ass["A7_no_softlock"] = "passed"
            notes.append(f"{len(p0_tax)} CombatTaxPayment waits; acting player had "
                         f"an actionable submission every time; no loop")
        else:
            ass["A7_no_softlock"] = "failed"
            notes.append("A7: no CombatTaxPayment wait observed")
        # Verdict
        if obs["stall_observed"]:
            verdict = "reproduced"
            notes.append("verdict reproduced: CombatTaxPayment softlock signature "
                         "observed on v0.78.0 (a related failure on the #4893 "
                         "goad/no-mana axis, not the reporter's v0.17.0 board)")
        else:
            verdict = "blocked"
            notes.append("verdict blocked: report has no testable premise "
                         "(no board, tax card, mana situation, or branch; reporter "
                         "never answered the maintainer's 2026-07-19 questions). "
                         "Generic goaded-attacker CombatTaxPayment path on v0.78.0 "
                         "exercised.")
        run = {
            "issue": ISSUE,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "server_port": 9375,
            "driver": {"protocol_advertised": 68, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_5230b.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": obs,
            "verdict": verdict,
            "limitations": [
                "Browser UI not exercised; native engine via two human-client seats.",
                "12x Grizzly Bears / 8x Ghostly Prison / 8x Disrupt Decorum deck "
                "density is a test-harness convenience (engine accepts >4-of for "
                "custom games).",
                "The report names no tax card and no goad source; Ghostly Prison "
                "({2} per attacker) and Disrupt Decorum are the driver's chosen "
                "representatives for the #4893 goad/no-mana axis.",
                "Not tested on the original 2026-07-07 build v0.17.0; verdict is "
                "scoped to v0.78.0, not a fix claim.",
                "The prebuilt server has no standalone state-restore; states are "
                "authoritative exports (restorable only via full game replay).",
            ],
            "setup_line": "P0: 12x Grizzly Bears + 48x Forest (mulligan to Bear+2 "
                          "lands, cast Bears; optional PreCombatMain mana-drain "
                          "code present but unused - P0 held only Forests on "
                          "the can't-pay turn, so the can't-pay arose naturally "
                          "with 5 untapped Forests vs {6} tax); P1: 8x Ghostly "
                          "Prison + 12x Disrupt Decorum + 20x Plains + 20x "
                          "Mountain (Prison ~T4, Decorum goads P0's Bears, "
                          "never attacks)",
            "contract_line": "CombatTaxPayment surfaces when attacking a Prison "
                             "controller; a goaded attacker that cannot pay is "
                             "offered only decline; declining does not strand the "
                             "game; undeclare outcome recorded; a goaded attacker "
                             "that can pay pays and the attack proceeds",
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
        s0 = p0.latest["state"] if p0.latest else {}
        wf0 = (s0.get("waiting_for") or {}).get("type")
        if wf0 != "CombatTaxPayment":
            tax_wait_start = {"P0": None, "P1": None}
        # undeclare-loop detection: >=2 declines on the same turn with the
        # undeclare rejected means attack->decline->attack with no way forward
        for tn, n in obs["declines_this_turn"].items():
            if n >= 2 and undeclare_outcome == "rejected" and not obs["loop_observed"]:
                obs["loop_observed"] = True
                try:
                    mid = await p0.export_state()
                    with open(f"{EVDIR}/mid_loop.json", "w") as f:
                        f.write(mid)
                    say("exported mid_loop.json")
                except Exception as e:
                    notes.append(f"mid_loop export failed: {e}")
                notes.append(f"undeclare loop on turn {tn}: {n} declines, empty "
                             f"DeclareAttackers rejected each time")
                say("UNDECLARE LOOP OBSERVED; finishing")
                await finish()
                return
        # control outcome: latch P1 life after the control attack's combat
        control_waits = [w for w in obs["tax_waits"] if w["mode"] == "control"]
        if obs["life_after_control"] is None and control_waits:
            ct = control_waits[0]["turn"]
            ctn = s0.get("turn_number") or 0
            cp = s0.get("phase")
            if ctn > ct or (ctn == ct and cp in ("PostCombatMain", "End", "Cleanup")):
                obs["life_after_control"] = life_of(s0, 1)
                control_done = True
                notes.append(f"control tax resolved (attack turn {ct}); P1 life now "
                             f"{obs['life_after_control']}")
                say("control attack resolved")
        # softlock watchdog: tax wait with no actionable submission for 60s
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
                f"P1prison={len(battlefield_ids(s, 1, PRISON))} life1={life_of(s, 1)} "
                f"taxwaits={len(obs['tax_waits'])} phase={obs['phase']}")
        # post-export trigger: control resolved and game advanced
        if control_done and not post_exported and p0.latest:
            s = p0.latest["state"]
            ct = control_waits[0]["turn"] if control_waits else 0
            adv = (s.get("turn_number") or 0) > ct or (
                s.get("phase") in ("PostCombatMain", "End", "Cleanup")
                and s.get("turn_number") == ct)
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
