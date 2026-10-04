#!/usr/bin/env python3
"""Render evidence summary PNG for PR #8813 (modal DFC faces, client defect).

Usage: render_summary_8813.py <evidence_dir>
Reads run.json + saved evidence states/assertions; writes summary.png.
The PNG is derived from saved states and assertion results only.
"""
import json
import os
import sys
from collections import Counter

from PIL import Image, ImageDraw

W, H = 1000, 1020
BG = (18, 20, 26)
PANEL = (26, 30, 38)
TEXT = (235, 238, 245)
DIM = (150, 160, 175)
ACCENT = (110, 180, 255)
GREEN = (110, 220, 140)
RED = (240, 120, 120)
YELLOW = (240, 200, 110)


def load(p):
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def names(id_list, objs):
    return [(objs.get(str(o), {}).get("base_name")
             or objs.get(str(o), {}).get("name") or "?") for o in id_list]


def summarize_state(env):
    s = env["state"]
    objs = s.get("objects", {})
    players = []
    for p in s.get("players", []):
        pid = p.get("id")
        hand = names(p.get("hand", []), objs)
        bf = [o for o in objs.values()
              if o.get("zone") == "Battlefield" and o.get("controller") == pid]
        bf_counts = Counter(o.get("base_name") or o.get("name") or "?" for o in bf)
        players.append(
            f"P{pid} life {p.get('life')} | hand({len(hand)}): "
            + (", ".join(hand[:5]) + ("..." if len(hand) > 5 else ""))
        )
        players.append(
            "    battlefield: " + (", ".join(f"{k}x{v}" for k, v in sorted(bf_counts.items())) or "empty")
        )
    stack = len(s.get("stack", []) or [])
    head = (f"turn {s.get('turn')} | phase {s.get('phase')} | "
            f"active P{s.get('active_player')} | stack {stack} | "
            f"wf {(s.get('waiting_for') or {}).get('type')}")
    return head, players


def mdfc_line(env):
    s = env["state"]
    hits = []
    for oid, o in s.get("objects", {}).items():
        nm = (o.get("base_name") or o.get("name") or "").lower()
        if nm in ("tony stark", "iron man, tony stark"):
            hits.append(f"oid {oid} {nm} zone={o.get('zone')} ctrl={o.get('controller')}")
    return "MDFC: " + ("; ".join(hits) if hits else "none on record")


def draw_text_block(d, x, y, lines, fill=TEXT, lh=20):
    for ln in lines:
        d.text((x, y), ln, fill=fill)
        y += lh
    return y


def main():
    evdir = sys.argv[1]
    run = load(os.path.join(evdir, "run.json")) or {}
    pre1 = load(os.path.join(evdir, "pre_cast1.json"))
    post1 = load(os.path.join(evdir, "post_front.json"))
    pre2 = load(os.path.join(evdir, "pre_cast2.json"))
    post2 = load(os.path.join(evdir, "post_cast2.json"))
    modal1 = load(os.path.join(evdir, "modalfacechoice_cast1.json")) or {}
    modal2 = load(os.path.join(evdir, "modalfacechoice_cast2.json")) or {}
    probe = load(os.path.join(evdir, "back_probe.json")) or {}

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase PR #8813 - modal DFC face choice (client defect site)", fill=ACCENT)
    y += 26
    srv = (run.get("server") or {})
    d.text((24, y), f"engine {srv.get('server_version')} ({srv.get('build_commit')}, protocol {srv.get('protocol_version')}) | "
                    f"client mainline {(run.get('client_mainline_commit') or '')[:12]} | "
                    f"verdict: {run.get('verdict')}", fill=DIM)
    y += 24
    d.text((24, y), "PR #8813 (closed, unmerged): paint only engine-legalized modal DFC faces; "
                    "unaffordable back face must not be clickable.", fill=DIM)
    y += 34

    d.text((24, y), "ASSERTIONS", fill=ACCENT)
    y += 22
    for k, v in (run.get("assertions") or {}).items():
        color = GREEN if v == "passed" else (YELLOW if v.startswith("not-run") else RED)
        d.text((40, y), f"{k}: {v}", fill=color)
        y += 20
    y += 10

    d.text((24, y), "GAME A - back face unaffordable (only {1}{U} available)", fill=ACCENT)
    y += 22
    if pre1:
        head, pls = summarize_state(pre1)
        y = draw_text_block(d, 40, y, ["pre_cast1: " + head] + ["  " + p for p in pls], DIM)
    la1 = [a.get("data") for a in (modal1.get("legal_actions") or []) if a.get("type") == "ChooseModalFace"]
    d.text((40, y), f"ModalFaceChoice legal ChooseModalFace: {la1}", fill=TEXT)
    y += 20
    rej = (probe.get("rejection") or {})
    d.text((40, y), f"client-constructed back_face=true dispatch -> "
                    f"{rej.get('type')}: {str((rej.get('data') or {}).get('rejection', {}).get('code'))}", fill=TEXT)
    y += 20
    if post1:
        head, pls = summarize_state(post1)
        y = draw_text_block(d, 40, y, ["post_front: " + head, "  " + mdfc_line(post1)] + ["  " + p for p in pls], DIM)
    y += 12

    d.text((24, y), "GAME B - back face affordable ({4}{U}{R} available, observational)", fill=ACCENT)
    y += 22
    if pre2:
        head, pls = summarize_state(pre2)
        y = draw_text_block(d, 40, y, ["pre_cast2: " + head] + ["  " + p for p in pls], DIM)
    else:
        d.text((40, y), "pre_cast2: not captured (cast2 never reached)", fill=DIM)
        y += 20
    la2 = [a.get("data") for a in (modal2.get("legal_actions") or []) if a.get("type") == "ChooseModalFace"]
    d.text((40, y), f"ModalFaceChoice legal ChooseModalFace: {la2}", fill=TEXT)
    y += 20
    if post2:
        head, pls = summarize_state(post2)
        y = draw_text_block(d, 40, y, ["post_cast2: " + head, "  " + mdfc_line(post2)] + ["  " + p for p in pls], DIM)
    else:
        d.text((40, y), "post_cast2: not captured", fill=DIM)
        y += 20
    y += 10

    lims = run.get("limitations") or []
    d.text((24, y), "LIMITATIONS", fill=ACCENT)
    y += 22
    for lm in lims:
        words, line, lines = lm.split(), "", []
        for w_ in words:
            if len(line) + len(w_) + 1 > 108:
                lines.append(line); line = w_
            else:
                line = (line + " " + w_).strip()
        lines.append(line)
        for ln in lines:
            d.text((40, y), "- " + ln, fill=DIM)
            y += 18
        y += 4

    img.save(os.path.join(evdir, "summary.png"))
    print("wrote summary.png")


main()
