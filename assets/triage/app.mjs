import { bestScore, parseRankings, rankingDetails } from './rankings.mjs?v=20260910-pending';
import { classifyRole, classifySeniority, jobFeeds, classifySource, parseSalary, localToday, displayDate, jobDateMs, jobFreshMs, compareByDate, EXCLUDED_TITLE_RE, EXCLUDED_SECURITY_RE, repairBiotechSourceCollision } from './model.mjs';
import { dedupe } from './groups.mjs?v=20260908-rank3';
import { createDecisionStore, DECIDE_KEY, TOMB_MS, decide, normalizeTriage, mergeTriage, gcDecisions, resolveDecision, reconcileAliases } from './decisions.mjs';
import { createFeedLoader, validateFeed } from './feeds.mjs';

// Old shared links are inert; remove the credential fragment without using it.
if (/^#sync(?:=|$)/i.test(location.hash)) history.replaceState(null, '', location.pathname + location.search);

// ---------- Sources ----------
const SOURCES = [
  { name: 'jobs.json',          src: 'Biotech'  },
  { name: 'linkedin_jobs.json', src: 'LinkedIn' },
  { name: 'indeed_jobs.json',   src: 'Indeed'   },
  { name: 'usajobs_jobs.json',  src: 'USAJOBS'  },
  { name: 'governmentjobs_jobs.json', src: 'NEOGOV' },
  { name: 'calopps_jobs.json',  src: 'CalOpps'  },
  { name: 'calcareers_jobs.json', src: 'CalCareers' },
  // Cumulative master kept by the scrapers (the rolling files above only hold
  // their latest window). Lets the Rank tab show every recently-seen role.
  { name: 'all_jobs.json',      src: 'Master'   },
];

// Daily ranking is separate from local save/apply/dismiss decisions.
const ENABLE_SCORING = true;
document.getElementById('view-rank').hidden = !ENABLE_SCORING;

// Sanitized daily resume comparisons, keyed by job URL.
let SCORES = {};
let rankingUpdate = '';
let rankingUnavailable = false;

const store = createDecisionStore({ storage: { getItem: k => localStorage.getItem(k), setItem: (k,v) => localStorage.setItem(k,v), removeItem: k => localStorage.removeItem(k) }, locks: navigator.locks, onError: reportSaveError });
const state = store.state;
let aliases = new Map();
function tri(url) { return resolveDecision(state.triage, aliases.get(url) || [url])?.s || null; }
let VIEW = [];   // deduped view of state.jobs, recomputed each render

function enrich(j) {
  j._role = classifyRole(j.title);
  j._sen  = classifySeniority(j.title);
  j._src  = classifySource(j);
  const sal = parseSalary(j);
  j._salMin = sal ? sal.min : null;
  j._salMax = sal ? sal.max : null;
  j._salDisp = sal ? sal.disp : '';
  const s = SCORES[j.url];
  j._score = bestScore(s);
  j._ranking = s;
  j._verdict = j._score == null ? null : 'Luna qualification match';
  j._family = null;
  j._why = '';
  j._opener = '';
  j._judgeOk = null;
  j._judgeNote = '';

}

// ---------- Data loading ----------
let SEED = { scraped_at: '', new_count: 0, jobs: [], new_jobs: [] };

const feedLoader = createFeedLoader(SOURCES);
async function loadJobs(options) {
  if (ENABLE_SCORING) {
    try {
      const r = await fetch('ranking_results.json?v=' + Date.now());
      if (!r.ok) throw new Error('Ranking data unavailable');
      const data = parseRankings(await r.json());
      SCORES = data.scores;
      rankingUpdate = data.updated_at || '';
      rankingUnavailable = false;
    } catch { SCORES = {}; rankingUnavailable = true; }
  }
  return feedLoader.load(options);
}
// ---------- Merge (additive — preserves triage history) ----------
// Fields refreshed from each fresh scrape onto already-cached jobs (e.g. a
// salary backfilled later). Triage lives separately in state.triage, so
// updating a job's content is safe and never loses a decision.
const REFRESHABLE = ['salary', 'location', 'company', 'title', 'date_posted', 'description', 'feeds', 'ats'];
function mergeJobs(incoming) {
  const seen = new Map(state.jobs.map(j => [j.url, j]));
  let added = 0;
  for (const j of incoming) {
    const existing = seen.get(j.url);
    if (existing) {
      for (const key of REFRESHABLE) {
        if (j[key] != null && j[key] !== '' && j[key] !== existing[key]) existing[key] = j[key];
      }
      if (j.first_seen && !existing.first_seen) existing.first_seen = j.first_seen;
    } else {
      state.jobs.push(j);
      seen.set(j.url, j);  // prevent internal dupes within incoming
      added++;
    }
  }
  return added;
}

// ---------- Filters ----------
const filters = {
  source: new Set(),
  role: new Set(),
  sen: new Set(),
  state: new Set(['active', 'saved', 'applied']),
  days: null,   // null = any time; else an integer day-window (7 = last 7 days)
  salMin: 0,        // minimum annual salary (USD); 0 = no floor
  salUnknown: false, // when a floor is set, exclude roles with no listed salary
  q: '',
  sort: 'newest',   // 'newest' | 'oldest' — date order of the visible list
};

// 'browse' = the classic date-ordered list; 'rank' = fit-score order.
let viewMode = 'browse';
let visibleLimit = 50, lastFilterKey = '';
function resetLimitForFilters() {
  const key = JSON.stringify(filters, (_, v) => v instanceof Set ? [...v].sort() : v);
  if (key !== lastFilterKey) { visibleLimit = 50; lastFilterKey = key; }
}

// `except` skips one dimension so a facet's pills don't constrain their own counts.
function matchesFilters(j, except) {
  if (viewMode === 'rank' && ['applied', 'dismissed'].includes(tri(j.url))) return false;
  // Merged cross-posts carry every source in _srcs (single jobs: [_src]).
  const srcs = j._srcs || [j._src];
  if (except !== 'source' && filters.source.size && !srcs.some(s => filters.source.has(s))) return false;
  if (except !== 'role'   && filters.role.size   && !filters.role.has(j._role)) return false;
  if (except !== 'sen'    && filters.sen.size    && !filters.sen.has(j._sen))   return false;
  if (except !== 'state') {
    const st = tri(j.url) || 'active';
    if (!filters.state.has(st)) return false;
  }
  if (except !== 'days' && filters.days != null) {
    const t = filters.days < 1 ? jobFreshMs(j) : jobDateMs(j);
    if (t == null || t < Date.now() - filters.days * 86400000) return false;
  }
  if (except !== 'salary' && filters.salMin > 0) {
    if (j._salMax == null) { if (!filters.salUnknown) return false; }
    else if (j._salMax < filters.salMin) return false;
  }
  if (except !== 'q') {
    const q = filters.q.trim().toLowerCase();
    if (q && !(`${j.title} ${j.company} ${j.location}`.toLowerCase().includes(q))) return false;
  }
  return true;
}

function visibleJobs() {
  return VIEW.filter(j => matchesFilters(j));
}

// ---------- Render ----------
const $ = id => document.getElementById(id);

function renderFilterGroup(containerId, key, values) {
  const el = $(containerId);
  el.innerHTML = '';
  const counts = {};
  VIEW.forEach(j => {
    if (!matchesFilters(j, key)) return;          // honor every filter except this facet's
    if (key === 'source') { (j._srcs || [j._src]).forEach(s => counts[s] = (counts[s] || 0) + 1); return; }
    const v = key === 'role' ? j._role : j._sen;
    counts[v] = (counts[v] || 0) + 1;
  });
  values.forEach(v => {
    const ct = counts[v] || 0;
    const active = filters[key].has(v);
    if (!ct && !active) return;                   // hide empties, but never an active pill
    const p = document.createElement('button');
    p.type = 'button';
    p.className = 'pill' + (active ? ' on' : '');
    p.setAttribute('aria-pressed', active ? 'true' : 'false');
    p.innerHTML = `${escape(v)}<span class="ct">${ct}</span>`;
    p.onclick = () => {
      if (filters[key].has(v)) filters[key].delete(v);
      else filters[key].add(v);
      renderAll();
    };
    el.appendChild(p);
  });
}

function renderDateFilter() {
  const el = $('filter-date');
  el.innerHTML = '';
  const opts = [
    [1/24, 'Last hour'],
    [1,  'Today'],
    [3,  '3 days'],
    [7,  'Week'],
    [30, 'Month'],
    [null, 'Any time'],
  ];
  opts.forEach(([days, label]) => {
    const ct = VIEW.filter(j => {
      if (!matchesFilters(j, 'days')) return false;   // honor Source/Role/Seniority/Show/search
      if (days == null) return true;
      const t = days < 1 ? jobFreshMs(j) : jobDateMs(j);
      return t != null && t >= Date.now() - days * 86400000;
    }).length;
    const active = filters.days === days;
    const p = document.createElement('button');
    p.type = 'button';
    p.className = 'pill' + (active ? ' on' : '');
    p.setAttribute('aria-pressed', active ? 'true' : 'false');
    p.innerHTML = `${label}<span class="ct">${ct}</span>`;
    p.onclick = () => { filters.days = (filters.days === days ? null : days); renderAll(); };
    el.appendChild(p);
  });
}

// Radio-style (unlike the facet pills): the list always has a date order, so
// clicking the active one is a no-op rather than clearing it.
function renderSortFilter() {
  const el = $('filter-sort');
  el.innerHTML = '';
  [['newest', '↓ Newest first'], ['oldest', '↑ Oldest first']].forEach(([k, label]) => {
    const active = filters.sort === k;
    const p = document.createElement('button');
    p.type = 'button';
    p.className = 'pill' + (active ? ' on' : '');
    p.setAttribute('aria-pressed', active ? 'true' : 'false');
    p.textContent = label;
    p.onclick = () => { filters.sort = k; renderAll(); };
    el.appendChild(p);
  });
}

function renderStateFilter() {
  const el = $('filter-state');
  el.innerHTML = '';
  const opts = [
    ['active', 'Untriaged'],
    ['saved', 'Saved'],
    ['applied', 'Applied'],
    ['dismissed', 'Dismissed'],
  ];
  const counts = { active: 0, saved: 0, applied: 0, dismissed: 0 };
  VIEW.forEach(j => {
    if (!matchesFilters(j, 'state')) return;
    counts[tri(j.url) || 'active']++;
  });
  opts.forEach(([k, label]) => {
    if (viewMode === 'rank' && ['applied', 'dismissed'].includes(k)) return;
    const active = filters.state.has(k);
    const p = document.createElement('button');
    p.type = 'button';
    p.className = 'pill' + (active ? ' on' : '');
    p.setAttribute('aria-pressed', active ? 'true' : 'false');
    p.innerHTML = `${label}<span class="ct">${counts[k]}</span>`;
    p.onclick = () => {
      if (filters.state.has(k)) filters.state.delete(k);
      else filters.state.add(k);
      renderAll();
    };
    el.appendChild(p);
  });
}

function renderKpis() {
  $('kpi-total').textContent = VIEW.length;
  $('kpi-new').textContent = (SEED.new_jobs || []).length || SEED.new_count || 0;
  const saved = VIEW.filter(j => tri(j.url) === 'saved').length;
  const applied = VIEW.filter(j => tri(j.url) === 'applied').length;
  $('kpi-saved').textContent = saved;
  $('kpi-applied').textContent = applied;
}

function renderCharts() {
  const jobs = visibleJobs();
  const byCo = {};
  jobs.forEach(j => byCo[j.company] = (byCo[j.company] || 0) + 1);
  const topCo = Object.entries(byCo).sort((a, b) => b[1] - a[1]).slice(0, 8);
  const maxCo = topCo[0] ? topCo[0][1] : 1;
  $('chart-companies').innerHTML = topCo.map(([name, n]) =>
    `<div class="barrow"><span class="name" title="${escape(name)}">${escape(name)}</span>
     <div class="bar"><span style="width:${(n/maxCo)*100}%"></span></div>
     <span class="count">${n}</span></div>`).join('') ||
    '<div style="color:var(--muted);font-size:13px">No jobs match current filters.</div>';

  const byRole = {};
  jobs.forEach(j => byRole[j._role] = (byRole[j._role] || 0) + 1);
  const topRole = Object.entries(byRole).sort((a, b) => b[1] - a[1]);
  const maxRole = topRole[0] ? topRole[0][1] : 1;
  $('chart-roles').innerHTML = topRole.map(([name, n]) =>
    `<div class="barrow"><span class="name">${escape(name)}</span>
     <div class="bar"><span style="width:${(n/maxRole)*100}%"></span></div>
     <span class="count">${n}</span></div>`).join('') ||
    '<div style="color:var(--muted);font-size:13px">—</div>';
}

function escape(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function scoreChip(j) {
  if (!ENABLE_SCORING) return '';
  if (j._score == null) return viewMode === 'rank'
    ? '<span class="scorechip lo" title="not scored yet">–</span>' : '';
  const cls = j._score >= 75 ? 'hi' : j._score >= 50 ? 'mid' : 'lo';
  const tip = `${j._verdict || ''}${j._family ? ' · ' + j._family : ''}`;
  // Score-audit marker (triage_agent --judge): ✅ judge agrees, ⚖️ judge skeptical.
  // Nothing when un-judged (_judgeOk === null).
  const mark = (j._judgeOk === true)
    ? ` <span class="judgemark ok" title="${escape('judge ✓: ' + (j._judgeNote || 'score looks justified'))}">✅</span>`
    : (j._judgeOk === false)
    ? ` <span class="judgemark warn" title="${escape('judge: ' + (j._judgeNote || 'score may be off'))}">⚖️</span>`
    : '';
  return `<span class="scorechip ${cls}" title="${escape(tip)}">${j._score}</span>${mark}`;
}

function renderJobs() {
  resetLimitForFilters();
  const allVisible = visibleJobs();
  $('results-heading').textContent = `${allVisible.length.toLocaleString()} matching roles`;
  $('load-more').hidden = viewMode === 'map' || allVisible.length <= visibleLimit;
  $('page-count').textContent = viewMode === 'map' ? '' : `Showing ${Math.min(visibleLimit, allVisible.length).toLocaleString()} of ${allVisible.length.toLocaleString()}`;
  const listEl = $('jobs'), mapEl = $('map'), mapNote = $('map-note');
  if (viewMode === 'map') {
    listEl.hidden = true; mapEl.hidden = false; $('rank-info').hidden = true;
    renderMap();
    return;
  }
  if (mapEl) { listEl.hidden = false; mapEl.hidden = true; if (mapNote) mapNote.hidden = true; }
  let jobs = visibleJobs();
  const info = $('rank-info');
  if (ENABLE_SCORING && viewMode === 'rank') {
    // Fit-score order; unscored sink to the bottom (never hidden). The Sort
    // pills break score ties, so equally-scored roles still read newest-first.
    jobs = jobs.slice().sort((a, b) =>
      ((b._score ?? -1) - (a._score ?? -1)) || compareByDate(a, b, filters.sort));
    const scored = jobs.filter(j => j._score != null).length;
    const reviewed = jobs.filter(j => j._ranking?.sonnet && j._score != null).length;
    info.hidden = false;
    info.textContent = `Daily ranking · ${scored} Luna-scored jobs · ${reviewed} Sonnet reviews. Applied and dismissed jobs are hidden. The worker selects newest postings first, up to 50 jobs + 5 reviews daily. ${rankingUnavailable ? 'Ranking data currently unavailable.' : rankingUpdate ? 'Updated ' + new Date(rankingUpdate).toLocaleString() + '.' : 'First daily run pending.'} New results are published during the run; use Refresh to load them. Unscored roles appear last.`;
  } else {
    jobs = jobs.slice().sort((a, b) => compareByDate(a, b, filters.sort));
    info.hidden = true;
  }
  const el = $('jobs');
  if (!jobs.length) {
    el.innerHTML = '<div class="empty">No jobs match the current filters.</div>';
    return;
  }
  el.innerHTML = jobs.slice(0, visibleLimit).map(j => {
    const st = tri(j.url) || 'active';
    const srcs = j._srcs || [j._src];
    const srcBadges = srcs.map(s => {
      const cls = s.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
      return `<span class="src ${cls}">${s}</span>`;
    }).join('');
    const sal = j._salDisp || j.salary;
    return `<div class="job" data-url="${escape(j.url)}" data-state="${st}">
      <div class="job-main">
        <div class="title">${scoreChip(j)}<a href="${escape(j.url)}" target="_blank" rel="noopener noreferrer">${escape(j.title)}</a></div>
        <div class="meta">
          <span class="co">${escape(j.company)}</span>
          <span class="src-item">🔗 ${srcBadges}</span>
          <span>📍 ${escape(j.location || 'Not specified')}</span>
          ${sal ? `<span>💰 ${escape(sal)}</span>` : ''}
          <span>🕒 ${escape(displayDate(j.date_posted))}</span>
          <span><span class="tag role">${j._role}</span><span class="tag">${j._sen}</span>${j._family ? `<span class="tag">${escape(j._family)}</span>` : ''}</span>
        </div>
        ${viewMode === 'rank' ? rankingDetails(j._ranking) : ''}
      </div>
      <div class="actions">
        ${j._opener ? '<button type="button" class="act" data-act="opener">✉ Opener</button>' : ''}
        <button type="button" class="act save ${st==='saved'?'on':''}" data-act="saved" aria-pressed="${st==='saved'}">★ Save</button>
        <button type="button" class="act applied ${st==='applied'?'on':''}" data-act="applied" aria-pressed="${st==='applied'}">✓ Applied</button>
        <button type="button" class="act dismiss ${st==='dismissed'?'on':''}" data-act="dismissed" aria-pressed="${st==='dismissed'}">✕ Dismiss</button>
      </div>
    </div>`;
  }).join('');
}

// ---------- Salary floor control + distribution histogram ----------
function renderSalaryControl() {
  const v = filters.salMin;
  const lbl = $('sal-val');
  if (!lbl) return;
  if (v <= 0) { lbl.textContent = 'Any'; }
  else {
    const pass = VIEW.filter(j => matchesFilters(j, 'salary')
      && (j._salMax != null ? j._salMax >= v : filters.salUnknown)).length;
    lbl.textContent = `≥ $${Math.round(v / 1000)}k/yr · ${pass}`;
  }
}

// Bucketed histogram of the current selection (every filter EXCEPT the salary
// floor, so the bars stay put while you drag the slider). Bars are clickable
// to set the floor; buckets below the active floor dim out.
const SAL_BUCKETS = [
  [0, 50000, '<50'], [50000, 75000, '50'], [75000, 100000, '75'],
  [100000, 125000, '100'], [125000, 150000, '125'], [150000, 175000, '150'],
  [175000, 200000, '175'], [200000, 250000, '200'], [250000, Infinity, '250+'],
];
function renderSalaryChart() {
  const host = $('chart-salary'), note = $('salary-note');
  if (!host) return;
  const pool = VIEW.filter(j => matchesFilters(j, 'salary'));
  const priced = pool.filter(j => j._salMin != null);
  if (!priced.length) {
    host.className = 'histo-empty';
    host.textContent = pool.length
      ? `No listed salaries among ${pool.length} selected role(s).`
      : 'No roles selected.';
    note.textContent = '';
    return;
  }
  host.className = 'histo';
  const mids = priced.map(j => (j._salMin + j._salMax) / 2);
  const counts = SAL_BUCKETS.map(([lo, hi]) => mids.filter(m => m >= lo && m < hi).length);
  const max = Math.max(...counts, 1);
  host.innerHTML = SAL_BUCKETS.map(([lo, hi, lab], i) => {
    const n = counts[i];
    const h = n ? Math.max(5, Math.round((n / max) * 100)) : 0;
    const dim = filters.salMin > 0 && lo < filters.salMin ? ' dim' : '';
    const range = `$${Math.round(lo / 1000)}k` + (hi === Infinity ? '+' : `–$${Math.round(hi / 1000)}k`);
    return `<div class="hbar${dim}" role="button" tabindex="0" aria-pressed="${filters.salMin === lo}" data-floor="${lo}" title="${range}/yr: ${n} role${n === 1 ? '' : 's'} (click to set floor)">
      <div class="fill" style="height:${h}%">${n ? `<span class="cnt">${n}</span>` : ''}</div>
      <span class="lab">${lab}</span></div>`;
  }).join('');
  const sorted = mids.slice().sort((a, b) => a - b);
  const med = sorted[Math.floor(sorted.length / 2)];
  const unpriced = pool.length - priced.length;
  note.innerHTML = `${priced.length} priced role(s) · median ≈ <strong>$${Math.round(med / 1000)}k/yr</strong>`
    + (unpriced ? ` · ${unpriced} with no listed salary` : '')
    + ' &nbsp;·&nbsp; <span>x-axis = $k floor</span>';
}

// ---------- Map view (Leaflet) ----------
// Client-side geocoding via a static city/region → [lat,lng] table (no API
// key, no network). Jobs cluster by location; the view auto-fits to wherever
// they are. Remote/unknown roles get a single marker at the US/CA center.
const CITY_COORDS = {
  'sacramento': [38.5816, -121.4944], 'west sacramento': [38.5805, -121.5302],
  'davis': [38.5449, -121.7405], 'rancho cordova': [38.5891, -121.3027],
  'elk grove': [38.4088, -121.3716], 'roseville': [38.7521, -121.288],
  'folsom': [38.678, -121.176], 'woodland': [38.6785, -121.7733],
  'san francisco': [37.7749, -122.4194], 'south san francisco': [37.6547, -122.4077],
  'oakland': [37.8044, -122.2712], 'berkeley': [37.8715, -122.273],
  'emeryville': [37.8313, -122.2852], 'richmond': [37.9358, -122.3477],
  'palo alto': [37.4419, -122.143], 'mountain view': [37.3861, -122.0839],
  'menlo park': [37.4538, -122.1817], 'sunnyvale': [37.3688, -122.0363],
  'santa clara': [37.3541, -121.9552], 'san jose': [37.3382, -121.8863],
  'san mateo': [37.563, -122.3255], 'redwood city': [37.4852, -122.2364],
  'fremont': [37.5485, -121.9886], 'hayward': [37.6688, -122.0808],
  'concord': [37.978, -122.0311], 'walnut creek': [37.9101, -122.0652],
  'pleasanton': [37.6624, -121.8747], 'livermore': [37.6819, -121.768],
  'novato': [38.1074, -122.5697], 'san rafael': [37.9735, -122.5311],
  'vacaville': [38.3566, -121.9877], 'vallejo': [38.1041, -122.2566],
  'fairfield': [38.2494, -122.0399], 'napa': [38.2975, -122.2869],
  'santa rosa': [38.4405, -122.7144], 'stockton': [37.9577, -121.2908],
  'modesto': [37.6391, -120.9969], 'fresno': [36.7378, -119.7871],
  'los angeles': [34.0522, -118.2437], 'long beach': [33.7701, -118.1937],
  'pasadena': [34.1478, -118.1445], 'santa monica': [34.0195, -118.4912],
  'irvine': [33.6846, -117.8265], 'san diego': [32.7157, -117.1611],
  'santa barbara': [34.4208, -119.6982], 'san luis obispo': [35.2828, -120.6596],
  'monterey': [36.6002, -121.8947], 'san carlos': [37.5072, -122.2605],
  'foster city': [37.5585, -122.2711], 'milpitas': [37.4323, -121.8996],
  'cupertino': [37.323, -122.0322], 'daly city': [37.6879, -122.4702],
  'santa cruz': [36.9741, -122.0308], 'santa clara county': [37.3337, -121.8907],
  'alameda county': [37.6017, -121.7195], 'san mateo county': [37.4337, -122.3573],
  // ---- Federal hubs / major US cities (e.g. USAJOBS roles) ----
  'washington, district of columbia': [38.9072, -77.0369], 'district of columbia': [38.9072, -77.0369],
  'bethesda': [38.9847, -77.0947], 'rockville': [39.084, -77.1528],
  'research triangle park': [35.8989, -78.8636], 'raleigh': [35.7796, -78.6382],
  'cincinnati': [39.1031, -84.512], 'atlanta': [33.749, -84.388],
  'denver': [39.7392, -104.9903], 'seattle': [47.6062, -122.3321],
  'las vegas': [36.1699, -115.1398], 'ann arbor': [42.2808, -83.743],
  'kansas city': [39.0997, -94.5786], 'chicago': [41.8781, -87.6298],
  'boston': [42.3601, -71.0589], 'philadelphia': [39.9526, -75.1652],
  'new york': [40.7128, -74.006], 'houston': [29.7604, -95.3698],
  'dallas': [32.7767, -96.797], 'phoenix': [33.4484, -112.074],
  'anchorage': [61.2181, -149.9003], 'honolulu': [21.3069, -157.8583],
  'portland, or': [45.5152, -122.6784], 'austin': [30.2672, -97.7431],
};
const CA_CENTER = [37.4, -119.5];
const US_CENTROID = [39.5, -98.0];
const US_STATE_RE = /\b(alabama|alaska|arizona|arkansas|colorado|connecticut|delaware|florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|new hampshire|new jersey|new mexico|new york|north carolina|north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|wisconsin|wyoming|district of columbia)\b/;
let _map = null, _markerLayer = null, _lastFitKey = '';

// Longest-name match wins ("south san francisco" beats "san francisco").
function geocodeJob(loc) {
  const s = (loc || '').toLowerCase();
  let best = null, bestLen = 0;
  for (const city in CITY_COORDS) {
    if (s.includes(city) && city.length > bestLen) { best = CITY_COORDS[city]; bestLen = city.length; }
  }
  if (best) return best;
  if (US_STATE_RE.test(s)) return US_CENTROID;   // matched a US state but no city
  return null;                                    // → remote/unknown bucket
}

function renderMap() {
  const note = $('map-note');
  if (typeof L === 'undefined') {
    note.hidden = false;
    note.textContent = 'Map library failed to load (offline?). The list views still work.';
    return;
  }
  if (!_map) {
    _map = L.map('map', { scrollWheelZoom: true }).setView(CA_CENTER, 6);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
      maxZoom: 18, attribution: '© OpenStreetMap'
    }).addTo(_map);
    _markerLayer = L.layerGroup().addTo(_map);
  }
  setTimeout(() => _map.invalidateSize(), 0);   // container was hidden → fix sizing
  _markerLayer.clearLayers();

  const jobs = visibleJobs();
  const buckets = new Map();
  let remoteCount = 0;
  jobs.forEach(j => {
    const c = geocodeJob(j.location);
    if (!c) { remoteCount++;
      const k = 'remote';
      if (!buckets.has(k)) buckets.set(k, { lat: CA_CENTER[0], lng: CA_CENTER[1], remote: true, jobs: [] });
      buckets.get(k).jobs.push(j); return;
    }
    const k = c[0] + ',' + c[1];
    if (!buckets.has(k)) buckets.set(k, { lat: c[0], lng: c[1], remote: false, jobs: [] });
    buckets.get(k).jobs.push(j);
  });

  let maxN = 1;
  buckets.forEach(b => { if (b.jobs.length > maxN) maxN = b.jobs.length; });
  buckets.forEach(b => {
    const n = b.jobs.length;
    const r = 7 + 13 * Math.sqrt(n / maxN);
    const top = b.jobs.slice().sort((x, y) => (y._score ?? -1) - (x._score ?? -1)).slice(0, 12);
    const list = top.map(j =>
      `<div style="margin:3px 0"><a href="${escape(j.url)}" target="_blank" rel="noopener">${escape(j.title)}</a>`
      + `<br><span style="color:#888">${escape(j.company)}${j._salDisp ? ' · ' + escape(j._salDisp) : ''}</span></div>`).join('');
    const where = b.remote ? 'Remote / Unknown' : escape(top[0].location || '');
    const m = L.circleMarker([b.lat, b.lng], {
      radius: r, color: b.remote ? '#a78bfa' : '#34d399',
      fillColor: b.remote ? '#a78bfa' : '#34d399', fillOpacity: 0.55, weight: 1.5,
    }).addTo(_markerLayer);
    m.bindTooltip(`${where}: ${n} role${n === 1 ? '' : 's'}`, { direction: 'top' });
    m.bindPopup(`<strong>${where}</strong> — ${n} role${n === 1 ? '' : 's'}`
      + `${n > top.length ? ` (top ${top.length})` : ''}<div style="max-height:240px;overflow:auto;margin-top:4px">${list}</div>`,
      { maxWidth: 320 });
  });

  // Auto-fit to wherever the jobs are; only refit when the extent changes.
  const pts = [...buckets.values()].map(b => [b.lat, b.lng]);
  const key = pts.map(p => p.join(',')).sort().join('|');
  if (pts.length && key !== _lastFitKey) {
    _map.fitBounds(pts, { padding: [34, 34], maxZoom: 9 });
    _lastFitKey = key;
  } else if (!pts.length) {
    _map.setView(CA_CENTER, 5); _lastFitKey = '';
  }

  note.hidden = false;
  note.textContent = `${jobs.length} role(s) mapped · green = located, purple = remote/unknown (${remoteCount}). `
    + 'Hover a dot for the location, click for the roles.';
}

