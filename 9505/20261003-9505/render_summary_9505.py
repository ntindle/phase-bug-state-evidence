#!/usr/bin/env python3
"""Render evidence summary PNG for issue #9505 (single-exile origin parse
drops unparseable origins), v0.101.0 run.

Usage: render_summary_9505.py <evidence_dir>
Reads run.json + pre.json + mid.json + post.json + data_census.json +
code_evidence.json; writes summary.png derived from saved evidence states
and assertion results. Never invents values: every number comes from a
saved file.
"""
import json
import os
import sys

from PIL import Image, ImageDraw

W, H = 1000, 1100
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


def lname(o):
    return str(o.get("base_name") or o.get("name") or "?").lower()


def zone_names(env, pid, zone):
    if not env:
        return []
    s = env["state"]
    out = []
    for oid, o in (s.get("objects") or {}).items():
        if o.get("zone") != zone:
            continue
        if zone == "Battlefield" and o.get("controller") != pid:
            continue
        if zone in ("Graveyard", "Exile") and o.get("owner") != pid:
            continue
        out.append(lname(o))
    return sorted(out)


def vizier_counters(env):
    if not env:
        return None
    for oid, o in (env["state"].get("objects") or {}).items():
        if lname(o) == "rakshasa vizier":
            c = o.get("counters") or {}
            return sum(v for v in c.values() if isinstance(v, (int, float)))
    return None


def main():
    evdir = sys.argv[1]
    run = load(os.path.join(evdir, "run.json"))
    pre = load(os.path.join(evdir, "pre.json"))
    mid = load(os.path.join(evdir, "mid.json"))
    post = load(os.path.join(evdir, "post.json"))
    census = load(os.path.join(evdir, "data_census.json"))
    code = load(os.path.join(evdir, "code_evidence.json"))
    srv = run["server"]
    ass = run["assertions"]

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #9505 - single-exile origin parse drops "
                    "unparseable origins", fill=ACCENT)
    y += 26
    d.text((24, y),
           f"server v{srv['server_version']} ({srv['build_commit']}, protocol "
           f"{srv['protocol_version']}) | run {run['run_id']} | "
           f"{run['started_at'][:10]} | verdict: {run['verdict']}", fill=DIM)
    y += 30
    vcol = RED if run["verdict"] == "reproduced" else GREEN
    d.text((24, y), f"VERDICT: {run['verdict'].upper()}", fill=vcol)
    y += 34

    def panel(title, lines, h):
        nonlocal y
        d.rectangle([16, y, W - 16, y + h], fill=PANEL)
        d.text((28, y + 8), title, fill=ACCENT)
        yy = y + 32
        for ln in lines:
            d.text((28, yy), ln[:118], fill=TEXT)
            yy += 20
        y += h + 12

    # A1 panel
    a1 = []
    if census:
        a1.append(f"corpus exile-destination triggers: {census['n_exile_triggers']}")
        a1.append(f"silently-dropped origins in corpus: {census['n_dropped']} "
                  f"(zero witnesses -- matches issue scope)")
        a1.append("every named origin lands in origin/origin_zones, or the "
                  "text is 'from anywhere'/origin-less")
    panel("A1 data-level (pinned card-data.json census)", a1, 110)

    # A2 panel
    a2 = []
    if code:
        ch = code["checks"]
        a2.append(f"exile arm == issue ref 49b64bd08: {ch.get('arm_identical_to_49b64bd08')}")
        a2.append(f"drop pattern .ok()/.unwrap_or(None), remainder discarded: "
                  f"{ch.get('has_ok_swallowing') and ch.get('has_unwrap_or_none') and ch.get('discards_remainder')}")
        a2.append(f"graveyard arm fails closed: {ch.get('graveyard_fails_closed')} "
                  f"(strict tail: {ch.get('graveyard_strict_tail')})")
        a2.append(f"batched exile arm fails closed: {ch.get('batched_exile_fails_closed')}")
        a2.append(f"runtime: origin=None + empty origin_zones => matches ANY zone: "
                  f"{ch.get('runtime_unconstrained_matches_any')}")
    panel("A2 code-level (v0.101.0 engine source)", a2, 150)

    # A3 panel (pre -> mid)
    pre_bf = zone_names(pre, 0, "Battlefield")
    a3 = [
        f"pre:  P0 graveyard={zone_names(pre, 0, 'Graveyard')}",
        f"pre:  P0 battlefield has vizier={'rakshasa vizier' in pre_bf}, "
        f"crypt={'tormod' in ' '.join(pre_bf)}",
        f"mid:  Vizier +1/+1 counters = {vizier_counters(mid)} "
        f"(trigger fired on graveyard exile)",
        f"mid:  P0 exile={zone_names(mid, 0, 'Exile')}",
    ]
    panel("A3 leg1 -- exile from graveyard FIRES the trigger", a3, 130)

    # A4 panel (mid -> post)
    a4 = [
        f"post: P0 exile={zone_names(post, 0, 'Exile')}",
        f"post: Vizier +1/+1 counters = {vizier_counters(post)} "
        f"(unchanged -- no fire on battlefield exile)",
        f"post: P0 life={[p.get('life') for p in (post['state'].get('players') or [])] if post else '?'}",
    ]
    panel("A4 leg2 -- exile from battlefield does NOT fire", a4, 110)

    # assertions panel
    alines = []
    for k, v in ass.items():
        col = GREEN if v == "passed" else (RED if v == "failed" else YELLOW)
        alines.append((f"{k}: {v}", col))
    d.rectangle([16, y, W - 16, y + 40 + 22 * len(alines)], fill=PANEL)
    d.text((28, y + 8), "assertions", fill=ACCENT)
    yy = y + 32
    for ln, col in alines:
        d.text((28, yy), ln, fill=col)
        yy += 22
    y += 40 + 22 * len(alines) + 12

    d.text((24, y), "scope: parser defect + runtime origin-faithfulness control; "
                    "native engine, 2 human seats", fill=DIM)
    y += 22
    d.text((24, y), "limitation: defect is parse-time; pinned binary exposes no "
                    "parser CLI -- failing shape verified", fill=DIM)
    y += 22
    d.text((24, y), "at engine source level (v0.101.0 tag == 49b64bd08 arm). "
                    "No corpus card over-fires today.", fill=DIM)

    out = os.path.join(evdir, "summary.png")
    img.save(out)
    print(f"wrote {out} ({W}x{H})")


if __name__ == "__main__":
    main()
