import re
from datetime import datetime, timezone

from benchmark.core import RESUMES, SYSTEM, digest, scores, winners
from benchmark.providers import Adapter

VERSION = 'daily-rank-v1'
DAILY_SYSTEM = SYSTEM + '''
Extract explicit candidate qualifications and skills, not ordinary future duties.
Responsibilities count only when they explicitly demand existing experience.
Preserve degree/experience alternatives as one qualification. A listed technology
does not establish years, production ownership, or leadership. If review context is
provided, independently check it, remove unsupported qualifications and add missing
ones. Return your own complete assessments. Never treat the first model as evidence.
'''
LIMITS = {'luna': 50, 'sonnet': 5}


class DailyAdapter(Adapter):
    def body(self, user):
        body = super().body(user)
        body['instructions' if self.name == 'luna' else 'system'] = DAILY_SYSTEM
        return body


def projection(result, job, model):
    """Allowlist output. No generated explanation, resume quote or private evidence."""
    values = scores(result)
    return {'model': model, 'scores': values, 'best_resumes': winners(values),
            'requirements': [{
                'text': max((job['evidence'][i] for i in r['job_evidence_ids']), key=len),
                'importance': r['importance'], 'hard_eligibility': r['hard_eligibility'],
                'statuses': {a['resume_id']: a['status'] for a in r['assessments']},
            } for r in result['requirements']]}


def track(job):
    title = job.get('title', '')
    if re.search(r'\b(senior|sr\.?|staff|principal|director|manager|lead|head|chief|founding|security|cybersecurity|devsecops|vice president)\b', title, re.I):
        return None
    if re.search(r'forward.deployed|solutions engineer', title, re.I):
        return 'FDE'
    if re.search(r'bioinformatics|computational|bio.*(machine|data)|scientist', title, re.I) and not re.search(r'data scientist', title, re.I):
        return 'BioScience_ML'
    if re.search(r'machine learning|\bML\b|\bAI\b|artificial intelligence', title, re.I):
        return 'ML'
    if re.search(r'data scien|data analy|data engineer', title, re.I):
        return 'DS'
    if re.search(r'software|full.?stack|front.?end|back.?end', title, re.I):
        return 'SWE'
    return None


def stamp(job):
    value = job.get('first_seen') or job.get('date_posted') or ''
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).replace(tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        return 0


def queue(jobs, now, initial=False):
    groups = {r: [] for r in RESUMES}
    seen = set()
    for job in sorted(jobs, key=lambda j: (stamp(j), j.get('url', ''))):
        family = track(job)
        location = job.get('location', '')
        if not family or not re.search(r'remote|san francisco|bay area|palo alto|san jose|san mateo|san carlos|redwood|mountain view|sunnyvale|south san|oakland|berkeley|new york|\bNYC\b|menlo park|foster city', location, re.I):
            continue
        age = (now.timestamp() - stamp(job)) / 86400
        if not 0 <= age <= (7 if initial else 14):
            continue
        duplicate = tuple(re.sub(r'\W+', '', job.get(k, '').lower()) for k in ('company', 'title', 'location'))
        if duplicate in seen:
            continue
        seen.add(duplicate)
        groups[family].append(job)
    # Round-robin gives each populated track ten slots, then borrows unused slots.
    result = []
    while any(groups.values()):
        for group in groups.values():
            if group:
                result.append(group.pop(0))
    return result


def shortlist(items):
    def best(item):
        return max((x for x in item['luna']['scores'].values() if x is not None), default=-1)
    ordered = sorted(items, key=lambda i: (-best(i), i['url']))
    high = [i for i in ordered if best(i) >= 70]
    chosen = high[:3]
    for item in ordered:
        values = sorted((v for v in item['luna']['scores'].values() if v is not None), reverse=True)
        uncertain = (len(values) > 1 and values[0] - values[1] <= 5) or any(
            r['hard_eligibility'] and 'not_evidenced' in r['statuses'].values()
            for r in item['luna']['requirements'])
        if len(chosen) < 5 and item not in chosen and best(item) >= 50 and uncertain:
            chosen.append(item)
    for item in high:
        if len(chosen) < 5 and item not in chosen:
            chosen.append(item)
    return chosen
