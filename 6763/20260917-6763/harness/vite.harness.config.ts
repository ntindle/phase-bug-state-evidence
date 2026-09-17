import path from "node:path";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const H = path.resolve(__dirname, "mocks");
// The harness is copied into the client/ dir under test so node resolution
// works; src is the sibling src/ of that same client tree.
const SRC = path.resolve(__dirname, "..", "src");

function mockAlias(): Plugin {
  const map: Array<[RegExp, string]> = [
    [/hooks\/useGameDispatch/, path.join(H, "useGameDispatch.ts")],
    [/hooks\/useCardImage/, path.join(H, "useCardImage.ts")],
    [/hooks\/useEngineCardData/, path.join(H, "useEngineCardData.ts")],
    [/stores\/multiplayerStore/, path.join(H, "multiplayerStore.ts")],
  ];
  return {
    name: "harness-mocks",
    enforce: "pre",
    resolveId(id: string, _importer?: string) {
      // "phase-src/..." -> the client tree's src/...
      if (id === "phase-src" || id.startsWith("phase-src/")) {
        return SRC + id.slice("phase-src".length);
      }
      // The harness never loads the real engine; stub the wasm modules so
      // the import graph resolves (game state is seeded directly).
      if (id === "@wasm/engine" || id === "@wasm/draft") {
        return path.join(H, "wasmStub.ts");
      }
      for (const [re, file] of map) {
        if (re.test(id)) return file;
      }
      return null;
    },
  };
}

const PORT = Number(process.env.HARNESS_PORT || 5201);

export default defineConfig({
  root: __dirname,
  plugins: [mockAlias(), react(), tailwindcss()],
  server: { port: PORT, strictPort: true, host: "127.0.0.1" },
  // Build-time globals the client tree expects from the real vite config.
  // Harness-safe defaults: empty URLs disable telemetry/supabase/card-data
  // fetches; the harness seeds all state directly.
  define: {
    __APP_VERSION__: '"0.0.0-harness"',
    __AUDIO_BASE_URL__: '""',
    __BUILD_HASH__: '"harness"',
    __CARD_DATA_DE_URL__: '""',
    __CARD_DATA_LOCALE_URL_TEMPLATE__: '"/card-data.{lng}.json"',
    __CARD_DATA_URL__: '"/card-data.json"',
    __CARD_NAMES_URL__: '""',
    __DEFAULT_MULTIPLAYER_SERVER_URL__: '""',
    __ENGINE_FINGERPRINT__: 'undefined',
    __ENGINE_WASM_URL__: 'undefined',
    __GIT_REPO_URL__: '"https://github.com/phase-rs/phase"',
    __IS_RELEASE_BUILD__: 'false',
    __OFFICIAL_MULTIPLAYER_SERVER_URL__: '""',
    __PREVIEW_SITE_URL__: '"https://preview.phase-rs.dev"',
    __RELEASE_SITE_URL__: '"https://phase-rs.dev"',
    __SCRYFALL_IMAGES_LOCALE_URL_TEMPLATE__: '""',
    __STATUS_URL__: '"/status.json"',
    __SUPABASE_ANON_KEY__: '""',
    __SUPABASE_URL__: '""',
    __TELEMETRY_URL__: '""',
  },
});
