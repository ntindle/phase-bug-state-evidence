import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import i18n from "i18next";
import { I18nextProvider } from "react-i18next";
import enGame from "../src/i18n/locales/en/game.json";
import "../src/index.css";
import { ReorderableTopChoice } from "../src/components/modal/cardChoice/libraryModals";

i18n.init({
  lng: "en",
  fallbackLng: "en",
  resources: { en: { game: enGame } },
  interpolation: { escapeValue: false },
});

declare global {
  interface Window {
    __payloads: unknown[];
    __payloadListeners: Array<() => void>;
  }
}
window.__payloads = [];
window.__payloadListeners = [];

function PayloadLog() {
  const [items, setItems] = useState<unknown[]>([]);
  useEffect(() => {
    const fn = () => setItems([...window.__payloads]);
    window.__payloadListeners.push(fn);
    return () => {
      window.__payloadListeners = window.__payloadListeners.filter((x) => x !== fn);
    };
  }, []);
  return (
    <div
      id="payload"
      data-testid="payload"
      style={{
        marginTop: 24,
        fontFamily: "monospace",
        whiteSpace: "pre-wrap",
        color: "#7CFC00",
        background: "#111",
        padding: 12,
        minHeight: 60,
      }}
    >
      {items.length === 0
        ? "(no dispatch yet)"
        : items.map((p, i) => (
            <div key={i} data-testid={`payload-${i}`}>
              {JSON.stringify(p)}
            </div>
          ))}
    </div>
  );
}

function App() {
  return (
    <I18nextProvider i18n={i18n}>
      <div style={{ padding: 24, background: "#0b0f1a", minHeight: "100vh", color: "#fff" }}>
        <ReorderableTopChoice
          cards={[1, 2]}
          title="Scry 2"
          subtitle="Look at the top two cards of your library."
          keepLabel="Top"
          restLabel="Bottom"
          reorderHint="reorder the kept cards"
          keepTone="blue"
        />
        <PayloadLog />
      </div>
    </I18nextProvider>
  );
}

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
