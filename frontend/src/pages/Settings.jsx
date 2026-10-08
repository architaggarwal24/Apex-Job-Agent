// frontend/src/pages/Settings.jsx
import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { THEMES } from '../App.jsx'

const TABS = ['General', 'Providers', 'Agent Behavior', 'Job Site Sessions']

// Server never returns a usable raw API key back to the client (by
// design — see server/routes/settings.py's /api/settings/raw/{key}
// docstring), only a masked "••••1234" once one is set. So there's
// nothing to "reveal" — the input just shows that mask as a placeholder,
// and typing replaces it. Leaving it untouched sends nothing for that
// key at all (see save()), matching the server's own skip-if-masked rule.
const isMasked = v => typeof v === 'string' && v.startsWith('••••')

export default function Settings({ theme, setTheme }) {
  const [tab, setTab] = useState('General')
  const [settings, setSettings] = useState(null)
  const [providers, setProviders] = useState(null)
  const [dirty, setDirty] = useState({})
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState('')
  const [testMsg, setTestMsg] = useState('')
  const [testing, setTesting] = useState(false)
  const [logins, setLogins] = useState(null)

  const load = () => api('/api/settings').then(r => setSettings(r.settings)).catch(() => {})
  useEffect(() => {
    load()
    api('/api/settings/providers').then(setProviders).catch(() => {})
    api('/api/settings/login-status').then(setLogins).catch(() => {})
  }, [])

  const val = k => dirty[k] ?? settings?.[k] ?? ''
  const setVal = (k, v) => setDirty(d => ({ ...d, [k]: v }))

  const save = async () => {
    // Never resend a masked placeholder as if it were a real value —
    // that's exactly the "••••1234" the server already knows to skip,
    // but there's no reason to round-trip it at all.
    const payload = Object.fromEntries(Object.entries(dirty).filter(([, v]) => !isMasked(v)))
    if (Object.keys(payload).length === 0) return
    setSaving(true); setMsg('')
    try {
      const r = await api('/api/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
      setMsg(`✓ Saved ${r.saved} setting${r.saved === 1 ? '' : 's'}`)
      setDirty({}); load()
    } catch (e) { setMsg('✗ ' + e.message) }
    setSaving(false)
  }

  const testLLM = async () => {
    setTesting(true); setTestMsg('Testing…')
    try {
      const r = await api('/api/settings/test-llm', { method: 'POST' })
      setTestMsg(`✓ ${r.provider} / ${r.model} replied: "${r.response}"`)
    } catch (e) { setTestMsg('✗ ' + e.message) }
    setTesting(false)
  }

  const clearCookies = async platform => {
    if (!confirm(`Clear the saved ${platform} session? You'll need to log in again next run.`)) return
    await fetch(`/api/settings/clear-cookies/${platform}`, { method: 'POST' })
    api('/api/settings/login-status').then(setLogins)
  }

  if (!settings || !providers) return <><h1>Settings</h1><p className="sub">Loading…</p></>

  const activeProvider = val('llm_provider')

  return (
    <>
      <h1>Settings</h1>
      <p className="sub">Configure the agent, its LLM providers, and how this looks.</p>

      <div className="tabs">
        {TABS.map(t => <button key={t} className={`tab ${tab === t ? 'on' : ''}`} onClick={() => setTab(t)}>{t}</button>)}
      </div>

      {tab === 'General' && (
        <div className="card">
          <h2>Theme</h2>
          <div className="themes">
            {THEMES.map(t => (
              <button key={t.id} className={`theme ${theme === t.id ? 'on' : ''}`} onClick={() => setTheme(t.id)}>
                <div className="sw">{t.colors.map(c => <i key={c} style={{ background: c }} />)}</div>
                <div className="t">{t.name}</div>
              </button>
            ))}
          </div>
        </div>
      )}

      {tab === 'Providers' && (
        <div className="card section">
          <div className="fg" style={{ maxWidth: 320 }}>
            <label>Temperature (0 = deterministic, 1 = creative)</label>
            <input className="in" type="number" step="0.1" min="0" max="1"
              value={val('llm_temperature')} onChange={e => setVal('llm_temperature', e.target.value)} />
          </div>

          {Object.entries(providers).map(([id, p]) => {
            const isActive = activeProvider === id
            const keyField = `${id}_api_key`, modelField = `${id}_model`, urlField = `${id}_base_url`
            const hasKey = p.key_label ? (settings[keyField] || dirty[keyField]) : true   // ollama needs no key
            return (
              <div key={id} className={`provider-row ${isActive ? 'active' : ''}`} style={{ flexDirection: 'column', alignItems: 'stretch' }}>
                <div className="row" style={{ marginBottom: p.key_label || p.models ? 12 : 0 }}>
                  <span className={`dot ${hasKey ? 'g' : 'm'}`} />
                  <b style={{ flex: 1 }}>{p.label}</b>
                  {isActive ? <span className="chip hi">Active</span> :
                    <button className="btn" onClick={() => setVal('llm_provider', id)}>Use this provider</button>}
                </div>
                <div className="grid2">
                  {p.key_label && (
                    <div className="fg">
                      <label>{p.key_label}</label>
                      <input className="in" type="password" placeholder={settings[keyField] || 'Not set'}
                        value={isMasked(val(keyField)) ? '' : val(keyField)}
                        onChange={e => setVal(keyField, e.target.value)} />
                    </div>
                  )}
                  {p.models?.length > 0 && (
                    <div className="fg">
                      <label>Model</label>
                      <select className="in" value={val(modelField)} onChange={e => setVal(modelField, e.target.value)}>
                        {!p.models.includes(settings[modelField]) && settings[modelField] &&
                          <option value={settings[modelField]}>{settings[modelField]} (custom)</option>}
                        {p.models.map(m => <option key={m} value={m}>{m}</option>)}
                      </select>
                    </div>
                  )}
                  {settings[urlField] !== undefined && (
                    <div className="fg">
                      <label>Base URL</label>
                      <input className="in" value={val(urlField)} onChange={e => setVal(urlField, e.target.value)} />
                    </div>
                  )}
                </div>
              </div>
            )
          })}

          <div className="row" style={{ marginTop: 6 }}>
            <button className="btn" disabled={testing} onClick={testLLM}>{testing ? 'Testing…' : 'Test connection'}</button>
            {testMsg && <span className="m">{testMsg}</span>}
          </div>
        </div>
      )}

      {tab === 'Agent Behavior' && (
        <div className="card section">
          <Toggle label="Dry Run Mode" hint="Fill forms but never actually submit — for safely testing the whole flow."
            checked={val('dry_run') === 'true'} onChange={v => setVal('dry_run', String(v))} />
          <Toggle label="Enable AI Job Scoring" hint="Use the LLM to score/rank jobs. Uses more API calls."
            checked={val('enable_llm_scoring') === 'true'} onChange={v => setVal('enable_llm_scoring', String(v))} />
          <div className="grid2" style={{ marginTop: 18 }}>
            <NumberField label="Daily Apply Limit" k="daily_apply_limit" val={val} setVal={setVal} />
            <NumberField label="Auto-Skip Score Threshold" k="auto_skip_score_threshold" val={val} setVal={setVal} />
            <NumberField label="Min Delay Between Applies (s)" k="min_delay_between_applies" val={val} setVal={setVal} />
            <NumberField label="Max Delay Between Applies (s)" k="max_delay_between_applies" val={val} setVal={setVal} />
          </div>
        </div>
      )}

      {tab === 'Job Site Sessions' && (
        <div className="card section">
          <p className="m" style={{ marginBottom: 16 }}>
            No passwords are stored anywhere — you log in once in the browser window the agent opens,
            and that session is kept (encrypted) from then on.
          </p>
          {['linkedin', 'indeed', 'naukri'].map(platform => {
            const s = logins?.[platform]
            const dotClass = { logged_in: 'g', expired: 'y', not_logged_in: 'm', unknown: 'r' }[s?.status] || 'm'
            return (
              <div key={platform} className="provider-row">
                <span className={`dot ${dotClass}`} />
                <b style={{ flex: 1, textTransform: 'capitalize' }}>{platform}</b>
                <span className="m" style={{ marginRight: 12 }}>{s?.message || '…'}</span>
                <button className="btn danger" onClick={() => clearCookies(platform)}>Clear session</button>
              </div>
            )
          })}
        </div>
      )}

      {tab !== 'General' && tab !== 'Job Site Sessions' && (
        <div className="save-bar">
          {msg && <span className="m" style={{ marginRight: 14, alignSelf: 'center' }}>{msg}</span>}
          <button className="btn" style={{ background: 'var(--acc)', color: '#fff', border: 0 }}
            disabled={saving || Object.keys(dirty).length === 0} onClick={save}>
            {saving ? 'Saving…' : `Save changes${Object.keys(dirty).length ? ` (${Object.keys(dirty).length})` : ''}`}
          </button>
        </div>
      )}
    </>
  )
}

function Toggle({ label, hint, checked, onChange }) {
  return (
    <div className="row" style={{ marginBottom: 14, alignItems: 'flex-start' }}>
      <div className={`toggle-sw ${checked ? 'on' : ''}`} onClick={() => onChange(!checked)}><i /></div>
      <div><div style={{ fontWeight: 600, fontSize: 14 }}>{label}</div><div className="m">{hint}</div></div>
    </div>
  )
}

function NumberField({ label, k, val, setVal }) {
  return (
    <div className="fg">
      <label>{label}</label>
      <input className="in" type="number" value={val(k)} onChange={e => setVal(k, e.target.value)} />
    </div>
  )
}
