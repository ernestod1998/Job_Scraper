// Parity test: the browser's copy of the merge rules vs the server's.
//
// triage.html is deliberately a single self-contained file with no build step,
// so the merge rule necessarily exists twice. Two copies of a rule drift, and
// when THIS rule drifts it corrupts triage data silently — the exact failure
// mode the two-key rewrite was built to eliminate. So rather than trusting a
// "keep in sync with" comment, this test pulls the live functions out of
// triage.html, runs them side by side with the server's, and fails on any
// disagreement.
//
// Run: node sync/merge.test.mjs      (also runs in tests.yml)

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import * as server from './merge.js';

const here = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(here, '..', 'triage.html'), 'utf8');

// Pull a top-level `function name(...) { ... }` out of the inline <script> by
// brace-matching from its opening brace. Regex alone can't do this safely.
function extractFunction(src, name) {
  const start = src.indexOf(`function ${name}(`);
  if (start === -1) throw new Error(`triage.html no longer defines ${name}() — did it get renamed?`);
  const open = src.indexOf('{', start);
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}' && --depth === 0) return src.slice(start, i + 1);
  }
  throw new Error(`unbalanced braces while extracting ${name}()`);
}

const NAMES = ['mergeTriage', 'gcDecisions'];
const client = new Function(
  `${NAMES.map(n => extractFunction(html, n)).join('\n')}
   const TOMB_MS = ${server.TOMB_MS}, DISMISS_MS = ${server.DISMISS_MS};
   return { ${NAMES.join(', ')} };`
)();

// The browser copies must not silently pick up different constants either.
for (const c of ['TOMB_MS', 'DISMISS_MS']) {
  const m = html.match(new RegExp(`${c}\\s*=\\s*([0-9*\\s]+);`));
  if (!m) throw new Error(`triage.html no longer defines ${c}`);
  const value = new Function(`return ${m[1]}`)();
  if (value !== server[c]) {
    throw new Error(`${c} differs: triage.html=${value} sync/merge.js=${server[c]}`);
  }
}

const NOW = 1_800_000_000_000;   // fixed clock: these must not depend on wall time
const DAY = 86400000;
const d = (s, t) => ({ s, t });

let failed = 0;
function parity(name, fn) {
  let a, b, err = null;
  try { a = JSON.stringify(fn(client)); b = JSON.stringify(fn(server)); }
  catch (e) { err = e; }
  const ok = !err && a === b;
  if (!ok) failed++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}`);
  if (err) console.log(`      threw: ${err.message}`);
  else if (!ok) console.log(`      browser: ${a}\n      server:  ${b}`);
}

// ---- mergeTriage ----
parity('newer t wins', m => m.mergeTriage({ a: d('saved', 100) }, { a: d('dismissed', 200) }));
parity('older t cannot erase newer', m => m.mergeTriage({ a: d('dismissed', 200) }, { a: d('saved', 100) }));
parity('equal t keeps the base', m => m.mergeTriage({ a: d('saved', 100) }, { a: d('dismissed', 100) }));
parity('absent locally is adopted', m => m.mergeTriage({}, { a: d('saved', 50) }));
parity('empty incoming erases nothing', m => m.mergeTriage({ a: d('dismissed', 200) }, {}));
parity('null/undefined sides', m => m.mergeTriage(null, undefined));
parity('newer tombstone clears a decision', m => m.mergeTriage({ a: d('dismissed', 100) }, { a: d(null, 200) }));
parity('older tombstone loses', m => m.mergeTriage({ a: d(null, 100) }, { a: d('dismissed', 200) }));
parity('missing t treated as 0', m => m.mergeTriage({ a: { s: 'saved' } }, { a: d('applied', 1) }));
parity('many urls at once', m => m.mergeTriage(
  { a: d('saved', 1), b: d('applied', 5), c: d('dismissed', 9) },
  { a: d('dismissed', 2), b: d('saved', 4), z: d('saved', 7) }));

// ---- gcDecisions ----
parity('old tombstone dropped', m => m.gcDecisions({ a: d(null, NOW - 61 * DAY) }, NOW));
parity('fresh tombstone kept', m => m.gcDecisions({ a: d(null, NOW - 59 * DAY) }, NOW));
parity('old dismissal dropped', m => m.gcDecisions({ a: d('dismissed', NOW - 31 * DAY) }, NOW));
parity('fresh dismissal kept', m => m.gcDecisions({ a: d('dismissed', NOW - 29 * DAY) }, NOW));
parity('old saved KEPT forever', m => m.gcDecisions({ a: d('saved', NOW - 900 * DAY) }, NOW));
parity('old applied KEPT forever', m => m.gcDecisions({ a: d('applied', NOW - 900 * DAY) }, NOW));
parity('t:0 migrated dismissal never aged out',
  m => m.gcDecisions({ a: d('dismissed', 0) }, NOW));
parity('t:0 migrated tombstone never aged out', m => m.gcDecisions({ a: d(null, 0) }, NOW));
parity('mixed bag', m => m.gcDecisions({
  keep1: d('saved', 0), keep2: d('applied', NOW - 400 * DAY), keep3: d('dismissed', NOW - 1 * DAY),
  keep4: d('dismissed', 0), drop1: d('dismissed', NOW - 40 * DAY), drop2: d(null, NOW - 70 * DAY),
}, NOW));
parity('empty and null input', m => m.gcDecisions(null, NOW));

// GC must never resurrect: merge(gc(x), gc(x)) === gc(x)
parity('gc is idempotent under merge', m => {
  const g = m.gcDecisions({ a: d('dismissed', NOW - 40 * DAY), b: d('saved', 1) }, NOW);
  return m.mergeTriage(g, g);
});

console.log(failed ? `\n${failed} PARITY FAILURE(S) — triage.html and sync/merge.js disagree`
                   : '\nAll merge-rule parity checks passed');
process.exit(failed ? 1 : 0);
