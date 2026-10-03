#!/usr/bin/env python3
"""Render evidence summary PNG for issue #7448 (The Legend of Yangchen), v0.101.0 run.

Usage: render_summary_7448.py <evidence_dir>
Reads run.json + pre.json + data_evidence.json; writes summary.png derived
from saved evidence states and assertion results.
"""
import json
import os
import sys

from PIL import Image, ImageDraw

W, H = 1000, 1100
BG = (18, 20, 26)
PANEL = (26, 30, 38)
TEXT = (235, 238, 245)
DIM = (150, 160, 175)
ACCENT = (110, 180, 255)
GREEN = (110, 220, 140)
RED = (240, 120, 120)
YELLOW = (240, 200, 110)


def load(p):
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def lname(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def summarize(env):
    s = env["state"]
    objs = s.get("objects", {})
    lines = []
    heresy = None
    for oid, o in objs.items():
        if lname(o) == "the legend of yangchen":
            heresy = (oid, o.get("zone"))
    for p in s.get("players", []):
        pid = p.get("id")
        lines.append(f"P{pid} life {p.get('life')} | hand {len(p.get('hand', []))} | "
                     f"library {len(p.get('library', []))} objects")
    hdr = (f"turn {s.get('turn_number')} | phase {s.get('phase')} | "
           f"active P{s.get('active_player')} | "
           f"wf {(s.get('waiting_for') or {}).get('type')}")
    if heresy:
        lines.append(f"The Legend of Yangchen live object: oid {heresy[0]}, zone {heresy[1]}")
    return hdr, lines


def main():
    evdir = sys.argv[1]
    run = load(os.path.join(evdir, "run.json"))
    pre = load(os.path.join(evdir, "pre.json"))
    data_ev = load(os.path.join(evdir, "data_evidence.json"))
    srv = run["server"]
    ass = run["assertions"]

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #7448 - The Legend of Yangchen (chapter I clause unparsed)",
           fill=ACCENT)
    y += 26
    d.text((24, y),
           f"server v{srv['server_version']} ({srv['build_commit']}, protocol {srv['protocol_version']})"
           f" | run {run['run_id']} | {run['started_at'][:10]} | verdict: {run['verdict']}",
           fill=DIM)
    y += 30
    vcol = RED if run["verdict"] == "reproduced" else GREEN
    d.text((24, y), f"VERDICT: {run['verdict'].upper()}", fill=vcol)
    y += 34

    # pre panel
    d.rectangle([16, y, W - 16, y + 150], fill=PANEL)
    d.text((28, y + 8), "PRE (live object snapshot, game started)", fill=YELLOW)
    if pre:
        hdr, lines = summarize(pre)
        d.text((28, y + 30), hdr, fill=DIM)
        yy = y + 52
        for ln in lines:
            d.text((28, yy), ln[:116], fill=TEXT)
            yy += 20
    else:
        d.text((28, y + 34), "(missing)", fill=RED)
    y += 162

    # parse tree panel
    d.rectangle([16, y, W - 16, y + 330], fill=PANEL)
    d.text((28, y + 8), "PARSE TREE (pinned v0.101.0 card-data.json)", fill=YELLOW)
    yy = y + 32
    for ln in (
        "The Legend of Yangchen: Enchantment -- Saga",
        "trigger ch.1: CounterAdded (lore 1) -> Spell",
        "  HEAD: Unimplemented ('unparsed_verb_arguments', 'choose permanent",
        "        with mana value 3 or greater from among permanents your",
        "        opponents control')",
        "  SUB : ChangeZone { destination: Exile } target TrackedSet(0)",
        "        player_scope All, starting_with You",
        "trigger ch.2: CounterAdded (lore 2) -> Draw (opponent draws 3;",
        "  if so, you draw 3)",
        "trigger ch.3: CounterAdded (lore 3) -> ChangeZone (exile saga,",
        "  return transformed)",
        "",
        "The Unimplemented resolver is a no-op (no GameEvent), so the chain",
        "tracked set publish allocates a fresh EMPTY set; ChangeZone reads it.",
    ):
        d.text((28, yy), ln, fill=TEXT if not ln.startswith(" ") else DIM)
        yy += 18
    y += 342

    # assertions panel
    d.rectangle([16, y, W - 16, y + 220], fill=PANEL)
    d.text((28, y + 8), "ASSERTIONS", fill=YELLOW)
    yy = y + 32
    acol = {"passed": GREEN, "failed": RED, "not-run": YELLOW}
    for k, v in ass.items():
        d.text((28, yy), f"{k}: {v}", fill=acol.get(v, TEXT))
        yy += 22
    yy += 8
    d.text((28, yy), "Head-name note: the issue quotes the head name 'choose'", fill=DIM)
    yy += 18
    d.text((28, yy), "(census corpus 9b7c66e30); pinned data names it", fill=DIM)
    yy += 18
    d.text((28, yy), "'unparsed_verb_arguments' -- still Unimplemented.", fill=DIM)
    y += 232

    # limitations panel
    d.rectangle([16, y, W - 16, y + 200], fill=PANEL)
    d.text((28, y + 8), "SCOPE / LIMITATIONS", fill=YELLOW)
    yy = y + 32
    for ln in (
        "- No runtime consumer symptom asserted: ChangeZone on an empty",
        "  tracked set is unmeasured (stated in the issue); no chapter-",
        "  resolution drive performed.",
        "- Live check reads the server's live object, not a restore from",
        "  the exported snapshot.",
        "- Native engine, two human-client seats; browser UI not exercised.",
    ):
        d.text((28, yy), ln, fill=DIM)
        yy += 18

    out = os.path.join(evdir, "summary.png")
    img.save(out)
    print(f"wrote {out} ({W}x{H})")


if __name__ == "__main__":
    main()