function renderAll() {
  const focused = document.activeElement;
  const card = focused?.closest('.job');
  const url = card?.dataset.url;
  const action = focused?.dataset.act;
  const oldCards = [...$('jobs').querySelectorAll('.job')];
  const index = card ? oldCards.indexOf(card) : -1;
  const facet = focused?.parentElement?.id?.startsWith('filter-') ? focused.parentElement.id : null;
  const label = facet ? focused.childNodes[0]?.textContent : null;
  const salaryFloor = focused?.closest('.hbar')?.dataset.floor;


  renderFilterGroup('filter-source', 'source',
    ['LinkedIn', 'Biotech', 'Direct ATS', 'Indeed', 'USAJOBS', 'NEOGOV', 'CalOpps', 'CalCareers']);
  renderFilterGroup('filter-role', 'role',
    ['AI Engineer', 'MLE', 'MLOps/Platform', 'Scientist', 'Comp Chem/Sci',
     'Data Science', 'SWE', 'Bioinformatics', 'Other']);
  // Senior-track/management roles are excluded at the scraper;
  // pruneState() clears any lingering untriaged ones from older sessions.
  renderFilterGroup('filter-sen', 'sen', ['Senior', 'Mid', 'Intern']);
  renderDateFilter();
  renderSortFilter();
  renderStateFilter();
  renderSalaryControl();
  renderKpis();
  renderCharts();
  renderSalaryChart();
  renderJobs();
  validateUndo();
  if (url) {
    const cards = [...$('jobs').querySelectorAll('.job')];
    const next = cards.find(c => (aliases.get(c.dataset.url) || [c.dataset.url]).includes(url)) || cards[Math.min(index, cards.length - 1)];
    (next?.querySelector(`[data-act="${action}"]`) || next?.querySelector('a') || $('results-heading')).focus({ preventScroll: true });
  } else if (salaryFloor != null) {
    $('chart-salary').querySelector(`[data-floor="${salaryFloor}"]`)?.focus({ preventScroll: true });
  } else if (facet) {
    const replacement = [...$(facet).children].find(b => b.childNodes[0]?.textContent === label);
    (replacement || $('results-heading')).focus({ preventScroll: true });
  }
}

