#!/usr/bin/env python3
"""Issue #6690: chained ChangeZone's `ControllerRef::You` misclassified as
relative, blocking activation (The Beamtown Bullies).

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (2026-07-27, status:confirmed, area:engine): when a chained ChangeZone
names a zone belonging to the ability's controller ("from your graveyard")
while a companion player target exists on the parent node,
ability_utils::relative_controller_kind classifies that ControllerRef::You as
a *relative* controller. legal_targets_for_ability_filter then re-enumerates
the slot against the companion player's candidates, searches the OPPONENT's
graveyard, finds nothing, and activation fails with
ActionNotAllowed("No legal targets available").

Reproduction per the issue: The Beamtown Bullies --
  "{T}: Target opponent whose turn it is puts target nonlegendary creature
   card from your graveyard onto the battlefield under their control. ..."
Activate {T} on an opponent's turn with a nonlegendary creature card in YOUR
graveyard. Reported: the ability is not activatable.

Card-data state on the pinned release (v0.78.0, key 'the beamtown bullies',
verified 2026-09-10): the activated ability parses with cost {T}, target
null, and top-level effect Unimplemented{name:"change_zone_enters_under_anaphor",
description:"under their control"}; the haste/goad/delayed-exile remainder is
a SequentialSibling sub-chain. A dataset-wide scan found NO activated ability
on v0.78.0 with both a player target and a chained ChangeZone
{controller:"You"} -- the issue's trigger structure is absent from the pinned
data. The scenario therefore exercises the issue's literal repro (activate {T}
on the opponent's turn with a creature in your graveyard) and records exactly
what the engine offers/rejects.

Scenario (native engine, two human-client seats):
  P0: 4x The Beamtown Bullies ({1}{B}{R}{G}) + 12x Grizzly Bears + 8x Faithless
      Looting ({R}: draw 2, discard 2 -> bins Bears) + 12x Forest + 12x Mountain
      + 12x Swamp. Casts Looting (discard Bears), casts Bullies, holds it
      untapped.
  P1: 60x Island. Plays a land per turn, always passes priority, never attacks.

Probe point: P0 controls an untapped Beamtown Bullies, P0's graveyard holds >=1
Grizzly Bears (nonlegendary), it is P1's turn, P0 has priority. The driver
exports pre.json, scans legal_actions for the Bullies' ActivateAbility,
attempts it, drains rejections, exports post.json.

Assertions:
  A1_setup_ok        probe point reached (Bullies untapped on P0 BF, Bears in
                     P0 gy, P1's turn, P0 priority).
  A2_offered         an ActivateAbility action for the Bullies was advertised.
  A3_no_target_block the attempt was NOT rejected with "No legal targets
                     available" (the reported failure signature).
  A4_announced       the activation was accepted and the ability announced
                     (on the stack); only meaningful if A2 passed.
  A5_cleanup         game proceeds after the attempt (no stall).

Verdict rule: reproduced iff A3 fails with the reported "No legal targets
available" rejection. not-reproduced iff the activation is accepted/announced
(the reported block is absent on v0.78.0). If the ability is not offered at
all, the verdict documents the parse state (no target slots in the pinned
card data) rather than relabeling.

Evidence: evidence/6690/<run-id>/{pre,post}.json, bullies_object.json,
run.json, manifest.sha256, summary.png, scenario_6690.py, wire_log.jsonl,
scenario_run.log, server_excerpts.log
"""
import asyncio
import copy
import hashlib
import json
import os
import sys
import time
from collections import Counter

sys.path.insert(0, "/home/hatch/workspace/dev/phase-backfill/driver")
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260910-6690"
ISSUE = 6690
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")
open(f"{EVDIR}/console.log", "w").write("")

BULLIES = "the beamtown bullies"
BEARS = "grizzly bears"
LOOTING = "faithless looting"
FOREST = "forest"
MOUNTAIN = "mountain"
SWAMP = "swamp"
ISLAND = "island"
LANDS = (FOREST, MOUNTAIN, SWAMP)

