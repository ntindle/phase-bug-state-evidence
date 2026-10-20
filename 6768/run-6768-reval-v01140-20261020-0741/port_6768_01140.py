#!/usr/bin/env python3
"""Mechanical port driver: scenario_6768_01130.py (v0.113.0 / protocol 151,
verified 2026-10-19) -> scenario_6768_01140.py (v0.114.0 / protocol 153).

Interaction conventions were proven identical 151->153 by the 2026-10-19/20
v0.114.0 re-validation runs (#6758, #6763, #6764, #6609, #6659, #6666, #6690,
#6729, #6760, #6765); this port changes only version/build/protocol strings,
run identifiers, digest paths, and the release-note prose. Regenerate with:
    python3 port_6768_01140.py
"""
import re

SRC = "driver/scenario_6768_01130.py"
DST = "driver/scenario_6768_01140.py"

src = open(SRC).read()

old_header = """Protocol-151 port (v0.113.0, 2026-10-19) of scenario_6768_01130.py
(verified 2026-10-18, run run-6768-reval-v01120-20261018-0841,
v01120 / protocol 147); interaction conventions proven identical
142->147->151 by the 2026-10-18/19 v0.112.0/v0.113.0 re-validation runs.
client.py HELLO already advertises protocol 151 (updated for v0.113.0).
Server digests computed from releases/v0.113.0/ at runtime (binary
f2b09aab..., card data eb309108..., draft pools 785376ea...; minisign key
id 436711b6a2d36828 unchanged). This run started a fresh pinned v0.113.0
server on 127.0.0.1:9374 (run-6768-reval-v01130-20261019-0841; handshake
verified: ServerHello 0.113.0/0571ee3/protocol 151/Full) because the VM
was replaced between runs (no listener on 9374)."""
new_header = """Protocol-153 port (v0.114.0, 2026-10-20) of scenario_6768_01130.py
(verified 2026-10-19, run run-6768-reval-v01130-20261019-0841,
v0.113.0 / protocol 151); interaction conventions proven identical
151->153 by the 2026-10-19/20 v0.114.0 re-validation runs (#6758, #6763,
#6764, #6609, #6659, #6666, #6690, #6729, #6760, #6765). client.py HELLO
already advertises protocol 153 (updated at the v0.114.0 pin).
Server digests computed from releases/v0.114.0/ at runtime (binary
025c26c7..., card data 7fe886cc..., draft pools 4a0b44a9...; minisign key
id 436711b6a2d36828 unchanged). This run reuses the backfill-owned pinned
v0.114.0 server already listening on 127.0.0.1:9374 (PID 5151, started
2026-10-20 07:12 by the completed #6765 run; games.db and log under
runs/run-6765-reval-v01140-20261020-0711/) per the server-reuse rule;
handshake verified: ServerHello 0.114.0/ac67cbb/protocol 153/Full."""
assert old_header in src, "header block not found"
src = src.replace(old_header, new_header)

old_rd = re.compile(
    r'"run_dir": "runs/run-6768-reval-v01130-20261019-0841 \(fresh "\s*'
    r'"pinned v0\.113\.0 server started by this run on 127\.0\.0\.1:9374 "\s*'
    r'"--single-user --no-data-download; ServerHello "\s*'
    r'"0\.113\.0/0571ee3/protocol 151/Full by handshake this run\)",')
new_rd = ('"run_dir": "runs/run-6765-reval-v01140-20261020-0711 (backfill-owned "\n'
          '                         "pinned v0.114.0 server on 127.0.0.1:9374, started 2026-10-20 "\n'
          '                         "07:12 by the completed #6765 run; reused per the "\n'
          '                         "server-reuse rule, not restarted; release check confirmed "\n'
          '                         "v0.114.0 still latest; ServerHello 0.114.0/ac67cbb/protocol "\n'
          '                         "153/Full by handshake this run)",')
assert old_rd.search(src), "run_dir block not found"
src = old_rd.sub(new_rd, src)

old_dn = re.compile(
    r'"Protocol-147 port of scenario_6768_01100\.py \(verified 2026-10-15, "\s*'
    r'"run run-6768-reval-v01100-20261015-1214, v0\.110\.0/protocol 142\)\.",')
new_dn = ('"Protocol-153 port (v0.114.0, 2026-10-20) of the verified v0.113.0 "\n'
          '            "scenario_6768_01130.py (protocol 151; run "\n'
          '            "run-6768-reval-v01130-20261019-0841, validated 2026-10-19). "\n'
          '            "Interaction conventions proven identical 151->153 by the "\n'
          '            "2026-10-19/20 v0.114.0 re-validation runs.",\n'
          '            "Protocol-147 port of scenario_6768_01100.py (verified 2026-10-15, "\n'
          '            "run run-6768-reval-v01100-20261015-1214, v0.110.0/protocol 142).",')
assert old_dn.search(src), "driver_notes lead not found"
src = old_dn.sub(new_dn, src)

swaps = [
    ('RUN_ID", "run-6768-reval-v01130-20261019-0841")',
     'RUN_ID", "run-6768-reval-v01140-20261020-0741")'),
    ("server/releases/v0.113.0/", "server/releases/v0.114.0/"),
    ("pinned v0.113.0 card-data.json", "pinned v0.114.0 card-data.json"),
    ("v0.113.0 data; re-verified each run", "v0.114.0 data; re-verified each run"),
    ("v0.112.0/protocol 151", "v0.114.0/protocol 153"),
    ('startswith("0.113.0")', 'startswith("0.114.0")'),
    ("int(proto) == 151", "int(proto) == 153"),
    ('build) == "0571ee3"', 'build) == "ac67cbb"'),
    ("on protocol 151", "on protocol 153"),
    ("(protocol 151)", "(protocol 153)"),
    ('"server_version": "v0.113.0"', '"server_version": "v0.114.0"'),
    ('"build_commit": "0571ee3"', '"build_commit": "ac67cbb"'),
    ('"protocol_version": 151', '"protocol_version": 153'),
    ('f"{EVDIR}/scenario_6768_01130.py"', 'f"{EVDIR}/scenario_6768_01140.py"'),
]
for old, new in swaps:
    assert old in src, f"swap not found: {old!r}"
    src = src.replace(old, new)

open(DST, "w").write(src)
print("wrote", DST)
