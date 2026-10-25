// frontend/src/components/settings/RunnerTokensSection.jsx
//
// Runner tokens (/api/settings/runner-tokens, owner only): create one - the token is shown
// ONCE, right after creating it, and never again (only a hash is stored) - revoke one, and see
// which runners are online (seen in the last minute). The list itself is owner-only too.
'use client'
import { useCallback, useEffect, useState } from 'react'
import { api, send } from '@/lib/api'
import { copyText } from '@/lib/live'
import { Msg, Section } from './AutomationParts'

const when = s => (s ? s.replace('T', ' ').slice(0, 16) : 'never')

export default function RunnerTokensSection({ owner }) {
  const [rows, setRows] = useState(null)
  const [label, setLabel] = useState('')
  const [fresh, setFresh] = useState(null)          // {label, token}: shown once
  const [m, setM] = useState(null)
  const load = useCallback(() => {
    if (!owner) return
    api('/api/settings/runner-tokens').then(r => setRows(r.tokens)).catch(e => setM({ ok: false, text: e.message }))
  }, [owner])
  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t) }, [load])
  const create = async () => {
    setM(null)
    try { const r = await send('/api/settings/runner-tokens', 'POST', { label }); setFresh({ label: r.label, token: r.token }); setLabel(''); load() }
    catch (e) { setM({ ok: false, text: e.message }) }
  }
  const revoke = async t => {
    if (!confirm(`Revoke "${t.label}"? A runner using it stops working at once.`)) return
    try { await send(`/api/settings/runner-tokens/${t.id}`, 'DELETE'); setM({ ok: true, text: 'Revoked' }); load() } catch (e) { setM({ ok: false, text: e.message }) }
  }
  return (
    <Section id="runners" title="Runner tokens" owner={owner}
             hint="A runner applies from your own computer instead of this server. Give it a token; the token is shown only once.">
      {!owner ? <div className="m">Runner tokens can only be seen and managed on the owner account.</div> : <>
        {fresh && <div className="alert token-once" data-token-once>
          <div><b>Copy the token for “{fresh.label}” now — it will not be shown again.</b>
            <code className="token">{fresh.token}</code></div>
          <button className="btn sm" onClick={async () => { const ok = await copyText(fresh.token); setM({ ok, text: ok ? 'Token copied' : 'Could not copy' }) }}>Copy</button>
          <button className="btn sm" onClick={() => setFresh(null)}>I have saved it</button>
        </div>}
        {rows === null ? <div className="m">Loading…</div> : rows.length === 0 ? <div className="m">No tokens yet.</div> : (
          <table className="auto-table" aria-label="Runner tokens">
            <thead><tr><th>Label</th><th>Status</th><th>Created</th><th>Last seen</th><th /></tr></thead>
            <tbody>{rows.map(t => (
              <tr key={t.id} data-token={t.id} style={{ cursor: 'default' }}>
                <td><b>{t.label}</b></td>
                <td>{!t.active ? <span className="badge">revoked</span> : t.online ? <span className="badge b-done" data-online>online</span> : <span className="badge b-idle">offline</span>}</td>
                <td className="m">{when(t.created_at)}</td><td className="m">{when(t.last_seen_at)}</td>
                <td>{t.active && <button className="btn sm danger" onClick={() => revoke(t)}>Revoke</button>}</td>
              </tr>))}</tbody>
          </table>)}
        <div className="row auto-save">
          <input className="in" style={{ width: '16rem' }} placeholder="Label, e.g. My laptop" value={label} onChange={e => setLabel(e.target.value)} aria-label="Token label" />
          <button className="btn" disabled={!label.trim()} onClick={create}>Create token</button><Msg m={m} />
        </div>
      </>}
    </Section>
  )
}
