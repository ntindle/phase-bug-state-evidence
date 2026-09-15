#!/usr/bin/env python3
"""Manual finalizer for #7181 run 20260915-7181.

The driver was stopped after the game soft-locked on a dangling
SearchChoice prompt (Beseech #1's library search never completed). The
decisive cost measurement (A3) is fully captured in the immutable
exports; this script assembles run.json / summary.png / manifest.sha256
from those exports plus the run log, with corrected assertion semantics
(the driver's in-memory ST would have marked A5 resolved merely because
the spell reached the graveyard, but no card was tutored and the search
prompt never completed).
"""
import hashlib
import json
import os
import re
import shutil
import sys
import time

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 7181
RUN_ID = "20260915-7181"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
RELDIR = f"{BACKFILL}/server/releases/v0.84.0"

BALANCER = "witherbloom, the balancer"
BESEECH = "beseech the queen"
BEAR = "grizzly bears"
FOREST = "forest"
SWAMP = "swamp"
LANDS = (FOREST, SWAMP)
P0_DECK = [(BALANCER, 8), (BEAR, 12), (BESEECH, 8), (FOREST, 16),
           (SWAMP, 16)]
P1_DECK = [(BEAR, 12), (FOREST, 48)]


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_PIN = {
    "binary_sha256": "a73f671c840398ab31834621caae6ba7be2d5265355ae793717af3cbdda6e336",
    "card_data_sha256": "6980906a6fef33b37f3ba4b5356ecfb8e89d4e6aa7407a585797b6f5a67d0c35",
    "draft_pools_sha256": "c9745019c2c7b933c4b4b2cbeed8b9ff2de75e0a15fe0f46e12b4633e1a47fbe",
}
_ACTUAL = {
    "binary_sha256": sha256_file(f"{RELDIR}/phase-server-slim-x86_64-unknown-linux-musl"),
    "card_data_sha256": sha256_file(f"{RELDIR}/data/card-data.json"),
    "draft_pools_sha256": sha256_file(f"{RELDIR}/data/draft-pools.json"),
}
assert _ACTUAL == _PIN, f"digest mismatch: {_ACTUAL}"

SERVER_IDENTITY = {
    "server_version": "v0.84.0",
    "build_commit": "eb7e93e",
    "protocol_version": 71,
    "mode": "single-user",
    "binary_sha256": _ACTUAL["binary_sha256"],
    "card_data_sha256": _ACTUAL["card_data_sha256"],
    "draft_pools_sha256": _ACTUAL["draft_pools_sha256"],
    "digests_match_pin": True,
    "signature_verified": True,
    "signature_note": "minisign global signatures on binary + signed data "
                      "manifest verified against repo-pinned "
                      "SERVER_ARTIFACT_PUBLIC_KEY (key id 436711b6a2d36828) "
                      "in prehashed (blake2b-512) mode at pin time "
                      "2026-09-15; data digests match the signed manifest; "
                      "digests recomputed against on-disk files this run; "
                      "release v0.84.0 confirmed latest stable via GitHub "
                      "releases API 2026-09-15",
    "observed_at": "2026-09-15",
    "handshake": "ServerHello observed pre-run: v0.84.0 / eb7e93e / "
                 "protocol 71 / mode Full on 127.0.0.1:9375",
    "source": "verified pin; isolated server on 127.0.0.1:9375 started by "
              "this run under runs/20260915-7181/",
}


def lname_of(objs, oid):
    o = objs[str(oid)]
    return str(o.get("base_name") or o.get("name") or "").lower()


