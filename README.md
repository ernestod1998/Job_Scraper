# 🧬 Biotech MLE Job Scraper

A GitHub Actions workflow that automatically scrapes **Machine Learning Engineer** job listings from biotech company career pages twice a week.

## Companies Tracked

| Company | Source |
|---|---|
| Genentech | careers.gene.com (JSON-LD + Phenom ATS) |
| Recursion Pharmaceuticals | Greenhouse ATS API |

## How It Works

1. **Scheduled trigger** — runs every Monday and Thursday at 9am PT
2. **`scrape_jobs.py`** — fetches job listings using only Python stdlib (no pip installs needed)
3. Results are saved to `jobs.json` and `jobs.md`
4. Changes are auto-committed back to the repo

## Output Files

- **`jobs.json`** — structured data (good for downstream processing)
- **`jobs.md`** — human-readable markdown (renders nicely on GitHub)

## Run Manually

Go to **Actions → Biotech MLE Job Scraper → Run workflow**

Or locally:
```bash
python scrape_jobs.py
```

## Add More Companies

In `scrape_jobs.py`, add a new function following the same pattern:

```python
def scrape_mycompany():
    # Option A: hit a Greenhouse/Lever/Ashby ATS JSON API
    # Option B: fetch HTML and regex/parse job titles
    ...
```

Then call it in `__main__`:
```python
all_jobs.extend(scrape_mycompany())
```

### Common ATS API patterns

| ATS | API URL pattern |
|---|---|
| Greenhouse | `https://boards-api.greenhouse.io/v1/boards/{company}/jobs` |
| Lever | `https://api.lever.co/v0/postings/{company}?mode=json` |
| Ashby | `https://jobs.ashbyhq.com/api/non-user-graphql` |
| Workday | Usually requires JS rendering — use `workflow_dispatch` + manual check |

## Repo Structure

```
├── scrape_jobs.py               # Scraper script
├── jobs.json                    # Latest results (auto-updated)
├── jobs.md                      # Latest results in Markdown (auto-updated)
└── .github/
    └── workflows/
        └── scrape_jobs.yml      # GitHub Actions workflow
```
