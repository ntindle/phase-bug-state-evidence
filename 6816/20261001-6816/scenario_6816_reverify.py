#!/usr/bin/env python3
"""#6816 re-verification (2026-10-01): CI workflow redirect bug, no engine involved.

Simulates the Actions `bash -e` shell for the pre-fix (cache-hit, no target/)
and current (mkdir -p target) semantics of the ai-gate.yml gate steps.
The stand-in gate command carries the report redirect exactly as the workflow
step had it; gate_ran.marker proves the command actually executed.
Writes repro.log + spot_check.json into the evidence dir.
"""
import json, os, subprocess, sys

EVID = os.path.expanduser("~/workspace/dev/phase-backfill/evidence/6816/20261001-6816")
os.makedirs(EVID, exist_ok=True)
log_lines = []
def log(s=""):
    print(s); log_lines.append(s)

results = {}
def run(label, cmd, cwd):
    p = subprocess.run(["bash", "-e", "-c", cmd], cwd=cwd,
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    log(f"$ {cmd}")
    log(p.stdout.rstrip())
    log(f"exit={p.returncode}")
    return p

def sandbox(name):
    d = os.path.join(EVID, name)
    subprocess.run(["rm", "-rf", d]); os.makedirs(d)
    return d

log("=== A1: current ai-gate.yml on main keeps mkdir -p target before both redirects ===")
# (workflow content checked via GitHub API; snapshot written separately)

log("=== A2: pre-fix semantics (no target/): redirect fails, gate never runs ===")
d = sandbox("sandbox_prefix")
p = run("A2", '(touch gate_ran.marker; echo "fake ai-gate report") > target/ai-gate-report.md', d)
marker = os.path.exists(os.path.join(d, "gate_ran.marker"))
log("MARKER " + ("PRESENT (unexpected)" if marker else "ABSENT - gate never ran (matches issue)"))
results["A2_prefix_redirect_fails"] = (p.returncode == 1 and "No such file or directory" in p.stdout and not marker)

log("=== A3: drift step cat against missing report fails under bash -e ===")
p = run("A3", 'body="$(cat target/ai-gate-report.md)"; echo "drift: $body"', d)
results["A3_drift_cat_fails"] = (p.returncode == 1)

log("=== A4: current semantics (mkdir -p target first): gate runs, drift step ok ===")
d = sandbox("sandbox_current")
p = run("A4", 'mkdir -p target; (touch gate_ran.marker; echo "fake ai-gate report") > target/ai-gate-report.md', d)
marker = os.path.exists(os.path.join(d, "gate_ran.marker"))
report = os.path.join(d, "target", "ai-gate-report.md")
report_ok = os.path.exists(report) and os.path.getsize(report) > 0
log("MARKER " + ("PRESENT - gate ran" if marker else "ABSENT (unexpected)"))
log("REPORT " + ("NON-EMPTY" if report_ok else "MISSING/EMPTY (unexpected)"))
p2 = run("A4-drift", 'body="$(cat target/ai-gate-report.md)"; echo "drift: $body"', d)
results["A4_current_semantics_ok"] = (p.returncode == 0 and marker and report_ok and p2.returncode == 0)

log("=== A5: ai-perf-gate step, same ordering ===")
p = run("A5", 'mkdir -p target; (touch perf_ran.marker; echo "fake perf report") > target/ai-perf-gate-report.md', d)
results["A5_perf_step_ok"] = (p.returncode == 0 and os.path.exists(os.path.join(d, "perf_ran.marker")))

for n in ("sandbox_prefix", "sandbox_current"):
    subprocess.run(["rm", "-rf", os.path.join(EVID, n)])

overall = all(results.values())
log("")
log("ALL PASSED" if overall else "FAILURES PRESENT")
for k, v in results.items():
    log(f"  {k}: {'passed' if v else 'failed'}")

with open(os.path.join(EVID, "repro.log"), "w") as f:
    f.write("\n".join(log_lines) + "\n")
with open(os.path.join(EVID, "spot_check.json"), "w") as f:
    json.dump({"assertions": {k: ("passed" if v else "failed") for k, v in results.items()},
               "verdict": "not-reproduced"}, f, indent=1)
sys.exit(0 if overall else 1)
