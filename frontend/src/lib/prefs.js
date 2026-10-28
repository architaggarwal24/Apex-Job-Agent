// frontend/src/lib/prefs.js
//
// Appearance preferences. Plain module (no 'use client') because layout.jsx — a
// server component — builds the pre-paint script from it, and AppShell uses the
// very same applyPrefs() at runtime, so first paint and later changes can never
// disagree about what a preference means.
//
// One look in two variants: THEME_IDS are the variants the CSS defines
// ([data-theme='dark'|'light'] in globals.css); the `theme` preference may also be
// "system", which follows the OS light/dark setting.

export const THEME_IDS = ['light', 'dark']          // the first is the fallback

export const DEFAULT_PREFS = {
  theme: 'light',         // system | light | dark
  density: 'comfortable', // compact | comfortable | spacious
  size: 'md',             // sm | md | lg | xl   (text size; the UI is rem-based)
  motion: 'system',       // system | reduce | full
}

// Every option offered in Settings, in one place. Values double as the whitelist
// applyPrefs() enforces, so a hand-edited localStorage can't inject anything.
export const OPTIONS = {
  theme:   [['system', 'Follow system'], ['light', 'Light'], ['dark', 'Dark']],
  density: [['compact', 'Compact'], ['comfortable', 'Comfortable'], ['spacious', 'Spacious']],
  size:    [['sm', 'Small'], ['md', 'Default'], ['lg', 'Large'], ['xl', 'Extra large']],
  motion:  [['system', 'Follow system'], ['reduce', 'Reduce'], ['full', 'Full']],
}

// ── Must stay fully self-contained (no outer variables, no helpers, ES5 style): ──
// its source text is embedded verbatim in the pre-paint <script> below.
// `sysDark`: does the OS prefer dark (matchMedia), used when theme is "system".
export function applyPrefs(p, root, themeIds, sysDark) {
  p = p || {}
  var allowed = { density: ['compact', 'spacious'], size: ['sm', 'lg', 'xl'], motion: ['reduce', 'full'] }
  var t = p.theme
  if (t === 'system') t = sysDark === false ? 'light' : 'dark'
  root.dataset.theme = themeIds.indexOf(t) >= 0 ? t : themeIds[0]
  for (var k in allowed) {
    if (allowed[k].indexOf(p[k]) >= 0) root.dataset[k] = p[k]
    else delete root.dataset[k]               // comfortable / md / system = the default
  }
  root.style.colorScheme = root.dataset.theme
}

export const STORAGE_KEY = 'apex-prefs'

// Runs before first paint (see layout.jsx) so the saved look is applied with no flash.
export const INIT_SCRIPT =
  '(function(){try{var f=' + applyPrefs.toString() +
  ';var raw=localStorage.getItem(' + JSON.stringify(STORAGE_KEY) + ');' +
  'var d=typeof matchMedia==="function"?matchMedia("(prefers-color-scheme: dark)").matches:true;' +
  'f(raw?JSON.parse(raw):{},document.documentElement,' + JSON.stringify(THEME_IDS) + ',d)}catch(e){}})()'

export const systemPrefersDark = () =>
  typeof window === 'undefined' || typeof window.matchMedia !== 'function' ? true : window.matchMedia('(prefers-color-scheme: dark)').matches

export function loadPrefs() {
  try {
    const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}')
    const p = { ...DEFAULT_PREFS }
    if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
      for (const k of Object.keys(DEFAULT_PREFS)) {
        if (OPTIONS[k].some(([v]) => v === raw[k])) p[k] = raw[k]
      }
    }
    return p
  } catch { return { ...DEFAULT_PREFS } }
}

export function savePrefs(p) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(p)) } catch {}
}
