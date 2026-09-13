#!/usr/bin/env python3
"""Assemble the #7068 harness binaries from verbatim release-tagged sources.

new_diff.rs = coverage_parse_diff.rs at tag v0.82.0, byte-identical except the
single `use engine::game::coverage::{...}` line, which is replaced by the four
type definitions copied verbatim from game/coverage.rs at the same tag, plus a
renamed original main and a fixture main appended.

old_diff.rs = the pre-fix comparison logic from 86ed39cc~1 (the parent of the
fix commit #7118), helpers copied verbatim, the inline comparison block wrapped
as old_compare(), same verbatim type block, plus a fixture main appended.
"""
import re, sys

COV = "/tmp/coverage_v0820.rs"
NEW = "/tmp/cpd_v0820.rs"
OLD = "/tmp/cpd_prefix.rs"
OUT = "/home/hatch/workspace/dev/phase-backfill/driver/issue_7068/diffharness/src/bin"

def lines(path):
    with open(path) as f:
        return f.read().splitlines(keepends=True)

cov = lines(COV)

# Verbatim type block: ParsedItem (283-302 1-based), ParseCategory (304-314),
# GapDetail (316-324), CardCoverageResult (376-401).
types_block = "".join(cov[282:302] + cov[303:314] + cov[315:324] + cov[375:401])
assert "pub struct ParsedItem" in types_block
assert "pub enum ParseCategory" in types_block
assert "pub struct GapDetail" in types_block
assert "pub struct CardCoverageResult" in types_block
types_block = (
    "// ---- verbatim from crates/engine/src/game/coverage.rs @ v0.82.0 ----\n"
    + types_block
    + "// ---- end verbatim type block ----\n"
)

# ---------------- new_diff.rs ----------------
new = lines(NEW)
engine_use = "use engine::game::coverage::{CardCoverageResult, ParseCategory, ParsedItem};\n"
assert new.count(engine_use) == 1, "engine use line not found exactly once"
new = [types_block if l == engine_use else l for l in new]
mains = [i for i, l in enumerate(new) if l == "fn main() {\n"]
assert len(mains) == 1
new[mains[0]] = "fn orig_main() {\n"

