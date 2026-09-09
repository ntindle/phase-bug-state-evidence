export function usePlayerAvatarImage() {
  return {
    src: null as string | null,
    isLoading: false,
    advanceFailedSource: (_src: string) => {},
  };
}