def main():
    notes = []
    ass = {}
    log = open(f"{EVDIR}/scenario_run.log").read()

    def log_has(s):
        return s in log

    # ---- load states ----
    states = {}
    for fn in ("pre_cast5", "post_cast5"):
        states[fn] = json.loads(
            open(f"{EVDIR}/{fn}.json").read())["state"]

    def measure(fn):
        st = states[fn]
        objs = st["objects"]
        p0bf = [oid for oid, o in objs.items()
                if o.get("zone") == "Battlefield"
                and o.get("controller") == 0]
        creatures = [oid for oid in p0bf
                     if lname_of(objs, oid) in (BALANCER, BEAR)]
        unt = [oid for oid in p0bf
               if lname_of(objs, oid) in LANDS
               and not objs[str(oid)].get("tapped")]
        return st, len(creatures), len(unt)

    pre_st, pre_cr, pre_unt = measure("pre_cast5")
    post_st, post_cr, post_unt = measure("post_cast5")

    # beseech spell object on the stack in post_cast5
    bq = None
    for oid, o in post_st["objects"].items():
        if lname_of(post_st["objects"], oid) == BESEECH \
                and o.get("zone") == "Stack":
            bq = o
            break
    assert bq is not None, "beseech spell object not on stack in post_cast5"

    # ---- A0: card-data parse (informational) ----
    cd = json.load(open(f"{RELDIR}/data/card-data.json"))
    bq_cd = cd[BESEECH]
    wb_cd = cd[BALANCER]
    shards = ((bq_cd.get("mana_cost") or {}).get("shards") or [])
    grant = [a for a in (wb_cd.get("static_abilities") or [])
             if ((a.get("mode") or {}).get("CastWithKeyword") or {})
             .get("keyword", {}).get("Affinity") is not None]
    own_aff = any(isinstance(k, dict) and "Affinity" in k
                  for k in (wb_cd.get("keywords") or []))
    notes.append(
        f"A0: card-data beseech the queen: mana_cost shards={shards}; "
        f"witherbloom, the balancer: own_affinity_keyword={own_aff}, "
        f"grants_affinity_to_instant_sorcery="
        f"{len(grant) > 0} (CastWithKeyword static ability)")

    # ---- A1: setup ----
    ok = (log_has("Balancer ENTERED oid=22 turn=12")
          and pre_cr == 5 and post_cr == 5
          and pre_st.get("turn_number") == 32
          and post_st.get("turn_number") == 32)
    notes.append(
        f"A1: balancer_oid=22 entered turn=12; pre_cast5 turn="
        f"{pre_st.get('turn_number')} phase={pre_st.get('phase')} "
        f"creatures={pre_cr} untapped_lands={pre_unt}; post_cast5 turn="
        f"{post_st.get('turn_number')} creatures={post_cr} "
        f"untapped_lands={post_unt}")
    ass["A1_setup"] = "passed" if ok else "failed"

    # ---- A2: bear control ----
    m = re.search(r"bear \(control\) on stack: tapped_delta=(\d+) "
                  r"\(untapped (\d+)->(\d+)\)", log)
    if m:
        d = int(m.group(1))
        ok = d == 2
        notes.append(f"A2: control bear tapped_delta={d} (expect 2 = "
                     f"full {{1}}{{G}}; untapped {m.group(2)}->"
                     f"{m.group(3)})")
        ass["A2_bear_control"] = "passed" if ok else "failed"
    else:
        notes.append("A2 not-run: control bear measurement missing")
        ass["A2_bear_control"] = "not-run"

    # ---- A3: five-creature cost (THE reported bug) ----
    delta = pre_unt - post_unt
    spent = bq.get("mana_spent_to_cast_amount")
    colors = bq.get("colors_spent_to_cast")
    kw = bq.get("cast_spell_keywords")
    srcs = [s.get("source_id")
            for s in (bq.get("mana_spent_source_snapshots") or [])]
    ok = (delta == 1)
    notes.append(
        f"A3: Beseech#1 at 5 creatures: untapped {pre_unt}->{post_unt} "
        f"(delta={delta}, expect 1 = 6 generic - 5 affinity); spell "
        f"object records mana_spent_to_cast_amount={spent} "
        f"colors_spent_to_cast={colors} sources={srcs}; "
        f"cast_spell_keywords={json.dumps(kw)}")
    ass["A3_five_cost"] = "passed" if ok else "failed"

    # ---- A4/A5: not-run (search stall) ----
    notes.append(
        "A4 not-run: the 6-creature ('free') leg never executed - after "
        "Beseech#1 the game stalled on its SearchChoice prompt "
        "(see A5).")
    ass["A4_six_cost"] = "not-run"
    notes.append(
        "A5 not-run: Beseech#1 reached the graveyard but its SearchLibrary "
        "never completed - no card was tutored (P0 library 37->37, hand "
        "gained nothing), the driver's search answer was rejected "
        "invalid_interaction_response, and a SearchChoice prompt "
        "(player 0, library_owner 0, count 1) was left dangling, "
        "soft-locking the game. Not attributed to the engine: the "
        "answer format may be a driver limitation.")
    ass["A5_resolution"] = "not-run"

    # ---- verdict ----
    if ass["A1_setup"] != "passed":
        verdict = "blocked"
        notes.append("verdict=blocked: A1 failed")
    elif ass["A3_five_cost"] == "failed":
        verdict = "reproduced"
        notes.append(
            "verdict=reproduced: the granted affinity is attached to the "
            "spell (cast_spell_keywords shows Affinity for creatures) but "
            "the cost computation ignored it - Beseech the Queen cost "
            "{B}{B}{B} (3 mana) with 5 creatures out instead of {1}. "
            "This is exactly the reported 'not recognizing the "
            "Witherbloom's affinity mechanic'.")
    elif all(ass.get(k) == "passed" for k in
             ("A2_bear_control", "A3_five_cost")):
        verdict = "not-reproduced"
        notes.append("verdict=not-reproduced")
    else:
        verdict = "blocked"
        notes.append("verdict=blocked: incomplete assertion chain")
    notes.append(f"verdict={verdict}")

    run = {
        "run_id": RUN_ID, "issue": ISSUE,
        "verdict": verdict, "validated_at": "2026-09-15",
        "server": SERVER_IDENTITY,
        "driver": {"protocol_advertised": 71,
                   "client": "driver/client.py",
                   "finalizer": "driver/finalize_7181.py "
                                "(manual; driver stopped after search "
                                "stall, assertions recomputed from "
                                "exports)"},
        "scenario_sha256": sha256_file(
            f"{BACKFILL}/driver/scenario_{ISSUE}.py"),
        "format_config": "default Bo1 (2 human-client seats, life 20)",
        "decks": {"P0": P0_DECK, "P1": P1_DECK},
        "assertions": ass,
        "observations": {
            "rejections": [
                "invalid_interaction_response on the Beseech#1 "
                "SearchChoice answer (driver-submitted sequence/choiceIds; "
                "see wire_log.jsonl)"
            ],
            "unexpected_prompts": [],
            "tick_errors": [],
            "search_stall": "after Beseech#1, waiting_for stayed "
                            "SearchChoice (player 0, count 1) with the "
                            "spell in the graveyard and no card tutored; "
                            "game soft-locked; driver stopped, remaining "
                            "legs recorded not-run",
        },
        "driver_state": {
            "game_code": "IX151R",
            "balancer_oid": 22,
            "balancer_cast_turn": 12,
            "pre_cast5": {"turn": pre_st.get("turn_number"),
                          "phase": pre_st.get("phase"),
                          "p0_creatures": pre_cr,
                          "p0_untapped_lands": pre_unt},
            "post_cast5": {"turn": post_st.get("turn_number"),
                           "phase": post_st.get("phase"),
                           "p0_creatures": post_cr,
                           "p0_untapped_lands": post_unt,
                           "tapped_delta": delta,
                           "mana_spent_to_cast_amount": spent,
                           "colors_spent_to_cast": colors,
                           "mana_source_ids": srcs,
                           "cast_spell_keywords": kw},
            "bear_control_tapped_delta": int(m.group(1)) if m else None,
        },
        "notes": notes,
        "evidence_files": ["pre_cast5.json", "cast5_detail.json",
                           "post_cast5.json", "run.json",
                           "manifest.sha256", "summary.png",
                           f"scenario_{ISSUE}.py",
                           f"finalize_{ISSUE}.py",
                           "wire_log.jsonl", "scenario_run.log",
                           "server.log"],
        "limitations": [
            "Browser UI not exercised; native engine via two "
            "human-client seats.",
            "Card density is a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "P1 is passive (land drops + up to 2 bears, no attacks) so "
            "the cost-measurement window stays clean.",
            "The prebuilt server has no standalone state-restore; "
            "states are authoritative exports (restorable only via "
            "full game replay).",
            "Mana paid is measured as the untapped-land delta between "
            "the pre-cast and stack-sighting exports (same turn 32), "
            "corroborated by the spell object's own "
            "mana_spent_to_cast_amount / colors_spent_to_cast / "
            "mana_spent_source_snapshots; no other P0 actions were in "
            "flight.",
            "The 6-creature ('should be free') leg was not executed: "
            "after Beseech#1, its SearchLibrary choice prompt stayed "
            "unanswered (driver answer rejected) and the game "
            "soft-locked; A4/A5 are not-run, not engine verdicts.",
            "The reporter's '5 creatures -> {B} or {2}' phrasing is "
            "tested as 'pay {1} generic' per CR 601.2f (affinity "
            "reduces the generic portion of {2/B}{2/B}{2/B}).",
        ],
        "duration_s": None,
    }
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    print("wrote run.json")

    shutil.copy(f"{BACKFILL}/driver/scenario_{ISSUE}.py",
                f"{EVDIR}/scenario_{ISSUE}.py")
    shutil.copy(f"{BACKFILL}/driver/finalize_{ISSUE}.py",
                f"{EVDIR}/finalize_{ISSUE}.py")
    print("copied scenario + finalizer")

    # server.log excerpts
    with open(f"{BACKFILL}/runs/{RUN_ID}/server.log", "rb") as f:
        raw = f.read().decode("utf-8", "replace")
    clean = re.sub(r"\x1b\[[0-9;]*m", "", raw)
    excerpt = [ln for ln in clean.splitlines() if "IX151R" in ln]
    if not excerpt:
        excerpt = clean.splitlines()[-400:]
    with open(f"{EVDIR}/server.log", "w") as f:
        f.write("\n".join(excerpt) + "\n")
    print(f"wrote server.log ({len(excerpt)} lines)")

    render_summary(run)
    write_manifest()
    print(f"DONE verdict={verdict} assertions={json.dumps(ass)}")


