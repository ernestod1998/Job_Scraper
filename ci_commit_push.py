#!/usr/bin/env python3
"""
Conflict-safe commit + push for the watcher workflows.

Every watcher used to end its job with:

    git add <its files>
    git commit -m "..."
    git pull --rebase origin main
    git push

That rebase conflicts whenever another watcher pushes between this run's
checkout and its push — all of them touch the same shared accumulators
(all_jobs.json, notified.json, workflow_runs.jsonl). A conflict exits 1, and
the run's completed scrape is thrown away. For LinkedIn that is *permanent*
data loss: its search window is one hour (LINKEDIN_LOOKBACK_SECONDS), so the
postings from that hour are never surfaced again. Live example — run
30171299344 on 2026-07-25 scraped fine (582 insertions) and then died on
"CONFLICT (content): Merge conflict in all_jobs.json" because the local-gov
watcher had pushed 96 seconds earlier.

This script replaces that tail. Rather than rebasing text, it rebuilds the
commit on top of the current remote tip and merges the shared accumulators
*semantically*. All of them are unordered collections keyed by a stable id,
so a union is the correct merge — and, being order-independent, it gives the
same result no matter which watcher happens to push first:

    all_jobs.json        union by url, earliest first_seen wins
    notified.json        union of ids, capped like notify.NOTIFIED_KEEP
    workflow_runs.jsonl  union of lines (append-only run log)
    scores.json          union by url, this run's fresh verdict wins

Any other file has a single writer (only linkedin_watch writes
linkedin_jobs.json), so this run's version wins outright.

If the push still races — someone pushed during our merge — the whole thing
retries against the new tip. The scraped data is snapshotted in memory up
front, so no retry can lose it.

Usage:
    python ci_commit_push.py --message "chore: update X [...]" file1 file2 ...
"""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REMOTE = "origin"
BRANCH = "main"

# Mirrors notify.NOTIFIED_KEEP; imported below when notify.py is importable so
# the two can't drift.
NOTIFIED_KEEP = 600
try:
    from notify import NOTIFIED_KEEP  # noqa: F811
except Exception:
    pass


# ---------------------------------------------------------------- git helpers

def git(*args, check=True):
    """Run a git command in the repo root, echoing it for the Actions log."""
    proc = subprocess.run(
        ["git", *args], cwd=SCRIPT_DIR,
        capture_output=True, text=True,
    )
    if proc.stdout.strip():
        print(proc.stdout.rstrip())
    if proc.stderr.strip():
        print(proc.stderr.rstrip(), file=sys.stderr)
    if check and proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed ({proc.returncode})")
    return proc


def remote_text(path: str):
    """Contents of `path` at the remote tip, or None if it isn't tracked there."""
    proc = subprocess.run(
        ["git", "show", f"{REMOTE}/{BRANCH}:{path}"], cwd=SCRIPT_DIR,
        capture_output=True, text=True,
    )
    return proc.stdout if proc.returncode == 0 else None


