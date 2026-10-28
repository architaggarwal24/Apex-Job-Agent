// frontend/src/lib/theme.js
'use client'
import { createContext, useContext } from 'react'

// Preview data for the appearance picker (Settings > General): the two variants of the
// one look. The colours MUST match globals.css — tests/test_frontend_themes.py enforces
// that, so the CSS can't change while its preview quietly keeps showing the old look.
export const THEMES = [
  { id: 'light', name: 'Light', tagline: 'Soft lavender, white cards, coral accent',
    bg: '#e9e7f3', bg2: '#e9e7f3', panel: '#ffffff', bdr: '#dcd8ea', txt: '#1d1830', mut: '#5a5474', acc: '#b8360f' },
  { id: 'dark', name: 'Dark', tagline: 'Deep aubergine, warm coral accent',
    bg: '#15121f', bg2: '#15121f', panel: '#1f1b2d', bdr: '#352f48', txt: '#f1eefb', mut: '#a9a2c4', acc: '#ff7d5e' },
]

export const PrefsContext = createContext({
  prefs: null, setPref: () => {}, resetPrefs: () => {},
})
export const usePrefs = () => useContext(PrefsContext)
