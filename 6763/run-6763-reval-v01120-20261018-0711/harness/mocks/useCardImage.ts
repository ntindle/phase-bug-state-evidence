// Mock card-image hook: no network in the harness. Every card renders the
// CardArtFallback tile (name text), which is exactly what the assertions
// compare — name-only presentation, no per-instance art.
export function useCardImage(_cardName: string, _options?: unknown) {
  return {
    src: null,
    isLoading: false,
    isRotated: false,
    isFlip: false,
    source: null,
    rungs: [],
    advanceFailedSource: undefined,
  };
}

export function useCardBackImage() {
  return { src: null, isLoading: false, source: null, advanceFailedSource: undefined };
}
