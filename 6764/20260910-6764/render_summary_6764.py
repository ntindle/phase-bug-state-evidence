#!/usr/bin/env python3
"""Render the #6764 evidence summary PNG from the saved run.json assertions."""
import json, os
from PIL import Image, ImageDraw

ROOT = os.path.expanduser(
    "~/workspace/dev/phase-backfill/evidence/6764/20260910-6764")
R = json.load(open(f"{ROOT}/run.json"))
A = R["assertions"]
TITLES = {
    "A1_setup_ok": "Knowledge Pool imprint exiles top 3 of each library",
    "A2_trigger1_fires": "Cast Shock from hand -> trigger exiles it (decline control)",
    "A2b_decline_control": "Declined may-cast leaves the Shock exiled, game continues",
    "A2_trigger2_fires": "Cast Shock from hand -> trigger exiles it (accept)",
    "A3_choice_scope": "Choice offers only other Pool-linked exiled cards (trigger excluded)",
    "A4_free_cast_executes": "Chosen exiled card casts without paying mana",
    "A5_cleanup": "Game settles cleanly after the interaction",
}
W = 1180
row_h = 52
H = 380 + len(A) * row_h

img = Image.new("RGB", (W, H), (16, 18, 24))
d = ImageDraw.Draw(img)


def tx(x, y, s, c=(230, 230, 235)):
    d.text((x, y), s, fill=c)


tx(30, 20, "phase-rs/phase #6764 - Knowledge Pool: may-cast choice offers the wrong cards")
tx(30, 46, "Pinned release: v0.79.0 (build 1cde7a2, WS protocol 69) | 2026-09-10 | run 20260910-6764")
tx(30, 72, "Empirical: two live client sessions vs pinned phase-server; Shock cast from hand with Pool on BF")
tx(30, 98, "Trigger fires, exiles the Shock, may-cast prompt appears. The card choice then offers")
tx(30, 124, "5 untapped BATTLEFIELD Mountains (tapLandForMana actions) - 0 of the 7 legal exiled cards.",
   (255, 120, 120))
tx(30, 150, "VERDICT: REPRODUCED - CastFromZone target resolves to battlefield lands instead of",
   (255, 120, 120))
tx(30, 176, "Pool-linked exiled cards; no legal card can be chosen, so the free cast never happens.",
   (255, 120, 120))
y = 236
tx(30, y - 30, "Assertions (from saved game states + wire log):")
for aid, status in A.items():
    c = {"passed": (120, 220, 140), "failed": (255, 120, 120)}.get(
        status, (200, 200, 120))
    d.rectangle([30, y, 52, y + 22], fill=c)
    tx(64, y, f"{aid}  {TITLES.get(aid, '')}")
    tx(64, y + 22, f"status: {status}", (160, 165, 175))
    y += row_h
tx(30, H - 76, "Exile at choice time: 6 imprint cards (5 Mountain + 1 Pool) + Shock#1 (prior trigger).")
tx(30, H - 54, "Offered: Mountain#7, #17, #32, #42, #39 - all zone=battlefield - plus a passPriority pseudo-choice.")
tx(30, H - 32, "Triggering Shock#2 correctly excluded. Imprint ETB and decline control both behaved.")
img.save(f"{ROOT}/summary.png")
print("saved", img.size)
