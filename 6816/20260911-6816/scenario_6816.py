#!/usr/bin/env python3
"""Scenario for phase-rs/phase #6816.

"Nightly AI gate + decision-cost monitors fail on a redirect, so the gate never runs"

Reported outcome (2026-07-30 nightly run on main):
  `cargo ai-gate --full-suite --games 100 > target/ai-gate-report.md` never ran:
  on a cardgen cache hit `target/` did not exist, bash opened the redirect
  BEFORE running cargo, the step failed with
  "target/ai-gate-report.md: No such file or directory" (exit 1), and the
  drift-issue step then died on `cat target/ai-gate-report.md` under bash -e.

Assertions test the mechanism (pre-fix semantics, reduced exactly as the issue
describes) and the CURRENT state of the workflow on main:
  A2/A3 - pre-fix semantics: the redirect fails before the command runs.
  A4     - current semantics: mkdir -p first -> command runs, report written.
  A5     - current ai-gate.yml on main contains mkdir -p before both redirects.
  A6     - latest nightly main run: "Run full AI gate" actually executed for
           hours (redirect did not fail instantly).

This issue involves no engine behavior: no phase-server game is needed.
Evidence "states" are the sandbox filesystem states plus the pinned workflow.
"""
import base64
import json
import os
import shutil
import subprocess
import sys
import time

ISSUE = 6816
RUN_ID = "20260911-6816"
EVIDIR = os.path.expanduser(f"~/workspace/dev/phase-backfill/evidence/{ISSUE}/{RUN_ID}")
GH = os.path.expanduser("~/workspace/skills/github/bin/gh-api.py")
LOG = []


def log(msg):
    print(msg, flush=True)
    LOG.append(msg)


def run_bash(script, cwd):
    """Run a script under the same bash -e shell Actions uses; capture outcome."""
    p = subprocess.run(["bash", "-e", "-c", script], cwd=cwd,
                       capture_output=True, text=True)
    return {"exit": p.returncode, "stdout": p.stdout, "stderr": p.stderr}


def gh(method, path):
    out = subprocess.run([sys.executable, GH, method, path],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"gh-api {method} {path} failed: {out.stderr[:500]}")
    return json.loads(out.stdout)


assertions = []
transcripts = {}


def record(name, ok, observed, expected):
    assertions.append({"id": name, "status": "passed" if ok else "failed",
                       "observed": observed, "expected": expected})
    log(f"{name}: {'PASSED' if ok else 'FAILED'} - {observed}")


