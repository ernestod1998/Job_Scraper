import { test, expect } from '@playwright/test';
const KEY='jobTriage:v2';
const makeJobs = count => Array.from({length:count}, (_,i)=>({
  url:`https://jobs.test/${i}`,title:`Software Engineer ${i}`,company:`Company ${i % 100}`,location:'San Francisco, CA',
  salary:'$130,000 - $170,000',date_posted:new Date().toLocaleDateString('en-CA'),ats:'LinkedIn',
}));
async function fixture(context, jobs=makeJobs(6), { fail=false, rankings={version:1,scores:{}} }={}) {
  const external=[];
  await context.route('**/*',async route=>{
    const url=new URL(route.request().url());
    if(url.origin!=='http://127.0.0.1:8765') {external.push(url.href);return route.abort();}
    if(url.pathname.endsWith('/ranking_results.json')) return route.fulfill({json:rankings});
    if(url.pathname.endsWith('.json')) return fail ? route.fulfill({status:503,body:'offline'}) : route.fulfill({json:{jobs:url.pathname.endsWith('/linkedin_jobs.json')?jobs:[],status:'cached',new_jobs:[]}});
    return route.continue();
  });
  await context.addInitScript(()=>{
    window.storageWrites=0;
    const set=Storage.prototype.setItem;
    Storage.prototype.setItem=function(k,v){window.storageWrites++;return set.call(this,k,v);};
  });
  return external;
}
async function loaded(page) { await expect(page.locator('#snapshot-line')).toContainText('checked'); }
for(const width of [390,1440]) test(`browse ${width}px: 6000 jobs, pagination, filters and no view writes`,async({context,page},testInfo)=>{
  await page.setViewportSize({width,height:1000});
  const external=await fixture(context,makeJobs(6000));
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto('triage.html#sync=old-code'); await loaded(page);
  await expect(page.locator('.job')).toHaveCount(50);
  await expect(page.locator('#results-heading')).toHaveText('6,000 matching roles');
  await expect(page.locator('#insights')).not.toHaveAttribute('open','');
  const writes=await page.evaluate(()=>window.storageWrites);
  await page.getByRole('button',{name:'Load 50 more'}).click();await expect(page.locator('.job')).toHaveCount(100);
  await page.getByRole('searchbox').fill('Software Engineer 5999');await expect(page.locator('.job')).toHaveCount(1);
  await page.getByRole('searchbox').fill('');await expect(page.locator('.job')).toHaveCount(50);
  await page.locator('#filter-sort button').last().click();await expect(page.locator('.job')).toHaveCount(50);
  await page.locator('#insights summary').click();
  await expect(page.locator('#kpi-total')).toHaveText('6000');
  const bucket=page.locator('.hbar[data-floor="125000"]');
  await bucket.focus();await page.keyboard.press('Enter');
  await expect(page.locator('.hbar[data-floor="125000"]')).toBeFocused();
  await expect(page.locator('#sal-min')).toHaveValue('125000');
  expect(await page.evaluate(()=>window.storageWrites)).toBe(writes);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  expect(await page.locator('.act').first().evaluate(e=>e.getBoundingClientRect().height)).toBeGreaterThanOrEqual(44);
  expect(external.filter(u=>/triage-sync|upstash|scores\.json/.test(u))).toEqual([]);
  expect(page.url()).not.toContain('#sync');expect(errors).toEqual([]);
  await page.locator('#insights summary').click();
  await page.screenshot({path:testInfo.outputPath(`dashboard-${width}.png`),fullPage:false});
});
test('dismiss, Undo, persistence, keyboard dialog and export omit old codes',async({context,page})=>{
  await fixture(context);
  await context.addInitScript(()=>{if(!localStorage.getItem('jobTriage:v2'))localStorage.setItem('jobTriage:v2',JSON.stringify({v:2,triage:{},jobs:[],code:'old-secret'}));});
  await page.goto('triage.html');await loaded(page);
  await page.locator('.job').first().getByRole('button',{name:'Dismiss'}).click();
  await expect(page.locator('.job')).toHaveCount(5);
  await page.getByRole('button',{name:'Undo',exact:true}).click();await expect(page.locator('.job')).toHaveCount(6);
  await page.locator('.job').first().getByRole('button',{name:'Save',exact:false}).click();
  await expect.poll(()=>page.evaluate(()=>Object.values(JSON.parse(localStorage.getItem('jobTriage:v2')).triage).some(d=>d.s==='saved'))).toBe(true);
  expect(await page.evaluate(()=>JSON.parse(localStorage.getItem('jobTriage:v2')).code)).toBeUndefined();
  await page.reload();await loaded(page);await expect(page.locator('.job[data-state=saved]')).toHaveCount(1);
  await page.locator('.data-tools summary').click();await page.getByRole('button',{name:'Paste fresh JSON'}).click();
  await expect(page.getByRole('textbox',{name:'Job JSON'})).toBeFocused();
  await page.getByRole('button',{name:'Merge',exact:true}).focus();await page.keyboard.press('Tab');await expect(page.getByRole('button',{name:'Close',exact:true})).toBeFocused();
  await page.keyboard.press('Escape');await expect(page.getByRole('button',{name:'Paste fresh JSON'})).toBeFocused();
  const download=page.waitForEvent('download');await page.getByRole('button',{name:'Export',exact:false}).click();
  const file=await download;const stream=await file.createReadStream();let text='';for await(const chunk of stream)text+=chunk;
  const payload=JSON.parse(text);expect(payload.code).toBeUndefined();expect(Object.values(payload.triage).some(d=>d.s==='saved')).toBe(true);
});
test('cached history renders before stalled refresh and survives every failed feed',async({context,page})=>{
  const jobs=makeJobs(1);
  await fixture(context,jobs,{fail:true});
  await context.addInitScript(jobs=>localStorage.setItem('jobTriage:v2',JSON.stringify({v:2,triage:{[jobs[0].url]:{s:'saved',t:1}},jobs})),jobs);
  await page.goto('triage.html');await expect(page.locator('.job')).toHaveCount(1);await loaded(page);
  await expect(page.locator('#snapshot-line')).toContainText('8 source(s) unavailable');
  await page.locator('.source-details summary').click();await expect(page.getByRole('button',{name:'Retry failed'})).toBeVisible();
});
test('duplicate clear and decisions coordinate across tabs, with conditional Undo',async({context,page})=>{
  const jobs=makeJobs(1);jobs.push({...jobs[0],url:'https://jobs.test/alias',ats:'Indeed',salary:'$180,000'});
  await fixture(context,jobs);await page.goto('triage.html');await loaded(page);
  const other=await context.newPage();await other.goto('triage.html');await loaded(other);
  await page.locator('.act.save').click();await expect(other.locator('.job')).toHaveAttribute('data-state','saved');
  await other.locator('.act.save').click();await expect(page.locator('.job')).toHaveAttribute('data-state','active');
  const triage=await page.evaluate(()=>JSON.parse(localStorage.getItem('jobTriage:v2')).triage);
  expect(Object.values(triage).every(d=>d.s===null)).toBe(true);expect(Object.keys(triage)).toHaveLength(2);
  await other.locator('#filter-state button').filter({hasText:'Dismissed'}).click();
  await page.locator('.act.dismiss').click();await expect(page.locator('#undo-bar')).toBeVisible();
  await expect(other.locator('.job')).toHaveAttribute('data-state','dismissed');
  await other.locator('.act.applied').click();await expect(page.locator('#undo-bar')).toBeHidden();
  await expect(page.locator('.job')).toHaveAttribute('data-state','applied');
});
test('save failure keeps in-memory decisions in Export',async({context,page})=>{
  await fixture(context);await page.goto('triage.html');await loaded(page);
  await page.evaluate(()=>{Storage.prototype.setItem=function(){throw new DOMException('full','QuotaExceededError');};});
  await page.locator('.act.save').first().click();await expect(page.locator('#save-error')).toBeVisible();
  await page.locator('.data-tools summary').click();const download=page.waitForEvent('download');await page.getByRole('button',{name:'Export',exact:false}).click();
  const file=await download,stream=await file.createReadStream();let text='';for await(const c of stream)text+=c;
  expect(Object.values(JSON.parse(text).triage).some(d=>d.s==='saved')).toBe(true);
});

