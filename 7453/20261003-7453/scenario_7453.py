#!/usr/bin/env python3
"""phase-rs/phase #7453 — parser data-inspection scenario.

Issue: 'Creatures with power less than ~'s power can't block it.' (pronoun
object) collapsed to mode=CantBlock affected=Some(SelfRef), inverting the
restriction for 12 cards (11x 'power less than', Silumgar Assassin
'power greater than').

Subsystem: parser / data pipeline. The pinned release ships pre-parsed
card-data.json; the reported defect IS the parse shape, and the triage
comment established "confirmed by data inspection". No engine session
applies: the engine loads exactly this dataset, so a correct parse here is
the observable contract.

Behavioral contract (all must hold for not-reproduced):
  A1 data_integrity      — card-data.json sha256 matches the pinned,
                           minisign-verified release manifest.
  A2 all_12_present      — all 12 issue-named cards carry the pronoun-object
                           block-restriction clause.
  A3 mode_correct        — every such clause parses to CantBeBlockedBy
                           (never the inverted CantBlock).
  A4 affected_self       — affected is SelfRef (the restriction's object is
                           the source, per the issue's expected lowering).
  A5 filter_semantics    — filter is a Creature Typed filter with a
                           PtComparison on current Power vs source power:
                           LE/offset -1 for the 11 'less than' cards,
                           GE/offset +1 for Silumgar Assassin.
  A6 dataset_sweep       — no "can't block it" static anywhere in the
                           35,879-card dataset still parses to CantBlock.

Verdict rule: all A1..A6 passed -> not-reproduced (per playbook: this does
not mean "fixed"). Any failed -> reproduced.
"""
import hashlib
import json
import os
import sys

RELEASE = "v0.101.0"
RELEASE_DIR = os.path.expanduser(
    "~/workspace/dev/phase-backfill/server/releases/" + RELEASE)
CARD_DATA = os.path.join(RELEASE_DIR, "data", "card-data.json")
MANIFEST = os.path.join(RELEASE_DIR, "release-server-" + RELEASE + ".json")

EXPECTED_CARD_DATA_SHA256 = (
    "b365361edafd3d901e361fe1eef845ca4748c7b2b371ee27e300013f64f37f00")

LESS_THAN_CARDS = [
    "aura gnarlid", "battering wurm", "den protector", "elusive otter",
    "feasting hobbit", "formation breaker", "howlgeist", "howling chorus",
    "shrill howler", "skarrgan pit-skulk", "wandering wolf",
]
GREATER_THAN_CARDS = ["silumgar assassin"]
ALL_12 = LESS_THAN_CARDS + GREATER_THAN_CARDS


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def block_restriction_statics(entry):
    out = []
    for s in entry.get("static_abilities", []) or []:
        desc = s.get("description") or ""
        if "can't block it" in desc.lower():
            out.append(s)
    return out


def check_filter(static, comparator, offset):
    """Verify CantBeBlockedBy filter: Creature Typed + PtComparison on
    current Power vs (source power + offset) with the given comparator."""
    mode = static.get("mode", {})
    inner = mode.get("CantBeBlockedBy")
    if not inner:
        return False, "mode is not CantBeBlockedBy"
    filt = inner.get("filter", {})
    if filt.get("type") != "Typed" or "Creature" not in filt.get("type_filters", []):
        return False, "filter is not a Creature Typed filter"
    props = filt.get("properties", [])
    pt = [p for p in props if p.get("type") == "PtComparison"
          and p.get("stat") == "Power" and p.get("scope") == "Current"]
    if not pt:
        return False, "no Power PtComparison property"
    p = pt[0]
    val = p.get("value", {})
    inner = val.get("inner") or {}
    qty = inner.get("qty") or {}
    if (p.get("comparator") != comparator
            or val.get("type") != "Offset"
            or val.get("offset") != offset
            or inner.get("type") != "Ref"
            or qty.get("type") != "Power"
            or qty.get("scope", {}).get("type") != "Source"):
        return False, "PtComparison does not compare Power vs source power %s %d" % (
            comparator, offset)
    return True, "ok"


