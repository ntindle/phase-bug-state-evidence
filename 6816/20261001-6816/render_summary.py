#!/usr/bin/env python3
"""Render a faithful summary PNG for the #6816 re-verification (no game states)."""
import json, os
from PIL import Image, ImageDraw, ImageFont

EVID = os.path.expanduser("~/workspace/dev/phase-backfill/evidence/6816/20261001-6816")
run = json.load(open(os.path.join(EVID, "run.json")))
checks = run["assertions"]

W, H = 1000, 760
BG = (18, 20, 26)
FG = (235, 235, 235)
DIM = (150, 160, 175)
GREEN = (110, 220, 140)
ACCENT = (120, 170, 255)

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)
f_title = ImageFont.load_default(size=26)
f_head = ImageFont.load_default(size=18)
f_body = ImageFont.load_default(size=16)

y = 24
d.text((30, y), "phase-rs/phase #6816 — nightly AI gate redirect", font=f_title, fill=ACCENT)
y += 44
d.text((30, y), "Re-verification 2026-10-01 (run 20261001-6816)  |  CI workflow bug — no engine/game states", font=f_body, fill=DIM)
y += 30
d.text((30, y), "Verdict: NOT-REPRODUCED on current main  |  ai-gate.yml content sha 390fd3c5", font=f_head, fill=GREEN)
y += 34
d.line([(30, y), (W - 30, y)], fill=(60, 65, 80), width=1)
y += 14

rows = [
    ("A1", "mkdir -p target still immediately before BOTH gate redirects on main", checks["A1_workflow_fix_present_on_main"]),
    ("A2", "pre-fix: redirect into missing target/ -> exit 1, gate never ran", checks["A2_prefix_redirect_fails"]),
    ("A3", "pre-fix: drift-step cat against missing report -> exit 1 under bash -e", checks["A3_drift_cat_fails"]),
    ("A4", "current: mkdir -p target first -> gate runs, report written, drift ok", checks["A4_current_semantics_ok"]),
    ("A5", "current: ai-perf-gate step, same ordering -> gate runs", checks["A5_perf_step_ok"]),
    ("A6", "2026-09-29 nightly: gate step ran ~2h51m then cancelled (no instant redirect failure)", checks["A6_no_instant_redirect_failure_in_latest_nightly"]),
]
for tag, text, status in rows:
    color = GREEN if status == "passed" else (240, 120, 120)
    d.text((40, y), f"[{tag}]", font=f_head, fill=ACCENT)
    d.text((100, y), text, font=f_body, fill=FG)
    d.text((W - 120, y), status.upper(), font=f_head, fill=color)
    y += 34

y += 8
d.line([(30, y), (W - 30, y)], fill=(60, 65, 80), width=1)
y += 14
d.text((30, y), "Notes", font=f_head, fill=ACCENT); y += 28
notes = [
    "Fix #6815 (commit 56509356, maintainer path, 2026-07-30) still in place; workflow unchanged",
    "since the 2026-09-11 validation (same content sha 390fd3c5).",
    "The issue's failure signature was re-confirmed as genuine pre-fix bash semantics in a sandbox.",
    "Original 2026-07-30 nightly failure is not disputed; this run only observes current state —",
    "not a fix claim.",
]
for n in notes:
    d.text((40, y), n, font=f_body, fill=DIM); y += 24

y += 10
d.text((30, y), "Evidence: ntindle/phase-bug-state-evidence 6816/20261001-6816", font=f_body, fill=DIM)
y += 26
d.text((30, y), "Server identity (release check, reused): v0.98.0 / 61e8550 / protocol 94 on 127.0.0.1:9374", font=f_body, fill=DIM)

img.save(os.path.join(EVID, "summary.png"))
print("summary.png written")
