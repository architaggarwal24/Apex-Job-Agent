// frontend/src/lib/insights.js
//
// Plain helpers for the Today and Insights pages (no React, so tests run them under Node).

// The outcome groups the Insights page shows (GET /api/analytics/outcomes "by").
export const DIMENSIONS = [['source', 'Source'], ['title', 'Title'], ['score_band', 'Score band'], ['resume_variant', 'Resume variant']]
export const RATES = [['reply_rate', 'Reply'], ['interview_rate', 'Interview'], ['offer_rate', 'Offer']]

// A rate as the server sent it. A group below the sample-size threshold comes back as the
// text "not enough data" - it is shown as that text and NEVER turned into a number or a bar.
export function rateView(v) {
  if (typeof v === 'number' && Number.isFinite(v)) return { text: `${v}%`, pct: Math.max(0, Math.min(100, v)) }
  return { text: typeof v === 'string' && v ? v : 'not enough data', pct: null }
}

export function medianView(v) {
  if (typeof v === 'number' && Number.isFinite(v)) return `${v} day${v === 1 ? '' : 's'}`
  if (v === null || v === undefined) return 'no responses yet'
  return String(v)
}

// The queue's daily budget: applied today against the limit (0 = no limit).
export function budget(applied, limit) {
  const a = Math.max(0, Number(applied) || 0), l = Math.max(0, Number(limit) || 0)
  if (!l) return { text: `${a} today · no daily limit`, pct: null, full: false }
  return { text: `${a} / ${l} today`, pct: Math.min(100, Math.round(a / l * 100)), full: a >= l }
}

// "in 3m 05s" until `at` (ISO, local), "now" once it has passed, "" when there is no time.
export function countdown(at, now = new Date()) {
  if (!at) return ''
  const t = new Date(at).getTime()
  if (Number.isNaN(t)) return ''
  let s = Math.round((t - now.getTime()) / 1000)
  if (s <= 0) return 'now'
  const h = Math.floor(s / 3600); s -= h * 3600
  const m = Math.floor(s / 60); s -= m * 60
  return `in ${h ? `${h}h ` : ''}${h || m ? `${h ? String(m).padStart(2, '0') : m}m ` : ''}${String(s).padStart(h || m ? 2 : 1, '0')}s`
}

// The links the Today page's counts open: the Applications workspace, filtered.
export const INTERVIEW_STAGES = ['recruiter_screen', 'technical_round', 'hiring_manager']
export const TODAY_LINKS = {
  needs_you: '/applications?needs_you=1',
  ready_for_review: '/applications?ready_for_review=1&view=review',
  interviews: `/applications?stage=${INTERVIEW_STAGES.join(',')}&next_step_within=48`,
  follow_ups: '/applications?followups=due',
}