def main():
    assertions = {}
    notes = []

    # A1: data integrity
    try:
        manifest = json.load(open(MANIFEST))
        want = {e["name"]: e["sha256"] for e in manifest["data"]}["card-data.json"]
        got = sha256_of(CARD_DATA)
        assert want == EXPECTED_CARD_DATA_SHA256 == got, \
            "digest mismatch: manifest=%s local=%s" % (want, got)
        assertions["A1_data_integrity"] = "passed"
        notes.append("card-data.json sha256 %s matches manifest" % got)
    except Exception as e:  # noqa: BLE001
        assertions["A1_data_integrity"] = "failed: %s" % e
        notes.append("data integrity check failed: %s" % e)
        print(json.dumps({"assertions": assertions, "notes": notes}, indent=1))
        return 1

    data = json.load(open(CARD_DATA))
    notes.append("dataset: %d cards" % len(data))

    # A2: all 12 present with the clause
    statics = {}
    missing = []
    for c in ALL_12:
        entry = data.get(c)
        found = block_restriction_statics(entry) if entry else []
        if not found:
            missing.append(c)
        else:
            statics[c] = found[0]
    assertions["A2_all_12_present"] = (
        "passed" if not missing else "failed: missing %s" % missing)

    # A3/A4/A5: per-card parse shape
    bad_mode, bad_aff, bad_filter, filter_notes = [], [], [], []
    excerpts = {}
    for c in ALL_12:
        s = statics.get(c)
        if s is None:
            continue
        mode = s.get("mode", {})
        mode_name = list(mode.keys())[0] if isinstance(mode, dict) else str(mode)
        aff = s.get("affected", {})
        aff_name = aff.get("type") if isinstance(aff, dict) else str(aff)
        if mode_name != "CantBeBlockedBy":
            bad_mode.append((c, mode_name))
        if aff_name != "SelfRef":
            bad_aff.append((c, aff_name))
        comp, off = ("LE", -1) if c in LESS_THAN_CARDS else ("GE", 1)
        ok, why = check_filter(s, comp, off)
        if not ok:
            bad_filter.append((c, why))
        filter_notes.append("%s: %s %s" % (c, "ok" if ok else "BAD", why))
        excerpts[c] = {
            "description": s.get("description"),
            "mode": mode,
            "affected": aff,
        }
    assertions["A3_mode_correct"] = (
        "passed" if not bad_mode else "failed: %s" % bad_mode)
    assertions["A4_affected_self"] = (
        "passed" if not bad_aff else "failed: %s" % bad_aff)
    assertions["A5_filter_semantics"] = (
        "passed" if not bad_filter else "failed: %s" % bad_filter)
    notes.extend(filter_notes)

    # A6: dataset-wide sweep — no "can't block it" static collapses to CantBlock
    sweep_bad, sweep_total, sweep_modes = [], 0, {}
    for name, entry in data.items():
        for s in block_restriction_statics(entry):
            sweep_total += 1
            mode = s.get("mode", {})
            mn = list(mode.keys())[0] if isinstance(mode, dict) else str(mode)
            sweep_modes[mn] = sweep_modes.get(mn, 0) + 1
            if mn == "CantBlock":
                sweep_bad.append(name)
    notes.append("sweep: %d \"can't block it\" statics dataset-wide; modes=%s"
                 % (sweep_total, sweep_modes))
    assertions["A6_dataset_sweep"] = (
        "passed" if not sweep_bad else "failed: %s" % sweep_bad)

    verdict = ("not-reproduced"
               if all(v == "passed" for v in assertions.values())
               else "reproduced")
    result = {
        "issue": 7453,
        "release": RELEASE,
        "card_data_sha256": EXPECTED_CARD_DATA_SHA256,
        "assertions": assertions,
        "notes": notes,
        "verdict": verdict,
    }
    print(json.dumps(result, indent=1))

    evdir = os.path.join(os.path.expanduser("~/workspace/dev/phase-backfill"),
                         "evidence", "7453", RUN_ID)
    os.makedirs(evdir, exist_ok=True)
    json.dump(excerpts, open(os.path.join(evdir, "card_excerpts.json"), "w"),
              indent=1, sort_keys=True)
    json.dump({"assertions": assertions, "notes": notes},
              open(os.path.join(evdir, "assertions.json"), "w"), indent=1)
    return 0 if verdict == "not-reproduced" else 2


RUN_ID = os.environ.get("RUN_ID", "20261003-7453")

if __name__ == "__main__":
    sys.exit(main())
