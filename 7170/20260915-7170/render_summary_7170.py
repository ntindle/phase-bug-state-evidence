#!/usr/bin/env python3
"""Render summary.png for #7170 from the saved curve_results.json + run.json."""
import json, os
from PIL import Image, ImageDraw

EV = os.path.dirname(os.path.abspath(__file__))
run = json.load(open(os.path.join(EV, "run.json")))
res = json.load(open(os.path.join(EV, "curve_results.json")))

W, H = 1000, 720
BG, PANEL, TEXT, DIM = (18, 20, 26), (26, 30, 38), (235, 238, 245), (150, 160, 175)
ACCENT, GREEN, RED, BAR = (110, 180, 255), (110, 220, 140), (240, 120, 120), (90, 200, 250)

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)

def text(x, y, s, fill=TEXT, size=16):
    d.text((x, y), s, fill=fill)

y = 24
text(30, y, "#7170 — Draft mana curve counts drafted lands at 0", size=20); y += 34
text(30, y, "client v0.83.0 (b7a59d460cc1) · component client/src/components/draft/ManaCurve.tsx", fill=DIM); y += 26
text(30, y, "Validated 2026-09-15 · Verdict: not-reproduced", fill=ACCENT); y += 40

# Assertion table
text(30, y, "Assertions (executed shipped logic in Node v24)", size=18); y += 28
for k, v in res["assertions"].items():
    c = GREEN if v == "passed" else RED
    d.ellipse([30, y + 4, 42, y + 16], fill=c)
    text(50, y, f"{k}: {v}", fill=c); y += 26
y += 12

# Bucket comparison
text(30, y, "Mana-curve buckets — main deck of 10 (5 lands, incl. 0-cost Memnite)", size=18); y += 30
labels = ["0", "1", "2", "3", "4", "5", "6+"]
cur = res["current_v0830"]; pre = res["pre_fix_7b0a1a2c"]
text(30, y, "bucket", fill=DIM); text(180, y, "v0.83.0 (current)", fill=DIM); text(430, y, "pre-fix 7b0a1a2c", fill=DIM); y += 24
maxc = max(max(cur.values()), max(pre.values()), 1)
for lb in labels:
    text(30, y, lb)
    w1 = int(cur[lb] / maxc * 180); w2 = int(pre[lb] / maxc * 180)
    d.rectangle([180, y + 2, 180 + max(w1, 2 if cur[lb] else 0), y + 18], fill=BAR)
    text(180 + w1 + 8, y, str(cur[lb]))
    d.rectangle([430, y + 2, 430 + max(w2, 2 if pre[lb] else 0), y + 18], fill=(120, 120, 130))
    text(430 + w2 + 8, y, str(pre[lb]))
    y += 26
y += 10
text(30, y, "Current: 0-bucket = 1 (Memnite, 0-cost nonland). 5 lands excluded.", fill=GREEN); y += 26
text(30, y, "Pre-fix: 0-bucket = 6 (Memnite + 5 lands) — the reported symptom.", fill=DIM); y += 34

text(30, y, "Fix: ef0ada6a (2026-08-28, after the 2026-08-10 report) added", fill=DIM); y += 24
text(30, y, '  if (card === undefined || /\\bland\\b/i.test(card.type_line)) continue;', fill=TEXT); y += 26
text(30, y, "Upstream regression test asserts the 0 meter reads 0 with a land in the deck.", fill=DIM); y += 30
text(30, y, "Limitation: constructed deck-builder cmcValues path still unfiltered (static read, not exercised).", fill=(240, 200, 110))

img.save(os.path.join(EV, "summary.png"))
print("wrote summary.png")
