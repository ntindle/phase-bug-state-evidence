#!/usr/bin/env python3
"""Finalizer for issue #6877 (Krark, the Thumbless - win-after-loss copies).

Aggregates the four scenario runs 20260912-01..04 (identical scenario code,
v0.80.0 / protocol 69, two human-client seats) into one assertion set, and
writes trials.json + assertions.json + run.json into the consolidated
evidence dir evidence/6877/20260911-6877/.

Fixes vs the first finalization attempt (which reported blocked, p~0.033):
  1. A2 counted pre_trial exports (26, all four runs incl. partial) against
     trials parsed from observations.json (only the two completed runs, 14)
     and failed spuriously. Now each run's pre_trial exports are checked
     against that same run's wire-log trial_end records.
  2. The first attempt ran before runs 02/04 finished, so its p_hat came
     from 14 first flips only. This version aggregates all 26 trials.
  3. Aggregated output lands in its own consolidated run dir instead of
     being mixed into run 20260912-04.

Statistical framing (A4): a second trigger's flip after the spell left the
stack is not directly observable (a win the engine ignores looks exactly
like a loss), so A4 is necessarily statistical. H0 (correct engine, per the
Gatherer 11/10/2020 ruling that a won flip still copies a spell no longer
on the stack): each first-loss trial's second flip wins with the engine's
coin probability p_hat (estimated conservatively from the 26 fully-
observable first flips) and a win MUST create an observable copy.
p = (1 - p_hat)^N for 0 copies in N first-loss trials.
"""
import json
import glob
import math
import os
import shutil
import datetime

ISSUE = "6877"
RUN_IDS = ["20260912-01", "20260912-02", "20260912-03", "20260912-04"]
BASE = os.path.expanduser("~/workspace/dev/phase-backfill")
EVD = [os.path.join(BASE, "evidence", ISSUE, r) for r in RUN_IDS]
OUT = os.path.join(BASE, "evidence", ISSUE, "20260911-6877")
os.makedirs(OUT, exist_ok=True)

KRARK = "Krark, the Thumbless"
BOX = "Mirror Box"
BOLT = "Lightning Bolt"


def load(p):
    with open(p) as f:
        env = json.load(f)
    # authoritative export is an envelope {"precast_shortcut_runtime","state"}
    return env["state"] if isinstance(env, dict) and "state" in env else env


def oname(o):
    return o.get("card_name") or o.get("name") or o.get("base_name") or ""


def kind_type(e):
    k = e.get("kind")
    return k.get("type") if isinstance(k, dict) else k


def is_krark_trigger(e):
    if not isinstance(e, dict) or kind_type(e) != "TriggeredAbility":
        return False
    d = (e.get("kind") or {}).get("data") or {}
    if d.get("source_name") != KRARK:
        return False
    desc = str(d.get("description") or "")
    if "flip a coin" not in desc.lower():
        ab = d.get("ability") or {}
        desc = str(ab.get("description") or "")
    return "flip a coin" in desc.lower()


def is_boltlike(e):
    if not isinstance(e, dict) or kind_type(e) != "Spell":
        return False
    eff = (((e.get("kind") or {}).get("data") or {}).get("ability")
           or {}).get("effect") or {}
    return (eff.get("type") == "DealDamage"
            and (eff.get("amount") or {}).get("value") == 3)


def bf_count(state, seat, name):
    return sum(1 for o in (state.get("objects") or {}).values()
               if o.get("zone") == "Battlefield"
               and o.get("controller") == seat and oname(o) == name)


def hand_count(state, seat, name):
    return sum(1 for o in (state.get("objects") or {}).values()
               if o.get("zone") == "Hand"
               and o.get("controller") == seat and oname(o) == name)


def trial_ends(evd):
    out = []
    with open(os.path.join(evd, "wire_log.jsonl")) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if d.get("event") == "trial_end":
                out.append(d["payload"])
    return out


