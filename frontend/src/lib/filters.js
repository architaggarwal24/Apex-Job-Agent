// frontend/src/lib/filters.js
//
// The Applications workspace's filters and their URL form - the contract other pages
// link in with (e.g. /applications?needs_you=1, ?stage=a,b&source=linkedin):
//   stage=a,b         one or more lifecycle stages (unknown names are ignored)
//   needs_you=1       adds needs_you          ready_for_review=1  adds ready_for_review
//   source=  title=   exact (source in any case)        search= (or q=)  substring of title/company/location
//   min= max=         score range, inclusive (an unscored job is outside any range)
//   view=board|list|review                    job=<job_id> is read by the page itself (opens its panel)
//   next_step_within=48   the card's next step (next_step_at) falls within the next N hours (1-720)
//   followups=due         the card has a follow-up due now (GET /api/follow-ups/due)
// Plain JS with no React, so tests can run it under Node.
import { STAGE_KEYS } from '@/lib/stages'

export function filtersFrom(params) {
  const stages = new Set((params.get('stage') || '').split(',').map(s => s.trim()).filter(s => STAGE_KEYS.includes(s)))
  if (['1', 'true'].includes(params.get('needs_you'))) stages.add('needs_you')
  if (['1', 'true'].includes(params.get('ready_for_review'))) stages.add('ready_for_review')
  const view = params.get('view')
  return {
    q: params.get('search') || params.get('q') || '', title: params.get('title') || '',
    source: params.get('source') || '', stages: [...stages],
    min: params.get('min') || '', max: params.get('max') || '',
    view: ['board', 'list', 'review'].includes(view) ? view : 'board',
    within: hoursParam(params.get('next_step_within')),
    followups: params.get('followups') === 'due' ? 'due' : '',
  }
}

function hoursParam(v) {
  const n = Number(v)
  return Number.isInteger(n) && n >= 1 && n <= 720 ? String(n) : ''
}

// A next_step_at as a local Date; a date alone ("2026-11-03") is that day's start.
export function parseWhen(s) {
  if (!s) return null
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s)
  const d = m ? new Date(+m[1], +m[2] - 1, +m[3]) : new Date(s)
  return Number.isNaN(d.getTime()) ? null : d
}

// Is the next step within the next `hours`? A date alone counts for that whole day - the same
// rule the notifier's "interview within 24 h" uses (core/notifier.py).
export function nextStepWithin(at, hours, now = new Date()) {
  const when = parseWhen(at)
  if (!when || !hours) return false
  const end = now.getTime() + Number(hours) * 3600e3
  if (/^\d{4}-\d{2}-\d{2}$/.test(at)) return when.getTime() <= end && when.getTime() + 864e5 > now.getTime()
  return when.getTime() >= now.getTime() && when.getTime() <= end
}

export function queryFrom(f, openId) {
  const p = new URLSearchParams()
  if (f.q) p.set('search', f.q)
  if (f.title) p.set('title', f.title)
  if (f.source) p.set('source', f.source)
  if (f.stages.length) p.set('stage', f.stages.join(','))
  if (f.min !== '') p.set('min', f.min)
  if (f.max !== '') p.set('max', f.max)
  if (f.view !== 'board') p.set('view', f.view)
  if (f.within) p.set('next_step_within', f.within)
  if (f.followups) p.set('followups', f.followups)
  if (openId) p.set('job', openId)
  const s = p.toString()
  return s ? `?${s}` : ''
}

export const byScore = (a, b) => (b.ai_score ?? -1) - (a.ai_score ?? -1) || String(a.stage_entered_at).localeCompare(String(b.stage_entered_at))

// `ctx`: {now, dueIds} - the clock, and the job ids with a follow-up due (needed for followups=due).
export function applyFilters(apps, f, ctx = {}) {
  const q = f.q.trim().toLowerCase()
  const now = ctx.now || new Date(), due = ctx.dueIds || new Set()
  const min = f.min === '' ? null : Number(f.min), max = f.max === '' ? null : Number(f.max)
  return apps.filter(a =>
    (!f.stages.length || f.stages.includes(a.stage)) &&
    (!f.title || a.title === f.title) &&
    (!f.source || (a.source || '').toLowerCase() === f.source.toLowerCase()) &&
    (min == null || (a.ai_score != null && a.ai_score >= min)) &&
    (max == null || (a.ai_score != null && a.ai_score <= max)) &&
    (!q || `${a.title} ${a.company} ${a.location || ''}`.toLowerCase().includes(q)) &&
    (!f.within || nextStepWithin(a.next_step_at, f.within, now)) &&
    (!f.followups || due.has(a.job_id)))
}

