// Curve harness for phase-rs/phase #7170.
// Executes the EXACT bucketing logic shipped in client/src/components/draft/ManaCurve.tsx
// (both the current v0.83.0 revision and the pre-fix revision 7b0a1a2c).
// The function bodies below are verbatim copies of the useMemo callback bodies in those
// files, with only TypeScript generics (`<string, number>`) erased. On startup the harness
// asserts, line by line, that each embedded statement is present verbatim in the
// downloaded shipped file. Any divergence aborts.
import { readFileSync } from "node:fs";
import { strict as assert } from "node:assert";

const DIR = new URL(".", import.meta.url).pathname;
const CUR = readFileSync(DIR + "client_ManaCurve_v0.83.0.tsx", "utf8");
const PRE = readFileSync(DIR + "client_ManaCurve_prefilter_7b0a1a2c.tsx", "utf8");

// The exact statements copied from the shipped v0.83.0 useMemo body (type generics erased).
const CURRENT_LOGIC_LINES = [
  "const cmcByName = new Map();",
  "for (const card of pool) {",
  "cmcByName.set(card.name, card.cmc);",
  "const buckets = new Map();",
  "for (const bucket of CMC_BUCKETS) buckets.set(bucket, 0);",
  "for (const name of cards) {",
  "const card = pool.find((entry) => entry.name === name);",
  "if (card === undefined || /\\bland\\b/i.test(card.type_line)) continue;",
  "const cmc = cmcByName.get(name) ?? 0;",
  'const key = cmc >= 6 ? "6+" : String(cmc);',
  "buckets.set(key, (buckets.get(key) ?? 0) + 1);",
  "return CMC_BUCKETS.map((key) => ({",
  "label: key,",
  "count: buckets.get(key) ?? 0,",
  "}));",
];
// The pre-fix revision (7b0a1a2c): identical except the land guard is absent.
const PREFIX_LOGIC_LINES = [
  "const cmcByName = new Map();",
  "for (const card of pool) {",
  "cmcByName.set(card.name, card.cmc);",
  "const buckets = new Map();",
  "for (const bucket of CMC_BUCKETS) buckets.set(bucket, 0);",
  "for (const name of cards) {",
  "const cmc = cmcByName.get(name) ?? 0;",
  'const key = cmc >= 6 ? "6+" : String(cmc);',
  "buckets.set(key, (buckets.get(key) ?? 0) + 1);",
  "return CMC_BUCKETS.map((key) => ({",
  "label: key,",
  "count: buckets.get(key) ?? 0,",
  "}));",
];

const norm = (s) => s.replace(/\s+/g, "").replace(/<[^<>{}]*>/g, "");
const CURn = norm(CUR), PREn = norm(PRE);
for (const ln of CURRENT_LOGIC_LINES) assert.ok(CURn.includes(norm(ln)), "CURRENT line missing from shipped file: " + ln);
for (const ln of PREFIX_LOGIC_LINES) assert.ok(PREn.includes(norm(ln)), "PRE line missing from pre-fix file: " + ln);
assert.ok(!PREn.includes(norm("if(card===undefined||/\\bland\\b/i.test(card.type_line))continue;")), "pre-fix file unexpectedly contains the land guard");
assert.ok(CURn.includes(norm("if(card===undefined||/\\bland\\b/i.test(card.type_line))continue;")), "current file missing the land guard");

const CMC_BUCKETS = ["0", "1", "2", "3", "4", "5", "6+"];

function bucketsCurrent(pool, cards) {
  const cmcByName = new Map();
  for (const card of pool) { cmcByName.set(card.name, card.cmc); }
  const buckets = new Map();
  for (const bucket of CMC_BUCKETS) buckets.set(bucket, 0);
  for (const name of cards) {
    const card = pool.find((entry) => entry.name === name);
    if (card === undefined || /\bland\b/i.test(card.type_line)) continue;
    const cmc = cmcByName.get(name) ?? 0;
    const key = cmc >= 6 ? "6+" : String(cmc);
    buckets.set(key, (buckets.get(key) ?? 0) + 1);
  }
  return CMC_BUCKETS.map((key) => ({ label: key, count: buckets.get(key) ?? 0 }));
}

function bucketsPreFix(pool, cards) {
  const cmcByName = new Map();
  for (const card of pool) { cmcByName.set(card.name, card.cmc); }
  const buckets = new Map();
  for (const bucket of CMC_BUCKETS) buckets.set(bucket, 0);
  for (const name of cards) {
    const cmc = cmcByName.get(name) ?? 0;
    const key = cmc >= 6 ? "6+" : String(cmc);
    buckets.set(key, (buckets.get(key) ?? 0) + 1);
  }
  return CMC_BUCKETS.map((key) => ({ label: key, count: buckets.get(key) ?? 0 }));
}

// Draft-pool fixtures shaped like DraftCardInstance (name/cmc/type_line).
const pool = [
  { name: "Forest", cmc: 0, type_line: "Basic Land — Forest" },
  { name: "Island", cmc: 0, type_line: "Basic Land — Island" },
  { name: "Evolving Wilds", cmc: 0, type_line: "Land" },
  { name: "Snow-Covered Mountain", cmc: 0, type_line: "Snow Land — Mountain" },
  { name: "Lightning Bolt", cmc: 1, type_line: "Instant" },
  { name: "Grizzly Bears", cmc: 2, type_line: "Creature — Bear" },
  { name: "Memnite", cmc: 0, type_line: "Artifact Creature — Construct" },
  { name: "Shivan Dragon", cmc: 6, type_line: "Creature — Dragon" },
];
// Main-deck names after the draft (the issue's exact scenario: drafted + basic lands in main deck).
const cards = ["Forest","Forest","Island","Evolving Wilds","Snow-Covered Mountain",
  "Lightning Bolt","Lightning Bolt","Grizzly Bears","Memnite","Shivan Dragon"];

const cur = bucketsCurrent(pool, cards);
const pre = bucketsPreFix(pool, cards);
const get = (rows, b) => rows.find((r) => r.label === b).count;

const results = {
  fixture: { pool, main_deck: cards },
  current_v0830: Object.fromEntries(cur.map((r) => [r.label, r.count])),
  pre_fix_7b0a1a2c: Object.fromEntries(pre.map((r) => [r.label, r.count])),
  assertions: {
    A1_reported_symptom_present_prefix: get(pre, "0") === 6 ? "passed" : "failed",
    A2_lands_excluded_current: get(cur, "0") === 1 && cur.reduce((s, r) => s + r.count, 0) === 5 ? "passed" : "failed",
    A3_zero_cost_nonland_still_counted: get(cur, "0") === 1 ? "passed" : "failed",
    A4_basic_snow_and_plain_lands_all_excluded: cur.reduce((s, r) => s + r.count, 0) === 5 ? "passed" : "failed",
    A5_spell_buckets_unchanged: (get(cur, "1") === 2 && get(cur, "2") === 1 && get(cur, "6+") === 1) ? "passed" : "failed",
  },
};
console.log(JSON.stringify(results, null, 1));
const fails = Object.entries(results.assertions).filter(([, v]) => v !== "passed");
if (fails.length) { console.error("FAILURES:", fails.map(([k]) => k).join(",")); process.exit(1); }
console.log("ALL ASSERTIONS PASSED");
