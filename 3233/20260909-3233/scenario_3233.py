#!/usr/bin/env python3
"""Issue #3233: Stuck decision: DeclareAttackers.

BEHAVIORAL CONTRACT (per PLAYBOOK.md step 2 - written before observing results)
--------------------------------------------------------------------------------
Report (github issue, build v0.1.56 a668283, 2026-06-14):
  "What happened" - EMPTY (no description at all).
  Diagnostic: Waiting for: DeclareAttackers | Stuck players: 0
  Maintainer comment (2026-07-19): asked what happened immediately before the
  stall - creatures able/required to attack, goad/tax/restriction effects,
  whose turn, saved game state or game log. NO reply from the reporter.

There is NO testable premise: no board state, no steps, no save, no
goad/tax/restriction info. Per PLAYBOOK step 2, a native engine test would
require inventing the premise. This run is therefore a blocked ATTEMPT:
drive a fresh native-engine game through the DeclareAttackers decision for
both players - with an actual attack, and with a deliberately empty
declaration - and record whether the decision ever leaves the acting player
with no available submission (the reported softlock signature).

Oracle/expected (generic engine behavior):
  E1: at each combat phase the active player gets DeclareAttackers offered
      with a usable submission.
  E2: submitting DeclareAttackers with an attackable creature records the
      attack; combat proceeds to DeclareBlockers/damage.
  E3: submitting DeclareAttackers with zero attackers also completes;
      the game advances.
  E4: unblocked 2/2 deals 2 damage (P1 20 -> 18).

Assertions:
  A1_setup_ok        P0's turn reaches DeclareAttackers with a Grizzly Bears
                     able to attack on the battlefield; pre.json exported.
  A2_attack_declared P0 submitted DeclareAttackers naming the Bear; no
                     ActionRejected; the Bear recorded as attacking
                     (P1 life 20->18 in post proves it).
  A3_combat_completes P1 life 20 -> 18 between pre.json and post.json
                     (unblocked bear damage), game left DeclareAttackers.
  A4_empty_declares  P1's DeclareAttackers with attacks=[] completed;
                     game advanced past it (no stall).
  A5_no_softlock     Every DeclareAttackers wait observed the advertised
                     DeclareAttackers action available to the acting player,
                     and the decision advanced; no wait exceeded 60s without
                     an available submission.
  A6_cleanup         post.json: stack empty, game proceeding.

Verdict rule: the reported bug has no testable premise (missing setup
information that materially changes the test), so the verdict is `blocked`
regardless of the generic attempt outcome. The generic attempt only becomes
`reproduced` if the softlock signature itself is observed (a DeclareAttackers
wait with no submission available to the acting player); it can never be
`not-reproduced` because the reported path was never identified.

Evidence: evidence/3233/<run-id>/pre.json (P0 DeclareAttackers, Bear able to
attack), post.json (after both players' DeclareAttackers resolved), run.json,
manifest.sha256, summary.png, scenario_3233.py, wire_log.jsonl,
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
from client import PhaseClient, deck  # noqa: E402

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260909-3233"
EVDIR = f"{BACKFILL}/evidence/3233/{RUN_ID}"
os.makedirs(EVDIR, exist_ok=True)
os.makedirs(f"{BACKFILL}/runs/{RUN_ID}", exist_ok=True)

WIRE = open(f"{EVDIR}/wire_log.jsonl", "w")
RUNLOG = open(f"{EVDIR}/scenario_run.log", "w")

BEAR = "Grizzly Bears"
FOREST = "Forest"

P0_DECK = [(BEAR, 12), (FOREST, 48)]
P1_DECK = [(BEAR, 12), (FOREST, 48)]

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
    "source": "sha256 match of pinned verified release artifacts (verified 2026-09-09 run, ledger); "
              "fresh isolated server on 127.0.0.1:9374 from this run's run dir",
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


def untapped_forests(state, pid):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and lname(state, oid) == FOREST.lower() and not o.get("tapped")]


def bear_ids(state, pid):
    return [int(oid) for oid, o in state.get("objects", {}).items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and lname(state, oid) == BEAR.lower()]


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
           ("A1_setup_ok", "A2_attack_declared", "A3_combat_completes",
            "A4_empty_declares", "A5_no_softlock", "A6_cleanup")}
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
    p0_bear_cast = False
    p1_bear_cast = False

    obs = {
        "declare_waits": [],      # every DeclareAttackers wait seen
        "stall_observed": False,  # waiting with NO submission offered
        "rejections": [],
        "life_pre": None,
        "life_post": None,
        "p0_attack_oid": None,
        "p1_empty_done": False,
    }
    declare_wait_start = {"P0": None, "P1": None}

    async def mulligan_tick(c, pid, acts, state, tag):
        ma = find_action(acts, "MulliganDecision")
        if ma and not kept.get(tag):
            hn = hand_lnames(state, pid)
            bears = sum(1 for n in hn if n == BEAR.lower())
            lands = sum(1 for n in hn if n == FOREST.lower())
            mulls = kept.get(f"{tag}_mulls", 0)
            if (bears >= 1 and lands >= 2) or mulls >= 3:
                kept[tag] = True
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Keep"}}})
                say(f"{tag} keeps (bears={bears}, lands={lands})")
            else:
                kept[f"{tag}_mulls"] = mulls + 1
                await submit_as_is(c, {"type": "MulliganDecision",
                                       "data": {"choice": {"type": "Mulligan"}}})
                say(f"{tag} mulligans #{mulls + 1} (bears={bears}, lands={lands})")
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
                    return 0 if nm == FOREST.lower() else (2 if nm == BEAR.lower() else 1)
                picks = sorted(hand_ids, key=bottom_key)[:count]
                kept[f"{tag}_bottomed"] = True
                await submit_as_is(c, {"type": "SelectCards",
                                       "data": {"cards": [int(x) for x in picks]}})
                say(f"{tag} bottoms {count}: {[lname(state, x) for x in picks]}")
                return True
        return False

    async def declare_attackers(c, pid, tag, attack_oids):
        """Submit a DeclareAttackers decision; record the attempt."""
        nonlocal pre_exported
        st_latest = c.latest
        state = st_latest["state"]
        acts = merged_actions(st_latest)
        da = find_action(acts, "DeclareAttackers")
        rec = {
            "turn": state.get("turn_number"),
            "active": state.get("active_player"),
            "who": tag,
            "action_offered": da is not None,
            "attack_oids": list(attack_oids),
        }
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
        nonlocal pre_exported, p0_bear_cast, post_exported
        wtype = (state.get("waiting_for") or {}).get("type")
        if await mulligan_tick(p0, 0, acts, state, "P0"):
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p0, a)
                return
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p0, oa)
            return
        if wtype == "DeclareAttackers" and state.get("active_player") == 0:
            if declare_wait_start["P0"] is None:
                declare_wait_start["P0"] = time.time()
            ready = [o for o in bear_ids(state, 0) if can_attack_now(state, o)]
            # export PRE at the first DeclareAttackers with an attack-ready bear
            if ready and not pre_exported:
                say("P0 DeclareAttackers with attack-ready Bear; exporting PRE")
                pre = await p0.export_state()
                with open(f"{EVDIR}/pre.json", "w") as f:
                    f.write(pre)
                pre_st = json.loads(pre)["state"]
                obs["life_pre"] = life_of(pre_st, 1)
                ass["A1_setup_ok"] = "passed"
                notes.append(f"pre: P0 bears={len(bear_ids(pre_st, 0))}, "
                             f"attack-ready={len([o for o in bear_ids(pre_st, 0) if can_attack_now(pre_st, o)])}, "
                             f"P1 life={obs['life_pre']}")
                pre_exported = True
            rec = await declare_attackers(p0, 0, "P0", ready)
            if rec["submitted"] and ready and obs["p0_attack_oid"] is None:
                obs["p0_attack_oid"] = ready[0]
            declare_wait_start["P0"] = None
            return
        if wtype == "DeclareBlockers" and state.get("active_player") == 1:
            db = find_action(acts, "DeclareBlockers")
            if db:
                sub = copy.deepcopy(db)
                sub["data"]["assignments"] = []
                await p0.send_action({"type": "DeclareBlockers", "data": sub["data"]})
                say("P0 declares no blockers")
            return
        if wtype != "Priority" or state.get("priority_player") != 0:
            return
        # ---- P0 priority ----
        if (not p0_bear_cast and not bear_ids(state, 0)
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and BEAR.lower() in hand_lnames(state, 0)
                and len(untapped_forests(state, 0)) >= 2):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" and lname(state, d.get("object_id")) == BEAR.lower():
                    say("P0 casts Grizzly Bears")
                    wire("cast_p0_bear", a)
                    await submit_as_is(p0, a)
                    p0_bear_cast = True
                    return
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(p0, a)
                return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p0, a)
                return

    async def p1_tick(st, acts, state):
        nonlocal p1_bear_cast, post_exported
        wtype = (state.get("waiting_for") or {}).get("type")
        if await mulligan_tick(p1, 1, acts, state, "P1"):
            return
        for a in acts:
            if a["type"] in ("PayManaAbilityMana", "PayMana"):
                await submit_as_is(p1, a)
                return
        if wtype == "OrderTriggers":
            oa = find_action(acts, "OrderTriggers")
            if oa:
                await submit_as_is(p1, oa)
            return
        if wtype == "AssignCombatDamage":
            ad = find_action(acts, "AssignCombatDamage")
            if ad:
                await submit_as_is(p1, ad)  # advertised default, verbatim
                say("P1 submits advertised AssignCombatDamage")
            return
        if wtype == "DeclareAttackers" and state.get("active_player") == 1:
            # P1 deliberately declares NO attackers (empty-declaration path)
            rec = await declare_attackers(p1, 1, "P1", [])
            if rec["submitted"]:
                obs["p1_empty_done"] = True
            return
        if wtype == "DeclareBlockers" and state.get("active_player") == 0:
            db = find_action(acts, "DeclareBlockers")
            if db:
                sub = copy.deepcopy(db)
                sub["data"]["assignments"] = []
                await p1.send_action({"type": "DeclareBlockers", "data": sub["data"]})
                say("P1 declares no blockers")
            return
        if wtype != "Priority" or state.get("priority_player") != 1:
            return
        # ---- P1 priority ----
        if (not p1_bear_cast and not bear_ids(state, 1)
                and state.get("phase") in ("PreCombatMain", "PostCombatMain")
                and BEAR.lower() in hand_lnames(state, 1)
                and len(untapped_forests(state, 1)) >= 2):
            for a in acts:
                d = a.get("data", {})
                if a["type"] == "CastSpell" and lname(state, d.get("object_id")) == BEAR.lower():
                    say("P1 casts Grizzly Bears")
                    wire("cast_p1_bear", a)
                    await submit_as_is(p1, a)
                    p1_bear_cast = True
                    return
        for a in acts:
            if a["type"] == "PlayLand":
                await submit_as_is(p1, a)
                return
        for a in acts:
            if a["type"] == "PassPriority":
                await submit_as_is(p1, a)
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
        if pre_st is not None and post_st is not None:
            obs["life_pre"] = life_of(pre_st, 1)
            obs["life_post"] = life_of(post_st, 1)
            # A2: attack declared and accepted (no rejection + bear attacked)
            p0_atks = [w for w in obs["declare_waits"]
                       if w["who"] == "P0" and w["submitted"] and w["attack_oids"]]
            if p0_atks and obs["life_post"] == obs["life_pre"] - 2:
                ass["A2_attack_declared"] = "passed"
                ass["A3_combat_completes"] = "passed"
                notes.append(f"P0 attacked with Bear oid {obs['p0_attack_oid']}; "
                             f"P1 life {obs['life_pre']} -> {obs['life_post']} "
                             f"(unblocked 2/2); combat completed")
            else:
                if not p0_atks:
                    ass["A2_attack_declared"] = "failed"
                    notes.append("P0 never submitted a DeclareAttackers with an attacker")
                else:
                    ass["A2_attack_declared"] = "failed"
                    notes.append(f"P0 attacked but P1 life pre={obs['life_pre']} "
                                 f"post={obs['life_post']} (expected -2)")
                ass["A3_combat_completes"] = "failed"
                notes.append(f"A3 failed: P1 life {obs['life_pre']} -> {obs['life_post']} "
                             f"(expected 20->18)")
            # A4: P1 empty declaration completed and game advanced
            if obs["p1_empty_done"]:
                p1w = post_st.get("waiting_for", {}) or {}
                progressed = (p1w.get("type") not in ("DeclareAttackers",) or
                              post_st.get("active_player") != 1)
                ass["A4_empty_declares"] = "passed" if progressed else "failed"
                notes.append(f"P1 empty DeclareAttackers submitted and game "
                             f"advanced (post wf={p1w.get('type')}, "
                             f"phase={post_st.get('phase')})")
            else:
                ass["A4_empty_declares"] = "failed"
                notes.append("P1 never reached/completed an empty DeclareAttackers")
            # A6 cleanup
            slen = len(post_st.get("stack", []) or [])
            if slen == 0:
                ass["A6_cleanup"] = "passed"
                notes.append(f"post.json: stack empty, game proceeding "
                             f"(turn={post_st.get('turn_number')}, "
                             f"phase={post_st.get('phase')}, "
                             f"active=P{post_st.get('active_player')})")
            else:
                ass["A6_cleanup"] = "failed"
                notes.append(f"post.json stack={slen}, not clean")
        else:
            for k in ("A2_attack_declared", "A3_combat_completes",
                      "A4_empty_declares", "A6_cleanup"):
                if ass[k] == "not-run":
                    ass[k] = "failed"
                    notes.append(f"{k} could not be evaluated (missing pre/post state)")
        # A5: no softlock across all DeclareAttackers waits
        stalled = [w for w in obs["declare_waits"] if not w["action_offered"]]
        if obs["declare_waits"] and not stalled:
            ass["A5_no_softlock"] = "passed"
            notes.append(f"{len(obs['declare_waits'])} DeclareAttackers waits; "
                         f"action offered to the acting player every time, "
                         f"all decisions advanced")
        elif stalled:
            ass["A5_no_softlock"] = "failed"
            obs["stall_observed"] = True
            notes.append(f"SOFTLOCK SIGNATURE: {len(stalled)} DeclareAttackers wait(s) "
                         f"with NO submission offered to the acting player")
        else:
            ass["A5_no_softlock"] = "failed"
            notes.append("no DeclareAttackers wait was ever observed")
        # Verdict: reported issue has no testable premise -> blocked.
        # Only a genuinely observed stall (softlock signature) would make this
        # a related failure (reproduced); the generic path cannot make it
        # not-reproduced because the reported path was never identified.
        if obs["stall_observed"]:
            verdict = "reproduced"
            notes.append("verdict reproduced: softlock signature observed on v0.78.0 "
                         "(a related failure, not the reporter's v0.1.56 board)")
        else:
            verdict = "blocked"
            notes.append("verdict blocked: report has no testable premise "
                         "(no board, steps, or save; reporter never answered "
                         "the maintainer's 2026-07-19 questions). Generic "
                         "DeclareAttackers path on v0.78.0 completes normally.")
        run = {
            "issue": 3233,
            "run_id": RUN_ID,
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t_start)),
            "duration_s": round(dur, 1),
            "server": SERVER_IDENTITY,
            "server_run_dir": f"runs/{RUN_ID}",
            "driver": {"protocol_advertised": 68, "client": "driver/client.py"},
            "scenario_sha256": hashlib.sha256(
                open(f"{BACKFILL}/driver/scenario_3233.py", "rb").read()).hexdigest(),
            "decks": {"P0": P0_DECK, "P1": P1_DECK},
            "assertions": ass,
            "notes": notes,
            "observations": obs,
            "verdict": verdict,
            "limitations": [
                "Browser UI not exercised; native engine via two human-client seats.",
                "12x Grizzly Bears deck density is a test-harness convenience "
                "(engine accepts >4-of for custom games).",
                "No goad/tax/restriction or attack-requirement effects on the board; "
                "the report names none, so the generic path is the only attempt.",
                "The prebuilt server has no standalone state-restore; states are "
                "authoritative exports (restorable only via full game replay).",
            ],
            "setup_line": "P0: 12x Grizzly Bears + 48x Forest (mulligan to Bear+2 lands, "
                          "cast Bear, attack when ready); P1: same but declares no attackers",
            "contract_line": "DeclareAttackers always offers a submittable action; "
                             "an attack declaration and an empty declaration both complete; "
                             "combat damage resolves normally",
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
        # softlock watchdog: DeclareAttackers wait with no offered submission
        for c, tag, pid in ((p0, "P0", 0), (p1, "P1", 1)):
            if c.latest and c.latest["state"].get("waiting_for", {}).get("type") == "DeclareAttackers" \
                    and c.latest["state"].get("active_player") == pid:
                start = declare_wait_start[tag]
                if start and time.time() - start > 60:
                    da = find_action(merged_actions(c.latest), "DeclareAttackers")
                    if da is None:
                        obs["stall_observed"] = True
                        mid = await p0.export_state()
                        with open(f"{EVDIR}/mid_stall.json", "w") as f:
                            f.write(mid)
                        notes.append(f"stall watchdog: {tag} DeclareAttackers wait >60s "
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
                f"pp={s.get('priority_player')} P0bears={len(bear_ids(s, 0))} "
                f"P1bears={len(bear_ids(s, 1))} life0={life_of(s, 0)} life1={life_of(s, 1)} "
                f"declares={len(obs['declare_waits'])}")
        # post-export trigger: P1 empty declaration done and game advanced
        if obs["p1_empty_done"] and not post_exported and p0.latest:
            s = p0.latest["state"]
            p1_post = (s.get("active_player") == 1
                       and s.get("phase") in ("PostCombatMain", "End", "Cleanup"))
            next_p0 = (s.get("active_player") == 0 and (s.get("turn_number") or 0) >= 7)
            if p1_post or next_p0:
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
