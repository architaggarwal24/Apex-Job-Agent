// frontend/src/App.jsx
import { useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import { api } from './api.js'
import Login from './pages/Login.jsx'
import Dashboard from './pages/Dashboard.jsx'
import Profile from './pages/Profile.jsx'
import Jobs from './pages/Jobs.jsx'
import Settings from './pages/Settings.jsx'

export const THEMES = [
  { id: 'midnight', name: 'Midnight', colors: ['#0a0b14', '#1a1d30', '#7c83ff'] },
  { id: 'aurora',   name: 'Aurora',   colors: ['#06110f', '#12302b', '#2dd4bf'] },
  { id: 'sunset',   name: 'Sunset',   colors: ['#140b12', '#331a2a', '#fb7185'] },
  { id: 'daylight', name: 'Daylight', colors: ['#f4f5fb', '#eceefb', '#5b5ff0'] },
]

// Apply and Pipeline aren't rebuilt in React yet (deferred on purpose —
// see MULTI_USER_STATUS.md) — they still live on the backend's own pages.
const BACKEND = import.meta.env.DEV ? 'http://localhost:8000' : ''

export default function App() {
  const [theme, setTheme] = useState(localStorage.getItem('apex-theme') || 'midnight')
  const [auth, setAuth] = useState(null)   // null = still checking

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    localStorage.setItem('apex-theme', theme)
  }, [theme])

  const checkAuth = () => api('/api/auth/me').then(setAuth).catch(() => setAuth({ authenticated: false }))
  useEffect(() => { checkAuth() }, [])

  const logout = async () => { await fetch('/api/auth/logout', { method: 'POST' }); checkAuth() }

  if (auth === null) return null
  if (!auth.authenticated) return <Login onAuthed={checkAuth} signupOpen={auth.signup_open} />

  return (
    <div className="shell">
      <aside className="side">
        <div className="logo">⚡ Apex</div>
        <NavLink className="nav" to="/" end>Dashboard</NavLink>
        <NavLink className="nav" to="/profile">Profile</NavLink>
        <NavLink className="nav" to="/jobs">Jobs</NavLink>
        <a className="nav" href={`${BACKEND}/apply`}>Apply</a>
        <a className="nav" href={`${BACKEND}/tracking`}>Pipeline</a>
        <NavLink className="nav" to="/settings">Settings</NavLink>
        <div className="spacer" />
        <div className="sep">{auth.email}</div>
        <button className="nav" style={{ background: 'none', border: 0, cursor: 'pointer', textAlign: 'left' }} onClick={logout}>Sign out</button>
      </aside>
      <main className="main">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/profile" element={<Profile />} />
          <Route path="/jobs" element={<Jobs />} />
          <Route path="/settings" element={<Settings theme={theme} setTheme={setTheme} />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </div>
  )
}
