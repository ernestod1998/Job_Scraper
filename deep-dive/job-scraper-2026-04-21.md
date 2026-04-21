# Deep Dive: Biotech MLE Job Scraper

**Generated**: 2026-04-21  
**Files**: `scrape_jobs.py`, `.github/workflows/scrape_jobs.yml`

---

## Overview

### What This Code Does

A fully automated pipeline that:
1. Fetches ~500 US biotech companies from Wikipedia's category API
2. For each company, generates plausible URL slugs and probes Greenhouse and Lever job board APIs
3. Filters results for Machine Learning Engineer roles by title keyword matching
4. Has a custom scraper for Genentech (which uses a different ATS called Phenom)
5. Saves results as `jobs.json` and `jobs.md`
6. Runs daily via GitHub Actions, commits the results back to the repo, and emails them to you

### Why This Approach Was Chosen

The key design constraint: **no third-party libraries**. The entire scraper uses only Python stdlib (`urllib`, `re`, `json`, `time`, `os`). This means zero `pip install` steps, zero dependency management, and it runs anywhere Python 3 is installed — including GitHub Actions with no setup beyond `python-version: "3.11"`.

Greenhouse and Lever both expose **public JSON APIs** (no authentication needed) for their job boards. This is by design — companies want their listings to be discoverable. Rather than scraping HTML, the code queries these structured APIs directly, which is more reliable and doesn't break when a company redesigns their careers page.

The Wikipedia API is used instead of a hard-coded company list so the scraper stays current as the biotech landscape changes.

### Context

Use this pattern when:
- You need to scrape job boards or APIs without adding dependencies
- You're automating recurring data collection and want the results versioned in git
- You want a "set and forget" pipeline that emails you results daily

---

## Code Walkthrough

### File 1: `scrape_jobs.py`

**Purpose**: All scraping logic — company discovery, ATS probing, filtering, and output.

#### `fetch(url)` — The HTTP primitive (lines 34–41)

```python
def fetch(url):
    req = Request(url, headers=HEADERS)
    try:
        with urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", errors="ignore")
    except URLError as e:
        print(f"  ⚠️  Could not fetch {url}: {e}")
        return ""
```

`urllib.request.Request` lets you attach headers before sending — here it adds a browser-like `User-Agent` string. Without this, many servers return 403 or empty responses because they block the default Python user agent.

`errors="ignore"` in `.decode()` is defensive: some job listings contain non-UTF-8 bytes (e.g., Windows-1252 encoded em-dashes). Instead of crashing, they're silently dropped.

Returning `""` on failure (rather than raising) keeps the outer loop running — one bad company doesn't stop the whole scrape.

#### `get_biotech_companies()` — Wikipedia API (lines 64–93)

```python
url = (
    "https://en.wikipedia.org/w/api.php"
    "?action=query&list=categorymembers"
    "&cmtitle=Category:Biotechnology_companies_of_the_United_States"
    "&cmlimit=500&cmtype=page&format=json"
)
```

The **MediaWiki API** returns structured JSON — no HTML parsing needed. `categorymembers` lists all pages in a Wikipedia category. `cmlimit=500` is the maximum per request (pagination would be needed for larger categories). `cmtype=page` excludes subcategories from the results.

#### `name_to_slugs(name)` — Slug generation (lines 96–119)

This is one of the more clever pieces. ATS platforms (Greenhouse, Lever) identify each employer by a URL "slug" — a short lowercase string. For example, Moderna's Greenhouse board is at `boards.greenhouse.io/modernatx`. There's no public directory mapping company names to their slugs, so the code guesses:

```python
no_sep  = re.sub(r'[^a-z0-9]', '', clean)        # "modernatx"
hyphen  = re.sub(r'[^a-z0-9]+', '-', clean)       # "moderna-tx"
```

Then it tries dropping common biotech suffixes (`pharmaceuticals`, `therapeutics`, etc.) because many companies register their slug under the shorter brand name: `"abbvie-biotherapeutics"` → try `"abbvie"` too.

The result is a set of 2–10 candidate slugs per company. The code tries each until one returns a valid board.

#### `probe_greenhouse` / `probe_lever` — ATS APIs (lines 126–178)

**Greenhouse** public API:
```
https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true
```
Returns a JSON object with a `"jobs"` array. Each job has `title`, `location.name`, `absolute_url`, and `updated_at`.

**Lever** public API:
```
https://api.lever.co/v0/postings/{slug}?mode=json
```
Returns a JSON array directly (no wrapper object). Each posting has `text` (the title), `categories.location`, and `hostedUrl`.

Both APIs are completely public — you can paste either URL in a browser and see real job listings. `REQUEST_DELAY = 0.3` sleeps 300ms between calls to avoid hammering servers.

#### `scrape_genentech()` — Custom Phenom ATS scraper (lines 215–264)

Genentech uses **Phenom**, a different ATS with no public JSON API. The scraper fetches the HTML careers page and extracts structured data two ways:

