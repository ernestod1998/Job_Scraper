#!/usr/bin/env python3
"""Bounded discovery, verification, and scraping for public ATS job boards.

The registry is deliberately separate from ``discovered_companies.json``.
That file remains the curated biotech expansion list; this module manages the
much larger, lower-trust set of boards found from public indexes.

Only documented public job-board interfaces are supported:

* Greenhouse Job Board API
* Lever Postings API
* Ashby public Job Posting API
* Gem Job Board API
* verified Workday CXS ``/jobs`` endpoints

Network operations are capped by both request count and elapsed time.  The
functions accept an injected client so tests never need the network.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import time
import urllib.parse
from copy import deepcopy
from datetime import date, timedelta
from pathlib import Path
from typing import Callable, Iterable, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SCRIPT_DIR = Path(__file__).resolve().parent
REGISTRY_PATH = SCRIPT_DIR / "ats_registry.json"
SCHEMA_VERSION = 1
ACTIVE_SHARDS = 7
DEFAULT_REQUEST_CAP = 1500
DEFAULT_TIME_CAP_SECONDS = 20 * 60

YC_HIRING_URL = "https://yc-oss.github.io/api/companies/hiring.json"
SIMPLIFY_LISTINGS_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/"
    "dev/.github/scripts/listings.json"
)
WAYBACK_CDX_URL = "https://web.archive.org/cdx/search/cdx"
COMMON_CRAWL_INDEXES_URL = "https://index.commoncrawl.org/collinfo.json"
DISCOVERY_HOSTS = (
    "boards.greenhouse.io",
    "job-boards.greenhouse.io",
    "jobs.lever.co",
    "jobs.ashbyhq.com",
    "jobs.gem.com",
)
DISCOVERY_PREFIXES = "abcdefghijklmnopqrstuvwxyz0123456789"

_HEADERS = {
    "User-Agent": "Job-Scraper-ATS-Registry/1.0 (+public-job-board-discovery)",
    "Accept": "application/json",
}

_SLUG_RE = {
    "greenhouse": re.compile(
        r"https?://(?:boards|job-boards)\.greenhouse\.io/(?!embed(?:/|\?|$))"
        r"([^/?#]+)", re.I
    ),
    "lever": re.compile(r"https?://jobs\.lever\.co/([^/?#]+)", re.I),
    "ashby": re.compile(r"https?://jobs\.ashbyhq\.com/([^/?#]+)", re.I),
    "gem": re.compile(r"https?://jobs\.gem\.com/([^/?#]+)", re.I),
}
_GREENHOUSE_EMBED_RE = re.compile(
    r"https?://[^\s\"']*greenhouse\.io/embed/job_board(?:/js)?\?[^\s\"']*\bfor=([^&#\s\"']+)",
    re.I,
)
_WORKDAY_CXS_RE = re.compile(
    r"^https://([^/]+)/wday/cxs/([^/]+)/([^/]+)/jobs/?$", re.I
)
_WORKDAY_PUBLIC_HOST_RE = re.compile(r"^[^.]+\.wd\d*\.myworkdayjobs\.com$", re.I)


def empty_registry() -> dict:
    """Return a fresh registry document."""
    return {
        "schema_version": SCHEMA_VERSION,
        "baseline_complete": False,
        "cursors": {
            "candidates": 0,
            "discovery_prefix": 0,
            "yc_companies": 0,
            "shards": {str(i): 0 for i in range(ACTIVE_SHARDS)},
        },
        "boards": {},
    }


def _ensure_schema(data: object) -> dict:
    if not isinstance(data, dict):
        data = empty_registry()
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("baseline_complete", False)
    cursors = data.setdefault("cursors", {})
    cursors.setdefault("candidates", 0)
    cursors.setdefault("discovery_prefix", 0)
    cursors.setdefault("yc_companies", 0)
    shards = cursors.setdefault("shards", {})
    for i in range(ACTIVE_SHARDS):
        shards.setdefault(str(i), 0)
    data.setdefault("boards", {})
    return data


def load_registry(path: os.PathLike | str = REGISTRY_PATH) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return _ensure_schema(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return empty_registry()


def save_registry(data: dict, path: os.PathLike | str = REGISTRY_PATH) -> None:
    """Atomically persist registry state so a cancelled run cannot truncate it."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(_ensure_schema(data), f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(temp, target)


