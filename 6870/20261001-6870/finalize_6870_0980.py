#!/usr/bin/env python3
"""Finalize #6870 re-validation run 20261001-6870 (v0.98.0, protocol 94):
recompute assertions from saved states, write run.json, render the summary
PNG, validate, and write the SHA-256 manifest."""
import hashlib
import json
import os
import shutil
import subprocess
import sys

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20261001-6870"
ISSUE = "6870"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"
SCENARIO = "scenario_6870_0980.py"
DATA_DIR = f"{BACKFILL}/server/releases/v0.98.0/data"


def load(p):
    return json.load(open(f"{EVDIR}/{p}"))


def state_of(p):
    return load(p)["state"]


def nm(o):
    return o.get("card_name") or o.get("name") or o.get("base_name") or ""


def bf(state, pid):
    return [(oid, o) for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def find(state, pid, name):
    for oid, o in state["objects"].items():
        if (nm(o) == name and o.get("zone") == "Battlefield"
                and o.get("controller") == pid):
            return str(oid)
    return None


def life_of(state, pid):
    for p in state.get("players", []):
        if p.get("id") == pid:
            return p.get("life")
    return None


def loyalty_of(o):
    v = o.get("loyalty")
    return int(v) if isinstance(v, (int, float)) else None


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                               "PostCombatMain"))


A = {}
notes = []

wire = [json.loads(l) for l in open(f"{EVDIR}/wire_log.jsonl")]
obs = load("observations.json").get("observations", {})
try:
    ids = load("assertions.json").get("ids", {})
except (json.JSONDecodeError, FileNotFoundError):
    ids = {}
if not ids.get("loyalty"):
    for d in wire:
        if d["event"] == "loyalty_on_stack":
            ids["loyalty"] = str(d["payload"]["sid"])
        elif d["event"] == "engine_ability_on_stack":
            ids["engine_ability"] = str(d["payload"]["sid"])
print("recovered ids:", ids)

# reconstruct act from the wire log
act = {"plus1_targeted": None, "engine": None, "engine_targeted": None,
       "retarget_answered": None, "retarget_may_answered": None}
for d in wire:
    ev = d["event"]
    if ev == "plus1_target_prompt":
        act["plus1_targeted"] = {"ref": "1"}
    elif ev == "engine_option_offered":
        act["engine"] = True
    elif ev == "engine_autotarget":
        act["engine_targeted"] = {"auto": True,
                                  "ref": d["payload"]["loyalty_sid"]}
    elif ev == "copy_retarget_prompt":
        act["retarget_answered"] = True

# ---- A1: setup
pre = state_of("pre_activate.json")
eng = find(pre, 0, "Lithoform Engine")
ch = find(pre, 0, "Chandra Nalaar")
eng_untapped = eng and not pre["objects"][eng].get("tapped")
A["A1_setup_ok"] = ("passed" if eng_untapped and ch and is_my_main(pre, 0)
                    else "failed")
notes.append(f"A1: engine_untapped={bool(eng_untapped)} chandra={bool(ch)} "
             f"p0_main={is_my_main(pre, 0)} turn={pre.get('turn_number')}")

# ---- A2: loyalty ability on the stack
loy_entry = None
for e in (state_of("mid_loyalty.json").get("stack") or []):
    if str(e.get("id")) == str(ids.get("loyalty")):
        loy_entry = e
        break
loy_ok = False
if loy_entry:
    ab = ((loy_entry.get("kind") or {}).get("data") or {}).get("ability") or {}
    loy_ok = ((loy_entry.get("kind") or {}).get("type") == "ActivatedAbility"
              and "deals 1 damage" in (ab.get("description") or "")
              and ab.get("targets") == [{"Player": 1}])
A["A2_loyalty_on_stack"] = "passed" if loy_ok else "failed"
notes.append(f"A2: loyalty_sid={ids.get('loyalty')} on_stack={loy_entry is not None} "
             f"signature_ok={loy_ok}")

# ---- A3: engine targeted the loyalty ability
tgt = act.get("engine_targeted")
eng_entry = None
for line in wire:
    if line["event"] == "engine_ability_on_stack":
        eng_entry = line["payload"]["entry"]
        break
targets = (((eng_entry.get("kind") or {}).get("data") or {})
           .get("ability") or {}).get("targets") if eng_entry else None
