import test from 'node:test';
import assert from 'node:assert/strict';
import { RESUMES, bestScore, parseRankings, rankingDetails } from './assets/triage/rankings.mjs';
const record = {status: 'valid', luna: {scores: Object.fromEntries(RESUMES.map(r=>[r,80])), best_resumes: ['SWE'], requirements: [{text:'<img src=x onerror=alert(1)>',importance:'required',statuses:{SWE:'matched'}}]}};
test('invalid and stale scores are never zero or ranked',()=>{
  assert.equal(bestScore(record),80);
  for(const status of ['stale','invalid_or_unknown','jd_unavailable']) assert.equal(bestScore({...record,status}),null);
  assert.equal(bestScore({status:'valid',luna:{scores:{SWE:99}}}),null);
  assert.throws(()=>parseRankings({scores:[]}));
});
test('six resumes, separate review, and escaped evidence',()=>{
  assert.equal(RESUMES.length, 6);
  const html=rankingDetails(record);
  for(const r of RESUMES) assert.ok(html.includes(r.replaceAll('_',' ')));
  assert.ok(html.includes('&lt;img'));
  assert.ok(!html.includes('<img'));
  assert.ok(html.includes('Not shortlisted'));
  assert.ok(rankingDetails({...record,sonnet:record.luna}).includes('Sonnet reviewed'));
});
test('old five-resume results are not presented as current comparisons',()=>{
  const old = structuredClone(record);
  delete old.luna.scores.Research_Software_Engineer;
  assert.equal(bestScore(old), null);
});
