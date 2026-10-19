#!/usr/bin/env python3
"""One-shot mechanical port: scenario_6768_01120.py -> scenario_6768_01130.py."""
SRC = "/home/hatch/workspace/dev/phase-backfill/driver/scenario_6768_01120.py"
DST = "/home/hatch/workspace/dev/phase-backfill/driver/scenario_6768_01130.py"

s = open(SRC).read()

def rep(old, new, count=1):
    global s
    assert s.count(old) >= count, f"missing: {old[:70]!r}"
    s = s.replace(old, new, count)

# --- docstring header: new 01130 port note prepended, old blocks preserved ---
rep('''"""Issue #6768: Bruna and Gisela don't merge. -- they just exile themselves.

Protocol-147 port (v0.112.0, 2026-10-18) of scenario_6768_01100.py''',
'''"""Issue #6768: Bruna and Gisela don't merge. -- they just exile themselves.

Protocol-151 port (v0.113.0, 2026-10-19) of scenario_6768_01120.py
(verified 2026-10-18, run run-6768-reval-v01120-20261018-0841,
v01120 / protocol 147); interaction conventions proven identical
142->147->151 by the 2026-10-18/19 v0.112.0/v0.113.0 re-validation runs.
client.py HELLO already advertises protocol 151 (updated for v0.113.0).
Server digests computed from releases/v0.113.0/ at runtime (binary
f2b09aab..., card data eb309108..., draft pools 785376ea...; minisign key
id 436711b6a2d36828 unchanged). This run started a fresh pinned v0.113.0
server on 127.0.0.1:9374 (run-6768-reval-v01130-20261019-0841; handshake
verified: ServerHello 0.113.0/0571ee3/protocol 151/Full) because the VM
was replaced between runs (no listener on 9374).

Protocol-147 port (v0.112.0, 2026-10-18) of scenario_6768_01100.py [preserved]''')

# --- run identity ---
rep('RUN_ID = "run-6768-reval-v01120-20261018-0841"',
    'RUN_ID = os.environ.get("RUN_ID", "run-6768-reval-v01130-20261019-0841")')

# --- card data / binary paths ---
s = s.replace("server/releases/v0.112.0/", "server/releases/v0.113.0/")
s = s.replace("scenario_6768_01120.py", "scenario_6768_01130.py")

# --- ServerHello asserts ---
rep('assert str(ver).startswith("0.112.0")', 'assert str(ver).startswith("0.113.0")')
rep('assert str(build) == "9a8e3fa"', 'assert str(build) == "0571ee3"')
rep('assert int(proto) == 147,', 'assert int(proto) == 151,')

# --- run.json meta ---
rep('"server_version": "v0.112.0"', '"server_version": "v0.113.0"')
rep('"build_commit": "9a8e3fa"', '"build_commit": "0571ee3"')
rep('"protocol_version": 147', '"protocol_version": 151')
rep('''            "run_dir": "runs/run-6765-reval-v01120-20261018-0830 (reused live "
                         "backfill-owned v0.112.0 server on 127.0.0.1:9374; ServerHello "
                         "re-verified 0.112.0/9a8e3fa/protocol 147 by handshake this run)",''',
    '''            "run_dir": "runs/run-6768-reval-v01130-20261019-0841 (fresh "
                         "pinned v0.113.0 server started by this run on 127.0.0.1:9374 "
                         "--single-user --no-data-download; ServerHello "
                         "0.113.0/0571ee3/protocol 151/Full by handshake this run)",''')

# --- prose version references (header rewritten by the first rep; the bare
# "on protocol 147" / "protocol 147):" / "v0.112.0/protocol 147" prose forms
# are handled below) ---
s = s.replace("on protocol 147", "on protocol 151")
s = s.replace("protocol 147):", "protocol 151):")
s = s.replace("v0.112.0/protocol 147", "v0.113.0/protocol 151")
rep("Oracle (pinned v0.112.0 card-data.json", "Oracle (pinned v0.113.0 card-data.json")
rep("Parser state (v0.112.0 data", "Parser state (v0.113.0 data")

# sanity: no stale artifacts (header history may still name old pins)
assert "server/releases/v0.112.0" not in s, "leftover v0.112.0 path"
assert "9a8e3fa" not in s, "leftover 9a8e3fa"
assert "scenario_6768_01120" not in s, "leftover 01120 filename"

open(DST, "w").write(s)
print("wrote", DST, len(s.splitlines()), "lines")
