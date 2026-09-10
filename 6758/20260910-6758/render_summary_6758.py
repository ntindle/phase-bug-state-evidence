#!/usr/bin/env python3
"""Render the #6758 summary PNG from the saved states and run.json.
Factual: derived from the authoritative exports in evidence/6758/20260910-6758/."""
import json
import os

from PIL import Image, ImageDraw

EVDIR = "/home/hatch/workspace/dev/phase-backfill/evidence/6758/20260910-6758"
W, H = 1100, 1020
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


def env(p):
    return load(os.path.join(EVDIR, p))["state"]


def name(o):
    return o.get("base_name") or o.get("name")


def life_line(s):
    pls = s["players"]
    return (f"turn {s.get('turn_number')} {s.get('phase')} | "
            f"waiting={((s.get('waiting_for') or {}).get('type'))} | "
            f"stack {len(s.get('stack') or [])} | "
            f"life P0={pls[0]['life']} P1={pls[1]['life']}")


def storm_line(s):
    storms = [o for o in s["objects"].values() if name(o) == "Comet Storm"]
    zones = {}
    for o in storms:
        zones[o.get("zone")] = zones.get(o.get("zone"), 0) + 1
    return f"Comet Storm zones {zones}"


def stack_targets(s):
    out = []
    for e in (s.get("stack") or []):
        d = (e.get("kind") or {}).get("data") or {}
        ab = d.get("ability") or {}
        chain = []
        a = ab
        while a:
            eff = (a.get("effect") or {}).get("type")
            tg = a.get("targets")
            chain.append(f"{eff}:{json.dumps(tg)}")
            a = a.get("sub_ability")
        out.append(f"id={e.get('id')} x={(ab or {}).get('chosen_x')} " +
                   " -> ".join(chain))
    return out


def main():
    run = load(os.path.join(EVDIR, "run.json"))
    files = {}
    for f in ("g1_kicked_pre", "g1_kicked_on_stack", "g1_kicked_post",
              "g2_control_pre", "g2_control_on_stack", "g2_control_post"):
        p = os.path.join(EVDIR, f + ".json")
        if os.path.exists(p):
            files[f] = env(f + ".json")
    srv = run["server"]

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 16
    d.text((24, y), "phase-rs/phase #6758 - Comet Storm kicked: no 2nd target, no damage",
           fill=ACCENT)
    y += 26
    d.text((24, y),
           f"server {srv['version']} (build {srv['build_commit']}, protocol "
           f"{srv['protocol_version']}) | run {run['run_id']} | "
           f"{run['validated_at']} | verdict: {run['verdict']}", fill=DIM)
    y += 24
    d.text((24, y), "G1: Comet Storm X=2, kicker {1} paid once, targets P1 then P0 "
                    "(expect 2 prompts, 2 dmg each)", fill=TEXT)
    y += 22
    d.text((24, y), "G2 control: X=1, no kicker, single target P1", fill=DIM)
    y += 30

    panels = [
        ("g1_kicked_pre", "G1 PRE-CAST (before Comet Storm cast)"),
        ("g1_kicked_on_stack", "G1 ON-STACK (all cast decisions done, awaiting resolution)"),
        ("g1_kicked_post", "G1 POST-RESOLUTION"),
    ]
    for tag, label in panels:
        s = files.get(tag)
        d.rectangle([16, y, W - 16, y + 76], fill=PANEL, outline=(45, 52, 64))
        d.text((28, y + 6), label, fill=YELLOW)
        if s:
            d.text((28, y + 30), life_line(s)[:150], fill=TEXT)
            d.text((28, y + 52), storm_line(s)[:150], fill=TEXT)
        else:
            d.text((28, y + 30), "not captured", fill=RED)
        y += 86

    s = files.get("g1_kicked_on_stack")
    d.rectangle([16, y, W - 16, y + 118], fill=PANEL, outline=(45, 52, 64))
    d.text((28, y + 8), "G1 stack entry: chosen X and recorded targets (from on-stack export)",
           fill=YELLOW)
    yy = y + 32
    if s:
        rows = stack_targets(s)
        if rows:
            for r in rows[:2]:
                d.text((28, yy), r[:168], fill=TEXT)
                yy += 22
        else:
            d.text((28, yy), "stack empty at export", fill=DIM)
            yy += 22
        d.text((28, yy), "Expectation: 2 target prompts (1 + 1 per kick); "
                         "observed prompts: "
               + str(run.get("g1_target_prompts", "?")), fill=RED if run.get("g1_target_prompts", 2) < 2 else TEXT)
    else:
        d.text((28, yy), "g1_kicked_on_stack.json not captured", fill=RED)
    y += 130

    d.rectangle([16, y, W - 16, y + 170], fill=PANEL, outline=(45, 52, 64))
    d.text((28, y + 8), "Assertions (from run.json)", fill=YELLOW)
    yy = y + 34
    for k, v in run["assertions"].items():
        color = GREEN if v == "passed" else (RED if v == "failed" else DIM)
        d.text((28, yy), f"{k}: {v}", fill=color)
        yy += 23
    y += 182

    facts = [
        "Observed on v0.79.0 (build 1cde7a2, protocol 69): after the single target",
        "  choice, waiting_for went TargetSelection -> OptionalCostChoice (kicker pay",
        "  answered, times_kicked 0->1) -> ChooseXValue -> Priority. No second",
        "  TargetSelection was ever offered; resolution dealt 0 damage (life 20/20).",
        "  The X=1 no-kicker control also dealt 0 damage: the no-damage defect is",
        "  independent of the kicker path. Engine auto-tapped only {2}{R}{R}; the",
        "  {1} kicker mana was never collected. Binary/data sha256 in run.json.",
    ]
    for f in facts:
        d.text((24, y), f, fill=TEXT if not f.startswith("  ") else DIM)
        y += 22

    out = os.path.join(EVDIR, "summary.png")
    img.save(out)
    print("wrote", out, os.path.getsize(out), "bytes")


main()