def normalize_workday_cxs(url: str) -> str | None:
    """Return a canonical Workday CXS jobs URL when it can be derived safely.

    Already-CXS URLs are preserved (apart from query/fragment/trailing slash).
    Public Workday URLs are converted only when both tenant and site are
    structurally present; merely seeing ``myworkdayjobs.com`` is insufficient.
    """
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except (TypeError, ValueError):
        return None
    if parts.scheme.lower() != "https" or not parts.netloc:
        return None
    host = parts.netloc.lower()
    path = re.sub(r"/+", "/", parts.path).rstrip("/")
    direct = _WORKDAY_CXS_RE.match(f"https://{host}{path}")
    if direct:
        tenant, site = direct.group(2), direct.group(3)
        return f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    if not _WORKDAY_PUBLIC_HOST_RE.match(host):
        return None
    # Public paths normally look like /en-US/Site/job/... or /Site/job/....
    segments = [urllib.parse.unquote(s) for s in path.split("/") if s]
    if segments and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", segments[0]):
        segments = segments[1:]
    if len(segments) < 2 or segments[1].lower() not in {"job", "jobs"}:
        return None
    site = segments[0]
    tenant = host.split(".", 1)[0]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", site):
        return None
    return f"https://{host}/wday/cxs/{tenant}/{site}/jobs"


def locator_from_url(url: str) -> tuple[str, str, str] | None:
    """Return ``(ats, locator_field, locator)`` for a supported board URL."""
    if not isinstance(url, str):
        return None
    m = _GREENHOUSE_EMBED_RE.search(url)
    if m:
        return "greenhouse", "slug", urllib.parse.unquote(m.group(1))
    for ats, pattern in _SLUG_RE.items():
        m = pattern.search(url)
        if m:
            return ats, "slug", urllib.parse.unquote(m.group(1))
    cxs = normalize_workday_cxs(url)
    if cxs:
        return "workday", "url", cxs
    return None


def board_key(ats: str, locator: str) -> str:
    """Stable normalized identity while the original locator stays in data."""
    ats_norm = ats.strip().lower()
    if ats_norm == "workday":
        try:
            p = urllib.parse.urlsplit(locator)
            locator_norm = f"https://{p.netloc.lower()}{p.path.rstrip('/')}".casefold()
        except ValueError:
            locator_norm = locator.strip().casefold()
    else:
        locator_norm = locator.strip().casefold()
    return f"{ats_norm}:{locator_norm}"


def keys_for_entries(entries: Iterable[dict]) -> set[str]:
    """Build skip keys from CURATED_BIOTECHS/discovered_companies entries."""
    keys: set[str] = set()
    for entry in entries:
        ats = str(entry.get("ats") or "").lower()
        locator = entry.get("url") if ats == "workday" else entry.get("slug")
        if ats and locator and ats in {*_SLUG_RE, "workday"}:
            if ats == "workday":
                locator = normalize_workday_cxs(str(locator)) or locator
            keys.add(board_key(ats, str(locator)))
    return keys


