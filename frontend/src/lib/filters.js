// frontend/src/lib/filters.js
//
// The Applications workspace's filters and their URL form - the contract other pages
// link in with (e.g. /applications?needs_you=1, ?stage=a,b&source=linkedin):
//   stage=a,b         one or more lifecycle stages (unknown names are ignored)
//   needs_you=1       adds needs_you          ready_for_review=1  adds ready_for_review
//   source=  title=   exact (source in any case)        search= (or q=)  substring of title/company/location
//   min= max=         score range, inclusive (an unscored job is outside any range)
//   view=cards|list|board|review (none: the tab's own - cards for the Inbox, list elsewhere)
//   job=<job_id> is read by the page itself (opens its panel)
//   next_step_within=48   the card's next step (next_step_at) falls within the next N hours (1-720)
//   followups=due         the card has a follow-up due now (GET /api/follow-ups/due)
//   form=1                the job applies on a company form (apply_url set: no job-board account needed)
// The filter panel's own (lists are comma-separated):
//   loc=pune              the location contains it        work=remote,hybrid,onsite
//   sal=12                salary of at least 12 LPA (when the posting says; nosal=1 also hides
//                         jobs that don't say)          age=7   posted (or found) in the last N days
//   kind=ai,quick,none    who scored it                  level=intern,junior,mid,senior (from the title)
//   co=acme,kla           only these companies           xco=tcs  never these
//   source=linkedin,indeed  these boards                  ats=greenhouse,lever,none (none: on the board itself)
// Plain JS with no React, so tests can run it under Node.
import { STAGE_KEYS, applyHost } from '@/lib/stages'

export function filtersFrom(params) {
  const stages = new Set((params.get('stage') || '').split(',').map(s => s.trim()).filter(s => STAGE_KEYS.includes(s)))
  if (['1', 'true'].includes(params.get('needs_you'))) stages.add('needs_you')
  if (['1', 'true'].includes(params.get('ready_for_review'))) stages.add('ready_for_review')
  const view = params.get('view')
  return {
    q: params.get('search') || params.get('q') || '', title: params.get('title') || '',
    source: params.get('source') || '', stages: [...stages],
    min: params.get('min') || '', max: params.get('max') || '',
    view: ['cards', 'board', 'list', 'review'].includes(view) ? view : '',
    within: hoursParam(params.get('next_step_within')),
    followups: params.get('followups') === 'due' ? 'due' : '',
    form: ['1', 'true'].includes(params.get('form')) ? '1' : '',
    loc: (params.get('loc') || '').slice(0, 80),
    work: listParam(params.get('work'), WORK),
    sal: numParam(params.get('sal'), 0, 500),
    nosal: ['1', 'true'].includes(params.get('nosal')) ? '1' : '',
    age: AGES.includes(params.get('age')) ? params.get('age') : '',
    kind: listParam(params.get('kind'), KINDS),
    level: listParam(params.get('level'), LEVELS),
    co: textList(params.get('co')), xco: textList(params.get('xco')),
    ats: textList(params.get('ats')),
  }
}

export const WORK = ['remote', 'hybrid', 'onsite']
export const KINDS = ['ai', 'quick', 'none']
export const LEVELS = ['intern', 'junior', 'mid', 'senior']
export const AGES = ['1', '3', '7', '30']
export const listOf = v => String(v || '').split(',').map(x => x.trim()).filter(Boolean)
function listParam(v, allowed) { return listOf(v).map(x => x.toLowerCase()).filter((x, i, a) => allowed.includes(x) && a.indexOf(x) === i).join(',') }
function textList(v) { return listOf(v).map(x => x.slice(0, 60)).slice(0, 20).join(',') }
function numParam(v, lo, hi) { const n = Number(v); return v !== null && v !== '' && Number.isFinite(n) && n >= lo && n <= hi ? String(n) : '' }