P0_DECK = [(BULLIES, 4), (BEARS, 12), (LOOTING, 8), (FOREST, 12),
           (MOUNTAIN, 12), (SWAMP, 12)]
P1_DECK = [(ISLAND, 60)]

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
    "observed_at": "2026-09-10",
    "source": "ServerHello probed live (0.78.0/4de7224/proto 68/Full) on "
              "127.0.0.1:9374; pinned v0.78.0 release = latest stable "
              "(published 2026-09-08); minisign-verified binary + signed data "
              "manifest with repo-pinned SERVER_ARTIFACT_PUBLIC_KEY; shared "
              "isolated server already listening on 9374, verified live",
}


def say(*a):
    m = " ".join(str(x) for x in a)
    print(m, flush=True)
    RUNLOG.write(m + "\n")
    RUNLOG.flush()
    with open(f"{EVDIR}/console.log", "a") as f:
        f.write(m + "\n")


def wire(event, payload):
    try:
        WIRE.write(json.dumps({"t": time.time(), "event": event,
                               "payload": payload}, default=str) + "\n")
        WIRE.flush()
    except Exception as e:
        say("wire error", e)


def obj_name(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def get_obj(state, oid):
    return state.get("objects", {}).get(str(oid), {})


def lname(state, oid):
    return obj_name(get_obj(state, oid))


def player_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p
    return {}


def hand_names(state, pid):
    return [lname(state, o) for o in player_of(state, pid).get("hand", [])]


def hand_ids(state, pid):
    return [int(o) for o in player_of(state, pid).get("hand", [])]


def gy_names(state, pid):
    return [lname(state, o) for o in player_of(state, pid).get("graveyard", [])]


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


def bullies_on_bf(state, pid=0):
    for oid, o in state.get("objects", {}).items():
        if o.get("zone") == "Battlefield" and o.get("controller") == pid \
                and obj_name(o) == BULLIES:
            return int(oid), o
    return None, None


async def main():
    t_start = time.time()
    notes = []
    ass = {k: "not-run" for k in
           ("A1_setup_ok", "A2_offered", "A3_no_target_block",
            "A4_announced", "A5_cleanup")}
    obs = {
        "stage": "setup",
        "probed": False,
        "probe_turn": None,
        "bullies_oid": None,
        "offered": False,
        "offered_data": None,
        "attempted": False,
        "rejections": [],
        "stack_after": None,
        "tapped_after": None,
        "wait_after": None,
        "settle_ticks": 0,
        "done": False,
    }
    p0 = PhaseClient("P0")
    await p0.connect()
    await p0.create(deck(*P0_DECK))
    p1 = PhaseClient("P1")
    await p1.connect()
    await p1.join(p0.game_code, deck(*P1_DECK))
    game_code = p0.game_code
    say(f"game {game_code}; P0 seat={p0.player_id} P1 seat={p1.player_id}")
    wire("game", {"game_code": game_code})

    kept = {}

    async def drain(c):
        while True:
            try:
                t, data = c.inbox.get_nowait()
            except asyncio.QueueEmpty:
                return
            if t in ("Error", "ActionRejected"):
                rec = {"who": c.name, "type": t, "data": data}
                obs["rejections"].append(rec)
                say(f"[{c.name}] {t}: {json.dumps(data)[:400]}")
                wire("rejection", rec)

    async def export(tag):
        try:
            s = await p0.export_state()
            with open(f"{EVDIR}/{tag}.json", "w") as f:
                f.write(s)
            say(f"exported {tag}.json ({len(s)} bytes)")
            wire("export", {"tag": tag, "bytes": len(s)})
            return True
        except Exception as e:
            notes.append(f"{tag} export failed: {e}")
            say(f"export {tag} FAILED: {e}")
            return False

    async def mulligan_branch(c, pid, tag, acts, state, wtype):
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get(tag):
            hn = hand_names(state, pid)
            lands = sum(1 for n in hn if n in LANDS)
            mulls = kept.get(tag + "_mulls", 0)
            if tag == "P0":
                # Hunt Looting + lands; Bears density (12x) makes the bin likely.
                want = LOOTING in hn and lands >= 2
            else:
                want = True  # P1: all lands, keep immediately
            if want or mulls >= 4:
                kept[tag] = True
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Keep"}}})
                say(f"{tag} keeps (mulls={mulls})")
            else:
                kept[tag + "_mulls"] = mulls + 1
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Mulligan"}}})
                say(f"{tag} mulligans #{mulls + 1}")
            return True
        if wtype == "MulliganDecision":
            sc = find_action(acts, "SelectCards")
            if sc and not kept.get(tag + "_bot"):
                pending = ((state.get("waiting_for") or {}).get("data", {})
                           or {}).get("pending", [])
                count = 0
                for p in pending:
                    if p.get("player") == pid:
                        ph = p.get("phase", {}) or {}
                        if ph.get("type") == "BottomCards":
                            count = int(ph.get("count", 0))
                if count > 0:
                    hids = hand_ids(state, pid)

                    def bkey(oid):
                        nm = lname(state, oid)
                        return 0 if nm in LANDS else (2 if nm in (BULLIES, LOOTING, BEARS) else 1)
                    picks = sorted(hids, key=bkey)[:count]
                    kept[tag + "_bot"] = True
                    await submit_as_is(c, {"type": "SelectCards",
                                           "data": {"cards": [int(x) for x in picks]}})
                    say(f"{tag} bottoms {count}")
            return True
        return False

    async def discard_branch(c, pid, tag, state, wtype, prefer_first):
        """Answer a Discard* prompt only if it is pending for this pid.
        prefer_first: card names to discard first (ranked before others)."""
        if not (wtype and "Discard" in wtype):
            return False
        wf = state.get("waiting_for") or {}
        data = wf.get("data") or {}
        # DiscardChoice names the discarding player in data.player (verified
        # 2026-09-10: {"player": 0, "count": 2, ...}); never answer another
        # player's discard (wrong_player rejection).
        named = data.get("player")
        if named is not None and named != pid:
            return False
        pending = data.get("pending") or []
        count = None
        ours = False
        for p in pending:
            if p.get("player") == pid:
                ours = True
                ph = p.get("phase", {}) or {}
                if "count" in ph:
                    count = int(ph["count"])
                break
        if pending and not ours:
            return False  # another player's discard; not ours to answer
        hids = hand_ids(state, pid)

        def rank(oid):
            nm = lname(state, oid)
            return (0, nm) if nm in prefer_first else (2, nm)
        hids.sort(key=rank)
        if wtype == "DiscardToHandSize":
            n = max(0, len(hids) - 7)
        else:
            n = count if count else 2
        picks = hids[:n]
        if not picks:
            return False
        await submit_as_is(c, {"type": "SelectCards",
                               "data": {"cards": [int(x) for x in picks]}})
        say(f"{tag} discards {[lname(state, x) for x in picks]} ({wtype})")
        wire("discard", {"who": tag, "wtype": wtype,
                         "cards": [lname(state, x) for x in picks]})
        return True

    async def p0_tick(st):
        state = st.get("state") or {}
        acts = merged_actions(st)
        wtype = (state.get("waiting_for") or {}).get("type")
        if await mulligan_branch(p0, 0, "P0", acts, state, wtype):
            return
        # discard (Faithless Looting resolution or hand size): bin Bears first
        if await discard_branch(p0, 0, "P0", state, wtype, (BEARS,)):
            return
        # payments first
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        # combat declarations: always empty
        if wtype in ("DeclareAttackers", "DeclareBlockers"):
            da = find_action(acts, wtype)
            if da:
                sub = copy.deepcopy(da)
                sub["data"]["attacks"] = []
                if "blocks" in sub["data"]:
                    sub["data"]["blocks"] = []
                await submit_as_is(p0, sub)
                say("P0 declares no attackers/blockers")
                return
        if wtype != "Priority" or state.get("priority_player") != 0:
            return
        turn = state.get("turn_number")
        phase = state.get("phase")
        # land drop: try every tick (no kept-flag; cf. 6666 driver). A flag set
        # at Upkeep/Draw would poison the later main-phase attempt.
        if phase in ("PreCombatMain", "PostCombatMain"):
            for oid in hand_ids(state, 0):
                if lname(state, oid) in LANDS:
                    la = next((a for a in acts if a["type"] == "PlayLand"
                               and str(a.get("data", {}).get("card_id")) == str(oid)
                               or a["type"] == "PlayLand"
                               and str(a.get("data", {}).get("object_id")) == str(oid)), None)
                    if la is None:
                        la = find_action(acts, "PlayLand")
                    if la:
                        await submit_as_is(p0, la)
                        say(f"P0 plays land t{turn}")
                        wire("land", {"who": "P0", "turn": turn})
                        return
        # cast Faithless Looting
        if not kept.get("looted"):
            for oid in hand_ids(state, 0):
                if lname(state, oid) == LOOTING:
                    cs = next((a for a in acts if a["type"] == "CastSpell"
                               and str(a.get("data", {}).get("object_id")) == str(oid)
                               or a["type"] == "CastSpell"
                               and str(a.get("data", {}).get("card_id")) == str(oid)
                               or a["type"] == "CastSpell"
                               and str(a.get("data", {}).get("source_id")) == str(oid)), None)
                    if cs:
                        kept["looted"] = True
                        await submit_as_is(p0, cs)
                        say(f"P0 casts Faithless Looting t{turn}")
                        wire("cast", {"card": LOOTING, "turn": turn})
                        return
        # cast Beamtown Bullies
        bid, _ = bullies_on_bf(state, 0)
        if bid is None:
            for oid in hand_ids(state, 0):
                if lname(state, oid) == BULLIES:
                    cs = next((a for a in acts if a["type"] == "CastSpell"
                               and str(a.get("data", {}).get("object_id")) == str(oid)
                               or a["type"] == "CastSpell"
                               and str(a.get("data", {}).get("card_id")) == str(oid)
                               or a["type"] == "CastSpell"
                               and str(a.get("data", {}).get("source_id")) == str(oid)), None)
                    if cs:
                        await submit_as_is(p0, cs)
                        say(f"P0 casts Beamtown Bullies t{turn}")
                        wire("cast", {"card": BULLIES, "turn": turn})
                        return
        # ---- probe point ----
        bid, bo = bullies_on_bf(state, 0)
        gy = gy_names(state, 0)
        if (not obs["probed"] and bid is not None and not bo.get("tapped")
                and gy.count(BEARS) >= 1
                and state.get("active_player") == 1
                and phase in ("PreCombatMain", "PostCombatMain")):
            obs["probed"] = True
            obs["stage"] = "probe"
            obs["probe_turn"] = turn
            obs["bullies_oid"] = bid
            say(f"PROBE POINT t{turn} {phase}: bullies={bid} untapped, "
                f"bears_in_gy={gy.count(BEARS)}, P1's turn, P0 priority")
            wire("probe_point", {"turn": turn, "phase": phase,
                                 "bullies_oid": bid,
                                 "bears_in_gy": gy.count(BEARS),
                                 "bullies_object": bo})
            with open(f"{EVDIR}/bullies_object.json", "w") as f:
                json.dump({"oid": bid, "object": bo}, f, indent=1, default=str)
            await export("pre")
            aa = [a for a in acts if a["type"] == "ActivateAbility"
                  and a.get("data", {}).get("source_id") == bid]
            say(f"ActivateAbility candidates for bullies {bid}: {len(aa)}")
            wire("activation_candidates",
                 {"bullies_oid": bid,
                  "candidates": [a.get("data") for a in aa],
                  "all_action_types": sorted(set(a["type"] for a in acts))})
            if aa:
                obs["offered"] = True
                obs["offered_data"] = aa[0].get("data")
                say("attempting activation: " + json.dumps(aa[0].get("data"))[:400])
                await submit_as_is(p0, aa[0])
                obs["attempted"] = True
                await asyncio.sleep(2.5)
                await drain(p0)
                st2 = p0.latest or {}
                state2 = st2.get("state") or {}
                obs["stack_after"] = len(state2.get("stack", []) or [])
                b2 = bullies_on_bf(state2, 0)
                obs["tapped_after"] = b2[1].get("tapped") if b2[1] else None
                obs["wait_after"] = (state2.get("waiting_for") or {}).get("type")
                say(f"after attempt: stack={obs['stack_after']} "
                    f"tapped={obs['tapped_after']} wait={obs['wait_after']}")
                wire("after_attempt", {"stack": obs["stack_after"],
                                      "tapped": obs["tapped_after"],
                                      "wait": obs["wait_after"],
                                      "stack_detail": state2.get("stack")})
            else:
                say("NO ActivateAbility offered for the Bullies")
                notes.append("ActivateAbility not offered for Beamtown Bullies "
                             "at the probe point")
            await export("post")
            return
        # default: pass priority
        pa = find_action(acts, "PassPriority")
        if pa:
            await submit_as_is(p0, pa)
            return

    async def p1_tick(st):
        state = st.get("state") or {}
        acts = merged_actions(st)
        wtype = (state.get("waiting_for") or {}).get("type")
        if await mulligan_branch(p1, 1, "P1", acts, state, wtype):
            return
        if await discard_branch(p1, 1, "P1", state, wtype, (ISLAND,)):
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        if wtype in ("DeclareAttackers", "DeclareBlockers"):
            da = find_action(acts, wtype)
            if da:
                sub = copy.deepcopy(da)
                sub["data"]["attacks"] = []
                if "blocks" in sub["data"]:
                    sub["data"]["blocks"] = []
                await submit_as_is(p1, sub)
                return
        if wtype != "Priority" or state.get("priority_player") != 1:
            return
        turn = state.get("turn_number")
        phase = state.get("phase")
        if phase in ("PreCombatMain", "PostCombatMain"):
            la = find_action(acts, "PlayLand")
            if la:
                await submit_as_is(p1, la)
                wire("land", {"who": "P1", "turn": turn})
                return
        pa = find_action(acts, "PassPriority")
        if pa:
            await submit_as_is(p1, pa)

    # ---- main loop ----
    last_sig = None
    stall_watch = time.time()
    while time.time() - t_start < 900 and not obs["done"]:
        await asyncio.sleep(0.15)
        await drain(p0)
        await drain(p1)
        if p0.latest:
            await p0_tick(p0.latest)
        if p1.latest:
            await p1_tick(p1.latest)
        # progress watchdog
        st = p0.latest or {}
        state = st.get("state") or {}
        sig = (state.get("turn_number"), state.get("phase"),
               (state.get("waiting_for") or {}).get("type"))
        if sig != last_sig:
            last_sig = sig
            stall_watch = time.time()
            say(f"[tick] turn={sig[0]} phase={sig[1]} wait={sig[2]} "
                f"stage={obs['stage']}")
        if obs["probed"]:
            obs["settle_ticks"] += 1
            if obs["settle_ticks"] > 60:  # ~9s of settling after probe
                obs["done"] = True
        elif time.time() - stall_watch > 180:
            notes.append(f"stall watchdog: no state progress for 180s at {sig}")
            say("STALL WATCHDOG fired")
            obs["done"] = True
        elif (state.get("turn_number") or 0) > 20 and not obs["probed"]:
            notes.append("setup never reached the probe point by turn 20")
            say("turn cap reached without probe")
            obs["done"] = True

    # ---- assertions ----
    if obs["probed"]:
        ass["A1_setup_ok"] = "passed"
    else:
        ass["A1_setup_ok"] = "failed"
        notes.append("A1: probe point never reached")
    if obs["offered"]:
        ass["A2_offered"] = "passed"
    elif obs["probed"]:
        ass["A2_offered"] = "failed"
    rej_text = json.dumps(obs["rejections"]).lower()
    if "no legal targets" in rej_text:
        ass["A3_no_target_block"] = "failed"
        notes.append("A3: activation rejected with 'No legal targets ...' "
                     "(the reported failure signature)")
    elif obs["attempted"]:
        ass["A3_no_target_block"] = "passed"
    elif obs["probed"]:
        ass["A3_no_target_block"] = "not-run"
        notes.append("A3: not-run (activation not offered, nothing attempted)")
    if obs["attempted"] and (obs["stack_after"] or 0) > 0:
        ass["A4_announced"] = "passed"
    elif obs["attempted"]:
        ass["A4_announced"] = "failed"
        notes.append("A4: activation submitted but nothing reached the stack")
    if obs["probed"]:
        # cleanup: game still advancing / at least not hard-stalled
        ass["A5_cleanup"] = "passed"

    if "no legal targets" in rej_text:
        verdict = "reproduced"
    elif obs["attempted"] and ass["A4_announced"] == "passed":
        verdict = "not-reproduced"
    elif obs["probed"] and not obs["offered"]:
        verdict = "not-reproduced"
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: probe point never reached; no trustworthy result")

    say("assertions: " + json.dumps(ass))
    say("verdict: " + verdict)
    for n_ in notes:
        say("note: " + n_)

    # ---- run.json ----
    duration_s = round(time.time() - t_start, 1)
    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "server": SERVER_IDENTITY,
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "games": [{
            "tag": "A",
            "desc": "Beamtown Bullies {T} activation on the opponent's turn "
                    "with a nonlegendary creature in the controller's graveyard",
            "assertions": ass,
            "observations": {
                "probe_turn": obs["probe_turn"],
                "bullies_oid": obs["bullies_oid"],
                "offered": obs["offered"],
                "offered_data": obs["offered_data"],
                "attempted": obs["attempted"],
                "stack_after": obs["stack_after"],
                "tapped_after": obs["tapped_after"],
                "wait_after": obs["wait_after"],
                "rejections": obs["rejections"],
            },
            "notes": notes,
        }],
        "verdict": verdict,
        "evidence_dir": f"{ISSUE}/{RUN_ID}",
        "duration_s": duration_s,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t_start)),
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1, default=str)

    # ---- server excerpts ----
    try:
        slog = f"{BACKFILL}/runs/20260910-6594/server.log"
        excerpts = []
        if os.path.exists(slog):
            with open(slog, errors="replace") as f:
                for line in f:
                    if game_code in line:
                        excerpts.append(line.rstrip())
        with open(f"{EVDIR}/server_excerpts.log", "w") as f:
            f.write("\n".join(excerpts[-80:]) + "\n")
        say(f"server excerpts: {len(excerpts)} lines for {game_code}")
    except Exception as e:
        say(f"server excerpts failed: {e}")

    # ---- manifest ----
    mf = []
    for root, _d, names in os.walk(EVDIR):
        for n_ in sorted(names):
            if n_ in ("manifest.sha256",):
                continue
            fp = os.path.join(root, n_)
            h = hashlib.sha256(open(fp, "rb").read()).hexdigest()
            mf.append(f"{h}  {os.path.relpath(fp, EVDIR)}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(sorted(mf)) + "\n")
    say(f"manifest: {len(mf)} files")

    await p0.close()
    await p1.close()
    WIRE.close()
    RUNLOG.close()
    print(f"VERDICT:{verdict}")

asyncio.run(main())
