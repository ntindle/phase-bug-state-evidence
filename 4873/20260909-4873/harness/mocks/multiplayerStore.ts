// Only the names used by NamedChoiceModal's player-choice branch are mocked.
export function useMultiplayerStore<T>(selector: (s: unknown) => T): T {
  return selector({ playerAvatars: new Map() });
}

export function getPlayerDisplayName(playerId: number): string {
  return `Player ${playerId}`;
}
