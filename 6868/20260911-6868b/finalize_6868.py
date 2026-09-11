#!/usr/bin/env python3
"""Finalize #6868 evidence: recompute assertions from saved states, write
run.json, render the summary PNG, validate, and write the SHA-256 manifest."""
import hashlib
import json
import os
import shutil
import subprocess
import sys

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260911-6868b"
ISSUE = "6868"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"


def load(p):
    return json.load(open(f"{EVDIR}/{p}"))


def state_of(p):
    return load(p)["state"]


def nm(o):
    return o.get("card_name") or o.get("name") or o.get("base_name") or ""


def bf(state, pid):
    return [(oid, o) for oid, o in state["objects"].items()
            if o.get("zone") == "Battlefield" and o.get("controller") == pid]


def loyalty_of(o):
    v = o.get("loyalty")
    return int(v) if isinstance(v, (int, float)) else None


def find(state, pid, name):
    for oid, o in state["objects"].items():
        if (nm(o) == name and o.get("zone") == "Battlefield"
                and o.get("controller") == pid):
            return str(oid)
    return None


A = {}
notes = []

wire = [json.loads(l) for l in open(f"{EVDIR}/wire_log.jsonl")]
t_minus3 = next((d["t"] for d in wire if d["event"] == "jared_minus3"), None)
subs = [d for d in wire
        if d["event"] == "interaction_submit"
        and (d["payload"].get("stage") == "MINUS3")]
rej_after = [d for d in wire
             if d["event"] == "rejected"
             and t_minus3 is not None and d["t"] >= t_minus3]

pre = state_of("pre_activate.json")
ko_pre = pre["objects"].get(find(pre, 0, "Kavu") or "", {})
colors = ko_pre.get("colors") or ko_pre.get("color") or []
A["A1_setup_ok"] = ("passed"
                    if find(pre, 0, "Jared Carthalion")
                    and find(pre, 0, "Kavu")
                    and len(colors) == 5
                    and find(pre, 1, "Grizzly Bears")
                    else "failed")
notes.append(f"A1: Jared={bool(find(pre,0,'Jared Carthalion'))} "
             f"Kavu={find(pre,0,'Kavu')} colors={colors} "
             f"P1 Bears={find(pre,1,'Grizzly Bears')}")

offered = any(d["event"] == "minus3_options"
              and d["payload"].get("count", 0) >= 1 for d in wire)
A["A2_minus3_offered"] = "passed" if offered else "failed"
notes.append(f"A2: -3 ActivateAbility offered={offered}")

obs = load("observations.json")["observations"]
answered = obs.get("minus3_answered", [])
post = state_of("post_activate.json")
zid = find(post, 0, "Jared Carthalion")
loy = loyalty_of(post["objects"][zid]) if zid else None
stack_empty = not any(o.get("zone") == "Stack"
                      for o in post["objects"].values())
A["A3_target_resolve"] = ("passed"
                          if len(answered) == 2 and loy == 3
                          and stack_empty and not rej_after
                          else "failed")
notes.append(f"A3: targets_answered={len(answered)}/2 "
             f"submissions={len(subs)} rejections_after_minus3={len(rej_after)} "
             f"loyalty={loy} stack_empty={stack_empty}")

ko = post["objects"].get(find(post, 0, "Kavu") or "", {})
k_counters = ko.get("counters") or {}
A["A4_kavu_counters"] = ("passed"
                         if ko.get("power") == 8 and ko.get("toughness") == 8
                         else "failed")
notes.append(f"A4: Kavu counters_field={k_counters} power={ko.get('power')} "
             f"toughness={ko.get('toughness')} (expected 8/8, five +1/+1)")

bo = post["objects"].get(find(post, 1, "Grizzly Bears") or "", {})
b_counters = bo.get("counters") or {}
A["A5_bears_counters"] = ("passed"
                          if bo.get("power") == 3 and bo.get("toughness") == 3
                          else "failed")
notes.append(f"A5: P1 Bears counters_field={b_counters} "
             f"power={bo.get('power')} toughness={bo.get('toughness')} "
             f"(expected 3/3, one +1/+1)")

final = state_of("post.json")
stack_empty_f = not any(o.get("zone") == "Stack"
                        for o in final["objects"].values())
game_over = (final.get("waiting_for") or {}).get("type") == "GameOver"
A["A6_cleanup"] = ("passed" if stack_empty_f and not game_over else "failed")
notes.append(f"A6: stack_empty={stack_empty_f} game_over={game_over}")

verdict = ("reproduced"
           if A["A3_target_resolve"] == "passed"
           and A["A4_kavu_counters"] == "failed"
           and A["A5_bears_counters"] == "failed"
           else ("not-reproduced"
                 if A["A4_kavu_counters"] == "passed"
                 and A["A5_bears_counters"] == "passed"
                 else "blocked"))
for k in sorted(A):
    print(f"{k}: {A[k]}")
print("verdict:", verdict)

with open(f"{EVDIR}/assertions.json", "w") as f:
    json.dump({"assertions": A, "notes": notes,
               "targets": obs.get("minus3_answered")}, f, indent=2)

# renderer inputs
shutil.copy(f"{EVDIR}/pre_activate.json", f"{EVDIR}/pre.json")
shutil.copy("/tmp/6868b_stdout.log", f"{EVDIR}/driver_stdout.log")
shutil.copy(f"{BACKFILL}/driver/scenario_6868.py",
            f"{EVDIR}/scenario_6868.py")