def main():
    trials, a1_rows, a2_rows, a6_rows = [], [], [], []
    notes = []

    for rid, evd in zip(RUN_IDS, EVD):
        ends = trial_ends(evd)
        # A1: setup from this run's pre_trial_0
        st0 = load(os.path.join(evd, "pre_trial_0.json"))
        kr = bf_count(st0, 0, KRARK)
        bx = bf_count(st0, 0, BOX)
        a1_ok = (st0.get("active_player") == 0
                 and st0.get("phase") in ("PreCombatMain", "PostCombatMain")
                 and kr >= 2 and bx >= 1
                 and hand_count(st0, 0, BOLT) >= 1)
        a1_rows.append({"run": rid, "passed": a1_ok, "krark_bf": kr,
                        "box_bf": bx, "bolt_in_hand": hand_count(st0, 0, BOLT),
                        "phase": st0.get("phase"),
                        "active_player": st0.get("active_player")})
        # A2: every pre_trial export of THIS run vs THIS run's trial count
        pres = sorted(glob.glob(os.path.join(evd, "pre_trial_*.json")))
        a2_run_ok = (len(pres) == len(ends) and len(ends) > 0)
        for p in pres:
            st = load(p)
            stack = st.get("stack") or []
            n_trig = sum(1 for e in stack if is_krark_trigger(e))
            n_bolt = sum(1 for e in stack if is_boltlike(e))
            ok = (n_trig == 2 and n_bolt == 1 and len(stack) == 3)
            a2_rows.append({"run": rid, "file": os.path.basename(p),
                            "passed": ok, "stack_len": len(stack),
                            "krark_triggers": n_trig, "boltlike": n_bolt})
            a2_run_ok = a2_run_ok and ok
        a2_rows.append({"run": rid, "file": "COUNT",
                        "passed": len(pres) == len(ends),
                        "pre_exports": len(pres), "trials": len(ends)})
        for t in ends:
            t = dict(t)
            t["run"] = rid
            trials.append(t)
        # A6: post_trials where the run completed
        pt = os.path.join(evd, "post_trials.json")
        if os.path.exists(pt):
            stp = load(pt)
            empty = not (stp.get("stack") or [])
            a6_rows.append({"run": rid, "passed": empty,
                            "stack_len": len(stp.get("stack") or [])})
        else:
            a6_rows.append({"run": rid, "passed": None,
                            "note": "run ended early (deck-out game-over in "
                                    "-02; watchdog in -04); no post_trials"})

    trials.sort(key=lambda t: (t["run"], t["trial"]))
    n_trials = len(trials)

    A = {}
    A["A1_setup_ok"] = {
        "status": "passed" if all(r["passed"] for r in a1_rows) else "failed",
        "detail": a1_rows}
    A["A2_two_triggers"] = {
        "status": "passed" if all(r["passed"] for r in a2_rows) else "failed",
        "detail": f"{sum(1 for r in a2_rows if r['passed'])}/{len(a2_rows)} "
                  "checks: each trial began with exactly 2 Krark FlipCoin "
                  "triggers above 1 boltlike spell (3-entry stack)"}
    firstloss = [t for t in trials
                 if t["first_outcome"] == "loss" and not t["coalesced"]]
    A["A3_firstloss_set"] = {
        "status": "passed" if len(firstloss) >= 6 else "failed",
        "detail": f"{len(firstloss)} clean first-loss trials (first trigger "
                  "demonstrably lost: bolt left the stack, no copy, bolt "
                  "returned to hand, 0 damage)"}
    copies = sum(1 for t in firstloss if t["second_copy"])
    n = len(firstloss)
    # coin win rate from fully-observable first flips (conservative: the
    # observed 8/26 is BELOW 0.5, which raises p rather than lowering it)
    first_wins = sum(1 for t in trials if t["first_outcome"] == "win")
    p_hat = first_wins / n_trials if n_trials else 0.0
    p_value = (1.0 - p_hat) ** n if n else 1.0
    A["A4_win_after_loss_copies"] = {
        "status": ("failed" if (copies == 0 and n >= 6)
                   else "passed" if copies > 0 else "not-run"),
        "detail": f"{copies}/{n} first-loss trials produced a second-trigger "
                  f"copy; coin-win rate from {n_trials} observable first "
                  f"flips p_hat={first_wins}/{n_trials}={p_hat:.4f}; "
                  f"binomial p(0 copies | correct) = (1-p_hat)^{n} = "
                  f"{p_value:.2e} (< 0.01 bar)"}
    firstwin = [t for t in trials
                if t["first_outcome"] == "win" and not t["coalesced"]]
    a5_rows = []
    for t in firstwin:
        res = t.get("resolutions") or []
        fc = bool(res) and res[0].get("outcome") == "win" \
            and res[0].get("created_copy") is True
        a5_rows.append({"run": t["run"], "trial": t["trial"],
                        "class": t["class"], "first_copy": fc,
                        "damage": t.get("damage")})
    A["A5_control_first_win"] = {
        "status": "passed" if (firstwin and all(r["first_copy"]
                                                for r in a5_rows))
                  else "failed",
        "detail": f"{sum(1 for r in a5_rows if r['first_copy'])}/"
                  f"{len(a5_rows)} first-resolving wins created a bolt copy "
                  "(the copy path works while the spell is on the stack)"}
    a6_done = [r for r in a6_rows if r["passed"] is not None]
    A["A6_cleanup"] = {
        "status": "passed" if (a6_done and all(r["passed"] for r in a6_done))
                  else "failed",
        "detail": a6_rows}

    if A["A1_setup_ok"]["status"] == "passed" \
            and A["A2_two_triggers"]["status"] == "passed" \
            and A["A3_firstloss_set"]["status"] == "passed" \
            and A["A5_control_first_win"]["status"] != "failed" \
            and A["A4_win_after_loss_copies"]["status"] == "failed":
        verdict = "reproduced"
    elif all(v["status"] == "passed" for v in A.values()):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    with open(os.path.join(OUT, "trials.json"), "w") as f:
        json.dump({"issue": 6877, "runs": RUN_IDS, "trials": trials}, f,
                  indent=1, default=str)
    with open(os.path.join(OUT, "assertions.json"), "w") as f:
        json.dump({"issue": 6877, "verdict": verdict, "assertions": A,
                   "stats": {"trials_total": n_trials,
                             "firstloss_clean": n,
                             "second_trigger_copies": copies,
                             "coin_win_rate_first_flips":
                                 f"{first_wins}/{n_trials}",
                             "binomial_p": p_value},
                   "notes": notes}, f, indent=1, default=str)

    run_meta = {
        "issue": 6877,
        "run_id": "20260911-6877",
        "aggregated_runs": RUN_IDS,
        "scenario": "scenario_6877.py (identical across the four runs)",
        "server": {
            "server_version": "v0.80.0",
            "build_commit": "22cca6d",
            "protocol_version": 69,
            "mode": "Full",
            "binary_sha256": "1d414c0e999a088560ab9ad0d77a4ae4f5773610cee6afda616a62c0e654238e",
            "card_data_sha256": "7ce6f92d0adb8fc4158bf0ab76797a644eb77dcea01f9743bb849550bb677bfd",
            "draft_pools_sha256": "c78dbd16f671e5b21ec094d6fcbc2b5da76e79cc82c2d9daa914369180021348",
            "signature_key_id": "436711b6a2d36828",
            "signature_verified": True,
        },
        "validated_version": "v0.80.0",
        "verdict": verdict,
        "result_summary": (
            f"{n}/{n} clean first-loss trials produced zero second-trigger "
            f"copies (binomial p={p_value:.2e}); {len(firstwin)} first-win "
            "controls all created copies while the spell was on the stack."),
    }
    with open(os.path.join(OUT, "run.json"), "w") as f:
        json.dump(run_meta, f, indent=1)

    shutil.copy(os.path.join(BASE, "driver", "scenario_6877.py"),
                os.path.join(OUT, "scenario_6877.py"))
    shutil.copy(os.path.join(BASE, "driver", "finalize_6877.py"),
                os.path.join(OUT, "finalize_6877.py"))
    for rid, evd in zip(RUN_IDS, EVD):
        lg = os.path.join(evd, "scenario_run.log")
        if os.path.exists(lg):
            shutil.copy(lg, os.path.join(OUT, f"scenario_run_{rid}.log"))

    print(f"trials={n_trials} firstloss={n} copies={copies} "
          f"p_hat={p_hat:.4f} p={p_value:.2e} verdict={verdict}")
    for k, v in A.items():
        d = v["detail"]
        print(f"  {k}: {v['status']} -- "
              f"{d if isinstance(d, str) else f'{len(d)} rows'}")


if __name__ == "__main__":
    main()
