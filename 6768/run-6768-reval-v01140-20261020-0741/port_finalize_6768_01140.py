#!/usr/bin/env python3
"""Mechanical port driver: finalize_6768_01130.py -> finalize_6768_01140.py.

The v0.114.0 run reuses the backfill-owned pinned server started by the
completed #6765 run, so SERVER_RUN_ID points at
runs/run-6765-reval-v01140-20261020-0711/ (the actual server.log owner).
The verdict panel is made verdict-conditional (reproduced vs fixed claim)
instead of asserting the no-op unconditionally. Regenerate with:
    python3 port_finalize_6768_01140.py
"""
import re

SRC = "driver/finalize_6768_01130.py"
DST = "driver/finalize_6768_01140.py"

src = open(SRC).read()
reps = [
    # longest names first: finalize_6768_01130.py is a substring of
    # port_finalize_6768_01130.py, so it must be replaced after.
    ("run-6768-reval-v01130-20261019-0841",
     "run-6768-reval-v01140-20261020-0741"),
    ("v0.113.0", "v0.114.0"),
    ("0571ee3", "ac67cbb"),
    ("protocol 151", "protocol 153"),
    ("2026-10-19", "2026-10-20"),
    ("port_finalize_6768_01130.py", "port_finalize_6768_01140.py"),
    ("finalize_6768_01130.py", "finalize_6768_01140.py"),
    ("port_6768_01130.py", "port_6768_01140.py"),
    ("scenario_6768_01130.py", "scenario_6768_01140.py"),
]
for old, new in reps:
    assert old in src, f"not found: {old!r}"
    src = src.replace(old, new)

src = src.replace(
    'SERVER_RUN_ID = "run-6768-reval-v01140-20261020-0741"',
    'SERVER_RUN_ID = "run-6765-reval-v01140-20261020-0711"')

# The verdict panel is static no-op prose in the source; make it
# verdict-conditional so a not-reproduced run cannot render "no-op".
old_panel = '''text(36, y + 32, "The Meld trigger fires and resolves, but NOTHING happens:",
     fill=TEXT)
text(36, y + 54, "both halves stay on the battlefield - no exile, no Brisela.",
     fill=TEXT)
text(36, y + 76, "On v0.86.0 the exile half fired and only the meld creation",
     fill=DIM)
text(36, y + 98, "failed; on v0.102.0-v0.114.0 the trigger "
                 "resolves as a complete no-op.",
     fill=DIM)'''
new_panel = '''if verdict == "reproduced":
    text(36, y + 32, "The Meld trigger fires and resolves, but NOTHING happens:",
         fill=TEXT)
    text(36, y + 54, "both halves stay on the battlefield - no exile, no Brisela.",
         fill=TEXT)
else:
    text(36, y + 32, "Gisela + Bruna exile and meld into Brisela, Voice of",
         fill=TEXT)
    text(36, y + 54, "Nightmares as expected (scenario result; not a fix claim).",
         fill=TEXT)
if verdict == "reproduced":
    text(36, y + 76, "On v0.86.0 the exile half fired and only the meld creation",
         fill=DIM)
    text(36, y + 98, "failed; on v0.102.0-v0.114.0 the trigger "
                     "resolves as a complete no-op.",
         fill=DIM)
else:
    text(36, y + 76, "The no-op seen on v0.102.0-v0.113.0 does NOT occur on "
                     "v0.114.0",
         fill=DIM)
    text(36, y + 98, "(scenario result only; not a fix claim).",
         fill=DIM)'''
assert old_panel in src, "verdict panel not found"
src = src.replace(old_panel, new_panel)

open(DST, "w").write(src)
print("wrote", DST)
