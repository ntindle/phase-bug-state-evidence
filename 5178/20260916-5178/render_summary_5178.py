#!/usr/bin/env python3
"""Render evidence summary PNG for issue #5178 (parseJoinCode bracketed-IPv6 fix PR).
Usage: render_summary_5178.py <evidence_dir>
Reads run.json and client_observations.json. Writes summary.png derived from the
saved observations. No gameplay states: this is a client-subsystem report.
"""
import json
import os
import sys
import textwrap

from PIL import Image, ImageDraw

W, H = 1100, 980
BG = (18, 20, 26)
TEXT = (235, 238, 245)
DIM = (150, 160, 175)
ACCENT = (110, 180, 255)
GREEN = (110, 220, 140)
RED = (240, 120, 120)


def load(p):
    with open(p) as f:
        return json.load(f)


def main():
    evdir = sys.argv[1]
    run = load(os.path.join(evdir, "run.json"))
    obs = load(os.path.join(evdir, "client_observations.json"))
    a = obs["assertions"]

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #5178 - parseJoinCode mangles port-less bracketed IPv6 "
                    "(fix PR, open/unmerged)", fill=ACCENT)
    y += 26
    up = run["underlying_pr"]
    d.text((24, y),
           "client mainline {} | PR head {} (unmerged) "
           "| run {} | {} "
           "| verdict: {}".format(up["base"][:9], up["head"][:9],
                                  run["run_id"], run["validated_at"],
                                  run["verdict"]), fill=DIM)
    y += 30
    d.text((24, y), "Contract: parseJoinCode(\"ABC123@[::1]\") must yield wss://[::1]/ws,", fill=TEXT)
    y += 22
    d.text((24, y), "not split the bracketed IPv6 literal at its internal last colon.", fill=TEXT)
    y += 40

    rows = [
        ("A1 mainline: 'ABC123@[::1]' MANGLED (bug reproduced)",
         a["A1_mainline_bug"]),
        ("A2 mainline: bracketed IPv6 WITH port already correct (control)",
         a["A2_mainline_with_port_control"]),
        ("A3 PR variant: 'ABC123@[::1]' -> correct address",
         a["A3_pr_fix"]),
        ("A4 PR variant: no regressions on 5 other join-code formats",
         a["A4_pr_no_regression"]),
        ("A5 bare code: no serverAddress on both variants",
         a["A5_bare_code"]),
    ]
    for label, ass in rows:
        color = GREEN if ass["status"] == "passed" else RED
        d.text((24, y), label, fill=color)
        y += 24
        for ln in str(ass["detail"]).split("\n"):
            for chunk in textwrap.wrap("observed: " + ln, 105):
                d.text((44, y), chunk, fill=DIM)
                y += 20
        y += 12

    d.text((24, y), "Bug mechanics (mainline):", fill=TEXT)
    y += 24
    for chunk in textwrap.wrap(obs["bug_mechanics"], 105):
        d.text((44, y), chunk, fill=RED)
        y += 20
    y += 16
    d.text((24, y), "The malformed URL visually resembles the correct one:", fill=TEXT)
    y += 22
    d.text((44, y), "got:      wss://[::1/ws   <- broken", fill=RED)
    y += 20
    d.text((44, y), "expected: wss://[::1]/ws  <- correct", fill=GREEN)
    y += 36
    d.text((24, y), "Files: run.json, client_observations.json, variant_mainline.js,", fill=DIM)
    y += 22
    d.text((24, y), "variant_pr.js, 5178_serverDetection_base.ts,", fill=DIM)
    y += 22
    d.text((24, y), "5178_serverDetection_pr_head.ts, build_5178_variants.py,", fill=DIM)
    y += 22
    d.text((24, y), "harness_5178.js, scenario_run.log, manifest.sha256", fill=DIM)

    out = os.path.join(evdir, "summary.png")
    img.save(out)
    print("wrote", out, img.size)


if __name__ == "__main__":
    main()
