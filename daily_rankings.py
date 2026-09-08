#!/usr/bin/env python3
"""Daily matching. Only allowlisted public projections leave the worker."""
import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo
from benchmark.core import digest, prompt, validate_result
from benchmark.prepare import evidence, fetch_description, text_from_html
from benchmark.providers import APIError, POLICIES
from ranking.core import DAILY_SYSTEM, LIMITS, VERSION, DailyAdapter, projection, queue, shortlist, stamp
from ranking.storage import GitHubStore

FEEDS = ('all_jobs.json', 'jobs.json', 'linkedin_jobs.json', 'indeed_jobs.json',
         'usajobs_jobs.json', 'governmentjobs_jobs.json', 'calopps_jobs.json', 'calcareers_jobs.json')


def schedule_due(now, completed_day=None):
    local = now.astimezone(ZoneInfo('America/Los_Angeles'))
    return local.hour >= 9 and completed_day != local.date().isoformat()


def load_jobs():
    jobs = {}
    for name in FEEDS:
        if not Path(name).exists():
            continue
        for job in json.loads(Path(name).read_text()).get('jobs', []):
            url = job.get('url', '')
            if urlparse(url).scheme != 'https':
                continue
            previous = jobs.get(url, {})
            jobs[url] = {**previous, **{k: v for k, v in job.items() if v},
                         'first_seen': previous.get('first_seen') or job.get('first_seen') or job.get('date_posted')}
    return list(jobs.values())


def full_job(job):
    url = urlparse(job['url'])
    if url.scheme != 'https' or url.username or url.password or url.port not in (None, 443):
        raise ValueError('unsupported_posting_url')
    if url.hostname in {'boards.greenhouse.io', 'job-boards.greenhouse.io', 'jobs.ashbyhq.com',
                        'jobs.lever.co', 'linkedin.com', 'www.linkedin.com'}:
        description, source = fetch_description(job)
    elif job.get('description'):
        description, source = text_from_html(job['description']), 'saved_description'
    else:
        raise ValueError('full_description_unavailable')
    if len(description.strip()) < 500:
        raise ValueError('description_too_short')
    return {**job, 'id': digest(job['url'])[:16], 'evidence': evidence(description, 'JD'),
            'description_hash': digest(description), 'description_source': source}


