#!/usr/bin/env python3
"""Render summary.png for phase-rs/phase #6759 (run 20261002-6759, v0.99.0)
from saved evidence states.

Derived ONLY from evidence/6759/20261002-6759/{run,g1_noparty_*.json,
g2_fullparty_*.json}.json. Run: python3 render_summary_6759_v099.py
"""
import json
import os

from PIL import Image, ImageDraw

EVDIR = "/home/hatch/workspace/dev/phase-backfill/evidence/6759/20261002-6759"
W, H = 1560, 1000
BG = (18, 20, 26)
PANEL = (26, 30, 38)
TEXT = (220, 224, 232)
DIM = (140, 148, 160)
GREEN = (110, 220, 130)
RED = (240, 120, 120)
YELLOW = (240, 210, 130)
ACCENT = (130, 180, 250)


def load(p):
    with open(p) as f:
        return json.load(f)


def env(fn):
    return load(os.path.join(EVDIR, fn))["state"]


def bf_rows(s, pid=0):
    rows = []
    objs = s.get("objects") or {}
    players = s.get("players") or []
    pmap = {p.get("id"): p for p in players} if isinstance(players, list) else {}
    for o in objs.values():
        if o.get("zone") == "Battlefield" and o.get("controller") == pid:
            nm = o.get("base_name") or o.get("name")
            if nm in ("Plains", "Swamp", "Island"):
                continue
            cnts = o.get("counters") or {}
            dt = "Deathtouch" in json.dumps(o.get("keywords") or [])
            rows.append((nm, int(cnts.get("P1P1", 0)), dt))
    return rows


def rows_line(rows):
    if not rows:
        return "battlefield creatures: none"
    parts = []
    for n, c, dt in rows:
        p = f"{n}: +{c}/+{c}" if c else f"{n}: no counters"
        if dt:
            p += " +DEATHTOUCH"
        parts.append(p)
    return " | ".join(parts)


def main():
    run = load(os.path.join(EVDIR, "run.json"))
    g1pre = env("g1_noparty_pre.json")
    g1post = env("g1_noparty_post.json")
    g2pre = env("g2_fullparty_pre.json")
    g2post = env("g2_fullparty_post.json")
    srv = run["server"]

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 16
    d.text((24, y), "phase-rs/phase #6759 - Nalia de'Arnise counter "
                    "requires a full party", fill=ACCENT)
    y += 26
    d.text((24, y),
           f"server {srv['server_version']} (build {srv['build_commit']}, protocol "
           f"{srv['protocol_version']}) | run {run['run_id']} | "
           f"{run['validated_at']} | verdict: {run['verdict']}", fill=DIM)
    y += 24
    d.text((24, y), "G1 (bug branch): Nalia alone, party size 1 -> expect NO "
                    "counters, NO deathtouch", fill=TEXT)
    y += 22
    d.text((24, y), "G2 (control): Nalia + Cleric + Warrior + Wizard, party "
                    "size 4 -> expect +1/+1 and deathtouch on all four",
           fill=TEXT)
    y += 30

    panels = [
        ("G1 PRE (PreCombatMain, party=1)", g1pre),
        ("G1 POST (DeclareAttackers, trigger window passed)", g1post),
        ("G2 PRE (PreCombatMain, party=4)", g2pre),
        ("G2 POST (DeclareAttackers, trigger resolved)", g2post),
    ]
    for label, s in panels:
        d.rectangle([16, y, W - 16, y + 76], fill=PANEL, outline=(45, 52, 64))
        d.text((28, y + 6), label, fill=YELLOW)
        d.text((28, y + 30), rows_line(bf_rows(s))[:150], fill=TEXT)
        players = s.get("players") if isinstance(s.get("players"), list) else []
        life = "/".join(str(p.get("life")) for p in players[:2])
        d.text((28, y + 52),
               f"stack depth: {len(s.get('stack') or [])} | life P0/P1: {life}",
               fill=DIM)
        y += 86

    d.rectangle([16, y, W - 16, y + 200], fill=PANEL, outline=(45, 52, 64))
    d.text((28, y + 8), "Assertions (from run.json)", fill=YELLOW)
    yy = y + 34
    for k, v in run["assertions"].items():
        color = GREEN if v == "passed" else (RED if v == "failed" else DIM)
        d.text((28, yy), f"{k}: {v}", fill=color)
        yy += 23
    y += 212

    facts = [
        "Observed on v0.99.0 (build d919616, protocol 98):",
        "  G1: at BeginCombat with party size 1, the trigger never reached the",
        "  stack (stack empty); at DeclareAttackers Nalia had 0 counters and no",
        "  deathtouch. The 'counter regardless of full party' report did NOT occur.",
        "  G2: at BeginCombat with party size 4, the trigger went on the stack",
        "  (effect PutCounterAll); at DeclareAttackers all four creatures showed",
        "  +1/+1 and deathtouch. The full-party condition is enforced correctly.",
        "  Binary/data sha256 in run.json; scenario_6759_v099.py with matching",
        "  hash in evidence dir.",
    ]
    for f in facts:
        d.text((24, y), f, fill=TEXT if not f.startswith("  ") else DIM)
        y += 22

    out = os.path.join(EVDIR, "summary.png")
    img.save(out)
    print("wrote", out, os.path.getsize(out), "bytes")


main()