// Storage failures remain visible until a verified retry succeeds.
function reportSaveError(error) {
  const el = $('save-error');
  el.hidden = false;
  el.textContent = `Couldn't save your decisions (${error.name || 'storage error'}). Keep this tab open and Export a backup. `;
  const retry = document.createElement('button');
  retry.className = 'iconbtn'; retry.textContent = 'Retry save'; retry.onclick = () => save();
  el.append(retry);
}
async function save(mutate, options) {
  const ok = await store.commit(mutate, { ...options, prepare: prepareJobs });
  if (ok) $('save-error').hidden = true;
  renderAll();
  return ok;
}
function prepareJobs() {
  state.jobs.forEach(repairBiotechSourceCollision);
  state.jobs.forEach(enrich);
  rebuildGroups();
  state.triage = reconcileAliases(state.triage, VIEW);
  pruneState();
  rebuildGroups();
}
let groupKey = '';
function rebuildGroups() {
  const key = JSON.stringify(state.jobs.map(j => [j.url, j.title, j.company, j.location, j.date_posted, j.first_seen, j._src, j._salMin, j._salMax, j._salDisp, j.salary, j.description, j._score, j._why, j._ranking]));
  if (key === groupKey) return;
  groupKey = key;
  VIEW = dedupe(state.jobs);
  aliases = new Map();
  for (const group of VIEW) for (const url of group._dupUrls) aliases.set(url, group._dupUrls);
}
// ---------- Prune (keeps localStorage bounded) ----------
// Drops untriaged jobs that are senior-track/exec (excluded at the scraper
// since 2026-06 — keep this regex in sync with EXCLUDED_SENIORITY_RE in
// scrape_jobs.py) or older than 30 days. Anything saved/applied/dismissed
// is kept — that's the user's triage history.
function pruneState() {
  const cutoff = Date.now() - 30 * 86400000;
  const before = state.jobs.length;
  state.jobs = state.jobs.filter(j => {
    repairBiotechSourceCollision(j);
    if (tri(j.url)) return true;   // tri(), not truthiness — a tombstone is an object
    if (EXCLUDED_TITLE_RE.test(j.title || '')) return false;
    if (EXCLUDED_SECURITY_RE.test(j.title || '')) return false;
    const t = jobFreshMs(j);
    return t == null || t >= cutoff;
  });
  if (state.jobs.length < before) {
    console.info(`[pruneState] dropped ${before - state.jobs.length} stale/excluded job(s)`);
  }
}

