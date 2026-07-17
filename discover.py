#!/usr/bin/env python3
"""
discover.py — find biotech/techbio startups and resolve their job boards,
with no manual homepage entry.

Modes:

  python discover.py --portfolios [--limit N] [--write]
      Pull the SOSV/IndieBio portfolio (bio trend/category companies) from
      SOSV's public WordPress REST API and Y Combinator's healthcare list
      (small + actively-hiring + target-hub only) — names + homepages, fully
      automatic — plus a small built-in list of ML-native shops not in those
      portfolios. Resolve each one's careers page + ATS. Unresolved companies
      go on a 30-day cooldown (discovered_todo.json) so repeat runs stay fast.
      --limit N   cap how many companies to resolve (default 60; 0 = all).
      --write     append resolved companies to discovered_companies.json, which
                  scrape_jobs.scrape_curated_biotechs() loads automatically — so
                  the daily sweep picks them up with no copy/paste at all.

  python discover.py <homepage-url> [...]        # resolve specific homepages
  python discover.py --file homepages.txt        # one homepage URL per line

Without --write it prints ready-to-paste CURATED_BIOTECHS lines to stdout;
unresolved / non-dispatchable ones print as `# TODO manual`. Already-tracked
companies (CURATED_BIOTECHS + Genentech + discovered_companies.json) are skipped.

Stdlib only. Best-effort: it cannot see JS-rendered careers pages (the ATS
detection still works when a company embeds Greenhouse/Ashby/Lever).
"""
import html as _html
import json
import os
import re
import sys
import urllib.parse
from datetime import date
from html.parser import HTMLParser

import scrape_jobs  # reuse HEADERS, fetch(), location gates, the tracked list

fetch = scrape_jobs.fetch
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DISCOVERED_PATH = os.path.join(SCRIPT_DIR, "discovered_companies.json")

# Attempted-but-unresolved companies, keyed by lowercased name, with a
# last-tried date. Skipped for TODO_RETRY_DAYS then retried — a company with
# no careers page today may add one later, so the skip expires rather than
# blinding discovery permanently.
TODO_PATH = os.path.join(SCRIPT_DIR, "discovered_todo.json")
TODO_RETRY_DAYS = 30

# --- Automatic source: SOSV / IndieBio portfolio via its WordPress REST API ---
SOSV_API = "https://sosv.com/wp-json/wp/v2/company"
SOSV_BIO_TRENDS = {2542, 2543}   # "Human Health", "Therapeutics" trend term IDs
SOSV_BIO_CATEGORIES = {1070, 1132}  # bio category term IDs (broader than trends)

# --- Automatic source: Y Combinator healthcare companies (static JSON) -------
YC_HEALTHCARE_API = "https://yc-oss.github.io/api/industries/healthcare.json"
# Non-technical subindustries to skip. NOTE: the API prefixes every
# subindustry with "Healthcare -> " (verified 2026-07-16) — the bare names
# would silently match nothing.
YC_EXCLUDED_SUBINDUSTRIES = {
    "Healthcare -> Healthcare Services",
    "Healthcare -> Consumer Health and Wellness",
}

# Small built-in supplement: ML-native drug-discovery shops not in the SOSV
# portfolio. Not "pasting" — a maintained default so these get resolved too.
EXTRA_HOMEPAGES = [
    ("Schrödinger",          "https://www.schrodinger.com"),
    ("Genesis Molecular AI", "https://genesis.ml"),
    ("Iambic Therapeutics",  "https://www.iambic.ai"),
    ("Terray Therapeutics",  "https://www.terraytx.com"),
    ("Enveda Biosciences",   "https://www.enveda.com"),
    ("Recursion",            "https://www.recursion.com"),
    ("EvolutionaryScale",    "https://www.evolutionaryscale.ai"),
    ("Cradle Bio",           "https://www.cradle.bio"),
    ("Noetik",               "https://www.noetik.ai"),
    ("Cartography Biosciences", "https://www.cartographybio.com"),
]

