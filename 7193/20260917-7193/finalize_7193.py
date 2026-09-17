#!/usr/bin/env python3
"""Post-run re-evaluation for issue #7193 (run 20260917-7193).

What the live run actually showed (from wire_log + saved states):
  1. After P0 announced Demand Answers, the engine did NOT present a
     sacrifice-vs-discard branch choice (P0 controlled no artifacts, so only
     the discard branch was payable). It went straight to
     waiting_for=PayCost, kind=Discard, with the 6 hand cards as candidates.
     The driver answered it with the engine-issued choice id for a Shock
     (oid 23) via {"type":"select","data":{"choiceIds":[...]}} -- accepted,
     no rejection. Shock oid 23 went to P0's graveyard (cost payment #1).
  2. The cast then STALLED: both seats passed priority across turns 4->7
     while the spell never appeared on the stack (120 pre-resolution stack
     samples, zero sightings -- the spell was not on the stack).
  3. ~10s later the engine issued a SECOND discard prompt (new iid
     OXBNJ6.0.95). The driver answered with a Shock (oid 44). Shock oid 44
     went to P0's graveyard (cost payment #2).
  4. 0.3s after the second payment the spell resolved exactly once
     (Demand Answers oid 2 in P0 graveyard; library 51->48 = drew 2 from the
     spell + 1 P0 draw step during the stall).
  5. Across 9 subsequent turns (turns 7-15, 120 stack samples) ZERO Demand
     Answers entries appeared on any later stack.

So: the REPORTED symptom ("added to every stack") did NOT reproduce, but a
related, precisely-characterized defect DID: the engine charged the discard
additional cost TWICE for a single cast (two engine-issued discard prompts,
both paid, cast stalled across turns until the second payment).

This script rewrites run.json assertions/notes/verdict with the corrected
understanding (preserving the originals under "assertions_initial" /
"notes_initial"), re-renders summary.png, and regenerates manifest.sha256.
"""
import hashlib
import json
import os
import sys

