import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Wiring check: the PR-variant hook copy (useResumables.pr.ts = mainline
 * useResumables.ts @ aa1fb24 with PR #5187's useResumables hunk applied,
 * adapted for the drifted `publicState.players` naming) must delegate the
 * count to the pure helper instead of the old `liveCount - 1` formula.
 * Source-level check: importing the full hook pulls in react-router +
 * session services, which is not needed to verify the wiring.
 */
const src = readFileSync(join(import.meta.dirname, "useResumables.pr.ts"), "utf8");

describe("PR-variant hook wiring", () => {
  it("imports countLiveOpponents from ./liveOpponents", () => {
    expect(src).toContain('import { countLiveOpponents } from "./liveOpponents";');
  });

  it("computes opponentCount via the helper on the live player list", () => {
    expect(src).toContain("opponentCount: countLiveOpponents(publicState.players),");
  });

  it("no longer contains the undercounting liveCount - 1 formula", () => {
    expect(src).not.toContain("liveCount - 1");
    expect(src).not.toContain("Math.max(0, liveCount - 1)");
  });
});
