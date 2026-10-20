#!/usr/bin/env python3
"""Render the evidence summary PNG for issue #6349 (v0.114.0 re-validation).

Usage: render_summary_6349_01140.py <evidence_dir>
Reads run.json (+ pre.json / post.json when present); writes summary.png.
Every number/name on the image is read from the saved states/assertions.
Defensive: renders a failure panel when pre/post snapshots are missing.
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


def main():
    evdir = sys.argv[1]
    run = load(os.path.join(evdir, "run.json"))
    srv = run["server"]
    pre_path = os.path.join(evdir, "pre.json")
    post_path = os.path.join(evdir, "post.json")
    pre = load(pre_path) if os.path.exists(pre_path) else None
    post = load(post_path) if os.path.exists(post_path) else None

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #6349 \u2014 Swiss bye is not credited a match win in odd-sized pods", fill=ACCENT)
    y += 26
    d.text(
        (24, y),
        f"server {srv['server_version']} ({srv['build_commit']}, protocol {srv['protocol_version']})"
        f" | run {run['run_id']} | 2026-10-20 | verdict: {run['verdict']}",
        fill=DIM,
    )
    y += 30
    d.text((24, y), "Setup: 3-seat Swiss Sealed draft pod, all seats submit 40-card decks, round-1 pairings auto-generated", fill=TEXT)
    y += 24
    d.text(
        (24, y),
        "Contract: the unpaired (bye) seat must end the round with match_wins == 1 and top the standings",
        fill=DIM,
    )
    y += 32

    if pre is not None:
        pv = pre["view"]
        d.rectangle([16, y, W - 16, y + 150], fill=PANEL, outline=(45, 52, 64))
        d.text((28, y + 8), "PRE \u2014 all decks submitted, before pairings", fill=YELLOW)
        yy = y + 34
        d.text((28, yy), f"draft_code={pre['draft_code']} status={pv.get('status')} current_round={pv.get('current_round')}", fill=TEXT)
        yy += 24
        d.text((28, yy), "seats: " + ", ".join(
            f"seat {s.get('seat_index')} ({s.get('display_name')}) submitted={s.get('has_submitted_deck')}"
            for s in pv.get("seats", [])
        ), fill=TEXT)
        yy += 24
        d.text((28, yy), f"standings pre-pairings: {pv.get('standings', [])} (expected empty)", fill=DIM)
        y += 162
    else:
        d.rectangle([16, y, W - 16, y + 60], fill=PANEL, outline=(45, 52, 64))
        d.text((28, y + 8), "PRE \u2014 pre.json missing (run failed before pairings)", fill=RED)
        y += 72

    if post is not None:
        qv = post["view"]
        d.rectangle([16, y, W - 16, y + 250], fill=PANEL, outline=(45, 52, 64))
        d.text((28, y + 8), "POST \u2014 round-1 pairings generated, spectator view", fill=YELLOW)
        yy = y + 34
        d.text((28, yy), f"status={qv.get('status')} current_round={qv.get('current_round')}", fill=TEXT)
        yy += 24
        prs = qv.get("pairings", [])
        if prs:
            p = prs[0]
            d.text(
                (28, yy),
                f"pairings: 1 table: seat {p.get('seat_a')} ({p.get('name_a')}) vs seat {p.get('seat_b')} ({p.get('name_b')})"
                f" status={p.get('status')}",
                fill=TEXT,
            )
            yy += 24
        all_seats = {s.get("seat_index") for s in qv.get("seats", [])}
        paired = {p.get("seat_a") for p in prs} | {p.get("seat_b") for p in prs}
        bye = sorted(all_seats - paired)
        d.text((28, yy), f"paired seats: {sorted(paired)}; unpaired (bye) seat: {bye}", fill=ACCENT)
        yy += 24
        for s in qv.get("standings", []):
            tag = " <- BYE" if s.get("seat_index") in bye else ""
            d.text(
                (28, yy),
                f"standings: seat {s.get('seat_index')} ({s.get('display_name')}): "
                f"match_wins={s.get('match_wins')} match_losses={s.get('match_losses')}{tag}",
                fill=TEXT,
            )
            yy += 24
        y += 262
    else:
        d.rectangle([16, y, W - 16, y + 60], fill=PANEL, outline=(45, 52, 64))
        d.text((28, y + 8), "POST \u2014 post.json missing (pairings never generated)", fill=RED)
        y += 72

    # Assertions panel
    n = len(run["assertions"])
    d.rectangle([16, y, W - 16, y + 44 + 24 * n], fill=PANEL, outline=(45, 52, 64))
    d.text((28, y + 8), "Assertions", fill=YELLOW)
    yy = y + 34
    for a in run["assertions"]:
        color = GREEN if a["status"] == "passed" else (RED if a["status"] == "failed" else DIM)
        d.text((28, yy), f"{a['name']}: {a['status']} \u2014 {a.get('detail', '')}"[:120], fill=color)
        yy += 24
    y += 44 + 24 * n + 12

    lims = run.get("limitations", [])
    d.text((24, y), "Limitations: " + "; ".join(lims)[:150], fill=DIM)

    out = os.path.join(evdir, "summary.png")
    img.save(out)
    print("wrote", out)


main()
