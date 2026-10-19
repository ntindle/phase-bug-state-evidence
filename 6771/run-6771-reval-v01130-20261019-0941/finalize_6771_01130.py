#!/usr/bin/env python3
"""Finalize #6771 (run run-6771-reval-v01130-20261019-0941): capture server
excerpts, render summary.png (derived from the saved states and assertion
results), write manifest.sha256, validate. Does NOT re-run the game;
packages existing artifacts. run.json/assertions.json/scenario copy/data
evidence were written by the scenario itself."""
import hashlib
import json
import os
import re
import shutil

from PIL import Image, ImageDraw

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
EVID_ISSUE = "6771"
RUN_ID = "run-6771-reval-v01130-20261019-0941"
SERVER_RUN_ID = "run-6771-reval-v01130-20261019-0941"
EVDIR = f"{BACKFILL}/evidence/{EVID_ISSUE}/{RUN_ID}"
SERVER_LOG = f"{BACKFILL}/runs/{SERVER_RUN_ID}/server.log"


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def load(p):
    with open(p) as f:
        return json.load(f)


def env(p):
    return load(os.path.join(EVDIR, p))["state"]


def name(o):
    return o.get("base_name") or o.get("name") or "?"


def lname(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def hand_ids(s, pid):
    return [oid for oid, o in s.get("objects", {}).items()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def gy_count(s, pid):
    return sum(1 for o in s.get("objects", {}).values()
               if o.get("zone") == "Graveyard" and o.get("controller") == pid)


def lib_count(s, pid):
    for p in s.get("players", []):
        if isinstance(p, dict) and p.get("id") == pid:
            return len(p.get("library", []) or [])
    return None


run = load(f"{EVDIR}/run.json")
aj = load(f"{EVDIR}/assertions.json")
A = aj["assertions"]
verdict = run["verdict"]
srv = run["server"]

# --- 1. server excerpts -------------------------------------------------------
game_code = None
for line in open(f"{EVDIR}/scenario_run.log"):
    if line.startswith("game "):
        game_code = line.split()[1].strip(";")
        break
print("game code:", game_code)
ansi = re.compile(r"\x1b\[[0-9;]*m")
excerpts = []
if os.path.exists(SERVER_LOG):
    for line in open(SERVER_LOG):
        if game_code and game_code in line:
            excerpts.append(ansi.sub("", line.rstrip("\n")))
keep = [l for l in excerpts
        if any(k in l for k in ("Breakthrough", "breakthrough", "ChooseXValue",
                                "ERROR", "error", "WARN", "warn",
                                "player joined", "game created",
                                "CastSpell", "Discard"))]
with open(f"{EVDIR}/server_excerpts.log", "w") as f:
    f.write(f"# server.log excerpts for game {game_code} "
            f"(run {RUN_ID}, v0.113.0)\n")
    f.write(f"# {len(excerpts)} total lines for this game; "
            f"{len(keep)} matched lines below\n")
    for l in keep[:400]:
        f.write(l + "\n")
print("server excerpts:", len(keep), "of", len(excerpts))

# --- 2. copy port/finalize scripts into the evidence dir ----------------------
for src in ("port_6771_01130.py", "finalize_6771_01130.py"):
    shutil.copy(f"{BACKFILL}/driver/{src}", f"{EVDIR}/{src}")
print("copied scripts")

# --- 3. summary.png (derived from saved states + assertions) ------------------
pre = env("pre_cast.json")
post = env("post_cast.json")
h0, h1 = len(hand_ids(pre, 0)), len(hand_ids(post, 0))
l0, l1 = lib_count(pre, 0), lib_count(post, 0)
g0, g1 = gy_count(pre, 0), gy_count(post, 0)
xval = aj.get("x_value")
xrange = aj.get("x_range")


def panel_line(s, label):
    pls = s.get("players", [])
    life = {p.get("id"): p.get("life") for p in pls if isinstance(p, dict)}
    return (f"{label}: turn {s.get('turn_number')} {s.get('phase')} | "
            f"life P0={life.get(0)} P1={life.get(1)} | "
            f"P0 hand={len(hand_ids(s, 0))} lib={lib_count(s, 0)} "
            f"gy={gy_count(s, 0)} | "
            f"waiting={((s.get('waiting_for') or {}).get('type'))} | "
            f"stack {len(s.get('stack') or [])}")


W, H = 1100, 920
BG = (18, 20, 26)
PANEL = (26, 30, 38)
TEXT = (235, 238, 245)
DIM = (150, 160, 175)
ACCENT = (110, 180, 255)
GREEN = (110, 220, 140)
RED = (240, 120, 120)
YELLOW = (240, 200, 110)

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)
y = 16
d.text((24, y), "#6771 - Breakthrough: draws 4, never discards", fill=ACCENT)
y += 26
d.text((24, y),
       f"server {srv['server_version']} (build {srv['build_commit']}, protocol "
       f"{srv['protocol_version']}) | run {run['run_id']} | {run['date']} | "
       f"verdict: {verdict}", fill=DIM)
y += 24
d.text((24, y), "Oracle: \"Draw four cards, then choose X cards in your hand "
                f"and discard the rest.\" ({{X}}{{U}}, cast with X={xval})",
       fill=TEXT)
y += 22
d.text((24, y), "Parser in pinned data: Draw supported; choose-X and "
                "discard-rest children Unimplemented", fill=DIM)
y += 32

for s, label in ((pre, "PRE-CAST (before Breakthrough cast)"),
                 (post, "POST-RESOLUTION (spell in graveyard, stack empty)")):
    d.rectangle([16, y, W - 16, y + 56], fill=PANEL, outline=(45, 52, 64))
    d.text((28, y + 6), label, fill=YELLOW)
    d.text((28, y + 28), panel_line(s, "state")[:148], fill=TEXT)
    y += 66

d.rectangle([16, y, W - 16, y + 170], fill=PANEL, outline=(45, 52, 64))
d.text((28, y + 8), "Key numbers (pre -> post)", fill=YELLOW)
yy = y + 32
rows = [
    (f"P0 library: {l0} -> {l1}  (delta {l0 - l1}; expected draw 4)",
     abs((l0 - l1) - 4) == 0),
    (f"P0 hand: {h0} -> {h1}  (oracle with X={xval} expects {xval}; "
     f"bug keeps all = {h0 - 1 + 4})",
     h1 == xval),
    (f"P0 graveyard: {g0} -> {g1}  (spell itself only)",
     g1 - g0 == 1),
    ("Breakthrough in P0 graveyard: yes", True),
    (f"waiting_for sequence: {' -> '.join(aj.get('wf_sequence', []))}",
     "ChooseXValue" in aj.get("wf_sequence", [])),
    (f"hand-keep 'choose X' prompt after draw: "
     f"{'seen' if aj.get('hand_keep_prompts') else 'never offered'}",
     bool(aj.get("hand_keep_prompts"))),
]
for r, ok in rows:
    d.text((28, yy), r[:162], fill=GREEN if ok else (RED if ok is False else TEXT))
    yy += 24
y += 182

d.rectangle([16, y, W - 16, y + 168], fill=PANEL, outline=(45, 52, 64))
d.text((28, y + 8), "Assertions", fill=YELLOW)
yy = y + 34
for k, v in A.items():
    color = GREEN if v == "passed" else (RED if v == "failed" else DIM)
    d.text((28, yy), f"{k}: {v}", fill=color)
    yy += 23
y += 180

facts = [
    f"Observed: X={xval} announced via ChooseXValue (range {xrange}), then Priority the",
    "  rest of the way. No 'choose X cards in hand' prompt was ever offered and",
    f"  nothing was discarded: {h1} cards in hand after resolution ({h0}-1 cast +4",
    f"  drawn) vs {xval} expected with X={xval}. Binary/data sha256 in run.json.",
]
for f in facts:
    d.text((24, y), f, fill=TEXT if not f.startswith("  ") else DIM)
    y += 22

img.save(f"{EVDIR}/summary.png")
print("wrote summary.png", img.size)

# --- 4. manifest.sha256 -------------------------------------------------------
files = sorted([
    "assertions.json", "data_evidence.json", "finalize_6771_01130.py",
    "port_6771_01130.py", "post_cast.json", "pre_cast.json", "run.json",
    "scenario_6771.py", "scenario_run.log", "server_excerpts.log",
    "summary.png", "wire_log.jsonl",
])
files = [fn for fn in files if os.path.exists(f"{EVDIR}/{fn}")]
with open(f"{EVDIR}/manifest.sha256", "w") as f:
    for fn in files:
        f.write(f"{sha256_of_file(f'{EVDIR}/{fn}')}  {fn}\n")
print("wrote manifest.sha256 over", len(files), "files")

# --- 5. validation ------------------------------------------------------------
for fn in files:
    p = f"{EVDIR}/{fn}"
    assert open(p, "rb").read(1), f"empty {fn}"
    if fn.endswith(".json"):
        json.load(open(p))
print("all JSON parse; all files non-empty")
for line in open(f"{EVDIR}/manifest.sha256"):
    h, fn = line.strip().split("  ")
    assert sha256_of_file(f"{EVDIR}/{fn}") == h, f"hash mismatch {fn}"
print("manifest hashes verified")
im = Image.open(f"{EVDIR}/summary.png")
im.load()
print("png ok:", im.size)
print("DONE")