def read_local(path: str):
    try:
        with open(os.path.join(SCRIPT_DIR, path), encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return None


def write_local(path: str, text: str):
    with open(os.path.join(SCRIPT_DIR, path), "w", encoding="utf-8") as f:
        f.write(text)


# ---------------------------------------------------------------- merge rules

def _load(text, default):
    if text is None:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def _first_seen(job: dict) -> str:
    # Sorts unstamped entries last so a stamped sighting always wins the tie.
    return job.get("first_seen") or "9999-99-99"


def merge_all_jobs(theirs: str, ours: str) -> str:
    """
    Union the cumulative master by url. first_seen is the "when did we first
    surface this" stamp the dashboard sorts on, so when both sides carry the
    same url the earlier sighting is the true one.

    No re-prune: both sides were written by scrape_jobs._merge_into_all_jobs,
    which prunes to ALL_JOBS_PRUNE_DAYS on every write, so their union is
    already inside the window.
    """
    theirs_data, ours_data = _load(theirs, {}), _load(ours, {})
    by_url = {}
    for job in list(theirs_data.get("jobs", [])) + list(ours_data.get("jobs", [])):
        url = job.get("url")
        if not url:
            continue
        prev = by_url.get(url)
        if prev is None or _first_seen(job) < _first_seen(prev):
            by_url[url] = job
    jobs = sorted(by_url.values(), key=lambda j: j.get("first_seen", ""), reverse=True)
    # Keep the later of the two stamps rather than stamping now: a run that
    # found nothing new must reproduce the remote's bytes exactly, or every
    # watcher would push an empty-but-different commit on every single run.
    stamps = [d.get("updated_at") for d in (theirs_data, ours_data) if d.get("updated_at")]
    updated_at = max(stamps) if stamps else \
        datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return json.dumps(
        {"updated_at": updated_at, "jobs": jobs},
        separators=(",", ":"),  # compact: the dashboard fetches this on every load
    )


def merge_notified(theirs: str, ours: str) -> str:
    """Union the already-notified ids so a race can't re-ping a stale role."""
    seen, ids = set(), []
    for i in list(_load(theirs, {}).get("ids", [])) + list(_load(ours, {}).get("ids", [])):
        if i not in seen:
            seen.add(i)
            ids.append(i)
    return json.dumps({"ids": ids[-NOTIFIED_KEEP:]})


def merge_jsonl(theirs: str, ours: str) -> str:
    """
    Union an append-only log line-for-line. The writer dumps with
    sort_keys=True, so the same entry always serialises to the same string and
    exact-line dedup can't merge two genuinely different runs.
    """
    seen, out = set(), []
    for text in (theirs or "", ours or ""):
        for line in text.splitlines():
            if not line.strip() or line in seen:
                continue
            seen.add(line)
            out.append(line)
    return "".join(line + "\n" for line in out)


def merge_scores(theirs: str, ours: str) -> str:
    """Union the triage verdicts by url; this run just scored, so ours wins."""
    theirs_data, ours_data = _load(theirs, {}), _load(ours, {})
    merged = {**theirs_data, **ours_data}
    merged["scores"] = {**theirs_data.get("scores", {}), **ours_data.get("scores", {})}
    return json.dumps(merged, separators=(",", ":"))


MERGERS = {
    "all_jobs.json": merge_all_jobs,
    "notified.json": merge_notified,
    "workflow_runs.jsonl": merge_jsonl,
    "scores.json": merge_scores,
}


# ---------------------------------------------------------------------- main

def commit_and_push(paths, message, attempts=5, sleep=time.sleep) -> int:
    # Snapshot what this run produced *before* touching git. Every retry
    # re-merges from this, so a lost race never costs us the scrape.
    ours = {p: read_local(p) for p in paths}

    for attempt in range(1, attempts + 1):
        git("fetch", REMOTE, BRANCH)
        # Move HEAD to the remote tip without touching the worktree (--mixed,
        # not --hard: files this run wrote but doesn't commit must survive).
        # The commit has to be a child of the tip or the push just races again.
        git("reset", "--mixed", f"{REMOTE}/{BRANCH}")

        staged = []
        for path in paths:
            merge = MERGERS.get(os.path.basename(path))
            content = merge(remote_text(path), ours[path]) if merge else ours[path]
            if content is None:
                continue  # this run never wrote it — leave the remote's copy alone
            # Keep the file's existing trailing newline (notified.json has one,
            # json.dump doesn't write one) so a whitespace-only diff never
            # becomes a commit.
            if merge and (ours[path] or "").endswith("\n") and not content.endswith("\n"):
                content += "\n"
            write_local(path, content)
            staged.append(path)

        if not staged:
            print("nothing this run produced; skipping commit")
            return 0

        git("add", "--", *staged)
        if git("diff", "--cached", "--quiet", check=False).returncode == 0:
            print("no changes to commit")
            return 0

        git("commit", "-m", message)
        push = git("push", REMOTE, f"HEAD:{BRANCH}", check=False)
        if push.returncode == 0:
            print(f"✅ pushed on attempt {attempt}/{attempts}")
            return 0

        print(f"⚠️  push rejected — another watcher pushed first "
              f"(attempt {attempt}/{attempts}); re-merging onto the new tip")
        sleep(min(3 * 2 ** (attempt - 1), 30))

    print(f"❌ could not push after {attempts} attempts", file=sys.stderr)
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--message", required=True, help="commit message")
    ap.add_argument("--attempts", type=int, default=5)
    ap.add_argument("paths", nargs="+", help="files to commit")
    args = ap.parse_args()
    return commit_and_push(args.paths, args.message, attempts=args.attempts)


if __name__ == "__main__":
    sys.exit(main())
