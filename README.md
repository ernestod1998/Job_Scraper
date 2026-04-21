# 🧬 Biotech MLE Job Scraper

A GitHub Actions workflow that automatically scrapes **Machine Learning Engineer and related AI/data roles** from ~337 biotech company career pages — 50 companies per day, cycling through the full list continuously.

## How It Works

1. **Scheduled trigger** — runs daily at 9am PT
2. **Wikipedia discovery** — fetches the current list of US biotech companies dynamically (no hard-coded list)
3. **ATS probing** — tries Greenhouse and Lever public JSON APIs for each company using generated URL slugs
4. **Genentech** — scraped separately via their Phenom ATS careers page
5. **50 companies/day limit** — progress is tracked in `scrape_progress.json` and committed back to the repo; after all companies are checked, it resets and starts over
6. Results saved to `jobs.json` and `jobs.md`, auto-committed, and emailed to you

## Keywords Matched

Roles are included if the job title contains any of:

- `machine learning engineer`
- `ml engineer`
- `mle`
- `machine learning infra`
- `applied scientist`
- `ai engineer`
- `research engineer`
- `data scientist`
- `mlops`

## Output Files

- **`jobs.json`** — structured data with title, company, location, URL, and date posted
- **`jobs.md`** — human-readable markdown (renders nicely on GitHub)
- **`scrape_progress.json`** — tracks current position in the company list across daily runs

## Setup

### 1. Gmail secrets (for email delivery)

Go to **Settings → Secrets and variables → Actions** and add:

| Secret | Value |
|---|---|
| `GMAIL_USER` | Your Gmail address |
| `GMAIL_APP_PASSWORD` | A [Gmail App Password](https://myaccount.google.com/apppasswords) |

### 2. Run manually

Go to **Actions → Biotech MLE Job Scraper → Run workflow**

Or locally:
```bash
python scrape_jobs.py
```

## Repo Structure

```
├── scrape_jobs.py               # All scraping logic
├── jobs.json                    # Latest results (auto-updated)
├── jobs.md                      # Latest results in Markdown (auto-updated)
├── scrape_progress.json         # Daily progress tracker (auto-updated)
└── .github/
    └── workflows/
        └── scrape_jobs.yml      # GitHub Actions workflow
```

## ATS API Patterns

| ATS | API URL |
|---|---|
| Greenhouse | `https://boards-api.greenhouse.io/v1/boards/{company}/jobs` |
| Lever | `https://api.lever.co/v0/postings/{company}?mode=json` |