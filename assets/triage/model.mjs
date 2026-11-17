// ---------- Classifiers ----------
// Forward deployed / solutions lane and the AI-engineer veto (2026-11-16) —
// keep both in sync with FORWARD_DEPLOYED_RE / EXCLUDED_AI_ENGINEER_RE in
// scrape_jobs.py (test_scraper_core compares the patterns).
export const FORWARD_DEPLOYED_RE = /forward[\s-]+deployed|\bfde\b|deployment\s+strategist|\bsolutions?\s+(?:engineer|architect)/i;
export const EXCLUDED_AI_ENGINEER_RE = /(?<!\/)(?<!\/\s)\b(?:generative\s+ai|gen\s?ai|ai|llm|prompt)\s+(?:engineer|developer)\b(?!\s+(?:platform|tools|tooling|experience|productivity)\b)/i;
export function isExcludedAiEngineer(title) {
  const t = String(title || '');
  return EXCLUDED_AI_ENGINEER_RE.test(t) && !FORWARD_DEPLOYED_RE.test(t);
}
export function classifyRole(title) {
  const t = (title || '').toLowerCase();
  // Forward deployed first: "Data Scientist (Forward Deployed)", "Forward
  // Deployed Software Engineer" and solutions engineer/architect titles
  // belong in this lane, not DS/SWE.
  if (FORWARD_DEPLOYED_RE.test(t)) return 'Forward Deployed';
  if (/bioinformatic|computational biolog|genomic|ngs/.test(t)) return 'Bioinformatics';
  // Comp-chem / DMPK / tox / imaging lane (mirrors the scraper's targeted
  // KEYWORDS) — checked before Scientist so "Toxicology Research Scientist"
  // lands here, not in the generic bucket.
  if (/computational chem|computational scientist|cheminformatic|computational tox|predictive tox|toxicolog|\bdmpk\b|\badmet\b|\bqsar\b|\bpbpk\b|\bpkdm\b|drug metabolism|molecular property|computational patholog|imaging scientist|medical imaging/.test(t)) return 'Comp Chem/Sci';
  if (/mlops|ml platform|ml infra|ml infrastructure|fullstack machine learning/.test(t)) return 'MLOps/Platform';
  if (/applied scientist|ml scientist|research scientist|research engineer/.test(t)) return 'Scientist';
  if (/data scientist|analytics/.test(t)) return 'Data Science';
  if (/\bai engineer|ai\/ml engineer|applied ai|genai|llm|agent/.test(t)) return 'AI Engineer';
  if (/machine learning engineer|ml engineer|mle\b/.test(t)) return 'MLE';
  if (/software engineer|swe\b|backend|frontend|full[- ]?stack/.test(t)) return 'SWE';
  return 'Other';
}
// ---------- Location (2026-11-16) ----------
// Feeds are already location-gated by the scraper (is_watch_location /
// is_target_location), so this only labels what survived. Mirrors the city
// sets in scrape_jobs.py: NYC = the five boroughs plus the NJ / Westchester /
// SW-CT commuter ring the scraper admits; Bay Area = BAY_AREA_LOCATIONS, with
// out-of-state namesakes (Newark, Dublin, Richmond, Concord, Brisbane)
// counted only when "CA" confirms them. A multi-city posting gets every metro.
const NYC_LOCATION_RE = /\bnew york\b|\bnyc\b|\bmanhattan\b|\bbrooklyn\b|\bqueens\b|\bbronx\b|\bstaten island\b|\bjersey city\b|\bhoboken\b|\bsecaucus\b|\bweehawken\b|\bnorth bergen\b|\bfort lee\b|\benglewood cliffs\b|\bhackensack\b|\bteaneck\b|\bparamus\b|\brutherford\b|\bmontclair\b|\bclifton\b|\bshort hills\b|\bparsippany\b|\bnewark\b(?=,?\s*(?:nj|new jersey)\b)|\blong island city\b|\byonkers\b|\bwhite plains\b|\btarrytown\b|\bnew rochelle\b|\bport chester\b|\b(?:harrison|purchase|rye)\b(?=,?\s*(?:ny|new york)\b)|\bstamford\b|\bgreenwich\b/i;
const BAY_LOCATION_RE = /\bbay area\b|\bsan francisco\b|\bdaly city\b|\boakland\b|\bberkeley\b|\balameda\b|\bemeryville\b|\bpalo alto\b|\bmountain view\b|\bmenlo park\b|\bsunnyvale\b|\bsanta clara\b|\bsan jose\b|\bcupertino\b|\blos altos\b|\blos gatos\b|\bsan mateo\b|\bfoster city\b|\bredwood city\b|\bsan carlos\b|\bmillbrae\b|\bsan bruno\b|\bburlingame\b|\bbelmont\b|\bfremont\b|\bhayward\b|\bunion city\b|\bmilpitas\b|\bwalnut creek\b|\bpleasanton\b|\bsan ramon\b|\bdanville\b|\blivermore\b|\bnovato\b|\bsan rafael\b|\bmill valley\b|\bsausalito\b|\bvacaville\b|\b(?:newark|dublin|richmond|concord|brisbane)\b(?=,?\s*(?:ca|california)\b)/i;
export const METROS = ['Bay Area', 'NYC', 'Remote', 'Other'];
export function classifyMetros(location) {
  const loc = String(location || '');
  const out = [];
  if (BAY_LOCATION_RE.test(loc)) out.push('Bay Area');
  if (NYC_LOCATION_RE.test(loc)) out.push('NYC');
  if (!out.length && /\bremote\b|work from home|\banywhere\b/i.test(loc)) out.push('Remote');
  return out.length ? out : ['Other'];
}
export function classifySeniority(title) {
  const t = (title || '').toLowerCase();
  if (/founding/.test(t)) return 'Founding';
  if (/principal|distinguished/.test(t)) return 'Principal';
  if (/\bstaff\b/.test(t)) return 'Staff';
  if (/\bsr\.?\b|\bsenior\b|\blead\b/.test(t)) return 'Senior';
  if (/intern\b/.test(t)) return 'Intern';
  return 'Mid';
}
export function jobFeeds(j) {
  const feeds = Array.isArray(j.feeds) ? j.feeds : (j.feed ? [j.feed] : []);
  return feeds.map(f => String(f).trim().toLowerCase()).filter(Boolean);
}
export function classifySource(j) {
  // Feed provenance wins over ATS vendor: an ATS host is infrastructure, not
  // proof that the company belongs to the biotech lane.
  if (jobFeeds(j).includes('biotech')) return 'Biotech';
  const ats = String(j.ats || '').toLowerCase();
  if (ats === 'usajobs' || (j.url || '').includes('usajobs.gov')) return 'USAJOBS';
  if (ats === 'neogov' || (j.url || '').includes('governmentjobs.com')) return 'NEOGOV';
  if (ats === 'calopps' || (j.url || '').includes('calopps.org')) return 'CalOpps';
  if (ats === 'calcareers' || (j.url || '').includes('calcareers.ca.gov')) return 'CalCareers';
  if (ats === 'linkedin') return 'LinkedIn';
  if (['greenhouse', 'lever', 'ashby', 'gem', 'workday'].includes(ats)) return 'Direct ATS';
  if (ats === 'indeed' || (j.url || '').includes('indeed.com')) return 'Indeed';
  return 'Other';
}
// ---------- Salary parsing & harmonization ----------
// Postings report pay inconsistently (Indeed "$150k–$190k/yr" or "$62.50/hr",
// CalCareers "$6,963 - $8,724 per month", LinkedIn titles "($175K – $250K)").
// Normalize everything to an ANNUAL USD {min,max}. Returns null when no pay
// signal is present.
export const PERIOD_MULT = [
  [/hour|hr\b|\/hr|per hour/, 2080],
  [/\bday|\/day|per day|daily/, 260],
  [/week|wk\b|\/wk|per week/, 52],
  [/month|mo\b|\/mo|per month|monthly/, 12],
  [/year|yr\b|\/yr|per year|annual|annually|p\.?a\.?/, 1],
];
export function _annualMult(s) {
  for (const [re, m] of PERIOD_MULT) if (re.test(s)) return m;
  return null;  // unknown period — inferred later from magnitude
}
// Pull dollar amounts out of a string. Tolerates comma OR space thousands
// separators ("$120,000", "$120 000", "$120000") and k-suffixes ("$120k").
export const _MONEY_RE = /\$\s*\d+(?:\.\d+)?\s*[kK]\b|\$?\s*\d{1,3}(?:[,\s]\d{3})+(?:\.\d+)?|\$\s*\d{2,7}(?:\.\d+)?/g;
export function _moneyNums(s) {
  const out = [];
  let m;
  _MONEY_RE.lastIndex = 0;
  while ((m = _MONEY_RE.exec(s)) !== null) {
    const tok = m[0];
    let n = parseFloat(tok.replace(/[\s,$]/g, ''));
    if (/[kK]/.test(tok)) n *= 1000;
    if (isFinite(n) && n >= 10) out.push(n);
  }
  return out;
}
export function _parseOnePay(raw) {
  const s = (raw || '').toLowerCase();
  const nums = _moneyNums(s);
  if (!nums.length) return null;
  let mult = _annualMult(s);
  let lo = Math.min(...nums), hi = Math.max(...nums);
  if (mult == null) {
    if (hi < 200) mult = 2080;        // looks hourly
    else if (hi < 20000) mult = 12;   // looks monthly
    else mult = 1;                     // looks yearly
  }
  lo = Math.round(lo * mult); hi = Math.round(hi * mult);
  if (hi < 15000 || hi > 1000000) return null;   // implausible → reject
  const k = n => '$' + Math.round(n / 1000) + 'k';
  return { min: lo, max: hi, disp: (lo === hi ? k(lo) : `${k(lo)}–${k(hi)}`) + '/yr' };
}
export function parseSalary(j) {
  const cands = [];
  if (j.salary) cands.push(j.salary);
  if (j.title) cands.push(j.title);
  if (j.description) {
    // Only parse the slice around a pay cue, so we don't grab random dollar
    // figures (or 401k) from the body text.
    const d = j.description;
    const cue = /(salary|compensation|pay\s*range|pay\s*rate|base\s*pay|hourly\s*rate|\bwage\b|\$\s?\d)/i.exec(d);
    if (cue) cands.push(d.slice(Math.max(0, cue.index - 24), cue.index + 160));
  }
  for (const raw of cands) {
    const got = _parseOnePay(raw);
    if (got) return got;
  }
  return null;
}

