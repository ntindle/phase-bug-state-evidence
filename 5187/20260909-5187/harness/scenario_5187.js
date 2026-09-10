// scenario_5187.js — drives the ACTUAL opponent-counting lines from
// client/src/hooks/useResumables.ts (mainline a0f9c55d, copied verbatim to
// harness/source_useResumables.ts) through fixtures, with the exact
// expressions extracted programmatically from the source file so the test
// cannot drift from the shipped logic.
//
// Contract (from phase-rs/phase#5187): in a still-resumable multi-seat/FFA
// match summary, opponentCount must equal the number of live opponents of
// the local human (seat 0). The shipped formula
//   liveCount = players.filter(p => !p.is_eliminated).length
//   opponentCount = Math.max(0, liveCount - 1)
// assumes seat 0 is alive. When seat 0 is eliminated it is not part of
// liveCount, so the -1 undercounts by one.
'use strict';
const fs = require('fs');
const path = require('path');

const EVIDENCE_DIR = __dirname + '/..';

const src = fs.readFileSync(path.join(__dirname, 'source_useResumables.ts'), 'utf8');
const lines = src.split('\n');

// Extract the two shipped expressions verbatim (strip leading indent/trailing comma).
const liveLine = lines.find((l) => l.includes('const liveCount = publicState.players'));
const oppLine = lines.find((l) => l.includes('opponentCount: Math.max'));
if (!liveLine || !oppLine) {
  console.error('FATAL: could not locate counting expressions in source_useResumables.ts');
  process.exit(2);
}
const liveStmt = liveLine.trim().replace(/;$/, '');
const oppExpr = oppLine.trim().split(':')[1].trim().replace(/,$/, '');
console.log('shipped line A:', JSON.stringify(liveLine.trim()));
console.log('shipped line B:', JSON.stringify(oppLine.trim()));

// Evaluate the shipped logic on a fixture `players` array.
const countOpponents = new Function('publicState', `
  ${liveStmt};
  return (${oppExpr});
`);

function mkPlayers(desc) {
  // desc: array of [id, is_eliminated]
  return desc.map(([id, elim]) => ({ id, is_eliminated: elim, life: elim ? 0 : 20 }));
}

const cases = [
  {
    id: 'A1_control_seat0_alive',
    fixture: [[0, false], [1, false], [2, false]],
    expected: 2,
    note: 'control: seat 0 alive, 2 live opponents',
  },
  {
    id: 'A2_issue_example_seat0_eliminated',
    fixture: [[0, true], [1, false], [2, false]],
    expected: 2,
    note: "the issue's discriminating case: seat 0 eliminated, 2 live opponents",
  },
  {
    id: 'A3_eliminated_opponent_excluded',
    fixture: [[0, false], [1, false], [2, true]],
    expected: 1,
    note: 'seat 0 alive; eliminated opponent must not count',
  },
  {
    id: 'A4_all_eliminated',
    fixture: [[0, true], [1, true], [2, true]],
    expected: 0,
    note: 'degenerate: nobody alive',
  },
  {
    id: 'A5_lone_survivor',
    fixture: [[0, false], [1, true], [2, true]],
    expected: 0,
    note: 'degenerate: seat 0 the only live player',
  },
];

const assertions = {};
for (const c of cases) {
  const observed = countOpponents({ players: mkPlayers(c.fixture) });
  const status = observed === c.expected ? 'passed' : 'failed';
  assertions[c.id] = {
    status,
    expected: c.expected,
    observed,
    fixture: c.fixture.map(([id, elim]) => ({ id, is_eliminated: elim })),
    note: c.note,
  };
  console.log(`${c.id}: expected=${c.expected} observed=${observed} -> ${status}`);
}

fs.writeFileSync(path.join(EVIDENCE_DIR, 'assertions.json'), JSON.stringify(assertions, null, 2) + '\n');
console.log('wrote assertions.json');
