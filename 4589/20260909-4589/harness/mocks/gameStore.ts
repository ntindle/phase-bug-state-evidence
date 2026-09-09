type Obj = { id: number; name: string };

const objects: Record<number, Obj> = {
  1: { id: 1, name: "Island" },
  2: { id: 2, name: "Mountain" },
};

// Mimics zustand selector usage: useGameStore((s) => s.gameState?.objects)
export function useGameStore<T>(selector: (s: unknown) => T): T {
  return selector({ gameState: { objects } });
}
