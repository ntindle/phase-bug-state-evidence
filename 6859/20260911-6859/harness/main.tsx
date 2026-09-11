import React, { useMemo } from "react";
import { createRoot } from "react-dom/client";
import i18n from "i18next";
import { I18nextProvider } from "react-i18next";
import enGame from "phase-src/i18n/locales/en/game.json";
import enCommon from "phase-src/i18n/locales/en/common.json";
import "phase-src/index.css";

import { TargetingOverlay } from "phase-src/components/targeting/TargetingOverlay.tsx";
import { OpponentHud } from "phase-src/components/hud/OpponentHud.tsx";
import { useResolvedGridRows } from "phase-src/hooks/useResolvedGridRows.ts";
import { useGameStore } from "phase-src/stores/gameStore.ts";
import { useMultiplayerStore } from "phase-src/stores/multiplayerStore.ts";
import { useUiStore } from "phase-src/stores/uiStore.ts";
import { usePreferencesStore } from "phase-src/stores/preferencesStore.ts";

i18n.init({
  lng: "en",
  fallbackLng: "en",
  resources: { en: { game: enGame, common: enCommon } },
  interpolation: { escapeValue: false },
});

declare global {
  interface Window {
    __actions: unknown[];
    __geom: () => Record<string, unknown>;
    __ready: boolean;
  }
}
window.__actions = [];
window.__ready = false;

const params = new URLSearchParams(window.location.search);
const bandPreset = params.get("band") === "layout2" ? "layout2" : "default";

// ---------------------------------------------------------------------------
// Seeded game state: Ajani, Nacatl Avenger's 0 loyalty ability has resolved its
// token creation and the "deals damage ... to any target" clause now needs a
// target. waitingFor is the engine's TargetSelection with the triage-noted
// "any target" classification (ObjectsAndPlayers / Object).
// ---------------------------------------------------------------------------
const AJANI_OID = 42;

const gameStateSeed = {
  players: [
    {
      id: 0,
      life: 20,
      name: "You",
      poison_counters: 0,
      player_counters: {},
      speed: 0,
      status: { type: "Active" },
      mana_pool: { mana: [] },
      library: [],
      hand: [],
      graveyard: [],
      has_drawn_this_turn: true,
      lands_played_this_turn: 0,
      turns_taken: 4,
    },
    {
      id: 1,
      life: 17,
      name: "Rival",
      poison_counters: 0,
      player_counters: {},
      speed: 0,
      status: { type: "Active" },
      mana_pool: { mana: [] },
      library: [],
      hand: [],
      graveyard: [],
      has_drawn_this_turn: true,
      lands_played_this_turn: 0,
      turns_taken: 4,
    },
  ],
  objects: {
    [AJANI_OID]: {
      name: "Ajani, Nacatl Avenger",
      controller: 0,
      zone: "Battlefield",
    },
  },
  stack: [],
  seat_order: [0, 1],
  active_player: 0,
  turn_number: 9,
  eliminated_players: [],
  combat: { attackers: [] },
  format_config: { team_based: false },
  match_config: { match_type: "Bo1" },
  derived: {
    current_target_kind: { type: "ObjectsAndPlayers", data: { category: "Object" } },
  },
} as never;

const waitingForSeed = {
  type: "TargetSelection",
  data: {
    player: 0,
    // Engine-authored TargetSelection description for the damage clause.
    description:
      "Choose any target. ~ deals damage equal to the number of creatures you control to that target.",
    pending_cast: {
      object_id: AJANI_OID,
      card_id: 0,
      ability: {
        description:
          "Create a 2/1 white Cat Warrior creature token. When you do, if you control a red permanent other than Ajani, he deals damage equal to the number of creatures you control to any target.",
      },
      cost: {},
    },
    target_slots: [
      {
        legal_targets: [{ Player: 0 }, { Player: 1 }],
      },
    ],
    selection: {
      current_slot: 0,
      current_legal_targets: [{ Player: 0 }, { Player: 1 }],
    },
  },
} as never;

useGameStore.setState({
  gameMode: "local",
  gameState: gameStateSeed,
  waitingFor: waitingForSeed,
  legalActionsByObject: {},
  dispatch: (async (action: unknown) => {
    window.__actions.push(action);
    return [];
  }) as never,
});

useMultiplayerStore.setState({
  connectionStatus: "connected",
  disconnectedPlayers: new Set<number>(),
  playerNames: new Map<number, string>([[1, "Rival"]]),
  isSpectator: false,
  activePlayerId: null,
} as never);

