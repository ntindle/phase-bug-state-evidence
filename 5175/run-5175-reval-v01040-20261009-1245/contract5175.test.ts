// Backfill contract for phase-rs/phase #5175 (issuecomment-5610054092).
// Bug: `additionalCostChoices` Choice branch indexes cost.data[1] straight;
// a short (malformed) engine payload makes formatAbilityCost(undefined)
// throw TypeError (undefined.type) and hard-crash the cost-choice modal.
// PR #5175 (open, unmerged) guards each option with "Pay"/"Decline" fallbacks.
//
// Mode is selected by env CONTRACT_MODE: "mainline" expects the crash
// (bug reproduced); "guarded" expects the fallback (guard variant works).
import { describe, expect, it } from "vitest";

import type { AdditionalCost } from "../../adapter/types.ts";
import { additionalCostChoices } from "../costLabel.ts";

const MODE = process.env.CONTRACT_MODE ?? "mainline";

const manaCost = {
  type: "Mana",
  cost: { type: "Cost", shards: [], generic: 2 },
};

describe(`#5175 contract (${MODE})`, () => {
  it("A2/A4: well-formed two-cost Choice labels both options from the payload", () => {
    const cost = { type: "Choice", data: [manaCost, manaCost] } as AdditionalCost;
    const { title, options } = additionalCostChoices(cost);
    expect(title).toBe("Choose additional cost");
    const pay = options.find((o) => o.id === "pay")!;
    const decline = options.find((o) => o.id === "decline")!;
    expect(pay.label).toBeTruthy();
    expect(decline.label).toBeTruthy();
    // labels must be identical with or without the guard (no regression on the good path)
    expect(pay.label).toBe(decline.label);
  });

  it("A1 (mainline): short Choice payload THROWS (bug reproduced)", () => {
    if (MODE !== "mainline") return;
    const cost = {
      type: "Choice",
      data: [manaCost],
    } as unknown as AdditionalCost;
    expect(() => additionalCostChoices(cost)).toThrow(TypeError);
  });

  it("A3 (guarded): short Choice payload does NOT throw; fallback labels", () => {
    if (MODE !== "guarded") return;
    const cost = {
      type: "Choice",
      data: [manaCost],
    } as unknown as AdditionalCost;
    let options: { id: string; label: string }[] = [];
    expect(() => {
      options = additionalCostChoices(cost).options;
    }).not.toThrow();
    const pay = options.find((o) => o.id === "pay")!;
    const decline = options.find((o) => o.id === "decline")!;
    expect(pay.label).toBeTruthy(); // data[0] present -> real mana label
    expect(decline.label).toBe("Decline");
  });

  it("A3b (guarded): empty Choice payload falls back to Pay/Decline", () => {
    if (MODE !== "guarded") return;
    const cost = {
      type: "Choice",
      data: [],
    } as unknown as AdditionalCost;
    let options: { id: string; label: string }[] = [];
    expect(() => {
      options = additionalCostChoices(cost).options;
    }).not.toThrow();
    const pay = options.find((o) => o.id === "pay")!;
    const decline = options.find((o) => o.id === "decline")!;
    expect(pay.label).toBe("Pay");
    expect(decline.label).toBe("Decline");
  });
});
