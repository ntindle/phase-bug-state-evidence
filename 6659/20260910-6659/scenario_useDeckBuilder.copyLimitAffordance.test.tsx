import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";

import type { GameFormat } from "../../../adapter/types";
import type { ScryfallCard } from "../../../services/scryfall";
import { parseDeckFile } from "../../../services/deckParser";
import { maxDeckCopies } from "../../../services/engineRuntime";

/**
 * Backfill evidence for phase-rs/phase#6659 — "Deck builder copy-limit
 * affordance: alias spellings counted separately, search-add ungated".
 *
 * Two stale-affordance gaps (neither can produce an illegal *playable* deck —
 * the engine's `copy_limit_violations` canonicalizes and still rejects):
 *
 *  Gap 1 — `combinedCopyCounts` in useDeckBuilder.ts keys by the raw entry
 *  name, while the engine's `max_deck_copies` canonicalizes through
 *  `canonical_deck_count_key`. A text-imported deck carrying both `Nazgul`
 *  and `Nazgûl` lets each spelling climb to nine, so the `+` stays live past
 *  the real ceiling.
 *
 *  Gap 2 — `handleAddCard` (the search-result add path) increments an
 *  existing main-deck entry without consulting `canIncrement`, so repeated
 *  clicks walk past the ceiling. #6654 gated only the new `+` control on the
 *  deck-list rows.
 *
 * The engine ceiling surface (`maxDeckCopies`) is mocked to return exactly
 * what the engine resolves: 9 for both Nazgûl spellings (printed override
 * "A deck can have up to nine cards named Nazgûl.", verified in the pinned
 * card-data.json whose canonical key is the lowercase `nazgûl`; the engine
 * canonicalizes engine-side per the issue triage) and 4 for Lightning Bolt
 * (the format default). The WASM engine cannot be built on this host (no
 * Rust toolchain; client-only sparse checkout), and ceiling resolution is
 * explicitly not the component under test — only the client aggregation
 * and the ungated add path are.
 */

// Engine-faithful ceilings: the real engine canonicalizes both spellings to
// the same card and returns the printed override (9); Lightning Bolt gets
// the format default (4).
const CEILINGS: Record<string, { type: "Limited"; data: number }> = {
  Nazgul: { type: "Limited", data: 9 },
  "Nazgûl": { type: "Limited", data: 9 },
  "Lightning Bolt": { type: "Limited", data: 4 },
};

vi.mock("../../../services/engineRuntime", () => ({
  isCardCommanderEligibleForFormat: vi.fn(async () => false),
  commanderPartnerCandidates: vi.fn(async () => [] as string[]),
  companionCandidates: vi.fn(async () => [] as string[]),
  isCardCommanderEligible: vi.fn(async () => false),
  maxDeckCopies: vi.fn(
    async (name: string) =>
      CEILINGS[name] ?? { type: "Limited", data: 4 },
  ),
  signatureSpellSelectionPolicy: vi.fn(async () => null),
}));

vi.mock("../../../hooks/useDeckCardData", () => ({
  useDeckCardData: () => ({ cardDataCache: new Map(), cacheCards: vi.fn() }),
}));

vi.mock("../../../hooks/useBracketEstimate", () => ({
  useBracketEstimate: () => ({ estimate: null, unsupported: true }),
}));

vi.mock("../../../hooks/useDecks", () => ({
  loadPreconDeckMap: vi.fn(async () => ({})),
}));

vi.mock("../../../services/deckCompatibility", () => ({
  evaluateDeckCompatibility: vi.fn(async () => null),
}));

vi.mock("../../../adapter/wasm-adapter", () => ({
  getSharedAdapter: () => ({}),
}));

import { useDeckBuilder } from "../useDeckBuilder";

type HookResult = { current: ReturnType<typeof useDeckBuilder> };

