/**
 * Backfill behavioral test for phase-rs/phase#6659
 * "Deck builder copy-limit affordance: alias spellings counted separately,
 *  search-add ungated"
 *
 * Drives the REAL useDeckBuilder hook (client-src/phase-main @ 12a8ef4, the
 * post-#6654 state the issue describes) through its actual handlers.
 *
 * engineRuntime.maxDeckCopies is mocked per name with the REAL engine-resolved
 * values (Nazgul/Nazgûl -> UpTo 9 per the printed Oracle override
 * "A deck can have up to nine cards named Nazgûl."; Standard default 4).
 * The engine itself canonicalizes alias spellings before resolving the ceiling
 * (canonical_deck_count_key folds accents: "Nazgul" and "Nazgûl" merge), so the
 * mock returns the same ceiling for both spellings, as the engine would.
 *
 * Gap 1: client combinedCopyCounts keys by raw entry name, so alias spellings
 *        are counted separately and the `+` affordance stays live past the
 *        real (canonical) ceiling.
 * Gap 2: handleAddCard (the search-result add path) increments an existing
 *        entry without consulting canIncrement, walking past the ceiling.
 * Control: handleIncrementCard (the `+` control gated by #6654) DOES consult
 *        canIncrement — this isolates the bugs to the two reported paths.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";

import type { GameFormat } from "../../../adapter/types";
import type { ScryfallCard } from "../../../services/scryfall";

const cardDataCache = new Map<string, { name: string; cmc: number; color_identity: string[] }>();

vi.mock("../../../services/engineRuntime", () => ({
  isCardCommanderEligibleForFormat: vi.fn(async () => false),
  commanderPartnerCandidates: vi.fn(async () => [] as string[]),
  companionCandidates: vi.fn(async () => [] as string[]),
  isCardCommanderEligible: vi.fn(async () => false),
  // Real engine-resolved ceilings: Nazgûl's printed override (UpTo 9) applies
  // to both alias spellings because the engine canonicalizes before resolving.
  maxDeckCopies: vi.fn(async (name: string) => {
    const key = name.toLowerCase();
    if (key === "nazgul" || key === "nazgûl") return { type: "UpTo", data: 9 };
    return { type: "UpTo", data: 4 };
  }),
  signatureSpellSelectionPolicy: vi.fn(async () => null),
}));

vi.mock("../../../hooks/useDeckCardData", () => ({
  useDeckCardData: () => ({ cardDataCache, cacheCards: vi.fn() }),
}));

vi.mock("../../../hooks/useBracketEstimate", () => ({
  useBracketEstimate: () => ({ estimate: null, unsupported: true }),
}));

vi.mock("../../../hooks/useDecks", () => ({
  loadPreconDeckMap: vi.fn(async () => ({}) ),
}));

vi.mock("../../../services/deckCompatibility", () => ({
  evaluateDeckCompatibility: vi.fn(async () => null),
}));

vi.mock("../../../adapter/wasm-adapter", () => ({
  getSharedAdapter: () => ({}),
}));

import { useDeckBuilder } from "../useDeckBuilder";

/** Evidence dir for state snapshots (set by the backfill runner). */
const EVDIR = process.env.BACKFILL_EVDIR ?? "/tmp/ev-6659";

import { mkdirSync, writeFileSync } from "node:fs";

function dump(name: string, payload: unknown) {
  mkdirSync(EVDIR, { recursive: true });
  writeFileSync(
    `${EVDIR}/${name}`,
    JSON.stringify(payload, null, 2) + "\n",
  );
}

function snapshotOf(result: { current: ReturnType<typeof useDeckBuilder> }) {
  const cur = result.current;
  return {
    main: cur.deck.main,
    canIncrement: {
      Nazgul: cur.canIncrement("Nazgul"),
      "Nazgûl": cur.canIncrement("Nazgûl"),
      "Lightning Bolt": cur.canIncrement("Lightning Bolt"),
    },
  };
}

