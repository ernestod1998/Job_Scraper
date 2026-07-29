#!/usr/bin/env python3
"""
Tests for posted-date normalization in scrape_jobs.py.

The bug (2026-07-28): the watchers run on UTC GitHub Actions runners, so every
date_posted was a UTC calendar day. After 17:00 PDT that day is already
tomorrow locally, and 34 roles on the live dashboard were dated 2026-07-29 on
the evening of the 28th.

The headline test is test_choke_point_clamps_a_future_dated_job: every scraper
writes through save_jobs_output(), so normalizing there is what makes a source
we haven't written yet safe by default. Run it with:

    python test_dates.py

No network, no secrets, no dependencies (scrape_jobs' module-level imports are
all stdlib — jobspy is imported lazily inside the scrape functions).
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)

import scrape_jobs as sj  # noqa: E402

TODAY = date(2026, 7, 28)


class NormalizePostedDate(unittest.TestCase):
    def norm(self, value):
        return sj.normalize_posted_date(value, today=TODAY)

    def test_utc_timestamp_becomes_the_pacific_day(self):
        # The whole bug in one assertion: 01:00Z on the 29th is 18:00 PDT on
        # the 28th, and the dashboard must say the 28th.
        self.assertEqual(self.norm("2026-07-29T01:00:00Z"), "2026-07-28")

    def test_offset_timestamp_converts(self):
        self.assertEqual(self.norm("2026-07-29T04:00:00+04:00"), "2026-07-28")

    def test_naive_timestamp_is_assumed_utc(self):
        # Must agree with _parse_posted_at, which also assumes UTC for naive
        # values. Two different answers for one string is how the writer and
        # the parser drift apart.
        self.assertEqual(self.norm("2026-07-29T01:00:00"), "2026-07-28")

    def test_bare_future_date_is_clamped(self):
        self.assertEqual(self.norm("2026-07-29"), "2026-07-28")

    def test_bare_past_date_is_untouched(self):
        self.assertEqual(self.norm("2026-07-20"), "2026-07-20")

    def test_relative_strings_pass_through(self):
        # The dashboard's jobDateMs() understands these; rewriting them would
        # destroy information.
        for s in ("Posted Today", "Posted 9 Days Ago", "Posted 30+ Days Ago"):
            self.assertEqual(self.norm(s), s)

    def test_empty_and_non_string_inputs(self):
        for v in ("", None, "   "):
            self.assertEqual(self.norm(v), "")

    def test_unparseable_input_survives(self):
        self.assertEqual(self.norm("sometime last week"), "sometime last week")

    def test_never_returns_a_future_day(self):
        for v in ("2026-12-25", "2027-01-01T00:00:00Z", "2026-07-29"):
            self.assertLessEqual(self.norm(v), TODAY.strftime("%Y-%m-%d"))

    def test_idempotent(self):
        # save_jobs_output normalizes on top of the probe-level fixes, so every
        # value gets normalized at least twice.
        for v in ("2026-07-29", "2026-07-29T01:00:00Z", "Posted Today", ""):
            once = self.norm(v)
            self.assertEqual(self.norm(once), once, v)


class FreshnessFilterInteraction(unittest.TestCase):
    """
    Regression guard for the trap this change walked into.

    Converting probe dates to Pacific shifts a bare date back by up to a day.
    _parse_posted_at used to read bare dates as UTC midnight, so a 30-minute-old
    role read as 25.5h old and is_recent_posting() dropped it against the 24h
    FRESH_JOB_LOOKBACK. Parsing bare dates as LOCAL_TZ midnight is what keeps
    the writer and the reader agreeing.
    """

    NOW = datetime(2026, 7, 29, 1, 30, tzinfo=timezone.utc)  # 18:30 PDT on the 28th

    def test_todays_pacific_date_is_still_fresh(self):
        self.assertTrue(sj.is_recent_posting({"date_posted": "2026-07-28"}, now=self.NOW))

    def test_bare_date_parses_as_local_midnight(self):
        parsed = sj._parse_posted_at("2026-07-28", now=self.NOW)
        self.assertEqual(parsed.utcoffset(), timedelta(hours=-7))
        self.assertLess(self.NOW - parsed, sj.FRESH_JOB_LOOKBACK)

    def test_naive_timestamp_still_parses_as_utc(self):
        # The other branch of the same if/else must NOT move to Pacific — a
        # naive timestamp is an instant, not a calendar day.
        parsed = sj._parse_posted_at("2026-07-29T01:00:00", now=self.NOW)
        self.assertEqual(parsed.utcoffset(), timedelta(0))

    def test_genuinely_stale_roles_are_still_dropped(self):
        self.assertFalse(sj.is_recent_posting({"date_posted": "2026-07-20"}, now=self.NOW))


class ChokePoint(unittest.TestCase):
    """
    The important one. Every saver routes through save_jobs_output(), so this
    is what makes a source nobody has written yet safe by default.
    """

    def setUp(self):
        # save_jobs_output builds every output path from the module-level
        # SCRIPT_DIR, so os.chdir() does NOT redirect it. Without this patch the
        # test would overwrite the real linkedin_jobs.json / all_jobs.json.
        self.tmp = tempfile.mkdtemp()
        self.real_script_dir = sj.SCRIPT_DIR
        sj.SCRIPT_DIR = self.tmp
        # Don't page a developer who has Pushover configured locally.
        self.saved_env = {k: os.environ.pop(k, None)
                          for k in ("PUSHOVER_TOKEN", "PUSHOVER_USER")}

    def tearDown(self):
        sj.SCRIPT_DIR = self.real_script_dir
        for k, v in self.saved_env.items():
            if v is not None:
                os.environ[k] = v

    def save(self, jobs, basename="test_choke_point"):
        sj.save_jobs_output(
            jobs, basename=basename, title="t", subtitle="s",
            accent="#000", empty_message="none", window_label="w",
        )
        with open(os.path.join(self.tmp, f"{basename}.json")) as f:
            return json.load(f)["jobs"]

    def test_choke_point_clamps_a_future_dated_job(self):
        today = sj.local_today().strftime("%Y-%m-%d")
        future = (sj.local_today() + timedelta(days=1)).strftime("%Y-%m-%d")
        out = self.save([{
            "company": "Acme", "title": "MLE", "location": "SF",
            "url": "https://example.com/jobs/1", "date_posted": future,
            "ats": "LinkedIn",
        }])
        self.assertEqual(out[0]["date_posted"], today)

    def test_no_written_job_is_ever_future_dated(self):
        today = sj.local_today().strftime("%Y-%m-%d")
        jobs = [
            {"company": "A", "title": "MLE", "url": "https://example.com/1",
             "date_posted": "2027-01-01", "location": "SF", "ats": "LinkedIn"},
            {"company": "B", "title": "MLE", "url": "https://example.com/2",
             "date_posted": "2026-07-01", "location": "SF", "ats": "Indeed"},
            {"company": "C", "title": "MLE", "url": "https://example.com/3",
             "date_posted": "Posted Today", "location": "SF", "ats": "Workday"},
        ]
        for j in self.save(jobs):
            d = j.get("date_posted", "")
            if d and d[:4].isdigit():
                self.assertLessEqual(d, today, j["url"])

    def test_all_jobs_master_is_normalized_too(self):
        # The dashboard's Rank tab reads all_jobs.json, so the accumulator has
        # to see normalized values, not raw ones.
        future = (sj.local_today() + timedelta(days=1)).strftime("%Y-%m-%d")
        self.save([{
            "company": "Acme", "title": "MLE", "location": "SF",
            "url": "https://example.com/jobs/master", "date_posted": future,
            "ats": "LinkedIn",
        }])
        with open(os.path.join(self.tmp, "all_jobs.json")) as f:
            master = json.load(f)["jobs"]
        self.assertTrue(master)
        self.assertEqual(master[0]["date_posted"],
                         sj.local_today().strftime("%Y-%m-%d"))


class RecurrenceGuards(unittest.TestCase):
    """
    The bug wasn't one line — it was a habit of deriving calendar days in UTC.
    Fail the build if either idiom comes back, whether by revert or by someone
    copy-pasting an existing probe to add a new ATS.
    """

    FORBIDDEN = [
        ('or "")[:10]',
         'truncating an ISO timestamp yields a UTC day — use normalize_posted_date()'),
        ('tz=timezone.utc).strftime("%Y-%m-%d")',
         'derive calendar days in LOCAL_TZ, not UTC'),
    ]

    def test_scrape_jobs_has_no_utc_day_derivation(self):
        with open(os.path.join(SCRIPT_DIR, "scrape_jobs.py")) as f:
            body = f.read()
        for literal, why in self.FORBIDDEN:
            self.assertNotIn(literal, body, why)

    def test_utc_labelled_timestamps_are_untouched(self):
        # Guard the guard: these are explicitly labelled UTC and are correct.
        # If a future tightening of FORBIDDEN starts matching them, this fails.
        with open(os.path.join(SCRIPT_DIR, "scrape_jobs.py")) as f:
            body = f.read()
        self.assertIn('strftime("%Y-%m-%d %H:%M UTC")', body)
        for literal, _ in self.FORBIDDEN:
            self.assertNotIn(literal, 'strftime("%Y-%m-%d %H:%M UTC")')

    def test_triage_html_does_not_build_a_day_from_toisostring(self):
        # The same bug in the browser: toISOString() is the UTC day.
        with open(os.path.join(SCRIPT_DIR, "triage.html")) as f:
            body = f.read()
        self.assertNotIn("toISOString().slice(0, 10)", body)
        self.assertNotIn("toISOString().slice(0,10)", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
