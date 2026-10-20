# Source audit — phase-rs/phase#10107 (Infernal Harvest)

Basis: sparse read-only checkout of tag `v0.114.0` (commit `ac67cbb`) — the pinned
release this run validated against. No build was performed; all findings are
static readings of the exact pinned source plus the official signed card-data.json.

## 1. The parser output (official artifact)

`server/releases/v0.114.0/data/card-data.json` (sha256
`7fe886cc031d9bd6532317572f84f65da66941ebdf6a43d337b625fbc369548e`,
matching the release manifest; 35,925 exported faces), card key
`"infernal harvest"`:

- `oracle_text`: "As an additional cost to cast this spell, return X Swamps you
  control to their owner's hand.\nInfernal Harvest deals X damage divided as you
  choose among any number of target creatures."
- `additional_cost`: `{"type":"Required","data":{"type":"EffectCost",
  "effect":{"type":"Bounce","target":{"type":"Typed",
  "type_filters":[{"Subtype":"Swamp"}],"controller":"You","properties":[]},
  "destination":null,"selection":"at_resolution"}}}`

Quantity audit of the `additional_cost` subtree: **zero** quantity-like fields
(`quantity`/`amount`/`count`/`qty`/`number` — none present; the character `X` and
the word `Variable` do not occur anywhere in the subtree). The X binding exists
only in the damage effect (`amount: Ref(Variable X)`) and the multi-target max —
never in the cost. The variable return cost's quantity is fully dropped by the
parser, exactly as the issue reports.

## 2. Why coverage marks it supported (coverage.rs)

`build_additional_cost_items` (coverage.rs:6108):

```rust
AdditionalCost::Required(_) => "AdditionalCost:Required",
...
items.push(ParsedItem {
    category: ParseCategory::Cost,
    label: label.to_string(),
    source_text: None,
    supported: true,   // <-- hardcoded; line 6150
    ...
});
```

The early-return branch above it fires only when
`additional_cost_has_unimplemented` is true, which recurses
`AbilityCost::contains_unimplemented` — true only for `Unimplemented` leaves.
`EffectCost(Effect::Bounce)` is a recognized variant, so the card takes the
hardcoded `supported: true` path.

`collect_ability_cost_missing_parts` (coverage.rs:8923) flags
`AbilityCost::EffectCost { effect }` **only** when the inner effect is
`Effect::Unimplemented`:

```rust
AbilityCost::EffectCost { effect } => {
    if let Effect::Unimplemented { name, .. } = effect.as_ref() {
        // ... push "Cost:{name}" gap
    }
}
_ => {}
```

`Effect::Bounce` is not `Unimplemented` → no gap recorded.

`extract_card_features` (coverage.rs:~9310) emits
`StructuralFeature::AdditionalCost` for any face with an additional cost, and
`StructuralFeature::support` (coverage.rs:9496) classifies it — along with every
other structural feature except two measured-unhandled ones — as
`FeatureSupport::Handled`, unconditionally, regardless of the inner cost shape.

Net: parse detail `AdditionalCost:Required` with `supported: true`, zero gap
contributions → the complete card coverage row must be `supported: true`,
`gap_count: 0`. The coverage machinery never asks whether the effect-cost shape
is actually resolvable as a cost.

## 3. The runtime cannot resolve it (costs.rs)

The shared effect-cost interpreter in `crates/engine/src/game/costs.rs`
implements arms for `Effect::Mana` (fixed production), `Effect::SetTapState`
(tap the granter/attachment host), and counter-placement costs; everything else
falls through (costs.rs:1545):

```rust
_ => {
    return Ok(payment_failed(format!(
        "Effect-as-cost not yet resolvable: {effect:?}"
    )));
}
```

`Effect::Bounce` hits the fallthrough. This confirms the issue's static claim:
Bounce is not one of the implemented effect-cost shapes, so the engine cannot
pay this additional cost — while coverage advertises the card as fully
supported. (No live cast was attempted; the issue explicitly scopes itself to
static parser/coverage/payment-source evidence.)