1. **JSON-LD** (`<script type="application/ld+json">`): Schema.org structured data embedded in the page. Many enterprise sites embed machine-readable job data here for SEO — it's more reliable than parsing visual HTML.

2. **Fallback HTML attributes** (`data-ph-at-job-title-text`): Phenom injects job titles as data attributes. If JSON-LD parsing yields nothing, regex extracts them directly.

This two-layer approach handles both the structured and unstructured cases.

#### `save_results(jobs)` — Dual output (lines 271–297)

Writes both `jobs.json` (structured data for programmatic use) and `jobs.md` (human-readable Markdown for GitHub rendering). The Markdown groups roles by company and formats them as a browsable report.

---

### File 2: `.github/workflows/scrape_jobs.yml`

**Purpose**: Automates daily execution, result storage, and email delivery.

#### Schedule trigger
```yaml
on:
  schedule:
    - cron: "0 17 * * *"   # 9am PT = 17:00 UTC
  workflow_dispatch:         # also allow manual trigger
```

`workflow_dispatch` is essential during development — it lets you trigger the workflow from the GitHub Actions UI without waiting for the cron.

#### Committing results back to the repo
```yaml
git diff --cached --quiet || git commit -m "chore: update job listings [$(date -u '+%Y-%m-%d')]"
```

The `||` is a shell short-circuit: `git diff --cached --quiet` exits 0 (success) if there are no staged changes. If it exits 0, the `git commit` is skipped — so on days when no new jobs are found, the workflow doesn't create an empty commit.

#### Inline Python email script
The email step runs a Python heredoc directly in the YAML:
```yaml
run: |
  python - <<'EOF'
  ...
  EOF
```

`python -` means "read from stdin." The heredoc passes the script body as stdin. This avoids creating a separate email script file while keeping the logic in one place.

Gmail credentials are stored as **GitHub Actions Secrets** (`GMAIL_USER`, `GMAIL_APP_PASSWORD`) and injected as environment variables. The `APP_PASSWORD` is a Gmail app-specific password (not your account password) — required when 2FA is enabled on the account.

---

## Concepts Explained

### Design Patterns Used

| Pattern | Where | Why |
|---------|-------|-----|
| Graceful degradation | `fetch()` returns `""` on error | One failing company shouldn't stop the whole run |
| Strategy pattern (implicit) | `probe_greenhouse` vs `probe_lever` | Same interface, different API shapes |
| Try-multiple-variants | `name_to_slugs` + loop in `scrape_company` | No authoritative mapping exists; brute-force with heuristics |
| Dual output formats | `save_results` writes JSON + Markdown | Machine-readable and human-readable from one pass |

---

### Key Technical Concepts

#### 1. Public ATS APIs (Greenhouse & Lever)

**What**: Applicant Tracking Systems (ATS) are software platforms companies use to manage job postings and applications. Greenhouse and Lever both expose unauthenticated public JSON endpoints for their job boards — this is intentional so third-party aggregators (LinkedIn, Indeed) can index listings.

**Why Used Here**: Querying a JSON API is far more reliable than scraping HTML. The API response structure is stable; the HTML layout of a careers page changes whenever someone updates the CSS.

**When to Use**: Whenever you need job listing data from companies that use Greenhouse or Lever. Check if `boards-api.greenhouse.io/v1/boards/{slug}/jobs` returns data for a company before building any custom scraper.

**Trade-offs**:
- Pros: Structured data, stable format, no HTML parsing
- Cons: Only works for Greenhouse/Lever; other ATS platforms (Workday, Phenom, iCIMS) have no equivalent public API

---

#### 2. URL Slug Inference

**What**: A "slug" is a URL-safe identifier — lowercase letters, numbers, and hyphens only. `"Moderna Therapeutics"` → slug might be `"modernatx"` or `"moderna"`.

**Why Used Here**: There's no public company-name-to-slug directory. The only way to find a company's ATS board is to guess slugs based on their name.

**When to Use**: Any time you're mapping human-readable names to URL identifiers without a lookup table.

