# Issue #7509 — backfill analysis (verdict: blocked)

**Issue:** "A parked CR 616.1 replacement choice is destroyed by the spell's own CR 608.2n graveyard move"
**URL:** https://github.com/phase-rs/phase/issues/7509
**Run:** run-7509-v01100-20261015-1425
**Server:** v0.110.0 (build 9bfd12e, protocol 142), pinned release dir verified; latest stable engine release on GitHub is v0.110.0 (published 2026-10-14), so the pin did not advance.
**Date:** 2026-10-15

## Reported defect

`pending_replacement` is a single slot. When a spell's effect parks a CR 616.1
replacement choice mid-resolution, the spell's own "final part of resolution"
move to its owner's graveyard (CR 608.2n) is proposed while that choice is still
parked. If that move also raises an optional replacement, it overwrites the
parked choice — and the card the effect had already selected is silently dropped
(it never leaves the hand, and nothing records it).

Smallest known repro (from the issue): a two-player `GameScenario` with
- P1: a permanent hosting `ReplacementDefinition::new(ReplacementEvent::Moved).destination_zone(Zone::Graveyard).mode(ReplacementMode::Optional { decline: None })` with `valid_card: None`, and
- P0: a sorcery reading "Target player discards two cards at random.",

P1 with four cards in hand. Cast targeting P1, decline every prompt. Two prompts
are raised; only one card leaves P1's hand (two should).

## What the backfill attempted

The backfill drives the public WebSocket protocol (`CreateGameWithSettings` +
`JoinGameWithPassword`) with real cards from the pinned `card-data.json`. The
issue's repro uses engine-internal test fixtures (`GameScenario`,
`ReplacementDefinition`, a custom sorcery text) that are not expressible via the
public protocol, so a real-card equivalent was sought.

**Step 1 — the sorcery.** "Target player discards two cards at random." is
exactly **Hymn to Tourach**, which is implemented (Spell/Discard effect,
`count: Fixed 2`, `random: true`).

**Step 2 — the optional replacement.** The defect requires TWO optional
replacement choices:
  (a) a parked choice for a discarded card, and
  (b) a second choice raised by the spell's own Stack→Graveyard move, which
      overwrites (a).

A full scan of the pinned `card-data.json` (`replacements` key, which is where
the parser stores replacement effects — not `abilities`) found:

- 2710 cards with replacements: 2657 Mandatory, **119 Optional**, 36 MayCost.
- All 119 Optional replacements by event: **Moved: 95, Draw: 19,
  CreateToken: 3, DamageDone: 1, Discard: 1**.
- The 95 Optional "Moved" replacements are ALL enter-the-battlefield
  replacements ("you may have ~ enter as a copy of...", Riot, Unleash) with
  `valid_card: SelfRef`. **None applies to a card moving to the graveyard.**
- The 36 MayCost replacements are ALL "as ~ enters, you may pay..." (shocklands
  et al.). None applies to a spell's graveyard move.
- The 19 Optional "Draw" replacements are dredge/Abundance-style draw
  replacements. Not applicable.
- The **single Optional "Discard" replacement is Library of Leng**
  ("If an effect causes you to discard a card, discard it, but you may put it
  on top of your library instead of into your graveyard").
- **Pulmonic Sliver** ("If this permanent would be put into a graveyard, you
  may put it on top of its owner's library instead") — the only real
  any-permanent optional graveyard replacement — is **unimplemented**
  (`replacements: []`, parser warning `SwallowedClause`).

So: Library of Leng can park choice (a) for a discarded card, but **no
implemented card raises an optional replacement choice for the resolving
spell's own graveyard move**. The spell in the repro must be covered by the
same broad optional replacement (the fixture uses `valid_card: None`), and no
real card provides that. Cipher ("you may exile this spell card encoded...")
is the only real optional spell-graveyard replacement, but no cipher spell
discards at random, and combining two different optionals still leaves the
spell's move without one in any Hymn to Tourach game.

**Step 3 — protocol check.** `CreateGameWithSettings` accepts only decks of
named cards from `card-data.json`. There is no protocol facility for custom
`GameScenario` fixtures, `ReplacementDefinition`s, or custom card text.

## Conclusion

The reported outcome ("two prompts are raised; only one card leaves P1's hand"
because the parked choice is overwritten) **cannot be exercised via the public
protocol with real cards on v0.110.0**: the engine implements no optional
replacement applicable to a resolving spell's 608.2n graveyard move, so the
overwriting second choice can never be raised. The issue's own "smallest known
repro" depends on engine-internal fixture APIs.

Related root cause (spell put into its owner's graveyard before paused
instructions finish — the subject of open fix PR #9728, which also closes
#6360 and #9283) **was** reproduced via WebSocket on v0.110.0 for #6360 on
2026-10-15 (spell object in Graveyard while its Dig keep-choice was pending).
That is a different issue with its own ledger entry and evidence; it is not
relabelled as this bug.

## Verdict: blocked

**Concrete prerequisite:** an implemented optional replacement effect that can
apply to a resolving instant/sorcery's move to its owner's graveyard (e.g.
Pulmonic Sliver's granted ability, or any "if a card would be put into a
graveyard, you may ... instead" for non-Slivers), OR a supported way to drive
engine `GameScenario` fixtures with custom `ReplacementDefinition`s through
the backfill's protocol path.

**Next action that would unblock:** re-run when (i) the card-data parser
implements an optional graveyard-move replacement usable by the spell, (ii) a
fixture-driving facility becomes available to the backfill, or (iii) fix PR
#9728 merges (which would moot the repro — the verdict would then be revisited
as not-reproduced only via a real post-fix scenario, never "fixed").

## Limitations

- Browser UI not exercised; analysis is of the native engine's card data and
  public protocol surface.
- No game was played for this issue; there are no pre/post state snapshots
  because the prerequisite card does not exist in the implemented set.
- The underlying single-slot `pending_replacement` defect is not disputed;
  this verdict reports only that the backfill cannot trigger it with real
  cards on the pinned release.
