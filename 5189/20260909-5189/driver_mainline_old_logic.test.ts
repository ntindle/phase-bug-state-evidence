/**
 * Driver test for phase-rs/phase#5189.
 *
 * The old inline gesture handler in client/src/hooks/useKeyboardShortcuts.ts
 * (verified verbatim from mainline@a0f9c55 below) is not exported as a pure
 * function, so this test replicates it EXACTLY as shipped: the same closure
 * state (tapCount/lastTap), the same TAP_WINDOW/REQUIRED_FINGERS constants,
 * and the same handler body, with only the terminal side effect
 * (useUiStore.getState().toggleDebugPanel()) replaced by a counter.
 *
 * Defect 1: `if (e.touches.length !== REQUIRED_FINGERS) { tapCount = 0; return; }`
 *   resets progress on every non-3-finger touchstart. On real hardware a
 *   3-finger tap arrives as separate 1-, then 2-, then 3-finger touchstarts,
 *   so the counter can never climb past 1 and the panel never opens.
 * Defect 2: even ignoring defect 1, the gesture fires at `tapCount >= 2`
 *   (a double-tap), not the documented triple-tap.
 */
import { describe, expect, it } from "vitest";

/** Verbatim copy of the old inline handler logic (mainline@a0f9c55). */
function makeOldHandler(onToggle: () => void) {
  let tapCount = 0;
  let lastTap = 0;
  const TAP_WINDOW = 500; // ms between taps
  const REQUIRED_FINGERS = 3;

  const handler = (e: { touches: { length: number } }, now: number) => {
    if (e.touches.length !== REQUIRED_FINGERS) {
      tapCount = 0;
      return;
    }
    if (now - lastTap > TAP_WINDOW) tapCount = 0;
    tapCount++;
    lastTap = now;
    if (tapCount >= 2) {
      tapCount = 0;
      onToggle();
    }
  };
  return handler;
}

/** Feed a sequence of (touchCount, now) touchstarts; return toggle count. */
function runOld(events: Array<[number, number]>): number {
  let toggles = 0;
  const handler = makeOldHandler(() => {
    toggles++;
  });
  for (const [count, now] of events) {
    handler({ touches: { length: count } }, now);
  }
  return toggles;
}

describe("old triple-tap gesture logic (mainline, verbatim)", () => {
  it("D1: real-hardware triple-tap NEVER opens the panel (defect 1)", () => {
    // Each of 3 taps arrives as 1-, then 2-, then 3-finger touchstarts.
    const events: Array<[number, number]> = [
      [1, 0], [2, 5], [3, 10], // tap 1
      [1, 100], [2, 105], [3, 110], // tap 2
      [1, 200], [2, 205], [3, 210], // tap 3
    ];
    expect(runOld(events)).toBe(0);
  });

  it("D2: a mere double 3-finger tap fires the toggle (defect 2)", () => {
    expect(runOld([[3, 0], [3, 100]])).toBe(1);
  });

  it("D1-control: isolated 3-finger events CAN accumulate past 1", () => {
    // Without the interleaved 1-/2-finger events, the counter climbs.
    expect(runOld([[3, 0], [3, 100]])).toBe(1);
  });
});
