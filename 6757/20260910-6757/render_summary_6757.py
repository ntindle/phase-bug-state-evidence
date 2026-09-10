#!/usr/bin/env python3
"""Render the #6757 summary PNG from the saved states and run.json.
Factual: derived from the authoritative exports in evidence/6757/20260910-6757/."""
import json
import os

from PIL import Image, ImageDraw

EVDIR = "/home/hatch/workspace/dev/phase-backfill/evidence/6757/20260910-6757"
W, H = 1100, 980
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


def bf_creatures(s, pid, nm):
    return [o for o in s["objects"].values()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid
            and name(o) == nm]


def sup(o):
    return ((o.get("card_types") or {}).get("supertypes")) or []


def zone_line(s, label):
    bof = [o for o in s["objects"].values() if name(o) == "Bofur, Reliable Guardian"]
    zones = {}
    for o in bof:
        zones[o.get("zone")] = zones.get(o.get("zone"), 0) + 1
    jk = next((o for o in s["objects"].values()
               if name(o) == "Jackal, Genius Geneticist"
               and o.get("zone") == "Battlefield"), None)
    return (
        f"{label}: turn {s.get('turn_number')} {s.get('phase')} | "
        f"waiting={((s.get('waiting_for') or {}).get('type'))} | "
        f"stack {len(s.get('stack') or [])} | life {s['players'][0]['life']}/{s['players'][1]['life']} | "
        f"Bofur zones {zones} | Jackal power {jk.get('power') if jk else '?'} "
        f"counters {json.dumps((jk or {}).get('counters'))}"
    )


def main():
    run = load(os.path.join(EVDIR, "run.json"))
    files = {}
    for f in ("pre_trigger", "pre_resolution", "post_resolution"):
        p = os.path.join(EVDIR, f + ".json")
        if os.path.exists(p):
            files[f] = env(f + ".json")
    srv = run["server"]

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 16
    d.text((24, y), "phase-rs/phase #6757 - Jackal, Genius Geneticist copy keeps legendary?", fill=ACCENT)
    y += 26
    d.text((24, y),
           f"server v{srv['server_version']} ({srv['build_commit']}, protocol {srv['protocol_version']}) "
           f"| run {run['run_id']} | {run['started_at'][:10]} | verdict: {run['verdict']}",
           fill=DIM)
    y += 24
    d.text((24, y), "Setup: " + run.get("setup_line", "")[:150], fill=TEXT)
    y += 22
    d.text((24, y), "Contract: " + run.get("contract_line", "")[:150], fill=DIM)
    y += 30

    labels = {"pre_trigger": "PRE_TRIGGER (Bofur cast, Jackal trigger pending)",
              "pre_resolution": "PRE_RESOLUTION (copy spell on stack)",
              "post_resolution": "POST_RESOLUTION (both permanents on battlefield)"}
    for tag in ("pre_trigger", "pre_resolution", "post_resolution"):
        s = files.get(tag)
        d.rectangle([16, y, W - 16, y + 52], fill=PANEL, outline=(45, 52, 64))
        d.text((28, y + 6), labels[tag], fill=YELLOW)
        d.text((28, y + 30), (zone_line(s, "") if s else "not captured")[:168], fill=TEXT)
        y += 62

    # copy spell detail from pre_resolution
    s = files.get("pre_resolution")
    d.rectangle([16, y, W - 16, y + 96], fill=PANEL, outline=(45, 52, 64))
    d.text((28, y + 8), "Copy spell on the stack (from pre_resolution.json)", fill=YELLOW)
    yy = y + 32
    if s:
        copies = [o for o in s["objects"].values()
                  if name(o) == "Bofur, Reliable Guardian" and o.get("zone") == "Stack"]
        for o in copies[:3]:
            d.text((28, yy),
                   f"id={o['id']} is_token={o.get('is_token')} supertypes={sup(o)}",
                   fill=TEXT)
            yy += 22
    else:
        d.text((28, yy), "pre_resolution.json not captured", fill=RED)
    y += 108

    d.rectangle([16, y, W - 16, y + 240], fill=PANEL, outline=(45, 52, 64))
    d.text((28, y + 8), "Assertions (from run.json)", fill=YELLOW)
    yy = y + 34
    for k, v in run["assertions"].items():
        color = GREEN if v == "passed" else (RED if v == "failed" else DIM)
        d.text((28, yy), f"{k}: {v}", fill=color)
        yy += 23
    y += 252

    facts = [
        "Card data (pinned v0.79.0) already encodes CopySpell.additional_modifications=",
        "  [RemoveSupertype Legendary]; this run tests whether the engine applies it.",
        "Expected: copy spell is a non-legendary token; both Bofur permanents survive;",
        "  Jackal gains exactly one +1/+1 counter. Binary/data sha256 in run.json.",
    ]
    for f in facts:
        d.text((24, y), f, fill=TEXT if not f.startswith("  ") else DIM)
        y += 22

    out = os.path.join(EVDIR, "summary.png")
    img.save(out)
    print("wrote", out, os.path.getsize(out), "bytes")


main()
