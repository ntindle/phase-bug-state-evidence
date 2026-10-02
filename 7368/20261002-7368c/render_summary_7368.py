#!/usr/bin/env python3
"""Render evidence summary PNG for issue #7368 (Battle at the Helvault:
declining the chapter's up-to-one target slot ends the chapter instead of
iterating to the next player).

Usage: render_summary_7368.py <evidence_dir>
Reads run.json, data_evidence.json, slots.json, pre.json,
mid_decline.json (optional), post.json; writes summary.png derived from the
saved evidence.
"""
import json
import os
import sys

from PIL import Image, ImageDraw

W, H = 1000, 1060
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


def bf_summary(env):
    s = env["state"]
    objs = s.get("objects", {}) or {}
    out = {}
    for pid in (0, 1):
        names = {}
        for oid, o in objs.items():
            if o.get("zone") == "Battlefield" and o.get("controller") == pid:
                n = str(o.get("base_name") or "?")
                names[n] = names.get(n, 0) + 1
        out[pid] = names
    exile = [str(o.get("base_name")) for o in objs.values()
             if o.get("zone") == "Exile"]
    saga_lore = None
    for oid, o in objs.items():
        if str(o.get("base_name", "")).lower() == "battle at the helvault":
            saga_lore = (o.get("zone"), o.get("counters"))
            break
    return (f"turn={s.get('turn_number')} phase={s.get('phase')} "
            f"stack={len(s.get('stack') or [])} | P0 BF={out[0]} | "
            f"P1 BF={out[1]} | exile={exile} | saga(zone,lore)={saga_lore}")


def main():
    evdir = sys.argv[1]
    run = load(os.path.join(evdir, "run.json"))
    slots = load_opt(evdir, "slots.json") or []
    pre = load_opt(evdir, "pre.json")
    mid = load_opt(evdir, "mid_decline.json")
    post = load_opt(evdir, "post.json")

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 24

    def text(line, color=TEXT, indent=24):
        nonlocal y
        d.text((indent, y), line, fill=color)
        y += 22

    d.text((24, y), "phase-rs/phase #7368 -- Battle at the Helvault: decline "
                     "ends the chapter, no iteration", fill=ACCENT)
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
    text("Chapters I/II: 'For each player, exile up to one target non-Saga,", DIM)
    text("nonland permanent that player controls'. Declining the", DIM)
    text("controller's slot must still offer a slot for the next player.", DIM)
    y += 6

    d.text((24, y), "Target slots observed (TriggerTargetSelection):", fill=ACCENT)
    y += 22
    if not slots:
        text("none -- no target prompt was ever offered", RED)
    for s in slots:
        c0 = s["candidates"][0] if s["candidates"] else {}
        text(f"slot {s['index']}: advertised to {s['advertised_to']}, "
             f"{s['candidate_summary']['n']} candidate(s), controllers="
             f"{s['candidate_summary']['controllers']}", DIM)
        text(f"    e.g. {c0.get('candidate_id', '?')} -> "
             f"{c0.get('text', '')[:70]}", DIM)
        text(f"    decision: {s['decision']}", DIM)
    y += 6

    d.text((24, y), "The failure:", fill=ACCENT)
    y += 22
    text("slot 0 (chapter I, controller's own Vanguard) declined via an", DIM)
    text("empty target list -- legal, 'up to one'. No second slot for the", RED)
    text("opponent's permanents was ever offered; chapter I resolved with", RED)
    text("zero exiles. Chapter II later offered one P0-scoped slot again.", DIM)
    y += 6

    for env, label in ((pre, "pre "), (mid, "mid "), (post, "post")):
        if env:
            for seg in [bf_summary(env)[i:i + 106]
                        for i in range(0, len(bf_summary(env)), 106)][:4]:
                text(label + seg, DIM)
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
    for n in (run.get("notes") or [])[:7]:
        for seg in [n[i:i + 106] for i in range(0, len(n), 106)][:3]:
            text("  " + seg, DIM)

    out = os.path.join(evdir, "summary.png")
    img.save(out)
    print("wrote", out)


if __name__ == "__main__":
    main()