// A salary text as yearly lakhs (LPA, the top of a range), or null when it names no
// rupee amount: "₹10L - ₹15L a year" -> 15, "12-18 LPA" -> 18, "₹50,000 a month" -> 6,
// "₹ 8,00,000 - 12,00,000" -> 12, "$120k" -> null (another currency), "" -> null.
export function salaryLpa(text) {
  const t = String(text || '').toLowerCase().replace(/,/g, '')
  if (!t.trim() || /[$€£]|\b(usd|eur|gbp|aud|cad|sgd)\b/.test(t)) return null
  const monthly = /month|\/mo\b|per mo|\bpm\b/.test(t)
  let nums = []
  const range = /(\d+(?:\.\d+)?)\s*(?:l|lpa|lacs?|lakhs?)?\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*(?:l|lpa|lacs?|lakhs?)\b/.exec(t)
  if (range) nums = [Number(range[1]), Number(range[2])]
  else {
    nums = [...t.matchAll(/(\d+(?:\.\d+)?)\s*(?:l|lpa|lacs?|lakhs?)\b/g)].map(m => Number(m[1]))
    if (!nums.length) {
      nums = [...t.matchAll(/(\d+(?:\.\d+)?)\s*(k|cr|crores?)?\b/g)]
        .map(m => (m[2] === 'k' ? Number(m[1]) * 1e3 : m[2] ? Number(m[1]) * 1e7 : Number(m[1])))
        .filter(n => n >= 1000).map(n => n / 1e5)
    }
  }
  if (!nums.length) return null
  const v = Math.max(...nums) * (monthly ? 12 : 1)
  return Math.round(v * 10) / 10
}

const SENIOR_RX = /\b(senior|sr\.?|staff|principal|lead|manager|director|head|architect|vp|vice president)\b/i
const JUNIOR_RX = /\b(junior|jr\.?|fresher|entry[ -]level|graduate|associate|new grad)\b/i
const INTERN_RX = /\b(intern|internship|trainee|apprentice)\b/i
// The seniority a title says: intern, junior, senior, else mid.
export const levelOf = title => (INTERN_RX.test(title || '') ? 'intern' : SENIOR_RX.test(title || '') ? 'senior' : JUNIOR_RX.test(title || '') ? 'junior' : 'mid')
// Where the work is, from the location (and title): remote, hybrid, else onsite.
export const workOf = a => {
  const t = `${a.location || ''} ${a.title || ''}`
  return /\b(remote|work from home|wfh|anywhere)\b/i.test(t) ? 'remote' : /\bhybrid\b/i.test(t) ? 'hybrid' : 'onsite'
}
// The form a job applies on: the ATS name ("greenhouse"), or "none" when it applies on the board.
export const atsOf = a => (a.apply_url && a.apply_url !== a.url ? applyHost(a.apply_url) || 'none' : 'none')
const daysSince = (at, now) => { const t = new Date(/^\d{4}-\d{2}-\d{2}$/.test(at || '') ? `${at}T00:00:00` : at).getTime(); return Number.isNaN(t) ? null : (now.getTime() - t) / 864e5 }

// The active filters as chips: [{key, label}] (the search box and the stage tabs are not chips).
const LABEL = { ai: 'AI-scored', quick: 'Quick match', none: 'Not scored', remote: 'Remote', hybrid: 'Hybrid', onsite: 'On-site',
  intern: 'Intern', junior: 'Junior', mid: 'Mid', senior: 'Senior' }