# --- ATS URL patterns --------------------------------------------------------
# Greenhouse's embed URL carries the real slug in ?for=<slug>, NOT the path —
# check it first so we don't capture the literal "embed" as the slug.
_GH_EMBED = re.compile(r'greenhouse\.io/embed/job_board(?:/js)?\?[^"\'<> ]*?for=([A-Za-z0-9_-]+)', re.IGNORECASE)
ATS_PATTERNS = [
    ("greenhouse", re.compile(r'(?:boards|job-boards)\.greenhouse\.io/(?!embed[/?])([A-Za-z0-9_-]+)')),
    ("lever",      re.compile(r'jobs\.lever\.co/([A-Za-z0-9_-]+)')),
    ("ashby",      re.compile(r'jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)')),
    ("workable",   re.compile(r'apply\.workable\.com/([A-Za-z0-9_-]+)')),
]
DISPATCHABLE_SLUG_ATS = {"greenhouse", "lever", "ashby"}

CAREERS_HREF = re.compile(r'/careers?|/jobs|/join|/work-with-us|/we.?re-hiring', re.IGNORECASE)
CAREERS_TEXT = re.compile(r'careers?|jobs|join us|we.?re hiring|open (?:roles|positions)', re.IGNORECASE)


class _AnchorParser(HTMLParser):
    """Collect every (text, href) anchor — including header/footer nav, where
    careers links usually live."""
    def __init__(self):
        super().__init__()
        self.anchors: list = []
        self._href = None
        self._text: list = []
        self._open = False

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href")
            self._text = []
            self._open = True

    def handle_data(self, data):
        if self._open:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._open:
            text = " ".join("".join(self._text).split())
            self.anchors.append((text, self._href or ""))
            self._open = False
            self._href = None


def _anchors(html: str) -> list:
    p = _AnchorParser()
    try:
        p.feed(html)
    except Exception:
        pass
    return p.anchors


def _detect_ats(text: str):
    """Return (ats_type, slug) for the first known ATS URL in text, else (None, None)."""
    text = text or ""
    m = _GH_EMBED.search(text)   # greenhouse embed carries the real slug in ?for=
    if m:
        return "greenhouse", m.group(1)
    for ats, pat in ATS_PATTERNS:
        m = pat.search(text)
        if m:
            return ats, m.group(1)
    return None, None


_STATE_FULL = {"NY": "new york", "MA": "massachusetts", "CA": "california",
               "WA": "washington", "NC": "north carolina"}


def _comma_form(low: str, city: str, state: str) -> bool:
    return (f"{city}, {state.lower()}" in low
            or f"{city}, {_STATE_FULL[state]}" in low)


def _guess_location(html: str) -> str:
    """Best-effort HQ city from a Bay-Area or biotech-hub mention on the
    homepage (reuses the scraper's city lists). On free homepage text,
    ambiguous city names (Cambridge, Dublin, Durham…) only count in their
    contiguous "city, st" / "city, state" form — the gate's word-boundary
    state confirmation is too loose across a whole page (CSS ids like "#ma"
    and phrases like "mass spectrometry" false-confirm; a Cambridge-UK
    company must not resolve to Cambridge, MA). Empty if none — the caller
    then flags the company for manual location rather than emitting a
    location-less custom entry that the is_target_location() gate would
    always drop."""
    low = (html or "").lower()
    for city in scrape_jobs.BAY_AREA_LOCATIONS:
        if city not in low:
            continue
        if city in scrape_jobs._BAY_AMBIGUOUS and not _comma_form(low, city, "CA"):
            continue
        return f"{city.title()}, CA"
    for tok, state in scrape_jobs.US_BIOTECH_HUBS.items():
        if tok == "ny office" or tok not in low:
            continue
        if tok in scrape_jobs._HUB_AMBIGUOUS and not _comma_form(low, tok, state):
            continue
        return f"{tok.title()}, {state}"
    return ""


