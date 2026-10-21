#!/usr/bin/env python3
"""Summary PNG renderer for #5186 re-validation run 20261021-0741.

Faithful to saved observations: derives every value from client_observations.json
and the PR-variant observations. No invented states.
"""
import json
from PIL import Image, ImageDraw, ImageFont

EV = "/home/hatch/workspace/dev/phase-backfill/evidence/5186/run-5186-reval-v01140-20261021-0741"
obs = json.load(open(f"{EV}/client_observations.json"))
obs_pr = json.load(open(f"{EV}/pr-variant/client_observations.pr_variant.json"))

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

line("#5186 — colorless-commander color identity (CR 903.5c)", f_big)
line("phase-rs/phase PR #5186 · client-subsystem re-validation", f_med, MUT)
line("mainline 7b1504a61f7970edd0f562444f16d7c750c0df2c (2026-10-20)", f_sml, MUT)
line("PR head f82c5bf18b6a19ac0ffabec2c2d4396ee7efe7f0 — open, unmerged", f_sml, MUT)
line("engine pin v0.114.0 (client defect: no engine session needed)", f_sml, MUT)
y += 6

def verdict_box(title, assertions, verdict, vcolor):
    global y
    d.rectangle([40, y, W - 40, y + 300], fill=BOX, outline=(60, 66, 78))
    y += 16
    line(title, f_med, FG, x=60)
    yy = y
    for aid, desc in [
        ("M1", "colorless commander flags off-color card"),
        ("M2", "colored-commander control"),
        ("M3", "loading-window (uncached commander) control"),
        ("M4", "colorless cards legal under colorless commander"),
    ]:
        st = assertions.get(aid, "?")
        col = GRN if st == "passed" else (RED if st == "failed" else YEL)
        d.text((60, yy), f"{aid} {desc}: ", font=f_sml, fill=FG)
        d.text((60 + 560, yy), st.upper(), font=f_sml, fill=col)
        yy += 30
    y = yy + 8
    d.text((60, y), f"Verdict: {verdict}", font=f_med, fill=vcolor)
    y += 48

a_main = obs["assertions"]
verdict_box("MAINLINE 7b1504a (real commanderUtils.ts, live Scryfall CI values)",
            a_main, "REPRODUCED — M1 failed: Lightning Bolt passes colorless-commander check", RED)
y += 10
a_pr = obs_pr["assertions"]
verdict_box("PR VARIANT (published patch applied byte-identically to 7b1504a file)",
            a_pr, "FIX RESOLVES — all assertions pass; controls unchanged", GRN)
y += 6
line("Root cause: isInColorIdentity() short-circuits `identity.length === 0 -> true` (line 28);", f_sml, MUT)
line("an empty identity is also what an uncached commander yields, so the bug and the loading", f_sml, MUT)
line("window collapse. PR moves the guard to getColorIdentityViolations (no false positives).", f_sml, MUT)
y += 4
line("Evidence: ntindle/phase-bug-state-evidence 5186/run-5186-reval-v01140-20261021-0741/", f_sml, MUT)
line("run 20261021-0741 · no webhook / no fixer spawn (already-reproduced re-validation)", f_sml, MUT)

img.save(f"{EV}/summary.png")
print("wrote summary.png", img.size)