describe("useDeckBuilder — #6659 copy-limit affordance gaps", () => {
  function setup() {
    return renderHook(() =>
      useDeckBuilder({
        format: "Modern" as GameFormat,
        onFormatChange: vi.fn(),
        searchFilters: {} as never,
      }),
    );
  }

  /** Import a deck, then wait until the engine copy-limit scan has landed. */
  async function importAndSettle(
    result: HookResult,
    main: Array<{ count: number; name: string }>,
  ) {
    act(() => {
      result.current.handleImport({ main, sideboard: [] });
    });
    await waitFor(() => {
      const names = vi
        .mocked(maxDeckCopies)
        .mock.calls.map((call) => call[0] as string);
      for (const entry of main) expect(names).toContain(entry.name);
    });
    // Flush the Promise.all(...).then(setCopyLimits) microtask chain so the
    // resolved ceilings are visible to canIncrement before we read it.
    await act(async () => {});
  }

  function mainCount(result: HookResult, name: string): number | undefined {
    return result.current.deck.main.find((e) => e.name === name)?.count;
  }

  beforeEach(() => {
    vi.mocked(maxDeckCopies).mockClear();
    localStorage.clear();
  });

  afterEach(() => {
    cleanup();
  });

  it("text import preserves both alias spellings as separate entries (reachable path)", () => {
    const parsed = parseDeckFile("5 Nazgul\n4 Nazgûl\n");
    const entries = parsed.main.map((e) => `${e.count}x ${e.name}`);
    console.log(`[6659] text-import entries: ${JSON.stringify(entries)}`);
    // deduplicateEntries merges exact matches only, so the two spellings
    // survive as separate entries — this is how the divergence is reachable.
    expect(parsed.main).toHaveLength(2);
    expect(mainCount({ current: { deck: parsed } } as HookResult, "Nazgul")).toBe(5);
    expect(mainCount({ current: { deck: parsed } } as HookResult, "Nazgûl")).toBe(4);
  });

  it("gap 1: alias spellings are counted separately, leaving the + affordance live past the real ceiling", async () => {
    const { result } = setup();
    await importAndSettle(result, [
      { count: 5, name: "Nazgul" },
      { count: 4, name: "Nazgûl" },
    ]);

    // Sanity: both spellings survived the import as separate entries.
    expect(result.current.deck.main).toHaveLength(2);

    // 5 + 4 = 9 reaches the engine-resolved ceiling of nine. Correct
    // behavior: the increment affordance is dead for both spellings.
    const nazgulGate = result.current.canIncrement("Nazgul");
    const nazgulAccentGate = result.current.canIncrement("Nazgûl");
    console.log(
      `[6659] gap1 canIncrement at 5x Nazgul + 4x Nazgûl (combined 9, ceiling 9): ` +
        `Nazgul=${nazgulGate}, Nazgûl=${nazgulAccentGate} (expected false/false)`,
    );
    expect(nazgulGate).toBe(false);
    expect(nazgulAccentGate).toBe(false);
  });

  it("gap 1: each spelling can climb to nine, blowing the real ceiling", async () => {
    const { result } = setup();
    await importAndSettle(result, [
      { count: 5, name: "Nazgul" },
      { count: 4, name: "Nazgûl" },
    ]);

    // Drive the deck-list row `+` (handleIncrementCard) for the accented
    // spelling five times. Correct behavior: increments stop once the
    // COMBINED count reaches nine. Buggy behavior: each spelling climbs to
    // its own nine.
    await act(async () => {
      for (let i = 0; i < 5; i++) {
        result.current.handleIncrementCard("Nazgûl", "main");
      }
    });
    const plain = mainCount(result, "Nazgul") ?? 0;
    const accented = mainCount(result, "Nazgûl") ?? 0;
    const combined = plain + accented;
    console.log(
      `[6659] gap1 after 5 row increments: Nazgul=${plain}, Nazgûl=${accented}, ` +
        `combined=${combined} (ceiling 9; expected combined <= 9)`,
    );
    expect(combined).toBeLessThanOrEqual(9);
  });

  it("gap 2: the search-result add path walks past the ceiling", async () => {
    const { result } = setup();
    await importAndSettle(result, [{ count: 4, name: "Lightning Bolt" }]);

    // Control: the deck-list row `+` IS gated by #6654 — incrementing there
    // at the ceiling is refused.
    expect(result.current.canIncrement("Lightning Bolt")).toBe(false);
    act(() => {
      result.current.handleIncrementCard("Lightning Bolt", "main");
    });
    expect(mainCount(result, "Lightning Bolt")).toBe(4);

    // Bug: the search-result add path (handleAddCard) never consults
    // canIncrement, so repeated clicks walk past the ceiling.
    const bolt = { name: "Lightning Bolt" } as unknown as ScryfallCard;
    act(() => {
      result.current.handleAddCard(bolt);
      result.current.handleAddCard(bolt);
      result.current.handleAddCard(bolt);
    });
    const final = mainCount(result, "Lightning Bolt");
    console.log(
      `[6659] gap2 Lightning Bolt count after 3 search-adds at ceiling 4: ${final} (expected 4)`,
    );
    expect(final).toBe(4);
  });
});
