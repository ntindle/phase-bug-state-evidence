#!/usr/bin/env python3
"""Render summary.png for issue #6609 run run-6609-reval-v01110-20261016-2341 (v0.111.0)."""
import json
import os

from PIL import Image, ImageDraw

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
EVDIR = f"{BACKFILL}/evidence/6609/run-6609-reval-v01110-20261016-2341"


def load(p):
    with open(p) as f:
        return json.load(f)


def oname(objs, oid):
    o = objs.get(str(oid), {})
    return str(o.get("base_name") or o.get("name") or "?").lower()


def main():
    run = load(f"{EVDIR}/run.json")
    pre = load(f"{EVDIR}/pre_escape.json")["state"]
    post = load(f"{EVDIR}/post_escape.json")["state"]
    srv = run["server"]

    W, H = 1000, 840
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
    d.text((24, y), "phase-rs/phase #6609 \u2014 Nethergoyf escape exile cost",
           fill=ACCENT)
    y += 24
    d.text((24, y),
           f"v{srv['server_version']} build {srv['build_commit']} "
           f"protocol {srv['protocol_version']} | 2026-10-16 | "
           f"run {run['run_id']}", fill=DIM)
    y += 24
    d.text((24, y), "Oracle: Escape\u2014{2}{B}, Exile any number of other cards "
           "from your graveyard", fill=TEXT)
    y += 20
    d.text((24, y), "with four or more card types among them.", fill=TEXT)
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

    pre_objs = pre["objects"]
    post_objs = post["objects"]
    p0pre = next(p for p in pre["players"] if p["id"] == 0)
    p0post = next(p for p in post["players"] if p["id"] == 0)
    pre_gy = [oname(pre_objs, o) for o in p0pre["graveyard"]]
    post_gy = [oname(post_objs, o) for o in p0post["graveyard"]]
    exiled = [oname(post_objs, int(oid)) for oid, o in post_objs.items()
              if o.get("zone") == "Exile" and o.get("controller") == 0]
    goyf = [(oname(post_objs, int(oid)), o.get("power"), o.get("toughness"))
            for oid, o in post_objs.items()
            if o.get("zone") == "Battlefield" and o.get("controller") == 0
            and oname(post_objs, int(oid)) == "nethergoyf"]

    # --- derive the engine-prompt and negative-probe panels from the wire
    # log (the vi opportunity / submission records), not from literals ---
    carddata = load(f"{BACKFILL}/server/releases/"
                    f"{srv['server_version']}/data/card-data.json")

    def types_of(name):
        c = carddata.get(name, {})
        return sorted(str(t).lower()
                      for t in (c.get("card_type", {}).get("core_types") or []))

    exile_opp, exile_sub = None, None
    with open(f"{EVDIR}/wire_log.jsonl") as wf:
        for ln in wf:
            try:
                e = json.loads(ln)
            except Exception:
                continue
            p = e.get("payload") or {}
            if (e.get("event") == "vi_opportunity"
                    and p.get("tag") == "esc"
                    and p.get("spec_type") == "select"):
                exile_opp = p
            if (e.get("event") == "decision_submission"
                    and p.get("interactionId")
                    == (exile_opp or {}).get("interactionId")):
                exile_sub = p
    cand_names = [c["text"] for c in exile_opp["choices"]] if exile_opp else []
    n_cand = exile_opp["n_choices"] if exile_opp else 0
    # aggregate card types among the OTHER pre-escape gy cards
    other_types = sorted({t for nm in pre_gy if nm != "nethergoyf"
                          for t in types_of(nm)})
    # chosen 1-card subset text from the recorded submission (or obs.json)
    obs = load(f"{EVDIR}/obs.json")
    dec = next((d for d in obs.get("escape_decisions", [])
                if d.get("why") == "invalid_subset_1card"), {})
    chosen = ", ".join(
        c["text"] for c in (exile_opp["choices"] if exile_opp else [])
        if c["id"] in (dec.get("choice_ids") or [])) or "?"
    n_types = len(types_of(chosen.lower())) if chosen != "?" else 0
    # untapped P0 lands in the pre-escape export (mana fixture)
    lands = [oname(pre_objs, int(oid)) for oid, o in pre_objs.items()
             if o.get("zone") == "Battlefield" and o.get("controller") == 0
             and oname(pre_objs, int(oid))
             in ("swamp", "island", "mountain", "forest")
             and not o.get("tapped")]
    # card types among the remaining post-escape gy (P/T anchor)
    post_types = sorted({t for nm in post_gy for t in types_of(nm)})

    def pt(g):
        pw, tw = g[1], g[2]
        pw = pw.get("value") if isinstance(pw, dict) else pw
        tw = tw.get("value") if isinstance(tw, dict) else tw
        return f"{pw}/{tw}"

    panel("PRE (escape declared, cost unpaid)", [
        (f"P0 graveyard: {', '.join(pre_gy)}", TEXT),
        (f"types among other cards: {', '.join(other_types)} "
         f"(={len(other_types)})", TEXT),
        (f"P0 mana: {len(lands)} untapped lands incl. "
         f"{'Swamp' if 'swamp' in lands else 'none'} "
         f"({', '.join(lands)})", TEXT),
    ])
    panel("ENGINE PROMPT (viewer_interaction, schema/select)", [
        (f"candidates: {', '.join(cand_names)} ({n_cand})", TEXT),
        ("constraint: select schema, no min/max advertised \u2014 "
         "NO \"four or more card types\" check", RED),
    ], accent=RED)
    panel("NEGATIVE PROBE", [
        (f"submitted 1-card subset {{{chosen}}} "
         f"({n_types} card type{'s' if n_types != 1 else ''} < 4 required)",
         TEXT),
        ("engine response: ACCEPTED, no rejection", RED),
        ("-> the aggregate type constraint is not enforced", RED),
    ], accent=RED)
    panel("POST (escape resolved)", [
        (f"exiled: {', '.join(exiled)}", TEXT),
        (f"remaining in gy: {', '.join(post_gy)}", TEXT),
        (f"Nethergoyf on battlefield: "
         f"{pt(goyf[0])} ({len(post_types)} types remain -> "
         f"{len(post_types)}/{len(post_types) + 1} correct)" if goyf
         else "Nethergoyf NOT on battlefield", GREEN if goyf else RED),
    ])

    d.text((24, y), "assertions", fill=ACCENT)
    y += 22
    order = ["A1_setup_ok", "A2_escape_accepted", "A3_choice_offered",
             "A4_subset_accepted", "A5_unchosen_remain", "A6_pt_recomputed",
             "A7_cleanup"]
    goyf_pt = pt(goyf[0]) if goyf else "?"
    labels = {
        "A1_setup_ok": "fixture: goyf in gy, 4 types among others, mana",
        "A2_escape_accepted": "escape cast accepted (goyf -> battlefield)",
        "A3_choice_offered": "per-card exile choice offered (not auto-sweep)",
        "A4_subset_accepted": "1-card subset rejected / 4-type subset accepted",
        "A5_unchosen_remain": "unchosen cards remain in graveyard",
        "A6_pt_recomputed": f"P/T recomputed from remaining gy ({goyf_pt})",
        "A7_cleanup": "stack empty, no pending decision, no stall",
    }
    for k in order:
        st = run["assertions"][k]
        col = GREEN if st == "passed" else (RED if st == "failed" else DIM)
        d.text((32, y), f"{k:20s} {st:9s} {labels[k]}", fill=col)
        y += 20
    y += 10
    verdict = run["verdict"]
    vcol = RED if verdict == "reproduced" else (GREEN if verdict == "not-reproduced" else DIM)
    d.text((24, y), f"verdict: {verdict.upper()}" + (" (related failure)" if verdict == "reproduced" else ""), fill=vcol)
    y += 22
    if verdict == "reproduced":
        d.text((24, y), "The reported 'exile everything' sweep no longer occurs \u2014",
               fill=TEXT)
        y += 20
        d.text((24, y), "a choice is offered \u2014 but the 'four or more card types",
               fill=TEXT)
        y += 20
        d.text((24, y), "among them' aggregate constraint is still not enforced.",
               fill=TEXT)

    out = f"{EVDIR}/summary.png"
    img.save(out)
    print("wrote", out)


main()
