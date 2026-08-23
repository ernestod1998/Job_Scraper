// Dashboard regression tests for disabled scoring, feed-aware source labels,
// cache safety, and the browser seniority veto. No DOM or dependencies needed.

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(root, 'triage.html'), 'utf8');

function extractFunction(src, name) {
  const start = src.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`triage.html no longer defines ${name}()`);
  const open = src.indexOf('{', start);
  let depth = 0;
  for (let i = open; i < src.length; i++) {
    if (src[i] === '{') depth++;
    else if (src[i] === '}' && --depth === 0) return src.slice(start, i + 1);
  }
  throw new Error(`unbalanced ${name}()`);
}

let failed = 0;
function check(name, condition) {
  const ok = Boolean(condition);
  if (!ok) failed++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}`);
}

const sourceFns = ['jobFeeds', 'classifySource'].map(n => extractFunction(html, n)).join('\n');
const { classifySource } = new Function(`${sourceFns}; return { classifySource };`)();

check('biotech provenance overrides ATS vendor',
  classifySource({ ats: 'LinkedIn', feeds: ['general', 'biotech'], url: 'https://example.test' }) === 'Biotech');
for (const ats of ['Greenhouse', 'Lever', 'Ashby', 'Gem', 'Workday']) {
  check(`${ats} general posting is Direct ATS`,
    classifySource({ ats, feeds: ['general'], url: 'https://example.test' }) === 'Direct ATS');
}
check('normalized lowercase ATS is supported',
  classifySource({ ats: 'greenhouse', feeds: [' GENERAL '], url: 'https://example.test' }) === 'Direct ATS');
check('government source retains its own label',
  classifySource({ ats: 'USAJOBS', feeds: ['general'], url: 'https://usajobs.gov/job/1' }) === 'USAJOBS');
check('legacy feed string is accepted',
  classifySource({ ats: 'Custom', feed: 'biotech', url: 'https://example.test' }) === 'Biotech');

const vetoMatch = html.match(/const EXCLUDED_TITLE_RE = (\/.*\/[a-z]*);/);
if (!vetoMatch) throw new Error('EXCLUDED_TITLE_RE not found');
const veto = new Function(`return ${vetoMatch[1]}`)();
for (const title of [
  'Senior Software Engineer', 'Sr. Data Scientist', 'ML Engineering Lead',
  'Product Manager',
  'Staff Engineer', 'Principal Scientist', 'Director of AI',
  'Vice President, Data', 'VP Engineering', 'SVP Research', 'Chief Data Officer',
  'Head of Machine Learning', 'Founding Engineer', 'Distinguished Engineer',
]) check(`seniority veto: ${title}`, veto.test(title));
for (const title of ['Machine Learning Engineer', 'Leadership Program Analyst', 'Managerial Economics Analyst']) {
  check(`seniority boundary allows: ${title}`, !veto.test(title));
}

check('scoring feature flag is disabled', /const ENABLE_SCORING\s*=\s*false\s*;/.test(html));
check('score fetch is feature-gated', /if \(ENABLE_SCORING\)\s*\{[\s\S]*?fetch\('scores\.json'/.test(html));
check('rank control follows feature flag', /view-rank'\)\.hidden\s*=\s*!ENABLE_SCORING/.test(html));
check('v1 is restored as the primary cache', /const CACHE_KEY\s*=\s*'jobTriage:cache:v1'/.test(html));
check('short-lived v2 cache remains readable during recovery',
  /const TRANSITION_CACHE_KEY\s*=\s*'jobTriage:cache:v2'/.test(html));
check('decision storage version unchanged', /jobTriage:v2/.test(html));
check('feeds and ats refresh cached records',
  /const REFRESHABLE = \[[^\]]*'feeds'[^\]]*'ats'[^\]]*\]/.test(html));
check('Direct ATS source facet is present', /\['LinkedIn', 'Biotech', 'Direct ATS'/.test(html));

const repairBiotechSourceCollision = new Function(
  'jobFeeds',
  `${extractFunction(html, 'repairBiotechSourceCollision')}; return repairBiotechSourceCollision;`,
)(j => Array.isArray(j.feeds) ? j.feeds : []);
const staleMeta = repairBiotechSourceCollision({ company: 'Meta', feeds: ['general', 'biotech'] });
const staleOura = repairBiotechSourceCollision({ company: 'ŌURA', feeds: ['biotech'] });
const realBiotech = repairBiotechSourceCollision({ company: 'Metagenomi', feeds: ['biotech'] });
check('cached Meta loses only false biotech provenance',
  staleMeta.feeds.length === 1 && staleMeta.feeds[0] === 'general');
check('cached short-name collision loses false biotech provenance', !staleOura.feeds);
check('real biotech provenance survives cache repair', realBiotech.feeds[0] === 'biotech');

const { mergeJobCaches } = new Function(
  `${extractFunction(html, 'mergeJobCaches')}; return { mergeJobCaches };`
)();
const mergedCache = mergeJobCaches(
  { jobs: [
    { url: 'https://x/v1-only', title: 'Senior Engineer' },
    { url: 'https://x/shared', title: 'Old title', salary: '$100k' },
  ] },
  { jobs: [
    { url: 'https://x/v2-only', title: 'Manager, ML' },
    { url: 'https://x/shared', title: 'Fresh title' },
  ] },
);
const cacheByUrl = new Map(mergedCache.jobs.map(j => [j.url, j]));
check('cache recovery keeps v1-only jobs', cacheByUrl.has('https://x/v1-only'));
check('cache recovery keeps v2-only jobs', cacheByUrl.has('https://x/v2-only'));
check('transition cache refreshes overlaps without dropping old fields',
  cacheByUrl.get('https://x/shared')?.title === 'Fresh title'
  && cacheByUrl.get('https://x/shared')?.salary === '$100k');

const finishMigration = new Function(
  'state', 'readJson', 'localStorage', 'CACHE_KEY', 'TRANSITION_CACHE_KEY',
  `${extractFunction(html, 'finishTransitionCacheMigration')}; return finishTransitionCacheMigration;`,
);
function runMigration(persisted, written) {
  const removed = [];
  const state = { _pendingTransitionCache: true };
  const fn = finishMigration(
    state,
    () => persisted,
    { removeItem: key => removed.push(key) },
    'jobTriage:cache:v1',
    'jobTriage:cache:v2',
  );
  fn(written);
  return { removed, pending: state._pendingTransitionCache };
}
const complete = [{ url: 'https://x/1' }, { url: 'https://x/2' }];
const completedMigration = runMigration({ jobs: complete }, complete);
check('v2 cache is removed only after verified v1 persistence',
  completedMigration.removed[0] === 'jobTriage:cache:v2' && !completedMigration.pending);
const incompleteMigration = runMigration({ jobs: complete.slice(0, 1) }, complete);
check('incomplete v1 writes retain the v2 recovery cache',
  incompleteMigration.removed.length === 0 && incompleteMigration.pending);

// Quota-recovery ladder: the decisions write evicts disposable data (caches,
// then the already-merged legacy blob, then its own cached job copies) rather
// than failing forever, and never touches other keys on a healthy write.
const makeWriteDecisions = new Function(
  'localStorage', 'state', 'console',
  'DECIDE_KEY', 'CACHE_KEY', 'TRANSITION_CACHE_KEY', 'LEGACY_KEY',
  `${extractFunction(html, 'writeDecisions')}; return writeDecisions;`,
);
function runWrite(failWhile) {
  const removed = [];
  const store = {};
  const state = { _pendingLegacy: true };
  const ls = {
    removeItem: k => removed.push(k),
    setItem: (k, v) => {
      if (failWhile(removed, JSON.parse(v))) {
        const e = new Error('quota'); e.name = 'QuotaExceededError'; throw e;
      }
      store[k] = v;
    },
  };
  const fn = makeWriteDecisions(
    ls, state, { warn: () => {}, error: () => {} },
    'jobTriage:v2', 'jobTriage:cache:v1', 'jobTriage:cache:v2', 'jobTriage:v1',
  );
  const payload = { v: 2, triage: { 'https://x/1': { s: 'saved', t: 1 } }, jobs: [{ url: 'https://x/1' }], code: 'c' };
  let result = null, threw = false;
  try { result = fn(payload); } catch { threw = true; }
  return { removed, store, state, result, threw };
}
const healthy = runWrite(() => false);
check('healthy decisions write evicts nothing',
  healthy.removed.length === 0 && healthy.result && !healthy.result.evictedCaches
  && healthy.result.payload.jobs.length === 1);
const afterCacheEvict = runWrite(removed => !removed.includes('jobTriage:cache:v1'));
check('quota pressure evicts the disposable caches first',
  afterCacheEvict.removed.includes('jobTriage:cache:v1')
  && afterCacheEvict.removed.includes('jobTriage:cache:v2')
  && !afterCacheEvict.removed.includes('jobTriage:v1')
  && afterCacheEvict.result.evictedCaches
  && afterCacheEvict.result.payload.jobs.length === 1);
const afterLegacyEvict = runWrite(removed => !removed.includes('jobTriage:v1'));
check('deeper quota pressure evicts the merged legacy blob',
  afterLegacyEvict.removed.includes('jobTriage:v1')
  && afterLegacyEvict.state._pendingLegacy === false
  && afterLegacyEvict.result.payload.jobs.length === 1);
const decisionsOnly = runWrite((removed, payload) => payload.jobs.length > 0);
check('last-resort write keeps every decision and drops only job copies',
  decisionsOnly.result
  && decisionsOnly.result.payload.jobs.length === 0
  && decisionsOnly.result.payload.code === 'c'
  && Object.keys(decisionsOnly.result.payload.triage).length === 1);
check('a write that can never succeed surfaces the error', runWrite(() => true).threw);
check('cache rewrite is skipped after a failed or quota-recovered save',
  /if \(ok && !evictedCaches\) try \{/.test(html));

for (const file of ['triage.yml', 'evals.yml']) {
  const workflow = readFileSync(join(root, '.github', 'workflows', file), 'utf8');
  const triggerBlock = workflow.split(/^jobs:/m)[0];
  check(`${file} retains manual dispatch`, /^\s{2}workflow_dispatch:/m.test(triggerBlock));
  check(`${file} has no schedule trigger`, !/^\s{2}schedule:/m.test(triggerBlock));
  check(`${file} has no push trigger`, !/^\s{2}push:/m.test(triggerBlock));
}

console.log(failed ? `\n${failed} DASHBOARD REGRESSION FAILURE(S)`
                   : '\nAll dashboard regression checks passed');
process.exit(failed ? 1 : 0);