shutil.copy(f"{BACKFILL}/driver/finalize_6868.py",
            f"{EVDIR}/finalize_6868.py")

# card-data excerpt: the -3's counter child is Unimplemented
data = json.load(open(f"{BACKFILL}/server/releases/v0.80.0/data/"
                      "card-data.json"))
jared = data.get("jared carthalion", {})
minus3 = (jared.get("abilities") or [None, None])[1] or {}
excerpt = {
    "card": jared.get("name"),
    "oracle_text": jared.get("oracle_text"),
    "minus3_effect_type": (minus3.get("effect") or {}).get("type"),
    "minus3_multi_target": minus3.get("multi_target"),
    "minus3_counter_child": (minus3.get("sub_ability") or {}).get("effect"),
}
with open(f"{EVDIR}/carddata_excerpt.json", "w") as f:
    json.dump(excerpt, f, indent=2)
print("carddata_excerpt:", minus3.get("multi_target"),
      (minus3.get("sub_ability") or {}).get("effect", {}).get("type"))

scenario_bytes = open(f"{BACKFILL}/driver/scenario_6868.py", "rb").read()
run_doc = {
    "issue": 6868,
    "run_id": RUN_ID,
    "started_at": "2026-09-11T20:30:00Z",
    "server": {
        "server_version": "v0.80.0",
        "build_commit": "22cca6d",
        "protocol_version": 69,
        "mode": "Full",
        "binary_sha256": "1d414c0e999a088560ab9ad0d77a4ae4f5773610cee6afda616a62c0e654238e",
        "card_data_sha256": "7ce6f92d0adb8fc4158bf0ab76797a644eb77dcea01f9743bb849550bb677bfd",
        "draft_pools_sha256": "c78dbd16f671e5b21ec094d6fcbc2b5da76e79cc82c2d9daa914369180021348",
        "signature_key_id": "436711b6a2d36828",
        "signature_verified": True,
        "observed_at": "2026-09-11",
        "source": "ServerHello + minisign verification against repo-pinned key",
    },
    "driver": {"protocol_version": 69, "client": "driver/client.py",
               "scenario": "driver/scenario_6868.py"},
    "scenario_sha256": hashlib.sha256(scenario_bytes).hexdigest(),
    "decks": {
        "P0": [["Jared Carthalion", 8], ["Grizzly Bears", 12],
               ["Plains", 8], ["Island", 8], ["Swamp", 8],
               ["Mountain", 8], ["Forest", 8]],
        "P1": [["Grizzly Bears", 12], ["Plains", 12], ["Island", 12],
               ["Swamp", 12], ["Mountain", 12], ["Forest", 12]],
    },
    "setup_line": "P0: Jared Carthalion +1 on turn of casting (loyalty 6), "
                  "3/3 all-colors Kavu token created; P1: Grizzly Bears on BF; "
                  "-3 on next P0 turn",
    "contract_line": "-3 must accept two creature targets, then put counters "
                     "equal to each target's own color count: 5 on the "
                     "all-colors Kavu, 1 on the green Bears; evaluated "
                     "independently at resolution",
    "assertions": A,
    "notes": notes + [
        "The -3's target selection is offered as sequential per-target "
        "prompts (spec max=1 per prompt); the driver answered Kavu first, "
        "then P1 Bears. A first attempt submitted both choiceIds in one "
        "sequence response and was correctly rejected "
        "interaction_constraint_unsatisfied (see wire_log.jsonl of the "
        "superseded run 20260911-6868) -- engine behavior, not a defect.",
        "Post-resolution: Jared loyalty 6->3, stack empty, game proceeds; "
        "Kavu stayed 3/3 with empty counters field, Bears stayed 2/2. "
        "Card data marks the -3's counter child Unimplemented "
        "(see carddata_excerpt.json), matching the observed no-counters "
        "outcome.",
    ],
    "limitations": [
        "Browser UI not exercised; native engine via two human-client seats.",
        "Dense playsets are a test-harness convenience (engine accepts >4-of "
        "for custom games).",
        "The prebuilt server has no standalone state import; states are "
        "authoritative exports (restorable only via full game replay).",
        "Only the -3 loyalty ability was exercised (+1 used for setup, -6 "
        "untouched).",
        "The two early mulligan-phase rejections (wrong_player / "
        "invalid_action) are driver races during mulligan, unrelated to the "
        "-3; zero rejections after the -3 activation.",
    ],
    "stats": {"targets_answered": len(answered),
              "submissions": len(subs)},
    "verdict": verdict,
}
with open(f"{EVDIR}/run.json", "w") as f:
    json.dump(run_doc, f, indent=2)

# PNG
title = ("Jared Carthalion -3: targets accepted (Kavu + Bears), ability "
         "resolves, but zero +1/+1 counters placed on either target")
r = subprocess.run([sys.executable, f"{BACKFILL}/driver/render_summary.py",
                    EVDIR, ISSUE, title], capture_output=True, text=True)
print(r.stdout, r.stderr)
assert os.path.getsize(f"{EVDIR}/summary.png") > 0

# validate
from PIL import Image
for fn in ["pre_activate.json", "post_activate.json", "pre.json",
           "post.json", "run.json", "assertions.json",
           "observations.json", "carddata_excerpt.json", "wire_log.jsonl",
           "scenario_run.log", "driver_stdout.log", "scenario_6868.py",
           "finalize_6868.py"]:
    p = f"{EVDIR}/{fn}"
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
