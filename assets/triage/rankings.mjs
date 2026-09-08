export const RESUMES = ['BioScience_ML', 'ML', 'DS', 'SWE', 'FDE'];
const escape = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const validScore = v => Number.isInteger(v) && v >= 0 && v <= 100;
export function bestScore(record) {
  if (record?.status !== 'valid') return null;
  const values = RESUMES.map(r => record.luna?.scores?.[r]).filter(validScore);
  return values.length === 5 ? Math.max(...values) : null;
}
export function parseRankings(data) {
  if (data?.version !== 1 || !data.scores || typeof data.scores !== 'object' || Array.isArray(data.scores)) throw new Error('Invalid rankings');
  return data;
}
const labels = {matched: 'Matched', partial: 'Partial', not_evidenced: 'Not evidenced', confirmed_unmet: 'Confirmed unmet'};
export function rankingDetails(record) {
  if (!record) return '<p class="ranking-note">Not ranked yet.</p>';
  if (bestScore(record) === null) return `<p class="ranking-note">${escape({stale:'Resume or scoring settings changed; awaiting a fresh ranking.', jd_unavailable:'Full job description unavailable.', invalid_or_unknown:'Scoring did not complete; no score assigned.'}[record.status] || 'No testable qualifications scored.')}</p>`;
  const score = (provider, resume) => validScore(record[provider]?.scores?.[resume]) ? record[provider].scores[resume] : '—';
  const requirements = provider => (record[provider]?.requirements || []).map(r => `<li><p>${escape(r.text)}</p><small>${escape(r.importance)}${r.hard_eligibility ? ' · Mandatory' : ''}</small><div class="match-statuses">${RESUMES.map(resume => `<span><b>${escape(resume.replaceAll('_', ' '))}:</b> ${escape(labels[r.statuses?.[resume]] || 'Unknown')}</span>`).join('')}</div></li>`).join('');
  return `<details class="ranking-details"><summary>Resume match · ${escape((record.luna.best_resumes || []).join(' / '))}${record.sonnet ? ' · Sonnet reviewed' : ''}</summary>
    <p class="ranking-note">Qualification match, not a hiring probability. Missing evidence does not mean you lack a skill. Sorted by Luna; scores are not averaged.</p>
    <table><caption>Resume match scores out of 100</caption><thead><tr><th>Resume</th><th>Luna</th><th>Sonnet</th></tr></thead><tbody>${RESUMES.map(r => `<tr><th>${escape(r.replaceAll('_', ' '))}</th><td>${score('luna', r)}</td><td>${score('sonnet', r)}</td></tr>`).join('')}</tbody></table>
    <p class="ranking-note">${record.sonnet ? 'Sonnet independently reviewed these qualifications.' : record.sonnet_status ? 'Sonnet review did not complete.' : 'Not shortlisted for Sonnet review yet.'}</p>
    <details><summary>Luna requirements and skills</summary><ul>${requirements('luna')}</ul></details>
    ${record.sonnet ? `<details><summary>Sonnet requirements and skills</summary><ul>${requirements('sonnet')}</ul></details>` : ''}
  </details>`;
}
