// frontend/src/lib/filters.js
//
// The Applications workspace's filters and their URL form - the contract other pages
// link in with (e.g. /applications?needs_you=1, ?stage=a,b&source=linkedin):
//   stage=a,b         one or more lifecycle stages (unknown names are ignored)
//   needs_you=1       adds needs_you          ready_for_review=1  adds ready_for_review
//   source=  title=   exact (source in any case)        search= (or q=)  substring of title/company/location
//   min= max=         score range, inclusive (an unscored job is outside any range)
//   view=board|list|review                    job=<job_id> is read by the page itself (opens its panel)
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
  }
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
  if (openId) p.set('job', openId)
  const s = p.toString()
  return s ? `?${s}` : ''
}

export const byScore = (a, b) => (b.ai_score ?? -1) - (a.ai_score ?? -1) || String(a.stage_entered_at).localeCompare(String(b.stage_entered_at))

export function applyFilters(apps, f) {
  const q = f.q.trim().toLowerCase()
  const min = f.min === '' ? null : Number(f.min), max = f.max === '' ? null : Number(f.max)
  return apps.filter(a =>
    (!f.stages.length || f.stages.includes(a.stage)) &&
    (!f.title || a.title === f.title) &&
    (!f.source || (a.source || '').toLowerCase() === f.source.toLowerCase()) &&
    (min == null || (a.ai_score != null && a.ai_score >= min)) &&
    (max == null || (a.ai_score != null && a.ai_score <= max)) &&
    (!q || `${a.title} ${a.company} ${a.location || ''}`.toLowerCase().includes(q)))
}

