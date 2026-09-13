import React from "react";
import { createRoot } from "react-dom/client";
import i18n from "i18next";
import { initReactI18next, I18nextProvider } from "react-i18next";
import "phase-src/index.css";

import { OptionalEffectModalContent } from "phase-src/components/modal/OptionalEffectModal.tsx";
import gameEn from "phase-src/i18n/locales/en/game.json";

declare global {
  interface Window {
    __PAYLOAD__?: {
      waiting_for: Record<string, unknown>;
      objects: Record<string, unknown>;
      exiled_name: string;
      source_name: string;
    };
    __DISPATCHED__?: unknown[];
    __geom: () => Record<string, unknown>;
    __ready: boolean;
    __render_error?: string;
  }
}

window.__ready = false;
window.__DISPATCHED__ = [];

void i18n.use(initReactI18next).init({
  lng: "en",
  fallbackLng: "en",
  resources: { en: { game: gameEn as Record<string, unknown> } },
  ns: ["game"],
  defaultNS: "game",
  interpolation: { escapeValue: false },
});

class Boundary extends React.Component<
  { children: React.ReactNode },
  { error: string | null }
> {
  constructor(props: { children: React.ReactNode }) {
    super(props);
    this.state = { error: null };
  }
  static getDerivedStateFromError(e: unknown) {
    return { error: String(e) };
  }
  componentDidCatch(e: unknown) {
    window.__render_error = String(e);
  }
  render() {
    if (this.state.error) {
      return <div id="harness-error">RENDER ERROR: {this.state.error}</div>;
    }
    return this.props.children;
  }
}

function App() {
  const p = window.__PAYLOAD__;
  if (!p) return <div id="harness-error">NO PAYLOAD</div>;
  const wf = p.waiting_for as unknown as Parameters<
    typeof OptionalEffectModalContent
  >[0]["waitingFor"];
  const objects = p.objects as Parameters<
    typeof OptionalEffectModalContent
  >[0]["objects"];
  const dispatch = (action: unknown) => {
    window.__DISPATCHED__!.push(action);
    return Promise.resolve();
  };
  return (
    <I18nextProvider i18n={i18n}>
      <Boundary>
        <div id="harness-root">
          <OptionalEffectModalContent
            waitingFor={wf}
            objects={objects}
            dispatch={dispatch}
          />
        </div>
      </Boundary>
    </I18nextProvider>
  );
}

function modalText(): string {
  const root = document.getElementById("harness-root");
  return (root?.textContent ?? "").replace(/\s+/g, " ").trim();
}

window.__geom = () => {
  const p = window.__PAYLOAD__;
  const text = modalText();
  const exiled = (p?.exiled_name ?? "").toLowerCase();
  const source = (p?.source_name ?? "").toLowerCase();
  const t = text.toLowerCase();
  // Every <img> inside the modal (card previews render as images).
  const imgs = Array.from(
    document.querySelectorAll("#harness-root img"),
  ).map((img) => ({
    alt: img.getAttribute("alt"),
    src: (img.getAttribute("src") || "").slice(0, 80),
  }));
  return {
    renderError: window.__render_error ?? null,
    modalText: text,
    exiledCardName: p?.exiled_name ?? null,
    exiledNameVisibleInModal: exiled !== "" && t.includes(exiled),
    sourceNameVisibleInModal: source !== "" && t.includes(source),
    waitingForDataKeys: Object.keys(
      ((p?.waiting_for as Record<string, unknown>)?.data ??
        {}) as Record<string, unknown>,
    ),
    modalImages: imgs,
    dispatched: window.__DISPATCHED__ ?? [],
  };
};

createRoot(document.getElementById("root")!).render(<App />);
window.__ready = true;
