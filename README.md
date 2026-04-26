# 🧬 Bay Area MLE / DS Job Scraper

Two GitHub Actions workflows that scrape **Machine Learning Engineer, Data Science, and related AI/ML roles** in the SF Bay Area and email the results as an HTML digest.

## What It Does

### 1. Curated Bay Area biotech sweep — daily at 6pm PT
Probes a hand-picked list of Bay Area biotech career boards directly:

- **Greenhouse** — 10x Genomics, Twist Bioscience, Maze Therapeutics, Freenome, Cytokinetics, Natera, Inceptive, Atomwise, Profluent, Eikon Therapeutics, Altos Labs, Arc Institute, Caribou Biosciences, Octant Bio
- **Workday** — Gilead Sciences (multi-term search across the Workday CXS API)
- **Phenom** — Genentech (HTML + JSON-LD parse of the careers page)

Results are filtered to Bay Area locations and reliable posting dates from the last 24 hours only. Output goes to `jobs.json`, `jobs.md`, and `jobs.html`, then auto-committed when changed. The workflow skips the email when there are no fresh biotech roles.

### 2. LinkedIn last-hour watcher — every 3 hours through 8pm PT
Hits LinkedIn's public guest endpoint for SF Bay Area roles posted in **the last hour** across multiple search terms, dedupes by job ID, and sorts by recency. Output goes to `linkedin_jobs.json`, `linkedin_jobs.md`, and `linkedin_jobs.html`.

Runs at **8am, 11am, 2pm, 5pm, and 8pm Pacific time**. The workflow uses a Pacific-time gate so the cadence stays correct across PDT/PST daylight saving changes.

> ⚠️ Uses the unauthenticated public guest endpoint only — **never** signs in with a user account and does not use LinkedIn cookies, tokens, or credentials.

## Keywords Matched

A title is included if it contains any of:

`machine learning engineer`, `ml engineer`, `mle`, `machine learning infra`, `ai engineer`, `mlops`, `research engineer`, `applied scientist`, `ai scientist`, `ml scientist`, `data scientist`, `data science`, `computational scientist`, `computational biologist`, `bioinformatics scientist`, `bioinformatics engineer`, `cheminformatics`

## Output Files

| File | Source | Description |
|---|---|---|
| `jobs.json` / `.md` / `.html` | Curated biotech sweep | Bay-Area-filtered MLE/DS roles posted in the last 24 hours |
| `linkedin_jobs.json` / `.md` / `.html` | LinkedIn watcher | Roles posted in the last hour |
| `checked_companies.json` | (legacy) | Tracking file from earlier Wikipedia-based discovery |

The `.html` files are styled email-ready digests; the `.md` files render nicely on GitHub.

Both workflows keep a GitHub history of generated digests: result files are committed when changed, and each scheduled workflow still runs `git push`.

## Setup

### Gmail secrets (for email delivery)

In **Settings → Secrets and variables → Actions**:

| Secret | Value |
|---|---|
| `GMAIL_USER` | Gmail address |
| `GMAIL_APP_PASSWORD` | [Gmail App Password](https://myaccount.google.com/apppasswords) |

Both workflows email `GMAIL_USER` from `GMAIL_USER` via `smtp.gmail.com:465`.

### Run manually

From the **Actions** tab:
- *Biotech MLE Job Scraper* → Run workflow (full sweep)
- *LinkedIn MLE/DS Watcher* → Run workflow (last hour only)

Or locally:
```bash
python scrape_jobs.py                  # full curated sweep
python scrape_jobs.py --linkedin-only  # LinkedIn last-hour only
```

No third-party Python deps — uses only the standard library.

## Repo Structure

```
├── scrape_jobs.py                  # All scraping logic
├── jobs.{json,md,html}             # Curated biotech sweep output
├── linkedin_jobs.{json,md,html}    # LinkedIn last-hour output
├── checked_companies.json          # Legacy tracking file
├── deep-dive/                      # Notes / analysis
└── .github/workflows/
    ├── scrape_jobs.yml             # Daily 6pm PT — fresh curated sweep
    └── linkedin_watch.yml          # 8am / 11am / 2pm / 5pm / 8pm PT — LinkedIn
```

## ATS Endpoints Used

| ATS | Endpoint |
|---|---|
| Greenhouse | `https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true` |
| Workday | `https://{tenant}.wd1.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` (POST) |
| Phenom (Genentech) | `https://careers.gene.com/us/en/search-results` (HTML + JSON-LD) |
| LinkedIn | `https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search` (public guest) |
