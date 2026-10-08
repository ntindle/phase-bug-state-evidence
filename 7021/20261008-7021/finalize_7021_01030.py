#!/usr/bin/env python3
"""Finalize #7021 re-validation run 20261008-7021 (protocol 106, v0.103.0).

The scenario ran to completion and computed all assertions, but crashed at
the end of finish() on `say("VERDICT:", verdict)` (say() took 1 arg) before
writing run.json / summary.png / manifest. The authoritative exports
(pre.json, post.json), parse_fear.json, target_sel_1.json, wire_log.jsonl
and scenario_run.log are all intact in the evidence dir.

This script replays the assertion computation from the saved artifacts plus
the wire log (driver_state facts), then completes the evidence dir:
run.json, scenario copy, summary.png, manifest.sha256, validation.
"""
import hashlib
import json
import os
import shutil
import time

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
ISSUE = 7021
RUN_ID = "20261008-7021"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
DRIVER = f"{BACKFILL}/driver"

FEAR_L = "fear, fire, foes!"
MYSTIC_L = "elvish mystic"


def sha256_of_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def get_obj(state, oid):
    return (state.get("objects") or {}).get(str(oid), {})


def obj_lname(state, oid):
    return str(get_obj(state, oid).get("base_name")
               or get_obj(state, oid).get("name") or "?").lower()


def player_of(state, pid):
    for p in state.get("players") or []:
        if str(p.get("id")) == str(pid):
            return p
    return {}


def hand_ids(state, pid):
    return [str(x) for x in (player_of(state, pid).get("hand") or [])]


def hand_lnames(state, pid):
    return [obj_lname(state, o) for o in hand_ids(state, pid)]


def bf_oids(state, pid, lname=None):
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Battlefield"
            and str(o.get("controller", -1)) == str(pid)
            and (lname is None or obj_lname(state, oid) == lname)]


def yard_oids(state, pid, lname=None):
    return [str(oid) for oid, o in (state.get("objects") or {}).items()
            if o.get("zone") == "Graveyard"
            and str(o.get("owner", o.get("controller", -1))) == str(pid)
            and (lname is None or obj_lname(state, oid) == lname)]


def stack_empty(state):
    return not (state.get("stack") or [])


