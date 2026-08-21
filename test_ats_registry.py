#!/usr/bin/env python3
"""Secret-free, no-network tests for the bounded ATS registry."""

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

import ats_registry as registry


class FakeClient:
    def __init__(self, responses=None, max_requests=100):
        self.responses = responses or {}
        self.requests = 0
        self.max_requests = max_requests

    @property
    def exhausted(self):
        return self.requests >= self.max_requests

    def json(self, url, *, payload=None):
        self.requests += 1
        value = self.responses.get(url)
        return value(payload) if callable(value) else value

    def text(self, url):
        self.requests += 1
        return self.responses.get(url)


class LocatorTests(unittest.TestCase):
    def test_supported_slug_urls_preserve_case(self):
        cases = {
            "https://boards.greenhouse.io/Some_Co/jobs/1": ("greenhouse", "slug", "Some_Co"),
            "https://jobs.lever.co/MixedCase/abc": ("lever", "slug", "MixedCase"),
            "https://jobs.ashbyhq.com/Acme-Inc/abc": ("ashby", "slug", "Acme-Inc"),
            "https://jobs.gem.com/AcmeAI/123": ("gem", "slug", "AcmeAI"),
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(registry.locator_from_url(url), expected)

    def test_greenhouse_embed_uses_for_parameter(self):
        url = "https://boards.greenhouse.io/embed/job_board?for=ActualSlug"
        self.assertEqual(registry.locator_from_url(url), ("greenhouse", "slug", "ActualSlug"))

    def test_workday_public_url_normalizes_only_when_site_is_present(self):
        public = "https://Acme.wd1.myworkdayjobs.com/en-US/Careers/job/Boston/Role_R1?x=1"
        self.assertEqual(
            registry.normalize_workday_cxs(public),
            "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/Careers/jobs",
        )
        self.assertIsNone(registry.normalize_workday_cxs("https://acme.wd1.myworkdayjobs.com/"))

    def test_key_is_casefolded_but_original_slug_is_stored(self):
        data = registry.empty_registry()
        self.assertTrue(registry.add_candidate(
            data, name="Acme", ats="Ashby", locator="AcmeAI", source="test"
        ))
        self.assertFalse(registry.add_candidate(
            data, name="Acme", ats="ashby", locator="acmeai", source="second"
        ))
        board = data["boards"]["ashby:acmeai"]
        self.assertEqual(board["slug"], "AcmeAI")
        self.assertEqual(board["sources"], ["second", "test"])


class SchemaAndSeedTests(unittest.TestCase):
    def test_bounded_client_claim_enforces_request_cap(self):
        client = registry.BoundedClient(max_requests=1, max_seconds=60)
        self.assertTrue(client.claim())
        self.assertFalse(client.claim())
        self.assertEqual(client.requests, 1)

    def test_round_trip_adds_missing_cursors(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "registry.json"
            path.write_text('{"boards": {}}')
            data = registry.load_registry(path)
            self.assertEqual(set(data["cursors"]["shards"]), {str(i) for i in range(7)})
            self.assertEqual(data["cursors"]["discovery_prefix"], 0)
            self.assertEqual(data["cursors"]["yc_companies"], 0)
            registry.save_registry(data, path)
            self.assertEqual(json.loads(path.read_text())["schema_version"], 1)

    def test_yc_company_homepage_resolves_to_board_locator(self):
        data = registry.empty_registry()
        yc = [{"name": "Acme", "website": "https://acme.example"}]
        responses = {
            registry.YC_HIRING_URL: yc,
            registry.SIMPLIFY_LISTINGS_URL: [],
            "https://acme.example": '<a href="https://jobs.lever.co/Acme">Jobs</a>',
        }
        result = registry.seed_registry(
            data, FakeClient(responses), limit=10, include_wayback=False
        )
        self.assertEqual(result["added"], 1)
        self.assertEqual(data["boards"]["lever:acme"]["name"], "Acme")

    def test_wayback_queries_rotate_by_slug_prefix(self):
        data = registry.empty_registry()
        responses = {registry.YC_HIRING_URL: [], registry.SIMPLIFY_LISTINGS_URL: []}
        for host in registry.DISCOVERY_HOSTS:
            responses[registry._cdx_url(host, 25, "a")] = [["original"]]
        registry.seed_registry(data, FakeClient(responses), limit=10)
        self.assertEqual(data["cursors"]["discovery_prefix"], 1)

    def test_wayback_prefix_does_not_advance_when_host_set_is_cut_short(self):
        data = registry.empty_registry()
        first = registry.DISCOVERY_HOSTS[0]
        responses = {
            registry.YC_HIRING_URL: [],
            registry.SIMPLIFY_LISTINGS_URL: [],
            registry._cdx_url(first, 25, "a"): [
                ["original"], [f"https://{first}/OnlyCandidate"]
            ],
        }
        registry.seed_registry(data, FakeClient(responses), limit=1)
        self.assertEqual(data["cursors"]["discovery_prefix"], 0)

    def test_json_candidate_walk_associates_company_name(self):
        data = [{"company_name": "A Co", "apply_url": "https://jobs.lever.co/ACo/123"}]
        self.assertEqual(list(registry.candidates_from_json(data, "fixture")), [{
            "name": "A Co", "ats": "lever", "locator": "ACo", "source": "fixture"
        }])

    def test_seed_limit_and_existing_board_skip(self):
        yc = registry.YC_HIRING_URL
        simplify = registry.SIMPLIFY_LISTINGS_URL
        client = FakeClient({
            yc: [{"name": "Old", "url": "https://jobs.ashbyhq.com/Old"},
                 {"name": "New", "url": "https://jobs.lever.co/New"}],
            simplify: [{"company": "Third", "url": "https://boards.greenhouse.io/Third"}],
        })
        data = registry.empty_registry()
        result = registry.seed_registry(
            data, client, limit=1, include_wayback=False,
            skip_keys={registry.board_key("ashby", "Old")},
        )
        self.assertEqual(result["added"], 1)
        self.assertIn("lever:new", data["boards"])
        self.assertNotIn("ashby:old", data["boards"])
        self.assertEqual(client.requests, 1)  # limit stops before Simplify


class VerifyTests(unittest.TestCase):
    def _candidate(self, data, slug="Acme"):
        registry.add_candidate(data, name=slug, ats="greenhouse", locator=slug, source="test")
        return data["boards"][f"greenhouse:{slug.casefold()}"]

    def test_success_activates_candidate(self):
        data = registry.empty_registry()
        board = self._candidate(data)
        client = FakeClient({registry._endpoint(board): {"jobs": []}})
        result = registry.verify_registry(data, client, limit=1, today=date(2026, 8, 5))
        self.assertEqual(result["activated"], 1)
        self.assertEqual(board["status"], "active")
        self.assertEqual(board["failure_count"], 0)

    def test_third_failure_creates_30_day_cooldown(self):
        data = registry.empty_registry()
        board = self._candidate(data)
        board["failure_count"] = 2
        result = registry.verify_registry(data, FakeClient(), limit=1, today=date(2026, 8, 5))
        self.assertEqual(result["failed"], 1)
        self.assertEqual(board["status"], "inactive")
        self.assertEqual(board["retry_after"], "2026-09-04")

    def test_cursor_advances_past_active_boards(self):
        data = registry.empty_registry()
        first = self._candidate(data, "A")
        first["status"] = "active"
        second = self._candidate(data, "B")
        client = FakeClient({registry._endpoint(second): {"jobs": []}})
        registry.verify_registry(data, client, limit=1)
        self.assertEqual(second["status"], "active")
        self.assertEqual(data["cursors"]["candidates"], 0)  # inspected both, wrapped


class ScrapeTests(unittest.TestCase):
    def test_role_filter_and_notification_free_first_baseline(self):
        data = registry.empty_registry()
        registry.add_candidate(data, name="Acme", ats="greenhouse", locator="Acme", source="test")
        board = data["boards"]["greenhouse:acme"]
        board["status"] = "active"
        response = {"jobs": [
            {"title": "Machine Learning Engineer", "location": {"name": "New York, NY"},
             "absolute_url": "https://example/1", "updated_at": "2026-08-05"},
            {"title": "Account Executive", "location": {"name": "New York, NY"},
             "absolute_url": "https://example/2"},
        ]}
        client = FakeClient({registry._endpoint(board): response})
        result = registry.scrape_registry(
            data, client, role_filter=lambda title: "Machine Learning" in title,
            shard=registry.stable_shard("greenhouse:acme"), today=date(2026, 8, 5),
        )
        self.assertEqual([j["title"] for j in result["jobs"]], ["Machine Learning Engineer"])
        self.assertFalse(result["jobs"][0]["registry_notify_eligible"])
        self.assertEqual(result["baseline_suppressed"], 1)
        self.assertTrue(data["baseline_complete"])
        self.assertTrue(board["baseline_complete"])
        self.assertEqual(board["promoted_until"], "2026-11-03")

    def test_date_filter_drops_stale_rows(self):
        data = registry.empty_registry()
        registry.add_candidate(data, name="Acme", ats="greenhouse", locator="Acme", source="test")
        board = data["boards"]["greenhouse:acme"]
        board["status"] = "active"
        response = {"jobs": [
            {"title": "ML Engineer", "location": {"name": "Remote"},
             "absolute_url": "https://example/new", "updated_at": "2026-08-05"},
            {"title": "ML Engineer (old req)", "location": {"name": "Remote"},
             "absolute_url": "https://example/old", "updated_at": "2024-09-04"},
        ]}
        client = FakeClient({registry._endpoint(board): response})
        result = registry.scrape_registry(
            data, client, role_filter=lambda _title: True,
            date_filter=lambda date_posted: not date_posted.startswith("2024"),
            shard=registry.stable_shard("greenhouse:acme"), today=date(2026, 8, 5),
        )
        self.assertEqual([j["url"] for j in result["jobs"]], ["https://example/new"])

    def test_second_run_allows_notifications(self):
        data = registry.empty_registry()
        registry.add_candidate(data, name="Acme", ats="lever", locator="Acme", source="test")
        board = data["boards"]["lever:acme"]
        board["status"] = "active"
        board["baseline_complete"] = True
        client = FakeClient({registry._endpoint(board): [{
            "text": "ML Engineer", "categories": {"location": "Remote"},
            "hostedUrl": "https://jobs.lever.co/Acme/1",
        }]})
        result = registry.scrape_registry(
            data, client, role_filter=lambda _title: True,
            shard=registry.stable_shard("lever:acme"),
        )
        self.assertTrue(result["jobs"][0]["registry_notify_eligible"])
        self.assertEqual(result["baseline_suppressed"], 0)

    def test_late_board_gets_its_own_silent_baseline(self):
        data = registry.empty_registry()
        data["baseline_complete"] = True
        registry.add_candidate(data, name="Late", ats="ashby", locator="Late", source="test")
        board = data["boards"]["ashby:late"]
        board["status"] = "active"
        response = {"jobs": [{"title": "ML Engineer", "location": "Remote", "jobUrl": "u"}]}
        result = registry.scrape_registry(
            data, FakeClient({registry._endpoint(board): response}),
            role_filter=lambda _title: True,
            shard=registry.stable_shard("ashby:late"),
        )
        self.assertFalse(result["jobs"][0]["registry_notify_eligible"])

    def test_third_active_scrape_failure_inactivates_board(self):
        data = registry.empty_registry()
        registry.add_candidate(data, name="Broken", ats="lever", locator="Broken", source="test")
        board = data["boards"]["lever:broken"]
        board.update(status="active", failure_count=2)
        registry.scrape_registry(
            data, FakeClient(), role_filter=lambda _title: True,
            shard=registry.stable_shard("lever:broken"), today=date(2026, 8, 5),
        )
        self.assertEqual(board["status"], "inactive")
        self.assertEqual(board["retry_after"], "2026-09-04")

    def test_workday_hook_preserves_existing_adapter_results(self):
        data = registry.empty_registry()
        cxs = "https://acme.wd1.myworkdayjobs.com/wday/cxs/acme/Careers/jobs"
        registry.add_candidate(data, name="Acme", ats="workday", locator=cxs, source="test")
        key = registry.board_key("workday", cxs)
        data["boards"][key]["status"] = "active"
        seen = []

        def existing_adapter(entry):
            seen.append(entry)
            return [{"company": "Acme", "title": "ML Engineer", "location": "Remote",
                     "url": "https://acme/job/1", "date_posted": "", "ats": "Workday"}]

        result = registry.scrape_registry(
            data, FakeClient(), role_filter=lambda _title: True,
            workday_fetcher=existing_adapter, shard=registry.stable_shard(key),
        )
        self.assertEqual(result["jobs"][0]["url"], "https://acme/job/1")
        self.assertEqual(seen[0]["url"], cxs)
        self.assertEqual(seen[0]["fallback_location"], "")

    def test_stable_sharding(self):
        key = "ashby:example"
        self.assertEqual(registry.stable_shard(key), registry.stable_shard(key))
        self.assertIn(registry.stable_shard(key), range(7))


if __name__ == "__main__":
    unittest.main()
