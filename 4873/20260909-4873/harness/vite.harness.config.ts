import path from "node:path";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const H = path.resolve(__dirname, "mocks");
// The harness is copied into each variant's client/ dir so node resolution
// works; src is the sibling src/ of that same client tree.
const SRC = path.resolve(__dirname, "..", "src");

function mockAlias(): Plugin {
  const map: Array<[RegExp, string]> = [
    [/hooks\/useGameDispatch/, path.join(H, "useGameDispatch.ts")],
    [/hooks\/useSeatColor/, path.join(H, "useSeatColor.ts")],
    [/hooks\/usePlayerId/, path.join(H, "usePlayerId.ts")],
    [/hooks\/usePlayerAvatarImage/, path.join(H, "usePlayerAvatarImage.ts")],
    [/services\/cardNames/, path.join(H, "cardNames.ts")],
    [/stores\/multiplayerStore/, path.join(H, "multiplayerStore.ts")],
  ];
  return {
    name: "harness-mocks",
    enforce: "pre",
    resolveId(id: string, importer?: string) {
      // "phase-src/..." -> the variant's client/src/...
      if (id === "phase-src" || id.startsWith("phase-src/")) {
        return SRC + id.slice("phase-src".length);
      }
      // Relative specifiers are seen raw here: "./ChoiceOverlay.tsx" from the
      // modal component, "../../hooks/..." etc.
      if (id === "./ChoiceOverlay.tsx" && importer?.includes("components/modal")) {
        return path.join(H, "ChoiceOverlay.tsx");
      }
      for (const [re, file] of map) {
        if (re.test(id)) return file;
      }
      return null;
    },
  };
}

const PORT = Number(process.env.HARNESS_PORT || 5199);

export default defineConfig({
  root: __dirname,
  plugins: [mockAlias(), react(), tailwindcss()],
  server: { port: PORT, strictPort: true, host: "127.0.0.1" },
});
