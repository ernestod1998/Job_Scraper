import { validJob } from './decisions.mjs';

export function validateFeed(data) {
  if (!data || typeof data !== 'object' || Array.isArray(data) || !Array.isArray(data.jobs)) throw new Error('Expected a jobs array');
  if (data.new_jobs != null && !Array.isArray(data.new_jobs)) throw new Error('Invalid new_jobs array');
  for (const key of ['total', 'new_count']) if (data[key] != null && (!Number.isInteger(data[key]) || data[key] < 0)) throw new Error(`Invalid ${key}`);
  for (const key of ['scraped_at', 'last_attempt_at', 'last_success_at']) if (data[key] != null && typeof data[key] !== 'string') throw new Error(`Invalid ${key}`);
  if (data.status != null && !['ok', 'partial', 'cached', 'error'].includes(data.status)) throw new Error('Invalid scrape status');
  const usable = j => validJob(j) && typeof j.title === 'string' && j.title.trim();
  const jobs = data.jobs.filter(usable).map(j => ({ ...j }));
  const newJobs = (data.new_jobs || []).filter(usable).map(j => ({ ...j }));
  const skipped = data.jobs.length - jobs.length + (data.new_jobs || []).length - newJobs.length;
  if (data.jobs.length && !jobs.length) throw new Error('No valid jobs in nonempty snapshot');
  return { ...data, jobs, new_jobs: newJobs, skipped };
}

// Keeps last good source snapshots across partial failures. A superseded request
// never changes either source health or data, even if fetch ignores cancellation.
export function createFeedLoader(sources, { fetcher = globalThis.fetch, timeoutMs = 15000 } = {}) {
  const snapshots = new Map(), health = new Map();
  let generation = 0, active = [];
  async function load({ failedOnly = false } = {}) {
    const id = ++generation;
    active.forEach(c => c.abort()); active = [];
    const selected = failedOnly ? sources.filter(s => health.get(s.name)?.delivery === 'error') : sources;
    await Promise.all(selected.map(async source => {
      const controller = new AbortController(); active.push(controller);
      let timer;
      try {
        const request = (async () => {
          const response = await fetcher(source.name + `?v=${Date.now()}`, { signal: controller.signal });
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          return validateFeed(await response.json());
        })();
        const data = await Promise.race([request, new Promise((_, reject) => {
          timer = setTimeout(() => { controller.abort(); reject(new Error('Timed out after 15 seconds')); }, timeoutMs);
        })]);
        if (id !== generation) return;
        snapshots.set(source.name, data);
        health.set(source.name, { delivery: data.skipped ? 'partial' : 'ok', skipped: data.skipped, scrape: data.status || 'unknown', last_success_at: data.last_success_at ?? null });
      } catch (error) {
        if (id !== generation) return;
        health.set(source.name, { ...health.get(source.name), delivery: 'error', message: error.message, cached: snapshots.has(source.name) });
      } finally { clearTimeout(timer); }
    }));
    if (id !== generation) return null;
    const byUrl = new Map(), newByUrl = new Map();
    let scraped_at = '', new_count = 0;
    for (const source of sources) {
      const data = snapshots.get(source.name);
      if (!data) continue;
      for (const row of data.jobs) {
        const j = { ...row };
        if (source.src === 'Biotech' && !j.feeds?.length && !j.feed) j.feeds = ['biotech'];
        const existing = byUrl.get(j.url);
        if (!existing) byUrl.set(j.url, j);
        else if (source.src === 'Master') {
          if (!existing.first_seen && j.first_seen) existing.first_seen = j.first_seen;
          const feeds = [...new Set([...(existing.feeds || (existing.feed ? [existing.feed] : [])), ...(j.feeds || (j.feed ? [j.feed] : []))])];
          if (feeds.length) existing.feeds = feeds;
          if (!existing.ats && j.ats) existing.ats = j.ats;
        }
      }
      for (const j of data.new_jobs) newByUrl.set(j.url, j);
      new_count += data.new_count || 0;
      if ((data.scraped_at || '') > scraped_at) scraped_at = data.scraped_at;
    }
    return { jobs: [...byUrl.values()], new_jobs: [...newByUrl.values()], new_count, scraped_at, health: new Map(health) };
  }
  return { load };
}
