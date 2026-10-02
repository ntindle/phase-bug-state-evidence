#!/usr/bin/env python3
"""Render evidence summary PNG for issue #7359 (Jeleva unbounded free cast).

Usage: render_summary_7359.py <evidence_dir>
Reads run.json, pre.json, mid_trigger.json, post.json from the dir;
writes summary.png derived from the saved evidence.
"""
import json
import os
import sys

from PIL import Image, ImageDraw

W, H = 1000, 900
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


def load_opt(evdir, name):
    p = os.path.join(evdir, name)
    return load(p) if os.path.exists(p) else None


def jeleva_line(env):
    s = env["state"]
    for o in s.get("objects", {}).values():
        if (o.get("zone") == "Battlefield"
                and str(o.get("base_name") or "").lower()
                == "jeleva, nephalia's scourge"):
            return (f"tapped={o.get('tapped')} "
                    f"p/t={o.get('power')}/{o.get('toughness')}")
    return "not on battlefield"


def exile_line(env):
    s = env["state"]
    n_spell = 0
    n_other = 0
    for o in s.get("objects", {}).values():
        if o.get("zone") != "Exile":
            continue
        ct = o.get("card_types") or {}
        core = [str(t).lower() for t in (ct.get("core_types") or [])]
        if "instant" in core or "sorcery" in core:
            n_spell += 1
        else:
            n_other += 1
    return f"exile: {n_spell} instant/sorcery, {n_other} other"


def stack_line(env):
    s = env["state"]
    kinds = [((e.get("kind") or {}).get("type"), e.get("source_id"))
             for e in s.get("stack", []) or []]
    return ("stack(%d): " % len(kinds)) + ", ".join(
        f"{k}(src={src})" for k, src in kinds) if kinds else "stack(0): empty"


def gy_bolts(env):
    s = env["state"]
    return sum(1 for o in s.get("objects", {}).values()
               if o.get("zone") == "Graveyard"
               and str(o.get("base_name") or "").lower() == "lightning bolt")


def main():
    evdir = sys.argv[1]
    run = load(os.path.join(evdir, "run.json"))
    pre = load(os.path.join(evdir, "pre.json"))
    mid = load_opt(evdir, "mid_trigger.json")
    post = load(os.path.join(evdir, "post.json"))
    srv = run["server"]
    obs = run.get("observations", {})

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #7359 - Jeleva, Nephalia's Scourge "
           "(unbounded free cast)", fill=ACCENT)
    y += 26
    d.text((24, y), f"server v{srv['server_version']} ({srv['build_commit']}, "
           f"protocol {srv['protocol_version']}) | run {run['run_id']} | "
           f"{run['started_at'][:10]} | verdict: {run['verdict']}", fill=DIM)
    y += 30
    d.text((24, y), "Setup: " + run.get("setup_line", ""), fill=TEXT)
    y += 24
    d.text((24, y), "Contract: " + run.get("contract_line", ""), fill=DIM)
    y += 34

    panels = [("PRE (attack declared; trigger on stack; exiled spells in exile)",
               pre)]
    if mid is not None:
        panels.append(("MID (optional-cast decision point)", mid))
    panels.append(("POST (observation window closed)", post))
    for tag, env in panels:
        d.rectangle([16, y, W - 16, y + 116], fill=PANEL, outline=(45, 52, 64))
        d.text((28, y + 8), tag, fill=YELLOW)
        s = env["state"]
        p1life = next((p.get("life") for p in s.get("players", [])
                       if p.get("id") == 1), "?")
        d.text((28, y + 32),
               f"turn {s.get('turn_number')} phase {s.get('phase')} | "
               f"Jeleva: {jeleva_line(env)} | P1 life={p1life}", fill=TEXT)
        d.text((28, y + 56), exile_line(env) +
               f" | GY bolts={gy_bolts(env)}", fill=TEXT)
        d.text((28, y + 80), stack_line(env)[:150], fill=TEXT)
        y += 128

    d.rectangle([16, y, W - 16, y + 142], fill=PANEL, outline=(45, 52, 64))
    d.text((28, y + 8), "Assertions", fill=YELLOW)
    yy = y + 34
    labels = {
        "A1_setup": "pre: Jeleva on BF, >=2 exiled instants/sorceries",
        "A2_first_cast": "first free cast completes (exile -> P1 takes 3)",
        "A3_single_bound": "no second free cast (FAILS = reported bug)",
        "A4_exile_remainder": "post: uncast exiled spells stay in exile",
    }
    for k, v in run["assertions"].items():
        color = GREEN if v == "passed" else (RED if v == "failed" else DIM)
        d.text((28, yy), f"{k}: {v}", fill=color)
        d.text((300, yy), labels.get(k, "")[:62], fill=DIM)
        yy += 24
    y += 154

    d.text((24, y), f"Free casts completed from the single attack trigger: "
           f"{obs.get('free_casts', '?')} "
           f"(optional accepts={obs.get('optional_accepts', '?')}, "
           f"card choices={obs.get('card_choices_made', '?')})", fill=TEXT)
    y += 26
    d.text((24, y), "Root cause (card-data parse): the Attacks trigger lowers "
           "to CastFromZone {target: exiled", fill=DIM)
    y += 22
    d.text((24, y), "instant/sorcery + ExiledBySource, free cast: yes} with NO "
           "count bound - the bound belongs on", fill=DIM)
    y += 22
    d.text((24, y), "CastFromZone generally.", fill=DIM)
    y += 30
    d.text((24, y), "Evidence: ntindle/phase-bug-state-evidence 7359/" +
           run["run_id"], fill=(120, 130, 150))

    out = os.path.join(evdir, "summary.png")
    img.save(out)
    print("wrote", out)


main()
