import test from 'node:test';
import assert from 'node:assert/strict';
import {validateFeed,createFeedLoader} from './assets/triage/feeds.mjs';
const job={url:'https://jobs.test/1',title:'Software Engineer'};
const response=data=>({ok:true,json:async()=>data});
test('feed validates envelopes, optional fields, rows and legitimate empty snapshots',()=>{
  for(const data of [{jobs:{}},{jobs:[job],new_jobs:{}},{jobs:[job],new_count:'1'},{jobs:[job],last_success_at:{}},{jobs:[{url:'javascript:alert(1)',title:'bad'}]}]) assert.throws(()=>validateFeed(data));
  const mixed=validateFeed({jobs:[job,{...job,title:5}],status:'cached',last_success_at:null});
  assert.equal(mixed.jobs.length,1);assert.equal(mixed.skipped,1);assert.equal(mixed.status,'cached');
  assert.deepEqual(validateFeed({jobs:[]}).jobs,[]);
});
test('one bad feed cannot abort good feeds; retry retains cached results and scrape status',async()=>{
  let failing=false;
  const loader=createFeedLoader([{name:'good',src:'LinkedIn'},{name:'bad',src:'Indeed'}],{fetcher:async path=>{
    if(path.startsWith('bad')) return response({jobs:[job],new_jobs:{}});
    if(failing) throw new Error('offline');
    return response({jobs:[job],status:'cached'});
  }});
  let data=await loader.load();assert.equal(data.jobs.length,1);assert.equal(data.health.get('good').scrape,'cached');assert.equal(data.health.get('bad').delivery,'error');
  failing=true;data=await loader.load();assert.equal(data.jobs.length,1);assert.equal(data.health.get('good').delivery,'error');
});
test('newest refresh wins even if fetch ignores abort',async()=>{
  let resolveOld, count=0;
  const loader=createFeedLoader([{name:'one',src:'LinkedIn'}],{fetcher:()=>++count===1?new Promise(resolve=>resolveOld=resolve):Promise.resolve(response({jobs:[{...job,title:'New'}]}))});
  const old=loader.load(),latest=await loader.load();resolveOld(response({jobs:[{...job,title:'Old'}]}));
  assert.equal(await old,null);assert.equal(latest.jobs[0].title,'New');
});
test('timeouts complete even when the transport ignores cancellation',async()=>{
  const loader=createFeedLoader([{name:'one',src:'LinkedIn'}],{fetcher:()=>new Promise(()=>{}),timeoutMs:5});
  assert.equal((await loader.load()).health.get('one').delivery,'error');
});
