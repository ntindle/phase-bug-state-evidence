// Final assertion harness for phase-rs/phase #5178.
// Runs the extracted real mainline parseJoinCode (PR base d6197d0d) and the
// PR-patched variant against the behavioral contract, writes observations.
const fs = require('fs');
const D = '/home/hatch/workspace/dev/phase-backfill/driver';
const mainline = require(D + '/variant_mainline.js').parseJoinCode;
const pr = require(D + '/variant_pr.js').parseJoinCode;

const EXPECT = 'wss://[::1]/ws'; // the PR's expected address for ABC123@[::1]
const MALFORMED = 'wss://[::1/ws'; // observed mainline output (host "[:", port 1)

const results = {};
function check(id, cond, detail) {
  results[id] = { status: cond ? 'passed' : 'failed', detail };
  console.log((cond ? 'PASS' : 'FAIL') + ' ' + id + ' — ' + detail);
}

// A1: the reported bug — mainline mangles the port-less bracketed IPv6 host.
const a1 = mainline('ABC123@[::1]');
check('A1_mainline_bug',
  a1.code === 'ABC123' && a1.serverAddress === MALFORMED && a1.serverAddress !== EXPECT,
  'mainline -> ' + JSON.stringify(a1) + ' (expected-correct would be ' + EXPECT + ')');

// A2: control — bracketed IPv6 WITH a port already works on mainline.
const a2 = mainline('ABC123@[::1]:9000');
check('A2_mainline_with_port_control',
  a2.serverAddress === 'wss://[::1]:9000/ws',
  'mainline -> ' + JSON.stringify(a2));

// A3: the PR fix produces the correct address.
const a3 = pr('ABC123@[::1]');
check('A3_pr_fix',
  a3.code === 'ABC123' && a3.serverAddress === EXPECT,
  'pr variant -> ' + JSON.stringify(a3));

// A4: no regressions — the PR variant matches mainline on every other
// documented join-code format.
const regressionCases = {
  'ABC123@[::1]:9000': 'wss://[::1]:9000/ws',
  'ABC123@play.example.com': 'wss://play.example.com/ws',
  'ABC123@192.168.1.5:9374': 'wss://192.168.1.5:9374/ws',
  'ABC123@ws://192.168.1.5:9374': 'ws://192.168.1.5:9374/ws',
  'ABC123@localhost': 'ws://localhost:9374/ws',
};
let regOk = true, regDetail = [];
for (const [input, want] of Object.entries(regressionCases)) {
  const got = pr(input).serverAddress;
  const ok = got === want && mainline(input).serverAddress === want;
  if (!ok) regOk = false;
  regDetail.push(input + ' -> ' + got + (ok ? '' : ' (WANT ' + want + ')'));
}
check('A4_pr_no_regression', regOk, regDetail.join(' ; '));

// A5: bare code (no address) unchanged on both variants.
const a5m = mainline('ABC123'), a5p = pr('ABC123');
check('A5_bare_code',
  a5m.serverAddress === undefined && a5p.serverAddress === undefined &&
  a5m.code === 'ABC123' && a5p.code === 'ABC123',
  'both variants return {code:"ABC123"} with no serverAddress');

const obs = {
  issue: 5178,
  generated_at: new Date().toISOString(),
  variants: {
    mainline: 'parseJoinCode extracted verbatim from client/src/services/serverDetection.ts @ base d6197d0d (blob 27177fbf), type-erased only',
    pr: 'same + the PR #5178 patch (endsWith("]") guard), applied byte-identical to the published diff',
  },
  expected_correct: EXPECT,
  mainline_malformed_output: MALFORMED,
  bug_mechanics: 'hostPort "[::1]": lastIndexOf(":")==2 < len-1, so hasPort=true; host="[:", port=parseInt("1]")=1; URL "wss://"+"[:"+" :1"+"/ws" renders as "wss://[::1/ws"',
  assertions: results,
};

const evdir = '/home/hatch/workspace/dev/phase-backfill/evidence/5178/20260916-5178';
fs.writeFileSync(evdir + '/client_observations.json', JSON.stringify(obs, null, 1) + '\n');

const allPass = Object.values(results).every(r => r.status === 'passed');
console.log(allPass ? 'ALL ASSERTIONS PASSED' : 'SOME ASSERTIONS FAILED');
process.exit(allPass ? 0 : 1);
