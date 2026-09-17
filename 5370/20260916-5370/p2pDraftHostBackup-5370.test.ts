/**
 * Adapted from the regression test in PR #5370
 * (client/src/adapter/__tests__/p2pDraftHostBackup.test.ts, PR head bb2ef2cc).
 *
 * Adaptation notes (2026-09-16):
 * - The PR's original test passed "ROOM01" as roomCode. Current mainline's
 *   P2PDraftHost constructor validates roomCode via parseRoomCode() (5 chars
 *   from ABCDEFGHJKMNPQRSTUVWXYZ23456789) and throws on invalid codes, so
 *   the fixture uses "RMABC" instead. All behavioral assertions are
 *   unchanged: after startDraft(), exactly one POST must go to
 *   <backupEndpoint>/p2p-draft-backup with host_peer_id and a draft_code
 *   matching the current hostDraftCode() shape (^draft-[0-9a-f]{8}$).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../draft-adapter", () => ({
  DraftAdapter: vi.fn().mockImplementation(function () {
    return {};
  }),
  // Mocked on current mainline only: startDraftInner -> loadCardDatabaseForSharedStackBots
  // calls this helper; returning false keeps the test on the non-shared-stack path.
  isSharedStackDistribution: () => false,
}));

vi.mock("../../services/draftPersistence", () => ({
  saveDraftHostSession: vi.fn().mockResolvedValue(undefined),
  clearDraftHostSession: vi.fn(),
}));

import { P2PDraftHost } from "../p2p-draft-host";
import type { DraftPlayerView } from "../draft-adapter";
import { saveDraftHostSession } from "../../services/draftPersistence";

describe("P2PDraftHost server backup (#5370)", () => {
  const BACKUP_URL = "https://backup.example";
  const draftingView = {
    status: "Drafting",
    pick_number: 1,
    seats: [
      { seat_index: 0, is_bot: false, display_name: "Host", picks: [] },
      { seat_index: 1, is_bot: true, display_name: "Bot 1", picks: [] },
    ],
    current_pack: [],
    pairings: [],
    current_round: 1,
  } as unknown as DraftPlayerView;

  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue({ ok: true });
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.clearAllMocks();
  });

  function makeHost(): P2PDraftHost {
    return new P2PDraftHost(
      { id: "host-peer-abc" } as never,
      () => () => {},
      { type: "Set", data: { set_pool_json: "{}" } } as never,
      "Premier",
      2,
      "Host",
      "Swiss",
      "Casual",
      undefined,
      "persist-backup-test",
      "RMABC",
      BACKUP_URL,
    );
  }

  function wireAdapter(host: P2PDraftHost): void {
    const adapter = (host as unknown as { adapter: Record<string, unknown> }).adapter;
    adapter.createMultiplayerDraft = vi.fn().mockResolvedValue(undefined);
    // Current mainline's startDraftInner calls ensureProcedure() before
    // creating the draft; the PR base did not, so this mock was not needed
    // in the original test.
    adapter.draftProcedure = vi.fn().mockResolvedValue({
      pod_size: 2,
      human_seats: 2,
      min_pod_size: 2,
      max_pod_size: 8,
      allowed_pod_sizes: [2],
      packs_per_player: 3,
      cards_per_pick: 1,
      pick_selection_mode: "Direct",
    });
    adapter.getViewForSeat = vi.fn(async () => draftingView);
    adapter.exportSession = vi.fn().mockResolvedValue('{"status":"Drafting"}');
  }

  async function flushPersistQueue(host: P2PDraftHost): Promise<void> {
    await (host as unknown as { persistQueue: Promise<void> }).persistQueue;
  }

  it("uploads the first backup snapshot when the draft starts", async () => {
    const host = makeHost();
    wireAdapter(host);

    await host.startDraft();
    await flushPersistQueue(host);

    expect(saveDraftHostSession).toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledWith(
      `${BACKUP_URL}/p2p-draft-backup`,
      expect.objectContaining({ method: "POST" }),
    );

    const body = JSON.parse(fetchMock.mock.calls[0][1].body as string);
    expect(body.host_peer_id).toBe("host-peer-abc");
    expect(body.draft_code).toMatch(/^draft-[0-9a-f]{8}$/);
  });
});
