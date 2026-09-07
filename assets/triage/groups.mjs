import { jobFreshMs } from './model.mjs';
// ---------- Cross-source de-duplication ----------
// The same role is often cross-posted to LinkedIn and Indeed with different
// URLs (so URL-dedupe misses it) but the same company and (usually) title.
// Collapse such records into one card, keyed on normalized title + location,
// then clustered by a compatible company name. The kept "primary" carries
// every member's URL + source so triage and source badges stay correct.
function _normTitle(s) {
  return (s || '').toLowerCase()
    .replace(/\bsr\.?\b/g, 'senior').replace(/\bjr\.?\b/g, 'junior')
    .replace(/&/g, 'and').replace(/[^a-z0-9]/g, '');
}
function _normLoc(s) {
  return (s || '').toLowerCase()
    .replace(/\b(united states|usa|us|california|ca)\b/g, '')
    .replace(/\b(greater|metropolitan|metro|bay area|area)\b/g, '')
    .replace(/[^a-z0-9]/g, '');
}
function _normCo(s) {
  return (s || '').toLowerCase()
    .replace(/\b(inc|llc|llp|ltd|corp|corporation|company|co|group|the|of|and|department|dept)\b/g, '')
    .replace(/[^a-z0-9]/g, '');
}
function _coMatch(a, b) {
  if (!a || !b) return false;
  if (a === b) return true;
  return a.length >= 5 && b.length >= 5 && (a.includes(b) || b.includes(a));
}
function _richness(j) { return (j._salMin != null ? 2 : 0) + (j.description ? 1 : 0); }

export function dedupe(list) {
  const groups = new Map();
  for (const j of list) {
    const k = _normTitle(j.title) + '|' + _normLoc(j.location);
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(j);
  }
  const out = [];
  for (const grp of groups.values()) {
    const companies = grp.map(j => _normCo(j.company));
    const used = new Array(grp.length).fill(false);
    for (let i = 0; i < grp.length; i++) {
      if (used[i]) continue;
      const cluster = [grp[i]]; used[i] = true;
      for (let k = i + 1; k < grp.length; k++) {
        if (!used[k] && _coMatch(companies[i], companies[k])) { cluster.push(grp[k]); used[k] = true; }
      }
      out.push(_mergeCluster(cluster));
    }
  }
  return out;
}
function _mergeCluster(cluster) {
  if (cluster.length === 1) {
    const j = { ...cluster[0] };
    j._srcs = [j._src]; j._dupUrls = [j.url];
    return j;
  }
  const primary = { ...cluster.slice().sort((a, b) =>
    _richness(b) - _richness(a) || (jobFreshMs(b) || 0) - (jobFreshMs(a) || 0))[0] };
  primary._dupUrls = cluster.map(j => j.url);
  primary._srcs = [...new Set(cluster.map(j => j._src))].sort();
  if (primary._salMin == null) {
    const w = cluster.find(j => j._salMin != null);
    if (w) { primary._salMin = w._salMin; primary._salMax = w._salMax; primary._salDisp = w._salDisp; }
  }
  return primary;
}