rejs = [d for d in wire if d["event"] == "rejected"]
tgt_ok = (tgt is not None
          and targets == [{"Object": int(ids["loyalty"])}]
          if ids.get("loyalty") else tgt is not None)
A["A3_engine_targets_loyalty"] = "passed" if tgt_ok else "failed"
notes.append(f"A3: engine_targeted={tgt} engine_stack_targets={targets} "
             f"rejections={len(rejs)}")

# ---- A4: copy damage
post = state_of("post_activate.json")
lb, la = life_of(pre, 1), life_of(post, 1)
ch_post = find(post, 0, "Chandra Nalaar")
loy = loyalty_of(post["objects"][ch_post]) if ch_post else None
dmg = (lb - la) if lb is not None and la is not None else None
A["A4_copy_damage"] = "passed" if dmg == 2 and loy == 7 else "failed"
notes.append(f"A4: P1 life {lb}->{la} (dmg={dmg}, expected 2 = original 1 + "
             f"copy 1); chandra loyalty={loy} (expected 7)")

# retarget observation (not a gating assertion)
rt = obs.get("retarget_answered")
notes.append(f"retarget: CopyRetarget prompt offered and answered: {rt} "
             f"(may-choose form: {obs.get('retarget_may_answered')}); "
             f"copy ids seen on stack: {obs.get('copy_seen')}")

# ---- A5: cleanup
final = state_of("post.json")
stack_empty = not (final.get("stack") or [])
game_over = ((final.get("waiting_for") or {}).get("type") == "GameOver")
A["A5_cleanup"] = "passed" if stack_empty and not game_over else "failed"
notes.append(f"A5: stack_empty={stack_empty} game_over={game_over}")

verdict = ("not-reproduced" if all(A[k] == "passed" for k in
           ["A1_setup_ok", "A2_loyalty_on_stack",
            "A3_engine_targets_loyalty", "A4_copy_damage"])
           else ("reproduced" if A["A1_setup_ok"] == "passed"
                 and A["A2_loyalty_on_stack"] == "passed"
                 and A["A3_engine_targets_loyalty"] == "failed"
                 else "blocked"))
for k in sorted(A):
    print(f"{k}: {A[k]}")
print("verdict:", verdict)

with open(f"{EVDIR}/assertions.json", "w") as f:
    json.dump({"assertions": A, "notes": notes,
               "observations": obs, "ids": ids,
               "act": act}, f, indent=2)

# supporting files
shutil.copy(f"{EVDIR}/pre_activate.json", f"{EVDIR}/pre.json")
shutil.copy("/tmp/6870_0980_stdout.log", f"{EVDIR}/driver_stdout.log")
shutil.copy(f"{BACKFILL}/driver/{SCENARIO}", f"{EVDIR}/{SCENARIO}")
shutil.copy(f"{BACKFILL}/driver/finalize_6870_0980.py",
            f"{EVDIR}/finalize_6870_0980.py")

# card-data excerpt: the copy ability is fully parsed
data = json.load(open(f"{DATA_DIR}/card-data.json"))
le = data.get("lithoform engine", {})
ab0 = (le.get("abilities") or [{}])[0]
excerpt = {
    "card": le.get("name"),
    "oracle_text": le.get("oracle_text"),
    "ability0_description": ab0.get("description"),
    "ability0_effect_type": (ab0.get("effect") or {}).get("type"),
    "ability0_target": (ab0.get("effect") or {}).get("target"),
    "ability0_retarget": (ab0.get("effect") or {}).get("retarget"),
}
with open(f"{EVDIR}/carddata_excerpt.json", "w") as f:
    json.dump(excerpt, f, indent=2)
print("carddata excerpt:", excerpt["ability0_effect_type"],
      excerpt["ability0_retarget"])

