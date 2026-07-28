# 🧬 Bay Area + NYC MLE / DS Job Scraper

Three GitHub Actions workflows that scrape **software engineering, ML/AI, data science, data engineering, platform/infra/security, and biotech informatics roles** in the SF Bay Area and New York City metro, commit the results to the repo, and surface them in the [`triage.html`](#interactive-triage-dashboard--triagehtml) dashboard.

## What It Does

### 1. Biotech LinkedIn digest — daily at 8pm PT, last 24h
Hits LinkedIn's public guest endpoint for SF Bay Area + NYC MLE/DS roles posted in the last 24 hours, then post-filters results to a **biotech company allowlist** derived from `CURATED_BIOTECHS` in `scrape_jobs.py` (10x Genomics, Twist, Maze, Freenome, Cytokinetics, Natera, Inceptive, Atomwise, Profluent, Eikon, Altos Labs, Arc Institute, Caribou, Octant, Genentech, Gilead). Add to that list to expand coverage.

Output goes to `jobs.json`, `jobs.md`, and `jobs.html`. Each run dedupes against the previously-committed `jobs.json` so the output surfaces only postings new since the last run.

> Why allowlist instead of LinkedIn's industry filter? The `f_I` industry parameter is silently ignored on the public guest endpoint (verified by probing IDs 12, 14, 16, 1763, 1862 — all returned identical non-biotech results).

### 2. LinkedIn MLE/DS watcher — hourly, last 1h
Hits LinkedIn's public guest endpoint for SF Bay Area + NYC roles posted in **the last hour** across multiple search terms, dedupes by job ID, and sorts by recency. Output goes to `linkedin_jobs.json`, `linkedin_jobs.md`, and `linkedin_jobs.html`.

Runs hourly at :17 PT (8am–8pm), driven externally by cron-job.org with the in-GH watchdog as backup. A block guard preserves the previous results when LinkedIn returns zero cards across every term (rate-limited run), so the dedupe baseline and dashboard column survive. Each run dedupes against the previous run so empty windows produce no new listings.

> ⚠️ Uses the unauthenticated public guest endpoint only — **never** signs in with a user account and does not use LinkedIn cookies, tokens, or credentials.

### 3. Indeed MLE/DS watcher — every 1h, last 24h
Uses [`python-jobspy`](https://pypi.org/project/python-jobspy/) (Indeed's public RSS and Publisher API were both deprecated in 2026; the site sits behind Cloudflare's top-tier bot product, so stdlib `urllib` is blocked at the edge). JobSpy uses Indeed's mobile-app API internally — no proxies required, no documented rate limit. Output goes to `indeed_jobs.json`, `indeed_jobs.md`, and `indeed_jobs.html`, deduped against the previous run.

Scheduled externally by cron-job.org at :47 PT, offset from the LinkedIn :17 slot to reduce contention on the shared commit-push concurrency group.

## Keywords Matched

A title is included if it contains any of (case-insensitive substring match):

**ML / AI:** `machine learning engineer`, `ml engineer`, `mle`, `machine learning infra`, `ml platform`, `ai platform`, `ai engineer`, `ai/ml engineer`, `mlops`, `research engineer`, `llm engineer`, `generative ai`, `genai engineer`, `prompt engineer`, `deep learning`, `reinforcement learning`, `computer vision`, `nlp engineer`

**Applied / scientist:** `applied scientist`, `ai scientist`, `ml scientist`, `data scientist`, `data science`

**Software engineering:** `software engineer`, `software developer`, `backend engineer`, `back-end engineer`, `backend developer`, `frontend engineer`, `front-end engineer`, `frontend developer`, `full stack engineer`, `full-stack engineer`, `fullstack engineer`, `mobile engineer`, `ios engineer`, `android engineer`

**Platform / infra / ops:** `platform engineer`, `infrastructure engineer`, `infra engineer`, `systems engineer`, `distributed systems`, `cloud engineer`, `devops engineer`, `devops`, `site reliability engineer`, `security engineer`

**Data engineering:** `data engineer`, `data engineering`, `analytics engineer`, `data platform`, `data infrastructure`, `etl engineer`, `etl developer`

**Robotics / perception:** `robotics engineer`, `perception engineer`

**Computational / informatics (biotech):** `computational scientist`, `computational biologist`, `bioinformatics scientist`, `bioinformatics engineer`, `cheminformatics`, `biostatistician`, `bioinformatician`, `bioinformatics analyst`, `genomics scientist`, `research software engineer`, `scientific software engineer`, `associate computational biologist`, `research associate, computational`, `research scientist, ai`

**Excluded seniority:** titles containing `staff`, `principal`, `distinguished`, `founding`, `director`, `vice president`, `vp`/`svp`, `chief`, or `head of` are dropped everywhere (mid-level IC focus). Single-word keywords are word-bounded, so `mle` can't match inside another word.

**Excluded companies:** `EXCLUDED_COMPANIES` in `scrape_jobs.py` is a blocklist for recruiting-platform/aggregator accounts that repost roles which mostly don't exist (e.g. "Jack & Jill"). Matched case-insensitively against the parsed company name in the LinkedIn parser, the Indeed scraper, at the top of `save_jobs_output()` (so every source — including blocked-run fallbacks and future scrapers — is filtered before any digest is written), and as a backstop before anything enters `all_jobs.json`. Add a line there to block the next one.

## Output Files

| File | Source | Description |
|---|---|---|
| `jobs.json` / `.md` / `.html` | Biotech LinkedIn digest | Allowlisted biotech-company roles in the last 24h, deduped against the previous run |
| `linkedin_jobs.json` / `.md` / `.html` | LinkedIn watcher | Roles posted in the last 2h, deduped against the previous run |
| `indeed_jobs.json` / `.md` / `.html` | Indeed watcher | Indeed-sourced roles posted in the last 24h, deduped against the previous run |
| `checked_companies.json` | (legacy) | Tracking file from earlier Wikipedia-based discovery |

The `.html` files are styled standalone digests; the `.md` files render nicely on GitHub. (Both are committed for history/browsing; the `triage.html` dashboard reads the `.json` files directly.)

Both workflows keep a GitHub history of generated digests: result files are committed when changed, and each scheduled workflow still runs `git push`.

### Interactive triage dashboard — `triage.html`

A single-file dashboard hosted on GitHub Pages that merges all the latest source JSONs into one filterable cockpit: search, role/seniority/source filters, save/applied/dismiss buttons persisted in localStorage, top-companies + role-mix charts, and Export/Import buttons for backing up your triage decisions to a file.

Triage state lives in two localStorage keys, deliberately kept apart: `jobTriage:v2` holds your decisions (small, merged on every write, never dropped) and `jobTriage:cache:v1` holds a capped copy of the job list (bulky, disposable, so a quota failure there can't cost you a decision). Every write merges against what's already stored — newest timestamp per job wins — so a second browser window refreshing in the background can no longer overwrite decisions it never saw. Open `triage.html?selftest=1` to run the merge-rule assertion suite.

#### Cross-device sync — on by default

**Your triage decisions leave your browser.** The ⇅ Sync button mirrors them to a small endpoint (`sync/`, deployed on Vercel, backed by Upstash Redis) so a phone and a laptop can share them. This is **on by default**; the dot on the button shows the current state and **Turn sync off** stops it completely, at which point nothing is uploaded and everything still works.

What's stored: the decision (`saved` / `applied` / `dismissed`) for each job URL, plus the title/company of the jobs you triaged — that's what lets a saved role display on a phone that never fetched it. So the roles you applied to are held on a third-party server. Nothing else is: the bulky job cache never syncs, and there is no account, email, or profile data involved.

How it identifies you: your browser generates a random 26-character code (~130 bits) and keeps it locally. The server only ever receives `SHA-256(code)`, sent as a request header — so it cannot learn your code, and the code never appears in a URL or a server log. To add a device, hit Sync → **Copy link** and open that link there. Anyone with the link can read and change your decisions, so treat it like a password. Pasting a code **merges** both devices' decisions; it never replaces either side.

Losing the code means losing the bucket — the server only knows its hash, by design. Use **Export** for a backup file.

Dismissals older than 30 days are garbage-collected (safe: `all_jobs.json` prunes at 14 days, so such a job can't reappear). **Saved and applied are kept forever.**

The merge rule exists twice — inline in `triage.html` for the browser and in `sync/merge.js` for the server — because the dashboard is a single file with no build step. `sync/merge.test.mjs` extracts the browser's copy and asserts the two agree; CI fails on any drift.

**View it:** [`https://ernestod1998.github.io/Job_Scraper/triage.html`](https://ernestod1998.github.io/Job_Scraper/triage.html)

The dashboard fetches `jobs.json` / `linkedin_jobs.json` / `indeed_jobs.json` from the same repo at view time, so it always reflects the latest committed scrape. Refresh in the browser to see new data after a cron fire (Pages serves with ~1–2 min lag after each push). No bake-on-cron step in the scraper — `triage.html` is committed once and never modified by automation.

To run locally (e.g. to edit the dashboard UI):
```bash
python3 -m http.server 8000
# then visit http://localhost:8000/triage.html
```
Opening from `file://` won't work — the dashboard needs same-origin HTTP to `fetch()` the source JSONs.

## Extra sources & features

Beyond the three core watchers, these run-on-demand sources and dashboard
features are available (all reuse the existing `KEYWORDS` / `is_mle_role` gate, so
they follow whatever roles you already target):

**More job sources** (each writes `{basename}.{json,md,html}` and feeds the dashboard):

| Flag | Source | Notes |
|---|---|---|
| `--usajobs-only` | [usajobs.gov](https://www.usajobs.gov) | Federal jobs **with salary**, no API key (public search endpoint). Nationwide. |
| `--governmentjobs-only` | [governmentjobs.com](https://www.governmentjobs.com) (NEOGOV) | State & local government; filtered to Bay Area + NYC via `is_watch_location()`. |
| `--calopps-only` | [calopps.org](https://www.calopps.org) | California local agencies (cities/counties/special districts). |
| `--calcareers-only` | [calcareers.ca.gov](https://calcareers.ca.gov) | California state civil service (ASP.NET postback). |
| `--boards-only` | ZipRecruiter + Google Jobs | Via `python-jobspy` (same library as Indeed); runs twice daily via `boards_watch.yml`. |

Heavier per-term sources share `GOV_SEARCH_TERMS` (a slice of `LINKEDIN_SEARCH_TERMS`); widen it to taste. Each new source has a matching workflow (`usajobs_watch.yml`, `localgov_watch.yml`, `calcareers_watch.yml`).

**Salary backfill:** the LinkedIn watcher now backfills pay from each posting's public guest page (search cards omit it). The dashboard harmonizes every format (hourly / monthly / yearly / `$k` ranges / title-embedded) to an annual figure.

**Dashboard additions:**
- **🗺 Map view** — Leaflet map of roles by city (client-side geocoding, no API key), auto-fitting to wherever the jobs are.
- **Salary distribution** chart + a salary-floor slider (filters by minimum annual pay; excludes unlisted-salary roles by default).
- **Cross-source de-duplication** — the same role cross-posted to multiple boards collapses into one card (matched on title + location + compatible company), showing all source badges; triage applies to every copy.
- **Explicit source** shown on each card (`🔗 LinkedIn`, etc.).
- `mergeJobs` now refreshes cached job fields (e.g. a later-added salary) instead of only adding new jobs.

**📲 Pushover notifications** (`notify.py`) — get a phone push for each new role.
No-op unless `PUSHOVER_TOKEN` + `PUSHOVER_USER` are set as Actions secrets; dedupes
via `notified.json`. Optional `NOTIFY_TERMS` variable filters by title words. Test
from the **Test Pushover Notification** workflow or `python notify.py --test`.

## Setup

### Triage secrets (for the nightly fit-scoring agent)

The scraper workflows need no secrets. The nightly triage workflow (`triage.yml`) reads
these from **Settings → Secrets and variables → Actions**:

| Secret | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key used by `triage_agent.py` |
| `CANDIDATE_PROFILE` | Candidate profile text (kept out of the public repo) |
| `CANDIDATE_RESUME` | Resume text (kept out of the public repo) |

The agent reads the actual job description wherever a source allows it: direct page fetch for Greenhouse/Workday/Phenom/Lever/Ashby, LinkedIn via the public guest posting endpoint, and Indeed via the JD text the scraper saves into `indeed_jobs.json`. Each verdict's `jd` field records whether the description was read (`read`) or the role was judged from metadata alone (`metadata-only`). Verdicts scored metadata-only before JD wiring landed (June 2026) are kept as-is; re-scoring them all would cost roughly $2 in Haiku calls if ever wanted. Published verdict fields (`why`, `flags`, `seniority_fit`, `outreach_opener`) are written to describe the role and general fit only — never the candidate's name, employers, or resume specifics (enforced by an eval case whose forbidden tokens are derived at runtime from the secret profile).

### Run manually

From the **Actions** tab:
- *Biotech MLE Job Scraper* → Run workflow (biotech LinkedIn, last 24h)
- *LinkedIn MLE/DS Watcher* → Run workflow (general LinkedIn, last 2h)
- *Indeed MLE/DS Watcher* → Run workflow (Indeed via python-jobspy, last 24h)

Or locally:
```bash
python scrape_jobs.py --biotech-only   # biotech LinkedIn, last 24h, allowlist-filtered
python scrape_jobs.py --linkedin-only  # general MLE/DS LinkedIn, last 2h
python scrape_jobs.py --indeed-only    # general MLE/DS Indeed, last 24h (requires python-jobspy)
python scrape_jobs.py                  # legacy curated Greenhouse/Workday/Phenom sweep
```

Biotech and LinkedIn pipelines use only the standard library. The Indeed pipeline requires `pip install -r requirements.txt` (single dep: `python-jobspy`).

## Repo Structure

```
├── scrape_jobs.py                  # All scraping logic
├── discover.py                     # Find startups from accelerator/VC portfolios + resolve their ATS
├── triage_agent.py                 # Nightly fit-scoring agent (Claude API / claude CLI)
├── eval_triage.py                  # Golden-case evals for the triage agent
├── requirements.txt                # python-jobspy (Indeed only; LinkedIn/biotech are stdlib)
├── jobs.{json,md,html}             # Curated biotech sweep output (last 24h)
├── linkedin_jobs.{json,md,html}    # LinkedIn watcher output (last 1h)
├── indeed_jobs.{json,md,html}      # Indeed watcher output (last 24h, includes JD text)
├── all_jobs.json                   # Cumulative 14-day master (feeds triage + Rank tab)
├── scores.json                     # Triage agent verdicts, keyed by job URL
├── workflow_runs.jsonl             # Per-run job counts (scheduler observability)
├── triage.html                     # Interactive dashboard (fetches the JSONs at view time)
├── checked_companies.json          # Legacy tracking file
├── deep-dive/                      # Notes / analysis
└── .github/workflows/
    ├── scrape_jobs.yml             # Daily 8pm PT — biotech (direct ATS + LinkedIn allowlist)
    ├── linkedin_watch.yml          # Hourly :17 PT — general LinkedIn (last 1h, cron-job.org-driven)
    ├── indeed_watch.yml            # Hourly :47 PT — Indeed (last 24h, cron-job.org-driven)
    ├── linkedin_watch_backup.yml   # In-GH watchdog at :33 PT — re-dispatches missed runs
    ├── triage.yml                  # Nightly 09:00 UTC — scores new roles vs candidate profile
    └── evals.yml                   # On push to scoring files — golden-case evals (must pass)
```

## ATS Endpoints Used

| ATS | Endpoint |
|---|---|
| Greenhouse | `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` |
| Workday | `https://{tenant}.wd1.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` (POST) |
| Ashby | `https://api.ashbyhq.com/posting-api/job-board/{slug}` |
| Lever | `https://api.lever.co/v0/postings/{slug}?mode=json` |
| Phenom (Genentech) | `https://careers.gene.com/us/en/search-results` (HTML + JSON-LD) |
| Custom (own site) | `careers_url` fetched directly; nav/footer stripped, job titles extracted heuristically (best-effort, no JS) |
| LinkedIn | `https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search` (public guest) |
| Indeed | `python-jobspy` library (mobile-app API; no public endpoint since 2026 deprecation) |

## Startup discovery & own-site monitoring

The curated biotech sweep (`--biotech-only`) checks each company in `CURATED_BIOTECHS`.
Beyond the established biotechs, it now also watches small techbio **startups** — including
ones that post only on their **own website** rather than a standard ATS.

**`discover.py` — find startups automatically, no homepage entry:**

```bash
python discover.py --portfolios --write      # pull SOSV/IndieBio portfolio via its API, resolve, auto-add
python discover.py --portfolios --limit 0    # resolve the whole list, print (no write)
python discover.py https://www.some-startup.bio   # resolve one homepage
python discover.py --file homepages.txt      # resolve a list (one homepage URL per line)
```

`--portfolios` pulls the SOSV/IndieBio portfolio (companies tagged *Human Health* / *Therapeutics*)
straight from SOSV's public WordPress REST API — names **and** homepages, no manual entry — plus a
small built-in list of ML-native drug-discovery shops. For each, it finds the careers page and
detects the ATS (Greenhouse/Lever/Ashby by URL pattern, including a Greenhouse `?for=` embed or a
board one hop into the careers page). `--limit N` caps how many to resolve (default 60; 0 = all).

`--write` appends resolved companies to **`discovered_companies.json`**, which
`scrape_curated_biotechs()` loads and merges automatically — so the daily sweep picks them up with
**no copy/paste** into `CURATED_BIOTECHS`. Already-tracked companies (and Genentech, scraped
separately) are skipped; unresolved ones and non-dispatchable ATSes (Workable/Rippling) print as
`# TODO manual`. Honest limits: the tiniest IndieBio companies often have no careers page (low hit
rate on that tail), and a `custom` careers page that renders its jobs via JavaScript yields nothing.

**`custom` ATS entries** monitor own-site boards: `{"name": .., "ats": "custom", "careers_url": .., "fallback_location": ..}`.
The handler respects `robots.txt` (timeout-guarded), rate-limits via `REQUEST_DELAY`, and extracts
job titles heuristically. **Limitation:** it can't see JavaScript-rendered job lists (stdlib runs no JS),
so those companies yield nothing — a known gap. A `fallback_location` is required, since the Bay-Area
gate drops location-less roles. Keep the curated list to O(dozens): `custom` probes are sequential.
