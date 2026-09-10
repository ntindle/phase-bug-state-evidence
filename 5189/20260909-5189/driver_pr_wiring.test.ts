/**
 * Wiring driver for phase-rs/phase#5189 (PR variant).
 *
 * Mirrors the PATCHED useKeyboardShortcuts handler exactly:
 *
 *   let tapState = initialTripleTapState();
 *   const handler = (e: TouchEvent) => {
 *     const { state, triggered } = advanceTripleTap(tapState, e.touches.length, Date.now());
 *     tapState = state;
 *     if (triggered) toggleDebugPanel();
 *   };
 *
 * Confirms the fix resolves both defects end-to-end through the exact wiring
 * the hook uses.
 */
import { describe, expect, it } from "vitest";

import {
  advanceTripleTap,
  initialTripleTapState,
} from "../tripleTap";

/** Feed a sequence of (touchCount, now) touchstarts; return toggle count. */
function runPatched(events: Array<[number, number]>): number {
  let toggles = 0;
  let tapState = initialTripleTapState();
  for (const [count, now] of events) {
    const { state, triggered } = advanceTripleTap(tapState, count, now);
    tapState = state;
    if (triggered) toggles++;
  }
  return toggles;
}

describe("patched triple-tap wiring (PR variant)", () => {
  it("W1: real-hardware triple-tap now opens the panel exactly once", () => {
    const events: Array<[number, number]> = [
      [1, 0], [2, 5], [3, 10], // tap 1
      [1, 100], [2, 105], [3, 110], // tap 2
      [1, 200], [2, 205], [3, 210], // tap 3 -> trigger
    ];
    expect(runPatched(events)).toBe(1);
  });

  it("W2: a mere double-tap no longer fires the toggle", () => {
    expect(runPatched([[3, 0], [3, 100]])).toBe(0);
  });

  it("W3: the leading 1-/2-finger events of each tap do not reset progress", () => {
    // Tap 1's leading events arrive before any 3-finger tap; they must not
    // poison the window for the gesture that follows.
    const events: Array<[number, number]> = [
      [1, 0], [2, 5],
      [3, 100], [3, 200], [3, 300], // three clean taps -> trigger
    ];
    expect(runPatched(events)).toBe(1);
  });
});
