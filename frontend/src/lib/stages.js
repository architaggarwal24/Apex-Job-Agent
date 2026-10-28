// frontend/src/lib/stages.js
//
// Names and labels for the lifecycle stages (db/lifecycle.py STAGES) and the
// needs_you reasons (BLOCKED_REASONS). Display only: which MOVES are legal is the
// backend's business - a refused move comes back with the server's own message.

export const STAGES = [
  ['found', 'Found'], ['queued', 'Queued'], ['preparing', 'Preparing'], ['needs_you', 'Needs you'],
  ['ready_for_review', 'Ready for review'], ['submitted', 'Submitted'], ['recruiter_screen', 'Recruiter screen'],
  ['technical_round', 'Technical round'], ['hiring_manager', 'Hiring manager'], ['offer', 'Offer'],
  ['rejected', 'Rejected'], ['withdrawn', 'Withdrawn'], ['skipped', 'Skipped'], ['failed', 'Failed'],
]
export const STAGE_KEYS = STAGES.map(([k]) => k)
export const stageLabel = k => (STAGES.find(([s]) => s === k) || [k, k])[1]

// "post-submission": the application went out. Cards may be dragged between these columns only.
export const POST_SUBMISSION = ['submitted', 'recruiter_screen', 'technical_round', 'hiring_manager', 'offer', 'rejected', 'withdrawn']
export const PRE_SUBMISSION = ['found', 'queued', 'preparing', 'needs_you', 'ready_for_review']

export const STAGE_COLOR = {
  found: 'var(--mut)', queued: 'var(--mut)', preparing: 'var(--acc2)', needs_you: 'var(--ylw)',
  ready_for_review: 'var(--acc2)', submitted: '#6366f1', recruiter_screen: '#0ea5e9', technical_round: '#8b5cf6',
  hiring_manager: '#f59e0b', offer: 'var(--grn)', rejected: 'var(--red)', withdrawn: 'var(--mut)',
  skipped: 'var(--mut)', failed: 'var(--red)',
}

export const REASONS = {
  question: 'Has questions for you', captcha: 'CAPTCHA', login_required: 'Login required',
  unsupported_form: 'Unsupported form', naukri_chat_screening: 'Naukri chat screening',
  validation_error: 'Form validation error', job_closed: 'Job closed', other: 'Needs a look',
}
export const reasonLabel = r => REASONS[r] || (r ? String(r).replace(/_/g, ' ') : '')

export const scoreClass = s => s == null ? '' : s >= 70 ? 'hi' : s >= 40 ? 'mid' : 'lo'

// Where an application is made when the job links out to a company form: the
// ATS's name for a known host ("greenhouse"), else the bare host.
const ATS_HOSTS = [
  ['greenhouse.io', 'greenhouse'], ['lever.co', 'lever'], ['ashbyhq.com', 'ashby'],
  ['myworkdayjobs.com', 'workday'], ['myworkdaysite.com', 'workday'], ['smartrecruiters.com', 'smartrecruiters'],
  ['workable.com', 'workable'], ['recruitee.com', 'recruitee'], ['teamtailor.com', 'teamtailor'],
  ['personio.de', 'personio'], ['personio.com', 'personio'], ['breezy.hr', 'breezy'], ['pinpointhq.com', 'pinpoint'],
  ['bamboohr.com', 'bamboohr'], ['applytojob.com', 'jazzhr'], ['rippling.com', 'rippling'], ['icims.com', 'icims'],
]
export const applyHost = url => {
  let host = ''
  try { host = new URL(url).hostname.toLowerCase().replace(/^www\./, '') } catch { return '' }
  const hit = ATS_HOSTS.find(([h]) => host === h || host.endsWith('.' + h))
  return hit ? hit[1] : host
}
