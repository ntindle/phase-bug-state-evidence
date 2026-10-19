#!/usr/bin/env python3
"""Mechanical port: scenario_6771_01120.py (protocol 147, v0.112.0)
 -> scenario_6771_01130.py (protocol 151, v0.113.0)."""
import re

SRC = "/home/hatch/workspace/dev/phase-backfill/driver/scenario_6771_01120.py"
DST = "/home/hatch/workspace/dev/phase-backfill/driver/scenario_6771_01130.py"
NEW_RUN = "run-6771-reval-v01130-20261019-0941"
OLD_RUN = "run-6771-reval-v01120-20261018-0945"

s = open(SRC).read()

# Docstring header: rebase onto the 01120 file
s = s.replace(
    "Protocol-147 port (v0.112.0, 2026-10-18) of scenario_6771_01100.py\n"
    "(verified 2026-10-15, run run-6771-reval-v01100-20261015-1315, v0.110.0/\n"
    "protocol 142, REPRODUCED: library 50->46, spell in graveyard, no choose-X\n"
    "prompt ever offered, nothing discarded; X=2 answered via ChooseXValue\n"
    "range [0,2]).\n"
    "Mechanical port: version strings, protocol numbers, release paths, digests.",
    "Protocol-151 port (v0.113.0, 2026-10-19) of scenario_6771_01120.py\n"
    "(verified 2026-10-18, run run-6771-reval-v01120-20261018-0945, v0.112.0/\n"
    "protocol 147, REPRODUCED: library 50->46, spell in graveyard, no choose-X\n"
    "prompt ever offered, nothing discarded; X=2 answered via ChooseXValue\n"
    "range [0,2]).\n"
    "Mechanical port: version strings, protocol numbers, release paths, digests.")

s = s.replace(
    "Interaction conventions identical 142->144->147 (proven by the 2026-10-16\n"
    "v0.111.0 runs and the 2026-10-17/18 v0.112.0 runs #301/#5654/#6666).",
    "Interaction conventions identical 142->144->147->151 (proven by the 2026-10-18\n"
    "v0.112.0 runs and the 2026-10-19 v0.113.0 runs #6728/#6763/#6765/#6768).")
s = s.replace("protocol 118/120/126/142/144/147", "protocol 118/120/126/142/144/147/151")
s = s.replace("142->144->147", "142->144->147->151")

s = s.replace(OLD_RUN, NEW_RUN)
s = s.replace("v0.112.0", "v0.113.0")
s = s.replace("0.112.0", "0.113.0")
s = s.replace("9a8e3fa", "0571ee3")

# Every remaining "147" is a protocol reference (verified by grep).
s = s.replace("147", "151")

open(DST, "w").write(s)
print("wrote", DST)