fixture = '''
// ================= fixture / assertions (#7068) =================
fn mk_item(category: ParseCategory, label: &str, details: &[(&str, &str)]) -> ParsedItem {
    ParsedItem {
        category,
        label: label.to_string(),
        source_text: None,
        supported: true,
        details: details.iter().map(|(k, v)| (k.to_string(), v.to_string())).collect(),
        children: vec![],
    }
}

fn mk_card(name: &str, oracle: &str, parse: Vec<ParsedItem>) -> CardCoverageResult {
    CardCoverageResult {
        card_face_key: None,
        card_name: name.to_string(),
        set_code: "TST".to_string(),
        supported: true,
        gap_details: vec![],
        gap_count: 0,
        oracle_text: Some(oracle.to_string()),
        parse_details: parse,
        printings: vec![],
    }
}

fn dmg(amount: &str) -> ParsedItem {
    mk_item(ParseCategory::Ability, "DealDamage", &[("amount", amount), ("target", "any")])
}

fn fixture() -> (Vec<CardCoverageResult>, Vec<CardCoverageResult>, Vec<CardCoverageResult>) {
    let bears = mk_card(
        "Grizzly Bears",
        "Trample",
        vec![mk_item(ParseCategory::Keyword, "Trample", &[])],
    );
    let bolt = mk_card(
        "Lightning Bolt",
        "Lightning Bolt deals 3 damage to any target.",
        vec![dmg("3")],
    );
    let forest = mk_card("Forest", "", vec![]);
    // Colliding lowercased name, two DISTINCT oracle texts (two printings).
    let mage_a = mk_card(
        "Spark Mage",
        "Spark Mage deals 1 damage to any target.",
        vec![dmg("1")],
    );
    let mage_b = mk_card(
        "Spark Mage",
        "Spark Mage deals 2 damage to any target.",
        vec![dmg("2")],
    );
    // base and head: identical row MULTISETS, different array order
    // (simulates coverage-report's HashMap-ordered `cards` flipping between runs).
    let base = vec![bears.clone(), bolt.clone(), mage_a.clone(), mage_b.clone(), forest.clone()];
    let head = vec![forest.clone(), mage_b.clone(), bears.clone(), mage_a.clone(), bolt.clone()];
    // head2: head with a GENUINE parse change planted on the second Spark Mage row.
    let mage_b_mut = mk_card(
        "Spark Mage",
        "Spark Mage deals 2 damage to any target.",
        vec![dmg("5")],
    );
    let head2 = vec![forest, mage_b_mut, bears, mage_a, bolt];
    (base, head, head2)
}

fn report(name: &str, ok: bool, detail: &str) {
    println!("[{}] {}: {}", if ok { "PASS" } else { "FAIL" }, name, detail);
}

fn main() {
    let (base, head, head2) = fixture();
    let mut failures = 0;

    let cmp = compare(&base, &head);
    let a1 = cmp.oracle_changed == 0;
    report("A1_no_spurious_oracle_changed", a1,
        &format!("oracle_changed={} (identical parse content, permuted order)", cmp.oracle_changed));
    failures += !a1 as i32;

    let a2 = cmp.changed_cards == 0 && cmp.added_cards.is_empty()
        && cmp.removed_cards.is_empty() && cmp.clusters.is_empty();
    report("A2_no_changes_no_add_remove", a2,
        &format!("changed={} added={:?} removed={:?} clusters={}",
            cmp.changed_cards, cmp.added_cards, cmp.removed_cards, cmp.clusters.len()));
    failures += !a2 as i32;

    let a3 = cmp.duplicate_names == 1;
    report("A3_duplicate_name_reported", a3,
        &format!("duplicate_names={} (colliding 'spark mage' rows compared, not silent)", cmp.duplicate_names));
    failures += !a3 as i32;

    // Control: a genuine parse change on a duplicate-name row must be detected.
    let cmp2 = compare(&base, &head2);
    let a4 = cmp2.changed_cards == 1 && cmp2.oracle_changed == 0 && cmp2.clusters.len() == 1;
    let sig = if cmp2.clusters.len() == 1 {
        format!("{} {} {} -> {}", cmp2.clusters[0].cards.join(","), cmp2.clusters[0].label,
            cmp2.clusters[0].before, cmp2.clusters[0].after)
    } else { "n/a".to_string() };
    report("A4_genuine_change_detected", a4,
        &format!("changed={} oracle_changed={} clusters={} [{}]", cmp2.changed_cards, cmp2.oracle_changed, cmp2.clusters.len(), sig));
    failures += !a4 as i32;

    if failures > 0 {
        eprintln!("{} assertion(s) FAILED", failures);
        std::process::exit(1);
    }
    println!("all assertions passed");
}
'''

with open(f"{OUT}/new_diff.rs", "w") as f:
    f.writelines(new)
    f.write(fixture)

# ---------------- old_diff.rs ----------------
old = lines(OLD)
# header: std uses + serde (drop the engine use line)
header = [l for l in old[:24] if "use engine" not in l]
helpers = old[22:313]  # doc comment + #[derive(Deserialize)] + CoverageFile..Cluster
assert "struct Cluster {" in "".join(helpers)

# inline comparison block from old main(): 1-based lines 414..492
block = old[413:492]
blk = "".join(block)
assert "let bmap: BTreeMap<String, &CardCoverageResult>" in blk
assert "oracle_changed += 1;" in blk
assert "let md = render_markdown" not in blk

indented = "\n".join(
    "    " + l.rstrip("\n") for l in block
    if "let base = load" not in l and "let head = load" not in l
)
# rebind the verbatim block to the function parameters
indented = indented.replace(": BTreeMap<String, &CardCoverageResult> = base\n",
                            ": BTreeMap<String, &CardCoverageResult> = base_cards\n")
indented = indented.replace(": BTreeMap<String, &CardCoverageResult> = head\n",
                            ": BTreeMap<String, &CardCoverageResult> = head_cards\n")
indented = indented.replace(": BTreeMap<String, &CardCoverageResult> = head_cards\n            .cards\n",
                            ": BTreeMap<String, &CardCoverageResult> = head_cards\n")
indented = indented.replace(": BTreeMap<String, &CardCoverageResult> = base_cards\n            .cards\n",
                            ": BTreeMap<String, &CardCoverageResult> = base_cards\n")
