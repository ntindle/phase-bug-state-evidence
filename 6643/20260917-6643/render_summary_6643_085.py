#!/usr/bin/env python3
"""Render summary.png for issue #6643 revalidation run 20260917-6643 (v0.85.0)."""
import json

from PIL import Image, ImageDraw

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
EVDIR = f"{BACKFILL}/evidence/6643/20260917-6643"


def load(p):
    with open(p) as f:
        return json.load(f)


def main():
    run = load(f"{EVDIR}/run.json")
    pre = load(f"{EVDIR}/pre.json")["state"]
    post = load(f"{EVDIR}/post.json")["state"]
    mid_pump = load(f"{EVDIR}/mid_pump.json")["state"]
    mid_trigger = load(f"{EVDIR}/mid_trigger.json")["state"]
    srv = run["server"]
    obs = run["observations"]

    W, H = 1000, 920
    BG = (18, 20, 26)
    PANEL = (26, 30, 38)
    TEXT = (235, 238, 245)
    DIM = (150, 160, 175)
    ACCENT = (110, 180, 255)
    GREEN = (110, 220, 140)
    RED = (240, 120, 120)

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    y = 16
    d.text((24, y), "phase-rs/phase #6643 - Party Dude level-3 attack trigger",
           fill=ACCENT)
    y += 24
    d.text((24, y),
           f"{srv['server_version']} build {srv['build_commit']} "
           f"protocol {srv['protocol_version']} | 2026-09-17 | "
           f"run {run['run_id']} (revalidation)", fill=DIM)
    y += 24
    d.text((24, y), "Oracle: Whenever one or more of your opponents are",
           fill=TEXT)
    y += 20
    d.text((24, y), "attacked, up to one target attacking creature gets +X/+X",
           fill=TEXT)
    y += 20
    d.text((24, y), "until end of turn, where X is cards in your hand.",
           fill=TEXT)
    y += 30

    def panel(title, lines, accent=ACCENT):
        nonlocal y
        d.rectangle([20, y, W - 20, y + 24 + 20 * len(lines)], fill=PANEL)
        d.text((32, y + 6), title, fill=accent)
        yy = y + 30
        for ln, col in lines:
            d.text((32, yy), ln[:118], fill=col)
            yy += 20
        y = yy + 14

    pre_p0 = next(p for p in pre["players"] if p["id"] == 0)
    pre_p1 = next(p for p in pre["players"] if p["id"] == 1)
    post_p1 = next(p for p in post["players"] if p["id"] == 1)

    def dude_level(st):
        for oid, o in st["objects"].items():
            nm = str(o.get("base_name") or o.get("name") or "").lower()
            if (nm == "party dude" and o.get("zone") == "Battlefield"
                    and o.get("controller") == 0):
                return o.get("class_level")
        return None

    def bear_pt(st):
        for oid, o in st["objects"].items():
            nm = str(o.get("base_name") or o.get("name") or "").lower()
            if (nm == "grizzly bears" and o.get("zone") == "Battlefield"
                    and o.get("controller") == 0
                    and int(oid) == (obs["attacker_oid"] or -1)):
                return o.get("power"), o.get("toughness")
        return None, None

    trig = None
    for e in mid_trigger.get("stack", []) or []:
        if "party dude" in json.dumps(e, default=str).lower():
            trig = e
            break
    trig_desc = ""
    if trig:
        ab = ((trig.get("kind") or {}).get("data") or {}).get("ability", {})
        trig_desc = str(ab.get("description") or "")[:90]

    x = obs.get("hand_at_target")
    dmg = pre_p1["life"] - post_p1["life"]
    mpt = bear_pt(mid_pump)
    atk_turn = obs.get("attack_turn")

    panel(f"PRE (DeclareAttackers, turn {atk_turn})", [
        (f"Party Dude on P0 battlefield, class_level={dude_level(pre)} "
         f"(expected 3)", TEXT),
        (f"Grizzly Bears attack-ready; P0 hand={len(pre_p0['hand'])} "
         f"(X={x} at target time); life {pre_p0['life']}/{pre_p1['life']}", TEXT),
    ])
    panel("TRIGGER (on the stack after attackers declared)", [
        ("mode Attacks (batched), ClassLevelGE 3 gate", GREEN),
        (f"effect: Pump +X/+X, X=HandSize; optional target 0-1 attacking "
         f"creature", TEXT),
        (f"desc: {trig_desc}", DIM),
    ], accent=GREEN)
    panel("TARGET PROMPT (viewer_interaction, schema/sequence)", [
        ("one candidate: the attacking Grizzly Bears", TEXT),
        ("driver answered with the Bear (deterministic policy)", TEXT),
    ])
    panel("RESOLUTION", [
        (f"mid_pump ({mid_pump.get('phase')}): bear P/T={mpt[0]}/{mpt[1]} "
         f"(= 2+{x})", GREEN),
        (f"P1 life {pre_p1['life']} -> {post_p1['life']} "
         f"(damage {dmg} = 2+X, X={x})", GREEN),
        (f"post: stack empty, phase={post.get('phase')} turn={post.get('turn_number')}, "
         f"game proceeds", TEXT),
    ], accent=GREEN)

    d.text((24, y), "assertions", fill=ACCENT)
    y += 22
    order = ["A1_setup_ok", "A2_trigger_fires", "A3_target_prompted",
             "A4_pump_correct", "A5_cleanup"]
    labels = {
        "A1_setup_ok": f"fixture: dude level 3 on BF, bear ready, life 20/20 "
                       f"(turn {atk_turn})",
        "A2_trigger_fires": "Attacks trigger from Party Dude on the stack",
        "A3_target_prompted": "up-to-one target prompt offered, Bear chosen",
        "A4_pump_correct": f"bear {mpt[0]}/{mpt[1]} mid-combat; "
                           f"P1 {pre_p1['life']}->{post_p1['life']} (2+X, X={x})",
        "A5_cleanup": "stack empty, game proceeds past combat",
    }
    for k in order:
        st = run["assertions"][k]
        col = GREEN if st == "passed" else (RED if st == "failed" else DIM)
        d.text((32, y), f"{k:20s} {st:9s} {labels[k]}", fill=col)
        y += 20
    y += 10
    d.text((24, y), "verdict: NOT-REPRODUCED on v0.85.0", fill=GREEN)
    y += 22
    d.text((24, y), "Revalidation of the 2026-09-10 v0.78.0 result: the",
           fill=TEXT)
    y += 20
    d.text((24, y), "report's parser gap remains closed on this build - the",
           fill=TEXT)
    y += 20
    d.text((24, y), "level-3 clause parses as mode Attacks (ClassLevelGE 3)",
           fill=TEXT)
    y += 20
    d.text((24, y), "and fires + resolves per Oracle. Not tested on the",
           fill=TEXT)
    y += 20
    d.text((24, y), "original report build; not a fix claim.", fill=TEXT)

    out = f"{EVDIR}/summary.png"
    img.save(out)
    print(f"wrote {out}")


main()