// ---------- Refresh flow ----------
let refreshGeneration = 0;
async function refresh(options = {}) {
  const generation = ++refreshGeneration;
  $('snapshot-line').textContent = 'Refreshing job sources…';
  $('refresh-btn').setAttribute('aria-busy', 'true');
  try {
    const result = await loadJobs(options);
    if (!result || generation !== refreshGeneration) return;
    SEED = result;
    await save(() => {
      if (generation !== refreshGeneration) return;
      mergeJobs(result.jobs);
      state._lastLoaded = Date.now();
    }, { jobsChanged: true });
    if (generation !== refreshGeneration) return;
    renderSourceHealth();
    updateSnapshotLine();
  } catch (error) {
    $('snapshot-line').textContent = `Refresh failed: ${error.message}. Your local jobs remain available.`;
  } finally {
    if (generation === refreshGeneration) $('refresh-btn').removeAttribute('aria-busy');
  }
}
function renderSourceHealth() {
  const health = SEED.health || new Map();
  $('source-health').innerHTML = SOURCES.map(source => {
    const h = health.get(source.name);
    if (!h) return '';
    const delivery = h.delivery === 'error' ? `unavailable (${h.message})` : h.delivery === 'partial' ? `partial data · ${h.skipped} invalid rows skipped` : 'downloaded';
    const scrape = h.scrape && h.scrape !== 'unknown' ? ` · scrape: ${h.scrape}` : '';
    const success = h.last_success_at ? ` · last successful scrape: ${h.last_success_at}` : '';
    return `<li class="health-${h.delivery}"><strong>${escape(source.src)}</strong> · ${escape(delivery + scrape + success)}</li>`;
  }).join('');
  $('retry-failed').hidden = ![...health.values()].some(h => h.delivery === 'error');
}
function updateSnapshotLine() {
  const health = [...(SEED.health || new Map()).values()];
  const failed = health.filter(h => h.delivery === 'error').length;
  const mins = Math.floor((Date.now() - (state._lastLoaded || Date.now())) / 60000);
  $('snapshot-line').textContent = `Bay Area · NYC · US remote · biotech hubs — ${failed ? `${failed} source(s) unavailable · ` : ''}${state._lastLoaded ? `checked ${mins < 1 ? 'just now' : mins + 'm ago'}` : 'local history'} · ${state.jobs.length.toLocaleString()} jobs`;
}
setInterval(() => { if (!document.hidden) updateSnapshotLine(); }, 60000);

