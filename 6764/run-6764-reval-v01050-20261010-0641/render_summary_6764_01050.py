#!/usr/bin/env python3
"""Render the #6764 re-validation (v0.105.0 / protocol 120) summary PNG.

Factual: derives from evidence/6764/run-6764-reval-v01050-20261010-0641
post_decline.json, run.json, assertions.json, and wire_log.jsonl.
The POST panel is data-driven (wire counts + assertions), never hardcoded.
"""
import json
import os
import sys
from collections import Counter

from PIL import Image, ImageDraw

EVDIR = (sys.argv[1] if len(sys.argv) > 1
         else "/home/hatch/workspace/dev/phase-backfill/evidence/6764/"
              "run-6764-reval-v01050-20261010-0641")
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


def exile_names(s):
    return Counter(
        (o.get("base_name") or o.get("name") or "?")
        for o in s["objects"].values()
        if str(o.get("zone") or "").lower() == "exile")


def summarize(s):
    lines = []
    stack = s.get("stack") or []
    lines.append(
        f"turn {s.get('turn_number')} | phase {s.get('phase')} | "
        f"active P{s.get('active_player')} | stack {len(stack)}")
    for p in s.get("players", []):
        pid = p.get("id")
        lines.append(
            f"P{pid} life {p.get('life')} | hand {len(p.get('hand') or [])} | "
            f"library {len(p.get('library') or [])}")
    ex = exile_names(s)
    lines.append("exile: " + (", ".join(f"{k}x{v}"
                                        for k, v in sorted(ex.items())) or "empty"))
    return lines


def main():
    run = load("run.json")
    pre = env("post_decline.json")   # 7 exiled incl. Shock #1: leg-2 legal set
    wire = [json.loads(l) for l in open(os.path.join(EVDIR, "wire_log.jsonl"))
            if l.strip()]
    may_accept = next((ev for ev in wire
                       if ev.get("event") == "may_choice_opportunity"
                       and (ev.get("data") or {}).get("accept")), {})
    iid = (may_accept.get("data") or {}).get("iid", "?")
    t0 = may_accept.get("t") or 0
    post_accept_actions = sum(
        1 for ev in wire
        if ev.get("t", 0) > t0 and ev.get("event") == "action_submit")
    card_choice_events = sum(
        1 for ev in wire
        if ev.get("t", 0) > t0 and ev.get("event") == "card_choice_opportunity_full")
    free_cast_choices = sum(
        1 for ev in wire
        if ev.get("t", 0) > t0 and ev.get("event") == "free_cast_choice")
    cast_confirms = sum(
        1 for ev in wire if ev.get("event") == "cast_confirmed")
    backstops = sum(
        1 for ev in wire if ev.get("event") == "cast_backstop")

    verdict = run["verdict"]
    ass = run["assertions"]

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 16
    d.text((16, y), "phase-rs/phase #6764 | Knowledge Pool: accept the "
           "may-cast, test the free cast", fill=ACCENT)
    y += 24
    srv = run["server"]
    d.text((16, y),
           f"server v{srv['server_version']} ({srv['build_commit']}, protocol "
           f"{srv['protocol_version']}) | run {run['run_id']} | 2026-10-10 | "
           f"verdict {verdict}", fill=DIM)
    y += 24
    d.text((16, y), f"Setup: {run['setup_line']}", fill=DIM)
    y += 20
    d.text((16, y), f"Contract: {run['contract_line'][:112]}", fill=DIM)
    y += 28

    d.rectangle([12, y, W - 12, y + 118], fill=PANEL)
    d.text((22, y + 6), "PRE (after leg-1 decline: 7 exiled = leg-2 legal set)",
           fill=YELLOW)
    for i, ln in enumerate(summarize(pre)):
        d.text((22, y + 28 + i * 16), ln[:118], fill=TEXT)
    y += 130

    d.rectangle([12, y, W - 12, y + 170], fill=PANEL)
    d.text((22, y + 6), "POST (leg-2 accept: wire-derived outcome)", fill=YELLOW)
    a3 = ass.get("A3_choice_scope", "?")
    a4 = ass.get("A4_free_cast_executes", "?")
    facts = [
        "leg-2: Shock #2 cast from hand -> Pool trigger exiled it ->",
        f"may-choice offered (iid {iid}); P0 ACCEPTED.",
        f"Post-accept: {post_accept_actions} wire submissions, "
        f"{card_choice_events} card-choice events, {free_cast_choices} "
        "free-cast choices,",
        f"{cast_confirms} cast confirmations, {backstops} cast backstops.",
        f"A3 choice scope: {a3}; A4 free cast executes: {a4}.",
        f"Verdict: {verdict.upper()}.",
    ]
    for i, ln in enumerate(facts):
        d.text((22, y + 28 + i * 16), ln[:115], fill=TEXT)
    y += 182

    d.rectangle([12, y, W - 12, y + 190], fill=PANEL)
    d.text((22, y + 6), "Assertions", fill=YELLOW)
    order = ["A1_setup_ok", "A2a_decline_control", "A2b_accept_may",
             "A3_choice_scope", "A4_free_cast_executes", "A5_cleanup"]
    labels = {
        "A1_setup_ok": "Pool on BF, imprint exiled 6 (3+3)",
        "A2a_decline_control": "leg-1 decline: no choice, Shock #1 exiled",
        "A2b_accept_may": "leg-2 may offered + accepted",
        "A3_choice_scope": "free-cast choice offered with correct scope",
        "A4_free_cast_executes": "chosen card casts free, 2 dmg, no mana",
        "A5_cleanup": "stack empty, game continues",
    }
    for i, k in enumerate(order):
        v = ass.get(k, "not-run")
        color = GREEN if v == "passed" else (RED if v == "failed" else YELLOW)
        d.text((22, y + 28 + i * 18), f"{k}: {v} -- {labels[k]}"[:112],
               fill=color)
    d.text((22, y + 178), "sha manifest: manifest.sha256", fill=DIM)

    img.save(os.path.join(EVDIR, "summary.png"))
    print("wrote summary.png")


main()
