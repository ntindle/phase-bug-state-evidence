import { describe, expect, it } from "vitest";

/**
 * Driver reproducing phase-rs/phase#5187 on current mainline.
 *
 * The computation below is a VERBATIM copy of the opponent-count formula in
 * client/src/hooks/useResumables.ts (mainline @ aa1fb24, lines ~113-117):
 *
 *   const liveCount = publicState.players.filter((p) => !p.is_eliminated).length;
 *   ...
 *   opponentCount: Math.max(0, liveCount - 1),
 *
 * liveCount already excludes eliminated players; the `- 1` assumes seat 0
 * (the local human) is always alive. In a still-resumable FFA match where
 * seat 0 has been eliminated, seat 0 is not in liveCount, so the formula
 * undercounts the opponents by one.
 */
interface P {
  id: number;
  is_eliminated?: boolean;
}

function oldOpponentCount(players: P[]): number {
  const liveCount = players.filter((p) => !p.is_eliminated).length;
  return Math.max(0, liveCount - 1);
}

describe("mainline opponentCount formula (useResumables.ts)", () => {
  it("PR-body example: seat 0 eliminated, two live opponents -> reports 2", () => {
    // players = [{id:0,eliminated}, {id:1,live}, {id:2,live}]
    // Bug: old formula returns 1, correct answer is 2.
    // This assertion FAILING is the reproduction of #5187.
    expect(
      oldOpponentCount([
        { id: 0, is_eliminated: true },
        { id: 1, is_eliminated: false },
        { id: 2, is_eliminated: false },
      ]),
    ).toBe(2);
  });

  it("control: seat 0 alive with two live opponents -> 2 (no undercount)", () => {
    expect(
      oldOpponentCount([
        { id: 0, is_eliminated: false },
        { id: 1, is_eliminated: false },
        { id: 2, is_eliminated: false },
      ]),
    ).toBe(2);
  });

  it("control: lone surviving local human -> 0", () => {
    expect(oldOpponentCount([{ id: 0, is_eliminated: false }])).toBe(0);
  });

  it("control: everyone eliminated -> 0 (no negative)", () => {
    expect(
      oldOpponentCount([
        { id: 0, is_eliminated: true },
        { id: 1, is_eliminated: true },
      ]),
    ).toBe(0);
  });
});
