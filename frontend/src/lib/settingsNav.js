// frontend/src/lib/settingsNav.js
//
// Settings, as five pages of folds (the page's menu and the command palette both read
// this list). A page is opened with /settings?s=<page>, one of its folds with &open=<fold>.
//
//   Start here     the four things to set, in order
//   AI model       providers · AI job scoring
//   Job search     job boards · job feeds · company boards · API keys
//   Applying       who submits · practice & pace · how many a day · skip these · answers ·
//                  resumes · where it runs · job-site logins · auto-submit (advanced)
//   You & alerts   appearance · account · notifications
//
// The old one-section links (?s=providers, ?s=logins, …) still land on the right fold.
// Plain JS for Node tests.
export const SETTINGS_PAGES = [
  ['start', 'Start here', 'The four things to set, in order. Everything else is optional.'],
  ['ai', 'AI model', 'The AI the agent thinks with, and what it uses it for.'],
  ['search', 'Job search', 'Where new jobs come from. The defaults need no logins and no keys.'],
  ['applying', 'Applying', 'How each application is filled, checked and sent.'],
  ['you', 'You & alerts', 'Your account, how Apex looks, and when it tells you things.'],
]

export const SETTINGS_FOLDS = {
  start: [],
  ai: [['providers', 'AI providers'], ['scoring', 'AI job scoring']],
  search: [['boards', 'Job boards'], ['sources', 'Job feeds'], ['companies', 'Company boards'], ['keys', 'API keys & Apify']],
  applying: [['modes', 'Who submits'], ['pace', 'Practice mode & pace'], ['limits', 'How many a day'], ['skip', 'Skip these'],
             ['answers', 'Answer bank'], ['resumes', 'Resumes'], ['runner', 'Where it runs'], ['logins', 'Job-site logins'],
             ['autonomy', 'Auto-submit (advanced)']],
  you: [['appearance', 'Appearance'], ['account', 'Account'], ['notifications', 'Notifications']],
}

// The old section keys (and any fold key) -> [page, fold].
const LEGACY = {
  agent: ['applying', 'pace'], blacklist: ['applying', 'skip'], runners: ['applying', 'runner'], apify: ['search', 'keys'],
}

export function resolveSettings(s, open) {
  if (SETTINGS_PAGES.some(([k]) => k === s)) {
    const folds = SETTINGS_FOLDS[s].map(([k]) => k)
    return { page: s, fold: folds.includes(open) ? open : '' }
  }
  if (LEGACY[s]) return { page: LEGACY[s][0], fold: LEGACY[s][1] }
  for (const [page, folds] of Object.entries(SETTINGS_FOLDS)) {
    if (folds.some(([k]) => k === s)) return { page, fold: s }
  }
  return { page: 'start', fold: '' }
}

export const settingsHref = (page, fold) => `/settings?s=${page}${fold ? `&open=${fold}` : ''}`

// Every place in Settings, for the command palette: [href, label, keywords].
export const SETTINGS_PLACES = SETTINGS_PAGES.flatMap(([p, label]) => [
  [settingsHref(p), `Settings › ${label}`, 'settings'],
  ...SETTINGS_FOLDS[p].map(([f, l]) => [settingsHref(p, f), `Settings › ${label} › ${l}`, `settings ${label}`]),
])
