// frontend/src/components/applications/SidePanel.jsx
//
// One application in detail, shown beside the list (`inline`; an overlay on narrow
// screens): title, the quick triage actions (Queue / Skip / open the posting or the
// company's form), a fact strip (fit, where it applies, salary or age), then tabs:
// Overview (why it fits, brief, description, move to a stage), Timeline, Notes, Next step
// (hunt fields + due follow-ups), Prep, Actions (start / answer / retry / assist pack),
// Review (a ready_for_review card) and the AI assistant. Reloaded whenever the workspace
// hears an event for this job (`version`).
'use client'
import { motion } from 'motion/react'
import { ExternalLink, X } from 'lucide-react'
import { EASE } from '@/lib/motion'
import { useCallback, useEffect, useState } from 'react'
import { api, send } from '@/lib/api'
import { safeHref } from '@/lib/live'
import { STAGES, applyHost, reasonLabel, stageLabel } from '@/lib/stages'
import { QUEUEABLE, SKIPPABLE } from '@/lib/triage'
import HuntTab from './HuntTab'
import PrepTab from './PrepTab'
import ApplyTab from './ApplyTab'
import ReviewPanel from './ReviewPanel'
import Ghostwriter from './Ghostwriter'
import { Sc, age } from './InboxList'
import { SkeletonCard } from '@/components/layout/Skeleton'

const when = s => (s ? String(s).replace('T', ' ').slice(0, 16) : '')

function defaultTab(stage) {
  if (stage === 'ready_for_review') return 'review'
  if (['preparing', 'needs_you', 'failed'].includes(stage)) return 'actions'
  return 'overview'
}

