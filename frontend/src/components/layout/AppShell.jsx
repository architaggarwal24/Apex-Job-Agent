// frontend/src/components/layout/AppShell.jsx
'use client'
import { useCallback, useEffect, useRef, useState } from 'react'
import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { AnimatePresence, motion } from 'motion/react'
import { BarChart3, KanbanSquare, LogOut, Moon, Search, Settings2, Sun, Sunrise, UserRound } from 'lucide-react'
import { api } from '@/lib/api'
import { PrefsContext } from '@/lib/theme'
import { DEFAULT_PREFS, THEME_IDS, applyPrefs, loadPrefs, savePrefs, systemPrefersDark } from '@/lib/prefs'
import { Motion, spring } from '@/lib/motion'
import Login from '@/components/auth/Login'
import LogoMark from './LogoMark'
import { SearchBar } from './SearchProgress'
import CommandPalette from './CommandPalette'
import { useToast } from '@/lib/toast'

// Order matters — this is the sidebar order. The old /jobs, /apply, /tracking
// and /setup URLs redirect to these pages (server/static_shell.py LEGACY_REDIRECTS).
const NAV = [
  { href: '/', label: 'Today' },
  { href: '/applications', label: 'Applications' },
  { href: '/insights', label: 'Insights' },
  { href: '/profile', label: 'Profile' },
  { href: '/settings', label: 'Settings' },
]
const ICONS = { '/': Sunrise, '/applications': KanbanSquare, '/insights': BarChart3, '/profile': UserRound, '/settings': Settings2 }
// Pages laid out edge to edge at full height (their own panes scroll), not as a centred column.
const FULL_BLEED = ['/applications']

// With `trailingSlash` in the production export, pathname is "/applications/".
const normalize = p => (p.length > 1 ? p.replace(/\/+$/, '') : p)

export default function AppShell({ children }) {
  const pathname = normalize(usePathname() || '/')
  const [auth, setAuth] = useState(null)     // null = still checking
  const [prefs, setPrefs] = useState(DEFAULT_PREFS)
  const prefsRef = useRef(prefs)

  const apply = p => applyPrefs(p, document.documentElement, THEME_IDS, systemPrefersDark())   // same function the pre-paint script runs

  // The pre-paint script in layout.jsx already applied the saved preferences to
  // <html>; read them back so the Settings controls agree with what's on screen.
  useEffect(() => { const p = loadPrefs(); prefsRef.current = p; setPrefs(p) }, [])
  // "Follow system" follows the OS while the page is open.
  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return undefined
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    const on = () => { if (prefsRef.current.theme === 'system') apply(prefsRef.current) }
    mq.addEventListener?.('change', on)
    return () => mq.removeEventListener?.('change', on)
  }, [])

  const commit = useCallback(next => {
    prefsRef.current = next
    setPrefs(next)
    apply(next)
    savePrefs(next)
  }, [])
  const setPref = useCallback((key, value) => commit({ ...prefsRef.current, [key]: value }), [commit])
  // "Reset" restores the appearance OPTIONS; the chosen theme itself is kept.
  const resetPrefs = useCallback(() => commit({ ...DEFAULT_PREFS, theme: prefsRef.current.theme }), [commit])

  const checkAuth = useCallback(
    () => api('/api/auth/me').then(setAuth).catch(() => setAuth({ authenticated: false })), [])
  useEffect(() => { checkAuth() }, [checkAuth])

  const logout = async () => { await fetch('/api/auth/logout', { method: 'POST' }); checkAuth() }

  // Ctrl+K / ⌘K opens the command palette from anywhere, even while typing.
  const [palette, setPalette] = useState(false)
  useEffect(() => {
    const onKey = e => { if ((e.ctrlKey || e.metaKey) && !e.altKey && e.key.toLowerCase() === 'k') { e.preventDefault(); setPalette(p => !p) } }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  const note = useToast()
  const closePalette = useCallback(() => setPalette(false), [])

  if (auth === null) return null
  if (!auth.authenticated) return <Motion motion={prefs.motion}><Login onAuthed={checkAuth} signupOpen={auth.signup_open} /></Motion>

  const initial = (auth.email || '?').trim()[0] || '?'
  const shown = typeof document !== 'undefined' ? document.documentElement.dataset.theme : 'dark'
  const toggleTheme = () => setPref('theme', shown === 'dark' ? 'light' : 'dark')
  const full = FULL_BLEED.includes(pathname)
  return (
    <PrefsContext.Provider value={{ prefs, setPref, resetPrefs }}>
      <Motion motion={prefs.motion}>
        <div className="shell">
          <aside className="side">
            <div className="logo"><LogoMark /><span>Apex</span></div>
            <button className="side-search" onClick={() => setPalette(true)} aria-label="Open the command palette (Ctrl+K)">
              <Search className="lucide" aria-hidden="true" /><span>Jump to…</span><kbd>Ctrl K</kbd></button>
            <nav className="side-nav" aria-label="Main">
              {NAV.map(n => {
                const Icon = ICONS[n.href]
                const on = pathname === n.href
                return (
                  <Link key={n.href} className={`nav ${on ? 'active' : ''}`} href={n.href} aria-current={on ? 'page' : undefined}>
                    {on && <motion.span layoutId="nav-pill" className="nav-pill" transition={spring} aria-hidden="true" />}
                    <Icon className="lucide" aria-hidden="true" /><span>{n.label}</span>
                  </Link>)
              })}
            </nav>
            <div className="spacer" />
            <div className="who">
              <span className="av" aria-hidden="true">{initial}</span>
              <span className="em" title={auth.email}>{auth.email}</span>
              <button className="out" onClick={toggleTheme} title={shown === 'dark' ? 'Switch to light' : 'Switch to dark'}
                      aria-label={shown === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}>
                {shown === 'dark' ? <Sun className="lucide" /> : <Moon className="lucide" />}</button>
              <button className="out" onClick={logout} title="Sign out" aria-label="Sign out"><LogOut className="lucide" /></button>
            </div>
          </aside>
          <main className={`main ${full ? 'full' : ''}`}><SearchBar />{children}</main>
        </div>
        <CommandPalette open={palette} onClose={closePalette} owner={!!auth.owner} theme={shown}
                        onTheme={t => setPref('theme', t)} onSignOut={logout} />
        <AnimatePresence>{note && <motion.div key={note.at} className={`toast app-toast ${note.ok ? 'ok' : 'err'}`} role="status" data-app-toast
          initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, transition: { duration: 0.12 } }}>{note.text}</motion.div>}</AnimatePresence>
      </Motion>
    </PrefsContext.Provider>
  )
}
