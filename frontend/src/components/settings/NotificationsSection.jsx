// frontend/src/components/settings/NotificationsSection.jsx
//
// Notification channels (core/notifier.py): e-mail over SMTP and a Telegram bot, which events
// to tell you about, and quiet hours. GET/PUT /api/notifications/settings and the Test buttons
// (POST /api/notifications/test/{email|telegram}) are owner-only. Secrets - the SMTP password
// and the bot token - are never shown: only "set" / "not set". Typing one replaces it, leaving
// it blank keeps it, "Clear" removes it.
'use client'
import { useCallback, useEffect, useState } from 'react'
import { api, send } from '@/lib/api'
import { Msg, Section, Toggle } from './AutomationParts'

const SECRETS = ['notify_smtp_password', 'notify_telegram_bot_token']
const EVENTS = [['needs_you', 'An application needs you'], ['ready_for_review', 'Ready for review (a digest)'],
  ['follow_up_due', 'A follow-up is due'], ['interview_soon', 'An interview within 24 hours'], ['autonomy_submitted', 'Autonomy submitted one']]

function Field({ k, label, v, set, type = 'text', placeholder }) {
  return <div className="fg"><label htmlFor={k}>{label}</label>
    <input id={k} className="in" type={type} placeholder={placeholder} value={v[k] ?? ''} onChange={e => set(k, e.target.value)} /></div>
}

function Secret({ k, label, saved, v, set }) {
  const isSet = !!saved[k]
  return (
    <div className="fg"><label htmlFor={k}>{label} <span className={`badge ${isSet && v[k] !== null ? 'b-done' : 'b-idle'}`} data-secret={k}>
      {v[k] === null ? 'will be cleared' : isSet ? 'set' : 'not set'}</span></label>
      <div className="row">
        <input id={k} className="in grow" type="password" autoComplete="new-password" placeholder={isSet ? 'Leave blank to keep it' : 'Not set'}
               value={v[k] || ''} onChange={e => set(k, e.target.value)} />
        {isSet && <button className="btn sm" type="button" onClick={() => set(k, null)}>Clear</button>}
      </div></div>
  )
}

export default function NotificationsSection({ owner }) {
  const [saved, setSaved] = useState(null)
  const [v, setV] = useState({})
  const [m, setM] = useState(null)
  const [testing, setTesting] = useState('')
  const load = useCallback(() => {
    if (!owner) return
    api('/api/notifications/settings').then(s => { setSaved(s); setV({ ...s, ...Object.fromEntries(SECRETS.map(k => [k, ''])) }) })
      .catch(e => setM({ ok: false, text: e.message }))
  }, [owner])
  useEffect(() => { load() }, [load])
  const set = (k, x) => setV(o => ({ ...o, [k]: x }))

  // Only what changed; a secret only when typed (or cleared).
  const changes = () => {
    const out = {}
    for (const [k, x] of Object.entries(v)) {
      if (!k.startsWith('notify_')) continue
      if (SECRETS.includes(k)) { if (x === null || x) out[k] = x; continue }
      if (String(x ?? '') !== String(saved[k] ?? '')) out[k] = x
    }
    return out
  }
  const save = async () => {
    setM(null)
    try { const s = await send('/api/notifications/settings', 'PUT', changes()); setSaved(s); setV({ ...s, ...Object.fromEntries(SECRETS.map(k => [k, ''])) }); setM({ ok: true, text: 'Saved' }) }
    catch (e) { setM({ ok: false, text: e.message }) }
  }
  const test = async channel => {
    setTesting(channel); setM(null)
    const c = changes()
    try { await send(`/api/notifications/test/${channel}`, 'POST', Object.keys(c).length ? c : undefined); setM({ ok: true, text: `Test ${channel} message sent` }) }
    catch (e) { setM({ ok: false, text: e.message }) }
    setTesting('')
  }

  return (
    <Section id="notifications" title="Notifications" owner={owner}
             hint="Tell you by e-mail and/or Telegram when the agent needs you. At most one message of a kind every 10 minutes; nothing during quiet hours.">
      {!owner ? <div className="m">Notification channels deliver the owner's news and can only be seen and changed on the owner account.</div>
        : !saved ? <div className="m">Loading…</div> : <>
        <div className="row"><span className="m">Ready now:</span><b data-channels-ready>{saved.channels_ready.length ? saved.channels_ready.join(', ') : 'no channel'}</b></div>
        <h3 className="auto-h3">E-mail (SMTP)</h3>
        <Toggle label="Send e-mail" name="email enabled" checked={v.notify_email_enabled === 'true'} onChange={x => set('notify_email_enabled', String(x))} />
        <div className="grid2">
          <Field k="notify_smtp_host" label="SMTP host" v={v} set={set} placeholder="smtp.example.com" />
          <Field k="notify_smtp_port" label="Port" v={v} set={set} type="number" />
          <div className="fg"><label htmlFor="notify_smtp_security">Security</label>
            <select id="notify_smtp_security" className="in" value={v.notify_smtp_security} onChange={e => set('notify_smtp_security', e.target.value)}>
              <option value="starttls">STARTTLS</option><option value="ssl">SSL/TLS</option><option value="none">none</option></select></div>
          <Field k="notify_smtp_username" label="User name" v={v} set={set} />
          <Secret k="notify_smtp_password" label="Password" saved={saved} v={v} set={set} />
          <Field k="notify_email_from" label="From" v={v} set={set} type="email" placeholder="agent@example.com" />
          <Field k="notify_email_to" label="To" v={v} set={set} type="email" placeholder="you@example.com" />
        </div>
        <div className="row"><button className="btn sm" type="button" disabled={!!testing} onClick={() => test('email')}>{testing === 'email' ? 'Sending…' : 'Test e-mail'}</button></div>
        <h3 className="auto-h3">Telegram</h3>
        <Toggle label="Send Telegram messages" name="telegram enabled" checked={v.notify_telegram_enabled === 'true'} onChange={x => set('notify_telegram_enabled', String(x))} />
        <div className="grid2">
          <Secret k="notify_telegram_bot_token" label="Bot token" saved={saved} v={v} set={set} />
          <Field k="notify_telegram_chat_id" label="Chat id" v={v} set={set} placeholder="123456789 or @channel" />
        </div>
        <div className="row"><button className="btn sm" type="button" disabled={!!testing} onClick={() => test('telegram')}>{testing === 'telegram' ? 'Sending…' : 'Test Telegram'}</button></div>
        <h3 className="auto-h3">What to tell you</h3>
        {EVENTS.map(([k, l]) => <Toggle key={k} label={l} name={`notify ${k}`} checked={v[`notify_event_${k}`] === 'true'} onChange={x => set(`notify_event_${k}`, String(x))} />)}
        <h3 className="auto-h3">Quiet hours</h3>
        <div className="row">
          <input className="in" style={{ width: '9rem' }} type="time" value={v.notify_quiet_start || ''} onChange={e => set('notify_quiet_start', e.target.value)} aria-label="Quiet hours start" />
          <span className="m">to</span>
          <input className="in" style={{ width: '9rem' }} type="time" value={v.notify_quiet_end || ''} onChange={e => set('notify_quiet_end', e.target.value)} aria-label="Quiet hours end" />
          <button className="btn sm" type="button" onClick={() => { set('notify_quiet_start', ''); set('notify_quiet_end', '') }}>No quiet hours</button>
        </div>
        <div className="row auto-save"><button className="btn primary" disabled={!Object.keys(changes()).length} onClick={save}>Save</button><Msg m={m} /></div>
      </>}
    </Section>
  )
}
