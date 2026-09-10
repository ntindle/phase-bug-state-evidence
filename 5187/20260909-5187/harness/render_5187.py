#!/usr/bin/env python3
"""Render evidence summary PNG for issue #5187 (client logic, no game states)."""
import json
from PIL import Image, ImageDraw

W, H = 1000, 760
BG = (18, 20, 26)
PANEL = (26, 30, 38)
TEXT = (235, 238, 245)
DIM = (150, 160, 175)
ACCENT = (110, 180, 255)
GREEN = (110, 220, 140)
RED = (240, 120, 120)

ED = '/home/hatch/workspace/dev/phase-backfill/evidence/5187/20260909-5187'
run = json.load(open(ED + '/run.json'))
a = json.load(open(ED + '/assertions.json'))

img = Image.new('RGB', (W, H), BG)
d = ImageDraw.Draw(img)
y = 28

def txt(x, y, s, fill=TEXT, size=24):
    d.text((x, y), s, fill=fill)

txt(36, y, 'phase-rs/phase #5187 — bug-state evidence', size=30); y += 46
txt(36, y, 'count opponents correctly when the local seat is eliminated', fill=DIM, size=20); y += 38
txt(36, y, f"run 20260909-5187 | mainline {run['client']['mainline_commit'][:12]} (2026-09-10) | client-only", fill=DIM, size=18); y += 40

verdict = run['verdict'].upper()
vc = RED if run['verdict'] == 'reproduced' else GREEN
d.rectangle([36, y, 36 + 220, y + 40], fill=(50, 20, 22))
txt(52, y + 7, 'VERDICT: ' + verdict, fill=vc, size=22); y += 62

txt(36, y, 'Shipped logic under test (useResumables.ts, verbatim lines):', fill=ACCENT, size=18); y += 30
d.rectangle([36, y, W - 36, y + 76], fill=PANEL)
for ln in run['shipped_lines_tested']:
    txt(52, y + 8, ln.strip(), size=17); y += 30
y += 16

txt(36, y, 'Fixtures → observed opponentCount (expected in parens):', fill=ACCENT, size=18); y += 34
for cid, c in a.items():
    ok = c['status'] == 'passed'
    color = GREEN if ok else RED
    fx = ' '.join(f"p{x['id']}:{'dead' if x['is_eliminated'] else 'live'}" for x in c['fixture'])
    d.rectangle([36, y, W - 36, y + 58], fill=PANEL)
    mark = 'PASS' if ok else 'FAIL'
    txt(52, y + 6, f"[{mark}] {cid}", fill=color, size=18)
    txt(52, y + 30, f"{fx}  ->  {c['observed']}  (expected {c['expected']})", fill=TEXT, size=17)
    y += 66

y += 6
txt(36, y, 'Key observation:', fill=ACCENT, size=18); y += 30
d.rectangle([36, y, W - 36, y + 60], fill=(50, 20, 22))
txt(52, y + 8, 'Seat 0 eliminated, 2 live opponents -> reports 1 opponent (must be 2).', fill=RED, size=18)
txt(52, y + 32, 'Eliminated seat 0 is not in liveCount; the -1 undercounts by one.', fill=DIM, size=16)
y += 72

txt(36, y, 'Proposed fix (liveOpponents.ts, PR head e3188933) is NOT on mainline:', fill=ACCENT, size=18); y += 30
txt(52, y, 'useResumables.ts lines 112/117 still carry the buggy formula; liveOpponents.ts absent.', fill=DIM, size=17); y += 30
txt(52, y, 'Limitations: shipped expressions evaluated against fixtures in node; full hook/IndexedDB path not run.', fill=DIM, size=17)

img.save(ED + '/summary.png')
print('saved', ED + '/summary.png', img.size)
