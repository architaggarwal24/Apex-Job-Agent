// frontend/src/lib/prefs.js
//
// Appearance preferences. Plain module (no 'use client') because layout.jsx — a
// server component — builds the pre-paint script from it, and AppShell uses the
// very same applyPrefs() at runtime, so first paint and later changes can never
// disagree about what a preference means.

export const THEME_IDS = ['graphite', 'paper', 'terminal', 'glass', 'brutal']

export const DEFAULT_PREFS = {
  theme: 'graphite',     // one of THEME_IDS
  accent: null,          // null = the theme's own accent; else a hex like '#6366f1'
  density: 'comfortable',// comfortable | compact | spacious
  radius: 'theme',       // theme | sharp | soft | round
  size: 'md',            // sm | md | lg | xl   (text size; the UI is rem-based)
  motion: 'system',      // system | reduce | full
  glow: 'theme',         // theme | off         (gradient / glass backdrop)
}

// Every option offered in Settings, in one place. Values double as the whitelist
// applyPrefs() enforces, so a hand-edited localStorage can't inject anything.
export const OPTIONS = {
  density: [['compact', 'Compact'], ['comfortable', 'Comfortable'], ['spacious', 'Spacious']],
  radius:  [['theme', 'Theme default'], ['sharp', 'Sharp'], ['soft', 'Soft'], ['round', 'Round']],
  size:    [['sm', 'Small'], ['md', 'Default'], ['lg', 'Large'], ['xl', 'Extra large']],
  motion:  [['system', 'Follow system'], ['reduce', 'Reduce'], ['full', 'Full']],
  glow:    [['theme', 'Theme default'], ['off', 'Off']],
}

// Chosen so each one clears 3:1 against BOTH the darkest and lightest theme
// surfaces (relative luminance 0.125–0.30) — verified by tests/test_frontend_themes.py.
export const ACCENTS = [
  { name: 'Indigo', hex: '#6366f1' }, { name: 'Blue',   hex: '#2f80ed' },
  { name: 'Teal',   hex: '#0d9488' }, { name: 'Green',  hex: '#16a34a' },
  { name: 'Amber',  hex: '#d97706' }, { name: 'Orange', hex: '#ea580c' },
  { name: 'Rose',   hex: '#e11d48' }, { name: 'Violet', hex: '#8b5cf6' },
]

// Readable text colour for a given background: whichever of black/white has the
// higher WCAG contrast. (For ANY colour the better of the two is ≥ 4.58:1.)
export function onColor(hex) {
  const lin = c => { c /= 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4) }
  const n = parseInt(hex.slice(1), 16)
  const L = 0.2126 * lin((n >> 16) & 255) + 0.7152 * lin((n >> 8) & 255) + 0.0722 * lin(n & 255)
  return 1.05 / (L + 0.05) >= (L + 0.05) / 0.05 ? '#ffffff' : '#000000'
}

// ── Must stay fully self-contained (no outer variables, no helpers, ES5 style): ──
// its source text is embedded verbatim in the pre-paint <script> below.
export function applyPrefs(p, root, themeIds) {
  p = p || {}
  var allowed = {
    density: ['compact', 'spacious'], radius: ['sharp', 'soft', 'round'],
    size: ['sm', 'lg', 'xl'], motion: ['reduce', 'full'], glow: ['off'],
  }
  root.dataset.theme = themeIds.indexOf(p.theme) >= 0 ? p.theme : themeIds[0]
  for (var k in allowed) {
    if (allowed[k].indexOf(p[k]) >= 0) root.dataset[k] = p[k]
    else delete root.dataset[k]               // comfortable / theme / md / system = the default
  }
  var a = p.accent
  if (typeof a === 'string' && /^#[0-9a-fA-F]{6}$/.test(a)) {
    var n = parseInt(a.slice(1), 16)
    var lin = function (c) { c = c / 255; return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4) }
    var L = 0.2126 * lin((n >> 16) & 255) + 0.7152 * lin((n >> 8) & 255) + 0.0722 * lin(n & 255)
    root.style.setProperty('--acc', a)
    root.style.setProperty('--on-acc', 1.05 / (L + 0.05) >= (L + 0.05) / 0.05 ? '#ffffff' : '#000000')
  } else {
    root.style.removeProperty('--acc')
    root.style.removeProperty('--on-acc')
  }
}

export const STORAGE_KEY = 'apex-prefs'

// Runs before first paint (see layout.jsx) so the saved look is applied with no flash.
export const INIT_SCRIPT =
  '(function(){try{var f=' + applyPrefs.toString() +
  ';var raw=localStorage.getItem(' + JSON.stringify(STORAGE_KEY) + ');' +
  'f(raw?JSON.parse(raw):{},document.documentElement,' + JSON.stringify(THEME_IDS) + ')}catch(e){}})()'

export function loadPrefs() {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}')
    return { ...DEFAULT_PREFS, ...(raw && typeof raw === 'object' ? raw : {}) }
  } catch { return { ...DEFAULT_PREFS } }
}

export function savePrefs(p) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(p)) } catch {}
}
