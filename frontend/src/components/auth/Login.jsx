// frontend/src/components/auth/Login.jsx
//
// Rendered directly by AppShell (not a separate route/page) when
// GET /api/auth/me comes back unauthenticated. Keeping this inside the
// same app — rather than a standalone login.html — is the whole point
// of "one frontend": there's exactly one place this UI is defined.
'use client'
import { useState } from 'react'

export default function Login({ onAuthed, signupOpen }) {
  const [mode, setMode] = useState('login')
  const [email, setEmail] = useState('')
  const [pw, setPw] = useState('')
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    if (!email || !pw) { setMsg('Enter your email and password'); return }
    setBusy(true); setMsg('')
    try {
      const r = await fetch(`/api/auth/${mode}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ email, password: pw }),
      })
      const j = await r.json().catch(() => ({}))
      if (r.ok) { onAuthed(j); return }
      setMsg(j.detail || 'Something went wrong — try again')
    } catch { setMsg('Could not reach the server') }
    setBusy(false)
  }

  return (
    <div className="login-shell">
      <div className="card login-card">
        <div className="logo" style={{ marginBottom: 6 }}>⚡ Apex</div>
        <p className="sub" style={{ marginBottom: 22 }}>
          {mode === 'signup' ? 'Create your account' : 'Sign in to your account'}
        </p>

        <div className="fg">
          <label>Email</label>
          <input className="in" type="email" autoFocus value={email}
            onChange={e => setEmail(e.target.value)} />
        </div>
        <div className="fg">
          <label>Password</label>
          <input className="in" type="password" value={pw}
            autoComplete={mode === 'signup' ? 'new-password' : 'current-password'}
            onChange={e => setPw(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && submit()} />
          {mode === 'signup' && <div className="m" style={{ marginTop: 6 }}>At least 8 characters.</div>}
        </div>

        {msg && <div className="fg" style={{ color: 'var(--red)', fontSize: 13 }}>{msg}</div>}

        <button className="btn primary" style={{ width: '100%' }}
          disabled={busy} onClick={submit}>
          {busy ? '…' : mode === 'signup' ? 'Create account' : 'Sign in'}
        </button>

        {signupOpen && (
          <div className="m" style={{ textAlign: 'center', marginTop: 18 }}>
            {mode === 'signup' ? (
              <>Already have an account? <button className="link-btn" onClick={() => { setMode('login'); setMsg('') }}>Sign in</button></>
            ) : (
              <>No account? <button className="link-btn" onClick={() => { setMode('signup'); setMsg('') }}>Create one</button></>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