old_src = "".join(header) + "\n" + types_block + "\n" + "".join(helpers) + """
struct OldComparison {
    clusters: Vec<Cluster>,
    changed_cards: usize,
    oracle_changed: usize,
    added_cards: Vec<String>,
    removed_cards: Vec<String>,
}

/// Verbatim pre-fix comparison logic (86ed39cc~1, parent of the #7118 fix):
/// BTreeMap keyed on lowercased card name (last-wins) + oracle_text mismatch
/// carve-out that skips the row. Wrapped as a function; rendering stripped.
fn old_compare(base_cards: &[CardCoverageResult], head_cards: &[CardCoverageResult]) -> OldComparison {
""" + indented + "\n"

# the wrapped block ends with the verbatim clusters collect/sort/dedup; close the fn
old_src = old_src.rstrip() + "\n    OldComparison {\n        clusters,\n        changed_cards: changed_card_set.len(),\n        oracle_changed,\n        added_cards,\n        removed_cards,\n    }\n}\n"

fixture_old = fixture.replace("let cmp = compare(&base, &head);", "let cmp = old_compare(&base, &head);") \
    .replace("let cmp2 = compare(&base, &head2);", "let cmp2 = old_compare(&base, &head2);") \
    .replace("cmp.duplicate_names", "cmp.duplicate_names")  # OldComparison has no duplicate_names
# old harness assertions differ: expect the BUG
fixture_old = fixture_old.replace(
    '''    let cmp = old_compare(&base, &head);
    let a1 = cmp.oracle_changed == 0;
    report("A1_no_spurious_oracle_changed", a1,
        &format!("oracle_changed={} (identical parse content, permuted order)", cmp.oracle_changed));
    failures += !a1 as i32;

    let a2 = cmp.changed_cards == 0 && cmp.added_cards.is_empty()
        && cmp.removed_cards.is_empty() && cmp.clusters.is_empty();
    report("A2_no_changes_no_add_remove", a2,
        &format!("changed={} added={:?} removed={:?} clusters={}",
            cmp.changed_cards, cmp.added_cards, cmp.removed_cards, cmp.clusters.len()));
    failures += !a2 as i32;

    let a3 = cmp.duplicate_names == 1;
    report("A3_duplicate_name_reported", a3,
        &format!("duplicate_names={} (colliding 'spark mage' rows compared, not silent)", cmp.duplicate_names));
    failures += !a3 as i32;

    // Control: a genuine parse change on a duplicate-name row must be detected.
    let cmp2 = old_compare(&base, &head2);
    let a4 = cmp2.changed_cards == 1 && cmp2.oracle_changed == 0 && cmp2.clusters.len() == 1;
    let sig = if cmp2.clusters.len() == 1 {
        format!("{} {} {} -> {}", cmp2.clusters[0].cards.join(","), cmp2.clusters[0].label,
            cmp2.clusters[0].before, cmp2.clusters[0].after)
    } else { "n/a".to_string() };
    report("A4_genuine_change_detected", a4,
        &format!("changed={} oracle_changed={} clusters={} [{}]", cmp2.changed_cards, cmp2.oracle_changed, cmp2.clusters.len(), sig));
    failures += !a4 as i32;''',
    '''    // OLD (pre-fix) expectations: the reported bug reproduces.
    let cmp = old_compare(&base, &head);
    let o1 = cmp.oracle_changed == 1;
    report("O1_spurious_oracle_changed", o1,
        &format!("oracle_changed={} (identical parse content, permuted order -> SPURIOUS)", cmp.oracle_changed));
    failures += !o1 as i32;

    // Genuine parse change planted on the discarded duplicate-name row: invisible.
    let cmp2 = old_compare(&base, &head2);
    let o2 = cmp2.changed_cards == 0 && cmp2.clusters.is_empty();
    report("O2_genuine_change_silently_skipped", o2,
        &format!("changed={} clusters={} oracle_changed={} (real change on 'Spark Mage' row INVISIBLE)",
            cmp2.changed_cards, cmp2.clusters.len(), cmp2.oracle_changed));
    failures += !o2 as i32;''')

with open(f"{OUT}/old_diff.rs", "w") as f:
    f.write(old_src)
    f.write(fixture_old)

print("assembled")
