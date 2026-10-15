#!/usr/bin/env python3
"""Mechanical port: scenario_6768_01081.py (v0.108.0 / protocol 134) ->
scenario_6768_01100.py (v0.110.0 / protocol 142).

Driver logic is byte-identical; interaction conventions proven identical
134->142 by the 2026-10-14/15 v0.110.0 re-validation runs (#301, #5654,
#6557, #6609, #6643, #6659, #6666, #6729, #6758, #6764, #6765, #7143,
#7166). The 01081 run was verified reproduced on v0.108.0 (A1/A2/A5
passed, A3/A4 failed: Meld trigger fired but resolved as a complete
no-op -- both halves stayed on the battlefield, no exile, no Brisela);
this run re-validates on v0.110.0.

Run from ~/workspace/dev/phase-backfill/driver.
"""

RUN_ID = "run-6768-reval-v01100-20261015-1214"

src = open("scenario_6768_01081.py").read()

reps = [
    # ---- docstring header -------------------------------------------------
    ('''Protocol-134 port (v0.108.0, 2026-10-12) of scenario_6768_01060.py
(verified 2026-10-11, run run-6768-reval-v01060-20261011-1005,
v0.106.0/protocol 126).''',
     '''Protocol-142 port (v0.110.0, 2026-10-15) of scenario_6768_01081.py
(verified 2026-10-12, run run-6768-reval-v01081-20261012-1611,
v0.108.0/protocol 134); interaction conventions proven identical
134->142 by the 2026-10-14/15 v0.110.0 re-validation runs.'''),
    ("Cast-confirmation guard (per the 2026-10-09 #6765 lesson): on protocol 134",
     "Cast-confirmation guard (per the 2026-10-09 #6765 lesson): on protocol 142"),
    # ---- Oracle / parser / setup identity ---------------------------------
    ("Oracle (pinned v0.108.0 card-data.json;",
     "Oracle (pinned v0.110.0 card-data.json;"),
    ("Parser state (v0.108.0 data;",
     "Parser state (v0.110.0 data;"),
    ("Setup (native engine, two human-client seats, v0.108.0/protocol 134):",
     "Setup (native engine, two human-client seats, v0.110.0/protocol 142):"),
    # ---- RUN_ID / release dir ---------------------------------------------
    ('RUN_ID = "run-6768-reval-v01081-20261012-1611"',
     f'RUN_ID = "{RUN_ID}"'),
    ('server/releases/v0.108.0/data/card-data.json',
     'server/releases/v0.110.0/data/card-data.json'),
    # ---- ServerHello asserts ------------------------------------------------
    ('''    assert str(ver).startswith("0.108.0"), f"unexpected version {ver}"
    assert int(proto) == 134, f"unexpected protocol {proto}"
    assert str(build) == "b8fa1d3", f"unexpected build {build}"''',
     '''    assert str(ver).startswith("0.110.0"), f"unexpected version {ver}"
    assert int(proto) == 142, f"unexpected protocol {proto}"
    assert str(build) == "9bfd12e", f"unexpected build {build}"'''),
    # ---- in-code comments -----------------------------------------------------
    ("# cast-confirmation guard (protocol 134):",
     "# cast-confirmation guard (protocol 142):"),
    # ---- evidence-copy filename -----------------------------------------------
    ('with open(f"{EVDIR}/scenario_6768_01081.py", "w") as f:',
     'with open(f"{EVDIR}/scenario_6768_01100.py", "w") as f:'),
    # ---- hash paths -------------------------------------------------------------
    ("server/releases/v0.108.0/", "server/releases/v0.110.0/"),
    # ---- run_meta identity ------------------------------------------------------
    ('"server_version": "v0.108.0",', '"server_version": "v0.110.0",'),
    ('"build_commit": "b8fa1d3",', '"build_commit": "9bfd12e",'),
    ('"protocol_version": 134,', '"protocol_version": 142,'),
    ('''"backfill-owned v0.108.0 server on 127.0.0.1:9374; ServerHello "
                         "re-verified 0.108.0/b8fa1d3/protocol 134)",''',
     '''"backfill-owned v0.110.0 server on 127.0.0.1:9374; ServerHello "
                         "re-verified 0.110.0/9bfd12e/protocol 142)",'''),
    # ---- driver_notes -------------------------------------------------------------
    ('"Protocol-134 port of scenario_6768_01060.py (verified 2026-10-11, "',
     '"Protocol-142 port of scenario_6768_01081.py (verified 2026-10-12, "'),
    ('"run run-6768-reval-v01060-20261011-1005, v0.106.0/protocol 126).",',
     '"run run-6768-reval-v01081-20261012-1611, v0.108.0/protocol 134).",'),
    ('"Cast-confirmation guard (protocol 134): after every CastSpell "',
     '"Cast-confirmation guard (protocol 142): after every CastSpell "'),
]

misses = []
for old, new in reps:
    if old not in src:
        misses.append(old[:90].replace("\n", "\\n"))
    src = src.replace(old, new)

print("misses:", misses if misses else "none")

# safety: no stray old-identity lines left (history/docstring lines whitelisted)
import re
history_ok = (
    "scenario_6768_01060", "scenario_6768_01081", "scenario_6768_01020",
    "scenario_6768_01050", "scenario_6768_v086", "01060", "01081", "01050",
    "01020", "v086",
    "Protocol-134", "protocol 134", "protocol-134",
    "v0.102.0", "v0.103.0", "v0.104.0", "v0.105.0", "v0.106.0", "v0.107.0",
    "v0.108.0", "b8fa1d3",
    "run-6768-reval-v01060-20261011-1005", "run-6768-reval-v01081-20261012-1611",
)
leftovers = [l for l in src.splitlines()
             if not any(h in l for h in history_ok)
             and (re.search(r'\bv0\.108\.0\b', l) or re.search(r'\bb8fa1d3\b', l)
                  or re.search(r'\b134\b', l))]
print("stray old-identity lines:", leftovers[:8] if leftovers else "none")

import ast
ast.parse(src)
open("scenario_6768_01100.py", "w").write(src)
print("written driver/scenario_6768_01100.py, len", len(src))
