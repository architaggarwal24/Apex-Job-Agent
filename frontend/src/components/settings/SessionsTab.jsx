// frontend/src/components/settings/SessionsTab.jsx
'use client'
import { useEffect, useState } from 'react'
import { api, send } from '@/lib/api'

const DOT = { logged_in: 'g', expired: 'y', not_logged_in: 'm', unknown: 'r' }

export default function SessionsTab() {
  const [logins, setLogins] = useState(null)
  const [err, setErr] = useState('')
  const load = () => api('/api/settings/login-status').then(setLogins).catch(e => setErr(e.message))
  useEffect(() => { load() }, [])

  const clear = async platform => {
    if (!confirm(`Clear the saved ${platform} session? You'll need to log in again next run.`)) return
    try { await send(`/api/settings/clear-cookies/${platform}`, 'POST'); setErr(''); load() } catch (e) { setErr(e.message) }
  }

  return (
    <div className="card section">
      <p className="m" style={{ marginBottom: 16 }}>
        No passwords are stored anywhere — you log in once in the browser window the agent opens,
        and that session is kept (encrypted) from then on.
      </p>
      {['linkedin', 'indeed', 'naukri'].map(platform => {
        const s = logins?.[platform]
        return (
          <div key={platform} className="provider-row row" style={{ flexWrap: 'nowrap' }}>
            <span className={`dot ${DOT[s?.status] || 'm'}`} />
            <b style={{ flex: 1, textTransform: 'capitalize' }}>{platform}</b>
            <span className="m" style={{ marginRight: 12 }}>{s?.message || '…'}</span>
            <button className="btn danger sm" onClick={() => clear(platform)}>Clear session</button>
          </div>
        )
      })}
      {err && <div className="msg-err" style={{ marginTop: 8 }}>{err}</div>}
    </div>
  )
}