// Today's LOCAL calendar day as 'YYYY-MM-DD'. Build it from the getters, NOT
// by slicing an ISO string — that yields the UTC day, which after 5pm Pacific
// is already tomorrow. That exact mistake is what put 2026-07-29 on the cards
// on the evening of the 28th, so test_dates.py fails the build if it returns.
// `now` is a test seam.
export function localToday(now) {
  const d = now ? new Date(now) : new Date();
  const p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}

// What the card's 🕒 shows. The scrapers now stamp Pacific days, but
// all_jobs.json keeps entries for 14 days, so rows written before that fix
// can still carry a future date — and a source we haven't normalized could
// reintroduce one. A date that hasn't happened yet is never right, so clamp
// it to the viewer's today. Relative strings and past dates pass through.
export function displayDate(dateStr, now) {
  const d = dateStr || '';
  if (!/^\d{4}-\d{2}-\d{2}$/.test(d)) return d;
  const today = localToday(now);
  return d > today ? today : d;
}

// Parse a job's posted date to epoch ms. Handles ISO 'YYYY-MM-DD' and the relative
// 'Posted N Days Ago' strings some boards emit. null = unknown (kept under "Any time",
// excluded by an active date window).
export function jobDateMs(j) {
  const d = j.date_posted || '';
  const iso = /^(\d{4})-(\d{2})-(\d{2})/.exec(d);
  if (iso) return Date.parse(`${iso[1]}-${iso[2]}-${iso[3]}T00:00:00`);
  const rel = /(\d+)\s*days?\s*ago/i.exec(d);
  if (rel) return Date.now() - Number(rel[1]) * 86400000;
  if (/today|hour|just posted/i.test(d)) return Date.now();
  return null;
}