**Trade-offs**:
- Pros: Covers most cases automatically
- Cons: Miss rate for companies with unusual slugs (e.g., Pfizer's Greenhouse slug is `"pfizer"` but a spinoff might use `"pfizer-oncology-inc"`)

**Alternatives**:
- Maintain a hand-curated `{name: slug}` mapping — more accurate, more maintenance
- Scrape the ATS platform's own search page — fragile, against ToS for some platforms

---

#### 3. Schema.org JSON-LD

**What**: JSON-LD (JSON Linked Data) is a format for embedding structured, machine-readable data inside HTML `<script>` tags. Schema.org defines vocabularies for common entities — including `JobPosting`. Google uses this data to power "Jobs" search results.

**Why Used Here**: Genentech's Phenom-powered careers page embeds job data as Schema.org `JobPosting` objects. This is more reliable than scraping visual HTML elements that may change.

**When to Use**: Before writing a custom HTML parser for any large company's careers page, check if they embed JSON-LD. Many enterprise ATS platforms do this for SEO.

**Trade-offs**:
- Pros: Stable, machine-readable, standardized
- Cons: Not all sites include it; coverage varies

---

#### 4. GitHub Actions for Scheduled Automation

**What**: GitHub Actions is a CI/CD platform built into GitHub. The `schedule` trigger uses cron syntax to run workflows on a timer, without any external infrastructure.

**Why Used Here**: Replaces the need for a server, cron job, or cloud function. The results are versioned in git automatically — you get a history of job listings over time.

**When to Use**: For recurring data collection, report generation, or any task that runs on a schedule and whose output belongs in a repo.

**Trade-offs**:
- Pros: Free (within limits), no infrastructure, results versioned
- Cons: 6-hour delay possible during high load on GitHub; minimum interval is 5 minutes; not suitable for latency-sensitive work

---

#### 5. smtplib + Gmail App Passwords

**What**: `smtplib` is Python's stdlib SMTP client. An "app password" is a 16-character password Google generates for a specific app — it grants email access without exposing your real password and can be revoked independently.

**Why Used Here**: Delivers results to your inbox without a third-party email service (SendGrid, Mailgun, etc.).

**When to Use**: Simple notification emails from automated scripts. For high-volume or transactional email, use a dedicated service.

**Trade-offs**:
- Pros: No dependencies, full control
- Cons: Gmail has sending limits (~500/day for free accounts); plain SMTP can be blocked by some networks; no delivery tracking

---

## Learning Resources

### Official Documentation

- [MediaWiki API docs](https://www.mediawiki.org/wiki/API:Main_page): Full reference for querying Wikipedia's API — categorymembers, search, and more
- [Greenhouse API docs](https://developers.greenhouse.io/job-board.html): The exact endpoints used in this scraper
- [Lever API docs](https://hire.lever.co/developer/postings): Lever's public postings API reference
- [Schema.org JobPosting](https://schema.org/JobPosting): All fields available in structured job data
- [GitHub Actions cron syntax](https://docs.github.com/en/actions/writing-workflows/choosing-when-your-workflow-runs/events-that-trigger-workflows#schedule): How to schedule workflows

### Tutorials & Articles

- [Python urllib tutorial (Real Python)](https://realpython.com/urllib-request/): Covers `urlopen`, `Request`, headers — everything used in `fetch()`
- [Scraping without libraries (Towards Data Science)](https://towardsdatascience.com/web-scraping-with-python-a-practical-introduction-9cf56c44b54e): Good overview of stdlib-only scraping
- [Understanding JSON-LD (CSS-Tricks)](https://css-tricks.com/json-ld-what-is-it-and-why-should-you-care/): Clear explainer on structured data in web pages
- [GitHub Actions for data collection (Simon Willison)](https://simonwillison.net/2020/Oct/9/git-scraping/): The "git scraping" pattern — exactly what this scraper does

### Videos

- [Automate the Boring Stuff — Chapter 12 (Web Scraping)](https://automatetheboringstuff.com/2e/chapter12/): Free online book chapter covering urllib and web fetching
- [GitHub Actions in 100 seconds (Fireship)](https://www.youtube.com/watch?v=eB0nUzAI7M8): Fast conceptual overview of the CI/CD platform

### Related Concepts (For Deeper Study)

- **Rate limiting / backoff**: `REQUEST_DELAY` is a fixed sleep. Production scrapers use exponential backoff when they hit 429 (Too Many Requests) responses.
- **robots.txt**: The ethical/legal standard for what scrapers are allowed to access. Worth checking before scraping any site at scale.
- **GitHub Actions artifacts**: An alternative to committing results — store `jobs.json` as a workflow artifact instead of committing it to the repo.
- **Selenium / Playwright**: For sites that require JavaScript execution (unlike this scraper's targets, which return data server-side).

---

## Related Code in This Project

| File | Relationship |
|------|-------------|
| `jobs.json` | Output of `save_results()` — auto-generated, not hand-written |
| `jobs.md` | Human-readable output, rendered on GitHub |
| `.github/workflows/scrape_jobs.yml` | Orchestrates `scrape_jobs.py` on a schedule |

---

## Next Steps

1. **Try it yourself**: Run `python scrape_jobs.py` locally. Watch the Wikipedia fetch, then observe how many companies get matched to Greenhouse/Lever boards vs. return empty.

2. **Extend the slug heuristics**: Find a company whose board you know (e.g., your target employer) and add its slug manually to a `KNOWN_SLUGS` dict as a fallback.

3. **Add pagination**: The Wikipedia API returns max 500 companies. Add a `cmcontinue` parameter loop to fetch all pages of the category.

4. **Common pitfalls**:
   - GitHub's cron scheduler can delay up to 6 hours during peak times — don't use it for time-sensitive workflows
   - App passwords are account-specific; if you rotate your Google account password, app passwords aren't invalidated, but if you revoke 2FA, all app passwords are deleted
   - Some companies register their Greenhouse board under a parent company's slug, not their own name — slug inference will miss these

---

*This deep dive was generated by AntiVibe - the anti-vibecoding learning framework.*  
*Learn what AI writes, not just accept it.*
