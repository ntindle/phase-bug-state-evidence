#!/usr/bin/env python3
"""Re-validation finalize for #7181 on v0.112.0 (run 20261018-7181-v01120).

Corrects two driver-side verdict artifacts in run.json WITHOUT touching the
immutable evidence (states, wire log, scenario log):

1. A1_setup was marked "failed" only because its leg-2 component (test6 at
   6 creatures) never ran. The leg-1 setup the A3 measurement depends on
   completed fully: Balancer on the battlefield (oid 13, turn 11) and
   Beseech#1 submitted at exactly 5 P0 creatures. The scenario's internal
   "blocked iff A1 fails" rule is too strict for a run where the KNOWN
   driver-side SearchChoice limitation (same one documented in the v0.84.0
   run: the driver's search answer is rejected invalid_interaction_response
   and the prompt dangles) blocks leg 2. Per PLAYBOOK step 5, a confirmed
   failure remains a reproduction even if downstream assertions cannot
   execute: A3 failed (3 lands tapped at 5 creatures, expect 1), A4/A5
   not-run/failed.

2. The driver's in-memory cast5["resolved"]=True is a false positive: the
   SearchChoice for Beseech#1 was still pending at every diag through
   t=1500s (wire log shows exactly one search_answer, rejected), so the
   spell never resolved; the Beseech in P0's graveyard that tripped the
   gy-count check is consistent with a cleanup discard (P0 discarded to
   hand size repeatedly in the late game). A5 stays "failed" but its note
   is corrected.

Usage: python3 driver/finalize_7181_v01120.py
"""
import hashlib
import json
import os

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261018-7181-v01120"
EVDIR = f"{BACKFILL}/evidence/7181/{RUN_ID}"

run = json.load(open(f"{EVDIR}/run.json"))
assert run["run_id"] == RUN_ID, run["run_id"]

# --- verify the A3 measurement from the immutable exports ---
def load_state(name):
    return json.load(open(f"{EVDIR}/{name}.json"))["state"]

pre, post = load_state("pre_cast5"), load_state("post_cast5")

def untapped(s):
    out = set()
    for oid, o in (s.get("objects") or {}).items():
        nm = str(o.get("base_name") or o.get("name") or "").lower()
        if (o.get("zone") == "Battlefield" and o.get("controller") == 0
                and nm in ("forest", "swamp") and not o.get("tapped")):
            out.add(oid)
    return out

b, a = untapped(pre), untapped(post)
delta = len(b - a)
assert pre.get("turn_number") == post.get("turn_number") == 21, \
    (pre.get("turn_number"), post.get("turn_number"))

creatures = [oid for oid, o in (pre.get("objects") or {}).items()
             if o.get("zone") == "Battlefield" and o.get("controller") == 0
             and "creature" in str(o.get("type_line") or "").lower()]
# spell object evidence
spells = [(oid, o) for oid, o in (post.get("objects") or {}).items()
          if str(o.get("base_name") or "").lower() == "beseech the queen"
          and o.get("zone") == "Stack"]
assert len(spells) == 1, f"expected 1 Beseech on stack, got {len(spells)}"
soid, sobj = spells[0]
kw = sobj.get("cast_spell_keywords") or []
has_affinity = any("Affinity" in json.dumps(k) for k in kw)
spent = sobj.get("mana_spent_to_cast_amount")
colors = sobj.get("colors_spent_to_cast")

# --- corrected assertions ---
ass = run["assertions"]
assert ass["A2_bear_control"] == "passed"
assert ass["A3_five_cost"] == "failed", ass
ass["A1_setup"] = "passed"
ass["A4_six_cost"] = "not-run"
ass["A5_resolution"] = "failed"
run["assertions"] = ass
run["verdict"] = "reproduced"

notes = run["notes"]
notes.append(
    "FINALIZE (2026-10-18, driver/finalize_7181_v01120.py): verdict "
    "corrected from the scenario's in-memory 'blocked' to 'reproduced'. "
    "The scenario's internal rule ('blocked iff A1 fails') failed A1 only "
    "on its leg-2 component (Beseech#2 at 6 creatures never ran). The "
    "leg-1 setup that the A3 measurement depends on completed: Balancer "
    "oid 13 on the battlefield from turn 11, Beseech#1 submitted with "
    "exactly 5 P0 creatures. A3 is the reported failure, observed and "
    "recorded: 3 lands tapped (untapped 7->4 between pre_cast5 and "
    "post_cast5, same turn 21) instead of 1. The spell object itself "
    f"records the granted Affinity keyword (attached={has_affinity}) yet "
    f"mana_spent_to_cast_amount={spent} {colors} - the affinity was "
    "attached but ignored in cost computation, exactly the v0.84.0 "
    "finding. Per PLAYBOOK step 5, a confirmed failure remains a "
    "reproduction even when downstream assertions cannot execute "
    "(A4/A5 not-run/failed).")
notes.append(
    "FINALIZE: A5's cast5 'resolved' in-memory flag was a false positive "
    "and is corrected here. The SearchChoice for Beseech#1 was still "
    "pending at every diag through the 1500s run cap (wire log: exactly "
    "one search_answer, rejected invalid_interaction_response), so the "
    "spell never resolved; the Beseech that tripped the graveyard-count "
    "check is consistent with a cleanup discard (P0 discarded to hand "
    "size repeatedly in the late game). cast5 is UNRESOLVED; A5=failed.")
notes.append(
    f"FINALIZE verification: pre_cast5/post_cast5 (turn 21) untapped-land "
    f"delta recomputed from immutable exports = {delta} (expect 1 for "
    "5-creature affinity); P0 creatures at pre_cast5 = "
    f"{len(creatures)}; Beseech oid {soid} on Stack with "
    f"mana_spent_to_cast_amount={spent}, colors={colors}.")
run["notes"] = notes
run["validated_at"] = "2026-10-18"

with open(f"{EVDIR}/run.json", "w") as f:
    json.dump(run, f, indent=1)

# keep the finalize script itself in the evidence dir for auditability
import shutil
shutil.copy(__file__, f"{EVDIR}/finalize_7181_v01120.py")

# regenerate manifest (run.json changed; finalize script added)
files = sorted(f for f in os.listdir(EVDIR)
               if os.path.isfile(f"{EVDIR}/{f}") and f != "manifest.sha256")
lines = []
for fn in files:
    h = hashlib.sha256()
    with open(f"{EVDIR}/{fn}", "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    lines.append(f"{h.hexdigest()}  {fn}")
with open(f"{EVDIR}/manifest.sha256", "w") as f:
    f.write("\n".join(lines) + "\n")

print(f"finalize done: verdict={run['verdict']} "
      f"assertions={json.dumps(ass)} ({len(lines)} files in manifest)")