export function chipsFor(f) {
  const out = []
  const many = (k, prefix) => { if (f[k]) out.push({ key: k, label: `${prefix}${listOf(f[k]).map(x => LABEL[x] || x).join(' / ')}` }) }
  if (f.title) out.push({ key: 'title', label: f.title })
  if (f.loc) out.push({ key: 'loc', label: `In “${f.loc}”` })
  many('work', '')
  if (f.sal) out.push({ key: 'sal', label: `≥ ₹${f.sal} LPA` })
  if (f.nosal) out.push({ key: 'nosal', label: 'Salary stated' })
  if (f.age) out.push({ key: 'age', label: f.age === '1' ? 'Last 24 h' : `Last ${f.age} days` })
  if (f.min !== '' && f.max !== '') out.push({ key: 'min', also: 'max', label: `Fit ${f.min}–${f.max}` })
  else if (f.min !== '') out.push({ key: 'min', label: `Fit ≥ ${f.min}` })
  else if (f.max !== '') out.push({ key: 'max', label: `Fit ≤ ${f.max}` })
  many('kind', '')
  many('level', '')
  many('co', 'Only ')
  many('xco', 'Not ')
  many('source', 'From ')
  if (f.ats) out.push({ key: 'ats', label: `On ${listOf(f.ats).map(x => (x === 'none' ? 'the board' : x)).join(' / ')}` })
  if (f.form) out.push({ key: 'form', label: 'Company form' })
  if (f.within) out.push({ key: 'within', label: `Next step within ${f.within} h` })
  if (f.followups) out.push({ key: 'followups', label: 'Follow-up due' })
  return out
}
export const FILTER_KEYS = ['title', 'source', 'min', 'max', 'form', 'loc', 'work', 'sal', 'nosal', 'age', 'kind', 'level', 'co', 'xco', 'ats', 'within', 'followups']
// `f` without the chip `chip` (both ends of a fit range at once).
export function withoutChip(f, chip) { return { ...f, [chip.key]: '', ...(chip.also ? { [chip.also]: '' } : {}) } }
export function clearFilters(f) { return { ...f, ...Object.fromEntries(FILTER_KEYS.map(k => [k, ''])) } }

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
  if (f.view) p.set('view', f.view)
  if (f.within) p.set('next_step_within', f.within)
  if (f.followups) p.set('followups', f.followups)
  if (f.form) p.set('form', '1')
  for (const k of ['loc', 'work', 'sal', 'nosal', 'age', 'kind', 'level', 'co', 'xco', 'ats']) if (f[k]) p.set(k, f[k])
  if (openId) p.set('job', openId)
  const s = p.toString()
  return s ? `?${s}` : ''
}

// The score a row shows and sorts by: `fit` from the API (the AI score when there is one,
// else the quick keyword match); rows from older payloads fall back to ai_score.
export const fitOf = a => (a.fit !== undefined ? a.fit : a.ai_score) ?? null
export const byScore = (a, b) => (fitOf(b) ?? -1) - (fitOf(a) ?? -1) || String(a.stage_entered_at).localeCompare(String(b.stage_entered_at))

// `ctx`: {now, dueIds} - the clock, and the job ids with a follow-up due (needed for followups=due).
export function applyFilters(apps, f, ctx = {}) {
  const q = f.q.trim().toLowerCase()
  const now = ctx.now || new Date(), due = ctx.dueIds || new Set()
  const min = f.min === '' ? null : Number(f.min), max = f.max === '' ? null : Number(f.max)
  const sources = listOf(f.source).map(x => x.toLowerCase()), work = listOf(f.work), kinds = listOf(f.kind), levels = listOf(f.level)
  const only = listOf(f.co).map(x => x.toLowerCase()), never = listOf(f.xco).map(x => x.toLowerCase()), ats = listOf(f.ats).map(x => x.toLowerCase())
  const loc = (f.loc || '').trim().toLowerCase(), sal = f.sal ? Number(f.sal) : null, age = f.age ? Number(f.age) : null
  return apps.filter(a =>
    (!f.stages.length || f.stages.includes(a.stage)) &&
    (!f.title || a.title === f.title) &&
    (!sources.length || sources.includes((a.source || '').toLowerCase())) &&
    (!loc || (a.location || '').toLowerCase().includes(loc)) &&
    (!work.length || work.includes(workOf(a))) &&
    (sal == null || (salaryLpa(a.salary) == null ? !f.nosal : salaryLpa(a.salary) >= sal)) &&
    (!f.nosal || salaryLpa(a.salary) != null) &&
    (age == null || (daysSince(a.posted_at || a.found_at, now) ?? Infinity) <= age) &&
    (!kinds.length || kinds.includes(a.fit_kind || (a.ai_score != null ? 'ai' : 'none'))) &&
    (!levels.length || levels.includes(levelOf(a.title))) &&
    (!only.length || only.some(c => (a.company || '').toLowerCase().includes(c))) &&
    (!never.length || !never.some(c => (a.company || '').toLowerCase().includes(c))) &&
    (!ats.length || ats.includes(atsOf(a))) &&
    (min == null || (fitOf(a) != null && fitOf(a) >= min)) &&
    (max == null || (fitOf(a) != null && fitOf(a) <= max)) &&
    (!q || `${a.title} ${a.company} ${a.location || ''}`.toLowerCase().includes(q)) &&
    (!f.within || nextStepWithin(a.next_step_at, f.within, now)) &&
    (!f.followups || due.has(a.job_id)) &&
    (!f.form || !!a.apply_url))
}

