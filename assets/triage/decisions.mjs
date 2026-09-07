import { slimJob, jobFreshMs } from './model.mjs';

export const DECIDE_KEY = 'jobTriage:v2';
export const CACHE_KEY = 'jobTriage:cache:v1';
export const TRANSITION_CACHE_KEY = 'jobTriage:cache:v2';
export const LEGACY_KEY = 'jobTriage:v1';
export const TOMB_MS = 60 * 86400000;
export const DISMISS_MS = 30 * 86400000;
const CACHE_CAP = 3000;
const STATUSES = new Set(['saved', 'applied', 'dismissed', null]);
export const decide = (s, t = Date.now()) => ({ s: s || null, t });

export function normalizeTriage(raw) {
  const out = {};
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return out;
  for (const [url, value] of Object.entries(raw)) {
    if (['__proto__', 'constructor', 'prototype'].includes(url)) continue;
    const s = typeof value === 'string' ? value : value?.s;
    const t = typeof value === 'string' ? 0 : value?.t ?? 0;
    if (STATUSES.has(s) && Number.isFinite(t) && t >= 0) out[url] = { s, t };
  }
  return out;
}

export function mergeTriage(base, incoming) {
  const out = { ...(base || {}) };
  for (const [url, inc] of Object.entries(incoming || {})) {
    if (['__proto__', 'constructor', 'prototype'].includes(url)) continue;
    const cur = out[url];
    if (!cur || (inc.t || 0) > (cur.t || 0)) out[url] = inc;
  }
  return out;
}

export function gcDecisions(triage, now = Date.now(), protectedUrls = new Set()) {
  return Object.fromEntries(Object.entries(triage || {}).filter(([url, d]) => {
    if (!d) return false;
    if (!d.t || protectedUrls === null || protectedUrls.has(url)) return true;
    if (!d.s && d.t < now - TOMB_MS) return false;
    return d.s !== 'dismissed' || d.t >= now - DISMISS_MS;
  }));
}

const PRIORITY = { applied: 4, saved: 3, dismissed: 1 };
export function resolveDecision(triage, urls) {
  let best = null;
  for (const url of urls) {
    const d = triage[url];
    if (d && (!best || d.t > best.t || (d.t === best.t && (PRIORITY[d.s] || 0) > (PRIORITY[best.s] || 0)))) best = d;
  }
  return best;
}

export function reconcileAliases(triage, groups) {
  const out = { ...triage };
  for (const group of groups) {
    const urls = group._dupUrls || [group.url];
    const best = resolveDecision(triage, urls);
    if (best) for (const url of urls) out[url] = { ...best };
  }
  return out;
}

export function validJob(j) {
  if (!j || typeof j !== 'object' || Array.isArray(j) || typeof j.url !== 'string') return false;
  try { if (!['http:', 'https:'].includes(new URL(j.url).protocol)) return false; } catch { return false; }
  for (const key of ['title', 'company', 'location', 'date_posted', 'first_seen', 'salary', 'description', 'ats', 'feed']) {
    if (j[key] != null && typeof j[key] !== 'string') return false;
  }
  return j.feeds == null || (Array.isArray(j.feeds) && j.feeds.every(f => typeof f === 'string'));
}

export function mergeJobCaches(...caches) {
  const byUrl = new Map();
  for (const cache of caches) for (const j of (Array.isArray(cache?.jobs) ? cache.jobs : [])) {
    if (validJob(j)) byUrl.set(j.url, { ...byUrl.get(j.url), ...j });
  }
  return { jobs: [...byUrl.values()] };
}

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') return Object.fromEntries(Object.keys(value).sort().map(k => [k, canonical(value[k])]));
  return value;
}
export const equal = (a, b) => JSON.stringify(canonical(a)) === JSON.stringify(canonical(b));

export function writeDecisions(storage, payload) {
  const full = { v: 2, triage: payload.triage, jobs: payload.jobs };
  let evictedCaches = false;
  for (let step = 0; step < 3; step++) {
    const candidate = step === 2 ? { ...full, jobs: [] } : full;
    try {
      if (step === 1) {
        storage.removeItem(CACHE_KEY);
        storage.removeItem(TRANSITION_CACHE_KEY);
        evictedCaches = true;
      }
      storage.setItem(DECIDE_KEY, JSON.stringify(candidate));
      const back = JSON.parse(storage.getItem(DECIDE_KEY));
      if (!equal(back, candidate)) throw new Error('Storage verification failed');
      return { payload: candidate, evictedCaches };
    } catch (error) {
      // Eviction cannot repair access-denied or mismatched read-back errors.
      if (step === 2 || error.name !== 'QuotaExceededError') throw error;
    }
  }
}