def main():
    if os.path.exists(EVIDIR):
        shutil.rmtree(EVIDIR)
    os.makedirs(EVIDIR)
    log(f"evidence dir: {EVIDIR}")
    log(f"run_id={RUN_ID} issue={ISSUE}")

    # ---- A1: setup / pre state -------------------------------------------
    pre_box = os.path.join(EVIDIR, "sandbox_prefix")
    fix_box = os.path.join(EVIDIR, "sandbox_fixed")
    os.makedirs(pre_box)
    os.makedirs(fix_box)
    no_target = not os.path.exists(os.path.join(pre_box, "target"))
    pre = {"sandbox_prefix": {"cwd": pre_box,
                              "target_exists": os.path.exists(os.path.join(pre_box, "target")),
                              "contents": os.listdir(pre_box)},
           "shell": "bash -e (same semantics as the Actions step shell)"}
    record("A1_setup_ok", no_target,
           "sandbox_prefix/ has no target/ dir (cache-hit simulation)",
           "sandbox pre-state with target/ absent, like a cache-hit job")

    # ---- A2: pre-fix step semantics --------------------------------------
    # Exact shape of the broken step: the redirect is attached to the whole
    # gate invocation, so bash opens it BEFORE any of the gate runs.
    pre_cmd = '(touch gate_ran.marker; echo \'{"win_rate":0.9}\') > target/ai-gate-report.md'
    t2 = run_bash(pre_cmd, pre_box)
    transcripts["prefix_gate_step"] = {"cmd": pre_cmd, **t2,
                                       "note": "pre-fix step semantics (no mkdir first)"}
    marker = os.path.exists(os.path.join(pre_box, "gate_ran.marker"))
    report = os.path.join(pre_box, "target", "ai-gate-report.md")
    report_exists = os.path.exists(report)
    ok2 = (t2["exit"] == 1 and "No such file or directory" in t2["stderr"]
           and not marker and not report_exists)
    record("A2_prefix_redirect_fails", ok2,
           f"exit={t2['exit']}, stderr={t2['stderr'].strip()!r}, "
           f"gate_ran.marker={'present' if marker else 'absent'} (gate never ran), "
           f"report={'present' if report_exists else 'absent'}",
           "exit 1, 'No such file or directory', gate marker absent (gate never ran), report absent")

    # ---- A3: the drift-step failure --------------------------------------
    drift_cmd = 'body="$(cat target/ai-gate-report.md)"; echo "$body"'
    t3 = run_bash(drift_cmd, pre_box)
    transcripts["prefix_drift_step"] = {"cmd": drift_cmd, **t3,
                                        "note": "pre-fix drift-issue step semantics"}
    ok3 = t3["exit"] == 1
    record("A3_prefix_drift_step_fails", ok3,
           f"exit={t3['exit']}, stderr={t3['stderr'].strip()!r}",
           "exit 1: cat of the never-created report fails under bash -e (no drift issue filed)")

    # ---- A4: fixed semantics ----------------------------------------------
    fix_cmd = ('mkdir -p target; '
               '(touch gate_ran.marker; echo \'{"win_rate":0.9}\') > target/ai-gate-report.md; '
               'body="$(cat target/ai-gate-report.md)"; echo "drift body: $body"')
    t4 = run_bash(fix_cmd, fix_box)
    transcripts["fixed_step"] = {"cmd": fix_cmd, **t4,
                                 "note": "current semantics: mkdir -p target before redirect"}
    marker4 = os.path.exists(os.path.join(fix_box, "gate_ran.marker"))
    report4 = os.path.join(fix_box, "target", "ai-gate-report.md")
    report_ok = os.path.exists(report4) and os.path.getsize(report4) > 0
    ok4 = t4["exit"] == 0 and marker4 and report_ok and "drift body:" in t4["stdout"]
    record("A4_fixed_semantics_gate_runs", ok4,
           f"exit={t4['exit']}, gate_ran.marker={'present' if marker4 else 'absent'}, "
           f"report={'non-empty' if report_ok else 'absent/empty'}",
           "exit 0, gate marker present (gate ran), report written non-empty")

    # ---- A5: current ai-gate.yml on main -----------------------------------
    wf = gh("GET", "/repos/phase-rs/phase/contents/.github/workflows/ai-gate.yml?ref=main")
    blob_sha = wf["sha"]
    wtext = base64.b64decode(wf["content"]).decode()
    with open(os.path.join(EVIDIR, "workflow_snapshot.txt"), "w") as f:
        f.write(f"# ai-gate.yml @ main, content sha {blob_sha}\n")
        f.write("# lines mentioning target/, mkdir, or the gate report redirects\n")
        for i, line in enumerate(wtext.splitlines(), 1):
            if "target/" in line or "mkdir" in line:
                f.write(f"{i}: {line}\n")
    lines = wtext.splitlines()
    def mkdir_guards_redirect(redirect_marker):
        for i, line in enumerate(lines):
            if redirect_marker in line:
                # mkdir -p target must appear within the preceding lines of the step
                prev = "\n".join(lines[max(0, i-6):i])
                if "mkdir -p target" in prev:
                    return True
        return False
    gate_ok = mkdir_guards_redirect("> target/ai-gate-report.md")
    perf_ok = mkdir_guards_redirect("> target/ai-perf-gate-report.md")
    ok5 = gate_ok and perf_ok
    record("A5_workflow_has_mkdir", ok5,
           f"ai-gate.yml@{blob_sha[:8]}: mkdir guards ai-gate redirect={gate_ok}, perf redirect={perf_ok}",
           "mkdir -p target immediately before both gate redirects on main")

    # ---- A6: the nightly gate actually executes on main --------------------
    runs = gh("GET", "/repos/phase-rs/phase/actions/workflows/ai-gate.yml/runs?per_page=5")
    main_run = next(r for r in runs["workflow_runs"] if r["head_branch"] == "main")
    run_id_num = main_run["id"]
    jobs = gh("GET", f"/repos/phase-rs/phase/actions/runs/{run_id_num}/jobs")
    job = next(j for j in jobs["jobs"] if j["name"] == "Nightly AI gate drift monitor")
    step = next(s for s in job["steps"] if s["name"] == "Run full AI gate")
    from datetime import datetime, timezone
    def dt(x): return datetime.fromisoformat(x.replace("Z", "+00:00"))
    dur_s = (dt(step["completed_at"]) - dt(step["started_at"])).total_seconds()
    transcripts["latest_nightly_main"] = {
        "run": main_run["html_url"], "run_conclusion": main_run["conclusion"],
        "job": job["name"], "job_conclusion": job["conclusion"],
        "step": step["name"], "step_conclusion": step["conclusion"],
        "step_duration_s": dur_s,
        "note": "a redirect failure would end the step in seconds with conclusion failure"
    }
    # The reported failure signature: step dies instantly (exit 1) at the redirect.
    ok6 = dur_s >= 600 and step["conclusion"] != "failure"
    record("A6_nightly_gate_executes", ok6,
           f"main run {run_id_num}: 'Run full AI gate' ran {dur_s/3600:.1f}h, "
           f"conclusion={step['conclusion']} (redirect did not fail instantly)",
           "gate step runs long (binary executes); no instant redirect failure")

    # ---- states / artifacts -------------------------------------------------
    post = {
        "sandbox_prefix": {"target_exists": os.path.exists(os.path.join(pre_box, "target")),
                           "gate_marker": os.path.exists(os.path.join(pre_box, "gate_ran.marker")),
                           "report": os.path.exists(os.path.join(pre_box, "target", "ai-gate-report.md"))},
        "sandbox_fixed": {"target_exists": os.path.exists(os.path.join(fix_box, "target")),
                          "gate_marker": os.path.exists(os.path.join(fix_box, "gate_ran.marker")),
                          "report_bytes": (os.path.getsize(os.path.join(fix_box, "target", "ai-gate-report.md"))
                                           if os.path.exists(os.path.join(fix_box, "target", "ai-gate-report.md")) else 0)},
        "workflow": {"file": ".github/workflows/ai-gate.yml", "ref": "main", "content_sha": blob_sha},
        "fix_commit": {"sha": "56509356", "message": "ci(ai-gate): create target/ before redirecting the nightly reports (#6815)",
                       "date": "2026-07-30T17:19:08Z"},
        "latest_nightly_main": transcripts["latest_nightly_main"],
    }
    with open(os.path.join(EVIDIR, "pre.json"), "w") as f:
        json.dump(pre, f, indent=2)
    with open(os.path.join(EVIDIR, "post.json"), "w") as f:
        json.dump(post, f, indent=2)
    with open(os.path.join(EVIDIR, "assertions.json"), "w") as f:
        json.dump(assertions, f, indent=2)
    with open(os.path.join(EVIDIR, "transcripts.json"), "w") as f:
        json.dump(transcripts, f, indent=2)

    verdict = "not-reproduced" if all(a["status"] == "passed" for a in assertions) else "mixed"
    run_doc = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "run_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "scope": "Nightly AI-gate workflow redirect failure: reproduce the reported failure mode (bash opens the redirect before cargo runs when target/ is missing) and check whether it still occurs on current main. No engine behavior involved; no phase-server game needed.",
        "verdict": verdict,
        "result": "; ".join(f"{a['id']}: {a['status']} ({a['observed']})" for a in assertions),
        "server": "not applicable (CI workflow bug; no engine test)",
        "files": sorted(os.listdir(EVIDIR)),
    }
    with open(os.path.join(EVIDIR, "run.json"), "w") as f:
        json.dump(run_doc, f, indent=2)
    with open(os.path.join(EVIDIR, "scenario_run.log"), "w") as f:
        f.write("\n".join(LOG) + "\n")
    shutil.copy(__file__, os.path.join(EVIDIR, f"scenario_{ISSUE}.py"))

    render_png()
    write_manifest()
    log(f"verdict: {verdict}")
    return 0 if verdict == "not-reproduced" else 1


