// tests/fixtures/settings_nav_check.mjs
//
// Run by tests/test_automation_tab.py under Node, next to a copy of frontend/src/lib/settingsNav.js.
import { SETTINGS_FOLDS, SETTINGS_PAGES, SETTINGS_PLACES, resolveSettings, settingsHref } from './settingsNav.mjs'

const old = ['appearance', 'account', 'providers', 'agent', 'boards', 'sources', 'companies', 'keys', 'limits', 'modes', 'autonomy',
  'answers', 'resumes', 'logins', 'runner', 'notifications', 'blacklist', 'runners', 'apify']
console.log(JSON.stringify({
  pages: SETTINGS_PAGES.map(([k]) => k),
  folds: Object.fromEntries(Object.entries(SETTINGS_FOLDS).map(([p, f]) => [p, f.map(([k]) => k)])),
  legacy: Object.fromEntries(old.map(k => [k, resolveSettings(k, null)])),
  page_and_fold: resolveSettings('applying', 'pace'), page_bad_fold: resolveSettings('applying', 'nope'),
  nothing: resolveSettings(null, null), junk: resolveSettings('<x>', 'y'),
  href: [settingsHref('ai'), settingsHref('applying', 'modes')],
  places: SETTINGS_PLACES.length, place0: SETTINGS_PLACES[0], place_fold: SETTINGS_PLACES.find(p => p[0].endsWith('open=runner')),
}))
