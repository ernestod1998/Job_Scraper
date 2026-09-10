export const RESUMES = ['BioScience_ML', 'ML', 'DS', 'SWE', 'FDE', 'Research_Software_Engineer'];
const escape = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const validScore = v => Number.isInteger(v) && v >= 0 && v <= 100;
function availableBest(record) {
  const scores = record?.luna?.scores;
  if (!RESUMES.slice(0, 5).every(r => validScore(scores?.[r]))) return null;
  const research = scores?.Research_Software_Engineer;
  if (research !== undefined && !validScore(research)) return null;
  return Math.max(...RESUMES.map(r => scores?.[r]).filter(validScore));
}
export function bestScore(record) {
  if (record?.status !== 'valid') return null;
  return availableBest(record);
}
export function parseRankings(data) {
  if (data?.version !== 1 || !data.scores || typeof data.scores !== 'object' || Array.isArray(data.scores)) throw new Error('Invalid rankings');
  return data;
}
const labels = {matched: 'Matched', partial: 'Partial', not_evidenced: 'Not evidenced', confirmed_unmet: 'Confirmed unmet'};
export function rankingDetails(record) {
  if (!record) return '<p class="ranking-note">Not ranked yet.</p>';
  const historical = record.status === 'stale' && availableBest(record) !== null;
  if (bestScore(record) === null && !historical) return `<p class="ranking-note">${escape({stale:'Resume or scoring settings changed; awaiting a fresh ranking.', jd_unavailable:'Full job description unavailable.', invalid_or_unknown:'Scoring did not complete; no score assigned.'}[record.status] || 'No testable qualifications scored.')}</p>`;
  const pending = !validScore(record.luna?.scores?.Research_Software_Engineer);
  const score = (provider, resume) => validScore(record[provider]?.scores?.[resume]) ? record[provider].scores[resume] : resume === 'Research_Software_Engineer' ? 'Pending' : '—';
  const requirements = provider => (record[provider]?.requirements || []).map(r => `<li><p>${escape(r.text)}</p><small>${escape(r.importance)}${r.hard_eligibility ? ' · Mandatory' : ''}</small><div class="match-statuses">${RESUMES.map(resume => `<span><b>${escape(resume.replaceAll('_', ' '))}:</b> ${escape(labels[r.statuses?.[resume]] || 'Unknown')}</span>`).join('')}</div></li>`).join('');
  return `<details class="ranking-details"><summary>${historical ? 'Previous resume match' : 'Resume match'} · ${escape((record.luna.best_resumes || []).join(' / '))}${record.sonnet ? ' · Sonnet reviewed' : ''}</summary>
    ${historical ? '<p class="ranking-note">Previous scores retained for reference. Resume or scoring settings changed; awaiting a fresh ranking. These scores are not used for current ranking order.</p>' : ''}
    ${pending ? '<p class="ranking-note">Research Software Engineer: pending. This comparison covers the original five resumes only.</p>' : ''}
    <p class="ranking-note">Qualification match, not a hiring probability. Missing evidence does not mean you lack a skill. Sorted by Luna; scores are not averaged.${record.scored_at ? ' Scored ' + escape(record.scored_at.slice(0,10)) + '.' : ''}</p>
    <table><caption>Resume match scores out of 100</caption><thead><tr><th>Resume</th><th>Luna</th><th>Sonnet</th></tr></thead><tbody>${RESUMES.map(r => `<tr><th>${escape(r.replaceAll('_', ' '))}</th><td>${score('luna', r)}</td><td>${score('sonnet', r)}</td></tr>`).join('')}</tbody></table>
    <p class="ranking-note">${record.sonnet ? 'Sonnet independently reviewed these qualifications.' : record.sonnet_status ? 'Sonnet review did not complete.' : 'Not shortlisted for Sonnet review yet.'}</p>
    <details><summary>Luna requirements and skills</summary><ul>${requirements('luna')}</ul></details>
    ${record.sonnet ? `<details><summary>Sonnet requirements and skills</summary><ul>${requirements('sonnet')}</ul></details>` : ''}
  </details>`;
}
