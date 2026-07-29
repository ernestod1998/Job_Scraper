// Date-clamping tests for triage.html's inline script.
//
// The 2026-07-28 bug was "derive a calendar day in UTC", and it existed in both
// languages. test_dates.py guards the Python side, but it can only grep the
// browser's copy — it can't run it. This does.
//
// The specific regression this locks down: building today's date by slicing an
// ISO string returns the UTC day, so after 5pm Pacific the dashboard showed
// tomorrow's date on job cards and stamped tomorrow onto export filenames.
//
// Run: node test_dates.mjs      (also runs in tests.yml)

// localToday() reads the *device's* timezone — correct for a browser, since the
// viewer's "today" is the one that matters. But it means these assertions are
// only meaningful from a Pacific clock, and CI runners are UTC: without this
// line every clamp test below passes locally and fails in Actions. Set before
// any Date is constructed.
process.env.TZ = 'America/Los_Angeles';

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
// NB: this file lives at the repo root, unlike sync/merge.test.mjs.
const html = readFileSync(join(here, 'triage.html'), 'utf8');

// Copied from sync/merge.test.mjs — that file exports nothing, runs its
// assertions at import time and calls process.exit(), so it can't be imported.
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

const NAMES = ['localToday', 'displayDate'];
const client = new Function(
  `${NAMES.map(n => extractFunction(html, n)).join('\n')}
   return { ${NAMES.join(', ')} };`
)();

// 2026-07-29T01:30:00Z === 18:30 PDT on 2026-07-28 — inside the window where
// the UTC day and the Pacific day disagree. Fixed clock: these must not depend
// on wall time (same convention as sync/merge.test.mjs).
const EVENING_PT = Date.parse('2026-07-29T01:30:00Z');
const MORNING_PT = Date.parse('2026-07-28T17:00:00Z');  // 10:00 PDT, days agree

let failed = 0;
function check(name, actual, expected) {
  const ok = actual === expected;
  if (!ok) failed++;
  console.log(`${ok ? '  ok  ' : '  FAIL'} ${name}${ok ? '' : ` — got ${JSON.stringify(actual)}, want ${JSON.stringify(expected)}`}`);
}

// The headline case. Slicing toISOString() here would give '2026-07-29'.
check('localToday() at 6:30pm PDT returns the Pacific day',
  client.localToday(EVENING_PT), '2026-07-28');
check('localToday() mid-morning returns the same day',
  client.localToday(MORNING_PT), '2026-07-28');
check('localToday() zero-pads single-digit months and days',
  client.localToday(Date.parse('2026-03-05T20:00:00Z')), '2026-03-05');

// A date that hasn't happened yet is never correct — clamp it. This is what
// covers the ~34 rows already committed to all_jobs.json before the fix, which
// stay in the 14-day window.
check('a future date_posted is clamped to today',
  client.displayDate('2026-07-29', EVENING_PT), '2026-07-28');
check('a far-future date_posted is clamped to today',
  client.displayDate('2027-01-01', EVENING_PT), '2026-07-28');
check('a past date_posted is untouched',
  client.displayDate('2026-07-20', EVENING_PT), '2026-07-20');
check("today's date_posted is untouched",
  client.displayDate('2026-07-28', EVENING_PT), '2026-07-28');

// Workday emits these; rewriting them would destroy information.
check('relative strings pass through',
  client.displayDate('Posted Today', EVENING_PT), 'Posted Today');
check('relative day-count strings pass through',
  client.displayDate('Posted 9 Days Ago', EVENING_PT), 'Posted 9 Days Ago');
check('empty date_posted stays empty',
  client.displayDate('', EVENING_PT), '');
check('missing date_posted stays empty',
  client.displayDate(undefined, EVENING_PT), '');

// Recurrence guard, mirroring RecurrenceGuards in test_dates.py: the browser
// must never derive a calendar day from an ISO string slice.
if (/toISOString\(\)\s*\.\s*slice\(\s*0\s*,\s*10\s*\)/.test(html)) {
  console.log('  FAIL triage.html derives a day from toISOString().slice(0,10) — that is the UTC day');
  failed++;
} else {
  console.log('  ok   triage.html never slices toISOString() for a calendar day');
}

console.log(failed ? `\n${failed} FAILURE(S) in triage.html date handling`
                   : '\nAll triage.html date checks passed');
process.exit(failed ? 1 : 0);
