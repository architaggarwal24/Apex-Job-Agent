// tests/fixtures/filters_check.mjs
//
// Run by tests/test_applications_ui.py under Node, next to copies of frontend/src/lib/stages.js
// and filters.js. Prints one JSON object of results; the Python test asserts on it.
import { filtersFrom, queryFrom, applyFilters } from './filters.mjs'
import { POST_SUBMISSION, PRE_SUBMISSION, STAGE_KEYS, reasonLabel, stageLabel } from './stages.mjs'

const q = s => filtersFrom(new URLSearchParams(s))
const apps = [
  { job_id: 'a', title: 'Backend Engineer', company: 'Acme', location: 'Pune', source: 'LinkedIn', ai_score: 82, stage: 'needs_you' },
  { job_id: 'b', title: 'Data Engineer', company: 'Globex', location: '', source: 'indeed', ai_score: 35, stage: 'ready_for_review' },
  { job_id: 'c', title: 'Backend Engineer', company: 'Initech', location: 'Remote', source: 'naukri', ai_score: null, stage: 'found' },
  { job_id: 'd', title: 'SRE', company: 'Hooli', location: 'Pune', source: 'linkedin', ai_score: 60, stage: 'offer' },
]
const ids = f => applyFilters(apps, f).map(a => a.job_id).join(',')

console.log(JSON.stringify({
  needs_you: q('needs_you=1'),
  both: q('needs_you=1&ready_for_review=true').stages,
  stage_list: q('stage=found,offer,bogus').stages,
  search_alias: q('q=acme').q,
  search: q('search=pied&q=other').q,
  view_bad: q('view=gallery').view,
  view_review: q('view=review').view,
  roundtrip: queryFrom(q('search=x y&title=SRE&source=linkedin&stage=offer,found&min=40&max=90&view=list'), 'j1'),
  empty_query: queryFrom(q(''), ''),
  f_needs_you: ids(q('needs_you=1')),
  f_source_any_case: ids(q('source=linkedin')),
  f_title_exact: ids(q('title=Backend Engineer')),
  f_search_location: ids(q('search=pune')),
  f_score: ids(q('min=60&max=82')),
  f_min_only: ids(q('min=30')),
  f_combined: ids(q('stage=needs_you,offer&source=linkedin&min=70')),
  stages: STAGE_KEYS.length,
  post: POST_SUBMISSION, pre: PRE_SUBMISSION,
  label: stageLabel('ready_for_review'), reason: reasonLabel('login_required'), reason_unknown: reasonLabel('some_new_reason'),
}))