def render_png():
    """Render summary.png from the saved assertions (1000x760)."""
    from PIL import Image, ImageDraw
    W, H = 1000, 760
    BG, PANEL, TEXT, DIM = (18, 20, 26), (26, 30, 38), (235, 238, 245), (150, 160, 175)
    GREEN, RED = (110, 220, 140), (240, 120, 120)
    acc = {a["id"]: a for a in assertions}
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 24
    d.text((24, y), f"#6816 — Nightly AI gate redirect bug", fill=TEXT)
    y += 26
    d.text((24, y), "cargo ai-gate ... > target/ai-gate-report.md with no target/ (cache hit)", fill=DIM)
    y += 44
    ver = "not-reproduced on current main" if all(a["status"] == "passed" for a in assertions) else "MIXED"
    d.rectangle([24, y, 24 + 480, y + 34], fill=(40, 60, 44) if ver.startswith("not") else (70, 40, 40))
    d.text((32, y + 9), f"verdict: {ver}  (not a fix claim)", fill=TEXT)
    y += 52
    for aid in ["A1_setup_ok", "A2_prefix_redirect_fails", "A3_prefix_drift_step_fails",
                "A4_fixed_semantics_gate_runs", "A5_workflow_has_mkdir", "A6_nightly_gate_executes"]:
        a = acc[aid]
        col = GREEN if a["status"] == "passed" else RED
        d.rectangle([24, y, W - 24, y + 66], fill=PANEL)
        d.text((32, y + 6), f"{aid}  [{a['status'].upper()}]", fill=col)
        obs = a["observed"]
        while len(obs) > 118:
            obs = obs[:115] + "..."
        d.text((32, y + 28), "obs: " + obs, fill=DIM)
        y += 76
    d.text((24, H - 30), f"run {RUN_ID} · evidence {ISSUE}/{RUN_ID} · mkdir fix in ai-gate.yml @ main",
           fill=DIM)
    img.save(os.path.join(EVIDIR, "summary.png"))
    log("summary.png written")


def write_manifest():
    import hashlib
    lines = []
    for root, _ds, names in os.walk(EVIDIR):
        for n in sorted(names):
            if n == "manifest.sha256":
                continue
            full = os.path.join(root, n)
            rel = os.path.relpath(full, EVIDIR)
            h = hashlib.sha256()
            with open(full, "rb") as f:
                h.update(f.read())
            lines.append(f"{h.hexdigest()}  {rel}")
    with open(os.path.join(EVIDIR, "manifest.sha256"), "w") as f:
        f.write("\n".join(lines) + "\n")
    log(f"manifest.sha256 written ({len(lines)} files)")


if __name__ == "__main__":
    sys.exit(main())
