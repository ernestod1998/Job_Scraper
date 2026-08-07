# 🤖 Job Triage Agent

An LLM agent that scores every newly-scraped role 0–100 against **your** profile and
resume — reading the actual job description where the ATS allows it — and publishes the
verdicts to `scores.json`, which powers the **★ Rank tab** and score badges in
[`triage.html`](https://ernestod1998.github.io/Job_Scraper/triage.html).

## Why this is "an agent"

The scraper finds roles by keyword; it can't judge them. The agent is the judgment
layer — the classic LLM-agent pattern, minimally:

- **Goal:** "is this role worth THIS candidate's time?"
- **Context per role:** your profile + resume + the posting + (when fetchable) the JD.
- **Loop:** once per not-yet-scored role in `all_jobs.json`.
- **Structured output:** `{score, verdict, role_family, seniority_fit, why, flags, outreach_opener}`.

The model backend is pluggable (`call_model()` in `triage_agent.py`): the loop is
identical locally and in CI — only the "call the model" line differs.

## How the pieces fit

```
scrapers (all day) ──commit──► all_jobs.json  (cumulative master, first_seen, ~14d)
                               + 3 rolling source JSONs
triage.yml (manual dispatch) ──reads master─► scores every UNSCORED role ─commits─► scores.json
triage.html (GitHub Pages) ────shows the normal browse/map views; scoring UI is paused
```

`all_jobs.json` exists because the per-source files are rolling windows —
`linkedin_jobs.json` holds only the last ~hour (verified: a single end-of-day read
missed ~485 of one day's 498 LinkedIn roles). The scrapers now merge every run's
`new_jobs` into this master so nothing is lost.

## Running locally (no API key needed)

Uses your logged-in `claude` CLI in headless mode, with **no tools** (the script does
all fetching; the model only judges).

```bash
python3 triage_agent.py --dry-run          # what would be scored
python3 triage_agent.py --limit 5          # score 5 roles
python3 triage_agent.py --no-jd --limit 20 # faster/cheaper: metadata only
```

| Flag | Effect |
|---|---|
| `--limit N` | Cap roles scored this run (default 50); prints `scored N of M` — no silent caps |
| `--no-jd` | Skip JD fetches, score on metadata only |
| `--since N` | Only roles `first_seen` in the last N days |
| `--model ID` | Model for the API path (default `claude-haiku-4-5-20251001`) |
| `--from-files` | Read the rolling snapshots instead of `all_jobs.json` |
| `--dry-run` | Report only, write nothing |

JD fetching is attempted for ATS sources (Greenhouse, Lever, Ashby, Workday, Phenom) —
it works reliably on Greenhouse-style pages; Workday is a JS shell and usually comes
back empty, which is handled gracefully. LinkedIn/Indeed block scraping and are skipped
outright. Every verdict is tagged `jd: read` or `jd: metadata-only`.

## Running in CI (manual ranking only)

Automated ranking is paused. `.github/workflows/triage.yml` runs only when manually
dispatched (**Actions → Manual Job Triage → Run workflow**). The agent, historical
`scores.json`, and dashboard scoring code are preserved, but the dashboard does not
fetch or display scores while `ENABLE_SCORING` is false.

Required repo secrets (Settings → Secrets and variables → Actions):

| Secret | Content |
|---|---|
| `ANTHROPIC_API_KEY` | API key from console.anthropic.com (pay-as-you-go, separate from a Claude subscription) |
| `CANDIDATE_PROFILE` | Your profile markdown (targets/anti-targets use the canonical role-family tokens) |
| `CANDIDATE_RESUME` | *(optional)* resume as plain text/markdown, < 48 KB |

Secrets reach the agent as env vars only — never written to disk, never committed.

**Cost:** with the default Haiku model + the prompt-cached profile prefix, a full day's
volume (~hundreds of new roles, `--limit 300`) runs very roughly **$0.50–$1.50/day**;
steady-state is whatever is genuinely new each day, and already-scored roles are never
re-billed. Levers: `--limit`, `--no-jd`, `--model`.

## Evals (`eval_triage.py`)

Golden-case evaluations: synthetic postings with known-correct outcomes, run through
the **exact production pipeline** (same prompt builders, same `parse_verdict`, same
backends). They test profile + prompt + model as one system — a profile edit, prompt
tweak, or model swap that shifts scoring fails a case *before* a manual run
publishes 300 bad verdicts.

```bash
python3 eval_triage.py                 # all 9 cases via the logged-in claude CLI
python3 eval_triage.py --only phd      # just the PhD cases
python3 eval_triage.py --runs 3        # flakiness check: per-case pass rate
```

Cases cover: hard PhD requirement sinks a dream role (regression for the June 2026
over-scoring incident), "PhD preferred"/"MS or PhD" not penalized, sweet-spot health
ML scores high, MLOps-platform counts as ml-ai, Staff-level bar scores low, anti-target
family scores low, prompt injection in JD text is ignored, metadata-only off-target
roles still judged. Evals never touch `scores.json`.

CI: `.github/workflows/evals.yml` is also manual-only so ordinary pushes never make
model calls. After changing the prompt, profile, resume, or model, dispatch it manually
(Actions → Triage Agent Evals → Run workflow). Cost: ~9 Haiku calls, pennies.

## Privacy (the repo is public)

- `candidate_profile.md`, `resume.md`/`.txt`, `shortlist.*` are **gitignored** — keep it
  that way. CI gets them via secrets.
- Published `scores.json` is sanitized by prompt: `why`/`outreach_opener` describe the
  role and general fit, never private resume specifics.
- JD text fetched from job pages is treated as **untrusted** (prompt-injection could at
  worst skew that one role's score — the model has no tools and must emit JSON).

## Rollback

Revert the commits touching `triage.html`, `scrape_jobs.py`, and the workflow files;
delete `.github/workflows/triage.yml`, `scores.json`, `all_jobs.json`; remove the
secrets. Do **not** `git clean` — it would wipe your untracked resume/profile.

## Graduating the agent (next steps when you want them)

- Swap `call_model()`'s CLI path for the **Claude Agent SDK** (`pip install
  claude-agent-sdk`) to make it a long-running, multi-tool agent.
- Switch the API path to the **Message Batches API** (~50% cheaper) if daily volume
  makes cost matter.
- Surface `role_family` as a dashboard filter alongside the regex-based Role filter.
