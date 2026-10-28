// frontend/src/components/auth/Login.jsx
//
// Rendered directly by AppShell (not a separate route/page) when
// GET /api/auth/me comes back unauthenticated. Keeping this inside the
// same app — rather than a standalone login.html — is the whole point
// of "one frontend": there's exactly one place this UI is defined.
'use client'
import { useEffect, useState } from 'react'
import { motion } from 'motion/react'
import { ArrowRight } from 'lucide-react'
import LogoMark from '@/components/layout/LogoMark'
import { EASE } from '@/lib/motion'

// The flow the agent runs, shown as the one moving thing on the page: the lit step
// walks along once, then rests on "you review".
const FLOW = ['search', 'score', 'fill', 'you review', 'submit']

export default function Login({ onAuthed, signupOpen }) {
  const [mode, setMode] = useState('login')
  const [email, setEmail] = useState('')
  const [pw, setPw] = useState('')
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState(false)
  const [lit, setLit] = useState(0)
  useEffect(() => {
    if (lit >= 3) return undefined
    const t = setTimeout(() => setLit(l => l + 1), 700)
    return () => clearTimeout(t)
  }, [lit])

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
      <section className="login-side" aria-label="About Apex">
        <div className="logo"><LogoMark /><span>Apex<small>Job agent</small></span></div>
        <div>
          <h1>Your job search, run for you. Nothing sent without you.</h1>
          <p className="lead">Apex finds postings, scores them against your profile, fills the forms and stops for your review before anything goes out.</p>
          <div className="login-flow" aria-hidden="true">
            {FLOW.map((s, i) => (
              <span key={s} className="row" style={{ gap: '.5rem' }}>
                <motion.span className={`st ${lit === i ? 'live' : ''}`} animate={{ scale: lit === i ? 1.04 : 1 }} transition={{ duration: 0.3, ease: EASE }}>{s}</motion.span>
                {i < FLOW.length - 1 && <ArrowRight className="lucide arr" />}
              </span>))}
          </div>
        </div>
        <p className="m">Runs on your machine. Your resume, answers and site logins stay here.</p>
      </section>

      <div className="login-main">
        <motion.div className="card login-card" initial={{ opacity: 0, y: 12 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.35, ease: EASE }}>
          <h2>{mode === 'signup' ? 'Create your account' : 'Welcome back'}</h2>
          <p className="sub" style={{ marginBottom: 'var(--sp3)' }}>
            {mode === 'signup' ? 'The first account becomes the owner.' : 'Sign in to pick up where you left off.'}
          </p>

          <div className="fg">
            <label htmlFor="login-email">Email</label>
            <input id="login-email" className="in" type="email" autoFocus value={email}
              onChange={e => setEmail(e.target.value)} />
          </div>
          <div className="fg">
            <label htmlFor="login-pw">Password</label>
            <input id="login-pw" className="in" type="password" value={pw}
              autoComplete={mode === 'signup' ? 'new-password' : 'current-password'}
              onChange={e => setPw(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && submit()} />
            {mode === 'signup' && <div className="hint">At least 8 characters.</div>}
          </div>

          {msg && <div className="fg msg-err" role="alert" style={{ fontSize: '.8125rem' }}>{msg}</div>}

          <button className="btn primary" style={{ width: '100%' }} disabled={busy} onClick={submit}>
            {busy ? 'Signing in…' : mode === 'signup' ? 'Create account' : 'Sign in'}
          </button>

          {signupOpen && (
            <div className="m" style={{ textAlign: 'center', marginTop: 'var(--sp3)' }}>
              {mode === 'signup' ? (
                <>Already have an account? <button className="link-btn" onClick={() => { setMode('login'); setMsg('') }}>Sign in</button></>
              ) : (
                <>No account? <button className="link-btn" onClick={() => { setMode('signup'); setMsg('') }}>Create one</button></>
              )}
            </div>
          )}
        </motion.div>
      </div>
    </div>
  )
}
