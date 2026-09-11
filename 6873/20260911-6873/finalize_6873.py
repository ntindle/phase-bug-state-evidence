#!/usr/bin/env python3
"""Finalize #6873 evidence: recompute assertions from saved states + wire
log, write assertions.json/run.json, render summary PNG, validate, and
write the SHA-256 manifest."""
import hashlib
import json
import os
import shutil
import subprocess
import sys

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
RUN_ID = "20260911-6873"
ISSUE = "6873"
EVDIR = f"{BACKFILL}/evidence/{ISSUE}/{RUN_ID}"

VANQ = "Wren's Run Vanquisher"
ELF = "Llanowar Elves"
FOREST = "Forest"


def load(p):
    return json.load(open(f"{EVDIR}/{p}"))


def state_of(p):
    return load(p)["state"]


def nm(o):
    return o.get("card_name") or o.get("name") or o.get("base_name") or ""


def is_my_main(state, pid):
    return (state.get("active_player") == pid
            and (state.get("phase") or "") in ("PreCombatMain",
                                               "PostCombatMain"))


def hand_names(state, pid):
    return [nm(o) for o in state["objects"].values()
            if o.get("zone") == "Hand" and o.get("controller") == pid]


def untapped_forests(state, pid):
    return sum(1 for o in state["objects"].values()
               if nm(o) == FOREST and o.get("zone") == "Battlefield"
               and o.get("controller") == pid and not o.get("tapped"))


def tapped_forests(state, pid):
    return sum(1 for o in state["objects"].values()
               if nm(o) == FOREST and o.get("zone") == "Battlefield"
               and o.get("controller") == pid and o.get("tapped"))


def vanq_on_bf(state, pid):
    return [o for o in state["objects"].values()
            if nm(o) == VANQ and o.get("zone") == "Battlefield"
            and o.get("controller") == pid]


A = {}
notes = []

wire = [json.loads(l) for l in open(f"{EVDIR}/wire_log.jsonl")]
cost_prompts = [d for d in wire if d["event"] == "cost_prompt"]
rejections = [d for d in wire if d["event"] == "rejected"]
submits = [d for d in wire if d["event"] == "interaction_submit"]


def mode_of(d):
    return (d.get("payload") or {}).get("mode")


# ---- A1: setup
try:
    pre_r, pre_p = state_of("pre_reveal.json"), state_of("pre_pay.json")
    hr, hp = hand_names(pre_r, 0), hand_names(pre_p, 0)
    ok_r = (is_my_main(pre_r, 0) and VANQ in hr and ELF in hr
            and untapped_forests(pre_r, 0) >= 3)
    ok_p = (is_my_main(pre_p, 0) and VANQ in hp
            and untapped_forests(pre_p, 0) >= 6)
    A["A1_setup_ok"] = "passed" if (ok_r and ok_p) else "failed"
    notes.append(f"A1: reveal pre: main={is_my_main(pre_r,0)} vanq={VANQ in hr} "
                 f"elf={ELF in hr} untapped_forests={untapped_forests(pre_r,0)}; "
                 f"pay pre: main={is_my_main(pre_p,0)} vanq={VANQ in hp} "
                 f"untapped_forests={untapped_forests(pre_p,0)}")
except Exception as e:
    A["A1_setup_ok"] = "not-run"
    notes.append(f"A1 eval error: {e}")

# ---- A2: cost prompted (OptionalCostChoice in both games; the reveal
# game additionally reaches the PayCost{Reveal} card-selection prompt.
# The pay game's {3} is auto-paid (Auto payment mode) with no separate
# PayCost prompt, which is fine.)
try:
    wf_types = {}
    for d in cost_prompts:
        wf_types.setdefault(mode_of(d), set()).add(
            (d["payload"]["waiting_for"] or {}).get("type"))
    ok = ("OptionalCostChoice" in wf_types.get("reveal", set())
          and "PayCost" in wf_types.get("reveal", set())
          and "OptionalCostChoice" in wf_types.get("pay", set()))
    A["A2_cost_prompted"] = "passed" if ok else "failed"
    notes.append(f"A2: waiting_for types per mode: "
                 f"{ {m: sorted(s) for m, s in wf_types.items()} } "
                 f"(pay game's {{3}} auto-paid, no PayCost prompt needed)")
except Exception as e:
    A["A2_cost_prompted"] = "failed"
    notes.append(f"A2 eval error: {e}")

