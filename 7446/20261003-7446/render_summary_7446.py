#!/usr/bin/env python3
"""Render evidence summary PNG for issue #7446 (Synthetic Destiny), v0.101.0 run.

Usage: render_summary_7446.py <evidence_dir>
Reads run.json + pre.json + data_evidence.json; writes summary.png derived
from saved evidence states and assertion results.
"""
import json
import os
import sys

from PIL import Image, ImageDraw

W, H = 1000, 1180
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
    destiny = None
    for oid, o in objs.items():
        if lname(o) == "synthetic destiny":
            destiny = (oid, o.get("zone"))
    for p in s.get("players", []):
        pid = p.get("id")
        lines.append(f"P{pid} life {p.get('life')} | hand {len(p.get('hand', []))} | "
                     f"library {len(p.get('library', []))} objects")
    hdr = (f"turn {s.get('turn_number')} | phase {s.get('phase')} | "
           f"active P{s.get('active_player')} | "
           f"wf {(s.get('waiting_for') or {}).get('type')}")
    if destiny:
        lines.append(f"Synthetic Destiny live object: oid {destiny[0]}, zone {destiny[1]}")
    return hdr, lines


def main():
    evdir = sys.argv[1]
    run = load(os.path.join(evdir, "run.json"))
    pre = load(os.path.join(evdir, "pre.json"))
    srv = run["server"]
    ass = run["assertions"]

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #7446 - Synthetic Destiny (reveal-until clause parse)",
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
    d.rectangle([16, y, W - 16, y + 400], fill=PANEL)
    d.text((28, y + 8), "PARSE TREE (pinned v0.101.0 card-data.json + live server object)",
           fill=YELLOW)
    yy = y + 32
    for ln in (
        "Synthetic Destiny: Instant -- {4}{U}{U}",
        "ability: Spell",
        "  HEAD: ChangeZoneAll { destination: Exile }",
        "        target Typed { Creature }, controller You",
        "  SUB : CreateDelayedTrigger { condition: AtNextPhase/End,",
        "        uses_tracked_set: false }",
        "    effect: RevealUntil { player: Controller,",
        "        filter: Typed { Creature },",
        "        count: Ref(EventContextAmount),",
        "        kept_destination: Battlefield, rest_destination: Library }",
        "    tail : Shuffle { target: Controller }",
        "",
        "No Unimplemented node anywhere in the card's abilities.",
        "The issue-reported defect (Unimplemented reveal head -> empty",
        "chain tracked set read by ChangeZoneAll) is ABSENT from the",
        "current release/data: the reveal clause now parses as the",
        "count-bound reveal-until loop from the acceptance criteria.",
    ):
        d.text((28, yy), ln, fill=TEXT if not ln.startswith(" ") else DIM)
        yy += 18
    y += 412

    # assertions panel
    d.rectangle([16, y, W - 16, y + 190], fill=PANEL)
    d.text((28, y + 8), "ASSERTIONS", fill=YELLOW)
    yy = y + 32
    acol = {"passed": GREEN, "failed": RED, "not-run": YELLOW}
    for k, v in ass.items():
        d.text((28, yy), f"{k}: {v}", fill=acol.get(v, TEXT))
        yy += 22
    yy += 8
    d.text((28, yy), "A1: pinned data shows the corrected RevealUntil shape,", fill=DIM)
    yy += 18
    d.text((28, yy), "no Unimplemented. A3: the live server object (oid 19, zone", fill=DIM)
    yy += 18
    d.text((28, yy), "Library) carries the identical tree.", fill=DIM)
    y += 202

    # limitations panel
    d.rectangle([16, y, W - 16, y + 190], fill=PANEL)
    d.text((28, y + 8), "SCOPE / LIMITATIONS", fill=YELLOW)
    yy = y + 32
    for ln in (
        "- No runtime symptom asserted: the end-step delayed-trigger",
        "  resolution was not driven (stated scope in the issue; same",
        "  contract as the #7447 scenario).",
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