def add_candidate(
    registry: dict,
    *,
    name: str,
    ats: str,
    locator: str,
    source: str,
    feed: str = "general",
    skip_keys: set[str] | None = None,
) -> bool:
    """Add/merge one candidate. Return True only for a newly-created board."""
    ats = ats.lower().strip()
    if ats not in {*_SLUG_RE, "workday"} or feed not in {"general", "biotech"}:
        return False
    locator_field = "url" if ats == "workday" else "slug"
    if ats == "workday":
        locator = normalize_workday_cxs(locator) or ""
    locator = locator.strip()
    if not locator:
        return False
    key = board_key(ats, locator)
    if skip_keys and key in skip_keys:
        return False
    boards = _ensure_schema(registry)["boards"]
    existing = boards.get(key)
    if existing:
        sources = existing.setdefault("sources", [])
        if source not in sources:
            sources.append(source)
            sources.sort()
        if not existing.get("name") and name:
            existing["name"] = name
        return False
    display_name = (name or locator).strip()
    boards[key] = {
        "name": display_name,
        "ats": ats,
        locator_field: locator,
        "sources": [source],
        "feed": feed,
        "status": "candidate",
        "failure_count": 0,
        "retry_after": None,
        "promoted_until": None,
        # Notification cold-start is per board, not global. Boards can be
        # activated weeks after the registry's first run and their existing
        # inventory must still establish a silent baseline.
        "baseline_complete": False,
    }
    return True


def _walk_urls(value: object, inherited_name: str = "") -> Iterator[tuple[str, str]]:
    if isinstance(value, dict):
        name = inherited_name
        for field in ("company_name", "companyName", "company", "name"):
            candidate = value.get(field)
            if isinstance(candidate, str) and candidate.strip():
                name = candidate.strip()
                break
        for child in value.values():
            yield from _walk_urls(child, name)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_urls(child, inherited_name)
    elif isinstance(value, str):
        for match in re.finditer(r"https?://[^\s\"'<>]+", value):
            yield inherited_name, match.group(0).rstrip(".,);]")


def candidates_from_json(data: object, source: str) -> Iterator[dict]:
    for name, url in _walk_urls(data):
        found = locator_from_url(url)
        if found:
            ats, field, locator = found
            inferred = name or (locator if field == "slug" else urllib.parse.urlsplit(locator).netloc)
            yield {"name": inferred, "ats": ats, "locator": locator, "source": source}


def candidates_from_cdx(data: object, source: str = "wayback-cdx") -> Iterator[dict]:
    """Parse CDX output=json rows (including their header row)."""
    if not isinstance(data, list) or not data:
        return
    header = data[0] if isinstance(data[0], list) else []
    try:
        original_i = header.index("original")
    except ValueError:
        original_i = 0
    for row in data[1:]:
        if not isinstance(row, list) or original_i >= len(row):
            continue
        found = locator_from_url(str(row[original_i]))
        if found:
            ats, _field, locator = found
            yield {"name": locator, "ats": ats, "locator": locator, "source": source}


