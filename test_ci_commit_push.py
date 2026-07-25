#!/usr/bin/env python3
"""
Tests for ci_commit_push.py — the conflict-safe replacement for the watchers'
`git pull --rebase && git push` tail.

The headline test (test_losing_the_race_keeps_both_runs_data) is a real
end-to-end reproduction of the 2026-07-25 failure: two clones of the same repo
both scrape, the other one pushes first, and this run must still land its data
instead of dying on a rebase conflict. Run it with:

    python test_ci_commit_push.py

No network, no secrets — it builds a throwaway bare repo in a temp dir.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import ci_commit_push as ccp  # noqa: E402


def job(url, first_seen, **extra):
    return {"url": url, "title": "MLE", "company": "Acme",
            "first_seen": first_seen, **extra}


def all_jobs_doc(*jobs):
    return json.dumps({"updated_at": "2026-07-25 00:00 UTC", "jobs": list(jobs)},
                      separators=(",", ":"))


class MergeRules(unittest.TestCase):
    """Unit-level: each accumulator's union is correct and order-independent."""

    def test_all_jobs_keeps_both_sides(self):
        theirs = all_jobs_doc(job("u/1", "2026-07-25T10:00:00Z"))
        ours = all_jobs_doc(job("u/2", "2026-07-25T11:00:00Z"))
        merged = json.loads(ccp.merge_all_jobs(theirs, ours))
        self.assertEqual({j["url"] for j in merged["jobs"]}, {"u/1", "u/2"})

    def test_all_jobs_keeps_earliest_first_seen(self):
        theirs = all_jobs_doc(job("u/1", "2026-07-20T10:00:00Z"))
        ours = all_jobs_doc(job("u/1", "2026-07-25T10:00:00Z"))
        merged = json.loads(ccp.merge_all_jobs(theirs, ours))
        self.assertEqual(len(merged["jobs"]), 1)
        self.assertEqual(merged["jobs"][0]["first_seen"], "2026-07-20T10:00:00Z")

    def test_all_jobs_prefers_a_stamped_entry_over_an_unstamped_one(self):
        theirs = all_jobs_doc({"url": "u/1", "title": "MLE"})  # no first_seen
        ours = all_jobs_doc(job("u/1", "2026-07-25T10:00:00Z"))
        merged = json.loads(ccp.merge_all_jobs(theirs, ours))
        self.assertEqual(merged["jobs"][0]["first_seen"], "2026-07-25T10:00:00Z")

    def test_all_jobs_merge_is_order_independent(self):
        a = all_jobs_doc(job("u/1", "2026-07-20T10:00:00Z"), job("u/3", "2026-07-22T10:00:00Z"))
        b = all_jobs_doc(job("u/2", "2026-07-21T10:00:00Z"), job("u/1", "2026-07-25T10:00:00Z"))
        # updated_at is stamped at merge time, so compare the payload that matters
        self.assertEqual(json.loads(ccp.merge_all_jobs(a, b))["jobs"],
                         json.loads(ccp.merge_all_jobs(b, a))["jobs"])

    def test_all_jobs_sorted_newest_first(self):
        theirs = all_jobs_doc(job("u/1", "2026-07-20T10:00:00Z"))
        ours = all_jobs_doc(job("u/2", "2026-07-25T10:00:00Z"))
        merged = json.loads(ccp.merge_all_jobs(theirs, ours))
        self.assertEqual([j["url"] for j in merged["jobs"]], ["u/2", "u/1"])

    def test_all_jobs_survives_a_corrupt_side(self):
        ours = all_jobs_doc(job("u/1", "2026-07-25T10:00:00Z"))
        merged = json.loads(ccp.merge_all_jobs("{not json", ours))
        self.assertEqual([j["url"] for j in merged["jobs"]], ["u/1"])

    def test_all_jobs_drops_entries_without_a_url(self):
        merged = json.loads(ccp.merge_all_jobs(
            all_jobs_doc({"title": "no url"}), all_jobs_doc(job("u/1", "2026-07-25T10:00:00Z"))))
        self.assertEqual([j["url"] for j in merged["jobs"]], ["u/1"])

    def test_notified_unions_without_duplicates(self):
        merged = json.loads(ccp.merge_notified(
            json.dumps({"ids": ["a", "b"]}), json.dumps({"ids": ["b", "c"]})))
        self.assertEqual(merged["ids"], ["a", "b", "c"])

    def test_notified_stays_capped(self):
        theirs = json.dumps({"ids": [f"t{i}" for i in range(ccp.NOTIFIED_KEEP)]})
        ours = json.dumps({"ids": ["fresh"]})
        merged = json.loads(ccp.merge_notified(theirs, ours))
        self.assertEqual(len(merged["ids"]), ccp.NOTIFIED_KEEP)
        self.assertEqual(merged["ids"][-1], "fresh")  # newest survives the trim

    def test_jsonl_unions_and_keeps_our_line_last(self):
        theirs = '{"run_id":"1"}\n{"run_id":"2"}\n'
        ours = '{"run_id":"1"}\n{"run_id":"3"}\n'
        self.assertEqual(ccp.merge_jsonl(theirs, ours),
                         '{"run_id":"1"}\n{"run_id":"2"}\n{"run_id":"3"}\n')

    def test_jsonl_handles_a_missing_side(self):
        self.assertEqual(ccp.merge_jsonl(None, '{"run_id":"1"}\n'), '{"run_id":"1"}\n')

    def test_scores_union_prefers_this_runs_verdict(self):
        theirs = json.dumps({"scores": {"u/1": {"score": 1}, "u/2": {"score": 2}},
                             "model": "old"})
        ours = json.dumps({"scores": {"u/1": {"score": 9}}, "model": "new"})
        merged = json.loads(ccp.merge_scores(theirs, ours))
        self.assertEqual(merged["scores"], {"u/1": {"score": 9}, "u/2": {"score": 2}})
        self.assertEqual(merged["model"], "new")


