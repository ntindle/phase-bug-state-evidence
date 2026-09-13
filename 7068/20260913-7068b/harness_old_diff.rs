//! `coverage-parse-diff` — diff the `parse_details` parse-trees of two
//! `coverage-data.json` snapshots and emit a clustered, review-oriented report.
//!
//! Purpose: the existing coverage-regression gate only reports `supported`
//! flips (Unimplemented <-> Supported). This tool surfaces *field-level* parse
//! changes — a target filter that gained a clause, an amount that changed from
//! Fixed to Variable, a condition that was swapped — even when `supported`
//! stays `true`. The clustered Markdown is posted as a PR comment so a
//! reviewing LLM gets the structural delta without re-deriving it.
//!
//! Baseline semantics live in CI (the caller passes the PR's merge-base
//! snapshot, never a lagging deployed-main snapshot); this binary is a pure
//! function of the two files it is handed.

use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::fs;
use std::process;

use serde::{Deserialize, Serialize};

/// Minimal view of `coverage-data.json` — only the per-card array is read; the
/// summary's other fields are ignored by serde, decoupling us from their shape.

// ---- verbatim from crates/engine/src/game/coverage.rs @ v0.82.0 ----
/// parsed item (keyword, ability, trigger, static, or replacement) with its
/// support status and any nested children (sub-abilities, modal modes, etc.).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ParsedItem {
    /// Category of the parsed item.
    pub category: ParseCategory,
    /// Human-readable label (e.g. "DealDamage", "Flying", "ChangesZone").
    pub label: String,
    /// Original Oracle text fragment that produced this item, when available.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_text: Option<String>,
    /// Whether this specific item is supported by the engine.
    pub supported: bool,
    /// Key-value pairs of parsed parameters (e.g., target, amount, zone).
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub details: Vec<(String, String)>,
    /// Nested items (sub-abilities, modal choices, composite costs).
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub children: Vec<ParsedItem>,
}
/// The category of a parsed item in the coverage tree.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ParseCategory {
    Keyword,
    Ability,
    Trigger,
    Static,
    Replacement,
    Cost,
}
/// An enriched gap entry with the handler key and the Oracle text that produced it.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GapDetail {
    /// Handler key in "Category:label" format (e.g., "Effect:unknown", "Trigger:ChangesZone").
    pub handler: String,
    /// The Oracle text fragment that produced this gap.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub source_text: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CardCoverageResult {
    /// Internal provenance for associating duplicate printed names with their
    /// authoritative database face. Coverage JSON remains byte-compatible.
    #[serde(skip)]
    pub card_face_key: Option<String>,
    pub card_name: String,
    pub set_code: String,
    pub supported: bool,
    /// Enriched gaps with Oracle text fragments — replaces the old `missing_handlers`.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub gap_details: Vec<GapDetail>,
    /// Number of distinct gaps (`gap_details.len()`), a distance-to-supported metric.
    pub gap_count: usize,
    /// Original Oracle text for the card face.
    #[serde(skip_serializing_if = "Option::is_none")]
    pub oracle_text: Option<String>,
    /// Hierarchical parse tree showing what each piece of Oracle text was parsed into.
    #[serde(skip_serializing_if = "Vec::is_empty", default)]
    pub parse_details: Vec<ParsedItem>,
    /// Set codes the card has been printed in (from MTGJSON `printings`).
    /// Used by the coverage dashboard to aggregate cards by set.
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub printings: Vec<String>,
}
// ---- end verbatim type block ----

/// Minimal view of `coverage-data.json` — only the per-card array is read; the
/// summary's other fields are ignored by serde, decoupling us from their shape.
#[derive(Deserialize)]
struct CoverageFile {
    #[serde(default)]
    cards: Vec<CardCoverageResult>,
}

fn cat_str(c: &ParseCategory) -> &'static str {
    match c {
        ParseCategory::Keyword => "keyword",
        ParseCategory::Ability => "ability",
        ParseCategory::Trigger => "trigger",
        ParseCategory::Static => "static",
        ParseCategory::Replacement => "replacement",
        ParseCategory::Cost => "cost",
    }
}

