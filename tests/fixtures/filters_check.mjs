// tests/fixtures/filters_check.mjs
//
// Run by tests/test_applications_ui.py under Node, next to copies of frontend/src/lib/stages.js
// and filters.js. Prints one JSON object of results; the Python test asserts on it.
import { filtersFrom, queryFrom, applyFilters, byScore, fitOf, salaryLpa, levelOf, workOf, atsOf, chipsFor, withoutChip, clearFilters } from './filters.mjs'
import { POST_SUBMISSION, PRE_SUBMISSION, STAGE_KEYS, reasonLabel, stageLabel } from './stages.mjs'
import { TABS, isNarrowed, shownStages, stagesForTab, tabFor } from './tabs.mjs'

const q = s => filtersFrom(new URLSearchParams(s))
const apps = [
  { job_id: 'a', title: 'Backend Engineer', company: 'Acme', location: 'Pune', source: 'LinkedIn', ai_score: 82, stage: 'needs_you', apply_url: 'https://boards.greenhouse.io/acme/jobs/1' },
  { job_id: 'b', title: 'Data Engineer', company: 'Globex', location: '', source: 'indeed', ai_score: 35, stage: 'ready_for_review' },
  { job_id: 'c', title: 'Backend Engineer', company: 'Initech', location: 'Remote', source: 'naukri', ai_score: null, stage: 'found' },
  { job_id: 'd', title: 'SRE', company: 'Hooli', location: 'Pune', source: 'linkedin', ai_score: 60, stage: 'offer' },
]
const ids = f => applyFilters(apps, f).map(a => a.job_id).join(',')

