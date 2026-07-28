// The merge rules for triage decisions.
//
// KEEP IN SYNC WITH: the inline copies in ../triage.html (`mergeTriage` and
// `gcDecisions`). These two implementations MUST behave identically —
// merge.test.mjs extracts the browser's copy and asserts they agree on every
// case, and CI fails if they drift. Divergence here corrupts data silently,
// which is the exact failure mode this whole feature exists to prevent, so if
// you change one you change both.
//
// The dashboard is deliberately a single self-contained HTML file with no build
// step, which is why the rule can't simply be imported in both places.

export const TOMB_MS = 60 * 86400000;       // tombstones: dropped after 60 days
export const DISMISS_MS = 30 * 86400000;    // dismissals: dropped after 30 days

// Per URL, the newer timestamp wins. That single rule is what makes a stale
// window (or a second device) unable to erase a decision it never saw.
export function mergeTriage(base, incoming) {
  const out = { ...(base || {}) };
  for (const url in (incoming || {})) {
    const inc = incoming[url], cur = out[url];
    if (!cur || (inc.t || 0) > (cur.t || 0)) out[url] = inc;
  }
  return out;
}

// Bound the decision map so it can't grow forever and eventually exceed the
// request body cap, which would stop sync permanently.
//
// Safe because all_jobs.json prunes at 14 days (ALL_JOBS_PRUNE_DAYS in
// scrape_jobs.py): any job still in the feed was first seen within 14 days, so
// a dismissal older than 30 days can no longer refer to anything visible, and
// dropping it cannot resurrect a job.
//
//   saved / applied  kept forever — that's the user's application history
//   dismissed        dropped after 30 days
//   tombstones       dropped after 60 days
//   t === 0          NEVER aged out: that's a migrated Phase 1 decision whose
//                    real age is unknown, and it may well refer to a job the
//                    user dismissed yesterday. Ageing those out would silently
//                    un-dismiss everything a migrating user ever dismissed.
//
// Must run on BOTH sides. Merge is a union, so anything only the client drops
// gets merged straight back from the server on the next pull.
export function gcDecisions(triage, nowMs) {
  const now = nowMs == null ? Date.now() : nowMs;
  const out = {};
  for (const url in (triage || {})) {
    const d = triage[url];
    if (!d) continue;
    const t = d.t || 0;
    if (t !== 0) {
      if (!d.s && t < now - TOMB_MS) continue;
      if (d.s === 'dismissed' && t < now - DISMISS_MS) continue;
    }
    out[url] = d;
  }
  return out;
}