/// Kind of a single field-level change within a card's parse tree.
#[derive(Clone, Copy, PartialEq, Eq, Debug)]
enum ChangeKind {
    FieldChanged,
    ItemAdded,
    ItemRemoved,
    SupportFlip,
}

impl ChangeKind {
    fn label(self) -> &'static str {
        match self {
            ChangeKind::FieldChanged => "field",
            ChangeKind::ItemAdded => "added",
            ChangeKind::ItemRemoved => "removed",
            ChangeKind::SupportFlip => "support",
        }
    }

    fn section_heading(self) -> &'static str {
        match self {
            ChangeKind::ItemAdded => "🟢 Added",
            ChangeKind::ItemRemoved => "🔴 Removed",
            ChangeKind::FieldChanged => "🟡 Modified fields",
            ChangeKind::SupportFlip => "🔵 Support status",
        }
    }

    fn marker(self) -> &'static str {
        match self {
            ChangeKind::ItemAdded => "➕",
            ChangeKind::ItemRemoved => "➖",
            ChangeKind::FieldChanged => "🔄",
            ChangeKind::SupportFlip => "↕️",
        }
    }
}

/// One field-level change, attributed to a card.
struct Change {
    category: &'static str,
    label: String,
    kind: ChangeKind,
    key: String,
    before: String,
    after: String,
}

/// Canonical identity of an item for multiset exact-match: category, label,
/// source_text, supported, sorted details, and recursively-canonicalized
/// children (sorted). Two items with the same canon string are "unchanged".
fn canon(item: &ParsedItem) -> String {
    let mut s = String::new();
    let _ = write!(
        s,
        "{}|{}|{}|{}|",
        cat_str(&item.category),
        item.label,
        item.source_text.as_deref().unwrap_or(""),
        item.supported,
    );
    let mut dets: Vec<&(String, String)> = item.details.iter().collect();
    dets.sort();
    s.push('{');
    for (k, v) in dets {
        let _ = write!(s, "{k}={v};");
    }
    s.push_str("}[");
    let mut kids: Vec<String> = item.children.iter().map(canon).collect();
    kids.sort();
    for k in kids {
        s.push_str(&k);
        s.push(',');
    }
    s.push(']');
    s
}

/// Weak key for residual reconciliation — discards `details`/`children` (the
/// fields a value-change lives in) so paired items can be field-diffed.
fn weak_key(item: &ParsedItem) -> (String, String, String) {
    (
        cat_str(&item.category).to_string(),
        item.label.clone(),
        item.source_text.clone().unwrap_or_default(),
    )
}

/// Compact one-line summary of an item (for add/remove change values).
fn summarize(item: &ParsedItem) -> String {
    if item.details.is_empty() {
        item.label.clone()
    } else {
        let mut dets: Vec<&(String, String)> = item.details.iter().collect();
        dets.sort();
        let body: Vec<String> = dets.iter().map(|(k, v)| format!("{k}={v}")).collect();
        format!("{} ({})", item.label, body.join(", "))
    }
}