export function finishLegacyMigration(storage, written) {
  const legacy = JSON.parse(storage.getItem(LEGACY_KEY));
  if (!legacy) return;
  const back = JSON.parse(storage.getItem(DECIDE_KEY));
  if (!equal(back, written)) return;
  const kept = new Map((back.jobs || []).map(j => [j.url, j]));
  for (const j of mergeJobCaches(legacy).jobs) {
    if (!['saved', 'applied'].includes(back.triage[j.url]?.s)) continue;
    const replacement = kept.get(j.url);
    if (!replacement) return;
    // New metadata may refresh old values, but must not drop the old fields.
    for (const [key, value] of Object.entries(slimJob(j))) {
      if (value != null && value !== '' && (replacement[key] == null || replacement[key] === '')) return;
    }
  }
  storage.removeItem(LEGACY_KEY);
}

export function finishTransitionCacheMigration(storage, writtenJobs) {
  const back = JSON.parse(storage.getItem(CACHE_KEY));
  if (equal(back?.jobs, writtenJobs)) storage.removeItem(TRANSITION_CACHE_KEY);
}

export function createDecisionStore({ storage, locks, now = Date.now, onError = () => {} }) {
  const read = key => JSON.parse(storage.getItem(key));
  const safeRead = key => { try { return read(key); } catch { return null; } };
  const legacyDecisions = legacy => Object.fromEntries(Object.entries(normalizeTriage(legacy?.triage)).map(([url, d]) => [url, { ...d, t: 0 }]));
  const disk = safeRead(DECIDE_KEY), legacy = safeRead(LEGACY_KEY);
  const state = {
    triage: mergeTriage(normalizeTriage(disk?.triage), legacyDecisions(legacy)),
    ...mergeJobCaches(safeRead(CACHE_KEY), safeRead(TRANSITION_CACHE_KEY), legacy, disk),
  };
  let tail = Promise.resolve(), lastIssued = 0, pending = false;
  function snapshot() {
    const stored = safeRead(DECIDE_KEY);
    const triage = mergeTriage(normalizeTriage(stored?.triage), state.triage);
    const jobs = mergeJobCaches(stored, state).jobs.filter(j => triage[j.url]?.s).map(slimJob).sort((a,b) => a.url.localeCompare(b.url));
    return { v: 2, triage, jobs };
  }
  function timestamp(urls) {
    lastIssued = Math.max(now(), lastIssued + 1, (resolveDecision(state.triage, urls)?.t || 0) + 1);
    return lastIssued;
  }
  function commit(mutate = () => {}, { incoming, prepare = () => {}, jobsChanged = false } = {}) {
    const work = async () => {
      let applied = false;
      try {
        // A read error must never become an empty baseline for a destructive write.
        const stored = read(DECIDE_KEY);
        const old = read(LEGACY_KEY);
        const base = mergeTriage(normalizeTriage(stored?.triage), state.triage);
        state.triage = mergeTriage(mergeTriage(base, normalizeTriage(incoming?.triage)), legacyDecisions(old));
        state.jobs = (pending ? mergeJobCaches(incoming, stored, old, state) : mergeJobCaches(old, state, incoming, stored)).jobs;
        applied = true;
        mutate(state, timestamp);
        prepare(state);
        state.triage = gcDecisions(state.triage, now(), new Set(Object.keys(legacyDecisions(old))));
        const payload = { v: 2, triage: state.triage, jobs: state.jobs.filter(j => state.triage[j.url]?.s).map(slimJob).sort((a,b) => a.url.localeCompare(b.url)) };
        let evictedCaches = false;
        if (!equal(stored, payload)) ({ evictedCaches } = writeDecisions(storage, payload));
        pending = false;
        // Cleanup errors leave recovery data in place. They do not invalidate a verified save.
        try { finishLegacyMigration(storage, read(DECIDE_KEY)); } catch { /* retry next save */ }
        if (jobsChanged && !evictedCaches) {
          const jobs = state.jobs.filter(j => !state.triage[j.url]?.s)
            .sort((a,b) => (jobFreshMs(b) || 0) - (jobFreshMs(a) || 0)).slice(0, CACHE_CAP).map(slimJob);
          try {
            storage.setItem(CACHE_KEY, JSON.stringify({ jobs, at: now() }));
            finishTransitionCacheMigration(storage, jobs);
          } catch { /* disposable cache; decisions already verified */ }
        }
        return true;
      } catch (error) {
        // Reading can fail before an action is applied; keep it exportable.
        if (!applied) {
          try { mutate(state, timestamp); prepare(state); } catch { /* retain whatever was recoverable */ }
        }
        pending = true;
        onError(error);
        return false;
      }
    };
    const run = () => locks?.request ? locks.request(DECIDE_KEY, work) : work();
    const result = tail.then(run).catch(error => { pending = true; onError(error); return false; });
    tail = result.then(() => {});
    return result;
  }
  return { state, commit, snapshot, timestamp };
}
