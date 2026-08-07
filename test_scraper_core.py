#!/usr/bin/env python3
"""Secret-free regression tests for scraper filtering and retrieval policy."""

import json
import os
import re
import tempfile
import unittest
from unittest.mock import patch

import scrape_jobs as sj


def role(url="https://example.com/job/1", **overrides):
    job = {
        "company": "Acme", "title": "Machine Learning Engineer",
        "location": "San Francisco, CA", "url": url,
        "date_posted": "2026-08-05", "ats": "Test",
    }
    job.update(overrides)
    return job


class RoleAndLocationPolicy(unittest.TestCase):
    def test_seniority_veto_is_word_bounded(self):
        for prefix in ("Senior", "Sr", "Sr.", "Lead", "Manager"):
            self.assertTrue(sj.is_mle_role(f"{prefix} Machine Learning Engineer"), prefix)
        for prefix in ("Staff", "Principal", "Founding", "Director"):
            self.assertFalse(sj.is_mle_role(f"{prefix} Machine Learning Engineer"), prefix)
        self.assertTrue(sj.is_mle_role("Machine Learning Engineer, Leadership Development"))

    def test_general_nyc_and_close_nj_allowlist(self):
        accepted = (
            "New York, NY", "New York City", "Brooklyn, NY", "Queens, New York",
            "Jersey City, NJ", "Hoboken, New Jersey", "Newark, NJ",
            "Secaucus, NJ", "Weehawken, NJ", "North Bergen, NJ", "Fort Lee, NJ",
            "Jersey City", "Hoboken", "Secaucus", "Weehawken", "North Bergen",
        )
        for location in accepted:
            self.assertTrue(sj.is_watch_location(location), location)

    def test_general_rejects_broad_or_distant_ny_nj(self):
        rejected = (
            "New York metro", "New York, United States", "New Jersey",
            "Long Island, NY", "White Plains, NY", "Tarrytown, NY",
            "Princeton, NJ", "Newark, DE", "Fort Lee, VA", "Stamford, CT",
        )
        for location in rejected:
            self.assertFalse(sj.is_watch_location(location), location)

    def test_general_keeps_bay_and_us_remote(self):
        for location in ("San Francisco, CA", "Oakland, CA", "Remote - USA", "Remote"):
            self.assertTrue(sj.is_watch_location(location), location)

    def test_biotech_keeps_existing_hubs(self):
        for location in ("Boston, MA", "Seattle, WA", "Tarrytown, NY", "San Diego, CA"):
            self.assertTrue(sj.is_target_location(location), location)
            self.assertFalse(sj.is_watch_location(location), location)

    def test_filter_is_feed_aware_and_reports_stats(self):
        rows = [
            role("https://x/ok"),
            role("https://x/senior", title="Senior Machine Learning Engineer"),
            role("https://x/staff", title="Staff Machine Learning Engineer"),
            role("https://x/far", location="Boston, MA"),
            role("https://x/company", company="Jack & Jill"),
        ]
        kept, rejected, stats = sj._filter_job_observations(rows, default_feed="general")
        self.assertEqual([j["url"] for j in kept], ["https://x/ok", "https://x/senior"])
        self.assertEqual(kept[0]["feeds"], ["general"])
        self.assertEqual(stats, {"company": 1, "seniority": 1, "role": 0, "location": 1})
        self.assertEqual({r["reason"] for r in rejected}, {"company", "seniority", "location"})
        bio, _, _ = sj._filter_job_observations([rows[3]], default_feed="biotech")
        self.assertEqual(bio[0]["feeds"], ["biotech"])


class MasterPolicy(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir = sj.SCRIPT_DIR
        sj.SCRIPT_DIR = self.tmp.name

    def tearDown(self):
        sj.SCRIPT_DIR = self.old_dir
        self.tmp.cleanup()

    def read_master(self):
        with open(os.path.join(self.tmp.name, "all_jobs.json")) as f:
            return json.load(f)["jobs"]

    def test_canonical_identity_refreshes_and_preserves_first_seen(self):
        sj._merge_into_all_jobs([role("https://example.com/job/1?source=a", feeds=["general"])])
        first = self.read_master()[0]["first_seen"]
        sj._merge_into_all_jobs([role(
            "https://example.com/job/1?source=b", feeds=["biotech"],
            title="Data Scientist", salary="$100k",
        )])
        jobs = self.read_master()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["first_seen"], first)
        self.assertEqual(jobs[0]["title"], "Data Scientist")
        self.assertEqual(jobs[0]["feeds"], ["biotech", "general"])

    def test_rejection_removes_only_one_feed(self):
        sj._merge_into_all_jobs([role(feeds=["general", "biotech"])])
        sj._merge_into_all_jobs([], [{
            "identity": sj._job_identity("https://example.com/job/1"),
            "feed": "general", "reason": "location",
        }])
        self.assertEqual(self.read_master()[0]["feeds"], ["biotech"])
        sj._merge_into_all_jobs([], [{
            "identity": sj._job_identity("https://example.com/job/1"),
            "feed": "biotech", "reason": "seniority",
        }])
        self.assertEqual(self.read_master(), [])

    def test_accepted_duplicate_wins_over_same_feed_rejection(self):
        sj._merge_into_all_jobs([role(feeds=["general"])])
        first = self.read_master()[0]["first_seen"]
        sj._merge_into_all_jobs([role(feeds=["general"], salary="$120k")], [{
            "identity": sj._job_identity("https://example.com/job/1"),
            "feed": "general", "reason": "location",
        }])
        self.assertEqual(self.read_master()[0]["first_seen"], first)
        self.assertEqual(self.read_master()[0]["salary"], "$120k")