test('real committed feeds load without page errors or source validation failures',async({context,page})=>{
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await context.route('**/*',route=>new URL(route.request().url()).origin==='http://127.0.0.1:8765'?route.continue():route.abort());
  await page.goto('triage.html');await loaded(page);
  await expect(page.locator('.job')).toHaveCount(50);
  await expect(page.locator('#snapshot-line')).not.toContainText('unavailable');
  expect(errors).toEqual([]);
});

test('old links and browser lifecycle never contact sync; import reaches another tab without a feed copy',async({context,page})=>{
  const external=await fixture(context,[]);
  await page.goto('triage.html#sync=legacy-code');await loaded(page);
  const other=await context.newPage();await other.goto('triage.html');await loaded(other);
  await page.locator('.data-tools summary').click();
  const job={...makeJobs(1)[0],url:'https://jobs.test/import-only'};
  await page.locator('#import-file').setInputFiles({name:'old-backup.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify({code:'old-secret',triage:{[job.url]:{s:'saved',t:1}},jobs:[job]}))});
  await expect(other.locator('.job[data-state=saved]')).toHaveCount(1);
  await page.getByRole('button',{name:'Refresh',exact:false}).click();await loaded(page);
  await page.evaluate(()=>{document.dispatchEvent(new Event('visibilitychange'));window.dispatchEvent(new Event('pagehide'));window.dispatchEvent(new Event('focus'));});
  await other.close();await page.reload();await loaded(page);
  expect(external.filter(u=>/triage-sync|upstash/.test(u))).toEqual([]);
  expect(await page.evaluate(()=>JSON.parse(localStorage.getItem('jobTriage:v2')).code)).toBeUndefined();
});

for (const width of [390, 1440]) test(`daily Rank at ${width}px compares five resumes and preserves decisions`, async ({context,page}) => {
  await page.setViewportSize({width,height:900});
  const resumes=['BioScience_ML','ML','DS','SWE','FDE'];
  const result = n => ({model:'fixture',scores:Object.fromEntries(resumes.map(r=>[r,n])),best_resumes:['SWE'],requirements:[{text:'Python required <img src=x onerror=alert(1)>',importance:'required',hard_eligibility:true,statuses:Object.fromEntries(resumes.map(r=>[r,'matched']))}]});
  await fixture(context,makeJobs(3),{rankings:{version:1,updated_at:new Date().toISOString(),scores:{
    'https://jobs.test/0':{status:'valid',luna:result(60)},
    'https://jobs.test/1':{status:'valid',luna:result(90),sonnet:result(75)},
    'https://jobs.test/2':{status:'stale',luna:result(100)},
  }}});
  await page.goto('triage.html');await loaded(page);
  await page.locator('#view-rank').click();
  await expect(page.locator('.job').first()).toHaveAttribute('data-url','https://jobs.test/1');
  await expect(page.locator('#rank-info')).toContainText('Daily ranking');
  const first=page.locator('.job').first();
  await first.locator('.ranking-details > summary').click();
  await expect(first.locator('tbody tr')).toHaveCount(5);
  await expect(first.locator('tbody tr').first()).toContainText('90');
  await expect(first.locator('tbody tr').first()).toContainText('75');
  await first.getByText('Luna requirements and skills',{exact:true}).click();
  await expect(first.locator('.ranking-details img')).toHaveCount(0);
  await expect(first.locator('.match-statuses').first()).toContainText('Matched');
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await first.getByRole('button',{name:'Save',exact:false}).click();
  await page.locator('#view-browse').click();
  await expect(page.locator('.job[data-url="https://jobs.test/1"]')).toHaveAttribute('data-state','saved');
  await page.locator('#view-rank').click();
  await expect(page.locator('.job').last()).toHaveAttribute('data-url','https://jobs.test/2');
  await page.locator('.job[data-url="https://jobs.test/1"] .act.applied').click();
  await expect(page.locator('.job[data-url="https://jobs.test/1"]')).toHaveCount(0);
  await expect(page.locator('#filter-state')).not.toContainText('Applied');
  await page.locator('#view-browse').click();
  await expect(page.locator('.job[data-url="https://jobs.test/1"]')).toHaveAttribute('data-state','applied');
  await page.locator('#view-rank').click();
  await expect(page.locator('.job[data-url="https://jobs.test/1"]')).toHaveCount(0);
});