def render_summary(run):
    from PIL import Image, ImageDraw
    W, H = 1000, 1180
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "phase-rs/phase #7181 - Beseech the Queen x "
           "Witherbloom", fill=(235, 240, 250))
    y += 28
    d.text((24, y), "server v0.84.0 (eb7e93e) protocol 71 - 2026-09-15"
           " - granted affinity for creatures on a sorcery",
           fill=(140, 160, 180))
    y += 28
    vcol = {"reproduced": (255, 90, 90),
            "not-reproduced": (120, 220, 120),
            "blocked": (230, 200, 120)}.get(run["verdict"],
                                            (180, 180, 180))
    d.text((24, y), f"verdict: {run['verdict'].upper()}", fill=vcol)
    y += 34
    for ln in [
            "Report: Beseech the Queen ({2/B}{2/B}{2/B}, MV 6) should",
            "be free with 6 creatures out via Witherbloom, the",
            "Balancer's 'instant and sorcery spells you cast have",
            "affinity for creatures' - but the affinity is allegedly",
            "not recognized.",
            "Observed: at 5 creatures the spell paid {B}{B}{B} (3)",
            "instead of {1}. The granted Affinity keyword IS attached",
            "to the spell object, but the cost computation ignores it."]:
        d.text((24, y), ln, fill=(200, 210, 225))
        y += 24
    y += 10
    d.text((24, y), "Assertions (from saved states / measurements):",
           fill=(200, 210, 225))
    y += 24
    labels = {
        "A1_setup": "Balancer on BF turn 12; test5 at 5 creatures, "
                    "turn 32",
        "A2_bear_control": "bear #4 (creature spell) tapped exactly "
                           "2 lands (full {1}{G})",
        "A3_five_cost": "Beseech#1 at 5 creatures: paid 3, expect 1 "
                        "(6 generic - 5 affinity)",
        "A4_six_cost": "not-run: 6-creature leg never executed "
                       "(search stall)",
        "A5_resolution": "not-run: Beseech#1 search never completed "
                         "(dangling SearchChoice)",
    }
    for k, lab in labels.items():
        v = run["assertions"].get(k, "?")
        col = {"passed": (120, 220, 120), "failed": (255, 110, 110),
               "not-run": (200, 180, 120)}.get(v, (180, 180, 180))
        d.text((24, y), f"[{v}] {k}: {lab}", fill=col)
        y += 26
    y += 8
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    ds = run["driver_state"]["post_cast5"]
    for ln in [
            f"pre_cast5: 5 creatures, 13 untapped lands (turn 32)",
            f"post_cast5: 5 creatures, 10 untapped lands "
            f"(delta={ds['tapped_delta']}, expect 1)",
            f"spell object: mana_spent_to_cast_amount="
            f"{ds['mana_spent_to_cast_amount']} "
            f"{ds['colors_spent_to_cast']} (3 Swamps tapped)",
            "cast_spell_keywords on the spell: Affinity for",
            "creatures (grant recognized, cost reduction missing)",
            f"bear control: tapped "
            f"{run['driver_state']['bear_control_tapped_delta']} "
            f"(expect 2) - discount correctly absent there",
            "Search stall: Beseech#1 reached GY with no card tutored;",
            "SearchChoice left dangling; A4/A5 not-run (not engine",
            "verdicts on the search).",
    ]:
        d.text((24, y), ln[:108], fill=(160, 175, 195))
        y += 22
    d.text((24, y + 14), "Generated from saved states/assertions; not "
           "a gameplay screenshot.", fill=(110, 125, 145))
    img.save(f"{EVDIR}/summary.png")
    print("wrote summary.png")


def write_manifest():
    files = sorted(
        f for f in os.listdir(EVDIR)
        if os.path.isfile(f"{EVDIR}/{f}") and f != "manifest.sha256")
    lines = []
    for fn in files:
        h = hashlib.sha256()
        with open(f"{EVDIR}/{fn}", "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        lines.append(f"{h.hexdigest()}  {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"wrote manifest.sha256 ({len(lines)} files)")


if __name__ == "__main__":
    main()
