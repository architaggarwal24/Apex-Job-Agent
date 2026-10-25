// frontend/src/components/applications/ReviewQueue.jsx
//
// The Review queue: every ready_for_review application, highest score first, reviewed one
// after another (ReviewPanel, keys A / E / S / N / ?). "Approve all unflagged" approves, one
// at a time, every application whose review packet has no flagged field - after a confirm
// that shows how many - waiting for each to leave review before starting the next, so two
// submissions never drive the browser at once.
'use client'
import { useEffect, useState } from 'react'
import { api, send } from '@/lib/api'
import { scoreClass } from '@/lib/stages'
import ReviewPanel from './ReviewPanel'

const sleep = ms => new Promise(r => setTimeout(r, ms))
const WAIT_MS = 5 * 60 * 1000

export default function ReviewQueue({ rows, onChanged, onOpen, flash }) {
  const [cur, setCur] = useState(rows[0]?.job_id || '')
  const [flags, setFlags] = useState({})           // job_id -> flagged count, or null (no packet)
  const [bulk, setBulk] = useState('')
  const ids = rows.map(r => r.job_id).join()

  useEffect(() => {
    if (!rows.some(r => r.job_id === cur)) setCur(rows[0]?.job_id || '')
    let gone = false
    Promise.all(rows.map(r => api(`/api/applications/${r.job_id}/review`).then(p => [r.job_id, p.flagged]).catch(() => [r.job_id, null])))
      .then(pairs => { if (!gone) setFlags(Object.fromEntries(pairs)) })
    return () => { gone = true }
  }, [ids])

  const next = () => {
    if (!rows.length) return
    const i = rows.findIndex(r => r.job_id === cur)
    setCur(rows[(i + 1) % rows.length].job_id)
  }

  const approveAll = async () => {
    const clean = rows.filter(r => flags[r.job_id] === 0)
    if (!clean.length) { flash(false, 'Every application in the queue has a flagged field (or no review packet) - review those one by one.'); return }
    if (!confirm(`Approve and submit ${clean.length} application${clean.length === 1 ? '' : 's'} with no flagged fields?`)) return
    let done = 0
    for (const r of clean) {
      setBulk(`Approving ${done + 1} of ${clean.length}: ${r.title}…`)
      try {
        await send(`/api/applications/${r.job_id}/review/approve`, 'POST')
      } catch (e) { flash(false, `${r.title}: ${e.message}`); break }
      done += 1
      const until = Date.now() + WAIT_MS
      while (Date.now() < until) {                     // one submission at a time
        await sleep(2000)
        const a = await api(`/api/applications/${r.job_id}`).catch(() => null)
        if (!a || a.stage !== 'ready_for_review') break
        const last = (a.events || []).at(-1)
        if (last?.kind === 'step' && last.detail?.step === 'review_approved' && last.detail?.dry_run) break  // a dry run stays in review
      }
      onChanged()
    }
    setBulk('')
    flash(true, `Approved ${done} of ${clean.length}`)
  }

  if (!rows.length) return <div className="card empty">Nothing is waiting for review.</div>
  const unflagged = rows.filter(r => flags[r.job_id] === 0).length
  const current = rows.find(r => r.job_id === cur) || rows[0]
  return (
    <div className="rq">
      <div className="rq-list card">
        <div className="row rq-head">
          <b>{rows.length} to review</b>
          <button className="btn sm primary" disabled={!!bulk || !unflagged} onClick={approveAll}>Approve all unflagged ({unflagged})</button>
        </div>
        {bulk && <div className="m" role="status">{bulk}</div>}
        {rows.map(r => (
          <button key={r.job_id} className={`rq-item ${r.job_id === current.job_id ? 'on' : ''}`} onClick={() => setCur(r.job_id)}>
            <span className={`chip ${scoreClass(r.ai_score)}`}>{r.ai_score ?? '—'}</span>
            <span className="rq-t"><span className="t">{r.title}</span><span className="m">{r.company}</span></span>
            <span className={`badge ${flags[r.job_id] ? 'b-wait' : flags[r.job_id] === 0 ? 'b-done' : 'b-idle'}`}>
              {flags[r.job_id] == null ? 'no packet' : flags[r.job_id] ? `${flags[r.job_id]} flagged` : 'clean'}</span>
          </button>))}
      </div>
      <div className="rq-main card section">
        <div className="row" style={{ marginBottom: 'var(--sp2)' }}>
          <div style={{ minWidth: 0 }}><div className="pn-title">{current.title}</div><div className="m">{current.company} · {current.source}</div></div>
          <span className="spacer" /><button className="btn sm" onClick={() => onOpen(current.job_id)}>Open in panel</button>
        </div>
        <ReviewPanel key={current.job_id} app={current} onChanged={onChanged} onNext={next} flash={flash} keys={!bulk} />
      </div>
    </div>
  )
}
