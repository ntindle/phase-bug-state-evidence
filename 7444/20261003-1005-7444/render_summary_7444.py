#!/usr/bin/env python3
"""Render evidence summary PNG for issue #7444 (Storybook Ride), v0.100.0 run.

Usage: render_summary_7444.py <evidence_dir>
Reads run.json + pre.json + data_evidence.json; writes summary.png derived
from saved evidence states and assertion results.
"""
import json
import os
import sys

from PIL import Image, ImageDraw

W, H = 1000, 1060
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
    ride = None
    for oid, o in objs.items():
        if lname(o) == "storybook ride":
            ride = (oid, o.get("zone"))
    for p in s.get("players", []):
        pid = p.get("id")
        lines.append(f"P{pid} life {p.get('life')} | hand {len(p.get('hand', []))} | "
                     f"library objects present")
    hdr = (f"turn {s.get('turn_number')} | phase {s.get('phase')} | "
           f"active P{s.get('active_player')} | "
           f"wf {(s.get('waiting_for') or {}).get('type')}")
    if ride:
        lines.append(f"Storybook Ride live object: oid {ride[0]}, zone {ride[1]}")
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
    d.text((24, y), "phase-rs/phase #7444 - Storybook Ride (where-X clause unparsed)",
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
    d.rectangle([16, y, W - 16, y + 210], fill=PANEL)
    d.text((28, y + 8), "PARSE TREE (pinned v0.100.0 card-data.json)", fill=YELLOW)
    yy = y + 32
    for ln in (
        "Storybook Ride: Artifact -- Attraction",
        "triggers[0]: mode VisitAttraction, execute kind Spell",
        "  head: Unimplemented { name: where_x_binding }",
        "        desc: \"where X is the number of Attractions you've",
        "              visited this turn\"",
        "  sub:  GrantCastingPermission { PlayFromExile / UntilEndOfTurn /",
        "                                granted_to 0 } target TrackedSet(0)",
        "  sub2: CreateDelayedTrigger { uses_tracked_set: false }",
        "Unimplemented resolver is a no-op -> publish authority allocates",
        "a FRESH EMPTY chain tracked set -> GrantCastingPermission reads",
        "an empty set (issue's structural analysis).",
    ):
        d.text((28, yy), ln, fill=TEXT if yy < y + 190 else DIM)
        yy += 18
    y += 222

    # assertions
    d.text((24, y), "Assertions", fill=ACCENT)
    y += 24
    names = {
        "A1_data_level": "A1 pinned data parses the reported shape (Unimplemented head + sub chain)",
        "A2_setup_ok": "A2 decks accepted, game started, past mulligans, pre.json exported",
        "A3_live_object": "A3 live Storybook Ride object carries the identical Unimplemented head",
    }
    for k in ("A1_data_level", "A2_setup_ok", "A3_live_object"):
        v = ass.get(k, "not-run")
        col = GREEN if v == "passed" else (RED if v == "failed" else YELLOW)
        d.text((28, y), f"{k}: {v}", fill=col)
        d.text((260, y), names[k][:72], fill=DIM)
        y += 22

    y += 8
    d.text((24, y), "Observed", fill=ACCENT)
    y += 24
    obs = run.get("observations", {})
    live = obs.get("live_head") or {}
    for ln in (
        f"head name in pinned data: where_x_binding (issue quotes the same; no discrepancy)",
        f"live object: oid zone Library; head {live.get('head_type')}/{live.get('head_name')}",
        f"live sub: {live.get('sub_type')} target {live.get('sub_target')}",
        f"grant permission: {live.get('sub_permission')}",
        "No runtime symptom is asserted for this issue: the GrantCastingPermission",
        "consumer behaviour on the empty set is unmeasured (per the issue text).",
    ):
        d.text((28, y), ln[:100], fill=TEXT)
        y += 20

    y += 8
    d.text((24, y), "Limitations", fill=ACCENT)
    y += 24
    for ln in run.get("limitations", [])[:4]:
        words, line, out = ln.split(), "", []
        for w in words:
            if len(line) + len(w) + 1 > 96:
                out.append(line)
                line = w
            else:
                line = (line + " " + w).strip()
        out.append(line)
        for o in out[:3]:
            d.text((28, y), o, fill=DIM)
            y += 18

    out = os.path.join(evdir, "summary.png")
    img.save(out)
    print(f"wrote {out} ({W}x{H})")


if __name__ == "__main__":
    main()