const BOLT: ScryfallCard = {
  name: "Lightning Bolt",
  mana_cost: "{R}",
  cmc: 1,
  type_line: "Instant",
  color_identity: ["R"],
};

function setup() {
  return renderHook(() =>
    useDeckBuilder({
      format: "Standard" as GameFormat,
      onFormatChange: vi.fn(),
      searchFilters: {} as never,
    }),
  );
}

function mainCount(
  result: { current: ReturnType<typeof useDeckBuilder> },
  name: string,
): number | undefined {
  return result.current.deck.main.find((e) => e.name === name)?.count;
}

describe("useDeckBuilder — #6659 copy-limit affordance gaps", () => {
  beforeEach(() => {
    cardDataCache.clear();
    localStorage.clear();
  });

  afterEach(() => {
    cleanup();
  });

  it("gap 1: alias spellings are counted separately, so the + affordance stays live past the canonical ceiling", async () => {
    const { result } = setup();

    // Text import carrying both spellings (reachable only via text import;
    // deduplicateEntries merges exact matches only, so both survive).
    act(() => {
      result.current.handleImport({
        main: [
          { count: 5, name: "Nazgul" },
          { count: 4, name: "Nazgûl" },
          { count: 4, name: "Lightning Bolt" },
        ],
        sideboard: [],
      });
    });

    // Both spellings survived import as separate entries.
    expect(mainCount(result, "Nazgul")).toBe(5);
    expect(mainCount(result, "Nazgûl")).toBe(4);

    // Canary: the engine limit scan landed once Lightning Bolt's gate closes.
    await waitFor(() => {
      expect(result.current.canIncrement("Lightning Bolt")).toBe(false);
    });

    // BUG (gap 1): canonical total is 5 + 4 = 9, at the ceiling — the
    // affordance should be dead for both spellings. The client counts each
    // raw-name bucket separately (5 < 9, 4 < 9), so `+` stays live.
    expect(result.current.canIncrement("Nazgul")).toBe(true);
    expect(result.current.canIncrement("Nazgûl")).toBe(true);
    dump("gap1_pre.json", {
      stage: "after text import, before increment",
      ...snapshotOf(result),
      canonical_total_nazgul: 9,
      engine_ceiling: 9,
    });

    // And the stale affordance actually increments past the real ceiling.
    await act(async () => {
      result.current.handleIncrementCard("Nazgul", "main");
    });
    expect(mainCount(result, "Nazgul")).toBe(6);
    dump("gap1_post.json", {
      stage: "after handleIncrementCard('Nazgul')",
      ...snapshotOf(result),
      canonical_total_nazgul: 10,
      engine_ceiling: 9,
    });
  });

  it("gap 2: the search-result add path walks past the ceiling", async () => {
    const { result } = setup();

    for (let i = 0; i < 4; i++) {
      act(() => {
        result.current.handleAddCard(BOLT);
      });
    }
    expect(mainCount(result, "Lightning Bolt")).toBe(4);

    // Canary: limits landed — the `+` gate is closed at the ceiling.
    await waitFor(() => {
      expect(result.current.canIncrement("Lightning Bolt")).toBe(false);
    });

    // BUG (gap 2): handleAddCard never consults canIncrement, so the
    // search-result click walks straight past the ceiling.
    dump("gap2_pre.json", {
      stage: "4 Bolts, at ceiling, before 5th search-add click",
      ...snapshotOf(result),
    });
    act(() => {
      result.current.handleAddCard(BOLT);
    });
    expect(mainCount(result, "Lightning Bolt")).toBe(5);
    expect(result.current.canIncrement("Lightning Bolt")).toBe(false);

    // Control: the `+` control (gated by #6654) IS closed — the walk-past is
    // specific to the search-add path.
    await act(async () => {
      result.current.handleIncrementCard("Lightning Bolt", "main");
    });
    expect(mainCount(result, "Lightning Bolt")).toBe(5);
    dump("gap2_post.json", {
      stage: "after 5th handleAddCard + handleIncrementCard attempt",
      ...snapshotOf(result),
    });
  });
});
