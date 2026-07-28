// Triage decision sync — one endpoint, one Redis key per person.
//
// The client generates a high-entropy code, keeps it locally, and sends only
// SHA-256(code) as `x-triage-key`. This server therefore never sees the code
// itself, so a Redis dump or a log line can't reveal it. The hash is still a
// bearer credential in transit — that's what HTTPS is for; don't oversell it.
//
// The key travels in a header, never a query string: request lines end up in
// access logs, and a credential in a URL is a credential in a log file.

import { Redis } from '@upstash/redis';
import { Ratelimit } from '@upstash/ratelimit';
import { mergeTriage, gcDecisions } from '../merge.js';

const ALLOWED_ORIGINS = new Set([
  'https://ernestod1998.github.io',
  'http://localhost:8765',    // the Playwright suite
]);

const TTL_SECONDS = 365 * 86400;   // refreshed on every write
const MAX_BODY = 1_000_000;        // 1MB — see the growth note in the plan
const MAX_ENTRIES = 20000;         // backstop against a malicious payload
const KEY_RE = /^[0-9a-f]{64}$/;   // SHA-256, hex

// NOT Redis.fromEnv(): the Vercel Marketplace integration injects these as
// KV_REST_API_URL / KV_REST_API_TOKEN, while fromEnv() looks for
// UPSTASH_REDIS_REST_*. Accept either so this works however it's provisioned.
const redis = new Redis({
  url: process.env.KV_REST_API_URL || process.env.UPSTASH_REDIS_REST_URL,
  token: process.env.KV_REST_API_TOKEN || process.env.UPSTASH_REDIS_REST_TOKEN,
});

// Per-key and per-IP. A public endpoint with no auth wall needs both: the key
// limit stops one bucket being hammered, the IP limit stops someone cycling
// through keys.
const limitByKey = new Ratelimit({
  redis, limiter: Ratelimit.slidingWindow(60, '1 m'), prefix: 'rl:key',
});
const limitByIp = new Ratelimit({
  redis, limiter: Ratelimit.slidingWindow(120, '1 m'), prefix: 'rl:ip',
});

function setCors(req, res) {
  const origin = req.headers.origin;
  if (origin && ALLOWED_ORIGINS.has(origin)) {
    res.setHeader('Access-Control-Allow-Origin', origin);
    res.setHeader('Vary', 'Origin');
  }
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'content-type, x-triage-key');
  res.setHeader('Access-Control-Max-Age', '86400');
}

// The body arrives as a parsed object (application/json) or a raw string
// (text/plain — the sendBeacon fallback, which must stay CORS-simple).
function parseBody(req) {
  const b = req.body;
  if (b == null) return null;
  if (typeof b === 'object' && !Buffer.isBuffer(b)) return b;
  const text = Buffer.isBuffer(b) ? b.toString('utf8') : String(b);
  if (text.length > MAX_BODY) throw new Error('body too large');
  try { return JSON.parse(text); } catch { throw new Error('invalid JSON'); }
}

// Never trust the wire. Coerce to {s,t}, drop anything malformed, and clamp
// timestamps from the future so a device with a wrong clock can't win every
// merge forever.
function sanitizeTriage(raw, now) {
  const out = {};
  const skew = now + 86400000;   // 24h
  let n = 0;
  for (const url in (raw || {})) {
    if (++n > MAX_ENTRIES) break;
    if (typeof url !== 'string' || url.length > 2048) continue;
    const d = raw[url];
    if (!d || typeof d !== 'object') continue;
    const s = d.s;
    if (s !== null && s !== 'saved' && s !== 'applied' && s !== 'dismissed') continue;
    let t = Number(d.t);
    if (!Number.isFinite(t) || t < 0) t = 0;
    out[url] = { s: s || null, t: Math.min(t, skew) };
  }
  return out;
}

function sanitizeJobs(raw, triage) {
  if (!Array.isArray(raw)) return [];
  const out = [];
  for (const j of raw) {
    if (!j || typeof j !== 'object') continue;
    if (typeof j.url !== 'string' || !/^https?:\/\//i.test(j.url)) continue;
    if (!triage[j.url]) continue;   // only jobs carrying a decision are worth storing
    out.push(j);
    if (out.length >= MAX_ENTRIES) break;
  }
  return out;
}

const EMPTY = { v: 2, triage: {}, jobs: [] };

export default async function handler(req, res) {
  setCors(req, res);
  if (req.method === 'OPTIONS') return res.status(204).end();

  // Remote kill switch. Sync is on by default for people who never asked for
  // it, so there has to be a way to stop it for everyone without waiting on a
  // client revert plus a GitHub Pages rebuild.
  if (process.env.SYNC_DISABLED === '1') {
    return res.status(200).json({ disabled: true });
  }

  if (req.method !== 'GET' && req.method !== 'POST') {
    return res.status(405).json({ error: 'method not allowed' });
  }

  const k = req.headers['x-triage-key'];
  if (typeof k !== 'string' || !KEY_RE.test(k)) {
    return res.status(400).json({ error: 'missing or malformed x-triage-key' });
  }

  const ip = (req.headers['x-forwarded-for'] || '').split(',')[0].trim() || 'unknown';
  const [byKey, byIp] = await Promise.all([limitByKey.limit(k), limitByIp.limit(ip)]);
  if (!byKey.success || !byIp.success) {
    return res.status(429).json({ error: 'rate limited' });
  }

  const redisKey = `triage:${k}`;
  const now = Date.now();

  try {
    if (req.method === 'GET') {
      const stored = await redis.get(redisKey);
      return res.status(200).json(stored || EMPTY);
    }

    let body;
    try { body = parseBody(req); }
    catch (e) { return res.status(413).json({ error: e.message }); }
    if (!body || typeof body !== 'object') {
      return res.status(400).json({ error: 'expected a JSON object' });
    }

    const incomingTriage = sanitizeTriage(body.triage, now);
    const stored = (await redis.get(redisKey)) || EMPTY;

    // Same rule the browser uses, applied server-side: two devices racing is
    // the same bug as two windows racing, and the client has to be able to
    // trust the merged result it gets back.
    const merged = gcDecisions(mergeTriage(stored.triage, incomingTriage), now);

    const byUrl = new Map();
    for (const j of (stored.jobs || [])) if (j && merged[j.url]) byUrl.set(j.url, j);
    for (const j of sanitizeJobs(body.jobs, merged)) byUrl.set(j.url, j);

    const next = { v: 2, triage: merged, jobs: [...byUrl.values()], at: now };
    await redis.set(redisKey, next, { ex: TTL_SECONDS });
    return res.status(200).json(next);
  } catch (e) {
    console.error('[triage-sync]', req.method, e && e.message);
    return res.status(500).json({ error: 'sync failed' });
  }
}