class RetrievalPolicy(unittest.TestCase):
    @staticmethod
    def card(job_id, title="Machine Learning Engineer"):
        return (
            f'<li><div data-entity-urn="urn:li:jobPosting:{job_id}">'
            f'<h3 class="base-search-card__title">{title}</h3>'
            '<h4 class="base-search-card__subtitle">Acme</h4>'
            '<span class="job-search-card__location">New York, NY</span>'
            '<time datetime="2026-08-05"></time></div></li>'
        )

    def test_linkedin_advances_by_raw_page_size_and_stops_repeat(self):
        page = "".join(self.card(str(i)) for i in range(10))
        urls = []

        def fake_fetch(url):
            urls.append(url)
            return page

        with patch.object(sj, "LINKEDIN_LOCATIONS", [("New York, NY", "1")]), \
             patch.object(sj, "fetch", side_effect=fake_fetch), \
             patch.object(sj.time, "sleep"):
            jobs, raw = sj._linkedin_search(["machine learning"], 3600)
        self.assertEqual(len(jobs), 10)
        self.assertEqual(raw, 20)  # repeated page is counted as received raw data
        self.assertEqual([re.search(r"start=(\d+)", u).group(1) for u in urls], ["0", "10"])

    def test_jobspy_retries_only_on_exactly_fifty(self):
        calls = []

        def fake(**kwargs):
            calls.append(kwargs["results_wanted"])
            return list(range(kwargs["results_wanted"]))

        self.assertEqual(len(sj._jobspy_fetch_with_retry(fake, site_name=["indeed"])), 100)
        self.assertEqual(calls, [50, 100])
        calls.clear()

        def short(**kwargs):
            calls.append(kwargs["results_wanted"])
            return list(range(49))

        self.assertEqual(len(sj._jobspy_fetch_with_retry(short)), 49)
        self.assertEqual(calls, [50])

    def test_jobspy_metro_radii(self):
        self.assertEqual(sj.JOBSPY_LOCATIONS, [("San Francisco, CA", 50), ("New York, NY", 25)])


class RefilterCommand(unittest.TestCase):
    def test_preview_is_read_only_and_write_preserves_first_seen(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "all_jobs.json")
            payload = {"updated_at": "old", "jobs": [
                role("https://x/keep", first_seen="2026-08-01T00:00:00Z"),
                role("https://x/senior", title="Senior Machine Learning Engineer",
                     first_seen="2026-08-01T00:00:00Z"),
                role("https://x/drop", title="Staff Machine Learning Engineer",
                     first_seen="2026-08-01T00:00:00Z"),
            ]}
            with open(path, "w") as f:
                json.dump(payload, f)
            with open(path) as f:
                before = f.read()
            with patch.object(sj, "SCRIPT_DIR", tmp):
                sj.refilter_existing_outputs(write=False)
                with open(path) as f:
                    self.assertEqual(f.read(), before)
                sj.refilter_existing_outputs(write=True)
            with open(path) as f:
                jobs = json.load(f)["jobs"]
            self.assertEqual([j["url"] for j in jobs], ["https://x/keep", "https://x/senior"])
            self.assertTrue(all(j["first_seen"] == "2026-08-01T00:00:00Z" for j in jobs))

    def test_master_migration_uses_biotech_source_url_provenance(self):
        job = role("https://unknown-biotech.example/job/1", location="Boston, MA")
        kept, stats = sj._refilter_master_jobs(
            [job], {sj._job_identity(job["url"])})
        self.assertEqual(stats["location"], 0)
        self.assertEqual(kept[0]["feeds"], ["biotech"])


class RegistrySaveIntegration(unittest.TestCase):
    def test_mixed_feeds_and_per_board_baseline_marker(self):
        rows = [
            role("https://registry/quiet", location="Boston, MA", ats="Greenhouse",
                 feeds=["biotech"], registry_notify_eligible=False),
            role("https://registry/loud", location="New York, NY", ats="Lever",
                 feeds=["general"], registry_notify_eligible=True),
        ]
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(sj, "SCRIPT_DIR", tmp), \
             patch("notify.notify_new_jobs") as mocked_notify:
            sj.save_jobs_output(
                rows, basename="registry_jobs", title="Registry", subtitle="Test",
                accent="#000", empty_message="Empty", window_label="test",
                default_feed="general",
            )
            with open(os.path.join(tmp, "registry_jobs.json")) as f:
                saved = json.load(f)["jobs"]
        self.assertEqual([j["feeds"] for j in saved], [["biotech"], ["general"]])
        self.assertTrue(all("registry_notify_eligible" not in j for j in saved))
        notified = mocked_notify.call_args.args[0]
        self.assertEqual([j["url"] for j in notified], ["https://registry/loud"])


if __name__ == "__main__":
    unittest.main()
