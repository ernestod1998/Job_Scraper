"""
Biotech MLE Job Scraper
Dynamically discovers US biotech companies via Wikipedia, then checks each
company's Greenhouse and Lever job boards for Machine Learning Engineer roles.
"""

import json
import os
import re
import time
from datetime import datetime
from urllib.request import urlopen, Request
from urllib.error import URLError

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
}

KEYWORDS = [
    "machine learning engineer", "ml engineer", "mle",
    "machine learning infra", "applied scientist", "ai engineer",
    "research engineer", "data scientist", "mlops",
]

# Seconds to wait between API probes — keeps us polite
REQUEST_DELAY = 0.3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def fetch(url):
    req = Request(url, headers=HEADERS)
    try:
        with urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", errors="ignore")
    except (URLError, TimeoutError, OSError) as e:
        print(f"  ⚠️  Could not fetch {url}: {e}")
        return ""


def is_mle_role(title: str) -> bool:
    return any(k in title.lower() for k in KEYWORDS)


def extract_location(job: dict) -> str:
    loc = job.get("jobLocation", {})
    if isinstance(loc, list):
        loc = loc[0] if loc else {}
    addr = loc.get("address", {})
    if isinstance(addr, dict):
        city = addr.get("addressLocality", "")
        state = addr.get("addressRegion", "")
        return f"{city}, {state}".strip(", ")
    return str(addr)


# ---------------------------------------------------------------------------
# Step 1 — Discover biotech companies from Wikipedia
# ---------------------------------------------------------------------------

def get_biotech_companies() -> list[tuple[str, str]]:
    """
    Returns a list of (company_name, normalized_slug) pairs from the
    Wikipedia category for US biotechnology companies.
    Uses the Wikipedia JSON API — no HTML parsing needed.
    """
    print("🌐 Fetching biotech company list from Wikipedia...")
    companies = []
    url = (
        "https://en.wikipedia.org/w/api.php"
        "?action=query&list=categorymembers"
        "&cmtitle=Category:Biotechnology_companies_of_the_United_States"
        "&cmlimit=500&cmtype=page&format=json"
    )
    raw = fetch(url)
    if not raw:
        return companies

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return companies

    for member in data.get("query", {}).get("categorymembers", []):
        name = member.get("title", "").strip()
        if name:
            companies.append(name)

    print(f"  ✅ Found {len(companies)} biotech companies on Wikipedia")
    return companies


def name_to_slugs(name: str) -> list[str]:
    """Generate candidate ATS slugs from a company name."""
    clean = re.sub(r'\([^)]+\)', '', name).strip().lower()

    # slug variants: no-separator and hyphenated
    no_sep = re.sub(r'[^a-z0-9]', '', clean)
    hyphen = re.sub(r'[^a-z0-9]+', '-', clean).strip('-')

    candidates = {no_sep, hyphen}

    # also try dropping common biotech suffixes
    suffixes = [
        'pharmaceuticals', 'pharmaceutical', 'therapeutics', 'biosciences',
        'bioscience', 'biotechnology', 'biotech', 'laboratories', 'labs',
        'sciences', 'science', 'healthcare', 'health', 'medicine', 'medicines',
        'oncology', 'genomics', 'informatics', 'technologies', 'technology',
    ]
    for suffix in suffixes:
        for base in [no_sep, hyphen.replace('-', '')]:
            if base.endswith(suffix) and len(base) - len(suffix) > 2:
                candidates.add(base[: -len(suffix)])

    # filter out very short or empty slugs
    return [s for s in candidates if len(s) > 2]


# ---------------------------------------------------------------------------
# Step 2 — Probe Greenhouse / Lever for each company
# ---------------------------------------------------------------------------

def probe_greenhouse(company_name: str, slug: str) -> list:
    time.sleep(REQUEST_DELAY)
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"
    raw = fetch(url)
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []

    jobs = []
    for job in data.get("jobs", []):
        title = job.get("title", "")
        if is_mle_role(title):
            jobs.append({
                "company": company_name,
                "title": title,
                "location": job.get("location", {}).get("name", ""),
                "url": job.get("absolute_url", f"https://boards.greenhouse.io/{slug}"),
                "date_posted": job.get("updated_at", "")[:10],
                "ats": "Greenhouse",
            })
    return jobs


def probe_lever(company_name: str, slug: str) -> list:
    time.sleep(REQUEST_DELAY)
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    raw = fetch(url)
    if not raw:
        return []
    try:
        postings = json.loads(raw)
    except json.JSONDecodeError:
        return []

    if not isinstance(postings, list):
        return []

    jobs = []
    for posting in postings:
        title = posting.get("text", "")
        if is_mle_role(title):
            jobs.append({
                "company": company_name,
                "title": title,
                "location": posting.get("categories", {}).get("location", ""),
                "url": posting.get("hostedUrl", f"https://jobs.lever.co/{slug}"),
                "date_posted": "",
                "ats": "Lever",
            })
    return jobs


