// tests/fixtures/prefs_check.mjs — run by tests/test_frontend_themes.py against a copy of frontend/src/lib/prefs.js
import { INIT_SCRIPT, applyPrefs, THEME_IDS, DEFAULT_PREFS, OPTIONS, loadPrefs } from './prefs.mjs'
import assert from 'node:assert/strict'

function fakeDom() {
  return { dataset: {}, style: {} }
}
function runInit(stored, sysDark = true) {
  const root = fakeDom()
  const document = { documentElement: root }
  const localStorage = { getItem: k => (k === 'apex-prefs' ? stored : null) }
  const matchMedia = q => ({ matches: q.includes('dark') ? sysDark : !sysDark })
  new Function('document', 'localStorage', 'matchMedia', INIT_SCRIPT)(document, localStorage, matchMedia)
  return root
}

// 1. nothing stored → light, no stray attributes, colour-scheme follows
let r = runInit(null)
assert.equal(r.dataset.theme, 'light'); assert.deepEqual(Object.keys(r.dataset), ['theme']); assert.equal(r.style.colorScheme, 'light')
assert.deepEqual(THEME_IDS, ['light', 'dark']); assert.equal(DEFAULT_PREFS.theme, 'light')

// 2. a full set of prefs is applied
r = runInit(JSON.stringify({ theme: 'dark', density: 'compact', size: 'lg', motion: 'reduce' }))
assert.deepEqual(r.dataset, { theme: 'dark', density: 'compact', size: 'lg', motion: 'reduce' }); assert.equal(r.style.colorScheme, 'dark')

// 3. "system" follows the OS
assert.equal(runInit(JSON.stringify({ theme: 'system' }), true).dataset.theme, 'dark')
assert.equal(runInit(JSON.stringify({ theme: 'system' }), false).dataset.theme, 'light')

// 4. defaults ("comfortable", "md", "system" motion) leave NO attribute behind
r = runInit(JSON.stringify({ theme: 'light', density: 'comfortable', size: 'md', motion: 'system' }))
assert.deepEqual(r.dataset, { theme: 'light' })

// 5. the old five themes (and junk) fall back to light instead of leaving the page unstyled
for (const old of ['graphite', 'paper', 'terminal', 'glass', 'brutal', '', null, 5, '__proto__']) {
  assert.equal(runInit(JSON.stringify({ theme: old })).dataset.theme, 'light', String(old))
}

// 6. tampered / corrupt storage can't inject anything
for (const evil of [
  { theme: '"><script>alert(1)</script>', density: '"><img src=x>' }, { size: 'huge' }, { density: 'x'.repeat(1000) },
  { radius: 'round', glow: 'off', accent: '#ff0000' },          // retired options are ignored
]) {
  r = runInit(JSON.stringify(evil))
  assert.equal(r.dataset.theme, 'light')
  for (const [k, v] of Object.entries(r.dataset)) { if (k !== 'theme') assert.fail(`unexpected data-${k}=${v} for ${JSON.stringify(evil)}`) }
}
for (const corrupt of ['{not json', '[]', '"str"', '42', 'null']) runInit(corrupt)   // must not throw
// no matchMedia at all (old browsers): still dark, never throws
new Function('document', 'localStorage', INIT_SCRIPT)({ documentElement: fakeDom() }, { getItem: () => '{"theme":"system"}' })

// 7. the init script and the runtime function are literally the same code
const a = fakeDom(), b = fakeDom(); const p = { theme: 'light', size: 'sm', motion: 'full' }
applyPrefs(p, a, THEME_IDS, true)
new Function('document', 'localStorage', 'matchMedia', INIT_SCRIPT)({ documentElement: b }, { getItem: () => JSON.stringify(p) }, () => ({ matches: true }))
assert.deepEqual(a.dataset, b.dataset)

// 8. loadPrefs merges over defaults, keeps only offered values, survives garbage
globalThis.localStorage = { getItem: () => JSON.stringify({ theme: 'dark', size: 'xl', accent: '#fff', density: 'nope' }) }
assert.deepEqual(loadPrefs(), { ...DEFAULT_PREFS, theme: 'dark', size: 'xl' })
globalThis.localStorage = { getItem: () => JSON.stringify({ theme: 'graphite' }) }
assert.deepEqual(loadPrefs(), DEFAULT_PREFS)
globalThis.localStorage = { getItem: () => '{bad' }
assert.deepEqual(loadPrefs(), DEFAULT_PREFS)
assert.deepEqual(OPTIONS.theme.map(([v]) => v), ['system', 'light', 'dark'])
console.log('ALL PREFS / PRE-PAINT SCRIPT CHECKS PASSED (' + INIT_SCRIPT.length + ' byte script)')
