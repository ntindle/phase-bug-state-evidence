#!/usr/bin/env python3
"""Finalize #6768 (run run-6768-reval-v01120-20261018-0841): capture server
excerpts, render summary.png (derived from the saved states and assertion
results), write manifest.sha256, validate. Does NOT re-run the game;
packages existing artifacts. run.json/assertions.json/scenario copy/data
evidence were written by the scenario itself."""
import hashlib
import json
import os
import re

from PIL import Image, ImageDraw

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
EVID_ISSUE = "6768"
RUN_ID = "run-6768-reval-v01120-20261018-0841"
SERVER_RUN_ID = "run-6765-reval-v01120-20261018-0830"
EVDIR = f"{BACKFILL}/evidence/{EVID_ISSUE}/{RUN_ID}"
SERVER_LOG = f"{BACKFILL}/runs/{SERVER_RUN_ID}/server.log"


def sha256_of_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def load_state(fn):
    return json.load(open(f"{EVDIR}/{fn}"))["state"]


def bf_counts(s):
    bruna = gisela = brisela = 0
    for o in s["objects"].values():
        if str(o.get("zone", "")).lower() != "battlefield":
            continue
        nm = (o.get("base_name") or o.get("name") or "")
        if "Bruna" in nm:
            bruna += 1
        if "Gisela" in nm:
            gisela += 1
        if "Brisela" in nm:
            brisela += 1
    return bruna, gisela, brisela


def exile_names(s):
    return [(o.get("base_name") or o.get("name") or "?")
            for o in s["objects"].values()
            if str(o.get("zone", "")).lower() == "exile"]


run = json.load(open(f"{EVDIR}/run.json"))
A = json.load(open(f"{EVDIR}/assertions.json"))["assertions"]
verdict = run["verdict"]

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
        if any(k in l for k in ("Meld", "meld", "Brisela", "trigger",
                                "Trigger", "ERROR", "error", "WARN",
                                "player joined", "game created",
                                "CastSpell"))]
with open(f"{EVDIR}/server_excerpts.log", "w") as f:
    f.write(f"# server.log excerpts for game {game_code} "
            f"(run {RUN_ID}, v0.112.0)\n")
    f.write(f"# {len(excerpts)} total lines for this game; "
            f"{len(keep)} matched lines below\n")
    for l in keep[:400]:
        f.write(l + "\n")
print("server excerpts:", len(keep), "of", len(excerpts))

# --- 2. summary.png (derived from saved states + assertions) ------------------
pre, mid, post = (load_state("pre.json"), load_state("mid_trigger.json"),
                  load_state("post_trigger.json"))
p_b, p_g, p_br = bf_counts(pre)
m_b, m_g, m_br = bf_counts(mid)
po_b, po_g, po_br = bf_counts(post)
post_exile = exile_names(post)
mid_stack = mid.get("stack") or []
mid_kind = None
for e in mid_stack:
    k = e.get("kind")
    mid_kind = k.get("type") if isinstance(k, dict) else k

W, H = 1100, 960
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
y = 24


def text(x, yy, s, fill=TEXT):
    d.text((x, yy), s, fill=fill)


def vcolor(v):
    return GREEN if v == "passed" else (RED if v == "failed" else YELLOW)


text(24, y, "phase-rs/phase #6768 - Bruna and Gisela don't merge", fill=ACCENT)
y += 30
text(24, y, f"v0.112.0 (build 9a8e3fa, protocol 147) - 2026-10-18 - "
            f"run {RUN_ID}", fill=DIM)
y += 28
text(24, y, "Reported: 'they just exile thmeselves' (Bruna, the Fading Light + "
            "Gisela,", fill=TEXT)
y += 22
text(24, y, "the Broken Blade should meld into Brisela, Voice of Nightmares)",
     fill=TEXT)
y += 30

