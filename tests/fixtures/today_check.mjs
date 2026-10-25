// tests/fixtures/today_check.mjs
//
// Run by tests/test_today.py under Node, next to copies of frontend/src/lib/{stages,filters,insights}.js.
// Prints one JSON object of results; the Python test asserts on it.
import { applyFilters, filtersFrom, nextStepWithin, queryFrom } from './filters.mjs'
import { TODAY_LINKS, budget, countdown, medianView, rateView } from './insights.mjs'

const now = new Date(2026, 9, 25, 12, 0, 0)               // 25 Oct 2026, noon local
const iso = (h) => { const d = new Date(now.getTime() + h * 3600e3); const p = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}` }
const apps = [
  { job_id: 'soon', title: 'A', company: 'x', stage: 'technical_round', next_step_at: iso(20) },
  { job_id: 'far', title: 'B', company: 'x', stage: 'hiring_manager', next_step_at: iso(60) },
  { job_id: 'day', title: 'C', company: 'x', stage: 'recruiter_screen', next_step_at: '2026-10-26' },
  { job_id: 'past', title: 'D', company: 'x', stage: 'recruiter_screen', next_step_at: iso(-3) },
  { job_id: 'other', title: 'E', company: 'x', stage: 'submitted', next_step_at: iso(5) },
  { job_id: 'none', title: 'F', company: 'x', stage: 'technical_round', next_step_at: '' },
]
const linkIds = (href, ctx = {}) => applyFilters(apps, filtersFrom(new URLSearchParams(href.split('?')[1])), { now, ...ctx }).map(a => a.job_id).join(',')

console.log(JSON.stringify({
  within_soon: nextStepWithin(iso(20), 48, now), within_far: nextStepWithin(iso(60), 48, now),
  within_past: nextStepWithin(iso(-3), 48, now), within_today_date: nextStepWithin('2026-10-25', 48, now),
  within_day_after: nextStepWithin('2026-10-27', 48, now), within_3days: nextStepWithin('2026-10-28', 48, now),
  within_empty: nextStepWithin('', 48, now),
  interviews_link: linkIds(TODAY_LINKS.interviews),
  followups_link: linkIds(TODAY_LINKS.follow_ups, { dueIds: new Set(['far', 'other']) }),
  followups_link_none_loaded: linkIds(TODAY_LINKS.follow_ups),
  needs_you_link: filtersFrom(new URLSearchParams(TODAY_LINKS.needs_you.split('?')[1])).stages,
  review_link: (() => { const f = filtersFrom(new URLSearchParams(TODAY_LINKS.ready_for_review.split('?')[1])); return [f.stages, f.view] })(),
  within_bad: [filtersFrom(new URLSearchParams('next_step_within=0')).within, filtersFrom(new URLSearchParams('next_step_within=abc')).within,
               filtersFrom(new URLSearchParams('next_step_within=999')).within],
  roundtrip: queryFrom(filtersFrom(new URLSearchParams('next_step_within=48&followups=due&followups_extra=1')), ''),
  rate_num: rateView(40.9), rate_small: rateView('not enough data'), rate_null: rateView(null), rate_numeric_string: rateView('42'),
  median: [medianView(3), medianView(1), medianView(null), medianView('not enough data')],
  budget: [budget(3, 10), budget(12, 10), budget(4, 0)],
  countdown: [countdown(iso(1 + 1 / 60 + 5 / 3600), now), countdown(iso(0.05), now), countdown(iso(-1), now), countdown('', now), countdown('junk', now)],
}))
