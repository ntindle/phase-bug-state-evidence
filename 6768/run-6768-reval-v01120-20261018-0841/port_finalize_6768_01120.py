#!/usr/bin/env python3
"""Mechanical port: finalize_6768_01100.py -> finalize_6768_01120.py.

Identity bump only (RUN_ID, server log location, version/build/protocol
strings, manifest scenario filename). PNG layout, assertion rendering, and
validation logic byte-identical to the verified 01100 finalize.
"""
RUN_ID = "run-6768-reval-v01120-20261018-0841"
SERVER_RUN_ID = "run-6765-reval-v01120-20261018-0830"

src = open("finalize_6768_01100.py").read()

reps = [
    # ---- docstring ------------------------------------------------------
    ('"""Finalize #6768 (run run-6768-reval-v01100-20261015-1214): capture server',
     f'"""Finalize #6768 (run {RUN_ID}): capture server'),
    # ---- RUN_ID / server log ---------------------------------------------
    ('RUN_ID = "run-6768-reval-v01100-20261015-1214"',
     f'RUN_ID = "{RUN_ID}"\nSERVER_RUN_ID = "{SERVER_RUN_ID}"'),
    ('SERVER_LOG = f"{BACKFILL}/runs/{RUN_ID}/server.log"',
     'SERVER_LOG = f"{BACKFILL}/runs/{SERVER_RUN_ID}/server.log"'),
    # ---- excerpts header ---------------------------------------------------
    ('f"(run {RUN_ID}, v0.110.0)\\n")',
     'f"(run {RUN_ID}, v0.112.0)\\n")'),
    # ---- PNG identity lines -------------------------------------------------
    ('text(24, y, f"v0.110.0 (build 9bfd12e, protocol 142) - 2026-10-15 - "',
     'text(24, y, f"v0.112.0 (build 9a8e3fa, protocol 147) - 2026-10-18 - "'),
    ('"Assertions (native engine v0.110.0, two human seats)"',
     '"Assertions (native engine v0.112.0, two human seats)"'),
    ('"failed; on v0.102.0-v0.108.0 (incl. this v0.110.0 run) the trigger "',
     '"failed; on v0.102.0-v0.110.0 (incl. this v0.112.0 run) the trigger "'),
    # ---- manifest file list --------------------------------------------------
    ('"run.json", "scenario_6768_01100.py",',
     '"run.json", "scenario_6768_01120.py",'),
]

for old, new in reps:
    assert src.count(old) == 1, f"expected 1 occurrence, got {src.count(old)}: {old[:70]!r}"
    src = src.replace(old, new)

# "v0.102.0-v0.110.0" is an intentional historical range in the PNG text;
# verify any remaining v0.110.0 sits only in that context.
for leftover in ("9bfd12e", "protocol 142", "01100",
                 "run-6768-reval-v01100", "2026-10-15"):
    assert leftover not in src, f"leftover: {leftover!r}"
import re as _re
for m in _re.finditer(r"v0\.110\.0", src):
    ctx = src[max(0, m.start()-40):m.end()+40]
    assert "v0.102.0-v0.110.0" in ctx, f"unintended remnant: ...{ctx}..."

open("finalize_6768_01120.py", "w").write(src)
print("wrote finalize_6768_01120.py")
