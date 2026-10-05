#!/usr/bin/env python3
"""Render summary.png for #6729 from saved states + run.json."""
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


def summarize_state(env):
    s = env["state"]
    objs = s.get("objects", {})
    lines = []
    for p in s.get("players", []):
        pid = p.get("id")
        hand = [objs.get(str(o), {}).get("base_name") or "?"
                for o in p.get("hand", [])]
        bf = [o for o in objs.values()
              if o.get("zone") == "Battlefield" and o.get("controller") == pid]
        bf_counts = Counter(o.get("base_name") or "?" for o in bf)
        lib = len(p.get("library", []) or [])
        gy = [objs.get(str(o), {}).get("base_name") or "?"
              for o in p.get("graveyard", [])]
        gy_counts = Counter(gy)
        lines.append(f"P{pid} life {p.get('life')} | hand({len(hand)}): "
                     + (", ".join(hand[:10]) + ("..." if len(hand) > 10 else "")))
        lines.append("    battlefield: "
                     + (", ".join(f"{k}x{v}" for k, v in sorted(bf_counts.items()))
                        or "empty"))
        lines.append(f"    library({lib}) | graveyard: "
                     + (", ".join(f"{k}x{v}" for k, v in sorted(gy_counts.items()))
                        or "empty"))
    stack = s.get("stack", []) or []
    hdr = (f"turn {s.get('turn_number')} | phase {s.get('phase')} | "
           f"active P{s.get('active_player')} | priority P{s.get('priority_player')} | "
           f"stack {len(stack)} | waiting_for {(s.get('waiting_for') or {}).get('type')}")
    return hdr, lines


def main():
    evdir = sys.argv[1]
    run = load(os.path.join(evdir, "run.json"))
    pre = load(os.path.join(evdir, "pre.json"))
    post = load(os.path.join(evdir, "post.json"))
    srv = run["server"]
    game = run["games"][0]
    obs = game["observations"]

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #6729 - [Card Bug] Green Sun's Zenith "
            "does not trigger ETB", fill=ACCENT)
    y += 26
    d.text((24, y), f"server v{srv['server_version']} ({srv['build_commit']}, protocol "
            f"{srv['protocol_version']}) | run {run['run_id']} | "
            f"{run['started_at'][:10]} | verdict: {run['verdict']}", fill=DIM)
    y += 30
    d.text((24, y), "Setup: P0 12x Zenith / 12x Arboreal Grazer / 36x Forest; "
            "P1 60x Island.", fill=TEXT)
    y += 24
    d.text((24, y), "Contract: Zenith X=1 -> Grazer to BF -> ETB fires -> land "
            "enters tapped; Zenith -> library; exactly 2 shuffles.", fill=DIM)
    y += 34

    for tag, env in (("PRE (Zenith X=1 on stack, search pending)", pre), ("POST (resolution complete)", post)):
        d.rectangle([16, y, W - 16, y + 240], fill=PANEL, outline=(45, 52, 64))
        d.text((28, y + 8), tag, fill=YELLOW)
        hdr, lines = summarize_state(env)
        d.text((28, y + 32), hdr, fill=TEXT)
        yy = y + 58
        for pl in lines[:8]:
            d.text((28, yy), pl[:150], fill=TEXT)
            yy += 22
        y += 252

    d.rectangle([16, y, W - 16, y + 176], fill=PANEL, outline=(45, 52, 64))
    d.text((28, y + 8), "Assertions", fill=YELLOW)
    yy = y + 34
    for k, v in game["assertions"].items():
        color = GREEN if v == "passed" else (RED if v == "failed" else DIM)
        d.text((28, yy), f"{k}: {v}", fill=color)
        yy += 22
    y += 188

    key = (f"X={obs['x_chosen']} search_submitted={obs['search_submitted']} "
           f"grazer_oid={obs['grazer_oid']} trigger_seen={obs['trigger_seen']} "
           f"optional_accepted={obs['optional_accepted']} "
           f"land_put_oid={obs['land_put_oid']}")
    d.text((24, y), "Key observations: " + key, fill=TEXT)
    y += 24
    d.text((24, y), f"LibraryShuffle commands pre->post: {obs['shuffles_pre']} -> "
            f"{obs['shuffles_post']} (expected +2); "
            f"rejections={len(obs['rejections'])}", fill=TEXT)
    y += 28
    notes = game.get("notes", [])
    for n in notes:
        d.text((24, y), "Note: " + n[:150], fill=DIM)
        y += 24
    y += 4
    d.text((24, y), "Parse (v0.78.0 card data): Zenith = SearchLibrary(green "
            "creature MV<=X) -> ChangeZone(found->BF) -> Shuffle -> "
            "Self->owner library -> Shuffle.", fill=DIM)

    out = os.path.join(evdir, "summary.png")
    img.save(out)
    print("wrote", out)


main()
