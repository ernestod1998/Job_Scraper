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
from datetime import datetime, timedelta, timezone
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

# Biotech email should only contain reliably fresh roles.
FRESH_JOB_LOOKBACK = timedelta(hours=24)


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


BAY_AREA_LOCATIONS = [
    "bay area",
    "san francisco", "south san francisco", "daly city",
    "oakland", "berkeley", "alameda", "emeryville", "richmond",
    "palo alto", "mountain view", "menlo park", "sunnyvale",
    "santa clara", "san jose", "cupertino", "los altos", "los gatos",
    "san mateo", "foster city", "redwood city", "brisbane", "millbrae",
    "san bruno", "burlingame", "belmont",
    "fremont", "hayward", "union city", "newark", "milpitas",
    "concord", "walnut creek", "pleasanton", "dublin", "san ramon",
    "danville", "livermore",
    "novato", "san rafael", "mill valley", "sausalito",
    "vacaville",
]


def is_bay_area(location: str) -> bool:
    if not location:
        return False
    loc = location.lower()
    return any(city in loc for city in BAY_AREA_LOCATIONS)


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


def _parse_posted_at(value: str, *, now: datetime | None = None) -> datetime | None:
    """
    Parse ATS posting dates into UTC datetimes.

    Some ATS APIs return exact ISO dates/datetimes, while Workday often returns
    relative strings like "Posted Today" or "Posted 3 hours ago".
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    raw = (value or "").strip()
    if not raw:
        return None

    text = re.sub(r'\s+', ' ', raw).strip().lower()
    text = text.removeprefix("posted ").strip()

    if text in {"today", "just posted", "just now"}:
        return now

    relative_m = re.search(
        r'(\d+)\s*(minutes?|mins?|hours?|hrs?)\b(?:\s*ago)?',
        text,
    )
    if relative_m:
        amount = int(relative_m.group(1))
        unit = relative_m.group(2)
        if unit.startswith(("minute", "min")):
            return now - timedelta(minutes=amount)
        return now - timedelta(hours=amount)

    iso_value = raw.replace("Z", "+00:00")
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', iso_value):
            parsed = datetime.strptime(iso_value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        else:
            parsed = datetime.fromisoformat(iso_value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            else:
                parsed = parsed.astimezone(timezone.utc)
        return parsed
    except ValueError:
        return None


def is_recent_posting(job: dict, *, now: datetime | None = None) -> bool:
    posted_at = _parse_posted_at(job.get("date_posted", ""), now=now)
    if posted_at is None:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)
    return timedelta(0) <= now - posted_at <= FRESH_JOB_LOOKBACK


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
# Curated Bay Area biotechs — direct ATS probes (Greenhouse / Workday)
# ---------------------------------------------------------------------------

# Each entry must include: name, ats, fallback_location, and the ATS-specific id
# - greenhouse: "slug" (used in boards-api.greenhouse.io/v1/boards/{slug}/jobs)
# - workday:    "url"  (full /wday/cxs/{tenant}/{site}/jobs endpoint)
CURATED_BIOTECHS = [
    # ---- Greenhouse (confirmed via probes) ----
    {"name": "10x Genomics",         "ats": "greenhouse", "slug": "10xgenomics",       "fallback_location": "Pleasanton, CA"},
    {"name": "Twist Bioscience",     "ats": "greenhouse", "slug": "twistbioscience",   "fallback_location": "South San Francisco, CA"},
    {"name": "Maze Therapeutics",    "ats": "greenhouse", "slug": "mazetherapeutics",  "fallback_location": "South San Francisco, CA"},
    {"name": "Freenome",             "ats": "greenhouse", "slug": "freenome",          "fallback_location": "South San Francisco, CA"},
    {"name": "Cytokinetics",         "ats": "greenhouse", "slug": "cytokinetics",      "fallback_location": "South San Francisco, CA"},
    {"name": "Natera",               "ats": "greenhouse", "slug": "natera",            "fallback_location": "San Carlos, CA"},
    {"name": "Inceptive",            "ats": "greenhouse", "slug": "inceptive",         "fallback_location": "Palo Alto, CA"},
    {"name": "Atomwise",             "ats": "greenhouse", "slug": "atomwise",          "fallback_location": "San Francisco, CA"},
    {"name": "Profluent",            "ats": "greenhouse", "slug": "profluent",         "fallback_location": "Berkeley, CA"},
    {"name": "Eikon Therapeutics",   "ats": "greenhouse", "slug": "eikontherapeutics", "fallback_location": "South San Francisco, CA"},
    {"name": "Altos Labs",           "ats": "greenhouse", "slug": "altoslabs",         "fallback_location": "Redwood City, CA"},
    {"name": "Arc Institute",        "ats": "greenhouse", "slug": "arcinstitute",      "fallback_location": "Palo Alto, CA"},
    {"name": "Caribou Biosciences",  "ats": "greenhouse", "slug": "caribou",           "fallback_location": "Berkeley, CA"},
    {"name": "Octant Bio",           "ats": "greenhouse", "slug": "octantbio",         "fallback_location": "Emeryville, CA"},
    # ---- Workday (confirmed) ----
    {"name": "Gilead Sciences",      "ats": "workday",
     "url": "https://gilead.wd1.myworkdayjobs.com/wday/cxs/gilead/gileadcareers/jobs",
     "fallback_location": "Foster City, CA"},
]


def probe_curated_greenhouse(entry: dict) -> list:
    time.sleep(REQUEST_DELAY)
    url = f"https://boards-api.greenhouse.io/v1/boards/{entry['slug']}/jobs?content=true"
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
        if not is_mle_role(title):
            continue
        loc = (job.get("location") or {}).get("name", "") or entry["fallback_location"]
        jobs.append({
            "company": entry["name"],
            "title": title,
            "location": loc,
            "url": job.get("absolute_url", f"https://boards.greenhouse.io/{entry['slug']}"),
            "date_posted": (job.get("updated_at") or "")[:10],
            "ats": "Greenhouse",
        })
    return jobs


WORKDAY_SEARCH_TERMS = [
    "machine learning",
    "data scientist",
    "applied scientist",
    "computational biology",
    "bioinformatics",
    "AI engineer",
]


def probe_curated_workday(entry: dict) -> list:
    """
    Workday's /jobs endpoint sometimes 400s on empty searchText, so we hit it
    once per term and dedupe by externalPath.
    """
    domain_m = re.match(r'https://([^/]+)', entry["url"])
    domain = domain_m.group(1) if domain_m else ""
    site_m = re.search(r'/wday/cxs/[^/]+/([^/]+)/jobs', entry["url"])
    site = site_m.group(1) if site_m else ""

    seen: dict[str, dict] = {}
    for term in WORKDAY_SEARCH_TERMS:
        time.sleep(REQUEST_DELAY)
        body = json.dumps({"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": term}).encode()
        try:
            req = Request(
                entry["url"],
                data=body,
                headers={**HEADERS, "Content-Type": "application/json", "Accept": "application/json"},
            )
            with urlopen(req, timeout=15) as r:
                data = json.loads(r.read().decode("utf-8", errors="ignore"))
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            print(f"  ⚠️  Workday {entry['name']} ({term!r}): {e}")
            continue

        for posting in data.get("jobPostings", []):
            ext_path = posting.get("externalPath", "")
            if ext_path in seen:
                continue
            title = posting.get("title", "")
            if not is_mle_role(title):
                continue
            public_url = f"https://{domain}/{site}{ext_path}" if ext_path else entry["url"]
            loc = posting.get("locationsText", "") or entry["fallback_location"]
            # Workday summarizes multi-location roles as "N Locations" — assume HQ
            if re.match(r'^\d+ Locations?$', loc):
                loc = entry["fallback_location"]
            seen[ext_path] = {
                "company": entry["name"],
                "title": title,
                "location": loc,
                "url": public_url,
                "date_posted": posting.get("postedOn") or "",
                "ats": "Workday",
            }
    return list(seen.values())


def scrape_curated_biotechs() -> list:
    print(f"🔬 Scraping {len(CURATED_BIOTECHS)} curated Bay Area biotechs...")
    all_jobs: list = []
    for entry in CURATED_BIOTECHS:
        if entry["ats"] == "greenhouse":
            jobs = probe_curated_greenhouse(entry)
        elif entry["ats"] == "workday":
            jobs = probe_curated_workday(entry)
        else:
            print(f"  ⚠️  Unknown ATS for {entry['name']}: {entry['ats']}")
            continue
        if jobs:
            print(f"  ✅ {entry['name']}: {len(jobs)} role(s)")
            all_jobs.extend(jobs)
    return all_jobs


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

# Only the past hour — fresh roles, no noise
LINKEDIN_LOOKBACK_SECONDS = 3600


def _parse_linkedin_cards(html: str) -> list[dict]:
    import html as html_mod
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

        title = html_mod.unescape(title_m.group(1).strip()) if title_m else ""
        if not title or not is_mle_role(title):
            continue
        company = (
            html_mod.unescape(re.sub(r'\s+', ' ', company_m.group(1).strip()))
            if company_m else "Unknown"
        )
        location = html_mod.unescape(
            (location_m.group(1).strip() if location_m else "")
        ).replace("\n", " ")
        parsed.append({
            "id": urn.group(1),
            "company": company,
            "title": title,
            "location": location,
            "date_posted": time_m.group(1) if time_m else "",
        })
    return parsed


def scrape_linkedin_recent() -> list:
    """
    Hits LinkedIn's public guest endpoint for SF Bay Area MLE/DS roles
    posted in the last hour. Returns a flat, deduped, recency-sorted list.
    """
    print("🔎 Scraping LinkedIn (last 1h)...")
    jobs_by_id: dict[str, dict] = {}

    for term in LINKEDIN_SEARCH_TERMS:
        for start in range(0, 75, 25):
            time.sleep(REQUEST_DELAY)
            url = (
                "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
                f"?keywords={urllib.parse.quote(term)}"
                "&location=San%20Francisco%20Bay%20Area"
                "&geoId=90000084"
                f"&f_TPR=r{LINKEDIN_LOOKBACK_SECONDS}"
                f"&start={start}"
            )
            html = fetch(url)
            if not html.strip():
                break
            parsed = _parse_linkedin_cards(html)
            if not parsed:
                break
            for p in parsed:
                if p["id"] in jobs_by_id:
                    continue
                jobs_by_id[p["id"]] = {
                    "company": p["company"],
                    "title": p["title"],
                    "location": p["location"],
                    "url": f"https://www.linkedin.com/jobs/view/{p['id']}/",
                    "date_posted": p["date_posted"],
                    "ats": "LinkedIn",
                }

    jobs = list(jobs_by_id.values())
    jobs.sort(key=lambda j: -_iso_to_ts(j.get("date_posted", "")))
    print(f"  ✅ LinkedIn: {len(jobs)} role(s) in last hour")
    return jobs


def _iso_to_ts(iso: str) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


def save_linkedin_results(jobs: list):
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    output = {"scraped_at": timestamp, "total": len(jobs), "jobs": jobs}
    with open(os.path.join(SCRIPT_DIR, "linkedin_jobs.json"), "w") as f:
        json.dump(output, f, indent=2)

    lines = [
        "# 🔥 LinkedIn — Last Hour MLE / DS Roles (SF Bay Area)",
        f"*Last updated: {timestamp}*\n",
        f"**{len(jobs)} role(s) posted in the last hour**\n",
    ]
    for job in jobs:
        lines.append(f"### [{job['title']}]({job['url']}) — {job['company']}")
        lines.append(f"- 📍 **Location:** {job['location'] or 'Not specified'}")
        if job.get("date_posted"):
            lines.append(f"- 🕒 **Posted:** {job['date_posted']}")
        lines.append("")
    with open(os.path.join(SCRIPT_DIR, "linkedin_jobs.md"), "w") as f:
        f.write("\n".join(lines))

    with open(os.path.join(SCRIPT_DIR, "linkedin_jobs.html"), "w") as f:
        f.write(_render_jobs_html(
            title="🔥 LinkedIn — Last Hour MLE / DS",
            subtitle="SF Bay Area · posted in the last hour",
            timestamp=timestamp,
            jobs=jobs,
            empty_message="No new roles posted in the last hour.",
            accent="#ff6b35",
        ))
    print(f"📄 Saved linkedin_jobs.json/.md/.html ({len(jobs)} roles)")


def _render_jobs_html(*, title: str, subtitle: str, timestamp: str,
                      jobs: list, empty_message: str, accent: str) -> str:
    import html as html_mod

    if not jobs:
        body = f'<div class="empty">{html_mod.escape(empty_message)}</div>'
    else:
        cards = []
        for j in jobs:
            posted = (
                f'<span class="meta-item">🕒 Posted {html_mod.escape(j["date_posted"])}</span>'
                if j.get("date_posted") else ""
            )
            ats_tag = (
                f'<span class="ats">{html_mod.escape(j["ats"])}</span>'
                if j.get("ats") else ""
            )
            cards.append(
                f'<div class="job">'
                f'<div class="title"><a href="{html_mod.escape(j["url"])}">'
                f'{html_mod.escape(j["title"])}</a></div>'
                f'<div class="company">{html_mod.escape(j["company"])} {ats_tag}</div>'
                f'<div class="meta">'
                f'<span class="meta-item">📍 {html_mod.escape(j["location"] or "Not specified")}</span>'
                f'{posted}'
                f'</div></div>'
            )
        body = "\n".join(cards)

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  max-width: 720px; margin: 24px auto; padding: 0 16px; color: #1a1a1a; background: #fff; line-height: 1.5; }}
h1 {{ font-size: 22px; margin: 0 0 4px 0; }}
.subtitle {{ color: #666; font-size: 14px; margin-bottom: 16px; }}
.summary {{ background: #fff7f2; padding: 12px 16px; border-left: 4px solid {accent};
  margin: 16px 0; border-radius: 4px; font-size: 14px; }}
.summary strong {{ font-size: 18px; color: {accent}; }}
.job {{ background: #fafafa; border: 1px solid #e8e8e8; border-radius: 8px;
  padding: 14px 18px; margin-bottom: 10px; }}
.title {{ font-size: 16px; font-weight: 600; margin-bottom: 4px; }}
.title a {{ color: #0a66c2; text-decoration: none; }}
.title a:hover {{ text-decoration: underline; }}
.company {{ color: #444; font-weight: 500; margin-bottom: 8px; font-size: 14px; }}
.ats {{ display: inline-block; background: #eaf3fb; color: #0a66c2; font-size: 11px;
  padding: 1px 8px; border-radius: 10px; font-weight: 500; margin-left: 6px; vertical-align: middle; }}
.meta {{ font-size: 13px; color: #666; }}
.meta-item {{ margin-right: 14px; }}
.empty {{ color: #999; font-style: italic; padding: 28px; text-align: center;
  background: #fafafa; border-radius: 8px; border: 1px dashed #ddd; }}
.foot {{ margin-top: 28px; padding-top: 12px; border-top: 1px solid #eee;
  color: #888; font-size: 12px; text-align: center; }}
.foot a {{ color: #0a66c2; }}
</style></head>
<body>
<h1>{title}</h1>
<div class="subtitle">{subtitle}</div>
<div class="summary"><strong>{len(jobs)}</strong> role(s) &nbsp;·&nbsp; scraped {timestamp}</div>
{body}
<div class="foot">Auto-generated by <a href="https://github.com/ernestod1998/Job_Scraper">Job_Scraper</a></div>
</body></html>"""


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def save_results(jobs: list):
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    output = {"scraped_at": timestamp, "total": len(jobs), "jobs": jobs}
    with open(os.path.join(SCRIPT_DIR, "jobs.json"), "w") as f:
        json.dump(output, f, indent=2)

    lines = [
        "# 🧬 Fresh Biotech MLE Job Listings (SF Bay Area)",
        f"*Last updated: {timestamp}*\n",
        f"**{len(jobs)} role(s) posted in the last 24 hours**\n",
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

    with open(os.path.join(SCRIPT_DIR, "jobs.html"), "w") as f:
        f.write(_render_jobs_html(
            title="🧬 Fresh Biotech MLE Job Listings",
            subtitle="SF Bay Area · posted in the last 24 hours",
            timestamp=timestamp,
            jobs=jobs,
            empty_message="No biotech roles posted in the last 24 hours.",
            accent="#2ea04f",
        ))

    print(f"\n📄 Saved jobs.json/.md/.html ({len(jobs)} total roles)")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if "--linkedin-only" in sys.argv:
        linkedin_jobs = scrape_linkedin_recent()
        save_linkedin_results(linkedin_jobs)
        sys.exit(0)

    all_jobs = list(scrape_genentech())
    all_jobs.extend(scrape_curated_biotechs())

    before = len(all_jobs)
    all_jobs = [j for j in all_jobs if is_bay_area(j.get("location", ""))]
    print(f"\n📍 Bay Area filter: {before} → {len(all_jobs)} roles")

    before = len(all_jobs)
    all_jobs = [j for j in all_jobs if is_recent_posting(j)]
    print(f"🕒 Freshness filter (last 24h): {before} → {len(all_jobs)} roles")

    save_results(all_jobs)