# ---- A3: reveal answerable (reveal game)
try:
    r_prompts = [d for d in cost_prompts if mode_of(d) == "reveal"]
    paycost = [d for d in r_prompts
               if (d["payload"]["waiting_for"] or {}).get("type") == "PayCost"]
    elf_offered = False
    elf_names = []
    for d in paycost:
        for c in d["payload"]["opportunity"]["response"]["data"]["candidates"]:
            surf = c["surfaces"][1]["data"]
            elf_names.append((surf.get("name"), surf.get("reference")))
            if surf.get("name") == ELF:
                elf_offered = True
    elf_answers = None  # authoritative check is the scenario run log below
    r_rej = [d for d in rejections if mode_of(d) == "reveal"]
    # authoritative: scenario log records the reveal_elf answer
    runlog = open(f"{EVDIR}/scenario_run.log").read()
    answered_elf = "cost answer (reveal): reveal_elf" in runlog
    post_r = state_of("post_reveal.json")
    completed = len(vanq_on_bf(post_r, 0)) == 1
    A["A3_reveal_answerable"] = ("passed" if (elf_offered and answered_elf
                                              and not r_rej and completed)
                                 else "failed")
    notes.append(f"A3: PayCost Reveal candidates offered={elf_names}; "
                 f"elf_offered={elf_offered} elf_answered={answered_elf} "
                 f"reveal-mode rejections={len(r_rej)} cast_completed={completed}")
except Exception as e:
    A["A3_reveal_answerable"] = "failed"
    notes.append(f"A3 eval error: {e}")

# ---- A4: cast completes (both games)
try:
    ok = True
    detail = []
    for m in ("reveal", "pay"):
        post = state_of(f"post_{m}.json")
        v = vanq_on_bf(post, 0)
        stack_empty = not (post.get("stack") or [])
        proceeding = (post.get("waiting_for") or {}).get("type") in (
            "Priority", None)
        pt = (v[0].get("power"), v[0].get("toughness")) if v else None
        good = len(v) == 1 and stack_empty and proceeding
        ok = ok and good
        detail.append(f"{m}: vanq_bf={len(v)} {pt} stack_empty={stack_empty} "
                      f"wf={(post.get('waiting_for') or {}).get('type')}")
    A["A4_cast_completes"] = "passed" if ok else "failed"
    notes.append("A4: " + "; ".join(detail))
except Exception as e:
    A["A4_cast_completes"] = "failed"
    notes.append(f"A4 eval error: {e}")

# ---- A5: control (pay branch)
try:
    runlog = open(f"{EVDIR}/scenario_run.log").read()
    decided = "cost answer (pay): decide_optional_cost" in runlog
    p_rej = [d for d in rejections if mode_of(d) == "pay"]
    post_p = state_of("post_pay.json")
    completed = len(vanq_on_bf(post_p, 0)) == 1
    tapped = tapped_forests(post_p, 0)
    A["A5_control_pay"] = ("passed" if (decided and not p_rej and completed)
                           else "failed")
    notes.append(f"A5: decideOptionalCost answered={decided} "
                 f"pay-mode rejections={len(p_rej)} cast_completed={completed}; "
                 f"OBSERVATION: engine auto-tap tapped {tapped}/6 Forests for "
                 f"the {{2}}{{G}}+{{3}} = 6-mana total (pools empty pre/post)")
except Exception as e:
    A["A5_control_pay"] = "failed"
    notes.append(f"A5 eval error: {e}")

# ---- A6: no softlock
try:
    runlog = open(f"{EVDIR}/scenario_run.log").read()
    stalled = "STALL" in runlog
    resolved_both = ("Vanquisher resolved on BF" in runlog
                     and runlog.count("Vanquisher resolved on BF") >= 2)
    A["A6_no_softlock"] = ("passed" if (not stalled and resolved_both)
                           else "failed")
    notes.append(f"A6: stall_watchdog_fired={stalled} "
                 f"both_casts_resolved={resolved_both}")
except Exception as e:
    A["A6_no_softlock"] = "failed"
    notes.append(f"A6 eval error: {e}")

verdict = ("not-reproduced" if all(v == "passed" for v in A.values())
           else "reproduced" if any(v == "failed" for v in A.values())
           else "blocked")

with open(f"{EVDIR}/assertions.json", "w") as f:
    json.dump({"assertions": A, "notes": notes,
               "wf_types": {m: sorted(s) for m, s in wf_types.items()}},
              f, indent=2)

# copy scenario into evidence
shutil.copy(f"{BACKFILL}/driver/scenario_{ISSUE}.py",
            f"{EVDIR}/scenario_{ISSUE}.py")
shutil.copy(f"{BACKFILL}/driver/finalize_{ISSUE}.py",
            f"{EVDIR}/finalize_{ISSUE}.py")

# server excerpts (INFO-only log: session lifecycle, no engine event lines)
se = [l for l in open(
    f"{BACKFILL}/runs/{RUN_ID}/server.log", errors="replace")
    if "listening" in l or "card database loaded" in l]