useUiStore.setState({ focusedOpponent: null } as never);

if (bandPreset === "layout2") {
  // mirrors components/flexlayout/presets.ts layout2 top band
  usePreferencesStore.setState((s) => ({
    flexLayout: {
      ...s.flexLayout,
      gridBands: {
        ...s.flexLayout.gridBands,
        top: { pct: 10, pxCap: 80 },
      },
    },
  }));
}

function rectOf(el: Element | null) {
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return { x: r.x, y: r.y, width: r.width, height: r.height, top: r.top, bottom: r.bottom, left: r.left, right: r.right };
}

function App() {
  // The REAL row resolver the game page uses (default persisted bands +
  // the live compact-height media query).
  const gridTemplateRows = useResolvedGridRows();
  const bands = usePreferencesStore((s) => s.flexLayout.gridBands);

  const geom = useMemo(
    () => () => {
      // The prompt bar: the fixed overlay's child that carries an explicit
      // top (the --game-targeting-prompt-top bar container), then the visible
      // pill inside it.
      const overlay = document.querySelector(".pointer-events-none.fixed.inset-0");
      let bar: Element | null = null;
      let pill: Element | null = null;
      if (overlay) {
        for (const child of Array.from(overlay.children)) {
          if ((child as HTMLElement).style.top !== "") bar = child;
        }
        pill = bar?.querySelector(".rounded-lg.bg-gray-900\\/90") ?? null;
      }
      // Opponent life total: the tabular-nums number inside player 1's HUD.
      const hud = document.querySelector('[data-player-hud="1"]');
      let life: Element | null = null;
      if (hud) {
        for (const el of Array.from(hud.querySelectorAll(".tabular-nums"))) {
          if (el.textContent?.trim() === "17") life = el;
        }
      }
      const promptEl = pill?.querySelector(".text-cyan-400");
      const chooseButtons = overlay
        ? Array.from(overlay.querySelectorAll("button")).filter((b) =>
            b.textContent?.includes("Choose:"),
          ).length
        : 0;
      return {
        band: bandPreset,
        bands,
        gridTemplateRows,
        bar: rectOf(bar),
        pill: rectOf(pill),
        life: rectOf(life),
        hud: rectOf(hud),
        promptText: promptEl?.textContent ?? null,
        chooseButtons,
        viewport: { w: window.innerWidth, h: window.innerHeight },
      };
    },
    [bands, gridTemplateRows],
  );
  React.useEffect(() => {
    window.__geom = geom;
    window.__ready = true;
  }, [geom]);

  return (
    <div
      id="harness-root"
      style={
        {
          width: "100vw",
          height: "100vh",
          background: "#0b1220",
          "--game-targeting-prompt-top": "0.25rem",
        } as React.CSSProperties
      }
    >
      {/* Focused-layout board grid: the real GamePage uses this exact grid
          with the real useResolvedGridRows() value. */}
      <div
        id="board-grid"
        className="relative grid h-full min-w-0"
        style={{ gridTemplateRows, gridTemplateColumns: "1fr" }}
      >
        {/* Row 1: opponent area. The real layout anchors the opponent-HUD
            rail with its top edge at the band's bottom edge (the fix
            authors' measured "rail tops" equal the band heights to the
            pixel), so the HUD hangs from the band boundary here. */}
        <div id="opp-band" data-flex-zone="opp-row" className="relative z-20 min-w-0 w-full">
          {/* Placeholder for the opponent's face-down hand (low-value space
              the prompt bar is allowed to overlap). Not a real component. */}
          <div className="flex h-full items-start justify-center pt-1 opacity-40">
            <div className="flex gap-1">
              {[0, 1, 2, 3, 4, 5, 6].map((i) => (
                <div key={i} className="h-10 w-7 rounded-sm bg-slate-700" />
              ))}
            </div>
          </div>
          <div
            id="opp-hud-anchor"
            className="absolute left-1/2 z-20 -translate-x-1/2"
            style={{ top: "100%" }}
          >
            <OpponentHud />
          </div>
        </div>
        {/* Row 2: battlefield placeholder */}
        <div id="battlefield-row" className="min-h-0" />
        {/* Row 3: player area placeholder */}
        <div id="player-row" className="min-h-0" />
      </div>

      {/* The real targeting overlay, fixed-positioned exactly as shipped. */}
      <TargetingOverlay />
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <I18nextProvider i18n={i18n}>
    <App />
  </I18nextProvider>,
);
