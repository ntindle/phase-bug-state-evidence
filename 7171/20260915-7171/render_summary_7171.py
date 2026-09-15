#!/usr/bin/env python3
"""Render summary.png for #7171 from the saved gap_results.json + run.json."""
import json, os
from PIL import Image, ImageDraw

EV = os.path.dirname(os.path.abspath(__file__))
res = json.load(open(os.path.join(EV, "gap_results.json")))

W, H = 1000, 900
BG, TEXT, DIM = (18, 20, 26), (235, 238, 245), (150, 160, 175)
ACCENT, GREEN, RED, YEL = (110, 180, 255), (110, 220, 140), (240, 120, 120), (240, 200, 110)

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)

def text(x, y, s, fill=TEXT):
    d.text((x, y), s, fill=fill)

y = 24
text(30, y, "#7171 - No way to set Companion in Build Deck after Draft"); y += 32
text(30, y, "client v0.83.0 (b7a59d460cc1) - draft deckbuilder", fill=DIM); y += 26
text(30, y, "Validated 2026-09-15 - Verdict: REPRODUCED", fill=RED); y += 40

text(30, y, "Assertions (executed shipped prop-gating logic, Node v24)"); y += 30
labels = {
    "A1_panel_gate_verbatim": "CommanderPanel companion gate + null default, verbatim",
    "A2_draft_never_passes_companion_props": "2x <CommanderPanel/> in LimitedDeckBuilder pass 0 companion props",
    "A3_gate_evaluates_false": "gate evaluates false -> companion section never renders",
    "A4_single_hardcoded_null": "only 'companion' token in LimitedDeckBuilder is hardcoded null",
    "A5_store_has_no_companion": "draftStore.ts has zero companion mentions (no state/action)",
    "A6_submit_never_carries_companion": "local submitDeck(names, []) - companion never supplied",
    "A7_control_constructed_has_companion": "CONTROL: constructed builder has handleSetCompanion flow",
    "A8_control_transport_supports_companion": "CONTROL: p2p transport DeckSeatPayload carries companion",
    "A9_no_companion_fix_LimitedDeckBuildertsx": "9 commits since report, 0 companion-mentioning (LimitedDeckBuilder)",
    "A9_no_companion_fix_draftStorets": "8 commits since report, 0 companion-mentioning (draftStore)",
}
for k, lab in labels.items():
    v = res[k]["status"]
    c = GREEN if v == "passed" else RED
    d.ellipse([30, y + 4, 42, y + 16], fill=c)
    text(50, y, f"{k}: {v}", fill=c); y += 22
    text(70, y, lab, fill=DIM); y += 26
y += 8

text(30, y, "The gap, verbatim:"); y += 28
text(50, y, "CommanderPanel.tsx:  {companionCandidates !== null && ( ... )}", fill=TEXT); y += 24
text(50, y, "CommanderPanel.tsx:  companionCandidates = null,   // default param", fill=TEXT); y += 24
text(50, y, "LimitedDeckBuilder:  renders <CommanderPanel> with commander props only", fill=TEXT); y += 24
text(50, y, "=> null !== null is false: the whole companion section is unreachable", fill=RED); y += 34

text(30, y, "Scope notes:", fill=ACCENT); y += 26
text(50, y, "- Reported path: Cube Draft -> draft Lutri -> Finish draft -> Build Deck", fill=DIM); y += 24
text(50, y, "- Lutri's companion clause is a UI/state gap, not a parser defect (triage)", fill=DIM); y += 24
text(50, y, "- Engine transport supports companion; only the draft client lacks the flow", fill=DIM); y += 24
text(50, y, "- No browser drive; verdict rests on shipped source + executed gating logic", fill=DIM); y += 24
text(30, y, "Limitation: constructed deck-builder cmcValues land note (#7170) is separate.", fill=YEL)

img.save(os.path.join(EV, "summary.png"))
print("wrote summary.png")
