// frontend/src/app/page.jsx
//
// Today: what needs you now. It replaces the old Dashboard (and its redirect to the
// backend's first-run /setup page - the get-started checklist here covers what /setup did).
//
//   counts          needs you · ready to review · interviews in the next 48 h · follow-ups due,
//                   each a link into the Applications workspace, filtered to exactly those cards
//   follow-ups due  with the draft, Copy, Done, Snooze               (/api/follow-ups/*)
//   queue           running / paused, today's budget, the countdown to the next apply,
//                   pause / resume (owner only)                       (/api/queue/*)
//   search          run the job search (owner only)                   (/api/pipeline/*)
//   get started     the checklist until every item is done           (GET /api/setup/checklist)
// Everything is read live from those endpoints; stage changes stream in (/api/events/stream).
'use client'
import { useCallback, useEffect, useState } from 'react'
import Link from 'next/link'
import { api, send } from '@/lib/api'
import { nextStepWithin } from '@/lib/filters'
import { INTERVIEW_STAGES, TODAY_LINKS, budget, countdown } from '@/lib/insights'
import { copyText, useLiveEvents } from '@/lib/live'
import { OWNER_ONLY, useMe } from '@/lib/me'

const KIND = { check_in: 'Check in', thank_you: 'Thank you', status_ask: 'Ask for news' }
const when = s => String(s || '').replace('T', ' ').slice(0, 16)

function Count({ n, label, href, tone }) {
  return (
    <Link className="card stat today-count" href={href} data-count={label}>
      <div className="l">{label}</div>
      <div className="n" style={tone ? { color: tone } : undefined}>{n ?? '—'}</div>
      <div className="m">Open in Applications →</div>
    </Link>
  )
}

function FollowUps({ items, reload, flash }) {
  const [busy, setBusy] = useState(0)
  const act = async (f, path, body, label) => {
    setBusy(f.id)
    try { await send(`/api/follow-ups/${f.id}/${path}`, 'POST', body); flash(true, label) } catch (e) { flash(false, e.message) }
    setBusy(0); reload()
  }
  if (items === null) return <div className="m">Loading…</div>
  if (!items.length) return <div className="m">No follow-ups due. Nice.</div>
  return items.map(f => (
    <div key={f.id} className="fu" data-follow-up={f.id}>
      <div className="row">
        <b>{KIND[f.kind] || f.kind}</b>
        <Link className="link-btn" href={`/applications?job=${encodeURIComponent(f.job_id)}`}>{f.title} · {f.company}</Link>
        <span className="m">due {when(f.status === 'snoozed' ? f.snoozed_until : f.due_at)}</span>
      </div>
      <pre className="pn-draft">{f.draft_text || 'No draft yet - write your own message.'}</pre>
      <div className="row">
        <button className="btn sm" disabled={!f.draft_text} onClick={async () => { const ok = await copyText(f.draft_text); flash(ok, ok ? 'Draft copied' : 'Could not copy') }}>Copy</button>
        <button className="btn sm primary" disabled={busy === f.id} onClick={() => act(f, 'done', undefined, 'Marked done')}>Done</button>
        <select className="in" style={{ width: 'auto' }} value="" disabled={busy === f.id} aria-label={`Snooze ${f.title}`}
                onChange={e => e.target.value && act(f, 'snooze', { days: Number(e.target.value) }, `Snoozed for ${e.target.value} day(s)`)}>
          <option value="">Snooze…</option><option value="1">1 day</option><option value="3">3 days</option><option value="7">7 days</option>
        </select>
      </div>
    </div>))
}