// ---------- Events ----------
let undo = null, undoTimer;
function clearUndo() { undo = null; clearTimeout(undoTimer); $('undo-bar').hidden = true; }
function validateUndo() {
  if (!undo) return;
  const urls = [...new Set(undo.urls.flatMap(url => aliases.get(url) || [url]))];
  const current = resolveDecision(state.triage, urls);
  if (!current || current.t !== undo.t || current.s !== 'dismissed') clearUndo();
}
$('jobs').addEventListener('click', async e => {
  const btn = e.target.closest('.act');
  if (!btn) return;
  const url = btn.closest('.job').dataset.url, act = btn.dataset.act;
  if (act === 'opener') { if (SCORES[url]?.outreach_opener) copy(SCORES[url].outreach_opener); return; }
  await save((current, timestamp) => {
    rebuildGroups();
    const urls = aliases.get(url) || [url];
    const previous = resolveDecision(current.triage, urls)?.s || null;
    const status = previous === act ? null : act;
    const t = timestamp(urls);
    for (const alias of urls) current.triage[alias] = decide(status, t);
    clearUndo();
    if (status === 'dismissed') {
      undo = { urls: [...urls], previous, t, expires: Date.now() + 5000 };
      $('undo-bar').hidden = false;
      undoTimer = setTimeout(clearUndo, 5000);
    }
  });
});
$('undo-btn').onclick = async () => {
  if (!undo) return;
  const captured = undo;
  await save((current, timestamp) => {
    rebuildGroups();
    const urls = [...new Set(captured.urls.flatMap(url => aliases.get(url) || [url]))];
    const latest = resolveDecision(current.triage, urls);
    if (Date.now() <= captured.expires && latest?.t === captured.t && latest.s === 'dismissed') {
      const t = timestamp(urls);
      for (const url of urls) current.triage[url] = decide(captured.previous, t);
    }
    clearUndo();
  });
  $('results-heading').focus();
};

