#!/usr/bin/env python3
"""Mechanical port: scenario_6768_01100.py (v0.110.0 / protocol 142) ->
scenario_6768_01120.py (v0.112.0 / protocol 147).

Driver logic is byte-identical; interaction conventions proven identical
142->147 by the 2026-10-18 v0.112.0 re-validation runs (#6763,
run-6763-reval-v01120-20261018-0711, verdict reproduced on protocol 147).
The 01100 run was verified reproduced on v0.110.0 (A1/A2 passed, A3/A4
failed: Meld trigger fired but resolved as a complete no-op -- both
halves stayed on the battlefield, no exile, no Brisela); this run
re-validates on v0.112.0.

Run from ~/workspace/dev/phase-backfill/driver.
"""

RUN_ID = "run-6768-reval-v01120-20261018-0841"

src = open("scenario_6768_01100.py").read()

reps = [
    # ---- docstring header -------------------------------------------------
    ('''Protocol-142 port (v0.110.0, 2026-10-15) of scenario_6768_01081.py
(verified 2026-10-12, run run-6768-reval-v01081-20261012-1611,
v0.108.0/protocol 134); interaction conventions proven identical
134->142 by the 2026-10-14/15 v0.110.0 re-validation runs.
Cast-confirmation guard (per the 2026-10-09 #6765 lesson): on protocol 142''',
     '''Protocol-147 port (v0.112.0, 2026-10-18) of scenario_6768_01100.py
(verified 2026-10-15, run run-6768-reval-v01100-20261015-1214,
v0.110.0/protocol 142); interaction conventions proven identical
142->147 by the 2026-10-18 v0.112.0 re-validation runs (#6763).
Cast-confirmation guard (per the 2026-10-09 #6765 lesson): on protocol 147'''),
    # ---- Oracle / parser / setup identity ---------------------------------
    ("Oracle (pinned v0.110.0 card-data.json;",
     "Oracle (pinned v0.112.0 card-data.json;"),
    ("Parser state (v0.110.0 data; re-verified each run):",
     "Parser state (v0.112.0 data; re-verified each run):"),
    ("Setup (native engine, two human-client seats, v0.110.0/protocol 142):",
     "Setup (native engine, two human-client seats, v0.112.0/protocol 147):"),
    # ---- RUN_ID / release dir ---------------------------------------------
    ('RUN_ID = "run-6768-reval-v01100-20261015-1214"',
     f'RUN_ID = "{RUN_ID}"'),
    ('CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.110.0/data/card-data.json"))',
     'CARD_DATA = json.load(open(f"{BACKFILL}/server/releases/v0.112.0/data/card-data.json"))'),
    # ---- ServerHello asserts ------------------------------------------------
    ('''    assert str(ver).startswith("0.110.0"), f"unexpected version {ver}"
    assert int(proto) == 142, f"unexpected protocol {proto}"
    assert str(build) == "9bfd12e", f"unexpected build {build}"''',
     '''    assert str(ver).startswith("0.112.0"), f"unexpected version {ver}"
    assert int(proto) == 147, f"unexpected protocol {proto}"
    assert str(build) == "9a8e3fa", f"unexpected build {build}"'''),
    # ---- cast-confirmation guard docstring (inherited "Protocol-134" label) --
    ('"""Protocol-134 cast-confirmation guard (2026-10-09 lesson), ST-adapted.',
     '"""Protocol-147 cast-confirmation guard (2026-10-09 lesson), ST-adapted.'),
    # ---- in-code comments ---------------------------------------------------
    ("# cast-confirmation guard (protocol 142): hold while a CastSpell is in",
     "# cast-confirmation guard (protocol 147): hold while a CastSpell is in"),
    # ---- evidence-copy filename ---------------------------------------------
    ('with open(f"{EVDIR}/scenario_6768_01100.py", "w") as f:',
     'with open(f"{EVDIR}/scenario_6768_01120.py", "w") as f:'),
    # ---- hash paths ----------------------------------------------------------
    ('open(f"{BACKFILL}/server/releases/v0.110.0/"',
     'open(f"{BACKFILL}/server/releases/v0.112.0/"'),
    ('open(f"{BACKFILL}/server/releases/v0.110.0/data/card-data.json", "rb")',
     'open(f"{BACKFILL}/server/releases/v0.112.0/data/card-data.json", "rb")'),
    ('open(f"{BACKFILL}/server/releases/v0.110.0/data/draft-pools.json", "rb")',
     'open(f"{BACKFILL}/server/releases/v0.112.0/data/draft-pools.json", "rb")'),
    # ---- run_meta identity ---------------------------------------------------
    ('"server_version": "v0.110.0",', '"server_version": "v0.112.0",'),
    ('"build_commit": "9bfd12e",', '"build_commit": "9a8e3fa",'),
    ('"protocol_version": 142,', '"protocol_version": 147,'),
    ('"run_dir": "runs/run-20261011-0811 (reused live "\n'
     '                         "backfill-owned v0.110.0 server on 127.0.0.1:9374; ServerHello "\n'
     '                         "re-verified 0.110.0/9bfd12e/protocol 142)",',
     '"run_dir": "runs/run-6765-reval-v01120-20261018-0830 (reused live "\n'
     '                         "backfill-owned v0.112.0 server on 127.0.0.1:9374; ServerHello "\n'
     '                         "re-verified 0.112.0/9a8e3fa/protocol 147 by handshake this run)",'),
    # ---- driver_notes ----------------------------------------------------------
    ('"Protocol-142 port of scenario_6768_01081.py (verified 2026-10-12, "\n'
     '            "run run-6768-reval-v01081-20261012-1611, v0.108.0/protocol 134).",',
     '"Protocol-147 port of scenario_6768_01100.py (verified 2026-10-15, "\n'
     '            "run run-6768-reval-v01100-20261015-1214, v0.110.0/protocol 142).",'),
    ('"Cast-confirmation guard (protocol 142): after every CastSpell "',
     '"Cast-confirmation guard (protocol 147): after every CastSpell "'),
]

for old, new in reps:
    assert src.count(old) == 1, f"expected exactly 1 occurrence, got {src.count(old)}: {old[:70]!r}"
    src = src.replace(old, new)

# sanity: no leftover prior-pin identity strings. Historical references to the
# v0.110.0/142 run are intentional (docstring header, driver_notes); verify
# every remaining one sits in those two historical contexts.
for leftover in ("9bfd12e", "protocol 134", "Protocol-134", "v0.108.0",
                 "134->142", "01081", "b8fa1d3"):
    assert leftover not in src, f"leftover identity string: {leftover!r}"
import re as _re
for m in _re.finditer(r"v0\.110\.0|protocol 142|Protocol-142|01100", src):
    ctx = src[max(0, m.start()-120):m.end()+120]
    assert ("scenario_6768_01100" in ctx or "v0.110.0/protocol 142" in ctx
            or "run-6768-reval-v01100-20261015-1214" in ctx), \
        f"unintended identity remnant: ...{ctx}..."

open("scenario_6768_01120.py", "w").write(src)
print("wrote scenario_6768_01120.py")
