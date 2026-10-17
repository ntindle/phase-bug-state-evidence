#!/usr/bin/env python3
"""Render the evidence summary PNG for issue #6557 from saved states.

Usage: render_summary_6557.py <evidence_dir>
Reads run.json, pre.json, post.json, post2.json, trigger_samples.json;
writes summary.png. Every number/name on the image is read from the saved
states/assertions.
"""
import json
import os
import sys
from collections import Counter

from PIL import Image, ImageDraw

W, H = 1000, 980
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


def oname(objs, oid):
    o = objs.get(str(oid), {})
    return o.get("base_name") or o.get("name") or "?"


def pval(o, key):
    v = o.get(key)
    if isinstance(v, dict):
        v = v.get("value")
    return v


def summarize_state(env):
    s = env["state"]
    objs = s.get("objects", {})
    players = []
    for p in s.get("players", []):
        pid = p.get("id")
        hand = [oname(objs, o) for o in p.get("hand", [])]
        bf = [o for o in objs.values()
              if o.get("zone") == "Battlefield" and o.get("controller") == pid]
        bf_counts = Counter((o.get("base_name") or o.get("name") or "?") for o in bf)
        players.append(
            f"P{pid} life {p.get('life')} | hand({len(hand)}): "
            + (", ".join(hand[:8]) + ("..." if len(hand) > 8 else "")))
        players.append(
            "    battlefield: " + (", ".join(f"{k}x{v}" for k, v in sorted(bf_counts.items())) or "empty"))
    stack = len(s.get("stack", []) or [])
    return (f"turn {s.get('turn_number')} | phase {s.get('phase')} | "
            f"active P{s.get('active_player')} | priority P{s.get('priority_player')} | "
            f"stack {stack}"), players


def tamiyo_line(env):
    s = env["state"]
    objs = s.get("objects", {})
    for oid, o in objs.items():
        if o.get("zone") == "Battlefield" and o.get("controller") == 0:
            nm = str(o.get("base_name") or o.get("name") or "")
            if "tamiyo" in nm.lower():
                return (f"oid {oid}: {nm} loyalty={o.get('loyalty')} "
                        f"(front={o.get('transformed') is False or 'n/a'})")
    return "no Tamiyo on P0 battlefield"


def bear_line(env, oid):
    s = env["state"]
    objs = s.get("objects", {})
    o = objs.get(str(oid), {})
    if not o:
        return f"bear oid {oid}: not found"
    return (f"bear oid {oid}: {oname(objs, oid)} "
            f"{pval(o, 'power')}/{pval(o, 'toughness')} "
            f"attacking={o.get('attacking')}")


def trigger_line(evdir):
    try:
        samples = load(os.path.join(evdir, "trigger_samples.json"))
    except Exception:
        return "trigger_samples.json missing"
    for rec in samples:
        e = rec.get("entry", {}) or {}
        data = (e.get("kind") or {}).get("data", {}) or {}
        ab = data.get("ability", {}) or {}
        eff = ab.get("effect", {}) or {}
        if eff.get("type") == "Pump":
            tgt = (eff.get("target") or {}).get("type")
            return (f"turn {rec.get('turn')} {rec.get('phase')}: TriggeredAbility "
                    f"Pump(power={eff.get('power')}, toughness={eff.get('toughness')}, "
                    f"target={tgt}) id={e.get('id')} src={e.get('source_id')} "
                    f"[nested Pump target SelfRef = bug fingerprint]")
    return "no Pump trigger sample found"


def main():
    evdir = sys.argv[1]
    run = load(os.path.join(evdir, "run.json"))
    pre = load(os.path.join(evdir, "pre.json"))
    post = load(os.path.join(evdir, "post.json"))
    post2 = load(os.path.join(evdir, "post2.json"))
    srv = run["server"]
    obs = run.get("observations", {})
    a1 = obs.get("attack1_bear")

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #6557 \u2014 Tamiyo, Seasoned Scholar: -1/-0 not applied",
           fill=ACCENT)
    y += 26
    d.text((24, y),
           f"server v{srv['server_version']} ({srv['build_commit']}, protocol {srv['protocol_version']})"
           f" | run {run['run_id']} | {run['started_at'][:10]} | verdict: {run['verdict']}",
           fill=DIM)
    y += 30
    d.text((24, y), "Setup: " + run.get("setup_line", ""), fill=TEXT)
    y += 24
    d.text((24, y), "Contract: " + run.get("contract_line", ""), fill=DIM)
    y += 32

    panels = (
        ("PRE \u2014 +2 activation gate (main phase)", pre, None),
        ("POST \u2014 first attack resolved (EndCombat)", post, a1),
        ("POST2 \u2014 second attack after watching effect expired", post2, obs.get("attack2_bear")),
    )
    for tag, env, boid in panels:
        d.rectangle([16, y, W - 16, y + 218], fill=PANEL, outline=(45, 52, 64))
        d.text((28, y + 8), tag, fill=YELLOW)
        hdr, players = summarize_state(env)
        d.text((28, y + 32), hdr, fill=TEXT)
        yy = y + 58
        for pl in players:
            d.text((28, yy), pl[:150], fill=TEXT)
            yy += 22
        d.text((28, yy), "Tamiyo: " + tamiyo_line(env), fill=ACCENT)
        yy += 22
        if boid is not None:
            d.text((28, yy), bear_line(env, boid), fill=TEXT)
            yy += 22
        y += 230

    d.text((24, y), "Delayed trigger (captured on stack):", fill=YELLOW)
    y += 24
    d.text((24, y), trigger_line(evdir)[:150], fill=TEXT)
    y += 32

    d.rectangle([16, y, W - 16, y + 190], fill=PANEL, outline=(45, 52, 64))
    d.text((28, y + 8), "Assertions", fill=YELLOW)
    yy = y + 34
    for k, v in run["assertions"].items():
        color = GREEN if v == "passed" else (RED if v == "failed" else DIM)
        d.text((28, yy), f"{k}: {v}", fill=color)
        yy += 22
    y += 202

    d.text((24, y), "Limitations: " + "; ".join(run.get("limitations", []))[:140], fill=DIM)

    out = os.path.join(evdir, "summary.png")
    img.save(out)
    print("wrote", out)


main()