def scrape_company(company_name: str) -> list:
    """Try all slug variants against Greenhouse then Lever."""
    slugs = name_to_slugs(company_name)
    for slug in slugs:
        jobs = probe_greenhouse(company_name, slug)
        if jobs is not None and len(jobs) >= 0:
            # valid board found — return even if 0 MLE roles (stop probing)
            raw = fetch(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
            if raw:
                try:
                    data = json.loads(raw)
                    if "jobs" in data:
                        return jobs
                except json.JSONDecodeError:
                    pass

        jobs = probe_lever(company_name, slug)
        if jobs is not None:
            raw = fetch(f"https://api.lever.co/v0/postings/{slug}?mode=json")
            if raw:
                try:
                    data = json.loads(raw)
                    if isinstance(data, list):
                        return jobs
                except json.JSONDecodeError:
                    pass

    return []


# ---------------------------------------------------------------------------
# Genentech — custom Phenom ATS, kept as standalone
# ---------------------------------------------------------------------------

def scrape_genentech():
    print("🔍 Scraping Genentech...")
    url = (
        "https://careers.gene.com/us/en/search-results"
        "?keywords=machine+learning+engineer&category=Data+Science+%26+AI%2FML"
    )
    html = fetch(url)
    jobs = []

    matches = re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.DOTALL)
    for match in matches:
        try:
            data = json.loads(match)
            items = (
                data if isinstance(data, list)
                else data.get("itemListElement", []) if data.get("@type") == "ItemList"
                else [data]
            )
            for item in items:
                job = item.get("item", item)
                title = job.get("title", job.get("name", ""))
                if title and is_mle_role(title):
                    jobs.append({
                        "company": "Genentech",
                        "title": title,
                        "location": extract_location(job),
                        "url": job.get("url", "https://careers.gene.com/us/en/c/data-science-ai-ml-jobs"),
                        "date_posted": job.get("datePosted", ""),
                        "ats": "Phenom",
                    })
        except json.JSONDecodeError:
            continue

    if not jobs:
        title_matches = re.findall(r'data-ph-at-job-title-text="([^"]+)"', html)
        link_matches = re.findall(r'href="(/us/en/job/[^"]+)"', html)
        for i, title in enumerate(title_matches):
            if is_mle_role(title):
                link = link_matches[i] if i < len(link_matches) else ""
                jobs.append({
                    "company": "Genentech",
                    "title": title,
                    "location": "South San Francisco, CA",
                    "url": f"https://careers.gene.com{link}" if link else "https://careers.gene.com/us/en/c/data-science-ai-ml-jobs",
                    "date_posted": "",
                    "ats": "Phenom",
                })

    print(f"  ✅ Found {len(jobs)} MLE role(s) at Genentech")
    return jobs


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_results(jobs: list):
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

    output = {"scraped_at": timestamp, "total": len(jobs), "jobs": jobs}
    with open(os.path.join(SCRIPT_DIR, "jobs.json"), "w") as f:
        json.dump(output, f, indent=2)

    lines = [
        "# 🧬 Biotech MLE Job Listings",
        f"*Last updated: {timestamp}*\n",
        f"**Total roles found: {len(jobs)}**\n",
    ]

    for company in sorted(set(j["company"] for j in jobs)):
        company_jobs = [j for j in jobs if j["company"] == company]
        lines.append(f"## {company} ({len(company_jobs)} role(s))\n")
        for job in company_jobs:
            lines.append(f"### [{job['title']}]({job['url']})")
            lines.append(f"- 📍 **Location:** {job['location'] or 'Not specified'}")
            if job.get("date_posted"):
                lines.append(f"- 📅 **Posted:** {job['date_posted']}")
            lines.append("")

    with open(os.path.join(SCRIPT_DIR, "jobs.md"), "w") as f:
        f.write("\n".join(lines))

    print(f"\n📄 Saved jobs.json and jobs.md ({len(jobs)} total roles)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

DAILY_LIMIT = 50

if __name__ == "__main__":
    progress_path = os.path.join(SCRIPT_DIR, "scrape_progress.json")

    companies = get_biotech_companies()

    if os.path.exists(progress_path):
        with open(progress_path) as f:
            progress = json.load(f)
        offset = progress["offset"]
        all_jobs = progress["jobs"]
        print(f"♻️  Resuming from company #{offset + 1} ({len(all_jobs)} jobs found so far)")
    else:
        offset = 0
        all_jobs = []

    all_jobs = [j for j in all_jobs if j["company"] != "Genentech"]
    all_jobs.extend(scrape_genentech())

    batch = companies[offset:offset + DAILY_LIMIT]
    for i, company_name in enumerate(batch, start=offset + 1):
        print(f"[{i}/{len(companies)}] Checking {company_name}...")
        jobs = scrape_company(company_name)
        if jobs:
            print(f"  ✅ Found {len(jobs)} MLE role(s)")
            all_jobs.extend(jobs)

    next_offset = offset + DAILY_LIMIT
    if next_offset >= len(companies):
        print(f"\n🔄 Reached end of company list — resetting to start")
        next_offset = 0

    with open(progress_path, "w") as f:
        json.dump({"offset": next_offset, "jobs": all_jobs}, f)

    save_results(all_jobs)