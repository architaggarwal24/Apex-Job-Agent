// frontend/src/components/applications/ApplyTab.jsx
//
// What the old Apply page did for one job, by stage:
//   found / queued / failed / skipped  Start application (POST /api/apply/start), queue or
//                                      unqueue it, skip it
//   preparing                          the agent is working: progress + live log
//   needs_you                          its reason and the right action -
//                                        question         answer inline (POST /api/apply/answer),
//                                                         each answer remembered unless you untick it
//                                        captcha / login  open the site, then Retry
//                                        anything else    Retry, or the assist pack
//   ready_for_review                   the Review tab - or, in assist mode, the assist pack
//   assist pack                        copy-ready fields + mark applied / mark unavailable
// The live log is the apply session's WebSocket (/ws/{job_id}) plus its saved log.
'use client'
import { useEffect, useRef, useState } from 'react'
import { api, send } from '@/lib/api'
import { backendUrl } from '@/lib/backend'
import { copyText, safeHref } from '@/lib/live'
import { POST_SUBMISSION, reasonLabel } from '@/lib/stages'

const STEPS = [['analyzing', 'Analyze'], ['filling', 'Fill'], ['awaiting_input', 'Your input'], ['refilling', 'Re-fill'],
  ['screenshot_review', 'Review'], ['submitting', 'Submit'], ['done', 'Done']]
const WORKING = { analyzing: 'Analyzing the form…', filling: 'Filling form fields…', refilling: 'Filling the missing fields…', submitting: 'Submitting the application…' }

function wsUrl(jobId) {
  const u = backendUrl(`/ws/${jobId}`)
  if (/^https?:/.test(u)) return u.replace(/^http/, 'ws')
  return `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}${u}`
}

export function AssistPack({ app, onChanged, flash }) {
  const [pack, setPack] = useState(null)
  const [note, setNote] = useState('')
  const [reason, setReason] = useState('')
  useEffect(() => { api(`/api/applications/${app.job_id}/pack`).then(setPack).catch(e => flash(false, e.message)) }, [app.job_id])
  const record = async (path, body, label) => {
    try { await send(`/api/applications/${app.job_id}/${path}`, 'POST', body); flash(true, label); onChanged() } catch (e) { flash(false, e.message) }
  }
  const copy = async v => { const ok = await copyText(v); flash(ok, ok ? 'Copied' : 'Could not copy') }
  if (!pack) return <div className="m">Loading the answer pack…</div>
  const href = safeHref(pack.url)
  return (
    <div className="pn-sec">
      <div className="pn-h">Assist pack — apply on the site yourself, copying from top to bottom</div>
      <div className="row" style={{ marginBottom: '.5rem' }}>
        {href && <a className="btn primary" href={href} target="_blank" rel="noopener noreferrer">Open the site ↗</a>}
        <button className="btn sm" onClick={() => copy(pack.fields.filter(f => f.kind === 'text').map(f => `${f.label}: ${f.value}`).join('\n'))}>Copy all</button>
      </div>
      {pack.fields.length === 0 ? <div className="m">Nothing prepared yet.</div> : (
        <div className="pack">{pack.fields.map(fl => (
          <div key={fl.order} className="pack-row">
            <span className="m">{fl.order}.</span>
            <div className="pack-v"><div className="m">{fl.label}{fl.kind === 'file' ? ' (upload this file)' : ''}</div><div className="pn-text">{fl.value}</div></div>
            {fl.kind === 'text' && <button className="btn sm" onClick={() => copy(fl.value)} aria-label={`Copy ${fl.label}`}>Copy</button>}
          </div>))}</div>)}
      <div className="row" style={{ marginTop: 'var(--sp2)' }}>
        <input className="in grow" placeholder="Note (optional)" value={note} onChange={e => setNote(e.target.value)} />
        <button className="btn primary" onClick={() => record('mark-applied', { note }, 'Marked applied')}>Mark applied</button>
      </div>
      <div className="row" style={{ marginTop: '.5rem' }}>
        <input className="in grow" placeholder="Why it can't be applied to (required)" value={reason} onChange={e => setReason(e.target.value)} />
        <button className="btn danger" disabled={!reason.trim()} onClick={() => record('mark-unavailable', { reason }, 'Marked unavailable')}>Mark unavailable</button>
      </div>
    </div>
  )
}

