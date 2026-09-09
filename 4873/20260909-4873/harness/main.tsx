import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import i18n from "i18next";
import { I18nextProvider } from "react-i18next";
import enGame from "phase-src/i18n/locales/en/game.json";
import "phase-src/index.css";
import { NamedChoiceModal } from "phase-src/components/modal/NamedChoiceModal.tsx";
import { CREATURE_TYPES } from "./options";

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

function App() {
  const data = {
    player: 0,
    choice_type: "CreatureType",
    options: CREATURE_TYPES,
  } as never;
  return (
    <I18nextProvider i18n={i18n}>
      <NamedChoiceModal data={data} />
    </I18nextProvider>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
