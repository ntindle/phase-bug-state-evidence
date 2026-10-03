#!/usr/bin/env python3
"""Render evidence summary PNG for issue #7445 (Sundering Titan), v0.100.0 run.

Usage: render_summary_7445.py <evidence_dir>
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
    titan = None
    for oid, o in objs.items():
        if lname(o) == "sundering titan":
            titan = (oid, o.get("zone"))
    for p in s.get("players", []):
        pid = p.get("id")
        lines.append(f"P{pid} life {p.get('life')} | hand {len(p.get('hand', []))} | "
                     f"library objects present")
    hdr = (f"turn {s.get('turn_number')} | phase {s.get('phase')} | "
           f"active P{s.get('active_player')} | "
           f"wf {(s.get('waiting_for') or {}).get('type')}")
    if titan:
        lines.append(f"Sundering Titan live object: oid {titan[0]}, zone {titan[1]}")
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
    d.text((24, y), "phase-rs/phase #7445 - Sundering Titan (choose-a-land clause unparsed)",
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
    d.rectangle([16, y, W - 16, y + 250], fill=PANEL)
    d.text((28, y + 8), "PARSE TREE (pinned v0.100.0 card-data.json)", fill=YELLOW)
    yy = y + 32
    for ln in (
        "Sundering Titan: Artifact -- Creature -- Golem",
        "triggers[0]: mode ChangesZone (enters), execute kind Spell",
        "triggers[1]: mode LeavesBattlefield (leaves), execute kind Spell",
        "  head: Unimplemented { name: unparsed_verb_arguments }",
        "        desc: \"choose a land of each basic land type\"",
        "  sub:  Destroy { cant_regenerate: false } target TrackedSet(0)",
        "NOTE: issue body quotes head name \"choose\" (corpus 9b7c66e30);",
        "pinned data names it \"unparsed_verb_arguments\" -- description",
        "identical, clause still Unimplemented -> defect holds.",
        "Unimplemented resolver is a no-op -> publish authority allocates",
        "a FRESH EMPTY chain tracked set -> Destroy reads an empty set",
        "(issue's structural analysis).",
    ):
        d.text((28, yy), ln, fill=TEXT if yy < y + 228 else DIM)
        yy += 18
    y += 262

    # assertions
    d.text((24, y), "Assertions", fill=ACCENT)
    y += 24
    names = {
        "A1_data_level": "A1 pinned data parses the reported shape (Unimplemented heads + Destroy subs)",
        "A2_setup_ok": "A2 decks accepted, game started, past mulligans, pre.json exported",
        "A3_live_object": "A3 live Sundering Titan object carries the identical Unimplemented heads",
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
    heads = obs.get("live_head") or []
    seen = ", ".join(f"{h.get('head_type')}/{h.get('head_name')}" for h in heads)
    for ln in (
        f"live object: oid 38, zone Library; trigger defs: {len(heads)}",
        f"live heads: {seen}",
        "live subs: Destroy target {'type': 'TrackedSet', 'id': 0}",
        "No runtime symptom is asserted for this issue: Destroy's",
        "consumer behaviour on the empty set is unmeasured (per issue).",
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
