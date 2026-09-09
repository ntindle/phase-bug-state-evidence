// Mock dispatch: record GameActions on window for the CDP driver to assert.
export function useGameDispatch() {
  return (action: unknown) => {
    window.__payloads.push(action);
    window.__payloadListeners.forEach((fn) => fn());
    return Promise.resolve();
  };
}

declare global {
  interface Window {
    __payloads: unknown[];
    __payloadListeners: Array<() => void>;
  }
}
