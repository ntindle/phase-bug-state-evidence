#!/usr/bin/env python3
"""Scenario for phase-rs/phase#9849: coverage + semantic-audit JSON byte-reproducibility.

Reported bug: two runs over identical inputs write different JSON because of
unseeded HashMap iteration feeding count-only sorts with top-N truncates.

Behavioral contract:
  - Setup: pinned v0.108.0 card-data.json as the identical input for both runs.
  - Trigger: run each tool twice (separate processes, separate CWDs for the
    semantic-audit writer).
  - Assertions:
      A1 coverage JSON parses (both runs).
      A2 coverage run1 == run2 byte-identical.  (EXPECTED FAIL per bug report)
      A3 semantic-audit JSON parses (both runs).
      A4 semantic-audit run1 == run2 byte-identical. (EXPECTED FAIL per bug report)
      A5 diffs are confined to ordering of tied entries (informational).

Verdict mapping: A2 or A4 failing with a diff that is ordering-only in the
predicted structures => reproduced. Both passing => not-reproduced.
"""
import argparse
import difflib
import hashlib
import json
import os
import subprocess
import sys
import tempfile

REPRO_STRUCTURES = (
    "oracle_patterns", "co_occurrences", "gap_bundles",
    "flagged_cards", "finding_counts",
)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run_tool(cmd, cwd, env):
    p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=False, timeout=3600)
    return p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--coverage-bin", required=True)
    ap.add_argument("--oracle-gen-bin", required=True)
    ap.add_argument("--data-dir", required=True, help="dir containing card-data.json")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--tool-commit", required=True, help="git commit the tools were built from")
    args = ap.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)
    env = dict(os.environ)
    env["RUST_LOG"] = "off"

    results = {}

    # ---- coverage-report x2 (JSON summary goes to stdout) ----
    cov_hashes, cov_paths = [], []
    for i in (1, 2):
        out_path = os.path.join(args.out_dir, f"coverage_run{i}.json")
        with tempfile.TemporaryDirectory() as tmp:
            p = run_tool([args.coverage_bin, "--brief", args.data_dir], tmp, env)
        if p.returncode != 0:
            results["A1_coverage_json_parses"] = {
                "status": "failed",
                "reason": f"coverage-report run{i} exited {p.returncode}: {p.stderr.decode()[:500]}",
            }
            break
        with open(out_path, "wb") as f:
            f.write(p.stdout)
        try:
            json.loads(p.stdout.decode())
            parsed = True
        except Exception as e:  # noqa: BLE001
            parsed = False
            results.setdefault("A1_coverage_json_parses", {
                "status": "failed", "reason": f"run{i} stdout not JSON: {e}"})
        cov_hashes.append(sha256_file(out_path))
        cov_paths.append(out_path)
    else:
        results["A1_coverage_json_parses"] = {"status": "passed"}

    if len(cov_hashes) == 2:
        same = cov_hashes[0] == cov_hashes[1]
        results["A2_coverage_byte_identical"] = {
            "status": "passed" if same else "failed",
            "run1_sha256": cov_hashes[0],
            "run2_sha256": cov_hashes[1],
        }
        if not same:
            with open(cov_paths[0]) as f1, open(cov_paths[1]) as f2:
                diff = list(difflib.unified_diff(
                    f1.read().splitlines(), f2.read().splitlines(),
                    "coverage_run1", "coverage_run2", n=2))
            diff_path = os.path.join(args.out_dir, "coverage_run1_vs_run2.diff")
            with open(diff_path, "w") as f:
                f.write("\n".join(diff[:400]))
            results["A2_coverage_byte_identical"]["diff_lines"] = len(diff)
            results["A2_coverage_byte_identical"]["diff_path"] = "coverage_run1_vs_run2.diff"
    else:
        results["A2_coverage_byte_identical"] = {"status": "not-run", "reason": "A1 failed"}

    # ---- oracle-gen semantic-audit x2 (writes data/semantic-audit.json under CWD) ----
    sem_hashes, sem_paths = [], []
    for i in (1, 2):
        workdir = tempfile.mkdtemp(prefix=f"sem_audit_run{i}_")
        os.makedirs(os.path.join(workdir, "data"))
        p = run_tool([args.oracle_gen_bin, "semantic-audit", args.data_dir], workdir, env)
        out_path = os.path.join(workdir, "data", "semantic-audit.json")
        if p.returncode != 0 or not os.path.exists(out_path):
            results["A3_semantic_audit_json_parses"] = {
                "status": "failed",
                "reason": f"run{i} rc={p.returncode} stderr={p.stderr.decode()[:500]}",
            }
            break
        dest = os.path.join(args.out_dir, f"semantic_audit_run{i}.json")
        with open(out_path, "rb") as src, open(dest, "wb") as dst:
            dst.write(src.read())
        try:
            with open(dest) as f:
                json.load(f)
            parsed = True
        except Exception as e:  # noqa: BLE001
            parsed = False
            results.setdefault("A3_semantic_audit_json_parses", {
                "status": "failed", "reason": f"run{i} not JSON: {e}"})
        sem_hashes.append(sha256_file(dest))
        sem_paths.append(dest)
    else:
        results["A3_semantic_audit_json_parses"] = {"status": "passed"}

    if len(sem_hashes) == 2:
        same = sem_hashes[0] == sem_hashes[1]
        results["A4_semantic_audit_byte_identical"] = {
            "status": "passed" if same else "failed",
            "run1_sha256": sem_hashes[0],
            "run2_sha256": sem_hashes[1],
        }
        if not same:
            with open(sem_paths[0]) as f1, open(sem_paths[1]) as f2:
                diff = list(difflib.unified_diff(
                    f1.read().splitlines(), f2.read().splitlines(),
                    "semantic_audit_run1", "semantic_audit_run2", n=2))
            diff_path = os.path.join(args.out_dir, "semantic_audit_run1_vs_run2.diff")
            with open(diff_path, "w") as f:
                f.write("\n".join(diff[:400]))
            results["A4_semantic_audit_byte_identical"]["diff_lines"] = len(diff)
            results["A4_semantic_audit_byte_identical"]["diff_path"] = "semantic_audit_run1_vs_run2.diff"
    else:
        results["A4_semantic_audit_byte_identical"] = {"status": "not-run", "reason": "A3 failed"}

    # ---- verdict ----
    a2 = results.get("A2_coverage_byte_identical", {}).get("status")
    a4 = results.get("A4_semantic_audit_byte_identical", {}).get("status")
    if a2 == "failed" or a4 == "failed":
        verdict = "reproduced"
    elif a2 == "passed" and a4 == "passed":
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    assertions_path = os.path.join(args.out_dir, "assertions.json")
    with open(assertions_path, "w") as f:
        json.dump({"issue": 9849, "verdict": verdict, "assertions": results,
                   "tool_commit": args.tool_commit}, f, indent=2)

    print(json.dumps({"verdict": verdict,
                      "A2": a2, "A4": a4}, indent=2))
    return 0 if verdict in ("reproduced", "not-reproduced") else 2


if __name__ == "__main__":
    sys.exit(main())
