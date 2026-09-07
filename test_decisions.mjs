import test from 'node:test';
import assert from 'node:assert/strict';
import { createDecisionStore, DECIDE_KEY, LEGACY_KEY, CACHE_KEY, TRANSITION_CACHE_KEY, mergeTriage, normalizeTriage, gcDecisions, resolveDecision, reconcileAliases, writeDecisions, finishLegacyMigration } from './assets/triage/decisions.mjs';
import { dedupe } from './assets/triage/groups.mjs';
const DAY = 86400000, NOW = Date.UTC(2026,8,6);
const job = (url='https://jobs.test/a') => ({url,title:'Software Engineer',company:'Example',location:'San Francisco',_src:'LinkedIn'});
const url = job().url;
function memory(initial={}) {
  const data = new Map(Object.entries(initial).map(([k,v])=>[k,JSON.stringify(v)]));
  return { data, writes:0, getItem:k=>data.get(k)??null, setItem(k,v){this.writes++;data.set(k,v);}, removeItem:k=>data.delete(k) };
}
function serializedLocks() {
  let queue=Promise.resolve();
  return {request(name,callback){assert.equal(name,DECIDE_KEY); const result=queue.then(callback);queue=result.catch(()=>{});return result;}};
}
test('merge uses newer timestamps, retains base ties and protects cleared decisions',()=>{
  assert.deepEqual(mergeTriage(null,undefined),{});
  assert.deepEqual(mergeTriage({a:{s:'saved',t:100}},{a:{s:null,t:200},b:{s:'applied',t:1}}),{a:{s:null,t:200},b:{s:'applied',t:1}});
  for(const t of [99,100]) assert.equal(mergeTriage({a:{s:null,t:100}},{a:{s:'saved',t}}).a.s,null);
  assert.deepEqual(normalizeTriage({a:'saved',b:{s:'wrong',t:0},c:{s:null,t:42},d:{s:'saved',t:'bad'}}),{a:{s:'saved',t:0},c:{s:null,t:42}});
});
test('retention boundaries, unknown age and recovery protection',()=>{
  for (const [s,days,kept] of [[null,61,false],[null,59,true],['dismissed',31,false],['dismissed',29,true],['saved',900,true],['applied',900,true]]) {
    const input={a:{s,t:NOW-days*DAY}};
    assert.equal(!!gcDecisions(input,NOW).a,kept);
    assert.deepEqual(gcDecisions(input,NOW,new Set(['a'])),input);
    assert.deepEqual(gcDecisions(input,NOW,null),input);
  }
  for(const s of [null,'dismissed']) assert.ok(gcDecisions({a:{s,t:0}},NOW).a);
});
test('duplicate grouping is pure, newest clear wins, alias reconciliation retains original time',()=>{
  const jobs=[job(),{...job('https://jobs.test/b'),_src:'Indeed',description:'More detail'}];
  const copy=structuredClone(jobs), groups=dedupe(jobs);
  assert.deepEqual(jobs,copy); assert.equal(groups.length,1);
  const triage={[url]:{s:'saved',t:100},[jobs[1].url]:{s:null,t:200}};
  assert.equal(resolveDecision(triage,groups[0]._dupUrls).s,null);
  const reconciled=reconcileAliases(triage,groups);
  assert.deepEqual(reconciled[url],{s:null,t:200}); assert.equal(triage[url].s,'saved');
  assert.equal(resolveDecision({a:{s:'saved',t:1},b:{s:'applied',t:1}},['a','b']).s,'applied');
});
test('quota eviction preserves legacy; complete failure retains disk and exportable memory',async()=>{
  const legacy={triage:{[url]:'saved'},jobs:[job()]};
  const storage=memory({[LEGACY_KEY]:legacy,[CACHE_KEY]:{jobs:[]},[TRANSITION_CACHE_KEY]:{jobs:[]}});
  storage.setItem=()=>{const e=new Error('full');e.name='QuotaExceededError';throw e;};
  const errors=[], store=createDecisionStore({storage,now:()=>NOW,onError:e=>errors.push(e)});
  assert.equal(await store.commit((state,ts)=>{state.triage[url]={s:'applied',t:ts([url])};}),false);
  assert.ok(storage.getItem(LEGACY_KEY)); assert.equal(storage.getItem(DECIDE_KEY),null);
  assert.equal(store.snapshot().triage[url].s,'applied'); assert.equal(store.snapshot().jobs.length,1);assert.equal(errors.length,1);
  assert.equal('code' in store.snapshot(),false);
});
test('decisions-only migration retains job details and blocks resurrection past GC windows',async()=>{
  const other='https://jobs.test/other';
  const storage=memory({[LEGACY_KEY]:{triage:{[url]:'saved',[other]:'saved'},jobs:[job(),job(other)]},[DECIDE_KEY]:{v:2,triage:{[url]:{s:null,t:NOW-61*DAY},[other]:{s:'saved',t:1}},jobs:[]}});
  const original=storage.setItem.bind(storage);
  storage.setItem=(k,v)=>{if(k===DECIDE_KEY && JSON.parse(v).jobs.length){const e=new Error('quota');e.name='QuotaExceededError';throw e;}original(k,v);};
  const store=createDecisionStore({storage,now:()=>NOW});
  assert.equal(await store.commit(),true);assert.ok(storage.getItem(LEGACY_KEY));
  const reloaded=createDecisionStore({storage,now:()=>NOW});
  assert.equal(reloaded.state.triage[url].s,null); assert.equal(reloaded.state.jobs.length,2);
  storage.setItem=original;await reloaded.commit();assert.equal(storage.getItem(LEGACY_KEY),null);
  await reloaded.commit();assert.equal(reloaded.state.triage[url],undefined);
});
test('migration checks content, metadata and successful deletion, including equal-time v2 precedence',()=>{
  const intended={v:2,triage:{[url]:{s:'saved',t:1}},jobs:[job()]};
  const storage=memory({[LEGACY_KEY]:{triage:{[url]:'saved'},jobs:[job()]},[DECIDE_KEY]:{...intended,triage:{[url]:{s:'applied',t:1}}}});
  finishLegacyMigration(storage,intended);assert.ok(storage.getItem(LEGACY_KEY));
  storage.setItem(DECIDE_KEY,JSON.stringify({...intended,triage:{[url]:{s:null,t:0}}}));
  assert.equal(createDecisionStore({storage}).state.triage[url].s,null);
});
test('two upgraded tabs serialize disjoint and same-millisecond conflicting writes',async()=>{
  const storage=memory(),locks=serializedLocks();
  const a=createDecisionStore({storage,locks,now:()=>NOW}), b=createDecisionStore({storage,locks,now:()=>NOW});
  await Promise.all([a.commit((s,ts)=>{s.triage.a={s:'saved',t:ts(['a'])};}),b.commit((s,ts)=>{s.triage.b={s:'applied',t:ts(['b'])};})]);
  let disk=JSON.parse(storage.getItem(DECIDE_KEY));assert.deepEqual(Object.keys(disk.triage).sort(),['a','b']);
  await Promise.all([a.commit((s,ts)=>{s.triage.a={s:'dismissed',t:ts(['a'])};}),b.commit((s,ts)=>{s.triage.a={s:null,t:ts(['a'])};})]);
  disk=JSON.parse(storage.getItem(DECIDE_KEY));assert.equal(disk.triage.a.s,null);assert.ok(disk.triage.a.t>NOW);
});
test('metadata-only storage events and imported jobs converge without write loops, with/without locks',async()=>{
  for(const locks of [undefined,serializedLocks()]){
    const storage=memory({[DECIDE_KEY]:{v:2,triage:{[url]:{s:'saved',t:1}},jobs:[job()]}});
    const store=createDecisionStore({storage,locks,now:()=>NOW});
    const incoming={v:2,triage:{[url]:{s:'saved',t:1}},jobs:[{...job(),salary:'$150,000'}]};
    storage.setItem(DECIDE_KEY,JSON.stringify(incoming));await store.commit(undefined,{incoming});
    assert.equal(store.state.jobs[0].salary,'$150,000');
    const writes=storage.writes;await store.commit(undefined,{incoming});assert.equal(storage.writes,writes);
    const other=job('https://jobs.test/imported');
    await store.commit(undefined,{incoming:{triage:{[other.url]:{s:'applied',t:2}},jobs:[other]}});
    assert.ok(store.state.jobs.some(j=>j.url===other.url));
  }
});
test('unreadable storage still retains the requested decision for Export',async()=>{
  const storage={getItem(){throw new Error('denied');},setItem(){throw new Error('denied');},removeItem(){}};
  const store=createDecisionStore({storage,now:()=>NOW});
  await store.commit((s,ts)=>{s.triage[url]={s:'saved',t:ts([url])};s.jobs.push(job());});
  assert.equal(store.snapshot().triage[url].s,'saved');
});
