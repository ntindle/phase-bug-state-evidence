# Forensic re-analysis — #6916 v0.114.0 run `run-6916-reval-v01140-20261020-2141`

## Why the scenario's A5 failed (and why the failure is wrong)

The scenario's `evaluate()` asserted:

```
draw_ok = (hand_now[1] == pre["hand"][1] + 1)   # 7 == 8 + 1 -> False
```

`pre["hand"][1]` (8) was captured from the viewer state at the moment P1
submitted the `ActivateAbility` action. Between that capture and the
`post.json` export, the driver itself moved two P1 hand cards (both in
`wire_log.jsonl`):

| t (rel. activation) | event | P1 hand effect |
|---|---|---|
| +0.82s | `play_land` P1 oid 85 | 8 -> 7 |
| +4.06s | `handsize_discard` P1, 1 pick (`GAOSHQ.0.234.s0`) | 8 -> 7 at end of turn |

The discard firing with exactly 1 pick (`n = len(hand) - 7 = 1`) proves P1's
hand was **8** at end of turn — i.e. something drew P1 a card after the land
play. The only P1-affecting draw in that window is the resolving Xantcha
`{3}` activation (the ability demonstrably resolved: controller P2 went
40 -> 38 life, stack empty in `post.json`).

## Card-identity proof (authoritative exports)

- `mid.json` (turn 9, P0 PreCombatMain): P1 hand = {67, 68, 82, 85, 86, 87, 114}
- `post.json` (turn 11, P2 Draw phase): P1 hand = {67, 68, 69, 82, 87, 114, 115}
- New cards: **69, 115** (2 draws). Removed: 85 (land play), 86 (discard).
- P1 had exactly **one** natural draw step in (mid, post] (turn 10; `post`
  is P2's turn-11 Draw phase, so no P1 turn-11 draw exists yet).

2 observed draws − 1 natural draw = **1 activation draw for P1**. The
"You draw a card" half of the resolved ability went to the activating
player, exactly as the oracle text requires ("you" = the ability's
controller = the activator, CR 109.5/602.2a). The life half also resolved
correctly (P2 40 -> 38, others unchanged).

Correct accounting: 8 (pre_act) − 1 (land) + 1 (activation draw) − 1
(discard) = 7 = observed post hand. **A5 passes.**

## The v0.110.0 run had the same artifact

Re-running this accounting on `run-6916-reval-v01100-20261014-2255`
(the run behind the maintained comment's "mis-bound" section):

- P1 played land oid 61 at t0+1.01s, discarded 1 at t0+4.46s (same pattern).
- mid P1 hand {61,62,63,82,84,94,105} -> post P1 hand
  {62,63,82,91,94,102,105}: new cards **91, 102** = turn-16 natural draw +
  activation draw. The activation draw resolved on v0.110.0 too.
- P2's 7 -> 8 was P2's own turn-17 natural draw
  (`cards_drawn_this_turn: {'2': [133]}` in post.json) — not a mis-bound
  effect. The maintained comment's "mis-bound" conclusion was this same
  driver-assertion artifact, not an engine defect.

## Corrected verdict

`assertions.json` (scenario-emitted) is preserved unchanged; the corrected
record is `assertions_corrected.json`. Corrected assertions: A1–A6 all
**passed** → verdict **`not-reproduced`**.

Both reported claims are absent on v0.114.0 (build `ac67cbb`, protocol 153):
(a) the entry opponent-choice prompt was offered and honored (P2 chosen,
Xantcha entered owned-by-0 controlled-by-2); (b) the `{3}` "any player may
activate" ability was offered to non-controller P1, submitted, and resolved
with the full correct effect. The v0.81.3–v0.103.0 failure (ability never
offered to any seat) is gone, and the v0.110.0 "mis-bound" reading is
retracted as a measurement artifact.
