"""Offline retrieval-health and output regressions; all writes use a temp directory."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

import scrape_jobs as sj


class SourceHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = patch.object(sj, 'SCRIPT_DIR', self.tmp.name)
        self.root.start()
        self.addCleanup(self.root.stop)
        self.addCleanup(self.tmp.cleanup)

    def previous(self, **fields):
        with open(os.path.join(self.tmp.name, 'linkedin_jobs.json'), 'w') as f:
            json.dump(fields, f)

    def test_statuses_preserve_actual_success_only(self):
        previous = '2026-09-01T00:00:00+00:00'
        self.previous(last_success_at=previous, scraped_at='2026-09-05 00:00 UTC')
        for raw, errors, cached, expected in [(5, 0, False, 'ok'), (5, 1, False, 'partial'), (0, 1, True, 'cached'), (0, 1, False, 'error')]:
            with self.subTest(status=expected):
                def fake(*, _health):
                    _health.raw_records, _health.errors, _health.used_cache = raw, errors, cached
                    return []  # raw rows all filtered out is still a successful retrieval
                result = sj._collect_run(fake, 'linkedin_jobs')
                self.assertEqual(result.status, expected)
                self.assertEqual(result.jobs, [])
                if expected == 'ok':
                    self.assertNotEqual(result.last_success_at, previous)
                else:
                    self.assertEqual(result.last_success_at, previous)
        self.previous(scraped_at='2026-09-05 00:00 UTC')
        self.assertIsNone(sj._collect_run(lambda **kw: [], 'linkedin_jobs').last_success_at)

    def test_transport_failure_is_recorded_at_fetch(self):
        health = sj._RetrievalHealth()
        with patch.object(sj, 'urlopen', side_effect=URLError('offline')):
            self.assertEqual(sj.fetch('https://example.test', health=health), '')
        self.assertEqual(health.errors, 1)

    def test_linkedin_fallback_keeps_prior_jobs_and_marks_cached(self):
        jobs = [{'url': 'https://example.test/job', 'title': 'Software Engineer'}]
        self.previous(jobs=jobs)
        with patch.object(sj, '_linkedin_search', return_value=([], 0)):
            result = sj.collect_linkedin_recent()
        self.assertEqual(result.jobs, jobs)
        self.assertEqual(result.status, 'cached')
        self.assertIsNone(result.last_success_at)

    def test_linkedin_collects_partial_transport_failure_without_changing_wrapper(self):
        def search(*args, health=None):
            health.errors += 1
            return [], 5
        with patch.object(sj, '_linkedin_search', side_effect=search), patch.object(sj, '_enrich_linkedin_salaries'):
            result = sj.collect_linkedin_recent()
        self.assertEqual(result.status, 'partial')
        with patch.object(sj, '_linkedin_search', return_value=([], 5)), patch.object(sj, '_enrich_linkedin_salaries'):
            self.assertIsInstance(sj.scrape_linkedin_recent(), list)

    def test_jobspy_failed_retry_retains_first_page_and_marks_failure(self):
        for outcome in [RuntimeError('blocked'), None]:
            with self.subTest(outcome=outcome):
                health = sj._RetrievalHealth()
                with patch('builtins.print'), patch.object(sj, 'REQUEST_DELAY', 0):
                    fn = unittest.mock.Mock(side_effect=[list(range(50)), outcome])
                    rows = sj._jobspy_fetch_with_retry(fn, _health=health)
                self.assertEqual(len(rows), 50)
                self.assertEqual(health.errors, 1)

    def test_saver_adds_metadata_and_preserves_existing_fields(self):
        metadata = {'last_attempt_at': '2026-09-06T00:00:00+00:00', 'last_success_at': None, 'status': 'error'}
        with patch.object(sj, '_merge_into_all_jobs'), patch('notify.notify_new_jobs'):
            sj.save_linkedin_results([], run_metadata=metadata)
        with open(os.path.join(self.tmp.name, 'linkedin_jobs.json')) as f:
            payload = json.load(f)
        for key in ['jobs', 'new_jobs', 'scraped_at', 'total', 'new_count', 'filter_stats']:
            self.assertIn(key, payload)
        for key, value in metadata.items():
            self.assertEqual(payload[key], value)


if __name__ == '__main__':
    unittest.main(verbosity=2)