class BoundedClient:
    """Small urllib client with hard request and elapsed-time ceilings."""

    def __init__(
        self,
        max_requests: int = DEFAULT_REQUEST_CAP,
        max_seconds: float = DEFAULT_TIME_CAP_SECONDS,
        timeout: float = 20,
    ):
        self.max_requests = max(0, max_requests)
        self.max_seconds = max(0.0, max_seconds)
        self.timeout = timeout
        self.requests = 0
        self.started = time.monotonic()

    @property
    def exhausted(self) -> bool:
        return self.requests >= self.max_requests or time.monotonic() - self.started >= self.max_seconds

    @property
    def elapsed_seconds(self) -> float:
        return time.monotonic() - self.started

    def claim(self) -> bool:
        """Reserve one HTTP request while enforcing both global ceilings."""
        if self.exhausted:
            return False
        self.requests += 1
        return True

    def json(self, url: str, *, payload: dict | None = None) -> object | None:
        if not self.claim():
            return None
        data = json.dumps(payload).encode() if payload is not None else None
        headers = dict(_HEADERS)
        if data is not None:
            headers["Content-Type"] = "application/json"
        try:
            with urlopen(Request(url, data=data, headers=headers), timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8", errors="replace"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError):
            return None

    def text(self, url: str) -> str | None:
        if not self.claim():
            return None
        try:
            with urlopen(Request(url, headers=_HEADERS), timeout=self.timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            return None


def _cdx_url(host: str, limit: int, prefix: str = "") -> str:
    query = urllib.parse.urlencode(
        {
            "url": f"{host}/{prefix}*" if prefix else f"{host}/*",
            "output": "json",
            "fl": "original",
            "filter": "statuscode:200",
            "collapse": "urlkey",
            "limit": str(limit),
        }
    )
    return f"{WAYBACK_CDX_URL}?{query}"


def resolve_yc_homepages(
    data: object,
    registry: dict,
    client: BoundedClient,
    *,
    company_limit: int = 3,
) -> list[dict]:
    """Resolve a cursor-bounded YC slice from company sites to public ATS URLs.

    The YC feed is a company directory, not a job listing feed. Its records
    normally contain only the company's homepage, so merely walking the feed's
    JSON yields no board locators. Probe a few homepages per run and, when
    present, one careers/jobs link from each. The shared client keeps every
    request inside the global ceiling.
    """
    if not isinstance(data, list) or not data or company_limit <= 0:
        return []
    cursor = int(registry["cursors"].get("yc_companies", 0)) % len(data)
    found: list[dict] = []
    inspected = 0
    while inspected < min(company_limit, len(data)) and not client.exhausted:
        item = data[(cursor + inspected) % len(data)]
        inspected += 1
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        homepage = str(item.get("website") or "").strip()
        if not homepage.startswith(("https://", "http://")):
            continue
        page = client.text(homepage) or ""
        page_candidates = list(candidates_from_json(page, "yc-hiring-homepage"))
        if not page_candidates and page and not client.exhausted:
            decoded = html.unescape(page)
            hrefs = re.findall(r'''href\s*=\s*["']([^"']+)["']''', decoded, re.I)
            careers_url = next((
                urllib.parse.urljoin(homepage, href)
                for href in hrefs
                if re.search(r"(?:^|[/_-])(careers?|jobs?|join-us|work-with-us)(?:[/_?#-]|$)", href, re.I)
            ), "")
            if careers_url:
                page_candidates = list(candidates_from_json(
                    client.text(careers_url) or "", "yc-hiring-careers"
                ))
        for candidate in page_candidates:
            candidate["name"] = name or candidate["name"]
            found.append(candidate)
    registry["cursors"]["yc_companies"] = (cursor + inspected) % len(data)
    return found


def seed_registry(
    registry: dict,
    client: BoundedClient,
    *,
    limit: int = 200,
    skip_keys: set[str] | None = None,
    include_wayback: bool = True,
    include_common_crawl: bool = False,
) -> dict:
    """Fetch bounded seed inputs and merge up to ``limit`` new candidates."""
    added = 0
    seen = 0
    yc_data: object = None

    def consume(items: Iterable[dict]) -> None:
        nonlocal added, seen
        for item in items:
            if added >= limit:
                return
            seen += 1
            if add_candidate(
                registry,
                name=item["name"],
                ats=item["ats"],
                locator=item["locator"],
                source=item["source"],
                skip_keys=skip_keys,
            ):
                added += 1

    for url, source in ((YC_HIRING_URL, "yc-hiring"), (SIMPLIFY_LISTINGS_URL, "simplify")):
        if added >= limit or client.exhausted:
            break
        data = client.json(url)
        if source == "yc-hiring":
            yc_data = data
        if data is not None:
            consume(candidates_from_json(data, source))

    if yc_data is not None and added < limit and not client.exhausted:
        consume(resolve_yc_homepages(yc_data, registry, client))

    if include_wayback and added < limit:
        prefix_i = int(registry["cursors"].get("discovery_prefix", 0)) % len(DISCOVERY_PREFIXES)
        prefix = DISCOVERY_PREFIXES[prefix_i]
        hosts_visited = 0
        for host in DISCOVERY_HOSTS:
            if added >= limit or client.exhausted:
                break
            data = client.json(_cdx_url(host, min(500, max(25, limit * 2)), prefix))
            hosts_visited += 1
            if data is not None:
                consume(candidates_from_cdx(data))
        # Do not skip remaining hosts when the candidate/request cap ends a
        # prefix early. Replaying is safe because registry keys deduplicate it.
        if hosts_visited == len(DISCOVERY_HOSTS):
            registry["cursors"]["discovery_prefix"] = (
                prefix_i + 1
            ) % len(DISCOVERY_PREFIXES)

    # Common Crawl is opt-in fallback because its index is shared public
    # infrastructure. One current-index lookup and bounded host queries only.
    if include_common_crawl and added < limit and not client.exhausted:
        indexes = client.json(COMMON_CRAWL_INDEXES_URL)
        index_url = ""
        if isinstance(indexes, list) and indexes and isinstance(indexes[0], dict):
            index_url = str(indexes[0].get("cdx-api") or "")
        for host in DISCOVERY_HOSTS:
            if not index_url or added >= limit or client.exhausted:
                break
            query = urllib.parse.urlencode({"url": f"{host}/*", "output": "json", "pageSize": "100"})
            raw = client.text(f"{index_url}?{query}")
            rows = []
            for line in (raw or "").splitlines()[:100]:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
            consume(candidates_from_json(rows, "common-crawl"))

    return {"added": added, "supported_urls_seen": seen, "requests": client.requests}


def _endpoint(board: dict) -> str:
    ats = board["ats"]
    if ats == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{urllib.parse.quote(board['slug'])}/jobs?content=true"
    if ats == "lever":
        return f"https://api.lever.co/v0/postings/{urllib.parse.quote(board['slug'])}?mode=json"
    if ats == "ashby":
        return f"https://api.ashbyhq.com/posting-api/job-board/{urllib.parse.quote(board['slug'])}"
    if ats == "gem":
        return f"https://api.gem.com/job_board/v0/{urllib.parse.quote(board['slug'])}/job_posts/"
    return board.get("url", "")


def _valid_response(ats: str, data: object) -> bool:
    if ats == "greenhouse":
        return isinstance(data, dict) and isinstance(data.get("jobs"), list)
    if ats == "lever":
        return isinstance(data, list)
    if ats == "ashby":
        return isinstance(data, dict) and isinstance(data.get("jobs"), list)
    if ats == "gem":
        return isinstance(data, (dict, list)) and (
            isinstance(data, list)
            or any(isinstance(data.get(k), list) for k in ("job_posts", "jobs", "data"))
        )
    if ats == "workday":
        return isinstance(data, dict) and isinstance(data.get("jobPostings"), list)
    return False


def fetch_board(board: dict, client: BoundedClient, *, verification_only: bool = False) -> object | None:
    if board.get("ats") == "workday":
        return client.json(_endpoint(board), payload={
            "appliedFacets": {}, "limit": 1 if verification_only else 20,
            "offset": 0, "searchText": "machine learning",
        })
    return client.json(_endpoint(board))


def verify_registry(
    registry: dict,
    client: BoundedClient,
    *,
    limit: int = 100,
    today: date | None = None,
) -> dict:
    """Verify a cursor-bounded slice of candidates/inactive retries."""
    today = today or date.today()
    registry = _ensure_schema(registry)
    boards = registry["boards"]
    keys = sorted(boards)
    if not keys or limit <= 0:
        return {"attempted": 0, "activated": 0, "failed": 0, "requests": client.requests}
    cursor = int(registry["cursors"].get("candidates", 0)) % len(keys)
    attempted = activated = failed = inspected = 0
    while inspected < len(keys) and attempted < limit and not client.exhausted:
        idx = (cursor + inspected) % len(keys)
        key = keys[idx]
        board = boards[key]
        inspected += 1
        retry = board.get("retry_after")
        eligible = board.get("status") == "candidate"
        if board.get("status") == "inactive" and retry:
            try:
                eligible = date.fromisoformat(retry) <= today
            except ValueError:
                eligible = True
        if not eligible:
            continue
        data = fetch_board(board, client, verification_only=True)
        attempted += 1
        board["last_verified"] = today.isoformat()
        if _valid_response(board["ats"], data):
            board["status"] = "active"
            board["failure_count"] = 0
            board["retry_after"] = None
            activated += 1
        else:
            board["failure_count"] = int(board.get("failure_count") or 0) + 1
            failed += 1
            if board["failure_count"] >= 3:
                board["status"] = "inactive"
                board["retry_after"] = (today + timedelta(days=30)).isoformat()
    registry["cursors"]["candidates"] = (cursor + inspected) % len(keys)
    return {"attempted": attempted, "activated": activated, "failed": failed, "requests": client.requests}


def stable_shard(key: str, count: int = ACTIVE_SHARDS) -> int:
    return int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16) % count


def _rows(ats: str, data: object) -> list:
    if ats == "lever":
        return data if isinstance(data, list) else []
    if not isinstance(data, dict):
        return []
    if ats in {"greenhouse", "ashby"}:
        return data.get("jobs") if isinstance(data.get("jobs"), list) else []
    if ats == "workday":
        return data.get("jobPostings") if isinstance(data.get("jobPostings"), list) else []
    for field in ("job_posts", "jobs", "data"):
        if isinstance(data.get(field), list):
            return data[field]
    return []


def _job_from_row(board: dict, row: dict) -> dict | None:
    ats = board["ats"]
    if ats == "greenhouse":
        return {
            "company": board["name"], "title": row.get("title", ""),
            "location": (row.get("location") or {}).get("name", ""),
            "url": row.get("absolute_url") or f"https://boards.greenhouse.io/{board['slug']}",
            "date_posted": row.get("updated_at") or "", "ats": "Greenhouse",
        }
    if ats == "lever":
        categories = row.get("categories") or {}
        return {
            "company": board["name"], "title": row.get("text", ""),
            "location": categories.get("location", ""),
            "url": row.get("hostedUrl") or f"https://jobs.lever.co/{board['slug']}",
            "date_posted": row.get("createdAt") or "", "ats": "Lever",
        }
    if ats == "ashby":
        return {
            "company": board["name"], "title": row.get("title", ""),
            "location": row.get("location") or "",
            "url": row.get("jobUrl") or f"https://jobs.ashbyhq.com/{board['slug']}",
            "date_posted": row.get("publishedAt") or "", "ats": "Ashby",
        }
    if ats == "gem":
        return {
            "company": board["name"], "title": row.get("title") or row.get("name") or "",
            "location": row.get("location") or row.get("location_name") or "",
            "url": row.get("url") or row.get("job_url") or f"https://jobs.gem.com/{board['slug']}",
            "date_posted": row.get("created_at") or row.get("published_at") or "", "ats": "Gem",
        }
    if ats == "workday":
        endpoint = urllib.parse.urlsplit(board["url"])
        match = _WORKDAY_CXS_RE.match(board["url"])
        site = match.group(3) if match else ""
        ext = row.get("externalPath") or ""
        return {
            "company": board["name"], "title": row.get("title", ""),
            "location": row.get("locationsText") or "",
            "url": f"https://{endpoint.netloc}/{site}{ext}" if ext else board["url"],
            "date_posted": row.get("postedOn") or "", "ats": "Workday",
        }
    return None


def scrape_registry(
    registry: dict,
    client: BoundedClient,
    *,
    role_filter: Callable[[str], bool],
    location_filter: Callable[[str, str], bool] | None = None,
    workday_fetcher: Callable[[dict], list[dict] | None] | None = None,
    shard: int | None = None,
    board_limit: int = 200,
    today: date | None = None,
) -> dict:
    """Scrape a cursor-bounded active-board shard.

    ``role_filter`` is required so the caller supplies the repository's
    existing ``is_mle_role`` function instead of this module inventing a new
    role universe. ``location_filter`` receives ``(location, feed)``.

    The caller should pass the repository's existing
    ``probe_curated_workday`` as ``workday_fetcher``. That adapter retains all
    established search terms, pagination, and multi-location detail lookups;
    the registry's one-result Workday request is verification-only.
    """
    today = today or date.today()
    registry = _ensure_schema(registry)
    if shard is None:
        shard = today.toordinal() % ACTIVE_SHARDS
    if not 0 <= shard < ACTIVE_SHARDS:
        raise ValueError(f"shard must be 0..{ACTIVE_SHARDS - 1}")
    active = []
    for key, board in registry["boards"].items():
        if board.get("status") != "active":
            continue
        promoted = False
        try:
            promoted = bool(board.get("promoted_until")) and date.fromisoformat(board["promoted_until"]) >= today
        except ValueError:
            pass
        if promoted or stable_shard(key) == shard:
            active.append(key)
    active.sort()
    if not active:
        return {"jobs": [], "boards_attempted": 0, "requests": client.requests,
                "boards_failed": 0, "baseline_suppressed": 0,
                "elapsed_seconds": round(getattr(client, "elapsed_seconds", 0.0), 3)}
    cursor_key = str(shard)
    cursor = int(registry["cursors"]["shards"].get(cursor_key, 0)) % len(active)
    jobs: list[dict] = []
    attempted = inspected = failed = 0
    while inspected < len(active) and attempted < board_limit and not client.exhausted:
        idx = (cursor + inspected) % len(active)
        key = active[idx]
        board = registry["boards"][key]
        inspected += 1
        attempted += 1
        normalized_jobs: list[dict] | None = None
        if board["ats"] == "workday" and workday_fetcher is not None:
            try:
                normalized_jobs = workday_fetcher({
                    **board,
                    "fallback_location": board.get("fallback_location") or "",
                })
            except Exception:
                normalized_jobs = None
            valid = isinstance(normalized_jobs, list)
        else:
            data = fetch_board(board, client)
            valid = _valid_response(board["ats"], data)
        if not valid:
            failed += 1
            board["failure_count"] = int(board.get("failure_count") or 0) + 1
            board["last_failure"] = today.isoformat()
            if board["failure_count"] >= 3:
                board["status"] = "inactive"
                board["retry_after"] = (today + timedelta(days=30)).isoformat()
            continue
        board["failure_count"] = 0
        board["retry_after"] = None
        board["last_success"] = today.isoformat()
        board_eligible = False
        board_was_baselined = bool(board.get("baseline_complete"))
        rows = normalized_jobs if normalized_jobs is not None else _rows(board["ats"], data)
        for row in rows:
            if not isinstance(row, dict):
                continue
            job = row.copy() if normalized_jobs is not None else _job_from_row(board, row)
            if not job or not role_filter(str(job.get("title") or "")):
                continue
            feed = board.get("feed") if board.get("feed") in {"general", "biotech"} else "general"
            if location_filter and not location_filter(str(job.get("location") or ""), feed):
                continue
            job["feeds"] = [feed]
            # The integration layer consumes this flag when deciding which
            # URLs may enter notify.py, then omits it from user-facing output.
            job["registry_notify_eligible"] = board_was_baselined
            jobs.append(job)
            board_eligible = True
        if board_eligible:
            board["promoted_until"] = (today + timedelta(days=90)).isoformat()
        # A valid empty board is also a successful baseline. Otherwise boards
        # with zero eligible jobs would never become notification-ready.
        board["baseline_complete"] = True
    registry["cursors"]["shards"][cursor_key] = (cursor + inspected) % len(active)
    active_boards = [b for b in registry["boards"].values() if b.get("status") == "active"]
    registry["baseline_complete"] = bool(active_boards) and all(
        b.get("baseline_complete") for b in active_boards
    )
    return {
        "jobs": jobs,
        "boards_attempted": attempted,
        "boards_failed": failed,
        "requests": client.requests,
        "elapsed_seconds": round(getattr(client, "elapsed_seconds", 0.0), 3),
        "baseline_suppressed": sum(
            1 for job in jobs if not job.get("registry_notify_eligible")
        ),
        "shard": shard,
    }


def copy_for_preview(registry: dict) -> dict:
    """Public helper for CLIs that must not mutate the loaded object."""
    return deepcopy(registry)