function Overview({ app, job, onChanged, flash }) {
  const [to, setTo] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const move = async () => {
    if (!to) return
    setBusy(true)
    try { await send(`/api/applications/${app.job_id}/transition`, 'POST', { to, note }); flash(true, `Moved to ${stageLabel(to)}`); setTo(''); setNote(''); onChanged() }
    catch (e) { flash(false, e.message) }
    setBusy(false)
  }
  return (
    <>
      {app.error_message && <div className="alert">{app.error_message}</div>}
      <div className="pn-why"><b>Why this score: </b>{app.ai_score_reason || app.fit_reason || 'Not scored yet.'}</div>
      {job?.brief && <div className="pn-sec"><div className="pn-h">AI brief</div><div className="pn-text">{job.brief}</div></div>}
      <div className="pn-sec"><div className="pn-h">About the role</div>
        <div className="pn-text pn-desc">{app.description || <span className="m">No description saved.</span>}</div></div>
      {app.contact_email && <div className="pn-sec"><a className="btn sm" href={`mailto:${app.contact_email}`}>Email {app.contact_name || 'contact'}</a></div>}
      <div className="pn-sec">
        <div className="pn-h">Move to a stage</div>
        <div className="row">
          <select className="in" style={{ width: 'auto' }} value={to} onChange={e => setTo(e.target.value)} aria-label="Move to stage">
            <option value="">Choose a stage…</option>
            {STAGES.filter(([k]) => k !== app.stage).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
          </select>
          <input className="in grow" placeholder="Note (optional), e.g. Recruiter called, first round next week" value={note} onChange={e => setNote(e.target.value)} />
          <button className="btn primary" disabled={!to || busy} onClick={move}>Move</button>
        </div>
        <div className="hint">The server checks every move; one it doesn't allow is refused with its reason.</div>
      </div>
    </>
  )
}

function Timeline({ events }) {
  if (!events?.length) return <div className="m">No events yet.</div>
  return (
    <ol className="tl">{[...events].reverse().map(e => {
      const d = e.detail || {}
      const text = d.message || d.note || d.reason || ''
      return (
        <li key={e.id} className={`tl-${e.kind}`}>
          <div className="m">{when(e.at)} · {e.actor}</div>
          <div>{e.kind === 'step' ? <b>{text || d.step}</b>
            : <><b>{e.from_stage ? `${stageLabel(e.from_stage)} → ` : ''}{stageLabel(e.to_stage)}</b>{e.kind === 'migrated' ? ' (migrated)' : ''}{text ? ` — ${text}` : ''}</>}</div>
          {e.screenshot && <a className="link-btn" href={`/screenshots/${encodeURIComponent(e.screenshot)}`} target="_blank" rel="noreferrer">screenshot</a>}
        </li>)
    })}</ol>
  )
}

function Notes({ jobId, notes, onChanged, flash }) {
  const [text, setText] = useState('')
  const add = async () => {
    if (!text.trim()) return
    try { await send(`/api/tracking/note/${jobId}`, 'POST', { note: text.trim() }); setText(''); onChanged() } catch (e) { flash(false, e.message) }
  }
  return (
    <>
      <textarea className="in" placeholder="Add a note…" value={text} onChange={e => setText(e.target.value)} />
      <div className="row" style={{ margin: '.5rem 0 var(--sp3)' }}><button className="btn primary" disabled={!text.trim()} onClick={add}>Add note</button></div>
      {notes?.length ? notes.map(n => <div key={n.id} className="pn-note"><div className="m">{when(n.created_at)}</div><div className="pn-text">{n.note}</div></div>)
        : <div className="m">No notes yet.</div>}
    </>
  )
}


function Header({ app, onClose, onQueue, onSkip, owner }) {
  const href = safeHref(app.url)
  const form = app.apply_url && app.apply_url !== app.url ? safeHref(app.apply_url) : ''
  return (
    <>
      <header className="pn-top">
        <div style={{ minWidth: 0 }}>
          <div className="pn-title">{app.title}</div>
          <div className="pn-sub"><span>{app.company}</span>{app.location && <span>{app.location}</span>}<span className="mono">{app.source}</span>
            <span className="chip">{stageLabel(app.stage)}</span>
            {app.stage === 'needs_you' && <span className="badge b-wait">⚠ {reasonLabel(app.blocked_reason)}</span>}
            {app.stale && <span className="badge b-wait" title="Waiting longer than this stage's limit">stale</span>}</div>
        </div>
        <button className="btn sm ghost" onClick={onClose} aria-label="Close panel" title="Close (Esc)"><X className="lucide" /></button>
      </header>
      <div className="pn-quick">
        {onQueue && QUEUEABLE.includes(app.stage) &&
          <button className="btn primary" disabled={!owner} title={owner ? 'Queue it: the agent applies when its turn comes' : 'Only the owner account can queue applications.'}
                  onClick={() => onQueue(app.job_id)}>Queue <kbd>Q</kbd></button>}
        {onSkip && SKIPPABLE.includes(app.stage) && <button className="btn" onClick={() => onSkip(app.job_id)}>Skip <kbd>X</kbd></button>}
        {href && <a className="btn" href={href} target="_blank" rel="noopener noreferrer">Posting <ExternalLink className="lucide" /></a>}
        {form && <a className="btn" href={form} target="_blank" rel="noopener noreferrer" title="The company's own form - no job-board account needed">
          Company form <ExternalLink className="lucide" /></a>}
      </div>
      <div className="pn-facts">
        <div><small>Fit</small><b><Sc n={app.fit !== undefined ? app.fit : app.ai_score} kind={app.fit_kind} /></b></div>
        <div><small>Applies on</small><b title={form ? 'no job-board account needed' : ''}>{form ? applyHost(app.apply_url) : app.source || '—'}</b></div>
        <div><small>{app.salary ? 'Salary' : app.posted_at ? 'Posted' : 'Found'}</small>
          <b>{app.salary || (app.posted_at ? `${age(app.posted_at)} ago` : `${age(app.found_at)} ago`)}</b></div>
      </div>
    </>
  )
}

export default function SidePanel({ jobId, version, onClose, onChanged, onNextReview, flash, inline = false, onQueue, onSkip, owner = true }) {
  const [app, setApp] = useState(null)
  const [job, setJob] = useState(null)
  const [err, setErr] = useState('')
  const [tab, setTab] = useState('')

  const load = useCallback(async () => {
    try {
      const [a, j] = await Promise.all([api(`/api/applications/${jobId}`), api(`/api/jobs/${jobId}`)])
      setApp(a); setJob(j); setErr('')
      setTab(t => t || defaultTab(a.stage))
    } catch (e) { setErr(e.status === 404 ? 'This application no longer exists.' : e.message) }
  }, [jobId])
  useEffect(() => { load() }, [load, version])
  // A card that moves into or out of review switches to the tab that fits it.
  const [lastStage, setLastStage] = useState('')
  useEffect(() => {
    if (!app || app.stage === lastStage) return
    if (lastStage) setTab(defaultTab(app.stage))
    setLastStage(app.stage)
  }, [app, lastStage])

  const changed = () => { load(); onChanged() }
  const tabs = [['overview', 'Overview'], ['actions', 'Actions'], ...(app?.stage === 'ready_for_review' ? [['review', 'Review']] : []),
    ['timeline', 'Timeline'], ['notes', 'Notes'], ['hunt', 'Next step'], ['prep', 'Prep'], ['assistant', 'Assistant']]

  return (
    <motion.aside className={`panel ${inline ? 'inline' : 'overlay'}`} aria-label="Application details"
      initial={{ opacity: 0, x: inline ? 0 : 32 }} animate={{ opacity: 1, x: 0 }} exit={{ opacity: 0, transition: { duration: 0.12 } }} transition={{ duration: 0.18, ease: EASE }}>
      {err ? <><header className="pn-top"><span /><button className="btn sm ghost" onClick={onClose} aria-label="Close panel"><X className="lucide" /></button></header><div className="empty">{err}</div></>
        : !app ? <div style={{ padding: '1.25rem' }}><SkeletonCard lines={6} label="Loading the job" /></div> : (
        <>
          <Header app={app} onClose={onClose} onQueue={onQueue} onSkip={onSkip} owner={owner} />
          <nav className="tabs pn-tabs" role="tablist">
            {tabs.map(([k, l]) => <button key={k} role="tab" aria-selected={tab === k} className={`tab ${tab === k ? 'on' : ''}`} onClick={() => setTab(k)}>{l}</button>)}
          </nav>
          <div className="pn-body">
            {tab === 'overview' && <Overview app={app} job={job} onChanged={changed} flash={flash} />}
            {tab === 'timeline' && <Timeline events={app.events} />}
            {tab === 'notes' && <Notes jobId={jobId} notes={job?.notes} onChanged={changed} flash={flash} />}
            {tab === 'hunt' && <HuntTab app={app} onChanged={changed} flash={flash} />}
            {tab === 'prep' && <PrepTab app={app} flash={flash} />}
            {tab === 'actions' && <ApplyTab app={app} job={job} onChanged={changed} flash={flash} goReview={() => setTab('review')} />}
            {tab === 'review' && app.stage === 'ready_for_review' &&
              <ReviewPanel app={app} onChanged={changed} onNext={() => onNextReview(jobId)} flash={flash} keys />}
            {tab === 'assistant' && <Ghostwriter app={app} />}
          </div>
        </>
      )}
    </motion.aside>
  )
}
