#!/usr/bin/env python3
"""Render the #6760 re-validation (v0.114.0 / protocol 153) summary PNG.

Factual: derives from evidence/6760/run-6760-reval-v01140-20261020-0541 pre_select_*.json,
post_select_*.json, run.json, and wire_log.jsonl.
"""
import glob
import json
import os
from collections import Counter

from PIL import Image, ImageDraw

EVDIR = "/home/hatch/workspace/dev/phase-backfill/evidence/6760/run-6760-reval-v01140-20261020-0541"
W, H = 1000, 800
BG = (18, 20, 26)
PANEL = (26, 30, 38)
TEXT = (235, 238, 245)
DIM = (150, 160, 175)
ACCENT = (110, 180, 255)
GREEN = (110, 220, 140)
RED = (240, 120, 120)
YELLOW = (240, 200, 110)


def load(p):
    with open(os.path.join(EVDIR, p)) as f:
        return json.load(f)


def env(p):
    return load(p)["state"]


def first(pattern):
    hits = sorted(glob.glob(os.path.join(EVDIR, pattern)))
    return os.path.basename(hits[0]) if hits else None


def bf_counts(s, pid):
    return Counter(
        (o.get("base_name") or o.get("name") or "?")
        for o in s["objects"].values()
        if o.get("zone") == "Battlefield" and o.get("controller") == pid
    )


def summarize(s):
    lines = []
    stack = s.get("stack") or []
    wf = s.get("waiting_for") or {}
    lines.append(
        f"turn {s.get('turn_number')} | phase {s.get('phase')} | "
        f"active P{s.get('active_player')} | stack {len(stack)} | wf {wf.get('type')}"
    )
    for p in s.get("players", []):
        pid = p.get("id")
        hand = [(s["objects"].get(str(o)) or {}).get("base_name") or "?"
                for o in (p.get("hand") or [])]
        lines.append(
            f"P{pid} life {p.get('life')} | hand({len(hand)}): "
            + (", ".join(hand[:8]) + ("..." if len(hand) > 8 else ""))
        )
        bc = bf_counts(s, pid)
        lands = sum(v for k, v in bc.items()
                    if k in ("Forest", "Island", "Plains", "Mountain", "Swamp"))
        lines.append(
            "    battlefield: "
            + (", ".join(f"{k}x{v}" for k, v in sorted(bc.items())) or "empty")
            + (f" | lands {lands}" if lands else "")
            + f" | library {len(p.get('library') or [])}"
        )
    return lines


def main():
    run = load("run.json")
    pre_name = first("pre_select_*.json")
    post_name = first("post_select_*.json")
    pre = env(pre_name) if pre_name else {}
    post = env(post_name) if post_name else {}
    wire = [json.loads(l) for l in open(os.path.join(EVDIR, "wire_log.jsonl"))
            if l.strip()]
    spec = None
    for ev in wire:
        if ev.get("event") == "select_opportunity_full":
            spec = (ev.get("data") or {}).get("spec")
            break
    rej = next((ev["data"] for ev in wire
                if ev.get("event") == "rejection" and (ev.get("data") or {}).get("who") == "P0"
                and (ev.get("data") or {}).get("type") == "ActionRejected"),
               {})

    verdict = run["verdict"]
    reproduced = verdict == "reproduced"

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 16
    d.text((16, y), "phase-rs/phase #6760 | Ojer Kaslem, Deepest Growth: only 1 "
           "creature-or-land, not both", fill=ACCENT)
    y += 24
    srv = run["server"]
    d.text((16, y),
           f"server v{srv['server_version']} ({srv['build_commit']}, protocol "
           f"{srv['protocol_version']}) | run {run['run_id']} | 2026-10-20 | "
           f"verdict {verdict}", fill=DIM)
    y += 24
    d.text((16, y), f"Setup: {run['setup_line']}", fill=DIM)
    y += 20
    d.text((16, y), f"Contract: {run['contract_line']}", fill=DIM)
    y += 28

    pre_title = ("PRE (bug manifests here: select prompt offers max 1 card)"
                 if reproduced else "PRE (revealed 6: creature+land both selectable)")
    post_title = ("POST (fallback: 1 creature entered, land never put onto battlefield)"
                  if reproduced else "POST (2-card selection accepted: creature + land on battlefield)")
    for title, s in ((pre_title, pre), (post_title, post)):
        d.rectangle([12, y, W - 12, y + 118], fill=PANEL)
        d.text((22, y + 6), title, fill=YELLOW)
        for i, ln in enumerate(summarize(s) if s else ["(no state captured)"]):
            d.text((22, y + 28 + i * 16), ln[:118], fill=TEXT)
        y += 130

    d.rectangle([12, y, W - 12, y + 128], fill=PANEL)
    d.text((22, y + 6), "Decisive engine behavior", fill=YELLOW)
    constraint = json.dumps((spec or {}).get("data")) if spec else "n/a"
    if reproduced:
        facts = [
            f"select prompt constraint: {constraint}",
            "2-card submission (1 creature + 1 land): REJECTED "
            f"({(rej.get('data') or '')[:90]})",
            "1-card fallback accepted: creature entered; other 5 revealed "
            "cards to bottom of library",
        ]
    else:
        facts = [
            f"select prompt constraint: {constraint}",
            "2-card submission (1 creature + 1 land): ACCEPTED",
            "both cards entered P0's battlefield; rest to bottom of library",
        ]
    for i, ln in enumerate(facts):
        d.text((22, y + 30 + i * 18), ln[:115], fill=TEXT)
    y += 140

    d.rectangle([12, y, W - 12, y + 170], fill=PANEL)
    d.text((22, y + 6), "Assertions", fill=YELLOW)
    order = ["A1_setup_ok", "A2_damage_and_reveal", "A3_may_accepted",
             "A4_both_categories", "A5_two_cards_enter", "A6_rest_to_bottom",
             "A7_cleanup"]
    for i, k in enumerate(order):
        v = run["assertions"].get(k, "not-run")
        color = GREEN if v == "passed" else (RED if v == "failed" else YELLOW)
        d.text((22, y + 28 + i * 18), f"{k}: {v}", fill=color)
    d.text((22, y + 158), "sha manifest: manifest.sha256", fill=DIM)

    img.save(os.path.join(EVDIR, "summary.png"))
    print("wrote summary.png")


main()