def resolve(name: str, homepage: str) -> dict:
    """Resolve how to monitor one company: {'status':'ok', ...} or {'status':'todo','reason':..}."""
    html = fetch(homepage)
    if not html:
        return {"status": "todo", "reason": "homepage unreachable"}

    ats, slug = _detect_ats(html)       # ATS linked directly on the homepage?
    careers_url = None
    if not ats:
        for text, href in _anchors(html):
            if not href:
                continue
            if CAREERS_HREF.search(href) or CAREERS_TEXT.search(text):
                careers_url = urllib.parse.urljoin(homepage, href)
                a2, s2 = _detect_ats(careers_url)
                if a2:
                    ats, slug = a2, s2
                break
        if not ats and careers_url:     # one hop deeper: embedded board on the careers page
            ats, slug = _detect_ats(fetch(careers_url))

    if ats in DISPATCHABLE_SLUG_ATS:
        return {"status": "ok", "ats": ats, "slug": slug, "location": _guess_location(html)}
    if ats == "workable":
        return {"status": "todo", "reason": f"workable/{slug} — ATS not dispatchable"}
    if careers_url:
        loc = _guess_location(html)
        if not loc:
            return {"status": "todo", "reason": f"custom {careers_url} — needs location"}
        return {"status": "ok", "ats": "custom", "careers_url": careers_url, "location": loc}
    return {"status": "todo", "reason": "no careers page or ATS found"}


def _entry_dict(name: str, r: dict) -> dict:
    loc = r.get("location") or "TODO, CA"
    if r["ats"] == "custom":
        return {"name": name, "ats": "custom", "careers_url": r["careers_url"], "fallback_location": loc}
    return {"name": name, "ats": r["ats"], "slug": r["slug"], "fallback_location": loc}


def _print_entry(entry: dict):
    if entry["ats"] == "custom":
        print(f'    {{"name": {entry["name"]!r}, "ats": "custom", '
              f'"careers_url": {entry["careers_url"]!r}, "fallback_location": {entry["fallback_location"]!r}}},')
    else:
        print(f'    {{"name": {entry["name"]!r}, "ats": {entry["ats"]!r}, '
              f'"slug": {entry["slug"]!r}, "fallback_location": {entry["fallback_location"]!r}}},')


def sosv_bio_companies(max_pages: int = 12) -> list:
    """Pull SOSV/IndieBio portfolio companies tagged Human Health / Therapeutics
    from the public WordPress REST API. Returns [(name, homepage)]."""
    out: list = []
    page = 1
    while page <= max_pages:
        raw = fetch(f"{SOSV_API}?per_page=100&page={page}&_fields=title,acf,tx_trend,tx_category")
        if not raw:
            break
        try:
            arr = json.loads(raw)
        except json.JSONDecodeError:
            break
        if not isinstance(arr, list) or not arr:
            break
        for c in arr:
            if (SOSV_BIO_TRENDS & set(c.get("tx_trend") or [])
                    or SOSV_BIO_CATEGORIES & set(c.get("tx_category") or [])):
                name = _html.unescape((c.get("title") or {}).get("rendered", "")).strip()
                site = ((c.get("acf") or {}).get("website") or "").strip()
                if name and site:
                    out.append((name, site))
        if len(arr) < 100:
            break
        page += 1
    print(f"# sosv/indiebio API: {len(out)} bio companies with homepage", file=sys.stderr)
    return out


def yc_bio_companies() -> list:
    """Y Combinator healthcare companies from the yc-oss static JSON mirror,
    kept to the same tight small+hiring target as the SOSV pull: Active,
    1–50 people, actively hiring, technical subindustry, located in a target
    hub (Bay / US biotech hubs / US-remote). Returns [(name, homepage)].
    isHiring/team_size are YC self-reported and can be stale."""
    raw = fetch(YC_HEALTHCARE_API)
    if not raw:
        return []
    try:
        arr = json.loads(raw)
    except json.JSONDecodeError:
        return []
    out: list = []
    for c in arr if isinstance(arr, list) else []:
        team = c.get("team_size")
        if (c.get("status") == "Active"
                and isinstance(team, int) and 1 <= team <= 50
                and c.get("isHiring")
                and (c.get("website") or "").strip()
                and (c.get("subindustry") or "") not in YC_EXCLUDED_SUBINDUSTRIES
                and scrape_jobs.is_target_location(c.get("all_locations") or "")):
            out.append((c.get("name", "").strip(), c["website"].strip()))
    print(f"# yc healthcare API: {len(out)} small hiring bio companies", file=sys.stderr)
    return out


def _name_from_url(url: str) -> str:
    host = urllib.parse.urlsplit(url if "//" in url else "//" + url).netloc.lower().replace("www.", "")
    return (host.split(".")[0] or url).replace("-", " ").title()