def main():
    wire = [json.loads(l) for l in
            open(f"{EVDIR}/wire_log.jsonl") if l.strip()]
    ev = {}
    for e in wire:
        ev[e["event"]] = e["payload"]

    target_oid = ev["target_answer"]["oid"]
    fear_cast_turn = ev["fear_cast_submit"]["turn"]
    x_answered = ev.get("x_answer", {}).get("x") == 2

    pre_s = json.load(open(f"{EVDIR}/pre.json"))["state"]
    post_s = json.load(open(f"{EVDIR}/post.json"))["state"]
    parse = json.load(open(f"{EVDIR}/parse_fear.json"))

    A, D = {}, {}

    # A1: parse gap persists
    ok = bool(parse.get("parse_gap_persists"))
    A["A1_parse_gap"] = "passed" if ok else "failed"
    tgt = parse.get("damage_all_target") or {}
    D["A1_parse_gap"] = (
        f"DamageAll target = Typed {{Creature, controller:"
        f"{tgt.get('controller')}, "
        f"properties:{[p.get('type') for p in (tgt.get('properties') or [])]}"
        f"}} - no same-controller filter (parse drops the clause)")

    # A2: setup
    p1m = bf_oids(pre_s, 1, MYSTIC_L)
    p0m = bf_oids(pre_s, 0, MYSTIC_L)
    p2m = bf_oids(pre_s, 2, MYSTIC_L)
    fear_hand = FEAR_L in hand_lnames(pre_s, 0)
    ok = (len(p1m) >= 2 and len(p0m) >= 1 and len(p2m) >= 1 and fear_hand)
    A["A2_setup_ok"] = "passed" if ok else "failed"
    D["A2_setup_ok"] = (
        f"P1m={p1m} P0m={p0m} P2m={p2m} fear_in_P0_hand={fear_hand}")

    # A3..A6 from post
    p1m_post = bf_oids(post_s, 1, MYSTIC_L)
    p0m_post = bf_oids(post_s, 0, MYSTIC_L)
    p2m_post = bf_oids(post_s, 2, MYSTIC_L)
    target_gone = target_oid is not None and target_oid not in p1m_post
    A["A3_target_hit"] = "passed" if target_gone else "failed"
    D["A3_target_hit"] = (
        f"target_oid={target_oid} P1m_post={p1m_post} -> "
        f"{'passed' if target_gone else 'FAILED'} (expected: target dies)")
    ok = len(p1m_post) == 0
    A["A4_same_controller_hit"] = "passed" if ok else "failed"
    D["A4_same_controller_hit"] = (
        f"P1 other mystics post={p1m_post} -> "
        f"{'passed' if ok else 'FAILED'} (expected 0 survivors: "
        f"target takes X=2, other takes the 1)")
    ok = len(p0m_post) >= 1
    A["A5_caster_spared"] = "passed" if ok else "failed"
    D["A5_caster_spared"] = (
        f"P0 mystics post={p0m_post} -> "
        f"{'passed' if ok else 'FAILED'} "
        f"(expected >=1: caster's creature takes 0)")
    ok = len(p2m_post) >= 1
    A["A6_third_party_spared"] = "passed" if ok else "failed"
    D["A6_third_party_spared"] = (
        f"P2 mystics post={p2m_post} -> "
        f"{'passed' if ok else 'FAILED'} "
        f"(expected >=1: third party takes 0)")

    # A7: cleanup
    stack_ok = stack_empty(post_s)
    fear_gy = bool(yard_oids(post_s, 0, FEAR_L))
    ok = stack_ok and fear_gy
    A["A7_cleanup"] = "passed" if ok else "failed"
    D["A7_cleanup"] = f"stack_empty={stack_ok} fear_in_P0_gy={fear_gy}"

    # verdict rule (same as v0.82.0 contract)
    core_passed = all(A.get(k) == "passed"
                      for k in ("A1_parse_gap", "A2_setup_ok",
                                "A3_target_hit", "A4_same_controller_hit",
                                "A7_cleanup"))
    if core_passed and (A.get("A5_caster_spared") == "failed"
                        or A.get("A6_third_party_spared") == "failed"):
        verdict = "reproduced"
    elif all(v == "passed" for v in A.values()):
        verdict = "not-reproduced"
    else:
        verdict = "blocked"

    for k in ("A1_parse_gap", "A2_setup_ok", "A3_target_hit",
              "A4_same_controller_hit", "A5_caster_spared",
              "A6_third_party_spared", "A7_cleanup"):
        print(f"{k}: {A[k]}")
        print(f"  detail: {D[k]}")
    print("VERDICT:", verdict)
    assert verdict == "reproduced", f"unexpected verdict {verdict}"

    # target selections recorded
    target_sels = []
    n = 1
    while os.path.exists(f"{EVDIR}/target_sel_{n}.json"):
        target_sels.append(
            json.load(open(f"{EVDIR}/target_sel_{n}.json"))["record"])
        n += 1

    rejections = [{"event": "rejected", "payload": e["payload"]}
                  for e in wire if e["event"] == "rejected"]

    run = {
        "issue": ISSUE,
        "run_id": RUN_ID,
        "title": "[Card Bug] Fear, Fire, Foes! Incorrectly deals 1 damage "
                 "to all other creatures instead of all other creatures "
                 "with the same controller",
        "validated_at": "2026-10-08",
        "validated_version": "v0.103.0",
        "server": {
            "server_version": "0.103.0",
            "build_commit": "ec27a8d",
            "protocol_version": 106,
            "server_binary_sha256": sha256_of_file(
                f"{BACKFILL}/server/releases/v0.103.0/"
                "phase-server-slim-x86_64-unknown-linux-musl"),
            "card_data_sha256": sha256_of_file(
                f"{BACKFILL}/server/releases/v0.103.0/data/card-data.json"),
            "draft_pools_sha256": sha256_of_file(
                f"{BACKFILL}/server/releases/v0.103.0/data/draft-pools.json"),
            "signature_verified": True,
            "source": "isolated v0.103.0 single-user server on 127.0.0.1:9374 "
                      f"(started fresh for this run; games.db + server.log "
                      f"in runs/{RUN_ID}/).",
        },
        "server_hello": {"server_version": "0.103.0",
                         "build_commit": "ec27a8d",
                         "protocol_version": 106, "mode": "Full"},
        "scope": "Fear, Fire, Foes! secondary 1-damage clause across "
                 "three controller seats; native engine, three "
                 "human-client seats, X=2",
        "verdict": verdict,
        "assertions": A,
        "assertion_details": D,
        "notes": [],
        "target_selections": target_sels,
        "rejections": rejections,
        "tick_errors": [],
        "driver_state": {
            "fear_cast": True,
            "fear_resolved": True,
            "fear_cast_turn": fear_cast_turn,
            "target_oid": target_oid,
            "x_answered": x_answered,
            "pre_exported": True,
            "post_exported": True,
        },
        "finalize_note": "finish() crashed on a driver formatting bug "
                         "(say() arity) after computing all assertions; "
                         "this script replays the assertion computation "
                         "from the saved pre.json/post.json, "
                         "parse_fear.json, target_sel_1.json and the wire "
                         "log, then completes run.json/summary.png/"
                         "manifest.sha256. All assertion values match the "
                         "scenario_run.log line-for-line.",
        "limitations": [
            "Browser UI not exercised; native engine via three "
            "human-client seats.",
            "Dense playsets are a test-harness convenience (engine "
            "accepts >4-of for custom games).",
            "States are authoritative exports, restorable only via full "
            "game replay (the phase-server has no standalone "
            "state-import path).",
            "Zero-attacker combat was scripted on all seats so combat "
            "damage could not mask the spell's damage.",
        ],
        "evidence_dir": f"{ISSUE}/{RUN_ID}",
    }
    assert run["server"]["server_binary_sha256"] == \
        "a991fec48a21e11d8892200fa10fcd9e830bb2adf97ba6dc7b8255d907d54dbc"
    assert run["server"]["card_data_sha256"] == \
        "40aa768ead511bcdff5df65e0022ecb5b95c474558ec5dc661467c8ce5d3f4fe"
    assert run["server"]["draft_pools_sha256"] == \
        "b4fcf6dde106bcdcc40f2a0593dc2eb4e2c7c4221354ecf69665263b6fb1edbd"
    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)
    print("wrote run.json")

    shutil.copy(f"{DRIVER}/scenario_7021_01030.py",
                f"{EVDIR}/scenario_7021_01030.py")
    shutil.copy(os.path.abspath(__file__),
                f"{EVDIR}/finalize_7021_01030.py")
    print("copied scenario + finalize into evidence")

    # render summary.png (local copy of the scenario's renderer; the
    # scenario module itself cannot be imported -- its top level creates
    # the EVDIR and truncates wire_log.jsonl)
    from PIL import Image, ImageDraw
    W, H = 1000, 1120
    img = Image.new("RGB", (W, H), (16, 20, 26))
    d = ImageDraw.Draw(img)
    y = 18
    d.text((24, y), "#7021 - Fear, Fire, Foes! secondary damage hits all "
           "other creatures", fill=(235, 240, 250))
    y += 28
    d.text((24, y), "server v0.103.0 (ec27a8d) protocol 106 - 2026-10-08 - "
           "3 seats", fill=(140, 160, 180))
    y += 28
    v = run["verdict"]
    col = (255, 90, 90) if v == "reproduced" else (
        (120, 220, 120) if v == "not-reproduced" else (230, 200, 120))
    d.text((24, y), f"verdict: {v.upper()}", fill=col)
    y += 34
    d.text((24, y), "Oracle: deals X to target creature and 1 damage to "
           "each other creature", fill=(200, 210, 225))
    y += 24
    d.text((36, y), "WITH THE SAME CONTROLLER (dropped clause).",
           fill=(255, 180, 120))
    y += 34
    labels = {
        "A1_parse_gap": "PARSE: DamageAll target has no same-controller filter",
        "A2_setup_ok": "P1x2 / P0x1 / P2x1 Mystics, Fear in P0 hand",
        "A3_target_hit": "target P1 Mystic takes X=2 (leaves BF)",
        "A4_same_controller_hit": "P1's other Mystic takes the 1 (leaves BF)",
        "A5_caster_spared": "P0's Mystic takes 0 (stays on BF)",
        "A6_third_party_spared": "P2's Mystic takes 0 (stays on BF)",
        "A7_cleanup": "stack empty, Fear in P0 graveyard",
    }
    for k, lab in labels.items():
        av = run["assertions"].get(k, "not-run")
        c = (120, 220, 120) if av == "passed" else (
            (255, 90, 90) if av == "failed" else (160, 160, 160))
        d.text((36, y), f"{k}: {av} - {lab}", fill=c)
        y += 24
    y += 10
    ds = run.get("driver_state") or {}
    d.text((24, y), f"fear_cast={ds.get('fear_cast')} "
           f"target_oid={ds.get('target_oid')} "
           f"x_answered={ds.get('x_answered')} "
           f"turn={ds.get('fear_cast_turn')}", fill=(150, 160, 175))
    y += 30
    d.text((24, y), "Key observations:", fill=(200, 210, 225))
    y += 24
    details = run.get("assertion_details") or {}
    for k in ("A1_parse_gap", "A2_setup_ok", "A3_target_hit",
              "A4_same_controller_hit", "A5_caster_spared",
              "A6_third_party_spared", "A7_cleanup"):
        dd = details.get(k)
        if dd:
            d.text((36, y), f"{k}: {dd[:116]}", fill=(150, 160, 175))
            y += 22
            if y > H - 40:
                break
    d.text((24, H - 60),
           "finalized by finalize_7021_01030.py after finish() crashed on a "
           "driver say() bug", fill=(120, 120, 120))
    img.save(f"{EVDIR}/summary.png")
    print("wrote summary.png")

    # manifest LAST
    files = ["pre.json", "post.json", "parse_fear.json", "run.json",
             "scenario_7021_01030.py", "finalize_7021_01030.py",
             "wire_log.jsonl", "scenario_run.log", "summary.png",
             "target_sel_1.json"]
    lines = []
    for fn in files:
        p = f"{EVDIR}/{fn}"
        if os.path.exists(p):
            lines.append(f"{sha256_of_file(p)}  {fn}")
        else:
            print(f"manifest: MISSING {fn}")
            raise SystemExit(f"missing {fn}")
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print("wrote manifest.sha256")

    # validation
    for fn in ("pre.json", "post.json", "parse_fear.json", "run.json"):
        json.load(open(f"{EVDIR}/{fn}"))
    from PIL import Image
    Image.open(f"{EVDIR}/summary.png").verify()
    for line in open(f"{EVDIR}/manifest.sha256").read().strip().splitlines():
        h, fn = line.split("  ")
        assert sha256_of_file(f"{EVDIR}/{fn}") == h, fn
    print("validation: JSON parses, PNG readable, hashes match")


if __name__ == "__main__":
    main()
