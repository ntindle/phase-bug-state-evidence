#!/usr/bin/env python3
"""
phase-rs/phase#6659 — engine-side canonicalization data leg (A1).

Replicates the ENGINE's own algorithm (not a re-derivation of the rules):
  crates/engine/src/database/card_db.rs::lookup_key + fold_card_name_key
  crates/engine/src/game/deck_validation.rs::canonical_deck_count_key
    ("Uses the indexed face name when the card resolves so alias spellings
      ('Nazgul' vs 'Nazgûl') merge into one bucket for copy-limit checks.")
  crates/engine/src/game/deck_validation.rs::combined_copy_counts
    (counts keyed by canonical_deck_count_key)
  crates/engine/src/game/deck_validation.rs::max_deck_copies
    ("let canonical = canonical_deck_count_key(db, name);" — the ceiling is
     resolved AFTER canonicalization, so both spellings get the same limit)

Run against the pinned v0.85.0 card-data.json to show that the deck the
client affordance builds (alias spellings as separate entries) merges into a
single canonical bucket the engine's copy_limit_violations flags — i.e. the
engine still rejects the illegal deck and the bug is confined to the client
affordance, exactly as the issue scopes it.
"""
import json
import sys
import unicodedata

DATA = "/home/hatch/workspace/dev/phase-backfill/server/releases/v0.85.0/data/card-data.json"

FOLD = {
    'á': 'a', 'à': 'a', 'â': 'a', 'ä': 'a', 'ã': 'a', 'å': 'a',
    'ç': 'c', 'é': 'e', 'è': 'e', 'ê': 'e', 'ë': 'e',
    'í': 'i', 'ì': 'i', 'î': 'i', 'ï': 'i',
    'ñ': 'n', 'ó': 'o', 'ò': 'o', 'ô': 'o', 'ö': 'o',
    'ú': 'u', 'ù': 'u', 'û': 'u', 'ü': 'u',
    'ý': 'y', 'ÿ': 'y',
}


def fold_card_name_key(name: str) -> str:
    out = []
    for ch in name:
        for lower in ch.lower():
            out.append(FOLD.get(lower, lower))
    return "".join(out)


def main():
    card_data = json.load(open(DATA))
    face_names = {}
    for key, face in card_data.items():
        if isinstance(face, dict) and face.get("name"):
            face_names[key] = face["name"]
    # alias index: folded key -> resolved key (engine builds this from card data)
    alias_index = {}
    for key in card_data.keys():
        folded = fold_card_name_key(key)
        if folded != key and folded not in card_data and folded not in alias_index:
            alias_index[folded] = key

    def lookup_key(name: str) -> str:
        lower = name.lower()
        if lower in face_names or lower in card_data:
            return lower
        alias = alias_index.get(fold_card_name_key(name))
        if alias:
            return alias
        front, _, _ = lower.partition("//")
        front = front.strip()
        if front in face_names or front in card_data:
            return front
        alias = alias_index.get(fold_card_name_key(front))
        if alias:
            return alias
        return lower

    def canonical_deck_count_key(name: str) -> str:
        resolved = lookup_key(name)
        return face_names.get(resolved, resolved).lower()

    deck_entries = [("Nazgul", 9), ("Nazgûl", 9)]  # the 9+9 deck from gap 1
    canonical_counts = {}
    per_entry = []
    for name, count in deck_entries:
        canon = canonical_deck_count_key(name)
        per_entry.append({"name": name, "canonical_key": canon, "count": count})
        canonical_counts[canon] = canonical_counts.get(canon, 0) + count

    face = card_data.get("nazgûl", {})
    oracle = face.get("oracle_text", "") if isinstance(face, dict) else ""
    ceiling = 9 if "up to nine" in oracle.lower() else None

    violations = [
        {"canonical_key": k, "count": c, "ceiling": ceiling}
        for k, c in canonical_counts.items()
        if ceiling is not None and c > ceiling
    ]

    result = {
        "inputs": {
            "deck_entries": [{"name": n, "count": c} for n, c in deck_entries],
            "card_data": "v0.85.0 pinned card-data.json",
        },
        "canonical_keys": per_entry,
        "canonical_counts": canonical_counts,
        "printed_ceiling_nazgul": ceiling,
        "oracle_text_excerpt": oracle[:160],
        "copy_limit_violations": violations,
        "verdict": (
            "MERGED" if len(canonical_counts) == 1 else "NOT MERGED"
        ),
    }
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    print()
    assert len(canonical_counts) == 1, "alias spellings did not merge"
    assert violations, "expected a copy-limit violation for 18 > 9"


if __name__ == "__main__":
    main()
