#!/usr/bin/env node
/**
 * #5186 reproduction driver: colorless-commander color identity enforcement.
 *
 * Drives the REAL getColorIdentityViolations / getCombinedColorIdentity from
 * the pinned mainline checkout (phase-rs/phase, client mainline HEAD cb58ef5;
 * commanderUtils.ts byte-identical to local checkout at aa1fb24) via Node 24
 * native type-stripping -- no test-file additions inside the repo tree, no
 * vitest, no DOM. Pure-function subsystem test per backfill playbook step 2.
 *
 * Card color_identity values verified against the live Scryfall API
 * (api.scryfall.com/cards/named) at run time; the card-data cache is built
 * from those API responses (only name + color_identity are read by the
 * functions under test).
 *
 * Assertions:
 *   M1 (bug leg):        Kozilek (CI []) + [Lightning Bolt (R), Wastes ([])]
 *                        -> expected ["Lightning Bolt"], buggy code returns [].
 *   M2 (colored control): Krenko (CI [R]) + [Lightning Bolt, Counterspell (U)]
 *                        -> expected ["Counterspell"] (unchanged behavior).
 *   M3 (loading control): uncached commander + [Lightning Bolt]
 *                        -> expected [] (no false positives mid-load).
 *   M4 (colorless legal): Kozilek + [Wastes, Sol Ring (both CI [])]
 *                        -> expected [].
 */
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync, writeFileSync } from "node:fs";

const FILE = "/home/hatch/workspace/dev/phase-mainline/client/src/components/deck-builder/commanderUtils.ts";
const { getColorIdentityViolations, getCombinedColorIdentity } = await import(FILE);

const SAY = [];
function say(line) { SAY.push(line); console.log(line); }

// ---- build card-data cache from live Scryfall API (authoritative CI values)
const NAMES = [
  "Kozilek, Butcher of Truth",
  "Lightning Bolt",
  "Wastes",
  "Sol Ring",
  "Krenko, Mob Boss",
  "Counterspell",
];
const cache = new Map();
const apiRows = [];
for (const name of NAMES) {
  const raw = execFileSync(
    "curl",
    ["-s", "--max-time", "20",
     `https://api.scryfall.com/cards/named?exact=${encodeURIComponent(name)}`,
     "-H", "User-Agent: phase-backfill/1.0"],
    { encoding: "utf8" },
  );
  const d = JSON.parse(raw);
  apiRows.push({ name: d.name, color_identity: d.color_identity, mana_cost: d.mana_cost, type_line: d.type_line });
  cache.set(d.name, {
    name: d.name,
    mana_cost: d.mana_cost ?? "",
    cmc: d.cmc ?? 0,
    type_line: d.type_line ?? "",
    color_identity: d.color_identity,
  });
  await new Promise((r) => setTimeout(r, 120)); // Scryfall rate-limit courtesy
}
say("Scryfall CI values: " + JSON.stringify(apiRows.map((r) => [r.name, r.color_identity])));

const deck = (...names) => names.map((n) => ({ name: n, count: 1 }));
const results = [];
function check(id, desc, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  results.push({ id, desc, expected, actual, status: ok ? "passed" : "failed" });
  say(`${id} ${desc}: expected=${JSON.stringify(expected)} observed=${JSON.stringify(actual)} -> ${ok ? "passed" : "FAILED"}`);
  return ok;
}

// M1: the reported bug -- genuinely colorless commander must flag the red card
check(
  "M1", "colorless-commander flags off-color card",
  getColorIdentityViolations(deck("Lightning Bolt", "Wastes"), ["Kozilek, Butcher of Truth"], cache),
  ["Lightning Bolt"],
);
// M2: colored-commander behavior unchanged
check(
  "M2", "colored-commander control still flags only off-color",
  getColorIdentityViolations(deck("Lightning Bolt", "Counterspell"), ["Krenko, Mob Boss"], cache),
  ["Counterspell"],
);
// M3: loading window -- commander data not cached yet -> no violations
check(
  "M3", "uncached-commander (loading window) yields no violations",
  getColorIdentityViolations(deck("Lightning Bolt"), ["Some Unfetched Commander"], cache),
  [],
);
// M4: colorless cards legal under a colorless commander
check(
  "M4", "colorless cards legal under colorless commander",
  getColorIdentityViolations(deck("Wastes", "Sol Ring"), ["Kozilek, Butcher of Truth"], cache),
  [],
);

// combined-identity introspection (shows the empty-identity root cause)
say("combined identity (Kozilek): " + JSON.stringify(getCombinedColorIdentity(["Kozilek, Butcher of Truth"], cache)));
say("combined identity (Krenko):  " + JSON.stringify(getCombinedColorIdentity(["Krenko, Mob Boss"], cache)));

const fileBytes = readFileSync(FILE);
const fileSha = createHash("sha256").update(fileBytes).digest("hex");
const observations = {
  issue: 5186,
  client: {
    repo: "phase-rs/phase",
    mainline_commit: "cb58ef5dde00978e2b3b829308fd256125ff8271",
    local_checkout_commit: "aa1fb24ad08863eedf6a00493f1e2865050ccc2d",
    file: "client/src/components/deck-builder/commanderUtils.ts",
    file_sha256: fileSha,
    file_identical_between_commits: true,
  },
  scryfall_color_identities: apiRows,
  assertions: Object.fromEntries(results.map((r) => [r.id, r.status])),
  assertion_details: results,
  verdict: results[0].status === "failed" ? "reproduced" : "not-reproduced",
};
writeFileSync(process.argv[2] ?? "/tmp/client_observations.json", JSON.stringify(observations, null, 2));
writeFileSync(process.argv[3] ?? "/tmp/scenario_run.log", SAY.join("\n") + "\n");
process.exit(results[0].status === "failed" ? 0 : 0); // exit code carries nothing; verdict is in JSON
