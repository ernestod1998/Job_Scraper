"""
Biotech MLE Job Scraper
Dynamically discovers US biotech companies via Wikipedia, then checks each
company's Greenhouse and Lever job boards for Machine Learning Engineer roles.
"""

import json
import os
import re
import sys
import time
import urllib.parse
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
    # ML engineering
    "machine learning engineer", "ml engineer", "mle",
    "machine learning infra", "ai engineer", "mlops",
    "research engineer",
    # Applied / AI / ML scientist
    "applied scientist", "ai scientist", "ml scientist",
    # Data science
    "data scientist", "data science",
    # Computational / informatics
    "computational scientist", "computational biologist",
    "bioinformatics scientist", "bioinformatics engineer",
    "cheminformatics",
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
# LinkedIn — public guest endpoint, bucketed by recency (broad US-wide net)
# ---------------------------------------------------------------------------

LINKEDIN_SEARCH_TERMS = [
    "machine learning engineer",
    "data scientist",
    "applied scientist",
    "AI engineer",
    "MLOps engineer",
    "computational biologist",
    "bioinformatics",
    "cheminformatics",
]

# (label, seconds) — iterate smallest first so tightest bucket wins on dedupe
LINKEDIN_BUCKETS = [("1h", 3600), ("6h", 21600), ("24h", 86400)]


def _parse_linkedin_cards(html: str) -> list[dict]:
    cards = re.split(r'<li[^>]*>', html)[1:]
    parsed = []
    for card in cards:
        urn = re.search(r'data-entity-urn="urn:li:jobPosting:(\d+)"', card)
        if not urn:
            continue
        title_m = re.search(r'base-search-card__title[^>]*>\s*([^<]+)', card)
        company_m = re.search(
            r'base-search-card__subtitle[^>]*>.*?<a[^>]*>\s*([^<]+)\s*</a>',
            card, re.DOTALL,
        ) or re.search(r'base-search-card__subtitle[^>]*>\s*([^<]+)', card)
        location_m = re.search(r'job-search-card__location[^>]*>\s*([^<]+)', card)
        time_m = re.search(r'<time[^>]*datetime="([^"]+)"', card)

        title = title_m.group(1).strip() if title_m else ""
        if not title or not is_mle_role(title):
            continue
        company = re.sub(r'\s+', ' ', company_m.group(1).strip()) if company_m else "Unknown"
        parsed.append({
            "id": urn.group(1),
            "company": company,
            "title": title,
            "location": (location_m.group(1).strip() if location_m else "").replace("\n", " "),
            "date_posted": time_m.group(1) if time_m else "",
        })
    return parsed


def scrape_linkedin_recent() -> list:
    """
    Hits LinkedIn's public guest endpoint, bucketed past 1h / 6h / 24h.
    Each job is tagged with its tightest bucket. Biotech + pharma industries.
    """
    print("🔎 Scraping LinkedIn (last 1h / 6h / 24h)...")
    jobs_by_id: dict[str, dict] = {}

    for bucket_label, seconds in LINKEDIN_BUCKETS:
        for term in LINKEDIN_SEARCH_TERMS:
            for start in range(0, 75, 25):
                time.sleep(REQUEST_DELAY)
                url = (
                    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
                    f"?keywords={urllib.parse.quote(term)}"
                    "&location=San%20Francisco%20Bay%20Area"
                    "&geoId=90000084"
                    f"&f_TPR=r{seconds}"
                    f"&start={start}"
                )
                html = fetch(url)
                if not html.strip():
                    break
                parsed = _parse_linkedin_cards(html)
                if not parsed:
                    break
                for p in parsed:
                    # tightest bucket wins — don't overwrite if already seen
                    if p["id"] in jobs_by_id:
                        continue
                    jobs_by_id[p["id"]] = {
                        "company": p["company"],
                        "title": p["title"],
                        "location": p["location"],
                        "url": f"https://www.linkedin.com/jobs/view/{p['id']}/",
                        "date_posted": p["date_posted"],
                        "ats": "LinkedIn",
                        "freshness": bucket_label,
                    }

    jobs = list(jobs_by_id.values())
    # sort: tightest bucket first, then most-recent datetime first
    bucket_order = {b[0]: i for i, b in enumerate(LINKEDIN_BUCKETS)}
    jobs.sort(key=lambda j: (bucket_order.get(j.get("freshness", "24h"), 99),
                             -_iso_to_ts(j.get("date_posted", ""))))
    bucket_counts = {b[0]: sum(1 for j in jobs if j["freshness"] == b[0]) for b in LINKEDIN_BUCKETS}
    print(f"  ✅ LinkedIn: {len(jobs)} role(s) — {bucket_counts}")
    return jobs


def _iso_to_ts(iso: str) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


def save_linkedin_results(jobs: list):
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    output = {"scraped_at": timestamp, "total": len(jobs), "jobs": jobs}
    with open(os.path.join(SCRIPT_DIR, "linkedin_jobs.json"), "w") as f:
        json.dump(output, f, indent=2)

    lines = [
        "# 🔥 LinkedIn — MLE / DS / Applied Science (SF Bay Area)",
        f"*Last updated: {timestamp}*\n",
        f"**Total roles found: {len(jobs)}**\n",
    ]
    for bucket_label, _ in LINKEDIN_BUCKETS:
        bucket_jobs = [j for j in jobs if j.get("freshness") == bucket_label]
        if not bucket_jobs:
            continue
        lines.append(f"## Posted in last {bucket_label} — {len(bucket_jobs)} role(s)\n")
        for job in bucket_jobs:
            lines.append(f"### [{job['title']}]({job['url']}) — {job['company']}")
            lines.append(f"- 📍 **Location:** {job['location'] or 'Not specified'}")
            if job.get("date_posted"):
                lines.append(f"- 🕒 **Posted:** {job['date_posted']}")
            lines.append("")

    with open(os.path.join(SCRIPT_DIR, "linkedin_jobs.md"), "w") as f:
        f.write("\n".join(lines))
    print(f"📄 Saved linkedin_jobs.json and linkedin_jobs.md ({len(jobs)} roles)")


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
    if "--linkedin-only" in sys.argv:
        linkedin_jobs = scrape_linkedin_recent()
        save_linkedin_results(linkedin_jobs)
        sys.exit(0)

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