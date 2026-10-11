// Driver test for phase-rs/phase#5184: corrupt preset storage must not throw.
// Mirrors the PR's own presets.test.ts cases (content identical) so the
// mainline outcome directly demonstrates the bug the PR claims to fix.
import { afterEach, describe, expect, it } from "vitest";

import { deletePreset, loadPresets, savePreset } from "../presets";

const STORAGE_KEY = "phase-game-presets";

afterEach(() => {
  localStorage.clear();
});

describe("loadPresets (driver, #5184)", () => {
  it("D1 returns the defaults when storage is empty", () => {
    const presets = loadPresets();
    expect(presets.length).toBeGreaterThan(0);
    expect(presets.some((p) => p.id.startsWith("default-"))).toBe(true);
  });

  it("D2 falls back to the defaults on corrupt storage instead of throwing", () => {
    localStorage.setItem(STORAGE_KEY, "{oops");
    expect(() => loadPresets()).not.toThrow();
    expect(loadPresets()).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ id: expect.stringMatching(/^default-/) }),
      ]),
    );
  });

  it("D3 does not let corrupt storage crash savePreset/deletePreset", () => {
    localStorage.setItem(STORAGE_KEY, "not json");
    expect(() =>
      savePreset({
        id: "custom-1",
        name: "My Preset",
        format: "Standard",
        formatConfig: {},
        deckId: null,
        aiDifficulty: "Medium",
        playerCount: 2,
      }),
    ).not.toThrow();
    expect(() => deletePreset("custom-1")).not.toThrow();
  });

  it("D4 returns saved presets when storage holds a valid non-empty array", () => {
    savePreset({
      id: "custom-2",
      name: "Saved",
      format: "Commander",
      formatConfig: {},
      deckId: null,
      aiDifficulty: "Hard",
      playerCount: 4,
    });
    const presets = loadPresets();
    expect(presets.some((p) => p.id === "custom-2")).toBe(true);
  });
});
