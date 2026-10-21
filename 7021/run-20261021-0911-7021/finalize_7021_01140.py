#!/usr/bin/env python3
"""Finalize #7021 re-validation run run-20261021-0911-7021 (protocol 153,
v0.114.0).

The scenario ran the full game to the verdict (reproduced) and wrote
pre.json/post.json/parse_fear.json/run.json/summary.png/wire_log.jsonl/
scenario_run.log/target_sel_1.json, but finish() had two driver bugs:

1. server_run_dir() matched the launcher shell's own ps line (its --games-db
   arg is the literal unexpanded "$RUN_DIR/games.db", so db.split("/runs/")
   raised IndexError) instead of the real phase-server-slim process ->
   server.log was never copied into the evidence dir.
2. The final validation re-hashed scenario_run.log AFTER say() appended
   "wrote manifest.sha256" to it (the log file is still open during
   finish()), so the manifest self-check crashed with
   AssertionError: scenario_run.log.

This script completes the evidence dir:
- copies server.log from the real run dir runs/run-20261021-0911-7021/
- patches run.json's notes + finalize_note (honest record of the repair)
- copies itself into the evidence dir
- recomputes manifest.sha256 over the complete final file set (logs are
  closed now, hashes are stable)
- validates: every JSON parses, PNG readable, hashes match the manifest
"""
import hashlib
import json
import os
import shutil

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 7021
RUN_ID = "run-20261021-0911-7021"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
SRV_RUN = "run-20261021-0911-7021"
DRIVER = f"{BACKFILL}/driver"


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    # 1. server.log from the real run dir (the launcher shell is dead; the
    #    phase-server-slim process itself is the backfill-owned server).
    src = f"{BACKFILL}/runs/{SRV_RUN}/server.log"
    assert os.path.exists(src), f"missing {src}"
    shutil.copy(src, f"{EVDIR}/server.log")
    print(f"copied server.log ({os.path.getsize(src)} bytes)")

    # 2. patch run.json notes (honest record: the scenario's finish() could
    #    not locate the run dir; server.log was added by this finalize).
    rp = f"{EVDIR}/run.json"
    run = json.load(open(rp))
    notes = [n for n in run.get("notes", [])
             if "server.log copy skipped" not in n]
    notes.append(
        "server.log copied into evidence by finalize_7021_01140.py (the "
        "scenario's server_run_dir() matched the launcher shell's own ps "
        "line whose --games-db arg was the literal unexpanded "
        "$RUN_DIR/games.db; the real phase-server-slim process served "
        "runs/run-20261021-0911-7021).")
    run["notes"] = notes
    run["finalize_note"] = (
        "finish() crashed on two driver bugs after computing all assertions "
        "and writing run.json/summary.png: (1) server_run_dir() matched the "
        "launcher shell's own ps line (unexpanded $RUN_DIR/games.db) so "
        "server.log was never copied; (2) the manifest self-check re-hashed "
        "scenario_run.log after say() appended to the still-open log file. "
        "This script copies server.log from runs/run-20261021-0911-7021/, "
        "patches the notes, and recomputes manifest.sha256 with all log "
        "files closed. Assertion values are the scenario's own "
        "(scenario_run.log lines match A1..A7 line-for-line); verdict "
        "reproduced is unchanged.")
    with open(rp, "w") as f:
        json.dump(run, f, indent=1)
        f.write("\n")
    print("patched run.json notes")

    # 3. copy self + scenario is already in evidence
    assert os.path.exists(f"{EVDIR}/scenario_7021_01140.py")
    shutil.copy(os.path.abspath(__file__), f"{EVDIR}/finalize_7021_01140.py")
    print("copied finalize_7021_01140.py into evidence")

    # 4. recompute manifest over the complete final file set (logs closed)
    files = ["pre.json", "post.json", "parse_fear.json", "run.json",
             "scenario_7021_01140.py", "finalize_7021_01140.py",
             "wire_log.jsonl", "scenario_run.log", "server.log",
             "summary.png", "target_sel_1.json"]
    lines = []
    for fn in files:
        p = f"{EVDIR}/{fn}"
        assert os.path.exists(p), f"MISSING {fn}"
        lines.append(f"{sha256_of_file(p)}  {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"wrote manifest.sha256 ({len(lines)} files)")

    # 5. validation
    for fn in ("pre.json", "post.json", "parse_fear.json", "run.json",
               "target_sel_1.json"):
        json.load(open(f"{EVDIR}/{fn}"))
    from PIL import Image
    Image.open(f"{EVDIR}/summary.png").verify()
    for line in open(f"{EVDIR}/manifest.sha256").read().strip().splitlines():
        h, fn = line.split("  ")
        assert sha256_of_file(f"{EVDIR}/{fn}") == h, fn
    print("validation: JSON parses, PNG readable, hashes match")

    print("VERDICT:", run["verdict"])


if __name__ == "__main__":
    main()
