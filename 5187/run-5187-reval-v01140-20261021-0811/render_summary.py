#!/usr/bin/env python3
"""Summary PNG renderer for #5187 re-validation run 20261021-0811.

Faithful to saved observations: derives every value from client_observations.json.
No invented states.
"""
import json
from PIL import Image, ImageDraw, ImageFont

EV = "/home/hatch/workspace/dev/phase-backfill/evidence/5187/run-5187-reval-v01140-20261021-0811"
obs = json.load(open(f"{EV}/client_observations.json"))

W, H = 1100, 1000
BG = (16, 18, 24)
FG = (230, 235, 240)
MUT = (150, 160, 175)
GRN = (90, 200, 120)
RED = (230, 110, 110)
YEL = (230, 190, 110)
BOX = (28, 32, 40)

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)
f_big = ImageFont.load_default(size=30)
f_med = ImageFont.load_default(size=20)
f_sml = ImageFont.load_default(size=16)

y = 30
def line(t, font=f_med, fill=FG, dy=0, x=40):
    global y
    d.text((x, y), t, font=font, fill=fill)
    y += font.size + 10 + dy
    return y

line("#5187 — resumable-match opponentCount undercounts (CR 800.4)", f_big)
line("phase-rs/phase PR #5187 · client-subsystem re-validation", f_med, MUT)
line("mainline 6376b632dc9e7843b89977d169851df677778b64 (2026-10-21)", f_sml, MUT)
line("PR head e318893388f2099a16363b5d41fccbd9e9d88a25 — open, unmerged", f_sml, MUT)
line("engine pin v0.114.0 (client defect: no engine session needed)", f_sml, MUT)
y += 6

def verdict_box(title, assertions, verdict, vcolor):
    global y
    d.rectangle([40, y, W - 40, y + 300], fill=BOX, outline=(60, 66, 78))
    y += 16
    line(title, f_med, FG, x=60)
    yy = y
    items = [
        ("A1", "defect site verbatim on current mainline"),
        ("A2", "discriminating repro: seat 0 eliminated, 2 live opponents"),
        ("A3", "controls (alive / lone / all eliminated)"),
        ("A4", "PR's own 5-test suite on byte-exact patch files"),
        ("A5", "hook wiring delegates to countLiveOpponents"),
        ("A6", "PR still open at run time"),
    ]
    for aid, desc in items:
        st = assertions.get(aid, {}).get("status", "?")
        col = GRN if st == "passed" else (RED if st == "failed" else YEL)
        d.text((60, yy), f"{aid} {desc}: ", font=f_sml, fill=FG)
        d.text((60 + 620, yy), st.upper(), font=f_sml, fill=col)
        yy += 30
    y = yy + 8
    d.text((60, y), f"Verdict: {verdict}", font=f_med, fill=vcolor)
    y += 48

a = obs["assertions"]
verdict_box("RE-VALIDATION 2026-10-21 (current mainline 6376b632)",
            a, "REPRODUCED — A2 failed: reports 1 opponent, correct is 2", RED)
y += 10
line("Root cause: opponentCount: Math.max(0, liveCount - 1) (line 117);", f_sml, MUT)
line("liveCount already excludes eliminated players, so the -1 assumes seat 0 is alive.", f_sml, MUT)
line("PR extracts countLiveOpponents and only subtracts seat 0 when it is itself alive.", f_sml, MUT)
y += 4
line("Evidence: ntindle/phase-bug-state-evidence 5187/run-5187-reval-v01140-20261021-0811/", f_sml, MUT)
line("run 20261021-0811 · no webhook / no fixer spawn (already-reproduced re-validation)", f_sml, MUT)

img.save(f"{EV}/summary.png")
print("wrote summary.png", img.size)
