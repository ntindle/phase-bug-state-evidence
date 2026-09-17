//! Reproduction harness for phase-rs/phase issue #6337 — re-validation on v0.85.0.
//!
//! "Draft color selection is non-deterministic (missing HashMap tie-break)"
//!
//! On v0.85.0 the reported defect partially changed shape:
//!
//! * `color_preference` (crates/draft-wasm/src/bot_ai.rs:304) now delegates to
//!   `draft_eval::dominant_colors` (crates/phase-ai/src/draft_eval.rs:223),
//!   which sorts by count THEN color name — a total order, deterministic.
//! * `find_best_colors` (crates/draft-wasm/src/suggest.rs:345, sort at :367)
//!   still sorts by score only, with no secondary key — the defective sort
//!   from the original report, byte-identical to v0.78.0.
//!
//! The functions below are copied VERBATIM from the v0.85.0 sources, with only
//! these minimal adaptations:
//!
//! * `DraftCardInstance` is replaced by the local `DraftCard` stub carrying the
//!   same `colors` field shape (elements expose `.as_str()`), because the real
//!   type comes from the `draft_core` crate and is irrelevant to the sorting
//!   behavior under test.
//! * `CardDatabase` is replaced by `()`; the real `score_card` is replaced by a
//!   stub returning a constant 1.0 per card. This is exactly the issue's stated
//!   repro condition: "a pool where 2+ colors tie on pip count (e.g. one card
//!   of each of W/U/B/R/G)". With a constant per-card score every color ties,
//!   so the defective sort (score-only comparison, no tie-break) is exercised
//!   precisely as reported. The HashMap tally, iteration, stable sort without
//!   a tie-break, and `.take(2)` are byte-identical logic to the release.
//! * `dominant_colors` is copied verbatim including its `&[&[String]]`
//!   signature (called with the same shape `color_preference` passes:
//!   one `&[String]` per card, `min_samples = 3`).
//!
//! `find_best_colors_fixed` is a CONTROL: the same function with the in-tree
//! house-style tie-break (`.then_with(|| a.0.cmp(b.0))`, cf. `distribute_lands`
//! in suggest.rs line 713) added. It must be stable across runs, just like
//! `dominant_colors`.

use std::collections::HashMap;

#[derive(Clone)]
struct Color(&'static str);
impl Color {
    fn as_str(&self) -> &str {
        self.0
    }
}

#[derive(Clone)]
struct DraftCard {
    colors: Vec<Color>,
}

type CardDatabase = ();

/// Stub standing in for the real score_card: constant 1.0 per card.
/// Substitution documented above; it produces the exact tie condition the
/// issue's repro describes (one card of each of W/U/B/R/G).
fn score_card(_card: &DraftCard, _db: Option<&CardDatabase>) -> f64 {
    1.0
}

// ===== BEGIN verbatim copy of find_best_colors from
// crates/draft-wasm/src/suggest.rs @ v0.85.0 (only DraftCardInstance -> DraftCard,
// CardDatabase -> local alias)
// =====
/// Find the 2 strongest colors in the pool by card count weighted by quality.
fn find_best_colors<'a>(pool: &[DraftCard], card_db: Option<&CardDatabase>) -> Vec<&'a str> {
    let mut color_scores: HashMap<&str, f64> = HashMap::new();

    for card in pool {
        let card_score = score_card(card, card_db);
        for color in &card.colors {
            let key = match color.as_str() {
                "W" => "W",
                "U" => "U",
                "B" => "B",
                "R" => "R",
                "G" => "G",
                _ => continue,
            };
            *color_scores.entry(key).or_insert(0.0) += card_score;
        }
    }

    let mut sorted: Vec<(&&str, &f64)> = color_scores.iter().collect();
    sorted.sort_by(|a, b| b.1.partial_cmp(a.1).unwrap_or(std::cmp::Ordering::Equal));

    sorted.iter().take(2).map(|(color, _)| **color).collect()
}
// ===== END verbatim copy =====

// ===== BEGIN verbatim copy of dominant_colors from
// crates/phase-ai/src/draft_eval.rs @ v0.85.0 (signature and body unchanged)
// =====
pub fn dominant_colors(color_lists: &[&[String]], min_samples: usize) -> Vec<String> {
    if color_lists.len() < min_samples {
        return Vec::new();
    }

    let mut counts: HashMap<&str, u32> = HashMap::new();
    for colors in color_lists {
        for color in colors.iter() {
            *counts.entry(color.as_str()).or_insert(0) += 1;
        }
    }

    let mut sorted: Vec<(&str, u32)> = counts.into_iter().collect();
    // Total order: most-played first, then alphabetical. The second key is what
    // makes the result a function of the input rather than of map iteration order.
    sorted.sort_by(|a, b| b.1.cmp(&a.1).then_with(|| a.0.cmp(b.0)));

    sorted
        .iter()
        .take(2)
        .map(|(color, _)| (*color).to_string())
        .collect()
}
// ===== END verbatim copy =====

/// CONTROL: find_best_colors with the in-tree house-style tie-break
/// (`.then_with(|| a.0.cmp(b.0))`, cf. distribute_lands in suggest.rs).
fn find_best_colors_fixed<'a>(pool: &[DraftCard], card_db: Option<&CardDatabase>) -> Vec<&'a str> {
    let mut color_scores: HashMap<&str, f64> = HashMap::new();

    for card in pool {
        let card_score = score_card(card, card_db);
        for color in &card.colors {
            let key = match color.as_str() {
                "W" => "W",
                "U" => "U",
                "B" => "B",
                "R" => "R",
                "G" => "G",
                _ => continue,
            };
            *color_scores.entry(key).or_insert(0.0) += card_score;
        }
    }

    let mut sorted: Vec<(&&str, &f64)> = color_scores.iter().collect();
    sorted.sort_by(|a, b| {
        b.1.partial_cmp(a.1)
            .unwrap_or(std::cmp::Ordering::Equal)
            .then_with(|| a.0.cmp(b.0))
    });

    sorted.iter().take(2).map(|(color, _)| **color).collect()
}

fn main() {
    // The issue's repro pool: one card of each of W/U/B/R/G.
    // Every color ties (count 1 / score 1.0), so the top-2 is decided purely by
    // HashMap iteration order -> run-dependent for the defective sort.
    let pool: Vec<DraftCard> = ["W", "U", "B", "R", "G"]
        .iter()
        .map(|c| DraftCard {
            colors: vec![Color(c)],
        })
        .collect();

    // Same input shape color_preference passes to dominant_colors on v0.85.0:
    // one &[String] per card, min_samples = 3.
    let color_strings: Vec<Vec<String>> = ["W", "U", "B", "R", "G"]
        .iter()
        .map(|c| vec![c.to_string()])
        .collect();
    let color_slices: Vec<&[String]> = color_strings.iter().map(|v| v.as_slice()).collect();

    let fbc = find_best_colors(&pool, None);
    let dom = dominant_colors(&color_slices, 3);
    let fixed = find_best_colors_fixed(&pool, None);

    println!(
        "find_best_colors={} dominant_colors={} control_fixed={}",
        fbc.join(","),
        dom.join(","),
        fixed.join(",")
    );
}
