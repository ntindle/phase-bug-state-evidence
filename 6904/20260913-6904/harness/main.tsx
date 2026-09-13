import React from "react";
import { createRoot } from "react-dom/client";
import "phase-src/index.css";

import { ManaCostPips } from "phase-src/components/mana/ManaCostPips.tsx";
import {
  handFanGeometry,
  playerHandFanSizingStyle,
} from "phase-src/components/hand/handFanPresentation.ts";
import type { ManaCost } from "phase-src/adapter/types.ts";

declare global {
  interface Window {
    __geom: () => Record<string, unknown>;
    __ready: boolean;
  }
}
window.__ready = false;

const params = new URLSearchParams(window.location.search);
// cfg can also be injected as window.__CFG__ when the page is loaded from
// inlined HTML (no query string available).
const cfg =
  (window as unknown as { __CFG__?: string }).__CFG__ ||
  params.get("cfg") ||
  "single5";

// ---------------------------------------------------------------------------
// Test costs (real ManaCost shapes, mirroring what spellCostDisplay hands the
// real hand card). single5/fan*: the widest single-face class (5 symbols);
// pair8: Esika, God of the Tree // The Prismatic Bridge ({1}{G}{G} //
// {W}{U}{B}{R}{G}, 8 symbols total) — the pair shrink-tier floor.
// ---------------------------------------------------------------------------
const COST_5: ManaCost = {
  type: "Cost",
  shards: ["White", "Blue", "Black", "Red", "Green"],
  generic: 0,
};
const PAIR_FRONT: ManaCost = { type: "Cost", shards: ["Green", "Green"], generic: 1 };
const PAIR_BACK: ManaCost = {
  type: "Cost",
  shards: ["White", "Blue", "Black", "Red", "Green"],
  generic: 0,
};

interface CardSpec {
  cost: ManaCost;
  backFace?: { cost: ManaCost; isReduced?: boolean };
  name: string;
}

function specsFor(cfgName: string): CardSpec[] {
  switch (cfgName) {
    case "pair8":
      return [
        {
          cost: PAIR_FRONT,
          backFace: { cost: PAIR_BACK },
          name: "Esika, God of the Tree // The Prismatic Bridge",
        },
      ];
    case "fan7":
      return Array.from({ length: 7 }, (_, i) => ({
        cost: COST_5,
        name: `Five-color test card ${i + 1}`,
      }));
    case "fan2":
      return [
        { cost: COST_5, name: "Five-color test card 1" },
        { cost: COST_5, name: "Five-color test card 2" },
      ];
    case "single5":
    default:
      return [{ cost: COST_5, name: "Niv-Mizzet Reborn, Five-Color Paragon" }];
  }
}

function rectOf(el: Element | null) {
  if (!el) return null;
  const r = el.getBoundingClientRect();
  return {
    x: r.x, y: r.y, width: r.width, height: r.height,
    top: r.top, bottom: r.bottom, left: r.left, right: r.right,
  };
}

function HandCard({ spec, index, total }: { spec: CardSpec; index: number; total: number }) {
  // Real fan geometry (wide profile, the desktop player hand): the exact
  // negative margin each later card slides over the previous card's right
  // portion with. zIndex = index, exactly as PlayerHand renders it.
  const fan = handFanGeometry(total);
  const marginLeft = index === 0 ? 0 : fan.overlap;
  return (
    <div
      data-harness-card={index}
      className="relative cursor-pointer leading-[0] select-none"
      style={{ marginLeft, zIndex: index }}
    >
      <div className="relative rounded-lg">
        {/* Card-frame stand-in at the real hand size. Title bar + name strip
            sit where the M15 frame puts them: title bar y ~3–10% of card
            height, name text left-aligned from ~7% (the printed-cost anchor
            the pip badge targets is right 6.5% / top 5% per ManaCostPips). */}
        <div
          data-harness-frame={index}
          className="!w-[var(--hand-card-w)] !h-[var(--hand-card-h)] relative overflow-hidden rounded-lg bg-slate-800"
        >
          <div className="absolute left-0 right-0 top-[3%] h-[7%] bg-slate-700/80" />
          <div
            data-harness-name={index}
            className="absolute left-[7%] top-[3.6%] w-[52%] truncate font-serif text-white"
            style={{ fontSize: "calc(var(--hand-card-w) * 0.052)" }}
          >
            {spec.name}
          </div>
          <div className="absolute inset-x-[6%] top-[13%] bottom-[4%] rounded bg-slate-900/60" />
        </div>
        {/* The REAL @container overlay + REAL ManaCostPips from PlayerHand. */}
        <div
          data-harness-overlay={index}
          className="pointer-events-none absolute inset-0 @container"
        >
          <ManaCostPips cost={spec.cost} backFace={spec.backFace} size="fluid" />
        </div>
      </div>
    </div>
  );
}

function App() {
  const specs = specsFor(cfg);
  const total = specs.length;
  // The real responsive sizing the player hand applies (92vw budget).
  const sizingStyle = playerHandFanSizingStyle(total);

  React.useEffect(() => {
    window.__geom = () => {
      const cards: unknown[] = [];
      document.querySelectorAll("[data-harness-card]").forEach((cardEl, j) => {
        const frame = cardEl.querySelector("[data-harness-frame]");
        const overlay = cardEl.querySelector("[data-harness-overlay]");
        const badge = overlay?.firstElementChild ?? null;
        const name = cardEl.querySelector("[data-harness-name]");
        const row = badge?.firstElementChild ?? null;
        const pips: unknown[] = [];
        if (row) {
          for (const child of Array.from(row.children)) {
            if (child.hasAttribute("data-mana-cost-backdrop")) continue;
            if (child.hasAttribute("data-mana-cost-face-separator")) continue;
            if (child.tagName === "DIV") pips.push(rectOf(child));
          }
        }
        cards.push({
          index: j,
          zIndex: (cardEl as HTMLElement).style.zIndex,
          frame: rectOf(frame),
          badge: rectOf(badge),
          name: rectOf(name),
          pips,
          pipCount: pips.length,
        });
      });
      const root = document.getElementById("harness-root");
      const cs = root ? getComputedStyle(root) : null;
      return {
        cfg,
        viewport: { w: window.innerWidth, h: window.innerHeight },
        handCardW: cs?.getPropertyValue("--hand-card-w") ?? null,
        cards,
      };
    };
    window.__ready = true;
  }, []);

  return (
    <div
      id="harness-root"
      style={{ width: "100vw", height: "100vh", background: "#0b1220", ...sizingStyle } as React.CSSProperties}
    >
      <div className="flex h-full items-center justify-center">
        <div className="flex items-start" data-harness-fan>
          {specs.map((spec, i) => (
            <HandCard key={i} spec={spec} index={i} total={total} />
          ))}
        </div>
      </div>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
