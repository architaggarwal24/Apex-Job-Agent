// frontend/src/components/applications/ReviewPanel.jsx
//
// Review one ready_for_review application (used by the Review queue and the side panel):
//   * every field from the review packet (GET /api/applications/{id}/review), flagged
//     first, with its source badge and confidence chip; edit one inline (POST .../review/edit)
//     with "Remember this answer" (auto) or not (once);
//   * the screenshots, as a carousel;
//   * the kit: which resume goes with it (swap: PUT /api/kits/{id}/resume) and the cover
//     letter (rewrite: POST /api/kits/{id}/regenerate);
//   * Approve (POST .../review/approve), Skip (POST .../review/reject), or describe
//     corrections and submit (POST /api/apply/submit) - the old Apply page's "Correct & submit".
// With no packet (an application reviewed the older way) the session's screenshots are shown
// and Approve / Correct go through /api/apply/submit. An assist-mode application (the agent
// prepared it; you apply on the site) shows its assist pack instead - assist mode never submits.
//
// Keys (when `keys`): A approve, E edit, S skip, N next, ? help.
'use client'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, send } from '@/lib/api'
import { copyText, useShortcuts } from '@/lib/live'
import { AssistPack } from './ApplyTab'

const SOURCE = { user: 'you', answer_bank: 'answer bank', profile: 'profile', ai: 'AI' }
const confClass = c => (c >= 0.9 ? 'hi' : c >= 0.7 ? 'mid' : 'lo')
const shotName = p => String(p).split(/[\\/]/).pop()

function Carousel({ urls }) {
  const [i, setI] = useState(0)
  if (!urls.length) return <div className="m">No screenshot.</div>
  const n = Math.min(i, urls.length - 1)
  return (
    <div className="shots">
      <a href={urls[n]} target="_blank" rel="noreferrer"><img src={urls[n]} alt={`Page ${n + 1} of the filled form`} /></a>
      {urls.length > 1 && <div className="row">
        <button className="btn sm" disabled={n === 0} onClick={() => setI(n - 1)} aria-label="Previous screenshot">‹</button>
        {urls.map((_, k) => <button key={k} className={`btn sm ${k === n ? 'primary' : ''}`} onClick={() => setI(k)}>Page {k + 1}</button>)}
        <button className="btn sm" disabled={n === urls.length - 1} onClick={() => setI(n + 1)} aria-label="Next screenshot">›</button>
      </div>}
    </div>
  )
}

function FieldRow({ f, editing, setEditing, onSave }) {
  const [val, setVal] = useState(f.value ?? '')
  const [remember, setRemember] = useState(true)
  const [busy, setBusy] = useState(false)
  const input = useRef(null)
  useEffect(() => { if (editing) { setVal(f.value ?? ''); setTimeout(() => input.current?.focus(), 0) } }, [editing])
  const save = async () => { setBusy(true); const ok = await onSave(f, val, remember); setBusy(false); if (ok) setEditing('') }
  return (
    <tr className={f.flagged ? 'flagged' : ''} style={{ cursor: 'default' }}>
      <td><div className="t">{f.label}{f.required ? ' *' : ''}</div>{f.flagged && <div className="m flag">⚑ {f.reason}</div>}</td>
      <td style={{ minWidth: '14rem' }}>{editing ? (
        <div>
          {f.options?.length
            ? <select ref={input} className="in" value={val} onChange={e => setVal(e.target.value)}><option value="">—</option>{f.options.map(o => <option key={o}>{o}</option>)}</select>
            : f.type === 'textarea' || String(val).length > 80
              ? <textarea ref={input} className="in" value={val} onChange={e => setVal(e.target.value)} />
              : <input ref={input} className="in" value={val} onChange={e => setVal(e.target.value)} onKeyDown={e => { if (e.key === 'Enter') save(); if (e.key === 'Escape') { e.stopPropagation(); setEditing('') } }} />}
          <label className="chk"><input type="checkbox" checked={remember} onChange={e => setRemember(e.target.checked)} /> Remember this answer</label>
          <div className="row"><button className="btn sm primary" disabled={busy} onClick={save}>Save</button><button className="btn sm" onClick={() => setEditing('')}>Cancel</button></div>
        </div>
      ) : <div className="rv-val" onClick={() => setEditing(f.id)} title="Click to edit">{String(f.value ?? '') || <span className="m">(empty)</span>}</div>}</td>
      <td><span className={`badge src-${f.source}`}>{SOURCE[f.source] || f.source}</span></td>
      <td><span className={`chip ${confClass(f.confidence)}`} title="Confidence">{Math.round((f.confidence || 0) * 100)}%</span></td>
    </tr>
  )
}