function Queue({ owner, flash }) {
  const [q, setQ] = useState(null)
  const [now, setNow] = useState(() => new Date())
  const [busy, setBusy] = useState(false)
  const load = useCallback(() => api('/api/queue/status').then(setQ).catch(e => flash(false, e.message)), [])
  useEffect(() => { load(); const t = setInterval(load, 10000); return () => clearInterval(t) }, [load])
  useEffect(() => { const t = setInterval(() => setNow(new Date()), 1000); return () => clearInterval(t) }, [])
  const toggle = async () => {
    setBusy(true)
    try { setQ(await send(`/api/queue/${q.paused ? 'resume' : 'pause'}`, 'POST')); flash(true, q.paused ? 'Queue resumed' : 'Queue paused') } catch (e) { flash(false, e.message) }
    setBusy(false)
  }
  if (!q) return <div className="m">Loading…</div>
  const b = budget(q.applied_today, q.daily_limit)
  const state = !q.running ? ['stopped', 'b-fail'] : q.paused ? ['paused', 'b-wait'] : q.current_job ? ['working', 'b-fill'] : ['running', 'b-done']
  const next = q.paused ? 'paused' : q.current_job ? 'working on one now' : !q.queue_length ? 'nothing queued'
    : q.next_at ? countdown(q.next_at, now) : 'now'
  return (
    <>
      <div className="row">
        <span className={`badge ${state[1]}`} data-queue-state={state[0]}>{state[0]}</span>
        <span className="m">{q.queue_length} queued</span>
        <span className="spacer" />
        <button className="btn sm" disabled={!owner || busy} title={owner ? '' : OWNER_ONLY} onClick={toggle}>{q.paused ? 'Resume' : 'Pause'}</button>
      </div>
      {q.current_job && <div className="m" style={{ marginTop: '.5rem' }}>Now: {q.current_job.title} · {q.current_job.company}</div>}
      <div className="budget" title="Applied today against the daily limit">
        <div className="row"><span className="m">Today's budget</span><span className="spacer" /><b data-budget>{b.text}</b></div>
        {b.pct !== null && <div className={`budget-bar ${b.full ? 'full' : ''}`}><i style={{ width: `${b.pct}%` }} /></div>}
      </div>
      <div className="row"><span className="m">Next apply</span><span className="spacer" /><b data-next-apply>{next}</b></div>
      {!owner && <div className="hint">{OWNER_ONLY} The queue runs the owner's browser.</div>}
    </>
  )
}

function SearchPrefs({ item, reload, flash }) {
  const [titles, setTitles] = useState((item.titles || []).join(', '))
  const [places, setPlaces] = useState((item.locations || []).join(', '))
  const split = s => s.split(',').map(x => x.trim()).filter(Boolean)
  const save = async () => {
    try { await send('/api/search-prefs', 'POST', { positions: split(titles), locations: split(places) }); flash(true, 'Search preferences saved'); reload() }
    catch (e) { flash(false, e.message) }
  }
  return (
    <div className="grid2" style={{ marginTop: '.5rem' }}>
      <input className="in" placeholder="Job titles, e.g. AI Engineer, Backend Engineer" value={titles} onChange={e => setTitles(e.target.value)} aria-label="Job titles" />
      <input className="in" placeholder="Locations, e.g. Bangalore, Remote" value={places} onChange={e => setPlaces(e.target.value)} aria-label="Locations" />
      <div className="row"><button className="btn sm" disabled={!split(titles).length || !split(places).length} onClick={save}>Save</button></div>
    </div>
  )
}

function Checklist({ data, reload, owner, runSearch, flash }) {
  return (
    <div className="card section checklist">
      <div className="row"><h2 style={{ padding: 0 }}>Get started</h2><span className="m">{data.done} of {data.total} done</span></div>
      <ol>{data.items.map(i => (
        <li key={i.key} className={i.done ? 'done' : ''} data-item={i.key}>
          <span className="ck" aria-label={i.done ? 'done' : 'to do'}>{i.done ? '✓' : '○'}</span>
          <div className="ck-body">
            <div className="row"><b>{i.label}</b><span className="m">{i.detail}</span><span className="spacer" />
              {!i.done && i.href && <Link className="btn sm" href={i.href}>Go</Link>}
              {!i.done && i.key === 'search' && <button className="btn sm" disabled={!owner} title={owner ? '' : OWNER_ONLY} onClick={() => runSearch(false)}>Run search</button>}
            </div>
            {!i.done && i.key === 'search_prefs' && <SearchPrefs item={i} reload={reload} flash={flash} />}
          </div>
        </li>))}</ol>
    </div>
  )
}

export default function Today() {
  const me = useMe()
  const owner = !!me?.owner
  const [apps, setApps] = useState(null)
  const [due, setDue] = useState(null)
  const [list, setList] = useState(null)
  const [status, setStatus] = useState(null)
  const [msg, setMsg] = useState(null)
  const [running, setRunning] = useState(false)
  const flash = (ok, text) => setMsg({ ok, text })
  useEffect(() => { if (!msg) return undefined; const t = setTimeout(() => setMsg(null), 6000); return () => clearTimeout(t) }, [msg])

  const loadApps = useCallback(() => api('/api/applications?limit=2000').then(setApps).catch(() => setApps([])), [])
  const loadDue = useCallback(() => api('/api/follow-ups/due').then(setDue).catch(() => setDue([])), [])
  const loadList = useCallback(() => api('/api/setup/checklist').then(setList).catch(() => {}), [])
  useEffect(() => {
    loadApps(); loadDue(); loadList(); api('/api/status').then(setStatus).catch(() => {})
    const t = setInterval(() => { loadDue(); loadList() }, 30000)
    return () => clearInterval(t)
  }, [loadApps, loadDue, loadList])
  useLiveEvents(() => { loadApps(); loadDue() })

  const runSearch = async quick => {
    setRunning(true)
    try { await send('/api/pipeline/run', 'POST', { quick }); flash(true, 'Search running in the background - new jobs appear in Applications as they are scored') }
    catch (e) { flash(false, e.status === 403 ? 'Only the owner account can run the search' : e.message) }
    setTimeout(() => { setRunning(false); loadList() }, 3000)
  }
  const cancelSearch = async () => {
    try { await send('/api/pipeline/cancel', 'POST'); flash(true, 'Cancel requested - finishing the current step, then stopping') } catch (e) { flash(false, e.message) }
  }

  const now = new Date()
  const count = pred => (apps ? apps.filter(pred).length : null)
  const counts = {
    needs_you: count(a => a.stage === 'needs_you'),
    ready: count(a => a.stage === 'ready_for_review'),
    interviews: count(a => INTERVIEW_STAGES.includes(a.stage) && nextStepWithin(a.next_step_at, 48, now)),
    follow_ups: due ? new Set(due.map(f => f.job_id)).size : null,
  }

  return (
    <>
      <h1>Today</h1>
      <p className="sub">
        {now.toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' })}
        {status?.llm && <span className="chip" style={{ marginLeft: 10 }}>{status.llm.label || status.llm.provider} / {status.llm.model}</span>}
        {status?.dry_run && <span className="chip mid" style={{ marginLeft: 6 }}>Dry run</span>}
      </p>
      {msg && <div className={`toast ${msg.ok ? 'ok' : 'err'}`} role="status">{msg.text}</div>}
      {list && !list.complete && <Checklist data={list} reload={loadList} owner={owner} runSearch={runSearch} flash={flash} />}

      <div className="stats today-counts">
        <Count n={counts.needs_you} label="Needs you" href={TODAY_LINKS.needs_you} tone="var(--ylw)" />
        <Count n={counts.ready} label="Ready to review" href={TODAY_LINKS.ready_for_review} />
        <Count n={counts.interviews} label="Interviews in 48 h" href={TODAY_LINKS.interviews} tone="var(--grn)" />
        <Count n={counts.follow_ups} label="Follow-ups due" href={TODAY_LINKS.follow_ups} />
      </div>

      <div className="today-grid">
        <div className="card section">
          <h2 style={{ padding: 0, marginBottom: 'var(--sp2)' }}>Follow-ups due</h2>
          <FollowUps items={due} reload={() => { loadDue(); loadApps() }} flash={flash} />
        </div>
        <div>
          <div className="card section">
            <h2 style={{ padding: 0, marginBottom: 'var(--sp2)' }}>Queue</h2>
            <Queue owner={owner} flash={flash} />
          </div>
          <div className="card section">
            <h2 style={{ padding: 0, marginBottom: 'var(--sp2)' }}>Job search</h2>
            <div className="row">
              <button className="btn primary" disabled={!owner || running} title={owner ? '' : OWNER_ONLY} onClick={() => runSearch(false)}>{running ? '…' : 'Run search'}</button>
              <button className="btn" disabled={!owner || running} title={owner ? '' : OWNER_ONLY} onClick={() => runSearch(true)}>Quick run</button>
              <button className="btn danger" disabled={!owner} title={owner ? '' : OWNER_ONLY} onClick={cancelSearch}>Cancel</button>
            </div>
            {!owner && me && <div className="hint">{OWNER_ONLY}</div>}
          </div>
        </div>
      </div>
    </>
  )
}
