// frontend/src/lib/theme.js
'use client'
import { createContext, useContext } from 'react'

// Preview data for the theme gallery. The colours MUST match globals.css —
// tests/test_frontend_themes.py enforces that, so a theme can't be restyled in
// CSS while its preview quietly keeps showing the old look.
export const THEMES = [
  { id: 'graphite', name: 'Graphite', tagline: 'Minimal · dark',
    bg: '#0c0c0d', bg2: '#101011', panel: '#141415', bdr: '#2a2a2d', txt: '#ececee', mut: '#9a9aa1', acc: '#f2f2f3',
    radius: 10, bw: 1, shadow: 'none', font: 'ui-sans-serif, system-ui, sans-serif', head: 'ui-sans-serif, system-ui, sans-serif', headWeight: 650 },
  { id: 'paper', name: 'Paper', tagline: 'Minimal · light, editorial',
    bg: '#f6f3ec', bg2: '#efebe1', panel: '#fffdf8', bdr: '#ddd6c6', txt: '#23201b', mut: '#6b665a', acc: '#a8421a',
    radius: 6, bw: 1, shadow: '0 1px 0 rgba(35,32,27,.06)', font: 'ui-sans-serif, system-ui, sans-serif', head: 'Georgia, "Iowan Old Style", serif', headWeight: 600 },
  { id: 'terminal', name: 'Terminal', tagline: 'Monospace · phosphor green',
    bg: '#040704', bg2: '#060a06', panel: '#09100a', bdr: '#1f4a24', txt: '#d2f7d6', mut: '#7fb887', acc: '#3dff8a',
    radius: 0, bw: 1, shadow: 'none', font: 'ui-monospace, Menlo, Consolas, monospace', head: 'ui-monospace, Menlo, Consolas, monospace', headWeight: 700 },
  { id: 'glass', name: 'Glass', tagline: 'Modern · translucent mesh',
    bg: '#0a0620', bg2: 'rgba(10,6,32,.55)', panel: 'rgba(12,8,36,.72)', bdr: 'rgba(255,255,255,.2)', txt: '#f6f4ff', mut: '#cfcaee', acc: '#8f7bff',
    radius: 18, bw: 1, shadow: '0 8px 24px rgba(4,0,24,.45)', font: 'ui-sans-serif, system-ui, sans-serif', head: 'ui-sans-serif, system-ui, sans-serif', headWeight: 700,
    mesh: 'radial-gradient(120px 80px at 8% -10%, rgba(124,58,237,.7), transparent 70%), radial-gradient(110px 80px at 100% 0%, rgba(14,165,233,.55), transparent 70%), radial-gradient(100px 70px at 50% 115%, rgba(219,39,119,.55), transparent 70%)' },
  { id: 'brutal', name: 'Brutal', tagline: 'Bold · thick lines, hard shadows',
    bg: '#fff4c7', bg2: '#ffe27a', panel: '#ffffff', bdr: '#111111', txt: '#111111', mut: '#4a4637', acc: '#ff4f8b',
    radius: 8, bw: 2, shadow: '3px 3px 0 #111', font: 'ui-sans-serif, system-ui, sans-serif', head: 'ui-sans-serif, system-ui, sans-serif', headWeight: 850 },
]

export const PrefsContext = createContext({
  prefs: null, setPref: () => {}, resetPrefs: () => {},
})
export const usePrefs = () => useContext(PrefsContext)
