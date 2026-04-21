# Deep Dive: Biotech MLE Job Scraper — Session Changes

**Generated**: 2026-04-21  
**Files**: `scrape_jobs.py`, `.github/workflows/scrape_jobs.yml`, `.gitignore`

---

## Overview

This session made four meaningful changes to the scraper:

1. Broadened the keyword filter to catch more relevant roles
2. Fixed a crash caused by unhandled `TimeoutError`
3. Added a daily 50-company limit with persistent progress tracking
4. Updated CI to commit the progress file

---

## Change 1 — Broadened Keywords (line 24)

### Before
```python
KEYWORDS = ["machine learning engineer", "ml engineer", "mle", "machine learning infra"]
```

### After
```python
KEYWORDS = [
    "machine learning engineer", "ml engineer", "mle",
    "machine learning infra", "applied scientist", "ai engineer",
    "research engineer", "data scientist", "mlops",
]
```

### Why this matters

The original list only matched a narrow set of exact title strings. Job boards are inconsistent — the same role at two companies might be "ML Engineer" vs "Research Engineer, AI" vs "Applied Scientist (NLP)". By expanding the list, the scraper catches more relevant listings without changing any logic.

### The pattern: substring matching on lowercased strings

```python
def is_mle_role(title: str) -> bool:
    return any(k in title.lower() for k in KEYWORDS)
```

This is a **linear scan** — for each job title it checks every keyword in order, stopping as soon as one matches (`any()` short-circuits). It's simple and fast enough for hundreds of job titles per run.

**Alternative**: regex with `re.search(pattern, title, re.IGNORECASE)` — more powerful (word boundaries, OR groups) but overkill here.

**When to upgrade**: If you start getting false positives (e.g. "data scientist" matching a role you don't want), switch to regex with word boundaries: `\bdata scientist\b`.

**Learn more**:
- [Python `any()` docs](https://docs.python.org/3/library/functions.html#any)
- [Short-circuit evaluation (Wikipedia)](https://en.wikipedia.org/wiki/Short-circuit_evaluation)

---

## Change 2 — Exception Handling Fix (line 43)

### Before
```python
except URLError as e:
```

### After
```python
except (URLError, TimeoutError, OSError) as e:
```

### Why it crashed

Python's exception hierarchy matters here:

```
BaseException
└── Exception
    └── OSError
        ├── TimeoutError      ← this one crashed the scraper
        └── URLError          ← this was already caught
```

`URLError` is from `urllib.error` and wraps network-level errors like DNS failures or HTTP 404s. But `TimeoutError` is a built-in Python exception (subclass of `OSError`) thrown when a socket read times out at the OS level — it's *not* a subclass of `URLError`, so it slipped through.

`OSError` is the common ancestor of both, so catching it covers all socket/IO failures. Adding `TimeoutError` explicitly makes the intent clear even though `OSError` would be sufficient alone.

### The concept: exception hierarchy

Python exceptions are a class hierarchy. `except SomeException` catches that class *and all its subclasses*. A common mistake is assuming two related exceptions share a parent — always check the docs.

**Learn more**:
- [Python built-in exception hierarchy](https://docs.python.org/3/library/exceptions.html#exception-hierarchy)
- [urllib.error docs](https://docs.python.org/3/library/urllib.error.html)

---

## Change 3 — Daily Limit + Progress Tracking (lines 308–344)

### The problem it solves

337 companies × multiple slug attempts × 0.3s delay = potentially 30+ minutes per full run. GitHub Actions has a 6-hour timeout, but burning API quota and runtime every day on all 337 companies is wasteful. The fix: process 50 per day, track where you left off, cycle back when done.

### How the progress file works

```python
# On first run: offset = 0
# On each run: read offset from file, process next 50, write offset + 50
# When offset + 50 >= len(companies): wrap back to 0
```

The progress is stored in `scrape_progress.json`:
```json
{
  "offset": 150,
  "jobs": [ ... all jobs found so far ... ]
}
```

The `offset` is an **array index** — it points to where in the `companies` list to start the next batch. This is simpler than storing company names (which could change if Wikipedia updates).

### The concept: stateful pipelines

This is a classic **checkpoint/resume** pattern — used everywhere from database migrations to ML training loops. The key insight: instead of re-running everything from scratch on failure or resumption, persist enough state to pick up where you left off.

**Why store jobs in the progress file too?**  
Because each day's batch only processes 50 companies, but `save_results()` needs *all* jobs found so far (across all runs) to produce a complete `jobs.json`. Accumulating jobs in the progress file means each run's output reflects the full rolling picture, not just today's 50.

**Trade-off**: the progress file grows as jobs accumulate. This is fine for hundreds of jobs, but if it grew to millions you'd want a database instead.

### The wrapping logic

```python
next_offset = offset + DAILY_LIMIT
if next_offset >= len(companies):
    print(f"\n🔄 Reached end of company list — resetting to start")
    next_offset = 0
```

This is a **modulo-style reset** — written explicitly instead of `next_offset % len(companies)` for clarity. When it resets, the accumulated jobs are cleared on the next cycle because Genentech jobs are always refreshed and the progress file is overwritten.

**Learn more**:
- [Checkpoint/restart pattern (Wikipedia)](https://en.wikipedia.org/wiki/Application_checkpointing)
- [Python list slicing](https://docs.python.org/3/library/stdtypes.html#sequence-types-list-tuple-range) — `companies[offset:offset + 50]` is how batching is done

---

## Change 4 — CI Commits the Progress File

### In `.github/workflows/scrape_jobs.yml`

```yaml
git add jobs.json jobs.md scrape_progress.json
```

### Why this is necessary

GitHub Actions runs in a **fresh, stateless VM** every time. Nothing persists between runs unless you explicitly commit it back to the repo. The progress file tracks where to resume — if it's not committed, every run starts from company #1 (offset = 0), defeating the whole batching system.

### The pattern: using git as a state store

This is a legitimate pattern for small automation scripts — use the repo itself as a persistent key-value store for state. The trade-off is that every run creates a commit, which pollutes the git history. Alternatives for heavier use cases:
- GitHub Actions cache (ephemeral, expires)
- A database (more infrastructure)
- GitHub Gists as a key-value store

**Learn more**:
- [GitHub Actions: persisting data between jobs](https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/storing-and-sharing-data-from-a-workflow)
- [GitHub Actions: caching dependencies](https://docs.github.com/en/actions/writing-workflows/choosing-what-your-workflow-does/caching-dependencies-to-speed-up-workflows)

---

## Concepts Summary

| Concept | Where it appears | Why it matters |
|---|---|---|
| Short-circuit evaluation | `is_mle_role()` | `any()` stops at first match — efficient for lists |
| Python exception hierarchy | `fetch()` error handling | Knowing which exceptions are subclasses of which |
| Checkpoint/resume pattern | `scrape_progress.json` | Stateful pipelines that survive crashes or partial runs |
| Git as a state store | CI workflow | Simple persistence without extra infrastructure |
| Array slicing for batching | `companies[offset:offset + DAILY_LIMIT]` | Core pattern for paginating through large lists |

---

## What to Read Next

- [Real Python: Exception Handling](https://realpython.com/python-exceptions/) — covers the hierarchy clearly
- [Real Python: Working with JSON](https://realpython.com/python-json/) — the format used for all state files
- [GitHub Actions: workflow syntax](https://docs.github.com/en/actions/writing-workflows/workflow-syntax-for-github-actions) — understanding `on`, `jobs`, `steps`
- [urllib.request tutorial](https://docs.python.org/3/howto/urllib2.html) — the stdlib HTTP layer this scraper uses