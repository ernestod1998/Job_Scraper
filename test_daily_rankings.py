import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from benchmark.core import RESUMES, scores
from daily_rankings import Runner, schedule_due
from ranking.core import projection, queue, shortlist

NOW = datetime(2026, 9, 8, 17, tzinfo=timezone.utc)
INPUTS = {'resumes': {r: {'evidence': {r + ':1': 'PRIVATE_RESUME_CANARY'}} for r in RESUMES},
          'shared_facts': {}}


def job(i=0):
    return {'id': str(i), 'url': 'https://example.test/' + str(i), 'company': 'Company ' + str(i),
            'title': 'Software Engineer', 'location': 'San Francisco', 'date_posted': '2026-09-08',
            'evidence': {'JD:1': 'Python experience required'}, 'description_hash': str(i)}


def result(status='matched'):
    return {'requirements': [{'id': 'r1', 'text': 'PRIVATE_GENERATED_CANARY',
             'importance': 'required', 'hard_eligibility': True, 'job_evidence_ids': ['JD:1'],
             'assessments': [{'resume_id': r, 'status': status, 'evidence_ids': [r + ':1'],
                              'explanation': 'PRIVATE_EXPLANATION_CANARY'} for r in RESUMES]}]}


class Store:
    def __init__(self):
        self.data, self.writes = {}, []
    def get(self, path):
        return copy.deepcopy(self.data.get(path)), str(len(self.writes))
    def put(self, path, value, sha=None):
        self.data[path] = copy.deepcopy(value)
        self.writes.append(path)
        return str(len(self.writes))


class Adapter:
    policy = SimpleNamespace(model='fixture')
    def __init__(self, store):
        self.store, self.calls, self.crash, self.invalid = store, 0, False, False
    def generate(self, user):
        assert self.store.data['daily-state.json']['days']['2026-09-08']['luna'] > 0
        self.calls += 1
        if self.crash:
            raise KeyboardInterrupt()
        return result()
    def decode(self, raw):
        return {} if self.invalid else raw
    def usage(self, raw):
        return {'cost': 1}


class Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old = os.getcwd()
        os.chdir(self.temp.name)
        self.store = Store()
        self.adapter = Adapter(self.store)
        self.adapters = {'luna': self.adapter, 'sonnet': self.adapter}
    def tearDown(self):
        os.chdir(self.old)
        self.temp.cleanup()
    def runner(self, now=NOW):
        return Runner(self.store, INPUTS, now, self.adapters)
    def test_projection_has_no_private_text(self):
        out = json.dumps(projection(result(), job(), 'fixture'))
        self.assertNotIn('PRIVATE', out)
        self.assertIn('Python experience required', out)
    def test_quota_survives_rerun(self):
        r = self.runner()
        for i in range(55):
            r.score(job(i), 'luna')
        self.assertEqual(self.adapter.calls, 50)
        r = self.runner()
        self.assertIsNone(r.score(job(99), 'luna'))
        self.assertIsNotNone(r.score(job(0), 'luna'))
        self.assertEqual(self.adapter.calls, 50)
    def test_unknown_dispatch_never_replayed(self):
        self.adapter.crash = True
        with self.assertRaises(KeyboardInterrupt):
            self.runner().score(job(), 'luna')
        self.adapter.crash = False
        self.assertIsNone(self.runner().score(job(), 'luna'))
        self.assertEqual(self.adapter.calls, 1)
    def test_invalid_response_consumes_quota(self):
        self.adapter.invalid = True
        r = self.runner()
        self.assertIsNone(r.score(job(), 'luna'))
        self.assertEqual(r.remaining('luna'), 49)
        self.assertIsNone(self.runner().score(job(), 'luna'))
        self.assertEqual(self.adapter.calls, 1)
    def test_checkpoint_failure_prevents_payment(self):
        self.store.put = lambda *a: (_ for _ in ()).throw(RuntimeError('checkpoint failed'))
        with self.assertRaises(RuntimeError):
            self.runner().score(job(), 'luna')
        self.assertEqual(self.adapter.calls, 0)
    def test_missing_evidence_does_not_trigger_cap(self):
        self.assertEqual(scores(result('not_evidenced'))['ML'], 0)
        self.assertEqual(scores(result('partial'))['ML'], 50)
        self.assertEqual(scores({'requirements': []})['ML'], None)
    def test_daily_reset_and_resume_change(self):
        first = self.runner()
        first.score(job(), 'luna')
        tomorrow = self.runner(NOW + timedelta(days=1))
        self.assertEqual(tomorrow.remaining('luna'), 50)
        self.assertIsNotNone(tomorrow.score(job(), 'luna'))
        self.assertEqual(self.adapter.calls, 1)
        changed = copy.deepcopy(INPUTS)
        changed['shared_facts']['FACT:new'] = 'Changed resume facts'
        self.assertNotEqual(first.context, Runner(self.store, changed, NOW, self.adapters).context)
    def test_queue_filters_deduplicates_and_balances(self):
        jobs = [job(i) for i in range(60)]
        jobs.append({**job(80), 'title': 'Data Scientist'})
        jobs.append({**job(81), 'title': 'Senior Software Engineer'})
        jobs.append({**job(82), 'date_posted': '2026-08-01'})
        jobs.append({**job(0), 'url': 'https://example.test/duplicate'})
        out = queue(jobs, NOW, True)
        self.assertEqual(len(out), 61)
        self.assertIn('Data Scientist', [j['title'] for j in out[:5]])
        self.assertEqual(out, queue(list(reversed(jobs)), NOW, True))
    def test_end_to_end_public_output_and_sonnet_quota(self):
        r = self.runner()
        r.run([job(i) for i in range(55)], fetcher=lambda j: j)
        data = json.loads(open('ranking_results.json').read())
        self.assertEqual(data['attempts_today'], {'luna': 50, 'sonnet': 5})
        self.assertEqual(len(data['scores']), 50)
        self.assertNotIn('PRIVATE', json.dumps(self.store.data))
        self.assertNotIn('PRIVATE', json.dumps(data))
    def test_shortlist_does_not_force_low_scores(self):
        items = [{'url': str(i), 'luna': {'scores': {r: 20 for r in RESUMES}, 'requirements': []}} for i in range(8)]
        self.assertEqual(shortlist(items), [])

    def test_recover_unpublished_results_after_quota_exhausted(self):
        r = self.runner()
        for i in range(50):
            r.score(job(i), 'luna')
        self.store.data['daily-state.json']['days']['2026-09-08']['sonnet'] = 5
        r = self.runner()
        r.run([job(i) for i in range(50)], fetcher=lambda j: j)
        self.assertEqual(len(r.output['scores']), 50)
        self.assertEqual(self.adapter.calls, 50)

    def test_smoke_limits_share_daily_quota(self):
        r = self.runner()
        r.run([job(i) for i in range(10)], fetcher=lambda j: j, run_limits={'luna': 2, 'sonnet': 1})
        self.assertEqual(r.output['attempts_today'], {'luna': 2, 'sonnet': 1})
        self.assertEqual(self.runner().remaining('luna'), 48)

    def test_schedule_handles_dst_and_skips_completed_day(self):
        winter = datetime(2026, 12, 8, 16, 15, tzinfo=timezone.utc)
        self.assertFalse(schedule_due(winter))
        self.assertTrue(schedule_due(winter + timedelta(hours=1)))
        summer = datetime(2026, 9, 8, 16, 15, tzinfo=timezone.utc)
        self.assertTrue(schedule_due(summer))
        self.assertFalse(schedule_due(summer + timedelta(hours=1), '2026-09-08'))
        self.assertTrue(schedule_due(summer + timedelta(hours=1), '2026-09-07'))


if __name__ == '__main__':
    unittest.main()
