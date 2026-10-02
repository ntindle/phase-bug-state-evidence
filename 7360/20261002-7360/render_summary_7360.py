#!/usr/bin/env python3
"""Render evidence summary PNG for issue #7360 (Prishe's Wanderings reflexive
counter trigger never fires).

Usage: render_summary_7360.py <evidence_dir>
Reads run.json, pre.json, mid_search.json (optional), post.json from the dir;
writes summary.png derived from the saved evidence.
"""
import json
import os
import sys

from PIL import Image, ImageDraw

W, H = 1000, 900
BG = (18, 20, 26)
PANEL = (26, 30, 38)
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


def bear_line(env, label):
    s = env["state"]
    for oid, o in s.get("objects", {}).items():
        if (o.get("zone") == "Battlefield"
                and str(o.get("base_name") or "").lower() == "grizzly bears"
                and o.get("controller") == 0):
            return (f"{label}: Bear oid {oid} tapped={o.get('tapped')} "
                    f"counters={o.get('counters') or {}}")
    return f"{label}: no Bear on P0 battlefield"


def land_line(env, label):
    s = env["state"]
    bf = sum(1 for o in s.get("objects", {}).values()
             if o.get("zone") == "Battlefield" and o.get("controller") == 0
             and str(o.get("base_name") or "").lower() == "forest")
    lib = 0
    for p in s.get("players", []):
        if p.get("id") == 0:
            lib = len(p.get("library", []))
    return f"{label}: BF Forests={bf}, P0 library={lib}"


def stack_line(env):
    s = env["state"]
    kinds = [((e.get("kind") or {}).get("type"), e.get("source_id"))
             for e in s.get("stack", []) or []]
    if not kinds:
        return "stack(0): empty"
    return ("stack(%d): " % len(kinds)) + ", ".join(
        f"{k}(src={src})" for k, src in kinds)


def main():
    evdir = sys.argv[1]
    run = load(os.path.join(evdir, "run.json"))
    pre = load_opt(evdir, "pre.json")
    mid = load_opt(evdir, "mid_search.json")
    post = load_opt(evdir, "post.json")

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 24

    def text(line, color=TEXT, indent=24):
        nonlocal y
        d.text((indent, y), line, fill=color)
        y += 22

    d.text((24, y), "phase-rs/phase #7360 -- Prishe's Wanderings counter trigger",
           fill=ACCENT)
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
    text("Cast Prishe's Wanderings with a creature on the battlefield ->", DIM)
    text("reflexive trigger prompts for the +1/+1 counter target and", DIM)
    text("places the counter on the creature.", DIM)
    y += 6

    if pre:
        text(bear_line(pre, "pre "), DIM)
        text(land_line(pre, "pre "), DIM)
        text(stack_line(pre), DIM)
        y += 4
    if mid:
        text(bear_line(mid, "mid "), DIM)
        text(land_line(mid, "mid "), DIM)
        text(stack_line(mid), DIM)
        y += 4
    if post:
        text(bear_line(post, "post"), DIM)
        text(land_line(post, "post"), DIM)
        text(stack_line(post), DIM)
        text("waiting_for: %s" % ((post["state"].get("waiting_for") or {})
                                  .get("type")), DIM)
        y += 6

    d.text((24, y), "Assertions:", fill=ACCENT)
    y += 24
    for k, v in (run.get("assertions") or {}).items():
        c = GREEN if v == "passed" else (RED if v == "failed" else YELLOW)
        text(f"  {k}: {v}", c)
    y += 6

    obs = run.get("observations") or {}
    text(f"wanderings casts: {obs.get('wand_casts')} "
         f"(turns {obs.get('wand_cast_turns')})", DIM)
    text(f"trigger on stack: {obs.get('trigger_seen')}  |  "
         f"target prompt: {obs.get('target_prompt_seen')}  |  "
         f"probe prompt: {obs.get('probe_prompt_seen')}", DIM)
    y += 6

    d.text((24, y), "Notes:", fill=ACCENT)
    y += 24
    for n in (run.get("notes") or [])[:10]:
        for seg in [n[i:i + 110] for i in range(0, len(n), 110)][:3]:
            text("  " + seg, DIM)

    out = os.path.join(evdir, "summary.png")
    img.save(out)
    print("wrote", out)


if __name__ == "__main__":
    main()