const now = new Date('2026-10-28T12:00:00Z')
const pool = [
  { job_id: 'p1', title: 'Senior AI Engineer', company: 'Postman', location: 'Bengaluru, India', source: 'linkedin', salary: '₹30L - ₹45L a year',
    posted_at: '2026-10-27', found_at: '2026-10-27T09:00:00', fit: 90, fit_kind: 'ai', ai_score: 90, stage: 'found', apply_url: 'https://boards.greenhouse.io/postman/jobs/1', url: 'https://www.linkedin.com/jobs/view/1' },
  { job_id: 'p2', title: 'AI Engineer (Remote)', company: 'EWOR', location: 'Remote, India', source: 'himalayas', salary: '',
    posted_at: '', found_at: '2026-10-20T09:00:00', fit: 70, fit_kind: 'quick', ai_score: null, stage: 'found', apply_url: '', url: 'https://himalayas.app/jobs/2' },
  { job_id: 'p3', title: 'Data Engineering Intern', company: 'Tata Consultancy Services', location: 'Pune, Hybrid', source: 'naukri', salary: '3-5 Lacs PA',
    posted_at: '2026-10-28', found_at: '2026-10-28T08:00:00', fit: null, fit_kind: null, ai_score: null, stage: 'found', apply_url: 'https://jobs.lever.co/tcs/3', url: 'https://www.naukri.com/3' },
  { job_id: 'p4', title: 'Backend Developer', company: 'Razorpay', location: 'Bengaluru', source: 'indeed', salary: '₹1,20,000 a month',
    posted_at: '2026-09-01', found_at: '2026-09-01T08:00:00', fit: 55, fit_kind: 'ai', ai_score: 55, stage: 'found', apply_url: '', url: 'https://in.indeed.com/4' },
]
const pick = query => applyFilters(pool, { ...q(query), stages: [] }, { now }).map(a => a.job_id)
const panel = {
  loc: pick('loc=bengaluru'), work_remote: pick('work=remote'), work_on: pick('work=onsite,hybrid'),
  sal12: pick('sal=12'), sal12_stated: pick('sal=12&nosal=1'), nosal: pick('nosal=1'),
  age3: pick('age=3'), age30: pick('age=30'),
  kind_ai: pick('kind=ai'), kind_none_quick: pick('kind=none,quick'),
  lvl: pick('level=senior,intern'), lvl_mid: pick('level=mid'),
  co: pick('co=postman,razor'), xco: pick('xco=tata'), src: pick('source=linkedin,indeed'),
  ats_gh: pick('ats=greenhouse'), ats_none: pick('ats=none'), range: pick('min=60&max=80'),
  all_none: pick(''),
  bad_params: q('work=space&kind=robot&level=ceo&age=5&sal=-3'),
  rt: queryFrom(q('loc=pune&work=remote,hybrid&sal=12&nosal=1&age=7&kind=ai&level=senior&co=a,b&xco=c&ats=lever&source=linkedin,indeed'), ''),
  chips: chipsFor(q('loc=pune&work=remote,hybrid&sal=12&age=1&min=60&max=80&kind=ai&co=Acme&xco=TCS&ats=greenhouse,none&form=1')).map(c => c.label),
  without_range: (() => { const f = q('min=60&max=80&loc=x'); const c = chipsFor(f).find(c => c.key === 'min'); const g = withoutChip(f, c); return [g.min, g.max, g.loc] })(),
  cleared: (() => { const g = clearFilters({ ...q('loc=x&work=remote&min=5&form=1&search=keep&stage=queued') }); return [g.loc, g.work, g.min, g.form, g.q, g.stages] })(),
  sal: ['₹10L - ₹15L a year', '12-18 LPA', '₹50,000 a month', '₹ 8,00,000 - 12,00,000 /year', '$120k', '', '3-5 Lacs PA', 'Not disclosed'].map(salaryLpa),
  lv: ['Senior AI Engineer', 'AI Engineering Intern', 'Junior Dev', 'Backend Engineer', 'Tech Lead'].map(levelOf),
  wk: [{ location: 'Remote, India' }, { location: 'Pune (Hybrid)' }, { location: 'Pune' }, { location: '', title: 'Engineer - Work from home' }].map(workOf),
  ats: [pool[0], pool[1], { apply_url: 'https://x.example/apply', url: 'https://x.example/apply' }].map(atsOf),
}
console.log(JSON.stringify({
  panel,
  needs_you: q('needs_you=1'),
  both: q('needs_you=1&ready_for_review=true').stages,
  stage_list: q('stage=found,offer,bogus').stages,
  search_alias: q('q=acme').q,
  search: q('search=pied&q=other').q,
  view_bad: q('view=gallery').view,
  view_review: q('view=review').view,
  roundtrip: queryFrom(q('search=x y&title=SRE&source=linkedin&stage=offer,found&min=40&max=90&view=board&form=1'), 'j1'),
  roundtrip_list: queryFrom(q('view=list'), ''),
  roundtrip_none: queryFrom(q(''), ''),
  form_flag: q('form=true').form,
  f_form: ids(q('form=1')),
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
  tab_stages: TABS.flatMap(t => t.stages),
  tab_for: { none: tabFor([]), found: tabFor(['found']), needs: tabFor(q('needs_you=1').stages), review: tabFor(['ready_for_review']),
             interviews: tabFor(['recruiter_screen', 'technical_round', 'hiring_manager']), offer: tabFor(['offer']),
             mixed: tabFor(['found', 'offer']), archive: tabFor(['skipped']) },
  stages_for: { inbox: stagesForTab('inbox'), applied: stagesForTab('applied'), nope: stagesForTab('nope') },
  shown_empty: shownStages([]), narrowed: isNarrowed(['offer', 'rejected']), whole: isNarrowed(POST_SUBMISSION),
  label: stageLabel('ready_for_review'), reason: reasonLabel('login_required'), reason_unknown: reasonLabel('some_new_reason'),
  // `fit` (AI score, else the quick keyword match) is what rows sort and filter by; old rows fall back to ai_score
  fit_order: [{ job_id: 'q', fit: 55, fit_kind: 'quick', ai_score: null }, { job_id: 'a', fit: 80, fit_kind: 'ai', ai_score: 80 },
              { job_id: 'n', fit: null, ai_score: null }, { job_id: 'o', ai_score: 60 }].sort(byScore).map(a => a.job_id),
  fit_min: applyFilters([{ job_id: 'q', fit: 55, ai_score: null, stage: 'found', title: 't', company: 'c' }], { ...q('min=50'), stages: [] }).length,
  fit_of_old: fitOf({ ai_score: 7 }),
}))
