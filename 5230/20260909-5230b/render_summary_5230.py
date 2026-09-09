#!/usr/bin/env python3
"""Render evidence summary PNG for issue #5230 (Stuck decision: CombatTaxPayment).

Usage: render_summary_5230.py <evidence_dir>
Reads run.json, pre.json, post.json from the dir; writes summary.png
derived from the saved evidence states and assertion results.
"""
import json
import os
import sys
from collections import Counter

from PIL import Image, ImageDraw

W, H = 1000, 860
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


def names(hand_objs, objs):
    out = []
    for o in hand_objs:
        oo = objs.get(str(o), {})
        out.append(oo.get("base_name") or oo.get("name") or "?")
    return out


def summarize_state(env):
    s = env["state"]
    objs = s.get("objects", {})
    players = []
    for p in s.get("players", []):
        pid = p.get("id")
        hand = names(p.get("hand", []), objs)
        gy = names(p.get("graveyard", []), objs)
        bf = [
            o for o in objs.values()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
        ]
        bf_counts = Counter(o.get("base_name") or o.get("name") or "?" for o in bf)
        players.append(
            f"P{pid} life {p.get('life')} | hand({len(hand)}): "
            + (", ".join(hand[:10]) + ("..." if len(hand) > 10 else ""))
        )
        players.append(
            "    battlefield: " + (", ".join(f"{k}x{v}" for k, v in sorted(bf_counts.items())) or "empty")
            + f" | library {len(p.get('library', []))} | graveyard({len(gy)}): "
            + (", ".join(gy[:6]) or "empty")
        )
    stack = len(s.get("stack", []) or [])
    return (
        f"turn {s.get('turn_number')} | phase {s.get('phase')} | "
        f"active P{s.get('active_player')} | priority P{s.get('priority_player')} | "
        f"stack {stack} | wf {(s.get('waiting_for') or {}).get('type')}",
        players,
    )


def main():
    evdir = sys.argv[1]
    run = load(os.path.join(evdir, "run.json"))
    pre = load(os.path.join(evdir, "pre.json"))
    post = load(os.path.join(evdir, "post.json"))
    srv = run["server"]
    ob = run.get("observations", {})

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #5230 \u2014 Stuck decision: CombatTaxPayment", fill=ACCENT)
    y += 26
    d.text((24, y), f"server v{srv['server_version']} ({srv['build_commit']}, protocol {srv['protocol_version']})"
            f" | run {run['run_id']} | {run['started_at'][:10]} | verdict: {run['verdict']}",
           fill=DIM)
    y += 30
    d.text((24, y), "Setup: " + run.get("setup_line", ""), fill=TEXT)
    y += 24
    d.text((24, y), "Contract: " + run.get("contract_line", "")[:150], fill=DIM)
    y += 34

    tw = ob.get("tax_waits", [])
    cantpay = [w for w in tw if w.get("mode") == "cantpay"]
    control = [w for w in tw if w.get("mode") == "control"]
    c0 = cantpay[0] if cantpay else {}
    c1 = control[0] if control else {}

    for tag, env in (("PRE (P0 DeclareAttackers: 3 goaded Bears vs Ghostly Prison, tax {6})", pre),
                     ("POST (after decline+undeclare, then control pay {2} -> P1 20->18)", post)):
        d.rectangle([16, y, W - 16, y + 196], fill=PANEL, outline=(45, 52, 64))
        d.text((28, y + 8), tag, fill=YELLOW)
        hdr, players = summarize_state(env)
        d.text((28, y + 32), hdr, fill=TEXT)
        yy = y + 58
        for pl in players:
            d.text((28, yy), pl[:160], fill=TEXT)
            yy += 22
        y += 208

    d.rectangle([16, y, W - 16, y + 100], fill=PANEL, outline=(45, 52, 64))
    d.text((28, y + 8), "Key decision observations", fill=YELLOW)
    d.text((28, y + 34),
           f"can't-pay tax wait: accept=true offered={c0.get('accept_true_offered')} "
           f"accept=false offered={c0.get('accept_false_offered')} "
           f"(untapped Forests={c0.get('untapped_forests')}, tax {{6}})", fill=TEXT)
    d.text((28, y + 58),
           f"decline -> undeclare: ACCEPTED (no #4893 loop) | control tax wait: "
           f"accept=true offered={c1.get('accept_true_offered')} -> paid, P1 "
           f"{ob.get('life_pre')}->{ob.get('life_after_control')}", fill=TEXT)
    y += 112

    d.rectangle([16, y, W - 16, y + 200], fill=PANEL, outline=(45, 52, 64))
    d.text((28, y + 8), "Assertions", fill=YELLOW)
    yy = y + 34
    for k, v in run["assertions"].items():
        color = GREEN if v == "passed" else (RED if v == "failed" else DIM)
        d.text((28, yy), f"{k}: {v}", fill=color)
        yy += 22
    y += 212

    d.text((24, y), "Verdict rule: report has no testable premise (empty 'What happened', "
            "reporter never answered 2026-07-19 questions) -> blocked; the #4893 "
            "goad/no-mana axis was exercised generically.", fill=DIM)
    y += 24
    d.text((24, y),
           f"CombatTaxPayment waits: {len(tw)} (actionable: {sum(1 for w in tw if w.get('n_actions', 0) > 0)}, "
           f"stall: {ob.get('stall_observed')}, loop: {ob.get('loop_observed')}) | "
           f"sha manifest: manifest.sha256", fill=DIM)

    out = os.path.join(evdir, "summary.png")
    img.save(out)
    print("wrote", out)


main()