function Kit({ jobId, flash }) {
  const [kit, setKit] = useState(null)
  const [resumes, setResumes] = useState([])
  const [busy, setBusy] = useState('')
  const load = useCallback(() => {
    api(`/api/kits/${jobId}`).then(k => setKit(k.current)).catch(() => setKit(null))
    api('/api/resumes').then(r => setResumes(r.resumes || [])).catch(() => setResumes([]))
  }, [jobId])
  useEffect(() => { load() }, [load])
  const swap = async id => {
    setBusy('resume')
    try { await send(`/api/kits/${jobId}/resume`, 'PUT', { variant_id: Number(id) }); flash(true, 'Resume changed for this application'); load() } catch (e) { flash(false, e.message) }
    setBusy('')
  }
  const rewrite = async () => {
    setBusy('letter')
    try { await send(`/api/kits/${jobId}/regenerate`, 'POST', {}); flash(true, 'Cover letter rewritten'); load() } catch (e) { flash(false, e.message) }
    setBusy('')
  }
  return (
    <div className="pn-sec">
      <div className="pn-h">Resume &amp; cover letter</div>
      <div className="row">
        <span className="m">Resume:</span>
        {resumes.length ? (
          <select className="in" style={{ width: 'auto' }} value={kit?.variant_id ?? ''} disabled={busy === 'resume'} onChange={e => e.target.value && swap(e.target.value)} aria-label="Resume for this application">
            {kit?.variant_id == null && <option value="">{kit ? 'Default resume file' : 'Default resume'}</option>}
            {resumes.map(r => <option key={r.id} value={r.id}>{r.label}{r.is_default ? ' (default)' : ''}</option>)}
          </select>) : <span>{kit?.variant_label || 'resume.pdf (no variants uploaded)'}</span>}
        {kit && <span className="m">{kit.selection === 'override' ? 'chosen by you' : kit.reason}</span>}
      </div>
      {kit?.summary && <><div className="m" style={{ marginTop: '.5rem' }}>Tailored summary</div><div className="pn-text">{kit.summary}</div></>}
      <div className="row" style={{ marginTop: '.5rem' }}><span className="m">Cover letter</span>
        {kit?.cover_letter && <button className="btn sm" onClick={() => copyText(kit.cover_letter)}>Copy</button>}
        <button className="btn sm" disabled={busy === 'letter'} onClick={rewrite}>{busy === 'letter' ? 'Writing…' : kit?.cover_letter ? 'Rewrite' : 'Write one'}</button></div>
      {kit?.cover_letter ? <pre className="pn-draft">{kit.cover_letter}</pre> : <div className="m">No cover letter in this application's kit.</div>}
    </div>
  )
}

export default function ReviewPanel({ app, onChanged, onNext, flash, keys = false }) {
  const jobId = app.job_id
  const [packet, setPacket] = useState(undefined)        // undefined loading, null none
  const [shots, setShots] = useState([])
  const [editing, setEditing] = useState('')
  const [help, setHelp] = useState(false)
  const [corr, setCorr] = useState('')
  const [busy, setBusy] = useState(false)
  const [assist, setAssist] = useState(false)

  const load = useCallback(async () => {
    api(`/api/applications/${jobId}`).then(a => setAssist(!![...(a.events || [])].reverse()
      .find(e => e.kind === 'transition' && e.to_stage === 'ready_for_review')?.detail?.assist)).catch(() => {})
    try { const p = await api(`/api/applications/${jobId}/review`); setPacket(p); setShots(p.screenshot_urls || []) }
    catch (e) {
      if (e.status !== 404) flash(false, e.message)
      setPacket(null)
      api(`/api/apply/status/${jobId}`).then(s => setShots((s.screenshots || []).map(p => `/screenshots/${shotName(p)}`))).catch(() => setShots([]))
    }
  }, [jobId])
  useEffect(() => { setPacket(undefined); setEditing(''); setCorr(''); load() }, [load])

  const fields = packet ? [...packet.fields].sort((a, b) => (b.flagged ? 1 : 0) - (a.flagged ? 1 : 0)) : []
  const saveField = async (f, value, remember) => {
    try {
      const r = await send(`/api/applications/${jobId}/review/edit`, 'POST', { field_id: f.id, value, remember: remember ? 'auto' : 'once' })
      flash(true, r.remembered ? `Saved and remembered (${r.remembered.scope})` : 'Saved for this application only')
      load(); return true
    } catch (e) { flash(false, e.message); return false }
  }
  const act = async (fn, ok, next = true) => {
    setBusy(true)
    try { const r = await fn(); flash(true, typeof ok === 'function' ? ok(r) : ok); onChanged(); if (next) onNext() } catch (e) { flash(false, e.message) }
    setBusy(false)
  }
  const approve = () => act(() => (packet ? send(`/api/applications/${jobId}/review/approve`, 'POST') : send(`/api/apply/submit/${jobId}`, 'POST', { corrections: [] })),
    r => (r?.dry_run ? 'Approved — dry run: the form is filled but not submitted' : 'Approved — submitting'))
  const skip = () => { if (confirm(`Skip ${app.title} at ${app.company}?`)) act(() => send(`/api/applications/${jobId}/review/reject`, 'POST', { reason: 'Skipped in review' }), 'Skipped') }
  const correct = () => act(() => send(`/api/apply/submit/${jobId}`, 'POST', { corrections: [corr.trim()] }), 'Submitting with your corrections')
  const editFirst = () => { const f = fields.find(x => x.flagged) || fields[0]; if (f) setEditing(f.id) }

  useShortcuts({ a: () => { if (!busy) approve() }, e: editFirst, s: () => { if (!busy && packet) skip() }, n: onNext,
                 '?': () => setHelp(h => !h) }, keys && !editing && !assist)
  useShortcuts({ n: onNext }, keys && assist)

  if (packet === undefined) return <div className="m">Loading the review…</div>
  if (assist) return (
    <div className="rv">
      <div className="row rv-actions"><span className="m">Assist mode: the agent prepared this one - you apply on the site.</span>
        <button className="btn" onClick={onNext} title="N">Next</button></div>
      <AssistPack app={app} onChanged={() => { onChanged(); onNext() }} flash={flash} />
    </div>)
  return (
    <div className="rv">
      <div className="row rv-actions">
        <button className="btn primary" disabled={busy} onClick={approve} title="A">Approve{packet ? '' : ' & submit'}</button>
        {packet && <button className="btn" disabled={busy || !fields.length} onClick={editFirst} title="E">Edit</button>}
        {packet && <button className="btn danger" disabled={busy} onClick={skip} title="S">Skip</button>}
        <button className="btn" onClick={onNext} title="N">Next</button>
        <button className="btn sm" onClick={() => setHelp(h => !h)} title="?" aria-label="Keyboard shortcuts">?</button>
        {packet && <span className="m">{packet.flagged} flagged of {packet.fields.length}</span>}
      </div>
      {help && <div className="card help" role="dialog" aria-label="Keyboard shortcuts">
        <b>Review keys</b><div><kbd>A</kbd> approve · <kbd>E</kbd> edit the first flagged field · <kbd>S</kbd> skip · <kbd>N</kbd> next · <kbd>?</kbd> this help</div>
        <div className="m">Keys are ignored while you type in a field.</div></div>}
      {packet ? (fields.length ? (
        <div style={{ overflowX: 'auto' }}><table className="rv-table" aria-label="Fields">
          <thead><tr><th>Field</th><th>Value</th><th>Source</th><th>Confidence</th></tr></thead>
          <tbody>{fields.map(f => <FieldRow key={f.id} f={f} editing={editing === f.id} setEditing={setEditing} onSave={saveField} />)}</tbody>
        </table></div>) : <div className="m">The form had no fields to review (a one-click apply).</div>)
        : <div className="m">This application was prepared before field-by-field review existed: check the screenshot, then approve or correct.</div>}
      <div className="pn-sec"><div className="pn-h">Screenshots</div><Carousel urls={shots} /></div>
      <Kit jobId={jobId} flash={flash} />
      <div className="pn-sec">
        <div className="pn-h">Corrections in your own words</div>
        <textarea className="in" placeholder="e.g. Change experience to 2 years…" value={corr} onChange={e => setCorr(e.target.value)} />
        <div className="row" style={{ marginTop: '.5rem' }}><button className="btn" disabled={busy || !corr.trim()} onClick={correct}>Correct &amp; submit</button></div>
      </div>
    </div>
  )
}
