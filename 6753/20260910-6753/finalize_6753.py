#!/usr/bin/env python3
"""Post-run correction for #6753 (cf. #4345/#4509 precedent: corrected post-run
from saved states).

The scenario's inline A4 checked: targeted permanent Battlefield->Library,
swap in AI graveyard, nothing stuck on the stack -> passed. A deeper
pre/post diff shows the exile step referenced the WRONG library:

  Oracle: "The owner of target nonenchantment permanent shuffles it into
           their library, then exiles the top card of their library."
  Observed (cast #1, pre.json -> post.json):
    - oid 8  Island (controller 0, owner 0): Battlefield -> Library   (correct:
      targeted permanent shuffled into its owner's library)
    - oid 88 Mountain (controller 1, in P1's library at pre): Library ->
      Battlefield under P1's control  (WRONG: the exiled top card came from
      the spell controller's library, not the targeted permanent's owner's)
    - P0 library 50 -> 51 (only the +1 from the shuffled target; no card left
      P0's library), P1 library 52 -> 51 (-1: the exiled Mountain)
    - oid 112 Audacious Swap: Stack -> P1 Graveyard (correct)

The pinned card-data parse is correct here: ExileTop player =
{"type": "ParentTarget"} (the Shuffle step correctly uses ParentTargetOwner).
The engine resolved that player reference to the spell's controller instead.

This defect is DISTINCT from the reported bug (AI endless target selection,
which did not occur: 10 casts, all resolved in <0.6s). The verdict on the
reported loop stays not-reproduced; the wrong-library defect is recorded as a
secondary finding (it fails triage acceptance criterion 3: "Audacious Swap
resolves its later owner decisions through the correct player").

This script updates run.json: A4 -> failed (with reason), adds the secondary
finding, and keeps the not-reproduced verdict with explicit reasoning.
"""
import json
import os

EVDIR = "/home/hatch/workspace/dev/phase-backfill/evidence/6753/20260910-6753"


def nm(o):
    return str(o.get("base_name") or o.get("name"))


def main():
    pre = json.load(open(f"{EVDIR}/pre.json"))["state"]
    post = json.load(open(f"{EVDIR}/post.json"))["state"]
    pre_objs, post_objs = pre["objects"], post["objects"]

    target_move = None
    exiled_move = None
    for oid in set(pre_objs) | set(post_objs):
        o1, o2 = pre_objs.get(oid, {}), post_objs.get(oid, {})
        if o1.get("zone") == "Battlefield" and o2.get("zone") == "Library":
            target_move = {"oid": oid, "name": nm(o1),
                           "controller": o1.get("controller"),
                           "owner": o1.get("owner")}
        if o1.get("zone") == "Library" and o2.get("zone") == "Battlefield":
            exiled_move = {"oid": oid, "name": nm(o1),
                           "controller": o2.get("controller"),
                           "owner": o2.get("owner")}

    p0pre = next(p for p in pre["players"] if p["id"] == 0)
    p0post = next(p for p in post["players"] if p["id"] == 0)
    p1pre = next(p for p in pre["players"] if p["id"] == 1)
    p1post = next(p for p in post["players"] if p["id"] == 1)

    wrong_library = (
        target_move is not None and exiled_move is not None
        and exiled_move["controller"] != target_move["owner"]
    )

    run = json.load(open(f"{EVDIR}/run.json"))
    casts = run["observations"]["casts"]

    run["assertions"]["A4_swap_resolves"] = "failed" if wrong_library else "passed"
    run["notes"].append(
        "A4 corrected post-run from saved states (cf. #4345 precedent): the "
        "targeted permanent WAS shuffled into its owner's library "
        f"({target_move}), but the exile/land-drop step used the spell "
        f"controller's library ({exiled_move}) instead of the targeted "
        "permanent's owner's library. P0 library "
        f"{len(p0pre['library'])}->{len(p0post['library'])} (only +1 from the "
        f"shuffled target; nothing exiled from it); P1 library "
        f"{len(p1pre['library'])}->{len(p1post['library'])} (-1: the exiled "
        "Mountain entered P1's battlefield). Per Oracle ('exiles the top "
        "card of their library', their = the owner) the exiled card should "
        "have come from P0's library."
    )
    run["secondary_finding"] = {
        "title": "Audacious Swap exiles the top card of the caster's library "
                 "instead of the targeted permanent's owner's library",
        "oracle": "The owner of target nonenchantment permanent shuffles it "
                  "into their library, then exiles the top card of their "
                  "library. If it's a land card, they put it onto the "
                  "battlefield. Otherwise, they may cast it without paying "
                  "its mana cost.",
        "observed": "Target P0 Island -> P0 library (correct); then the top "
                    "card of P1's (caster's) library was exiled and the "
                    "Mountain put onto P1's battlefield (wrong player).",
        "parse": "Pinned card-data parse is correct: ExileTop player = "
                 '{"type": "ParentTarget"} (Shuffle step uses ParentTargetOwner '
                 "and behaved); the engine resolved the player reference to "
                 "the spell's controller.",
        "relation_to_report": "Distinct defect from the reported AI "
                              "target-selection loop. Fails triage acceptance "
                              "criterion 3 ('resolves its later owner "
                              "decisions through the correct player').",
        "evidence": {"pre": "pre.json (swap on stack, oid 112)",
                     "post": "post.json (oid 8 Island BF->Library, oid 88 "
                             "Mountain Library->Battlefield under P1, oid 112 "
                             "Swap Stack->P1 graveyard)",
                     "casts_observed": len(casts)},
    }
    run["notes"].append(
        "Verdict reasoning: the REPORTED bug is the AI getting stuck "
        "'trying to select a target for this spell endlessly'. The AI cast "
        f"Audacious Swap {len(casts)} times; every cast's target selection "
        "completed and the spell resolved within 0.6s (worst observed). No "
        "target-selection stall occurred, so the reported loop is "
        "not-reproduced on v0.78.0. The wrong-library defect above is a "
        "separate finding, documented here and in the issue comment."
    )
    # P0 ActionRejected entries are driver-side stale-submission races
    # (submit based on a revision superseded mid-poll), not engine defects.
    run["notes"].append(
        f"P0 ActionRejected entries in the wire log ({len(run['observations']['rejections'])}) "
        "are driver-side races (action submitted from a superseded revision, "
        "disposition 'unavailable'), not engine defects; the game advanced "
        "normally throughout."
    )

    with open(f"{EVDIR}/run.json", "w") as f:
        json.dump(run, f, indent=2, default=str)
    print("run.json updated: A4 =", run["assertions"]["A4_swap_resolves"],
          "| verdict =", run["verdict"])
    print("wrong_library:", wrong_library)
    print("target_move:", target_move)
    print("exiled_move:", exiled_move)


main()
