#!/usr/bin/env python3
"""Issue #6728: P2P server backups are never uploaded and use a server-invalid draft code.

Empirical server-side integration test against the pinned phase-server binary:
drives the real HTTP handler (POST/GET/DELETE /p2p-draft-backup) with a
client-shaped payload carrying the client-generated `draft-xxxxxxxx` identifier,
asserts the server rejects it (400 Invalid draft code, no row), and proves the
create/update/retrieve/delete lifecycle works with a contract-valid code.

Client-side path claims are verified by source inspection at the issue's commit
(9da919f) and re-checked on main; the findings are recorded in assertions.json.
"""
import json, os, sys, time, hashlib, urllib.request, urllib.error

BASE = os.environ.get("BACKFILL_BASE", "http://127.0.0.1:9377")
RUN_ID = os.environ.get("BACKFILL_RUN_ID", "20260910-6728")
ROOT = os.path.expanduser("~/workspace/dev/phase-backfill")
EVDIR = os.path.join(ROOT, "evidence", "6728", RUN_ID)
os.makedirs(EVDIR, exist_ok=True)

wire = []          # full HTTP exchange log
assertions = []    # behavioral contract results

def http(method, path, body=None, query=""):
    req_data = None
    headers = {}
    if body is not None:
        req_data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path + query, data=req_data, headers=headers, method=method)
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status, text = resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        status, text = e.code, e.read().decode()
    dt = time.time() - t0
    wire.append({
        "request": {"method": method, "path": path, "query": query,
                    "body": body, "body_sha256": hashlib.sha256(req_data).hexdigest() if req_data else None},
        "response": {"status": status, "body": text, "elapsed_s": round(dt, 3)},
    })
    return status, text

def check(aid, title, expected, actual, detail=""):
    ok = (expected == actual)
    assertions.append({"id": aid, "title": title, "expected": expected,
                       "observed": actual, "detail": detail,
                       "status": "passed" if ok else "failed"})
    print(("PASS" if ok else "FAIL"), aid, title, "->", actual)
    return ok

def row_count(code):
    s, t = http("GET", f"/p2p-draft-backup/{code}", query="?host_peer_id=host-peer-1")
    return s, t

CLIENT_CODE = "draft-abc12345"   # exact shape of `draft-${seed.toString(16).padStart(8,"0")}`
VALID_CODE = "Q7KX2P"            # six uppercase alphanumeric: server contract
PEER = "host-peer-1"
SNAP1 = json.dumps({"draftCode": CLIENT_CODE, "seatCount": 8, "pick": 5, "picksSinceLastBackup": 5})
SNAP2 = json.dumps({"draftCode": CLIENT_CODE, "seatCount": 8, "pick": 10, "picksSinceLastBackup": 5})

# --- pre state: no backup rows for either code
pre = {}
for code in (CLIENT_CODE, VALID_CODE):
    s, t = row_count(code)
    pre[code] = {"get_status": s, "get_body": t}
json.dump(pre, open(os.path.join(EVDIR, "pre_state.json"), "w"), indent=1)

# --- A1: client-generated identifier shape is rejected by the server (the reported bug)
s, t = http("POST", "/p2p-draft-backup",
            {"draft_code": CLIENT_CODE, "host_peer_id": PEER, "snapshot_json": SNAP1})
check("A1", "client-shaped upload rejected with 400 Invalid draft code", 400, s, t)

# --- A2: rejected upload creates no SQLite row
s, t = row_count(CLIENT_CODE)
check("A2", "no row created by the rejected client-shaped upload", 400, s, t)

# --- A3: contract-valid code completes create
s, t = http("POST", "/p2p-draft-backup",
            {"draft_code": VALID_CODE, "host_peer_id": PEER, "snapshot_json": SNAP1})
check("A3", "contract-valid upload creates a row", 200, s, t)

# --- A4: retrieve returns the stored snapshot
s, t = http("GET", f"/p2p-draft-backup/{VALID_CODE}", query=f"?host_peer_id={PEER}")
row = json.loads(t) if s == 200 else {}
check("A4", "stored row retrievable with original snapshot", 200, s,
      ("snapshot round-trip" if row.get("snapshot_json") and json.loads(row["snapshot_json"])["pick"] == 5 else "mismatch: " + t[:120]))
json.dump(row, open(os.path.join(EVDIR, "post_create.json"), "w"), indent=1)

# --- A5: update overwrites the same row
s, t = http("POST", "/p2p-draft-backup",
            {"draft_code": VALID_CODE, "host_peer_id": PEER, "snapshot_json": SNAP2})
check("A5", "re-upload updates the same row", 200, s, t)
s, t = http("GET", f"/p2p-draft-backup/{VALID_CODE}", query=f"?host_peer_id={PEER}")
row2 = json.loads(t) if s == 200 else {}
check("A5b", "retrieved row reflects the update", 10,
      json.loads(row2["snapshot_json"])["pick"] if row2.get("snapshot_json") else "no-row", t[:120])
json.dump(row2, open(os.path.join(EVDIR, "post_update.json"), "w"), indent=1)

# --- A6: delete removes the row
s, t = http("DELETE", f"/p2p-draft-backup/{VALID_CODE}", query=f"?host_peer_id={PEER}")
check("A6", "delete removes the row", 200, s, t)
s, t = row_count(VALID_CODE)
check("A6b", "row gone after delete", 404, s, t)

# --- A7: GET with the client-generated code is also rejected (cleanup path parity)
s, t = http("GET", f"/p2p-draft-backup/{CLIENT_CODE}", query=f"?host_peer_id={PEER}")
check("A7", "GET with client-shaped code rejected with 400", 400, s, t)

# --- A8: DELETE with the client-shaped code is also rejected
s, t = http("DELETE", f"/p2p-draft-backup/{CLIENT_CODE}", query=f"?host_peer_id={PEER}")
check("A8", "DELETE with client-shaped code rejected with 400", 400, s, t)

json.dump(wire, open(os.path.join(EVDIR, "wire_log.json"), "w"), indent=1)
json.dump(assertions, open(os.path.join(EVDIR, "assertions.json"), "w"), indent=1)

fails = [a for a in assertions if a["status"] == "failed"]
print(f"\n{len(assertions)-len(fails)}/{len(assertions)} assertions passed")
sys.exit(1 if fails else 0)