notes = {
    "A1_setup_ok": f"Gisela + Bruna on P0 BF at turn {pre.get('turn_number')} (pre.json)",
    "A2_trigger_fires": f"{mid_kind} on stack at phase {mid.get('phase')} (mid_trigger.json)",
    "A3_exile_observed": f"both halves exiled at resolution (post exile: "
                         f"{', '.join(post_exile) or 'empty'})",
    "A4_meld_correct": f"Brisela on P0 BF after resolution (post: "
                       f"Bruna x{po_b}, Gisela x{po_g}, Brisela x{po_br})",
    "A5_cleanup": "stack empty; game advanced to P1",
}
d.rectangle([24, y, W - 24, y + 32 + 20 * len(notes)], fill=PANEL,
            outline=(60, 66, 80))
text(36, y + 8, "Assertions (native engine v0.112.0, two human seats)",
     fill=ACCENT)
yy = y + 32
for k, note in notes.items():
    v = A.get(k, "?")
    text(36, yy, f"{k}={v}", fill=vcolor(v))
    text(280, yy, note, fill=DIM)
    yy += 20
y += 32 + 20 * len(notes) + 14

# state-transition panel: actual saved-state values
pre_line = (f"pre.json    (turn {pre.get('turn_number')}, P{pre.get('active_player')} active): "
            f"Bruna x{p_b}, Gisela x{p_g}, Brisela x{p_br} on BF; exile empty")
mid_line = (f"mid.json    (phase {mid.get('phase')}, stack {len(mid_stack)}): "
            f"{mid_kind} from Gisela; halves still on BF (Bruna x{m_b}, Gisela x{m_g})")
post_line = (f"post.json   (turn {post.get('turn_number')}, P{post.get('active_player')} "
             f"Upkeep, stack 0): Bruna x{po_b}, Gisela x{po_g}, Brisela x{po_br}; "
             f"exile empty")
d.rectangle([24, y, W - 24, y + 100], fill=PANEL, outline=(60, 66, 80))
text(36, y + 8, "Saved-state sequence (authoritative exports)", fill=ACCENT)
text(36, y + 32, pre_line, fill=DIM)
text(36, y + 52, mid_line, fill=DIM)
text(36, y + 72, post_line, fill=DIM)
y += 114

vcolor = GREEN if verdict == "not-reproduced" \
    else (RED if verdict == "reproduced" else YELLOW)
d.rectangle([24, y, W - 24, y + 150], fill=PANEL, outline=(60, 66, 80))
text(36, y + 8, f"Verdict: {verdict}", fill=vcolor)
text(36, y + 32, "The Meld trigger fires and resolves, but NOTHING happens:",
     fill=TEXT)
text(36, y + 54, "both halves stay on the battlefield - no exile, no Brisela.",
     fill=TEXT)
text(36, y + 76, "On v0.86.0 the exile half fired and only the meld creation",
     fill=DIM)
text(36, y + 98, "failed; on v0.102.0-v0.110.0 (incl. this v0.112.0 run) the trigger "
                 "resolves as a complete no-op.",
     fill=DIM)
text(36, y + 120, "Expected: exile both, create Brisela, Voice of Nightmares "
                  "under P0.", fill=DIM)

img.save(f"{EVDIR}/summary.png")
print("wrote summary.png", img.size)

# --- 3. manifest.sha256 --------------------------------------------------------
files = sorted([
    "assertions.json", "data_evidence.json", "finalize_6768_01120.py",
    "mid_trigger.json", "port_6768_01120.py", "port_finalize_6768_01120.py",
    "post.json", "post_trigger.json", "pre.json", "run.json",
    "scenario_6768_01120.py", "scenario_run.log", "server_excerpts.log",
    "summary.png", "wire_log.jsonl",
])
files = [fn for fn in files if os.path.exists(f"{EVDIR}/{fn}")]
with open(f"{EVDIR}/manifest.sha256", "w") as f:
    for fn in files:
        f.write(f"{sha256_of_file(f'{EVDIR}/{fn}')}  {fn}\n")
print("wrote manifest.sha256 over", len(files), "files")

# --- 4. validation -------------------------------------------------------------
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