// Finest freshness signal for sub-day windows: date_posted is day-resolution
// across every source, but first_seen (stamped by the scraper's all_jobs
// accumulator) is a full timestamp — and the LinkedIn watcher's hourly 1h
// lookback makes "first seen within the hour" ≈ "posted within the hour".
export function jobFreshMs(j) {
  if (j.first_seen) {
    const t = Date.parse(j.first_seen);
    if (!isNaN(t)) return t;
  }
  return jobDateMs(j);
}

// Date ordering for the list. Primary key is date_posted (what the card's 🕒
// shows, so the order matches the visible label); first_seen breaks same-day
// ties with sub-day precision. Undated roles sink to the bottom in BOTH
// directions rather than masquerading as the oldest listings.
export function compareByDate(a, b, dir) {
  const ad = jobDateMs(a), bd = jobDateMs(b);
  if (ad == null || bd == null) return ad == null ? (bd == null ? 0 : 1) : -1;
  if (ad !== bd) return dir === 'oldest' ? ad - bd : bd - ad;
  const af = jobFreshMs(a) ?? ad, bf = jobFreshMs(b) ?? bd;
  return dir === 'oldest' ? af - bf : bf - af;
}

// Senior / Sr. excluded again since 2026-11-16 — keep in sync with
// EXCLUDED_SENIORITY_RE in scrape_jobs.py.
export const EXCLUDED_TITLE_RE = /\b(senior|sr\.?|staff|principal|distinguished|founding|lead|manager|director|vice president|vp|svp|chief|head\s+of)\b/i;
// Security-domain veto (lane retired 2026-09-03) — keep in sync with
// EXCLUDED_SECURITY_RE in scrape_jobs.py.
export const EXCLUDED_SECURITY_RE = /\b(security|secure|securing|cyber\w*|infosec|appsec|devsecops|secops|threat|vulnerability|vulnerabilities|pentest\w*|penetration|red\s+team|incident\s+response|soc|iam|cryptograph\w*|grc)\b/i;
// Internship / student-cohort veto (2026-11-12); junior, early-career and
// new-grad roles are kept — keep in sync with EXCLUDED_INTERNSHIP_RE in
// scrape_jobs.py.
export const EXCLUDED_INTERNSHIP_RE = /\b(intern|interns|internship|internships|co-?op|apprentice\w*|(summer|fall|autumn|winter|spring)\s+20\d\d|20\d\d\s+start|class\s+of\s+20\d\d)\b/i;
// .NET / C# stack veto (2026-11-12) — keep in sync with EXCLUDED_STACK_RE in
// scrape_jobs.py.
export const EXCLUDED_STACK_RE = /(?:(?<!\w)|\b(?:asp|vb))\.net\b|\bdotnet\b|(?<!\w)c#|\bc[\s-]?sharp\b/i;
export function repairBiotechSourceCollision(j) {
  const company = String(j.company || '').normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '').replace(/[^a-z0-9]/gi, '').toLowerCase();
  // These were admitted by the retired bidirectional substring matcher
  // (Meta→Metagenomi/Metabolic, ŌURA/Compass→longer biotech names).
  if (!['meta', 'oura', 'compass'].includes(company)) return j;
  const feeds = jobFeeds(j).filter(f => f !== 'biotech');
  if (feeds.length) j.feeds = feeds;
  else delete j.feeds;
  return j;
}
export function slimJob(j) {
  const o = {};
  for (const k in j) if (k[0] !== '_' && k !== 'description') o[k] = j[k];
  return o;
}
