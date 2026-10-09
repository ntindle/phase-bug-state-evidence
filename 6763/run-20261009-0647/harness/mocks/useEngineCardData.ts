// Mock engine card-data hook: the harness never needs oracle text — the
// CardArtFallback tiles render name-only, which is what the assertions
// compare. Avoids pulling in @wasm/engine via services/engineRuntime.
export function useEngineCardData(_cardName: string | null): null {
  return null;
}

export function useCardParseDetails(_cardName: string | null): null {
  return null;
}

export function useCardRulings(_cardName: string | null): never[] {
  return [];
}