// Browse / Rank view toggle
let browseStates = null;
function setView(mode) {
  if (mode === 'rank' && !ENABLE_SCORING) mode = 'browse';
  if (mode === 'rank' && viewMode !== 'rank') {
    browseStates = new Set(filters.state);
    filters.state = new Set(['active', 'saved']);
  } else if (viewMode === 'rank' && mode !== 'rank' && browseStates) {
    filters.state = browseStates;
    browseStates = null;
  }
  viewMode = mode;
  ['browse', 'rank', 'map'].forEach(m => {
    const b = $('view-' + m);
    if (b) { b.classList.toggle('on', mode === m); b.setAttribute('aria-pressed', String(mode === m)); }
  });
  renderAll();
}
$('view-browse').onclick = () => setView('browse');
$('view-rank').onclick = () => setView('rank');
$('view-map').onclick = () => setView('map');

// Salary floor slider + "include unlisted" toggle + clickable histogram.
$('sal-min').addEventListener('input', e => {
  filters.salMin = Number(e.target.value) || 0;
  renderSalaryControl(); renderSalaryChart(); renderJobs();
});
$('sal-min').addEventListener('change', renderAll);
$('sal-unknown').addEventListener('change', e => { filters.salUnknown = e.target.checked; renderAll(); });
$('chart-salary').addEventListener('keydown', e => {
  if ((e.key === 'Enter' || e.key === ' ') && e.target.closest('.hbar')) {
    e.preventDefault(); e.target.closest('.hbar').click();
  }
});
$('chart-salary').addEventListener('click', e => {
  const bar = e.target.closest('.hbar');
  if (!bar) return;
  const floor = Number(bar.dataset.floor) || 0;
  filters.salMin = (filters.salMin === floor) ? 0 : floor;   // click active bar = clear
  $('sal-min').value = filters.salMin;
  renderAll();
});

