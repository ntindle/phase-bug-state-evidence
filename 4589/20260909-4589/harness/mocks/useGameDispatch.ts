declare global {
  interface Window {
    __payloads: unknown[];
    __payloadListeners: Array<() => void>;
  }
}

// Captures dispatched actions on window.__payloads and notifies listeners.
export function useGameDispatch() {
  return (action: unknown) => {
    window.__payloads = window.__payloads || [];
    window.__payloads.push(action);
    (window.__payloadListeners || []).forEach((fn) => fn());
  };
}