function Answers({ app, session, onChanged, flash }) {
  const prompts = session?.missing_prompts || []
  const [vals, setVals] = useState(() => Object.fromEntries(prompts.map(p => [p.field_key, p.suggested || ''])))
  const [keep, setKeep] = useState(() => Object.fromEntries(prompts.map(p => [p.field_key, true])))
  const [busy, setBusy] = useState(false)
  if (!prompts.length) return <div className="m">No questions are waiting in this application's session. Retry to run it again.</div>
  const submit = async () => {
    const answers = Object.fromEntries(Object.entries(vals).filter(([, v]) => String(v).trim()).map(([k, v]) => [k, String(v).trim()]))
    const remember = Object.fromEntries(Object.keys(answers).map(k => [k, keep[k] ? 'auto' : 'once']))
    setBusy(true)
    try { await send(`/api/apply/answer/${app.job_id}`, 'POST', { answers, remember }); flash(true, `Sent ${Object.keys(answers).length} answer(s); the agent is filling them in`); onChanged() }
    catch (e) { flash(false, e.message) }
    setBusy(false)
  }
  return (
    <div className="pn-sec">
      <div className="pn-h">{prompts.length} question{prompts.length === 1 ? '' : 's'} for you</div>
      {prompts.map(p => (
        <div key={p.field_key} className="fg mf">
          <label htmlFor={`q-${p.id}`}>{p.label || p.field_key}</label>
          {p.placeholder && <div className="hint">Agent tried: “{p.placeholder}”</div>}
          {p.needs_confirmation && <div className="hint">Pre-filled from your saved answer — confirm or edit it.</div>}
          {p.options?.length
            ? <select id={`q-${p.id}`} className="in" value={vals[p.field_key] || ''} onChange={e => setVals({ ...vals, [p.field_key]: e.target.value })}>
                <option value="">Select…</option>{p.options.map(o => <option key={o}>{o}</option>)}</select>
            : <input id={`q-${p.id}`} className="in" value={vals[p.field_key] || ''} placeholder={`Enter ${p.label || p.field_key}…`}
                     onChange={e => setVals({ ...vals, [p.field_key]: e.target.value })} />}
          <label className="chk"><input type="checkbox" checked={!!keep[p.field_key]} onChange={e => setKeep({ ...keep, [p.field_key]: e.target.checked })} /> Remember this answer</label>
        </div>))}
      <div className="row"><button className="btn primary" disabled={busy} onClick={submit}>Submit &amp; continue</button></div>
    </div>
  )
}

