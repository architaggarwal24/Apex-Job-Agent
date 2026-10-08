// frontend/src/components/settings/GeneralTab.jsx
'use client'
import { useEffect, useState } from 'react'
import { api, send } from '@/lib/api'
import { THEMES, usePrefs } from '@/lib/theme'
import { ACCENTS, OPTIONS } from '@/lib/prefs'
import ThemePreview from './ThemePreview'

function Segmented({ options, value, onChange }) {
  return (
    <div className="pills" role="radiogroup">
      {options.map(([v, label]) => (
        <button key={v} role="radio" aria-checked={value === v} className={`pill ${value === v ? 'on' : ''}`} onClick={() => onChange(v)}>{label}</button>
      ))}
    </div>
  )
}

function Row({ title, hint, children }) {
  return (
    <div className="setting-row">
      <div className="what"><b>{title}</b>{hint && <div className="m">{hint}</div>}</div>
      <div>{children}</div>
    </div>
  )
}

function Appearance() {
  const { prefs, setPref, resetPrefs } = usePrefs()
  if (!prefs) return null
  return (
    <>
      <div className="card section">
        <h2>Theme</h2>
        <div className="themes">
          {THEMES.map(t => (
            <button key={t.id} className={`theme ${prefs.theme === t.id ? 'on' : ''}`} onClick={() => setPref('theme', t.id)} aria-pressed={prefs.theme === t.id}>
              <ThemePreview t={t} />
              <div className="nm">{t.name}</div>
              <div className="tg">{t.tagline}</div>
            </button>
          ))}
        </div>
      </div>

      <div className="card section">
        <h2>Appearance</h2>
        <Row title="Accent colour" hint="Overrides the theme's accent for buttons, links and highlights.">
          <div className="row" style={{ gap: 10 }}>
            <button className={`sw-btn none ${prefs.accent ? '' : 'on'}`} title="Theme default" aria-label="Theme default accent" onClick={() => setPref('accent', null)} />
            {ACCENTS.map(a => (
              <button key={a.hex} className={`sw-btn ${prefs.accent === a.hex ? 'on' : ''}`} style={{ background: a.hex }}
                title={a.name} aria-label={`${a.name} accent`} onClick={() => setPref('accent', a.hex)} />
            ))}
          </div>
        </Row>
        <Row title="Density" hint="How much breathing room tables, cards and forms get."><Segmented options={OPTIONS.density} value={prefs.density} onChange={v => setPref('density', v)} /></Row>
        <Row title="Corners" hint="Override the theme's corner rounding."><Segmented options={OPTIONS.radius} value={prefs.radius} onChange={v => setPref('radius', v)} /></Row>
        <Row title="Text size" hint="Scales text and spacing together."><Segmented options={OPTIONS.size} value={prefs.size} onChange={v => setPref('size', v)} /></Row>
        <Row title="Motion" hint="“Follow system” respects your OS reduce-motion setting."><Segmented options={OPTIONS.motion} value={prefs.motion} onChange={v => setPref('motion', v)} /></Row>
        <Row title="Background effects" hint="Gradients and frosted-glass blur. Turn off for a flatter, faster look."><Segmented options={OPTIONS.glow} value={prefs.glow} onChange={v => setPref('glow', v)} /></Row>
        <div className="row" style={{ marginTop: 16 }}>
          <button className="btn" onClick={resetPrefs}>Reset appearance options</button>
          <span className="m">Saved in this browser only. Keeps your chosen theme.</span>
        </div>
      </div>
    </>
  )
}

function Account() {
  const [me, setMe] = useState(null)
  const [f, setF] = useState({ current: '', next: '', again: '' })
  const [msg, setMsg] = useState(null)       // {ok: bool, text}
  const [busy, setBusy] = useState(false)
  useEffect(() => { api('/api/auth/me').then(setMe).catch(() => {}) }, [])

  const changePassword = async () => {
    if (f.next.length < 8) return setMsg({ ok: false, text: 'New password must be at least 8 characters' })
    if (f.next !== f.again) return setMsg({ ok: false, text: "The new passwords don't match" })
    setBusy(true); setMsg(null)
    try {
      await send('/api/auth/change-password', 'POST', { current_password: f.current, new_password: f.next })
      setF({ current: '', next: '', again: '' })
      setMsg({ ok: true, text: 'Password changed. Every other device has been signed out; this one stays signed in.' })
    } catch (e) { setMsg({ ok: false, text: e.message }) }
    setBusy(false)
  }

  const signOutEverywhere = async () => {
    if (!confirm('Sign out of every device? You will stay signed in here.')) return
    try { await send('/api/auth/logout-everywhere', 'POST'); setMsg({ ok: true, text: 'Signed out everywhere else.' }) }
    catch (e) { setMsg({ ok: false, text: e.message }) }
  }

  return (
    <div className="card section">
      <h2>Account</h2>
      <Row title="Signed in as"><span className="t">{me?.email || '…'}</span></Row>
      <div className="setting-row" style={{ display: 'block' }}>
        <div className="what" style={{ marginBottom: 12 }}><b>Change password</b>
          <div className="m">Changing it signs out every other device.</div></div>
        <div className="grid2" style={{ maxWidth: 640 }}>
          <div className="fg"><label>Current password</label>
            <input className="in" type="password" autoComplete="current-password" value={f.current} onChange={e => setF({ ...f, current: e.target.value })} /></div>
          <div />
          <div className="fg"><label>New password</label>
            <input className="in" type="password" autoComplete="new-password" value={f.next} onChange={e => setF({ ...f, next: e.target.value })} /></div>
          <div className="fg"><label>Repeat new password</label>
            <input className="in" type="password" autoComplete="new-password" value={f.again} onChange={e => setF({ ...f, again: e.target.value })}
              onKeyDown={e => e.key === 'Enter' && changePassword()} /></div>
        </div>
        <div className="row">
          <button className="btn primary" disabled={busy || !f.current || !f.next} onClick={changePassword}>{busy ? 'Changing…' : 'Change password'}</button>
          <button className="btn" onClick={signOutEverywhere}>Sign out of all devices</button>
          {msg && <span className={msg.ok ? 'msg-ok' : 'msg-err'}>{msg.text}</span>}
        </div>
      </div>
    </div>
  )
}

export default function GeneralTab() {
  return <><Appearance /><Account /></>
}
