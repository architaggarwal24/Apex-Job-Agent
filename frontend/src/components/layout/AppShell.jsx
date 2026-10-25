// frontend/src/components/layout/AppShell.jsx
'use client'
import { useCallback, useEffect, useRef, useState } from 'react'
import Link from 'next/link'
import { usePathname } from 'next/navigation'
import { api } from '@/lib/api'
import { backendUrl } from '@/lib/backend'
import { PrefsContext } from '@/lib/theme'
import { DEFAULT_PREFS, THEME_IDS, applyPrefs, loadPrefs, savePrefs } from '@/lib/prefs'
import Login from '@/components/auth/Login'

// Order matters — this is the sidebar order. `legacy` items are still served
// by the FastAPI backend (see lib/backend.js), so they're plain anchors
// rather than client-side <Link>s. Applications replaces the old Jobs page and
// the backend's Apply and Pipeline pages.
const NAV = [
  { href: '/', label: 'Dashboard' },
  { href: '/profile', label: 'Profile' },
  { href: '/applications', label: 'Applications' },
  { href: '/settings', label: 'Settings' },
]

// With `trailingSlash` in the production export, pathname is "/jobs/".
const normalize = p => (p.length > 1 ? p.replace(/\/+$/, '') : p)

export default function AppShell({ children }) {
  const pathname = normalize(usePathname() || '/')
  const [auth, setAuth] = useState(null)     // null = still checking
  const [prefs, setPrefs] = useState(DEFAULT_PREFS)
  const prefsRef = useRef(prefs)

  // The pre-paint script in layout.jsx already applied the saved preferences to
  // <html>; read them back so the Settings controls agree with what's on screen.
  useEffect(() => { const p = loadPrefs(); prefsRef.current = p; setPrefs(p) }, [])

  const commit = useCallback(next => {
    prefsRef.current = next
    setPrefs(next)
    applyPrefs(next, document.documentElement, THEME_IDS)   // same function the pre-paint script runs
    savePrefs(next)
  }, [])
  const setPref = useCallback((key, value) => commit({ ...prefsRef.current, [key]: value }), [commit])
  // "Reset" restores the appearance OPTIONS; the chosen theme itself is kept.
  const resetPrefs = useCallback(() => commit({ ...DEFAULT_PREFS, theme: prefsRef.current.theme }), [commit])

  const checkAuth = useCallback(
    () => api('/api/auth/me').then(setAuth).catch(() => setAuth({ authenticated: false })), [])
  useEffect(() => { checkAuth() }, [checkAuth])

  const logout = async () => { await fetch('/api/auth/logout', { method: 'POST' }); checkAuth() }

  if (auth === null) return null
  if (!auth.authenticated) return <Login onAuthed={checkAuth} signupOpen={auth.signup_open} />

  return (
    <PrefsContext.Provider value={{ prefs, setPref, resetPrefs }}>
      <div className="shell">
        <aside className="side">
          <div className="logo">⚡ Apex</div>
          {NAV.map(n => n.legacy
            ? <a key={n.href} className="nav" href={backendUrl(n.href)}>{n.label}</a>
            : <Link key={n.href} className={`nav ${pathname === n.href ? 'active' : ''}`} href={n.href}>{n.label}</Link>)}
          <div className="spacer" />
          <div className="sep">{auth.email}</div>
          <button className="nav" style={{ background: 'none', border: 0, cursor: 'pointer', textAlign: 'left' }} onClick={logout}>Sign out</button>
        </aside>
        <main className="main">{children}</main>
      </div>
    </PrefsContext.Provider>
  )
}
