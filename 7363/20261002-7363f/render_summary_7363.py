#!/usr/bin/env python3
"""Render evidence summary PNG for issue #7363 (Emet-Selch of the Third Seat
may-cast from graveyard: Ponder never leaves the graveyard).

Usage: render_summary_7363.py <evidence_dir>
Reads run.json, pre.json, mid_cast.json (optional), post.json from the dir;
writes summary.png derived from the saved evidence.
"""
import json
import os
import sys

from PIL import Image, ImageDraw

W, H = 1000, 980
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


def ponder_line(env, label, oid):
    s = env["state"]
    o = (s.get("objects") or {}).get(str(oid), {})
    zone = o.get("zone")
    in_gy = oid in [str(x) for p in s.get("players", []) if p.get("id") == 0
                    for x in p.get("graveyard", [])]
    return (f"{label}: Ponder oid {oid} zone={zone} "
            f"in_P0_graveyard_list={in_gy}")


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
    mid = load_opt(evdir, "mid_cast.json")
    post = load_opt(evdir, "post.json")
    obs = run.get("observations") or {}
    oid = obs.get("target_ponder_oid")

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 24

    def text(line, color=TEXT, indent=24):
        nonlocal y
        d.text((indent, y), line, fill=color)
        y += 22

    d.text((24, y), "phase-rs/phase #7363 -- Emet-Selch of the Third Seat",
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
    text("Opponent loses life -> Emet-Selch trigger targets Ponder in", DIM)
    text("graveyard -> may-cast accepted -> Ponder moves graveyard ->", DIM)
    text("stack -> resolves (Dig) -> exiled (not back to graveyard).", DIM)
    y += 6

    d.text((24, y), "Observed (zone desync):", fill=ACCENT)
    y += 22
    text("After accepting the may-cast, a Spell entry for Ponder appears", DIM)
    text("on the stack, BUT the Ponder card object never leaves the", DIM)
    text("Graveyard (still in P0's graveyard list). The game then stalls", DIM)
    text("at ManaPayment (Ponder should cost {0} via the {2} reduction).", DIM)
    text("Ponder never resolves, never reaches exile.", DIM)
    y += 6

    if pre and oid:
        text(ponder_line(pre, "pre ", oid), DIM)
        text(stack_line(pre), DIM)
        y += 4
    if mid and oid:
        text(ponder_line(mid, "mid ", oid), DIM)
        text(stack_line(mid), DIM)
        text("mid waiting_for: %s" % ((mid["state"].get("waiting_for") or {})
                                      .get("type")), DIM)
        y += 4
    if post and oid:
        text(ponder_line(post, "post", oid), DIM)
        text(stack_line(post), DIM)
        text("post waiting_for: %s" % ((post["state"].get("waiting_for") or {})
                                       .get("type")), DIM)
        y += 6

    d.text((24, y), "Assertions:", fill=ACCENT)
    y += 24
    for k, v in (run.get("assertions") or {}).items():
        c = GREEN if v == "passed" else (RED if v == "failed" else YELLOW)
        text(f"  {k}: {v}", c)
    y += 6

    text(f"target Ponder oid: {oid}  |  "
         f"on_stack_by_ref_only: {obs.get('ponder_on_stack_by_ref_only')}",
         DIM)
    y += 6

    d.text((24, y), "Notes:", fill=ACCENT)
    y += 24
    for n in (run.get("notes") or [])[:12]:
        for seg in [n[i:i + 108] for i in range(0, len(n), 108)][:3]:
            text("  " + seg, DIM)

    out = os.path.join(evdir, "summary.png")
    img.save(out)
    print("wrote", out)


if __name__ == "__main__":
    main()