/// Diff a matched item pair: support flip, detail key adds/removes/changes,
/// then recurse into children.
fn diff_items(base: &ParsedItem, head: &ParsedItem, out: &mut Vec<Change>) {
    let category = cat_str(&head.category);
    if base.supported != head.supported {
        out.push(Change {
            category,
            label: head.label.clone(),
            kind: ChangeKind::SupportFlip,
            key: String::new(),
            before: base.supported.to_string(),
            after: head.supported.to_string(),
        });
    }
    let bmap: BTreeMap<&str, &str> = base
        .details
        .iter()
        .map(|(k, v)| (k.as_str(), v.as_str()))
        .collect();
    let hmap: BTreeMap<&str, &str> = head
        .details
        .iter()
        .map(|(k, v)| (k.as_str(), v.as_str()))
        .collect();
    for (k, bv) in &bmap {
        match hmap.get(k) {
            Some(hv) if hv != bv => out.push(Change {
                category,
                label: head.label.clone(),
                kind: ChangeKind::FieldChanged,
                key: (*k).to_string(),
                before: (*bv).to_string(),
                after: (*hv).to_string(),
            }),
            None => out.push(Change {
                category,
                label: head.label.clone(),
                kind: ChangeKind::FieldChanged,
                key: (*k).to_string(),
                before: (*bv).to_string(),
                after: "∅".to_string(),
            }),
            _ => {}
        }
    }
    for (k, hv) in &hmap {
        if !bmap.contains_key(k) {
            out.push(Change {
                category,
                label: head.label.clone(),
                kind: ChangeKind::FieldChanged,
                key: (*k).to_string(),
                before: "∅".to_string(),
                after: (*hv).to_string(),
            });
        }
    }
    diff_level(&base.children, &head.children, out);
}

/// Diff a sibling list (top-level or children): cancel structurally-identical
/// items as a multiset, then reconcile residuals by weak key — pairing leftover
/// items as value-changes and reporting the rest as adds/removes. Cannot
/// mis-pair: ambiguous residuals degrade to truthful add+remove.
fn diff_level(base_items: &[ParsedItem], head_items: &[ParsedItem], out: &mut Vec<Change>) {
    // Cancel exact structural matches as a multiset.
    let mut base_left: Vec<&ParsedItem> = Vec::new();
    let mut head_counts: BTreeMap<String, usize> = BTreeMap::new();
    for h in head_items {
        *head_counts.entry(canon(h)).or_insert(0) += 1;
    }
    for b in base_items {
        let c = canon(b);
        if let Some(n) = head_counts.get_mut(&c) {
            if *n > 0 {
                *n -= 1;
                continue; // structurally identical → unchanged
            }
        }
        base_left.push(b);
    }
    let head_left: Vec<&ParsedItem> = head_items
        .iter()
        .filter(|h| {
            // Keep heads whose canon budget was not consumed by a base match.
            // Recompute remaining budget lazily: a head is "matched" iff its
            // canon still has count earmarked. We decrement here to mirror.
            let c = canon(h);
            match head_counts.get_mut(&c) {
                Some(n) if *n > 0 => {
                    *n -= 1;
                    true
                }
                _ => false,
            }
        })
        .collect();

    // Group residuals by weak key.
    let mut bgroups: BTreeMap<(String, String, String), Vec<&ParsedItem>> = BTreeMap::new();
    let mut hgroups: BTreeMap<(String, String, String), Vec<&ParsedItem>> = BTreeMap::new();
    for b in &base_left {
        bgroups.entry(weak_key(b)).or_default().push(b);
    }
    for h in &head_left {
        hgroups.entry(weak_key(h)).or_default().push(h);
    }
    let mut keys: Vec<(String, String, String)> = bgroups.keys().cloned().collect();
    for k in hgroups.keys() {
        if !bgroups.contains_key(k) {
            keys.push(k.clone());
        }
    }
    for k in keys {
        let bs = bgroups.get(&k).cloned().unwrap_or_default();
        let hs = hgroups.get(&k).cloned().unwrap_or_default();
        let paired = bs.len().min(hs.len());
        for i in 0..paired {
            diff_items(bs[i], hs[i], out);
        }
        for b in bs.iter().skip(paired) {
            out.push(Change {
                category: cat_str(&b.category),
                label: b.label.clone(),
                kind: ChangeKind::ItemRemoved,
                key: String::new(),
                before: summarize(b),
                after: "∅".to_string(),
            });
        }
        for h in hs.iter().skip(paired) {
            out.push(Change {
                category: cat_str(&h.category),
                label: h.label.clone(),
                kind: ChangeKind::ItemAdded,
                key: String::new(),
                before: "∅".to_string(),
                after: summarize(h),
            });
        }
    }
}

