// frontend/src/components/applications/HuntTab.jsx
//
// The card's hunt fields (PUT /api/applications/{id}/hunt - db/hunt.py: next step + date,
// contact, last contact), its stale indicator, and its follow-ups that are due now
// (/api/follow-ups/due, filtered to this job) with snooze / done / dismiss / regenerate.
'use client'
import { useEffect, useState } from 'react'
import { api, send } from '@/lib/api'
import { copyText } from '@/lib/live'

const split = v => {
  const s = v || ''
  return s.length > 10 ? [s.slice(0, 10), s.slice(11, 16)] : [s, '']
}
const KIND = { check_in: 'Check in', thank_you: 'Thank you', status_ask: 'Ask for news' }

function FollowUps({ jobId, flash }) {
  const [items, setItems] = useState(null)
  const [busy, setBusy] = useState(0)
  const load = () => api('/api/follow-ups/due').then(rows => setItems(rows.filter(f => f.job_id === jobId))).catch(e => { setItems([]); flash(false, e.message) })
  useEffect(() => { load() }, [jobId])
  const act = async (f, path, body, label) => {
    setBusy(f.id)
    try { await send(`/api/follow-ups/${f.id}/${path}`, 'POST', body); flash(true, label) } catch (e) { flash(false, e.message) }
    setBusy(0); load()
  }
  if (items === null) return <div className="m">Loading follow-ups…</div>
  if (!items.length) return <div className="m">No follow-ups due for this application right now.</div>
  return items.map(f => (
    <div key={f.id} className="pn-note">
      <div className="row"><b>{KIND[f.kind] || f.kind}</b><span className="m">due {String(f.due_at).replace('T', ' ').slice(0, 16)}</span>
        {f.status === 'snoozed' && <span className="badge">snoozed</span>}</div>
      <pre className="pn-draft">{f.draft_text || 'No draft yet - write your own, or regenerate one.'}</pre>
      <div className="row">
        {f.draft_text && <button className="btn sm" onClick={async () => { const ok = await copyText(f.draft_text); flash(ok, ok ? 'Draft copied' : 'Could not copy') }}>Copy draft</button>}
        <select className="in" style={{ width: 'auto' }} defaultValue="" disabled={busy === f.id} aria-label="Snooze"
                onChange={e => e.target.value && act(f, 'snooze', { days: Number(e.target.value) }, `Snoozed for ${e.target.value} day(s)`)}>
          <option value="">Snooze…</option><option value="1">1 day</option><option value="3">3 days</option><option value="7">7 days</option>
        </select>
        <button className="btn sm primary" disabled={busy === f.id} onClick={() => act(f, 'done', undefined, 'Marked done')}>Done</button>
        <button className="btn sm" disabled={busy === f.id} onClick={() => act(f, 'dismiss', undefined, 'Dismissed')}>Dismiss</button>
        <button className="btn sm" disabled={busy === f.id} onClick={() => act(f, 'regenerate', undefined, 'Draft rewritten')}>Regenerate draft</button>
      </div>
    </div>))
}

export default function HuntTab({ app, onChanged, flash }) {
  const [d, setD] = useState(() => {
    const [date, time] = split(app.next_step_at)
    return { next_step_text: app.next_step_text || '', date, time, contact_name: app.contact_name || '',
             contact_email: app.contact_email || '', last_contact_at: (app.last_contact_at || '').slice(0, 10) }
  })
  const [busy, setBusy] = useState(false)
  const set = (k, v) => setD(x => ({ ...x, [k]: v }))
  const save = async () => {
    setBusy(true)
    try {
      await send(`/api/applications/${app.job_id}/hunt`, 'PUT', {
        next_step_text: d.next_step_text, next_step_at: d.date ? (d.time ? `${d.date}T${d.time}` : d.date) : '',
        contact_name: d.contact_name, contact_email: d.contact_email, last_contact_at: d.last_contact_at })
      flash(true, 'Saved'); onChanged()
    } catch (e) { flash(false, e.message) }
    setBusy(false)
  }
  return (
    <>
      <div className="pn-sec">
        <div className="row">
          <span className="m">{app.days_in_stage != null ? `${app.days_in_stage} day(s) in this stage` : 'Not in a stage yet'}</span>
          {app.stale ? <span className="badge b-wait">stale — it has waited longer than this stage's limit</span> : <span className="badge b-done">on time</span>}
        </div>
      </div>
      <div className="fg"><label htmlFor="h-next">Next step</label>
        <input id="h-next" className="in" placeholder="e.g. Technical round with Dana" value={d.next_step_text} onChange={e => set('next_step_text', e.target.value)} /></div>
      <div className="grid2">
        <div className="fg"><label htmlFor="h-date">Date</label><input id="h-date" type="date" className="in" value={d.date} onChange={e => set('date', e.target.value)} /></div>
        <div className="fg"><label htmlFor="h-time">Time (optional)</label><input id="h-time" type="time" className="in" value={d.time} onChange={e => set('time', e.target.value)} disabled={!d.date} /></div>
        <div className="fg"><label htmlFor="h-cn">Contact name</label><input id="h-cn" className="in" value={d.contact_name} onChange={e => set('contact_name', e.target.value)} /></div>
        <div className="fg"><label htmlFor="h-ce">Contact email</label><input id="h-ce" type="email" className="in" value={d.contact_email} onChange={e => set('contact_email', e.target.value)} /></div>
        <div className="fg"><label htmlFor="h-lc">Last contact</label><input id="h-lc" type="date" className="in" value={d.last_contact_at} onChange={e => set('last_contact_at', e.target.value)} /></div>
      </div>
      <div className="row"><button className="btn primary" disabled={busy} onClick={save}>Save</button>
        <span className="hint">Setting a date also schedules a follow-up for when it passes.</span></div>
      <hr className="divider" />
      <div className="pn-h">Follow-ups due</div>
      <FollowUps jobId={app.job_id} flash={flash} />
    </>
  )
}