BACKFILL = "/home/hatch/workspace/dev/phase-backfill"
EVDIR = f"{BACKFILL}/evidence/7193/20260917-7193"


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def main():
    with open(f"{EVDIR}/run.json") as f:
        run = json.load(f)

    ass = {}
    notes = []
    cd = run["cost_decision"]
    dc = run["discard"]
    rs = run["resolution"]
    w = run["watch"]

    notes.append(
        "parse (pinned v0.85.0 card-data): Demand Answers {1}{R} Instant -- "
        "'As an additional cost to cast this spell, sacrifice an artifact or "
        "discard a card. Draw two cards.' additional_cost=Choice[Sacrifice, "
        "Discard].")

    # A1: the cost prompt, correctly understood post-hoc
    ass["A1_cost_branch"] = "passed"
    notes.append(
        "A1 passed: NO separate sacrifice-vs-discard branch choice was ever "
        "offered (P0 controlled no artifacts, so only the discard branch was "
        "payable); the engine went straight to waiting_for=PayCost "
        f"(kind=Discard, 6 hand-card candidates, iid {cd['interaction_id']}). "
        "The driver answered with the engine-issued choice id for Shock "
        f"(choice {cd['choice_id']}, oid 23) via "
        '{"type":"select","data":{"choiceIds":[...]}} -- accepted, no '
        "rejection. (The driver's live 'branch choice' framing was wrong; "
        "the submitted selection was still a valid discard payment.)")

    # A2: the double charge
    ass["A2_single_discard"] = "failed"
    notes.append(
        "A2 FAILED (related defect, precisely characterized): the engine "
        "charged the discard additional cost TWICE for the single cast. "
        f"Payment #1: PayCost prompt {cd['interaction_id']} -> Shock oid 23 "
        "discarded. The cast then stalled (both seats passing priority, "
        "turns 4->7, spell never on the stack in any sample). Payment #2: a "
        f"SECOND engine-issued discard prompt ({dc['interaction_id']}) ~10s "
        "later -> Shock oid 44 discarded. Post-resolution state confirms "
        "BOTH Shock oid 23 and Shock oid 44 in P0's graveyard. The spell "
        "resolved 0.3s after the second payment. Both prompts were answered "
        "with engine-issued choice ids; zero rejections -- this is engine "
        "behavior, not a driver artifact.")

    # A3: resolution itself
    ass["A3_resolve_once"] = "passed"
    notes.append(
        "A3 passed: the spell resolved EXACTLY once -- Demand Answers oid 2 "
        "in P0's graveyard post-resolution; P0 library 51->48 (drew 2 from "
        "the spell plus 1 P0 draw step during the turn-4->7 stall).")

    # A4: stack clean right after resolution
    ass["A4_stack_clean"] = "passed"
    notes.append("A4 passed: post-resolution export shows zero Demand "
                 "Answers entries on the stack.")

    # A5: the reported symptom, tested directly
    turns = w["turns_observed"]
    if len(turns) >= 8 and not w["sightings"]:
        ass["A5_no_later_stack"] = "passed"
        notes.append(
            f"A5 passed: {len(turns)} post-resolution turns observed (turns "
            f"{turns[0]}-{turns[-1]}, {w['stack_samples']} stack samples on "
            "every state revision), ZERO Demand Answers entries on any later "
            "stack. The reported 'added to every stack' symptom did NOT occur.")
    else:
        ass["A5_no_later_stack"] = "failed"
        notes.append(f"A5 FAILED: sightings={w['sightings']}")

    # A6: no rejections
    bad = {i: n for i, n in (run.get("rejections") or {}).items() if n > 0}
    if not bad and rs["resolved"]:
        ass["A6_no_rejects"] = "passed"
        notes.append("A6 passed: zero rejections on any interaction; the "
                     "announced cast completed.")
    else:
        ass["A6_no_rejects"] = "failed"
        notes.append(f"A6 FAILED: rejections={bad}")

    verdict = "not-reproduced"
    notes.append(
        "verdict: not-reproduced -- the reported symptom (Demand Answers "
        "reappearing on later stacks after a discard-branch cast) did NOT "
        "occur across 4+ full turn cycles of per-revision stack sampling. "
        "RELATED DEFECT OBSERVED (single run, needs confirmation): the "
        "engine charged the discard additional cost twice for one cast "
        "(two engine-issued discard prompts, both paid; cast stalled across "
        "turns 4->7 until the second payment). This violates the triage "
        "criterion 'casting requires exactly one of the additional costs' "
        "and is documented here with full evidence; it is a distinct defect "
        "from the reported stack-reappearance.")

    run["assertions_initial"] = run.get("assertions")
    run["notes_initial"] = run.get("notes")
    run["assertions"] = ass
    run["notes"] = notes
    run["verdict"] = verdict
    run["evaluation_correction"] = (
        "Assertions re-evaluated post-run by driver/finalize_7193.py: the "
        "live driver's 'branch choice' framing was corrected (the PayCost "
        "prompt WAS the discard-card selection; no branch choice exists when "
        "only one branch is payable), and the double-discard observation "
        "was verified against pre_cast.json/post_resolution.json "
        "(both Shock oids 23+44 in P0 graveyard). Originals preserved under "
        "assertions_initial/notes_initial. No new game was run.")

    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=1)

    # re-render PNG from the corrected run dict
    sys.path.insert(0, f"{BACKFILL}/driver")
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "sc7193", f"{BACKFILL}/driver/scenario_7193.py")
    # avoid executing the module (it runs amain on import); copy render fn
    from PIL import Image, ImageDraw
    W, H = 1040, 1180
    bg = (16, 18, 24)
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    y = 24

    def line(t, fill=(230, 230, 235), size=20):
        nonlocal y
        d.text((28, y), t, fill=fill)
        y += size + 10

    line("#7193 Demand Answers: discard-branch cast reappears on later "
         "stacks?", fill=(255, 210, 90))
    line(f"server v{run['server']['server_version']} build "
         f"{run['server']['build_commit']} protocol "
         f"{run['server']['protocol_version']}  |  run {run['run_id']}  |  "
         f"{run['validated_at']}")
    v = run["verdict"]
    line(f"verdict: {v.upper()}",
         fill=(255, 170, 90) if v == "reproduced" else (120, 255, 160)
         if v == "not-reproduced" else (255, 120, 120))
    y += 6
    line("setup: P0 12x Demand Answers / 24x Mountain / 24x Shock vs P1 60x "
         "Mountain", size=16)
    line("PayCost=discard-card selection (no branch choice; no artifacts); "
         "then 4 turn cycles of stack sampling", size=16)
    y += 6
    line("assertions:", fill=(160, 200, 255))
    for k, val in run["assertions"].items():
        col = (120, 255, 160) if val == "passed" else ((255, 120, 120)
              if val == "failed" else (200, 200, 200))
        line(f"  {k}: {val}", fill=col, size=17)
    y += 6
    line("notes:", fill=(160, 200, 255))
    for n in run["notes"][:13]:
        line(f"  - {n[:112]}", size=15)
    img.save(f"{EVDIR}/summary.png")
    print("rendered summary.png")

    lines = []
    for fn in sorted(os.listdir(EVDIR)):
        if fn == "manifest.sha256":
            continue
        lines.append(sha(f"{EVDIR}/{fn}") + "  " + fn)
    with open(f"{EVDIR}/manifest.sha256", "w") as f:
        f.write("\n".join(lines) + "\n")
    print("wrote manifest.sha256")
    print(json.dumps({"verdict": verdict, "assertions": ass}, indent=1))


main()
