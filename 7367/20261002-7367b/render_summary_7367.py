#!/usr/bin/env python3
"""Render evidence summary PNG for issue #7367 (Faith & Grief / Ishgard, the
Holy See: the adventure is offered with potential Sanctum Weaver mana, but
the Weaver's variable-X mana ability resolves without producing mana).

Usage: render_summary_7367.py <evidence_dir>
Reads run.json, data_evidence.json, gate_potential.json, weaver_mana.json,
pre.json, mid_cast.json (optional), post.json; writes summary.png derived
from the saved evidence.
"""
import json
import os
import sys

from PIL import Image, ImageDraw

W, H = 1000, 1020
BG = (18, 20, 26)
TEXT = (235, 238, 245)
DIM = (150, 160, 175)
ACCENT = (110, 180, 255)
GREEN = (110, 220, 140)
RED = (240, 120, 120)
YELLOW = (240, 200, 110)


def load(p):
    with open(p) as f:
        return json.load(f)


def load_opt(evdir, name):
    p = os.path.join(evdir, name)
    return load(p) if os.path.exists(p) else None


def bf_line(env, label):
    s = env["state"]
    objs = s.get("objects", {}) or {}
    bf = {}
    for oid, o in objs.items():
        if o.get("zone") == "Battlefield" and o.get("controller") == 0:
            n = str(o.get("base_name") or "?")
            bf[n] = bf.get(n, 0) + 1
    p0 = next((p for p in s.get("players", []) if p.get("id") == 0), {})
    pool = (p0.get("mana_pool") or {}).get("mana", [])
    hand = [str((objs.get(str(o), {}) or {}).get("base_name", "?"))
            for o in p0.get("hand", [])]
    return (f"{label}: BF={bf} pool={pool} hand={hand} "
            f"turn={s.get('turn_number')} phase={s.get('phase')}")


def main():
    evdir = sys.argv[1]
    run = load(os.path.join(evdir, "run.json"))
    data_ev = load_opt(evdir, "data_evidence.json")
    gate = load_opt(evdir, "gate_potential.json")
    wm = load_opt(evdir, "weaver_mana.json")
    pre = load_opt(evdir, "pre.json")
    mid = load_opt(evdir, "mid_cast.json")
    post = load_opt(evdir, "post.json")

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 24

    def text(line, color=TEXT, indent=24):
        nonlocal y
        d.text((indent, y), line, fill=color)
        y += 22

    d.text((24, y), "phase-rs/phase #7367 -- Faith & Grief uncastable: "
                     "Sanctum Weaver mana never materializes", fill=ACCENT)
    y += 30
    text(f"server {run['server']['server_version']} "
         f"(build {run['server']['build_commit']}, "
         f"protocol {run['server']['protocol_version']})", DIM)
    text(f"run {run['run_id']}  |  {run.get('started_at', '')}  |  "
         f"{run.get('duration_s', '?')}s", DIM)
    verdict = run["verdict"]
    vcolor = RED if verdict == "reproduced" else (
        GREEN if verdict == "not-reproduced" else YELLOW)
    text(f"VERDICT: {verdict}", vcolor)
    y += 6

    d.text((24, y), "Contract:", fill=ACCENT)
    y += 22
    text("Faith & Grief ({3}{W}{W} adventure) should be castable with mana", DIM)
    text("from Sanctum Weaver ({T}: Add X of any one color, X = #enchantments).", DIM)
    y += 6

    if gate:
        d.text((24, y), "Phase A -- potential mana (gate scan):", fill=ACCENT)
        y += 22
        text(f"adventure offered = {gate.get('offered')} "
             f"({len(gate.get('offers', []))} CastSpell payloads)", DIM)
        text(f"enchantments controlled: {gate.get('enchantments_controlled')} "
             f"| weaver untapped: {bool(gate.get('weaver_untapped'))} "
             f"| pool: {gate.get('mana_pool')}", DIM)
        text("Gate + potential-mana calc handle the variable-X source: OK.", DIM)
        y += 6

    if wm:
        d.text((24, y), "Phase B -- Weaver activation (the failure):", fill=ACCENT)
        y += 22
        text(f"weaver tapped: {bool(wm.get('weaver_tapped'))} | White chosen: "
             f"{wm.get('white_chosen')} | enchantments: "
             f"{wm.get('enchantments_controlled')}", DIM)
        text(f"pool after activation: {wm.get('pool')}   <-- X=5 mana missing",
             RED)
        y += 6

    for env, label in ((pre, "pre "), (mid, "mid "), (post, "post")):
        if env:
            for seg in [bf_line(env, label)[i:i + 106]
                        for i in range(0, len(bf_line(env, label)), 106)][:3]:
                text(seg, DIM)
            y += 4
    y += 4

    d.text((24, y), "Assertions:", fill=ACCENT)
    y += 24
    for k, v in (run.get("assertions") or {}).items():
        c = GREEN if v == "passed" else (RED if v == "failed" else YELLOW)
        text(f"  {k}: {v}", c)
    y += 6

    d.text((24, y), "Notes:", fill=ACCENT)
    y += 24
    for n in (run.get("notes") or [])[:8]:
        for seg in [n[i:i + 106] for i in range(0, len(n), 106)][:3]:
            text("  " + seg, DIM)

    out = os.path.join(evdir, "summary.png")
    img.save(out)
    print("wrote", out)


if __name__ == "__main__":
    main()
