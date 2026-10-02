#!/usr/bin/env python3
"""Render evidence summary PNG for issue #7366 (Dusk // Dawn: the Aftermath
half Dawn is absent from card data, so it cannot be played from the
graveyard).

Usage: render_summary_7366.py <evidence_dir>
Reads run.json, data_evidence.json, deck_reject.json, pre.json,
mid_cast.json (optional), post.json from the dir; writes summary.png
derived from the saved evidence.
"""
import json
import os
import sys

from PIL import Image, ImageDraw

W, H = 1000, 980
BG = (18, 20, 26)
TEXT = (235, 238, 245)
DIM = (150, 160, 175)
ACCENT = (110, 180, 255)
GREEN = (110, 220, 140)
RED = (240, 120, 120)
YELLOW = (240, 200, 110)


def load(p):
    with open(p) as f:
        return json.load(f)


def load_opt(evdir, name):
    p = os.path.join(evdir, name)
    return load(p) if os.path.exists(p) else None


def gy_line(env, label):
    s = env["state"]
    for p in s.get("players", []):
        if p.get("id") == 0:
            gy = [str((s.get("objects", {}).get(str(o), {}) or {})
                       .get("base_name", "?")) for o in p.get("graveyard", [])]
            return f"{label}: P0 graveyard={gy} life={p.get('life')}"
    return f"{label}: P0 not found"


def stack_line(env):
    s = env["state"]
    kinds = [((e.get("kind") or {}).get("type"))
             for e in s.get("stack", []) or []]
    return "stack(%d): %s" % (len(kinds), ", ".join(kinds) if kinds else "empty")


def main():
    evdir = sys.argv[1]
    run = load(os.path.join(evdir, "run.json"))
    data_ev = load_opt(evdir, "data_evidence.json")
    deck_rej = load_opt(evdir, "deck_reject.json")
    pre = load_opt(evdir, "pre.json")
    mid = load_opt(evdir, "mid_cast.json")
    post = load_opt(evdir, "post.json")

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 24

    def text(line, color=TEXT, indent=24):
        nonlocal y
        d.text((indent, y), line, fill=color)
        y += 22

    d.text((24, y), "phase-rs/phase #7366 -- Dusk // Dawn: Dawn half missing "
                     "from card data", fill=ACCENT)
    y += 30
    text(f"server {run['server']['server_version']} "
         f"(build {run['server']['build_commit']}, "
         f"protocol {run['server']['protocol_version']})", DIM)
    text(f"run {run['run_id']}  |  {run.get('started_at', '')}  |  "
         f"{run.get('duration_s', '?')}s", DIM)
    verdict = run["verdict"]
    vcolor = RED if verdict == "reproduced" else (
        GREEN if verdict == "not-reproduced" else YELLOW)
    text(f"VERDICT: {verdict}", vcolor)
    y += 6

    d.text((24, y), "Contract:", fill=ACCENT)
    y += 22
    text("Dawn (Aftermath: castable only from graveyard) is absent from", DIM)
    text("card data -> engine rejects it as unresolvable; after Dusk", DIM)
    text("resolves to the graveyard, no Dawn cast path exists.", DIM)
    y += 6

    if data_ev:
        d.text((24, y), "Data level (pinned card-data.json):", fill=ACCENT)
        y += 22
        text(f"dusk oracle_text: {data_ev.get('dusk_oracle_text')!r}", DIM)
        mc = data_ev.get("dusk_mana_cost") or {}
        text(f"dusk mana_cost: generic={mc.get('generic')} "
             f"shards={mc.get('shards')}  (true Dusk is {{2}}{{B}}{{B}})", DIM)
        text(f"faces={data_ev.get('dusk_faces')} layout={data_ev.get('dusk_layout')} "
             f"face_index={data_ev.get('dusk_face_index')}", DIM)
        text(f"'dawn' key present: {data_ev.get('dawn_key_present')}  |  "
             f"'dusk // dawn' key present: {data_ev.get('dusk_dawn_key_present')}", DIM)
        corp = data_ev.get("corpus") or {}
        backs = corp.get("backs_missing") or []
        text(f"class scope: {len(backs)}/{corp.get('fronts_checked')} split-card "
             f"back halves missing", DIM)
        text("  " + ", ".join(backs[:9]), DIM)
        text("  " + ", ".join(backs[9:18]), DIM)
        text("  " + ", ".join(backs[18:]), DIM)
        y += 6

    if deck_rej:
        d.text((24, y), "Runtime level:", fill=ACCENT)
        y += 22
        resp = deck_rej.get("response") or {}
        text(f"deck ['Dawn'x4, 'Swamp'x56] -> {resp.get('type')}: "
             f"{json.dumps(resp.get('data', {}))[:88]}", DIM)
        y += 6

    if pre:
        text(gy_line(pre, "pre "), DIM)
        text(stack_line(pre), DIM)
        y += 4
    if mid:
        text(gy_line(mid, "mid "), DIM)
        text(stack_line(mid), DIM)
        y += 4
    if post:
        text(gy_line(post, "post"), DIM)
        text(stack_line(post), DIM)
        text("waiting_for: %s" % ((post["state"].get("waiting_for") or {})
                                  .get("type")), DIM)
        y += 6

    d.text((24, y), "Assertions:", fill=ACCENT)
    y += 24
    for k, v in (run.get("assertions") or {}).items():
        c = GREEN if v == "passed" else (RED if v == "failed" else YELLOW)
        text(f"  {k}: {v}", c)
    y += 6

    obs = run.get("observations") or {}
    text(f"dusk cast: {obs.get('dusk_cast')}  |  resolved to graveyard: "
         f"{obs.get('dusk_resolved')}  |  'dawn' mentions at post: "
         f"{obs.get('dawn_mentions_at_post')}", DIM)
    text(f"mana payment observed: {obs.get('mana_payment_seen')}", DIM)
    y += 6

    d.text((24, y), "Notes:", fill=ACCENT)
    y += 24
    for n in (run.get("notes") or [])[:9]:
        for seg in [n[i:i + 108] for i in range(0, len(n), 108)][:3]:
            text("  " + seg, DIM)

    out = os.path.join(evdir, "summary.png")
    img.save(out)
    print("wrote", out)


if __name__ == "__main__":
    main()