with open(f"{EVDIR}/server_excerpts.log", "w") as f:
    f.write("".join(se))
    f.write("# server logs only session/ws lifecycle at INFO; no per-action "
            "engine event lines. Cast/payment behavior is captured in "
            "wire_log.jsonl and scenario_run.log.\n")

scenario_bytes = open(f"{BACKFILL}/driver/scenario_{ISSUE}.py", "rb").read()
run_doc = {
    "issue": int(ISSUE),
    "run_id": RUN_ID,
    "started_at": "2026-09-11T22:42:00Z",
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
        "source": "ServerHello + minisign verification against repo-pinned key (pinned v0.80.0)",
    },
    "driver": {"protocol_version": 69, "client": "driver/client.py",
               "scenario": f"driver/scenario_{ISSUE}.py"},
    "scenario_sha256": hashlib.sha256(scenario_bytes).hexdigest(),
    "decks": {
        "P0": [[VANQ, 12], [ELF, 12], [FOREST, 36]],
        "P1": [[FOREST, 60]],
    },
    "setup_line": ("P0: 12x Wren's Run Vanquisher / 12x Llanowar Elves / 36x "
                   "Forest; proof casts on P0 main phases (reveal: turn 5 "
                   "with 3 Forests; pay: turn 17 with 6 Forests)"),
    "contract_line": ("Cast Wren's Run Vanquisher and take the REVEAL branch "
                      "of its 'reveal an Elf card from your hand or pay {3}' "
                      "additional cost: the reveal choice must be offered, the "
                      "Elf selectable, the cast must complete (3/3 on the "
                      "battlefield, stack empty, game proceeding, no "
                      "perpetual-stack stall). Control: the PAY {3} branch "
                      "completes the same way."),
    "assertions": A,
    "notes": notes + [
        "Wire shape (protocol 69): the additional cost first surfaces as "
        "waiting_for OptionalCostChoice with exactChoices carrying EMPTY "
        "text; the two choices are identified by action-surface codes "
        "cancelCast vs decideOptionalCost (cf. the protocol-69 kicker flow). "
        "Choosing decideOptionalCost advances to a PayCost{kind: Reveal} "
        "schema/select prompt whose candidates carry object surfaces "
        "(reference/name/zone/controller).",
        "OBSERVATION (candidate enumeration): the PayCost Reveal prompt "
        "offered 3 candidates for the Elf-subtype filter: two Wren's Run "
        "Vanquisher copies plus one Llanowar Elves. The driver selected the "
        "Elf; the submission was accepted with zero rejections and the cast "
        "completed.",
        "OBSERVATION (auto-tap count): the engine's Auto payment tapped "
        "cost-minus-one lands in both games (reveal: 2 tapped for {2}{G}; "
        "pay: 5 tapped for {2}{G}+{3}); mana pools were empty before and "
        "after each cast. Not part of the reported outcome; flagged for "
        "follow-up, not asserted.",
        "Driver pitfall hit and fixed mid-run: the first attempt answered "
        "the OptionalCostChoice with cancelCast (empty-text c0), abandoning "
        "the cast; the scenario now never selects cancelCast and keys all "
        "cost decisions off action-surface codes, never choice text.",
    ],
    "limitations": [
        "Browser UI not exercised; native engine via two human-client seats. "
        "The report's frontend claim (UI not rendering the reveal choice) "
        "was not tested.",
        "12x playset density is a test-harness convenience (engine accepts "
        ">4-of for custom games).",
        "Only Wren's Run Vanquisher exercised; the other six cards named in "
        "the issue share the mechanic but were not tested.",
        "Not tested on the original 2026-08-02 report build; verdict is "
        "scoped to v0.80.0, not a fix claim.",
        "The prebuilt server has no standalone state import; states are "
        "authoritative exports (restorable only via full game replay).",
    ],
    "verdict": verdict,
    "stats": {
        "wire_events": len(wire),
        "rejections": len(rejections),
        "cost_prompts": len(cost_prompts),
    },
}
with open(f"{EVDIR}/run.json", "w") as f:
    json.dump(run_doc, f, indent=2)

# PNG
title = ("Wren's Run Vanquisher reveal-or-pay additional cost: reveal branch "
         "offered, Elf selected, cast completes (no softlock); pay-{3} "
         "control completes")
r = subprocess.run([sys.executable, f"{BACKFILL}/driver/render_6873.py"],
                   capture_output=True, text=True)
print(r.stdout, r.stderr)
assert os.path.getsize(f"{EVDIR}/summary.png") > 0

# validate
from PIL import Image
for fn in ["pre_reveal.json", "post_reveal.json", "pre_pay.json",
           "post_pay.json", "run.json", "assertions.json",
           "wire_log.jsonl", "scenario_run.log", "server_excerpts.log",
           f"scenario_{ISSUE}.py", f"finalize_{ISSUE}.py"]:
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