export default function ApplyTab({ app, job, onChanged, flash, goReview }) {
  const [session, setSession] = useState(undefined)       // undefined = loading, null = none
  const [live, setLive] = useState([])
  const [busy, setBusy] = useState(false)
  const [showPack, setShowPack] = useState(false)
  const logRef = useRef(null)
  const jobId = app.job_id

  const loadSession = () => api(`/api/apply/status/${jobId}`).then(setSession).catch(() => setSession(null))
  useEffect(() => { loadSession() }, [jobId, app.stage, app.blocked_reason])

  useEffect(() => {
    let ws
    try { ws = new WebSocket(wsUrl(jobId)) } catch { return undefined }
    ws.onmessage = e => {
      let msg
      try { msg = JSON.parse(e.data) } catch { return }
      if (msg.event === 'ping') return
      const text = msg.event === 'log' ? msg.data : msg.event === 'state' ? `→ ${msg.data}` : msg.event === 'error' ? `❌ ${msg.data}`
        : msg.event === 'done' ? (msg.data?.dry_run ? 'Dry run complete — nothing was submitted' : '✅ Application submitted') : `• ${msg.event.replace(/_/g, ' ')}`
      setLive(l => [...l.slice(-200), `[${new Date().toLocaleTimeString()}] ${text}`])
      if (msg.event !== 'log') { loadSession(); onChanged() }
    }
    return () => { try { ws.close() } catch { /* already closed */ } }
  }, [jobId])
  useEffect(() => { if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight }, [live, session])

  const call = async (fn, ok) => { setBusy(true); try { await fn(); if (ok) flash(true, ok); onChanged(); loadSession() } catch (e) { flash(false, e.message) } setBusy(false) }
  const start = () => call(() => send(`/api/apply/start/${jobId}`, 'POST'), 'Started — the agent is opening the application')
  const skip = () => { if (confirm('Skip this job?')) call(() => send(`/api/apply/skip/${jobId}`, 'POST'), 'Skipped') }
  const queue = which => call(() => send(`/api/applications/${which}`, 'POST', { job_ids: [jobId] }).then(r => {
    if (r.rejected?.length) throw new Error(r.rejected[0].message || r.rejected[0].reason)
  }), which === 'queue' ? 'Queued' : 'Back to found')

  const st = session?.state
  const cur = STEPS.findIndex(([k]) => k === st)
  const assistReady = app.stage === 'ready_for_review' &&
    [...(app.events || [])].reverse().find(e => e.kind === 'transition' && e.to_stage === 'ready_for_review')?.detail?.assist
  const pre = !POST_SUBMISSION.includes(app.stage)
  const reason = app.blocked_reason
  const href = safeHref(app.url)

  return (
    <>
      {session && <div className="steps" aria-label="Progress">{STEPS.map(([k, l], i) =>
        <div key={k} className={`step ${i < cur ? 'done' : i === cur ? 'on' : ''}`}><i>{i + 1}</i><span>{l}</span></div>)}</div>}

      {app.stage === 'preparing' && <div className="working"><span className="sp" /> {WORKING[st] || 'The agent is working on this application…'}</div>}

      {app.stage === 'needs_you' && <div className="pn-sec">
        <div className="alert">⚠ {reasonLabel(reason)}{app.error_message ? ` — ${app.error_message}` : ''}</div>
        {reason === 'question' && session !== undefined && <Answers key={(session?.missing_prompts || []).map(p => p.id).join()} app={app} session={session} onChanged={() => { onChanged(); loadSession() }} flash={flash} />}
        {(reason === 'captcha' || reason === 'login_required') && <div className="row">
          {href && <a className="btn primary" href={href} target="_blank" rel="noopener noreferrer">Open the site ↗</a>}
          <span className="m">{reason === 'captcha' ? 'Solve the CAPTCHA there,' : 'Sign in there,'} then retry.</span>
          <button className="btn" disabled={busy} onClick={start}>Retry</button></div>}
        {reason === 'job_closed' && <div className="m">The posting looks closed — mark it unavailable below, or retry if it isn't.</div>}
        {!['question', 'captcha', 'login_required'].includes(reason) && <div className="row">
          <button className="btn" disabled={busy} onClick={start}>Retry</button>
          <button className="btn" onClick={() => setShowPack(true)}>Apply by hand (assist pack)</button></div>}
        {(showPack || ['unsupported_form', 'naukri_chat_screening', 'job_closed'].includes(reason)) && <AssistPack app={app} onChanged={onChanged} flash={flash} />}
      </div>}

      {app.stage === 'ready_for_review' && (assistReady
        ? <AssistPack app={app} onChanged={onChanged} flash={flash} />
        : <div className="pn-sec"><div className="row"><span>Ready for your review.</span><button className="btn primary" onClick={goReview}>Open review</button>
            <button className="btn" onClick={() => setShowPack(s => !s)}>Assist pack</button></div>
            {showPack && <AssistPack app={app} onChanged={onChanged} flash={flash} />}</div>)}

      {['found', 'queued', 'failed', 'skipped'].includes(app.stage) && <div className="pn-sec">
        <div className="pn-h">{app.stage === 'failed' ? 'This application failed' : app.stage === 'skipped' ? 'Skipped' : 'Ready to apply'}</div>
        <div className="m" style={{ marginBottom: '.5rem' }}>The agent opens the application form, fills every field it knows from your profile and answers, then asks you for anything it couldn't find. Works on any URL.</div>
        <div className="row">
          <button className="btn primary" disabled={busy} onClick={start}>{app.stage === 'failed' ? 'Retry' : 'Start application'}</button>
          {app.stage === 'found' && <button className="btn" disabled={busy} onClick={() => queue('queue')}>Add to queue</button>}
          {app.stage === 'queued' && <button className="btn" disabled={busy} onClick={() => queue('unqueue')}>Remove from queue</button>}
          <button className="btn" onClick={() => setShowPack(s => !s)}>Assist pack</button>
        </div>
        {showPack && <AssistPack app={app} onChanged={onChanged} flash={flash} />}
      </div>}

      {!pre && <div className="pn-sec"><div className="done-card">✅ Applied{app.applied_at ? ` on ${app.applied_at.slice(0, 10)}` : ''} — now at {app.stage.replace(/_/g, ' ')}.</div></div>}

      {pre && app.stage !== 'skipped' && <div className="row" style={{ marginBottom: 'var(--sp2)' }}>
        <button className="btn danger sm" disabled={busy} onClick={skip}>Skip this job</button></div>}

      <div className="pn-h">Log</div>
      <div className="log" ref={logRef} aria-live="polite">
        {(session?.log || []).slice(-30).map((l, i) => <div key={`s${i}`}>{l}</div>)}
        {live.map((l, i) => <div key={`l${i}`} className="log-live">{l}</div>)}
        {!session?.log?.length && !live.length && <div className="m">Nothing yet.</div>}
      </div>
      {job?.tailored_summary && <div className="pn-sec"><div className="pn-h">Tailored summary</div><div className="pn-text">{job.tailored_summary}</div></div>}
    </>
  )
}