let searchTimer;
$('search').addEventListener('input', e => {
  clearTimeout(searchTimer);
  const value = e.target.value;
  searchTimer = setTimeout(() => { filters.q = value; renderAll(); }, 150);
});
$('load-more').onclick = () => { visibleLimit += 50; renderJobs(); };
$('retry-failed').onclick = () => refresh({ failedOnly: true });

$('clear-filters').onclick = () => {
  clearTimeout(searchTimer);
  filters.source.clear(); filters.role.clear(); filters.sen.clear();
  filters.state = new Set(['active', 'saved', 'applied']);
  filters.days = null;
  filters.salMin = 0; filters.salUnknown = false;
  $('sal-min').value = 0; $('sal-unknown').checked = false;
  filters.q = ''; $('search').value = '';
  renderAll();
};

// Toast
function toast(msg) {
  const t = $('toast'); t.textContent = msg; t.classList.add('show');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.remove('show'), 1800);
}

// ---------- Modal (paste fresh JSON — offline escape hatch) ----------
let _modalPrevFocus = null;
function openModal() {
  _modalPrevFocus = document.activeElement;
  $('modal-bg').classList.add('show');
  setTimeout(() => $('paste-area').focus(), 0);
}
function closeModal() {
  $('modal-bg').classList.remove('show');
  if (_modalPrevFocus && _modalPrevFocus.focus) _modalPrevFocus.focus();
}
$('paste-btn').onclick = openModal;
$('modal-cancel').onclick = closeModal;
$('modal-close-x').onclick = closeModal;
$('modal-bg').addEventListener('click', e => {
  if (e.target === $('modal-bg')) closeModal();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Tab' && $('modal-bg').classList.contains('show')) {
    const controls = [...$('modal-bg').querySelectorAll('button, textarea')];
    const first = controls[0], last = controls.at(-1);
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
  if (e.key === 'Escape' && $('modal-bg').classList.contains('show')) closeModal();
});
$('modal-merge').onclick = async () => {
  try {
    const txt = $('paste-area').value.trim();
    if (!txt) return;
    const obj = JSON.parse(txt);
    const data = validateFeed(Array.isArray(obj) ? { jobs: obj } : obj);
    let added = 0;
    const ok = await save(() => { added = mergeJobs(data.jobs); }, { jobsChanged: true });
    closeModal();
    $('paste-area').value = '';
    toast(ok ? `Merged ${added} new job${added === 1 ? '' : 's'}` : 'Merged in memory — Export before closing');
  } catch (err) {
    toast('Invalid JSON: ' + err.message);
  }
};

// ---------- Clipboard (used by the ✉ Opener button) ----------
async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast('Copied to clipboard'); }
  catch { toast('Copy failed — select text manually'); }
}

