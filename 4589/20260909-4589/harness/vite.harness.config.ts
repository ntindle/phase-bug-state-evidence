import path from "node:path";
import { defineConfig, type Plugin } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

const H = path.resolve(__dirname, "harness/mocks");

function mockAlias(): Plugin {
  const map: Array<[RegExp, string]> = [
    [/stores\/gameStore/, path.join(H, "gameStore.ts")],
    [/hooks\/useGameDispatch/, path.join(H, "useGameDispatch.ts")],
    [/card\/CardImage/, path.join(H, "CardImage.tsx")],
    [/services\/cardImageLookup/, path.join(H, "cardImageLookup.ts")],
    [/hooks\/useInspectHoverProps/, path.join(H, "useInspectHoverProps.ts")],
    [/ChoiceOverlay(\.tsx)?$/, path.join(H, "ChoiceOverlay.tsx")],
  ];
  return {
    name: "harness-mocks",
    enforce: "pre",
    resolveId(id: string) {
      for (const [re, file] of map) {
        if (re.test(id)) return file;
      }
      return null;
    },
  };
}

const PORT = Number(process.env.HARNESS_PORT || 5199);

export default defineConfig({
  root: path.resolve(__dirname, "harness"),
  plugins: [mockAlias(), react(), tailwindcss()],
  server: { port: PORT, strictPort: true },
});
