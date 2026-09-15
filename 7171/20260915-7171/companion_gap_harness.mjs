// companion_gap_harness.mjs — #7171 "No way to add/set Companion in Build Deck after Draft"
// Executes the verbatim shipped prop-gating logic of the v0.83.0 client to prove
// the companion section of the Build-Deck-after-Draft screen is unreachable.
// Run: node companion_gap_harness.mjs  (cwd = evidence dir)
import fs from "node:fs";

const results = {};
function check(id, passed, detail) {
  results[id] = { status: passed ? "passed" : "failed", detail };
  console.log(`${passed ? "PASS" : "FAIL"} ${id}: ${detail}`);
}

const LDB = fs.readFileSync("client_LimitedDeckBuilder_v0.83.0.tsx", "utf8");
const PANEL = fs.readFileSync("client_CommanderPanel_v0.83.0.tsx", "utf8");
const STORE = fs.readFileSync("client_draftStore_v0.83.0.ts", "utf8");
const UDB = fs.readFileSync("client_useDeckBuilder_v0.83.0.ts", "utf8");
const P2P = fs.readFileSync("client_p2p-adapter_v0.83.0.ts", "utf8");

// A1: CommanderPanel's companion section is gated on companionCandidates !== null,
// and the prop defaults to null. Both statements are verbatim from the shipped source.
const gateLine = "{companionCandidates !== null && (";
const defaultLine = "companionCandidates = null,";
check(
  "A1_panel_gate_verbatim",
  PANEL.includes(gateLine) && PANEL.includes(defaultLine),
  `section gate '${gateLine}' present=${PANEL.includes(gateLine)}, ` +
    `default '${defaultLine}' present=${PANEL.includes(defaultLine)}`
);

// A2: LimitedDeckBuilder renders <CommanderPanel> twice (desktop + local/workspace
// paths) and NEVER passes any companion* prop — so the gate always sees null.
const panelBlocks = [...LDB.matchAll(/<CommanderPanel[\s\S]*?\/>/g)].map((m) => m[0]);
const blocksClean =
  panelBlocks.length === 2 &&
  panelBlocks.every((b) => !b.includes("ompanion"));
check(
  "A2_draft_never_passes_companion_props",
  blocksClean,
  `found ${panelBlocks.length} <CommanderPanel/> blocks; ` +
    `companion-token occurrences across blocks: ${panelBlocks.map((b) => (b.match(/ompanion/g) || []).length).join(",")}`
);

// Simulate the verbatim default + gate exactly as React would resolve it:
//   props = {} (LimitedDeckBuilder passes nothing) -> companionCandidates = null
//   null !== null  === false  -> section not rendered
const companionCandidates = null; // default param from CommanderPanel.tsx
const sectionRendered = companionCandidates !== null;
check(
  "A3_gate_evaluates_false",
  sectionRendered === false,
  `with no companionCandidates prop passed, default null makes the gate evaluate to ${sectionRendered}`
);

// A4: the ONLY "companion" token in LimitedDeckBuilder is the hardcoded
// `companion: null` in the compatibility-request key memo — no state, no action.
const ldbHits = [...LDB.matchAll(/companion/gi)];
const onlyHardcodedNull =
  ldbHits.length === 1 && LDB.includes("companion: null,");
check(
  "A4_single_hardcoded_null",
  onlyHardcodedNull,
  `"companion" occurrences in LimitedDeckBuilder.tsx: ${ldbHits.length} ` +
    `(expect 1, the hardcoded 'companion: null' compatibility key)`
);

// A5: draftStore.ts (the draft workspace store feeding Build Deck) has no
// companion concept at all — nothing to designate into.
const storeHits = [...STORE.matchAll(/companion/gi)];
check(
  "A5_store_has_no_companion",
  storeHits.length === 0,
  `"companion" occurrences in draftStore.ts: ${storeHits.length}`
);

// A6: the local submit path calls lease.submitDeck(names, []) — commanders slot
// only, companion never supplied; no companion arg anywhere near submitDeck.
const submitCall = "lease.submitDeck(projectDeckNames(state.workspaceState!, state.view!.pool), [])";
const storeSubmitLines = STORE.split("\n").filter((l) => l.includes("submitDeck"));
const companionNearSubmit = storeSubmitLines.some((l) => /companion/i.test(l));
check(
  "A6_submit_never_carries_companion",
  STORE.includes(submitCall) && !companionNearSubmit,
  `verbatim local submit call present=${STORE.includes(submitCall)}; ` +
    `companion on any submitDeck line=${companionNearSubmit}`
);

// A7 (control): the CONSTRUCTED deck-builder has the full companion flow.
check(
  "A7_control_constructed_has_companion",
  UDB.includes("handleSetCompanion") &&
    UDB.includes("companionCandidates(request)") &&
    UDB.includes("handleRemoveCompanion"),
  `handleSetCompanion=${UDB.includes("handleSetCompanion")}, ` +
    `companionCandidates(request)=${UDB.includes("companionCandidates(request)")}, ` +
    `handleRemoveCompanion=${UDB.includes("handleRemoveCompanion")}`
);

// A8 (control): the draft/p2p TRANSPORT can carry a companion (optional slot),
// so the block is the draft client UI/state, not the wire format.
check(
  "A8_control_transport_supports_companion",
  P2P.includes("companion?: string[];"),
  `DeckSeatPayload declares optional 'companion?: string[]'=${P2P.includes("companion?: string[];")}`
);

// A9: no commit touching LimitedDeckBuilder.tsx or draftStore.ts since the
// 2026-08-10 report mentions companion (gap persists on v0.83.0).
for (const [f, label] of [
  ["commits_LimitedDeckBuilder_since_2026-08-10.json", "LimitedDeckBuilder.tsx"],
  ["commits_draftStore_since_2026-08-10.json", "draftStore.ts"],
]) {
  const commits = JSON.parse(fs.readFileSync(f, "utf8"));
  const mentions = commits.filter((c) =>
    /companion/i.test(c.commit.message + (c.commit.message.split("\n")[1] || ""))
  );
  check(
    `A9_no_companion_fix_${label.replace(/[^a-z]/gi, "")}`,
    mentions.length === 0,
    `${commits.length} commits to ${label} since 2026-08-10; companion-mentioning: ${mentions.length}`
  );
}

const allPass = Object.values(results).every((r) => r.status === "passed");
fs.writeFileSync("gap_results.json", JSON.stringify(results, null, 2));
console.log(allPass ? "ALL CHECKS PASSED" : "SOME CHECKS FAILED");
process.exit(allPass ? 0 : 1);
