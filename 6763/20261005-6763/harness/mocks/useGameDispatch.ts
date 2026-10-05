// Mock dispatch: record GameActions on window for the CDP driver; never
// touches a transport (the harness seeds the store directly).
export function useGameDispatch() {
  return (action: unknown) => {
    window.__payloads.push(action);
    return Promise.resolve();
  };
}

declare global {
  interface Window {
    __payloads: unknown[];
  }
}

export const currentSnapshot = null;
