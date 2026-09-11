import path from "node:path";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const HERE = __dirname;
// The harness is copied into the variant's client/ dir so node resolution
// works; src is the sibling src/ of that same client tree.
const SRC = path.resolve(HERE, "..", "src");

function srcAlias(): Plugin {
  return {
    name: "harness-src-alias",
    enforce: "pre",
    resolveId(id: string) {
      // "phase-src/..." -> the variant's client/src/...
      if (id === "phase-src" || id.startsWith("phase-src/")) {
        return SRC + id.slice("phase-src".length);
      }
      return null;
    },
  };
}

const PORT = Number(process.env.HARNESS_PORT || 5199);

// Stub for the WASM engine module: the harness never instantiates an engine
// adapter (stores are seeded directly), but vite statically resolves
// engine-worker.ts's named imports from "@wasm/engine".
import { fileURLToPath } from "node:url";
const wasmStub = fileURLToPath(new URL("./wasm-stub.js", import.meta.url));

// Mirror the client build-time defines (vite.config.ts dataFileDefines):
// the harness never fetches data files or contacts multiplayer, but modules
// reference these identifiers at evaluation time.
const harnessDefines: Record<string, string> = {
  __APP_VERSION__: JSON.stringify("0.0.0-harness"),
  __BUILD_HASH__: JSON.stringify("harness"),
  __ENGINE_FINGERPRINT__: "undefined",
  __ENGINE_WASM_URL__: "undefined",
  __IS_RELEASE_BUILD__: JSON.stringify(false),
  __SUPABASE_URL__: JSON.stringify(""),
  __SUPABASE_ANON_KEY__: JSON.stringify(""),
  __GIT_REPO_URL__: JSON.stringify("https://github.com/phase-rs/phase"),
  __PREVIEW_SITE_URL__: JSON.stringify("https://preview.phase-rs.dev"),
  __RELEASE_SITE_URL__: JSON.stringify("https://phase-rs.dev"),
  __AUDIO_BASE_URL__: JSON.stringify(""),
  __TELEMETRY_URL__: JSON.stringify(""),
  __STATUS_URL__: JSON.stringify(""),
};
for (const name of [
  "__CARD_DATA_URL__", "__CARD_DATA_META_URL__", "__CARD_DATA_LOCALE_URL_TEMPLATE__",
  "__CARD_NAMES_URL__", "__CHANGELOG_URL__", "__CHANGELOG_META_URL__",
  "__COVERAGE_DATA_URL__", "__COVERAGE_SUMMARY_URL__", "__DECKS_URL__",
  "__DRAFT_POOLS_URL__", "__SCRYFALL_DATA_URL__", "__SCRYFALL_IMAGES_LOCALE_URL_TEMPLATE__",
  "__SCRYFALL_PRINTINGS_URL__", "__SCRYFALL_SETS_URL__", "__SCRYFALL_TOKEN_IMAGES_URL__",
  "__SET_LIST_URL__", "__OFFICIAL_MULTIPLAYER_SERVER_URL__",
  "__DEFAULT_MULTIPLAYER_SERVER_URL__",
]) {
  harnessDefines[name] = JSON.stringify("");
}

export default defineConfig({
  root: HERE,
  plugins: [srcAlias(), react(), tailwindcss()],
  define: harnessDefines,
  resolve: { alias: [{ find: /^@wasm\/engine$/, replacement: wasmStub }] },
  server: { port: PORT, strictPort: true, host: "0.0.0.0", allowedHosts: true },
  build: {
    outDir: "harness-dist",
    assetsInlineLimit: 100_000_000,
    cssCodeSplit: false,
    rollupOptions: { output: { inlineDynamicImports: true } },
  },
});
