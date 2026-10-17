#!/usr/bin/env python3
"""Post-hoc correction for run-9934-v01110-20261017-0910 (#9934).

The live run wrote A3_own_source_control="failed", but that overstates
what was established. What actually happened in the CONTROL leg:

- P0 declared Survivors (oid 18) as an attacker vs P1 (combat.attackers
  confirms object_id 18 -> Player 1; survivors tapped from attacking).
- The driver answered a viewer-interaction "exert" prompt affirmatively
  (interaction 6W9Y2X.0.648, choice c0, accepted with no rejection).
- BUT a top-level ChooseExert action remained advertised/pending for the
  full 18s control window (see DIAG atypes=['ChooseExert']); the driver
  has no ChooseExert submitter, so the exert-as-attacks decision was
  never actually submitted through the action the engine was waiting on.
- exerted_this_turn stayed [] and no trigger fired -- but since the
  exert decision itself never completed, "the engine didn't fire the
  own-source trigger" was NOT established.

Per the #9921 correction precedent (AGENTS.md 2026-10-17), verdict-
relevant assertions must reflect only what was observed. A3 is a
non-verdict-driving control; correcting failed -> not-run with the
reason does not change the run's verdict (reproduced, driven by A2).

This script updates assertions.json and run.json in place and records
the correction in both files' notes. The original (incorrect) assertion
values are preserved under "correction.superseded".
"""
import copy
import json
import os
import sys

EVDIR = ("/home/hatch/workspace/dev/phase-backfill/evidence/9934/"
         "run-9934-v01110-20261017-0910")

REASON = (
    "A3 corrected failed -> not-run post-hoc: the exert-as-attacks "
    "decision was never submitted. The driver answered a "
    "viewer-interaction exert prompt (iid 6W9Y2X.0.648, no rejection) "
    "but the engine's top-level ChooseExert action stayed pending for "
    "the full 18s control window (DIAG atypes=['ChooseExert']); the "
    "driver has no ChooseExert submitter. Survivors attacked (tapped, "
    "in combat.attackers) but whether an own-source exert would fire "
    "its trigger was not established. Non-verdict-driving control; "
    "verdict unchanged (reproduced via A2)."
)


def main():
    ap = os.path.join(EVDIR, "assertions.json")
    rp = os.path.join(EVDIR, "run.json")
    a = json.load(open(ap))
    r = json.load(open(rp))

    old_assertions = copy.deepcopy(a["assertions"])
    old_notes = list(a.get("notes", []))

    assert a["assertions"].get("A3_own_source_control") == "failed", \
        "unexpected A3 value; refusing to correct"
    assert a["verdict"] == "reproduced"
    assert r["verdict"] == "reproduced"

    a["assertions"]["A3_own_source_control"] = "not-run"
    a["notes"].append(REASON)
    a["correction"] = {
        "corrected_at": "2026-10-17T08:15:00-05:00",
        "script": "driver/correct_9934_assertions.py",
        "superseded": {"assertions": old_assertions,
                       "notes_tail": old_notes[-3:]},
        "reason": REASON,
        "verdict_unchanged": True,
    }
    with open(ap, "w") as f:
        json.dump(a, f, indent=1)

    r["assertions"]["A3_own_source_control"] = "not-run"
    r["notes"].append(REASON)
    r.setdefault("corrections", []).append(a["correction"])
    with open(rp, "w") as f:
        json.dump(r, f, indent=1)

    print("corrected A3 failed -> not-run; verdict still",
          a["verdict"])


if __name__ == "__main__":
    main()