scenario_bytes = open(f"{BACKFILL}/driver/{SCENARIO}", "rb").read()
run_doc = {
    "issue": 6870,
    "run_id": RUN_ID,
    "started_at": "2026-10-01T11:45:00-05:00",
    "server": {
        "server_version": "v0.98.0",
        "build_commit": "61e8550",
        "protocol_version": 94,
        "server_binary_sha256": "15c50bbd3e90b9af49c851d9a56a775f4d74f5874f19e5ea231520816c8170a9",
        "card_data_sha256": "1a5919f2a50754c7f5e48922390816150a20b703821114adfe08427ff0b11960",
        "draft_pools_sha256": "bf3316202d84068ac38bcec48c5fc57d38d7834aef5f18c6b410ba9f64afd594",
        "signature_key_id": "436711b6a2d36828",
        "signature_verified": True,
    },
    "driver": {"protocol_version": 94, "client": "driver/client.py",
               "scenario": f"driver/{SCENARIO}"},
    "scenario_sha256": hashlib.sha256(scenario_bytes).hexdigest(),
    "decks": {
        "P0": [["Lithoform Engine", 8], ["Chandra Nalaar", 8],
               ["Mountain", 44]],
        "P1": [["Mountain", 60]],
    },
    "setup_line": "P0: Lithoform Engine + Chandra Nalaar; proof on a P0 "
                  "main phase after both clear summoning sickness",
    "contract_line": "Chandra +1 targets P1 (loyalty ability on stack, "
                     "targets=[{Player:1}]); Lithoform Engine {2},{T} must "
                     "accept the loyalty ability as its target "
                     "(StackAbility/You); the copy is created and its "
                     "may-choose-new-targets prompt answered (P1); both the "
                     "copy and the original resolve for 1 damage each "
                     "(P1 20->18), Chandra 6->7 loyalty",
    "assertions": A,
    "notes": notes + [
        "Re-validation of the 2026-09-11 v0.80.0/protocol-69 run (verdict "
        "not-reproduced) on the pinned v0.98.0 (protocol 94); the maintained "
        "evidence comment already publishes the v0.80.0 result, so this run "
        "adds a v0.98.0 section rather than creating a new comment.",
    ],
    "limitations": [
        "Browser UI not exercised; native engine via two human-client seats.",
        "Dense playsets (8x) are a test-harness convenience (engine accepts "
        ">4-of for custom games).",
        "Only Chandra Nalaar's +1 (targeted damage) exercised as the "
        "loyalty ability; triggered abilities and other planeswalkers not "
        "tested.",
        "The copy's new-target choice was answered P1 (same as original); "
        "choosing a different new target was not exercised.",
        "Not tested on the original 2026-08-02 report build; verdict is "
        "scoped to v0.98.0, not a fix claim.",
        "The prebuilt server has no standalone state import; states are "
        "authoritative exports (restorable only via full game replay).",
    ],
    "verdict": verdict,
    "stats": {
        "states_seen": len([f for f in os.listdir(EVDIR)
                            if f.endswith(".json")
                            and f not in ("run.json", "assertions.json",
                                          "observations.json",
                                          "carddata_excerpt.json")]),
        "trigger_observations": 0,
        "wire_events": len(wire),
        "rejections": len([d for d in wire
                           if d["event"] == "rejected"]),
    },
}
with open(f"{EVDIR}/run.json", "w") as f:
    json.dump(run_doc, f, indent=2)

# PNG via the generic renderer (reads run.json + pre.json + post.json)
title = ("Lithoform Engine copies Chandra +1 loyalty ability (v0.98.0 "
         "re-validation)")
r = subprocess.run([sys.executable, f"{BACKFILL}/driver/render_summary.py",
                    EVDIR, ISSUE, title], capture_output=True, text=True)
print(r.stdout, r.stderr)
assert os.path.getsize(f"{EVDIR}/summary.png") > 0

# validate
from PIL import Image
for fn in sorted(os.listdir(EVDIR)):
    p = f"{EVDIR}/{fn}"
    if fn == "manifest.sha256":
        continue
    if fn.endswith(".json"):
        json.load(open(p))
        print("json ok:", fn)
    elif fn.endswith(".jsonl"):
        for l in open(p):
            json.loads(l)
        print("jsonl ok:", fn, os.path.getsize(p))
    else:
        assert os.path.getsize(p) > 0, fn
        print("file ok:", fn, os.path.getsize(p))
img = Image.open(f"{EVDIR}/summary.png")
img.verify()
print("png ok:", os.path.getsize(f"{EVDIR}/summary.png"))

# manifest
lines = []
for fn in sorted(os.listdir(EVDIR)):
    if fn == "manifest.sha256":
        continue
    p = f"{EVDIR}/{fn}"
    if not os.path.isfile(p):
        continue
    h = hashlib.sha256(open(p, "rb").read()).hexdigest()
    lines.append(f"{h}  {fn}\n")
with open(f"{EVDIR}/manifest.sha256", "w") as f:
    f.writelines(lines)
print("manifest written:", len(lines), "files")
print("FINAL VERDICT:", verdict)
