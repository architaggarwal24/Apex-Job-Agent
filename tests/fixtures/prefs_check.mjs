// tests/fixtures/prefs_check.mjs — run by tests/test_frontend_themes.py against a copy of frontend/src/lib/prefs.js
import { INIT_SCRIPT, applyPrefs, THEME_IDS, DEFAULT_PREFS, ACCENTS, onColor, loadPrefs } from './prefs.mjs'
import assert from 'node:assert/strict'

function fakeDom() {
  const style = {}
  return { dataset: {}, style: { setProperty: (k, v) => { style[k] = v }, removeProperty: k => { delete style[k] }, _: style } }
}
function runInit(stored) {
  const root = fakeDom()
  const document = { documentElement: root }
  const localStorage = { getItem: k => (k === 'apex-prefs' ? stored : null) }
  new Function('document', 'localStorage', INIT_SCRIPT)(document, localStorage)
  return root
}

// 1. nothing stored → default theme, no stray attributes
let r = runInit(null)
assert.equal(r.dataset.theme, 'graphite'); assert.deepEqual(Object.keys(r.dataset), ['theme'])
assert.deepEqual(r.style._, {})

// 2. a full set of prefs is applied
r = runInit(JSON.stringify({ theme: 'terminal', density: 'compact', radius: 'sharp', size: 'lg', motion: 'reduce', glow: 'off', accent: '#e11d48' }))
assert.deepEqual(r.dataset, { theme: 'terminal', density: 'compact', radius: 'sharp', size: 'lg', motion: 'reduce', glow: 'off' })
assert.equal(r.style._['--acc'], '#e11d48'); assert.equal(r.style._['--on-acc'], '#ffffff')

// 3. defaults ("comfortable", "theme", "md", "system") leave NO attribute behind
r = runInit(JSON.stringify({ theme: 'paper', density: 'comfortable', radius: 'theme', size: 'md', motion: 'system', glow: 'theme', accent: null }))
assert.deepEqual(r.dataset, { theme: 'paper' })

// 4. an old/unknown theme id (e.g. the previous "midnight") falls back instead of leaving the page unstyled
for (const old of ['midnight', 'aurora', 'sunset', 'daylight', '', null, 5, '__proto__']) {
  assert.equal(runInit(JSON.stringify({ theme: old })).dataset.theme, 'graphite', String(old))
}

// 5. tampered / corrupt storage can't inject anything
for (const evil of [
  { theme: '"><script>alert(1)</script>', density: '"><img src=x>', accent: 'red; background:url(//evil)' },
  { accent: '#12345' }, { accent: '#12345g' }, { accent: 'javascript:alert(1)' }, { accent: ['#112233'] },
  { size: 'huge' }, { density: 'x'.repeat(1000) },
]) {
  r = runInit(JSON.stringify(evil))
  assert.equal(r.dataset.theme, 'graphite')
  for (const [k, v] of Object.entries(r.dataset)) { if (k !== 'theme') assert.fail(`unexpected data-${k}=${v} for ${JSON.stringify(evil)}`) }
  assert.deepEqual(r.style._, {}, JSON.stringify(evil))
}
for (const corrupt of ['{not json', '[]', '"str"', '42', 'null']) runInit(corrupt)   // must not throw

// 6. the init script and the runtime function are literally the same code
const a = fakeDom(), b = fakeDom(); const p = { theme: 'glass', radius: 'round', accent: '#2f80ed', glow: 'off' }
applyPrefs(p, a, THEME_IDS)
new Function('document', 'localStorage', INIT_SCRIPT)({ documentElement: b }, { getItem: () => JSON.stringify(p) })
assert.deepEqual(a.dataset, b.dataset); assert.deepEqual(a.style._, b.style._)

// 7. switching accent back to "theme default" removes the override
const c = fakeDom(); applyPrefs({ accent: '#6366f1' }, c, THEME_IDS); assert.ok(c.style._['--acc'])
applyPrefs({ accent: null }, c, THEME_IDS); assert.deepEqual(c.style._, {})

// 8. onColor: always picks the better of black/white, and that is always >= 4.5:1 for every offered accent
const lum = h => { const n = parseInt(h.slice(1), 16), l = c => { c /= 255; return c <= .03928 ? c / 12.92 : ((c + .055) / 1.055) ** 2.4 }; return .2126 * l(n >> 16 & 255) + .7152 * l(n >> 8 & 255) + .0722 * l(n & 255) }
const contrast = (L1, L2) => (Math.max(L1, L2) + .05) / (Math.min(L1, L2) + .05)
for (const { name, hex } of ACCENTS) {
  const on = onColor(hex), Lon = on === '#ffffff' ? 1 : 0
  assert.ok(contrast(lum(hex), Lon) >= 4.5, `${name}: ${contrast(lum(hex), Lon).toFixed(2)}`)
}
assert.equal(onColor('#ffffff'), '#000000'); assert.equal(onColor('#000000'), '#ffffff')

// 9. loadPrefs merges over defaults and survives garbage
globalThis.localStorage = { getItem: () => JSON.stringify({ theme: 'paper' }) }
assert.deepEqual(loadPrefs(), { ...DEFAULT_PREFS, theme: 'paper' })
globalThis.localStorage = { getItem: () => '{bad' }
assert.deepEqual(loadPrefs(), DEFAULT_PREFS)
console.log('ALL PREFS / PRE-PAINT SCRIPT CHECKS PASSED (' + INIT_SCRIPT.length + ' byte script)')