// ---------- Export / Import ----------
// Decisions only, never the job cache — the cache is disposable public data
// and would bloat the file. This is the offline backup, the way to move
// decisions between devices by hand, and the recovery path if a migration
// ever goes wrong. Browser storage can be cleared; Export is a separate copy.
function exportDecisions() {
  const payload = store.snapshot();
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = `job-triage-${localToday()}.json`;   // local day, not UTC
  a.click();
  URL.revokeObjectURL(url);
  toast(`Exported ${Object.keys(payload.triage || {}).length} decision(s)`);
}

function importDecisions(file) {
  const rd = new FileReader();
  rd.onload = async () => {
    try {
      const data = JSON.parse(rd.result);
      if (!data || typeof data.triage !== 'object' || Array.isArray(data.triage) || !data.triage) throw new Error('No decisions in that file');
      if (data.jobs != null && !Array.isArray(data.jobs)) throw new Error('Invalid jobs array');
      const ok = await save(current => {
        current.triage = mergeTriage(current.triage, normalizeTriage(data.triage));
      }, { incoming: data, jobsChanged: true });
      toast(ok ? 'Backup merged' : 'Backup merged in memory — Export before closing');
    } catch (error) { toast('Import failed — ' + error.message); }
  };
  rd.onerror = () => toast('Import failed — could not read that file');
  rd.readAsText(file);
}

$('export-btn').onclick = exportDecisions;
$('import-btn').onclick = () => $('import-file').click();
$('import-file').addEventListener('change', e => {
  const f = e.target.files && e.target.files[0];
  if (f) importDecisions(f);
  e.target.value = '';   // let the same file be re-picked
});

// ---------- Refresh button + tab-focus auto-refresh ----------
$('refresh-btn').onclick = () => refresh();
document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;

  if (Date.now() - (state._lastLoaded || 0) > 5 * 60 * 1000) refresh();
});

// Another window changed its decisions — merge them in and re-render, so two
// open windows converge instead of drifting apart and fighting over the key.
window.addEventListener('storage', e => {
  if (e.key !== DECIDE_KEY || !e.newValue) return;
  try { save(undefined, { incoming: JSON.parse(e.newValue) }); } catch { /* malformed external record */ }
});

// ---------- Self-test (?selftest=1) ----------
// These cover the merge rules everything else trusts. A bad merge fails
// silently — which is this bug's entire failure mode — so this suite is the
// regression net, not polish. Pure functions only: no network, no writes.
function runSelfTest() {
  const results = [];
  const ok = (name, cond) => results.push({ name, pass: !!cond });
  const T = TOMB_MS, now = Date.now();

  // The merge rule: newer timestamp wins, symmetrically.
  ok('newer t wins over older',
    mergeTriage({ a: decide('saved', 100) }, { a: decide('dismissed', 200) }).a.s === 'dismissed');
  ok('older t cannot erase newer',
    mergeTriage({ a: decide('dismissed', 200) }, { a: decide('saved', 100) }).a.s === 'dismissed');
  ok('a decision absent locally is adopted',
    mergeTriage({}, { a: decide('saved', 50) }).a.s === 'saved');
  ok('a stale window with NO decision cannot erase one (the reported bug)',
    mergeTriage({ a: decide('dismissed', 200) }, {}).a.s === 'dismissed');

  // Tombstones: un-toggling must not be undone by a stale copy.
  ok('newer tombstone clears an older decision',
    mergeTriage({ a: decide('dismissed', 100) }, { a: decide(null, 200) }).a.s === null);
  ok('older tombstone cannot block a newer decision',
    mergeTriage({ a: decide(null, 100) }, { a: decide('dismissed', 200) }).a.s === 'dismissed');
  ok('gc drops tombstones past the window',
    Object.keys(gcDecisions({ a: decide(null, now - T - 1) })).length === 0);
  ok('gc keeps fresh tombstones',
    Object.keys(gcDecisions({ a: decide(null, now) })).length === 1);
  ok('gc never drops a live decision, however old',
    Object.keys(gcDecisions({ a: decide('saved', 0) })).length === 1);

  // v1 → v2 coercion.
  const norm = normalizeTriage({ x: 'dismissed', y: { s: 'saved', t: 42 } });
  ok('v1 string coerces to {s,t} with t:0', norm.x.s === 'dismissed' && norm.x.t === 0);
  ok('v2 object survives normalize intact', norm.y.s === 'saved' && norm.y.t === 42);
  ok('a migrated decision loses to any later one',
    mergeTriage(norm, { x: decide('saved', 1) }).x.s === 'saved');

  // tri(): the accessor guarding the tombstone-truthiness trap.
  const keep = state.triage;
  state.triage = { live: decide('dismissed', 1), tomb: decide(null, 1) };
  ok('tri() returns the status string', tri('live') === 'dismissed');
  ok('tri() returns null for a tombstone (NOT a truthy object)', tri('tomb') === null);
  ok('tri() returns null for an unknown url', tri('nope') === null);
  state.triage = keep;

  const pass = results.filter(r => r.pass).length, fail = results.length - pass;
  results.forEach(r => console[r.pass ? 'info' : 'error'](`${r.pass ? 'PASS' : 'FAIL'}  ${r.name}`));
  console.info(`[selftest] ${pass}/${results.length} passed`);
  const el = $('jobs');
  if (el) {
    el.innerHTML = '<pre style="padding:16px;font-size:13px;line-height:1.8;white-space:pre-wrap">' +
      results.map(r => `${r.pass ? '✓' : '✗'}  ${escape(r.name)}`).join('\n') +
      `\n\n${fail ? '✗ ' + fail + ' FAILED' : '✓ all ' + pass + ' passed'}</pre>`;
  }
  $('snapshot-line').textContent = fail ? `self-test: ${fail} FAILED` : `self-test: all ${pass} passed`;
  return fail === 0;
}

async function start() {
  prepareJobs();
  if (new URLSearchParams(location.search).get('view') === 'rank') setView('rank');
  else renderAll();
  await save(undefined, { jobsChanged: true });
  refresh();
}

// ---------- Initial load ----------
if (new URLSearchParams(location.search).has('selftest')) runSelfTest();
else { start(); }