class GitRace(unittest.TestCase):
    """End-to-end against a real git repo: the regression that started this."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ccp-test-")
        self.origin = os.path.join(self.tmp, "origin.git")
        subprocess.run(["git", "init", "--bare", "-b", "main", self.origin],
                       check=True, capture_output=True)
        seed = self.clone("seed")
        self.write(seed, "all_jobs.json", all_jobs_doc(job("u/base", "2026-07-01T00:00:00Z")))
        self.write(seed, "workflow_runs.jsonl", '{"run_id":"base"}\n')
        self.write(seed, "notified.json", json.dumps({"ids": ["base"]}))
        self.write(seed, "linkedin_jobs.json", json.dumps({"total": 0}))
        self.git(seed, "add", "-A")
        self.git(seed, "commit", "-m", "seed")
        self.git(seed, "push", "origin", "main")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- helpers ----------------------------------------------------------
    def clone(self, name):
        path = os.path.join(self.tmp, name)
        subprocess.run(["git", "clone", self.origin, path], check=True, capture_output=True)
        self.git(path, "config", "user.email", "test@example.com")
        self.git(path, "config", "user.name", "test")
        return path

    def git(self, repo, *args):
        return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)

    def write(self, repo, name, text):
        with open(os.path.join(repo, name), "w") as f:
            f.write(text)

    def read_origin(self, name):
        out = subprocess.run(["git", "show", f"main:{name}"], cwd=self.origin,
                             check=True, capture_output=True, text=True)
        return out.stdout

    def run_commit_push(self, repo, paths, message):
        """Invoke ci_commit_push as the watchers do, from inside `repo`."""
        shutil.copy(os.path.join(SCRIPT_DIR, "ci_commit_push.py"), repo)
        proc = subprocess.run(
            [sys.executable, "ci_commit_push.py", "--message", message, *paths],
            cwd=repo, capture_output=True, text=True,
        )
        return proc

    # -- tests ------------------------------------------------------------
    def test_losing_the_race_keeps_both_runs_data(self):
        """
        Reproduces run 30171299344: we check out, scrape, and while we work
        another watcher pushes to the same accumulators. The old tail hit
        "CONFLICT (content): Merge conflict in all_jobs.json" and exited 1,
        binning this run's scrape. Now both runs' jobs must land.
        """
        ours = self.clone("linkedin")          # our watcher checks out...
        theirs = self.clone("localgov")        # ...and so does another

        # The other watcher finishes first and pushes.
        self.write(theirs, "all_jobs.json",
                   all_jobs_doc(job("u/localgov", "2026-07-25T19:47:00Z"),
                                job("u/base", "2026-07-01T00:00:00Z")))
        self.write(theirs, "workflow_runs.jsonl", '{"run_id":"base"}\n{"run_id":"localgov"}\n')
        self.git(theirs, "add", "-A")
        self.git(theirs, "commit", "-m", "local-gov listings")
        self.git(theirs, "push", "origin", "main")

        # Our scrape lands on the now-stale checkout.
        self.write(ours, "all_jobs.json",
                   all_jobs_doc(job("u/linkedin", "2026-07-25T19:48:00Z"),
                                job("u/base", "2026-07-01T00:00:00Z")))
        self.write(ours, "workflow_runs.jsonl", '{"run_id":"base"}\n{"run_id":"linkedin"}\n')
        self.write(ours, "linkedin_jobs.json", json.dumps({"total": 1}))

        proc = self.run_commit_push(
            ours, ["linkedin_jobs.json", "workflow_runs.jsonl", "all_jobs.json", "notified.json"],
            "chore: update linkedin listings")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

        merged = json.loads(self.read_origin("all_jobs.json"))
        self.assertEqual({j["url"] for j in merged["jobs"]},
                         {"u/base", "u/localgov", "u/linkedin"},
                         "the losing run's scrape was dropped — the original bug")
        self.assertEqual(self.read_origin("workflow_runs.jsonl"),
                         '{"run_id":"base"}\n{"run_id":"localgov"}\n{"run_id":"linkedin"}\n')
        self.assertEqual(json.loads(self.read_origin("linkedin_jobs.json"))["total"], 1)

    def test_retries_when_the_push_itself_races(self):
        """
        Tighter race: the other watcher pushes *after* we've merged and are
        mid-push. The first push is rejected; the retry must re-merge onto the
        new tip rather than give up or clobber.
        """
        ours = self.clone("linkedin")
        theirs = self.clone("localgov")
        self.write(ours, "all_jobs.json",
                   all_jobs_doc(job("u/linkedin", "2026-07-25T19:48:00Z")))

        pushed = []
        real_remote_text = ccp.remote_text

        def sneak_in_a_push(path):
            """
            Fires once, after attempt 1 has already fetched — so the tip moves
            under us between our merge and our push, which is exactly the
            window the concurrency group doesn't cover.
            """
            if not pushed:
                pushed.append(True)
                self.write(theirs, "all_jobs.json",
                           all_jobs_doc(job("u/localgov", "2026-07-25T19:47:00Z")))
                self.git(theirs, "add", "-A")
                self.git(theirs, "commit", "-m", "local-gov listings")
                self.git(theirs, "push", "origin", "main")
            return real_remote_text(path)

        cwd = os.getcwd()
        os.chdir(ours)
        try:
            ccp.SCRIPT_DIR = ours
            ccp.remote_text = sneak_in_a_push
            rc = ccp.commit_and_push(["all_jobs.json"], "chore: update linkedin listings",
                                     attempts=3, sleep=lambda _s: None)
        finally:
            os.chdir(cwd)
            ccp.SCRIPT_DIR = SCRIPT_DIR
            ccp.remote_text = real_remote_text

        self.assertTrue(pushed, "the competing push never happened — race not exercised")

        self.assertEqual(rc, 0)
        merged = json.loads(self.read_origin("all_jobs.json"))
        self.assertEqual({j["url"] for j in merged["jobs"]}, {"u/linkedin", "u/localgov"})

    def test_no_changes_is_not_an_error(self):
        """A watcher that scraped nothing new must exit 0, not fail the run."""
        ours = self.clone("linkedin")
        proc = self.run_commit_push(ours, ["all_jobs.json", "notified.json"], "chore: no-op")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("no changes to commit", proc.stdout)

    def test_untouched_files_are_left_alone(self):
        """
        A file this run never wrote must not be committed as a deletion, and a
        file it wrote but didn't list must not be swept into the commit.
        """
        ours = self.clone("linkedin")
        os.remove(os.path.join(ours, "notified.json"))       # simulate "never written"
        self.write(ours, "all_jobs.json", all_jobs_doc(job("u/new", "2026-07-25T19:48:00Z")))
        self.write(ours, "unrelated.json", json.dumps({"scratch": True}))

        proc = self.run_commit_push(ours, ["all_jobs.json", "notified.json"], "chore: update")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(json.loads(self.read_origin("notified.json"))["ids"], ["base"])
        files = subprocess.run(["git", "show", "--name-only", "--format=", "main"],
                               cwd=self.origin, check=True, capture_output=True, text=True)
        self.assertEqual(files.stdout.split(), ["all_jobs.json"])


class WorkflowsUseIt(unittest.TestCase):
    """
    Recurrence guard. The bug wasn't in one workflow — all nine ended with the
    same fragile `git pull --rebase && git push`. If a new watcher (or a
    revert) reintroduces that tail, it will silently start binning scrapes
    again, so fail the build here instead.
    """

    def workflow_files(self):
        d = os.path.join(SCRIPT_DIR, ".github", "workflows")
        return [os.path.join(d, f) for f in sorted(os.listdir(d)) if f.endswith((".yml", ".yaml"))]

    def test_no_workflow_rebases_or_pushes_by_hand(self):
        offenders = []
        for path in self.workflow_files():
            with open(path) as f:
                body = f.read()
            if "git pull --rebase" in body or "git push" in body:
                offenders.append(os.path.basename(path))
        self.assertEqual(offenders, [], "use ci_commit_push.py instead of a raw rebase+push")

    def test_every_committing_workflow_calls_the_script(self):
        for path in self.workflow_files():
            with open(path) as f:
                body = f.read()
            if "git config user." in body:  # i.e. this workflow makes a commit
                self.assertIn("ci_commit_push.py", body, os.path.basename(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