def _tracked_names() -> set:
    names = {e["name"].strip().lower() for e in scrape_jobs.CURATED_BIOTECHS}
    names.add("genentech")                       # handled by scrape_jobs.scrape_genentech()
    for e in _load_discovered():                 # already-discovered on a prior run
        names.add(e.get("name", "").strip().lower())
    return names


def _load_discovered() -> list:
    try:
        with open(DISCOVERED_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, list) else data.get("companies", [])
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _load_todo() -> dict:
    try:
        with open(TODO_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _in_cooldown(todo_entry, today: date) -> bool:
    if not isinstance(todo_entry, dict):
        return False
    try:
        last = date.fromisoformat(todo_entry.get("last_tried", ""))
    except (TypeError, ValueError):
        return False
    return (today - last).days < TODO_RETRY_DAYS


def _int_flag(argv: list, flag: str, default: int) -> int:
    if flag in argv:
        try:
            return int(argv[argv.index(flag) + 1])
        except (IndexError, ValueError):
            pass
    return default


def main(argv: list) -> int:
    write = "--write" in argv
    if "--portfolios" in argv:
        # Reliable ML-native shops first, so --limit never cuts them off; then
        # the auto-pulled YC healthcare and SOSV/IndieBio portfolios (larger,
        # lower hit-rate tail).
        pairs = EXTRA_HOMEPAGES + yc_bio_companies() + sosv_bio_companies()
        limit = _int_flag(argv, "--limit", 60)
        if limit and len(pairs) > limit:
            print(f"# resolving first {limit} of {len(pairs)} (use --limit 0 for all)", file=sys.stderr)
            pairs = pairs[:limit]
    elif "--file" in argv:
        with open(argv[argv.index("--file") + 1]) as f:
            urls = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
        pairs = [(_name_from_url(u), u) for u in urls]
    else:
        urls = [a for a in argv[1:] if not a.startswith("-")]
        if not urls:
            print(__doc__)
            return 1
        pairs = [(_name_from_url(u), u) for u in urls]

    tracked = _tracked_names()
    todo_cache = _load_todo()
    today = date.today()
    print("# --- discover.py results ---")
    new_entries: list = []
    n_ok = n_todo = n_skip = n_cooldown = 0
    seen: set = set()
    for i, (name, homepage) in enumerate(pairs, 1):
        key = name.strip().lower()
        if not key or key in tracked or key in seen:
            n_skip += 1
            continue
        seen.add(key)
        if key in todo_cache and _in_cooldown(todo_cache[key], today):
            n_cooldown += 1
            continue
        if i % 20 == 0:
            print(f"# ...resolved {i}/{len(pairs)}", file=sys.stderr)
        r = resolve(name, homepage)
        if r["status"] == "ok":
            todo_cache.pop(key, None)
            entry = _entry_dict(name, r)
            new_entries.append(entry)
            _print_entry(entry)
            n_ok += 1
        else:
            # A transient homepage failure shouldn't blind discovery to the
            # company for TODO_RETRY_DAYS — only cache structural misses.
            if "unreachable" not in r["reason"]:
                todo_cache[key] = {"name": name, "reason": r["reason"],
                                   "last_tried": today.isoformat()}
            print(f'    # TODO manual: {name} — {r["reason"]}')
            n_todo += 1

    if write and new_entries:
        existing = _load_discovered()
        have = {e.get("name", "").strip().lower() for e in existing}
        existing.extend(e for e in new_entries if e["name"].strip().lower() not in have)
        with open(DISCOVERED_PATH, "w") as f:
            json.dump(existing, f, indent=2)
        print(f"# wrote {len(new_entries)} new to discovered_companies.json "
              f"({len(existing)} total) — the daily sweep will pick them up", file=sys.stderr)
    if write:
        with open(TODO_PATH, "w") as f:
            json.dump(todo_cache, f, indent=2)
        print(f"# todo cache: {len(todo_cache)} unresolved on {TODO_RETRY_DAYS}-day "
              f"cooldown (discovered_todo.json)", file=sys.stderr)

    print(f"# resolved={n_ok}  todo={n_todo}  skipped(tracked/dup)={n_skip}  "
          f"skipped(cooldown)={n_cooldown}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
