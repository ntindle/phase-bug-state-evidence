#!/usr/bin/env python3
"""Render the #6768 re-validation (v0.105.0 / protocol 120) summary PNG.

Factual: derives from evidence/6768/run-6768-reval-v01050-20261010-0741
pre.json, mid_trigger.json, post_trigger.json, post.json, run.json,
assertions.json, and wire_log.jsonl. All panels are data-driven (zone
counts + wire counts + assertions), never hardcoded.
"""
import json
import os
import sys
from collections import Counter

from PIL import Image, ImageDraw

EVDIR = (sys.argv[1] if len(sys.argv) > 1
         else "/home/hatch/workspace/dev/phase-backfill/evidence/6768/"
              "run-6768-reval-v01050-20261010-0741")
W, H = 1000, 820
BG = (18, 20, 26)
PANEL = (26, 30, 38)
TEXT = (235, 238, 245)
DIM = (150, 160, 175)
ACCENT = (110, 180, 255)
GREEN = (110, 220, 140)
RED = (240, 120, 120)
YELLOW = (240, 200, 110)

TRIO = ("bruna, the fading light", "gisela, the broken blade",
        "brisela, voice of nightmares")


def load(p):
    with open(os.path.join(EVDIR, p)) as f:
        return json.load(f)


def env(p):
    return load(p)["state"]


def zone_names(s, zone):
    return Counter(
        (o.get("base_name") or o.get("name") or "?")
        for o in s["objects"].values()
        if str(o.get("zone") or "").lower() == zone)


def trio_zones(s):
    out = {}
    for o in s["objects"].values():
        nm = str(o.get("base_name") or o.get("name") or "?").lower()
        if nm in TRIO:
            out.setdefault(str(o.get("zone") or "?"), []).append(nm)
    return {z: Counter(v) for z, v in out.items()}


def summarize(s, label):
    lines = [label]
    lines.append(
        f"turn {s.get('turn_number')} | phase {s.get('phase')} | "
        f"active P{s.get('active_player')} | stack {len(s.get('stack') or [])}")
    for p in s.get("players", []):
        lines.append(
            f"P{p.get('id')} life {p.get('life')} | hand "
            f"{len(p.get('hand') or [])} | library {len(p.get('library') or [])}")
    for z, c in sorted(trio_zones(s).items()):
        lines.append(f"{z}: " + ", ".join(f"{k}x{v}" for k, v in sorted(c.items())))
    return lines


def main():
    run = load("run.json")
    ass = run["assertions"]
    wire = [json.loads(l) for l in open(os.path.join(EVDIR, "wire_log.jsonl"))
            if l.strip()]
    stack_meld_hits = sum(
        1 for ev in wire if ev.get("event") == "stack"
        and any("meld" in json.dumps(e, default=str).lower()
                or "brisela" in json.dumps(e, default=str).lower()
                for e in (ev.get("payload") or {}).get("entries", []) or []))
    cast_confirms = sum(1 for ev in wire if ev.get("event") == "cast_confirmed")
    backstops = sum(1 for ev in wire if ev.get("event") == "cast_dropped_retry")
    pre = env("pre.json")
    post_src = None
    post = None
    for cand in ("post_trigger.json", "post.json"):
        try:
            post = env(cand)
            post_src = cand
            break
        except FileNotFoundError:
            continue

    verdict = run["verdict"]
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 16
    d.text((16, y), "phase-rs/phase #6768 | Bruna + Gisela meld: the "
           "end-step trigger must exile both halves and create Brisela",
           fill=ACCENT)
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

    d.rectangle([12, y, W - 12, y + 150], fill=PANEL)
    for i, ln in enumerate(summarize(pre, "PRE (both halves on P0 BF)")):
        d.text((22, y + 6 + i * 17), ln[:114],
               fill=YELLOW if i == 0 else TEXT)
    y += 162

    d.rectangle([12, y, W - 12, y + 150], fill=PANEL)
    if post is not None:
        for i, ln in enumerate(
                summarize(post, f"POST ({post_src}: after trigger resolution)")):
            d.text((22, y + 6 + i * 17), ln[:114],
                   fill=YELLOW if i == 0 else TEXT)
    else:
        d.text((22, y + 6), "POST: no post-resolution state captured", fill=RED)
    y += 162

    d.rectangle([12, y, W - 12, y + 118], fill=PANEL)
    d.text((22, y + 6), "Trigger observation (wire-derived)", fill=YELLOW)
    trig = ass.get("A2_trigger_fires", "?")
    facts = [
        f"stack snapshots with meld/Brisela entries: {stack_meld_hits}",
        f"cast confirmations: {cast_confirms}; cast backstops: {backstops}",
        f"A2 trigger_fires: {trig} -- A3 exile_observed: "
        f"{ass.get('A3_exile_observed', '?')}",
        f"Verdict: {verdict.upper()}.",
    ]
    for i, ln in enumerate(facts):
        d.text((22, y + 28 + i * 17), ln[:112], fill=TEXT)
    y += 130

    d.rectangle([12, y, W - 12, y + 150], fill=PANEL)
    d.text((22, y + 6), "Assertions", fill=YELLOW)
    order = ["A1_setup_ok", "A2_trigger_fires", "A3_exile_observed",
             "A4_meld_correct", "A5_cleanup"]
    labels = {
        "A1_setup_ok": "Gisela + Bruna on P0 BF at pre, life 20/20",
        "A2_trigger_fires": "meld trigger observed (stack / triggers_fired)",
        "A3_exile_observed": "both halves left BF for Exile at resolution",
        "A4_meld_correct": "Brisela, Voice of Nightmares on P0 BF after",
        "A5_cleanup": "stack empty, game advanced past resolution",
    }
    for i, k in enumerate(order):
        v = ass.get(k, "not-run")
        color = GREEN if v == "passed" else (RED if v == "failed" else YELLOW)
        d.text((22, y + 28 + i * 18), f"{k}: {v} -- {labels[k]}"[:112],
               fill=color)
    d.text((22, y + 128), "sha manifest: manifest.sha256", fill=DIM)

    img.save(os.path.join(EVDIR, "summary.png"))
    print("wrote summary.png")


main()
