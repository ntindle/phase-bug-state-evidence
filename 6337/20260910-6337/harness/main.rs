//! Reproduction harness for phase-rs/phase issue #6337.
//!
//! "Draft color selection is non-deterministic (missing HashMap tie-break)"
//!
//! The two functions below are copied VERBATIM from the pinned release
//! v0.78.0 sources (crates/draft-wasm/src/bot_ai.rs and
//! crates/draft-wasm/src/suggest.rs), with only these minimal adaptations:
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
//!
//! `find_best_colors_fixed` is a CONTROL: the same function with the in-tree
//! house-style tie-break (`.then_with(|| a.0.cmp(b.0))`, cf. `distribute_lands`
//! in suggest.rs line 713) added. It must be stable across runs.

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

// ===== BEGIN verbatim copy of color_preference from
// crates/draft-wasm/src/bot_ai.rs @ v0.78.0 (only DraftCardInstance -> DraftCard)
// =====
/// Extract the 1-2 most common colors from prior picks.
/// Returns empty vec if no clear preference (early draft).
fn color_preference(prior_picks: &[DraftCard]) -> Vec<String> {
    if prior_picks.len() < 3 {
        return Vec::new();
    }

    let mut counts: HashMap<&str, u32> = HashMap::new();
    for card in prior_picks {
        for color in &card.colors {
            *counts.entry(color.as_str()).or_insert(0) += 1;
        }
    }

    if counts.is_empty() {
        return Vec::new();
    }

    let mut sorted: Vec<(&&str, &u32)> = counts.iter().collect();
    sorted.sort_by(|a, b| b.1.cmp(a.1));

    // Take top 2 colors
    sorted
        .iter()
        .take(2)
        .map(|(color, _)| color.to_string())
        .collect()
}
// ===== END verbatim copy =====

// ===== BEGIN verbatim copy of find_best_colors from
// crates/draft-wasm/src/suggest.rs @ v0.78.0 (only DraftCardInstance -> DraftCard,
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
    sorted.sort_by(|a, b| {
        b.1.partial_cmp(a.1)
            .unwrap_or(std::cmp::Ordering::Equal)
    });

    sorted.iter().take(2).map(|(color, _)| **color).collect()
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
    // HashMap iteration order -> run-dependent.
    let pool: Vec<DraftCard> = ["W", "U", "B", "R", "G"]
        .iter()
        .map(|c| DraftCard {
            colors: vec![Color(c)],
        })
        .collect();

    let cp = color_preference(&pool);
    let fbc = find_best_colors(&pool, None);
    let fixed = find_best_colors_fixed(&pool, None);

    println!(
        "color_preference={} find_best_colors={} control_fixed={}",
        cp.join(","),
        fbc.join(","),
        fixed.join(",")
    );
}
