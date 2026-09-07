// Dashboard regression tests for disabled scoring, feed-aware source labels,
// cache safety, and the browser seniority veto. No DOM or dependencies needed.

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));
const html = readFileSync(join(root, 'assets/triage/app.mjs'), 'utf8');
import { classifySource, EXCLUDED_TITLE_RE as veto, EXCLUDED_SECURITY_RE as secVeto, repairBiotechSourceCollision } from './assets/triage/model.mjs';
import { CACHE_KEY, TRANSITION_CACHE_KEY, DECIDE_KEY, mergeJobCaches, finishTransitionCacheMigration } from './assets/triage/decisions.mjs';

let failed = 0;
function check(name, condition) {
  const ok = Boolean(condition);
  if (!ok) failed++;
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}`);
}

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

for (const title of [
  'Security Engineer', 'Cybersecurity Engineer', 'Cyber Security Engineer',
  'Software Engineer, Cloud Security', 'Application Security Engineer',
  'DevSecOps Engineer', 'Software Engineer, Threat Detection',
  'Software Engineer, Vulnerability Management', 'Machine Learning Engineer, Secure Systems',
  'Software Engineer, iOS - Securing Engineering, USDS',
]) check(`security veto: ${title}`, secVeto.test(title));
for (const title of ['Software Engineer', 'Machine Learning Engineer', 'Data Scientist, Insecurity Index']) {
  check(`security boundary allows: ${title}`, !secVeto.test(title));
}

check('scoring feature flag is disabled', /const ENABLE_SCORING\s*=\s*false\s*;/.test(html));
check('score fetch is feature-gated', /if \(ENABLE_SCORING\)\s*\{[\s\S]*?fetch\('scores\.json'/.test(html));
check('rank control follows feature flag', /view-rank'\)\.hidden\s*=\s*!ENABLE_SCORING/.test(html));
check('v1 is restored as the primary cache', CACHE_KEY === 'jobTriage:cache:v1');
check('short-lived v2 cache remains readable during recovery',
  TRANSITION_CACHE_KEY === 'jobTriage:cache:v2');
check('decision storage version unchanged', DECIDE_KEY === 'jobTriage:v2');
check('feeds and ats refresh cached records',
  /const REFRESHABLE = \[[^\]]*'feeds'[^\]]*'ats'[^\]]*\]/.test(html));
check('Direct ATS source facet is present', /\['LinkedIn', 'Biotech', 'Direct ATS'/.test(html));

const staleMeta = repairBiotechSourceCollision({ company: 'Meta', feeds: ['general', 'biotech'] });
const staleOura = repairBiotechSourceCollision({ company: 'ŌURA', feeds: ['biotech'] });
const realBiotech = repairBiotechSourceCollision({ company: 'Metagenomi', feeds: ['biotech'] });
check('cached Meta loses only false biotech provenance',
  staleMeta.feeds.length === 1 && staleMeta.feeds[0] === 'general');
check('cached short-name collision loses false biotech provenance', !staleOura.feeds);
check('real biotech provenance survives cache repair', realBiotech.feeds[0] === 'biotech');

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

function runMigration(persisted, written) {
  const removed = [];
  const state = { _pendingTransitionCache: true };
  finishTransitionCacheMigration({
    getItem: () => JSON.stringify(persisted),
    removeItem: key => { removed.push(key); state._pendingTransitionCache = false; },
  }, written);
  return { removed, pending: state._pendingTransitionCache };
}
const complete = [{ url: 'https://x/1' }, { url: 'https://x/2' }];
const completedMigration = runMigration({ jobs: complete }, complete);
check('v2 cache is removed only after verified v1 persistence',
  completedMigration.removed[0] === 'jobTriage:cache:v2' && !completedMigration.pending);
const incompleteMigration = runMigration({ jobs: complete.slice(0, 1) }, complete);
check('incomplete v1 writes retain the v2 recovery cache',
  incompleteMigration.removed.length === 0 && incompleteMigration.pending);

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
