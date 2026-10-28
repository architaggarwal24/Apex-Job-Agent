// tests/fixtures/palette_check.mjs
//
// Run by tests/test_command_palette.py under Node, next to a copy of frontend/src/lib/palette.js.
// Prints one JSON object of results; the Python test asserts on it.
import { matchJobs, parseQueue, rank, score } from './palette.mjs'

const items = [
  { id: 'today', label: 'Today', keywords: 'home checklist' },
  { id: 'apps', label: 'Applications', keywords: 'jobs inbox' },
  { id: 'inbox', label: 'Applications › Inbox', keywords: 'jobs tab' },
  { id: 'keys', label: 'Settings › API keys', keywords: 'settings Job search' },
  { id: 'prov', label: 'Settings › AI providers', keywords: 'settings AI' },
  { id: 'run', label: 'Run a job search', keywords: 'start find new jobs' },
]
const ids = xs => xs.map(x => x.id)
const apps = [
  { job_id: '1', title: 'AI Engineer', company: 'Aquent', stage: 'found', fit: 91 },
  { job_id: '2', title: 'Senior AI Engineer', company: 'KLA', stage: 'found', fit: 79 },
  { job_id: '3', title: 'AI Engineer', company: 'Postman', stage: 'queued', fit: 95 },
  { job_id: '4', title: 'Data Engineer', company: 'Acme AI', stage: 'found', fit: 88 },
  { job_id: '5', title: 'AI Platform Engineer', company: 'Swiggy', stage: 'found', fit: null },
  { job_id: '6', title: 'ML Engineer', company: 'Cred', stage: 'found', ai_score: 85 },
  { job_id: '7', title: 'Retail Analyst', company: 'Paytm', stage: 'found', fit: 99 },
]
console.log(JSON.stringify({
  empty: ids(rank(items, '')),
  set: ids(rank(items, 'set')),
  api: ids(rank(items, 'api k')),
  prefix_first: ids(rank(items, 'app')),
  keyword: ids(rank(items, 'find')),
  none: ids(rank(items, 'zzz')),
  mid_word: score({ label: 'Applications' }, 'plic'),
  limit: rank(items, '', 2).length,
  pq: parseQueue('queue ai engineer 70'), pq_default: parseQueue('q ai'), pq_plus: parseQueue('queue backend 85+'),
  pq_fit: parseQueue('queue ai fit ≥ 60'), pq_clamp: parseQueue('queue ai 400'),
  pq_no: [parseQueue('queue'), parseQueue('queued jobs'), parseQueue('settings'), parseQueue('queue 80')],
  jobs80: ids(matchJobs(apps, ['ai'], 80).map(a => ({ id: a.job_id }))),
  jobs75: ids(matchJobs(apps, ['ai', 'engineer'], 75).map(a => ({ id: a.job_id }))),
  jobs_old: ids(matchJobs(apps, ['ml'], 80).map(a => ({ id: a.job_id }))),
}))