class Runner:
    def __init__(self, store, inputs, now=None, adapters=None):
        self.store, self.inputs = store, inputs
        self.now = now or datetime.now(timezone.utc)
        self.day = self.now.astimezone(ZoneInfo('America/Los_Angeles')).date().isoformat()
        self.context = digest({'inputs': inputs, 'system': DAILY_SYSTEM, 'version': VERSION,
                               'models': {n: POLICIES[n].model for n in LIMITS}})
        self.adapters = adapters or {n: DailyAdapter(n, os.environ) for n in LIMITS}
        self.state, self.sha = store.get('daily-state.json')
        self.initial = self.state is None
        self.state = self.state or {'attempts': {}, 'days': {}}
        self.state.setdefault('seed_floor', (self.now - timedelta(days=7)).timestamp())
        self.state['days'].setdefault(self.day, {'luna': 0, 'sonnet': 0})
        self.output = {'version': 1, 'updated_at': self.now.isoformat(), 'day': self.day,
                       'context': self.context, 'scores': {}, 'limits': LIMITS}
        p = Path('ranking_results.json')
        if p.exists():
            self.output['scores'] = json.loads(p.read_text()).get('scores', {})
        for item in self.output['scores'].values():
            if item.get('context') != self.context:
                item['status'] = 'stale'

    def checkpoint(self):
        self.sha = self.store.put('daily-state.json', self.state, self.sha)

    def remaining(self, name):
        return LIMITS[name] - self.state['days'][self.day][name]

    def score(self, job, name, review=None):
        identity = digest({'job': job['evidence'], 'url': job['url'], 'context': self.context,
                           'name': name, 'review': review})
        path = 'results/' + identity + '.json'
        attempt = self.state['attempts'].get(identity)
        if attempt:
            if attempt['status'] in ('dispatched', 'valid'):
                saved, _ = self.store.get(path)
                if saved:
                    return saved
            return None
        if self.remaining(name) <= 0:
            return None
        self.state['days'][self.day][name] += 1
        self.state['attempts'][identity] = {'day': self.day, 'status': 'dispatched', 'provider': name,
            'url': job['url'], 'description_hash': job['description_hash'], 'context': self.context}
        self.checkpoint()  # Never call a paid API unless dispatch is durable.
        adapter = self.adapters[name]
        facts = dict(self.inputs['shared_facts'])
        facts['FACT:asof'] = 'Assessment date: ' + self.day
        user = prompt(job, self.inputs['resumes'], facts)
        if review:
            user = json.dumps({'original': json.loads(user), 'review_context': review})
        try:
            raw = adapter.generate(user)
            result = validate_result(adapter.decode(raw), job, self.inputs['resumes'], facts)
            public = projection(result, job, adapter.policy.model)
            try:
                public['usage'] = adapter.usage(raw)
            except ValueError:
                public['usage'] = None
        except (APIError, ValueError):
            self.state['attempts'][identity]['status'] = 'invalid_or_unknown'
            self.checkpoint()
            print(name + ': response unavailable or invalid; no automatic retry', flush=True)
            return None
        self.store.put(path, public)
        self.state['attempts'][identity]['status'] = 'valid'
        self.checkpoint()
        print(name + ': validated and checkpointed', flush=True)
        return public

    def publish(self):
        self.output['attempts_today'] = self.state['days'][self.day]
        self.output['updated_at'] = datetime.now(timezone.utc).isoformat()
        Path('ranking_results.json').write_text(json.dumps(self.output, ensure_ascii=False, indent=2) + '\n')

    def run(self, jobs, fetcher=full_job, run_limits=None):
        run_limits = run_limits or LIMITS
        starting = dict(self.state['days'][self.day])
        # Recover results even if the worker exhausted its quota before a crash
        # prevented publishing to main. Reading a checkpoint never calls a model.
        latest = {}
        for identity, attempt in self.state['attempts'].items():
            if attempt.get('context') == self.context:
                latest[(attempt['url'], attempt['provider'])] = (identity, attempt)
        for (url, name), (identity, attempt) in latest.items():
            old = self.output['scores'].get(url, {})
            same = old.get('context') == self.context and old.get('description_hash') == attempt['description_hash']
            if same and name in old:
                continue
            if attempt['status'] not in ('valid', 'dispatched'):
                continue
            saved, _ = self.store.get('results/' + identity + '.json')
            if not saved:
                continue
            if name == 'luna':
                self.output['scores'][url] = {**(old if same else {}), 'url': url, 'status': 'valid',
                    'context': self.context, 'description_hash': attempt['description_hash'],
                    'scored_at': attempt['day'], 'luna': saved}
            elif same and 'luna' in old:
                old['sonnet'] = saved
        prepared = {}
        candidates = queue([j for j in jobs if stamp(j) >= self.state['seed_floor']], self.now, self.initial)
        candidates_by_url = {j['url']: j for j in candidates}
        for job in candidates:
            if self.remaining('luna') <= 0 or self.state['days'][self.day]['luna'] - starting['luna'] >= run_limits['luna']:
                break
            try:
                full = fetcher(job)
            except (APIError, ValueError, OSError):
                self.output['scores'].setdefault(job['url'], {'status': 'jd_unavailable', 'context': self.context})
                continue
            prepared[job['url']] = full
            luna = self.score(full, 'luna')
            if luna:
                previous = self.output['scores'].get(job['url'], {})
                same = previous.get('context') == self.context and previous.get('description_hash') == full['description_hash']
                self.output['scores'][job['url']] = {**(previous if same else {}),
                    'url': job['url'], 'context': self.context, 'description_hash': full['description_hash'],
                    'status': 'valid', 'luna': luna,
                    'scored_at': previous.get('scored_at', self.now.isoformat()) if same else self.now.isoformat()}
            else:
                self.output['scores'][job['url']] = {'status': 'invalid_or_unknown', 'context': self.context}
            self.publish()
        eligible = [v for u, v in self.output['scores'].items()
                    if u in candidates_by_url and v.get('context') == self.context
                    and v.get('status') == 'valid' and 'sonnet' not in v and not v.get('sonnet_status')]
        for item in shortlist(eligible):
            if self.remaining('sonnet') <= 0 or self.state['days'][self.day]['sonnet'] - starting['sonnet'] >= run_limits['sonnet']:
                break
            if item['url'] not in prepared:
                try:
                    prepared[item['url']] = fetcher(candidates_by_url[item['url']])
                except (APIError, ValueError, OSError):
                    continue
            if prepared[item['url']]['description_hash'] != item['description_hash']:
                item['status'] = 'stale'
                continue
            reviewed = self.score(prepared[item['url']], 'sonnet', item['luna']['requirements'])
            if reviewed:
                item['sonnet'] = reviewed
            else:
                item['sonnet_status'] = 'invalid_or_unknown'
            self.publish()
        cutoff = (self.now - timedelta(days=35)).date().isoformat()
        self.state['attempts'] = {k: v for k, v in self.state['attempts'].items() if v['day'] >= cutoff}
        self.state['days'] = {k: v for k, v in self.state['days'].items() if k >= cutoff}
        self.checkpoint()
        self.publish()


def main():
    now = datetime.now(timezone.utc)
    if os.environ.get('GITHUB_EVENT_NAME') == 'schedule' and not schedule_due(now):
        return
    inputs = json.loads(os.environ['RANKING_INPUTS'])
    store = GitHubStore(os.environ['GITHUB_REPOSITORY'], os.environ['GITHUB_TOKEN'])
    store.initialize()
    runner = Runner(store, inputs, now)
    scheduled = os.environ.get('GITHUB_EVENT_NAME') == 'schedule'
    if scheduled and not schedule_due(now, runner.state.get('completed_schedule')):
        return
    try:
        limits = {n: min(LIMITS[n], max(0, int(os.environ.get('RUN_' + n.upper()) or LIMITS[n]))) for n in LIMITS}
        runner.run(load_jobs(), run_limits=limits)
        if scheduled:
            runner.state['completed_schedule'] = runner.day
            runner.checkpoint()
    finally:
        runner.publish()


if __name__ == '__main__':
    main()
