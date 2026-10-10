// Mock multiplayer store: local two-human game viewed from seat 0 (P0, the
// responding opponent). Only the selectors the components under test read.
export function useMultiplayerStore<T>(selector: (s: unknown) => T): T {
  return selector({ isSpectator: false, activePlayerId: 0, playerAvatars: new Map() });
}

export function getPlayerDisplayName(playerId: number): string {
  return `Player ${playerId}`;
}

export function getOpponentDisplayName(playerId: number): string {
  return getPlayerDisplayName(playerId);
}

export const MAX_USER_LOBBY_SOURCES = 8;
export const FORMAT_DEFAULTS: Record<string, unknown> = {};
export function lobbySources(): never[] {
  return [];
}
