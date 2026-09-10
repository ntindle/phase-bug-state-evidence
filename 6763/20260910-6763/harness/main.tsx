import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import i18n from "i18next";
import { I18nextProvider } from "react-i18next";
import enGame from "phase-src/i18n/locales/en/game.json";
import "phase-src/index.css";
import { StackDisplay } from "phase-src/components/stack/StackDisplay.tsx";
import { GraveyardPile } from "phase-src/components/zone/GraveyardPile.tsx";
import { ZoneViewer } from "phase-src/components/zone/ZoneViewer.tsx";
import { objectAnchorSelector } from "phase-src/utils/objectAnchorSelector.ts";
import { useGameStore } from "phase-src/stores/gameStore.ts";

i18n.init({
  lng: "en",
  fallbackLng: "en",
  resources: { en: { game: enGame } },
  interpolation: { escapeValue: false },
});

declare global {
  interface Window {
    __READY: boolean;
    __SEED: {
      variant: string;
      entryId: number | null;
      targetId: number | null;
      gyIds: number[];
    } | null;
    __payloads: unknown[];
    __resolveAnchor: (id: number) => unknown;
    __arcPaths: () => string[];
    __targetLabels: () => string[];
    __openViewer: () => void;
  }
}
window.__payloads = [];
window.__READY = false;
window.__SEED = null;

/** The exact anchor resolution StackTargetArcs performs per frame. */
function resolveAnchor(id: number) {
  const el = document.querySelector(objectAnchorSelector(id));
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return {
    tag: el.tagName,
    pile: el.getAttribute("data-graveyard-pile"),
    objectId: el.getAttribute("data-object-id"),
    grouped: el.getAttribute("data-grouped-ids"),
    rect: {
      x: Math.round(r.x * 10) / 10,
      y: Math.round(r.y * 10) / 10,
      w: Math.round(r.width * 10) / 10,
      h: Math.round(r.height * 10) / 10,
    },
  };
}

function App() {
  const [snap, setSnap] = useState<any>(null);
  const [viewerOpen, setViewerOpen] = useState(false);

  useEffect(() => {
    const variant = new URLSearchParams(location.search).get("variant") ?? "A";
    fetch(`/snapshot_${variant}.json`)
      .then((r) => r.json())
      .then((s) => {
        // Mirror ws-adapter.ts: the engine-authored derived views ride
        // alongside `state` in the envelope and are merged into gameState.
        const gameState = { ...s.state, derived: s.derived ?? s.state.derived };
        useGameStore.setState({
          gameMode: "local",
          gameState,
          waitingFor: gameState.waiting_for,
          legalActions: s.legal_actions ?? [],
          viewerInteraction: s.viewer_interaction ?? null,
          legalActionsByObject: {},
          dispatch: async () => {},
        });
        // Identify the Emperor trigger stack entry and its chosen target,
        // the same way the scenario does on the engine side.
        let entryId: number | null = null;
        let targetId: number | null = null;
        for (const e of s.state.stack ?? []) {
          const blob = JSON.stringify(e).toLowerCase();
          if (!blob.includes("graveyard")) continue;
          const m = JSON.stringify(e?.kind?.data?.ability ?? {}).match(
            /"Object":\s*(\d+)/,
          );
          if (m) {
            entryId = e.id;
            targetId = Number(m[1]);
            break;
          }
        }
        const gyIds: number[] = (s.state.players?.[0]?.graveyard ?? []).map(
          Number,
        );
        window.__SEED = { variant, entryId, targetId, gyIds };
        window.__resolveAnchor = resolveAnchor;
        window.__arcPaths = () =>
          [...document.querySelectorAll("svg path[stroke]")].map((p) =>
            p.getAttribute("d"),
          );
        window.__targetLabels = () => {
          const entries = [...document.querySelectorAll("[data-stack-entry]")];
          const out: string[] = [];
          for (const el of entries) {
            for (const sp of el.querySelectorAll("span")) {
              const t = (sp.textContent ?? "").trim();
              if (t.startsWith("→")) out.push(t);
            }
          }
          return out;
        };
        window.__openViewer = () => setViewerOpen(true);
        setSnap(s);
        window.__READY = true;
      });
  }, []);

  if (!snap) return <div>loading</div>;
  return (
    <I18nextProvider i18n={i18n}>
      <div style={{ width: 1440, height: 900, position: "relative" }}>
        <GraveyardPile playerId={0} onClick={() => setViewerOpen(true)} />
        <StackDisplay effectiveMultiplayerBoardLayout="focused" />
        {viewerOpen && (
          <ZoneViewer
            zone="graveyard"
            playerId={0}
            onClose={() => setViewerOpen(false)}
          />
        )}
      </div>
    </I18nextProvider>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