/// Replace case-insensitive occurrences of the card name with `~` so a
/// per-card value (e.g. a target naming the card itself) clusters across cards.
fn template(val: &str, card_name: &str) -> String {
    if card_name.is_empty() {
        return val.to_string();
    }
    let lower_val = val.to_lowercase();
    let lower_name = card_name.to_lowercase();
    let mut out = String::with_capacity(val.len());
    let mut idx = 0;
    while let Some(pos) = lower_val[idx..].find(&lower_name) {
        let start = idx + pos;
        out.push_str(&val[idx..start]);
        out.push('~');
        idx = start + lower_name.len();
    }
    out.push_str(&val[idx..]);
    out
}

struct Cluster {
    category: &'static str,
    label: String,
    kind: ChangeKind,
    key: String,
    before: String,
    after: String,
    cards: Vec<String>,
}

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
        let bmap: BTreeMap<String, &CardCoverageResult> = base_cards
            .iter()
            .map(|c| (c.card_name.to_ascii_lowercase(), c))
            .collect();
        let hmap: BTreeMap<String, &CardCoverageResult> = head_cards
            .iter()
            .map(|c| (c.card_name.to_ascii_lowercase(), c))
            .collect();
    
        let mut sig_to_cluster: BTreeMap<(String, String, String, String, String, String), Cluster> =
            BTreeMap::new();
        let mut oracle_changed = 0usize;
        let mut added_cards: Vec<String> = Vec::new();
        let mut removed_cards: Vec<String> = Vec::new();
        let mut changed_card_set: std::collections::BTreeSet<String> =
            std::collections::BTreeSet::new();
    
        for (k, h) in &hmap {
            let Some(b) = bmap.get(k) else {
                added_cards.push(h.card_name.clone());
                continue;
            };
            // Oracle-text change → parse legitimately differs for a non-parser
            // reason (errata/reprint). Carve out; do not attribute to the PR.
            if b.oracle_text != h.oracle_text {
                oracle_changed += 1;
                continue;
            }
            let mut changes = Vec::new();
            diff_level(&b.parse_details, &h.parse_details, &mut changes);
            if changes.is_empty() {
                continue;
            }
            changed_card_set.insert(h.card_name.clone());
            for ch in changes {
                let before_t = template(&ch.before, &h.card_name);
                let after_t = template(&ch.after, &h.card_name);
                let sig = (
                    ch.category.to_string(),
                    ch.label.clone(),
                    ch.kind.label().to_string(),
                    ch.key.clone(),
                    before_t.clone(),
                    after_t.clone(),
                );
                let cluster = sig_to_cluster.entry(sig).or_insert_with(|| Cluster {
                    category: ch.category,
                    label: ch.label.clone(),
                    kind: ch.kind,
                    key: ch.key.clone(),
                    before: before_t,
                    after: after_t,
                    cards: Vec::new(),
                });
                cluster.cards.push(h.card_name.clone());
            }
        }
        for (k, b) in &bmap {
            if !hmap.contains_key(k) {
                removed_cards.push(b.card_name.clone());
            }
        }
    
        let mut clusters: Vec<Cluster> = sig_to_cluster.into_values().collect();
        // Dedup card lists within a cluster (a card may hit the same signature
        // more than once via repeated structures) and sort by impact.
        for c in &mut clusters {
            c.cards.sort();
            c.cards.dedup();
        }
        clusters.sort_by(|a, b| {
            b.cards
                .len()
                .cmp(&a.cards.len())
                .then(a.label.cmp(&b.label))
        });
    OldComparison {
        clusters,
        changed_cards: changed_card_set.len(),
        oracle_changed,
        added_cards,
        removed_cards,
    }
}

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

    // OLD (pre-fix) expectations: the reported bug reproduces.
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
    failures += !o2 as i32;

    if failures > 0 {
        eprintln!("{} assertion(s) FAILED", failures);
        std::process::exit(1);
    }
    println!("all assertions passed");
}
