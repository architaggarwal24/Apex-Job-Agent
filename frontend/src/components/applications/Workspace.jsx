// frontend/src/components/applications/Workspace.jsx
//
// The Applications workspace, full height, nothing scrolls but its panes:
//   tabs     Inbox (new jobs to triage) · Queued · In progress · Needs you · Review ·
//            Applied · Archive - each a set of lifecycle stages (lib/tabs.js), with counts
//            that follow the search and filters
//   cards    the Inbox's default: a deck, one job at a time (TriageDeck) - drag or arrow keys
//   list     one dense row per job (InboxList) and, beside it, the selected job (SidePanel
//            inline). Applied can also be a board of its stages (drag between them);
//            Review is the review queue.
//
// Queue and skip are instant: the job moves on screen at once, the server is asked after,
// only what it refused moves back, and the toast offers Undo (Z) - lib/triage.js.
//
// Filters come from - and are written back to - the URL query, so other pages can link
// in filtered: ?stage=a,b  ?needs_you=1  ?ready_for_review=1  ?source=  ?search= (or q=)
// ?title=  ?min= ?max= (score)  ?form=1  ?view=cards|list|board|review  ?job=<job_id>.
//
// Keys (not while typing): j / k next / previous (opens it), q queue, x skip, o open the
// posting, space select, z undo, Esc close / clear. Cards: → / ← / ↓ / ↑ queue, skip, later,
// details. Review keys (A E S N ?) belong to ReviewPanel.
'use client'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { usePathname, useRouter, useSearchParams } from 'next/navigation'
import { AnimatePresence, motion } from 'motion/react'
import { Gauge, LayoutList, MoreHorizontal, Rows3, SlidersHorizontal, X } from 'lucide-react'
import { api, send } from '@/lib/api'
import { POST_SUBMISSION, STAGE_KEYS, stageLabel } from '@/lib/stages'
import { applyFilters, atsOf, byScore, chipsFor, clearFilters, filtersFrom, queryFrom, withoutChip } from '@/lib/filters'
import { TABS, isNarrowed, shownStages, stagesForTab, tabFor } from '@/lib/tabs'
import { safeHref, useLiveEvents, useShortcuts } from '@/lib/live'
import { useMe } from '@/lib/me'
import { useOnSearchFinished } from '@/lib/pipeline'
import { QUEUEABLE, SKIPPABLE, arrivals, deckOrder, minutesLeft, pace, revertRefused, undoFor, withStages } from '@/lib/triage'
import { SkeletonRows } from '@/components/layout/Skeleton'
import InboxList from './InboxList'
import Board from './Board'
import ReviewQueue from './ReviewQueue'
import SidePanel from './SidePanel'
import TriageDeck from './TriageDeck'
import FilterPanel from './FilterPanel'

const OWNER_ONLY = 'Only the owner account can queue applications.'

const EMPTY = {
  inbox: <><b>Inbox zero.</b>Run a search from Today; new jobs land here to triage.</>,
  queued: <><b>Nothing queued.</b>Press Q on a job in the Inbox to queue it; the agent works through the queue.</>,
  working: <><b>Nothing in progress.</b>The agent picks jobs from the queue one at a time.</>,
  needs: <><b>Nothing needs you.</b>When the agent gets stuck (a question, a CAPTCHA, a login) the job waits here.</>,
  applied: <><b>No applications sent yet.</b>Approved reviews and jobs you mark applied show up here.</>,
  archive: <><b>Nothing archived.</b>Skipped and failed jobs end up here.</>,
  custom: <><b>No jobs match.</b>Clear a filter to see more.</>,
}

function useSorted(rows, sort) {
  return useMemo(() => {
    const k = sort.key
    return [...rows].sort((a, b) => {
      const x = a[k], y = b[k]
      if ((x == null || x === '') && (y == null || y === '')) return byScore(a, b)
      if (x == null || x === '') return 1
      if (y == null || y === '') return -1
      return (typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y))) * sort.dir || byScore(a, b)
    })
  }, [rows, sort])
}

export default function Workspace() {
  const params = useSearchParams()
  const router = useRouter()
  const pathname = usePathname()
  const me = useMe()
  const owner = !!me?.owner
  const [f, setF] = useState(() => filtersFrom(params))
  const [openId, setOpenId] = useState(() => params.get('job') || '')
  const [apps, setApps] = useState(null)
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState(null)
  const [cursor, setCursor] = useState('')
  const [panelVersion, setPanelVersion] = useState(0)
  const [sort, setSort] = useState({ key: 'fit', dir: -1 })
  const [selected, setSelected] = useState(() => new Set())
  const [menu, setMenu] = useState(false)
  const reloadTimer = useRef(null)

  const [dueIds, setDueIds] = useState(null)        // job ids with a follow-up due; only loaded for ?followups=due
  const wantsDue = f.followups === 'due'
  const load = useCallback(() => {
    if (wantsDue) api('/api/follow-ups/due').then(due => setDueIds(new Set(due.map(x => x.job_id)))).catch(() => setDueIds(new Set()))
    return api('/api/applications?limit=2000').then(rows => { setApps(rows); setErr('') }).catch(e => setErr(e.message))
  }, [wantsDue])
  useEffect(() => { load() }, [load])

  // Another page linking in (or Back/Forward) changes the query: follow it.
  const lastQuery = useRef('')
  useEffect(() => {
    const qs = params.toString()
    if (qs === lastQuery.current) return
    lastQuery.current = qs
    setF(filtersFrom(params))
    setOpenId(params.get('job') || '')
  }, [params])
  // ...and write ours back, so the view can be bookmarked or shared.
  useEffect(() => {
    const qs = queryFrom(f, openId)
    if (qs.slice(1) === lastQuery.current) return
    lastQuery.current = qs.slice(1)
    router.replace(`${pathname}${qs}`, { scroll: false })
  }, [f, openId, pathname, router])

  const scheduleReload = useCallback(() => {
    clearTimeout(reloadTimer.current)
    reloadTimer.current = setTimeout(load, 250)
  }, [load])
  const liveMode = useLiveEvents(ev => {
    scheduleReload()
    if (!ev || ev.job_id === openId) setPanelVersion(v => v + 1)
  })

  const flash = (ok, text) => setMsg({ ok, text, at: Date.now() })
  useEffect(() => { if (!msg) return undefined; const t = setTimeout(() => setMsg(null), 6000); return () => clearTimeout(t) }, [msg])

  const all = apps || []
  // A link-in that filters by something other than stage (?followups=due, ?next_step_within=)
  // means "wherever they are": every stage, shown as a filtered view.
  const linkOnly = !f.stages.length && !!(f.followups || f.within)
  const tab = f.view === 'review' ? 'review' : linkOnly ? 'custom' : tabFor(f.stages)
  const view = tab === 'applied' && f.view === 'board' && !isNarrowed(f.stages) ? 'board' : tab === 'review' ? 'review'
    : tab === 'inbox' && f.view !== 'list' ? 'cards' : 'list'
  // Everything but the stage: the tab counts follow the search and filters.
  const filtered = useMemo(() => applyFilters(all, { ...f, stages: [] }, { now: new Date(), dueIds: dueIds || new Set() }), [all, f, dueIds])
  const counts = useMemo(() => Object.fromEntries(TABS.map(t => [t.key, filtered.filter(a => t.stages.includes(a.stage)).length])), [filtered])
  const stages = linkOnly ? STAGE_KEYS : shownStages(f.stages)
  const rows = useSorted(useMemo(() => filtered.filter(a => stages.includes(a.stage)), [filtered, stages.join(',')]), sort)
  const sources = useMemo(() => [...new Set(all.map(a => (a.source || '').toLowerCase()).filter(Boolean))].sort(), [all])
  const atsHosts = useMemo(() => [...new Set(all.map(atsOf).filter(x => x !== 'none'))].sort(), [all])
  const unfiltered = useMemo(() => all.filter(a => stages.includes(a.stage)).length, [all, stages.join(',')])

  // The filter panel and the saved views (per account, /api/views).
  const [panel, setPanel] = useState(false)
  const [views, setViews] = useState([])
  const loadViews = useCallback(() => api('/api/views').then(r => setViews(r.views)).catch(() => {}), [])
  useEffect(() => { loadViews() }, [loadViews])
  const chips = chipsFor(f)
  const saveView = async name => {
    try { await send('/api/views', 'POST', { name, query: queryFrom(f, '') }); flash(true, `Saved the view “${name}”`); loadViews() }
    catch (e) { flash(false, e.message) }
  }
  const applyView = v => { setPanel(false); router.replace(`${pathname}${v.query}`, { scroll: false }) }
  const deleteView = async v => {
    try { await api(`/api/views/${v.id}`, { method: 'DELETE' }); flash(true, `Deleted the view “${v.name}”`); loadViews() } catch (e) { flash(false, e.message) }
  }
  const reviewRows = useMemo(() => all.filter(a => a.stage === 'ready_for_review').sort(byScore), [all])
  const columns = useMemo(() => POST_SUBMISSION.map(stage => ({ stage, cards: filtered.filter(a => a.stage === stage).sort(byScore) })), [filtered])
  // The deck: the Inbox best first, the cards put off with "Later" at the back.
  const [later, setLater] = useState([])
  const [front, setFront] = useState([])                // brought back by Undo: back on top of the deck
  const [keep, setKeep] = useState('')                  // the card on screen stays put while jobs stream in
  const [decided, setDecided] = useState([])            // times (ms) of this session's deck decisions
  const deck = useMemo(() => (view === 'cards' ? deckOrder(rows, later, front, keep) : []), [view, rows, later, front, keep])
  useEffect(() => { if (view === 'cards' && (!keep || !rows.some(a => a.job_id === keep))) setKeep(deck[0]?.job_id || '') }, [view, rows, deck, keep])

  // Jobs that streamed in since you looked: a pill to bring them up, and a highlight on their rows.
  const [known, setKnown] = useState(null)
  const inboxIds = useMemo(() => all.filter(a => a.stage === 'found'), [all])
  useEffect(() => { if (apps && !known) setKnown(new Set(inboxIds.map(a => a.job_id))) }, [apps, inboxIds, known])
  const fresh = useMemo(() => new Set(arrivals(inboxIds, known)), [inboxIds, known])
  const seeFresh = () => {
    setKnown(new Set(inboxIds.map(a => a.job_id)))
    setKeep('')
    setTab('inbox')
    document.querySelector('.il')?.scrollTo({ top: 0, behavior: 'smooth' })
  }
  const order = view === 'board' ? columns.flatMap(c => c.cards) : view === 'review' ? reviewRows : view === 'cards' ? deck : rows

  // Leaving a tab clears what was selected in it.
  useEffect(() => { setSelected(new Set()) }, [tab])
  // The open job, when it is in the list, is the cursor too.
  useEffect(() => { if (openId) setCursor(openId) }, [openId])

  const openApp = all.find(a => a.job_id === openId) || null
  const open = id => { setOpenId(id); setCursor(id) }
  const close = () => setOpenId('')
  const setTab = key => {
    setF(x => ({ ...x, stages: stagesForTab(key), view: key === 'review' ? 'review' : key === 'applied' && x.view === 'board' ? 'board'
      : key === 'inbox' && x.view === 'list' ? 'list' : '' }))
    setOpenId('')
  }

  const neighbour = id => {
    const i = order.findIndex(a => a.job_id === id)
    if (i < 0) return order[0]?.job_id || ''
    return (order[i + 1] || order[i - 1] || {}).job_id || ''
  }
  const step = d => {
    if (!order.length) return
    const i = order.findIndex(a => a.job_id === (cursor || openId))
    const next = order[Math.max(0, Math.min(order.length - 1, i < 0 ? 0 : i + d))]
    setCursor(next.job_id)
    if (view === 'list' || openId) setOpenId(next.job_id)
    document.getElementById(`card-${next.job_id}`)?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  }

  // ── triage actions: instant, with Undo ──────────────────────────
  // The jobs move at once; the server is asked after; only what it refused moves back.
  const act = async (kind, ids) => {
    if (!ids.length) return { done: [] }
    if (kind === 'queue' && !owner) { flash(false, OWNER_ONLY); return { done: [] } }
    const before = (apps || []).filter(a => ids.includes(a.job_id)).map(a => ({ job_id: a.job_id, stage: a.stage, title: a.title }))
    setApps(xs => withStages(xs || [], ids, kind === 'queue' ? 'queued' : 'skipped'))
    let done = [], refused = [], why = ''
    try {
      if (kind === 'queue') {
        const r = await send('/api/applications/queue', 'POST', { job_ids: ids })
        done = r.queued || []
        refused = (r.rejected || []).map(x => x.job_id); why = r.rejected?.[0]?.message || ''
      } else {
        for (const id of ids) {
          try { await send(`/api/apply/skip/${id}`, 'POST'); done.push(id) } catch (e) { refused.push(id); why = why || e.message }
        }
      }
    } catch (e) { refused = ids; done = []; why = e.message }
    if (refused.length) setApps(xs => revertRefused(xs || [], before, refused))
    const verb = kind === 'queue' ? 'Queued' : 'Skipped'
    const what = done.length === 1 ? `“${before.find(b => b.job_id === done[0])?.title || 'job'}”` : `${done.length} jobs`
    const text = !done.length ? `Not ${verb.toLowerCase()}: ${why}`
      : refused.length ? `${verb} ${what} — ${refused.length} kept: ${why}` : `${verb} ${what}`
    const plan = undoFor(kind, before, done)
    setMsg({ ok: !refused.length, text, at: Date.now(), undo: plan && (() => undo(plan, what)) })
    setSelected(new Set())
    scheduleReload()
    setPanelVersion(v => v + 1)
    return { done }
  }
  const undo = async (plan, what) => {
    setMsg(null)
    const ids = plan.unqueue || plan.restore.map(r => r.id)
    const was = plan.unqueue ? ids.map(id => ({ id, to: 'found' })) : plan.restore
    setApps(xs => (xs || []).map(a => { const w = was.find(r => r.id === a.job_id); return w ? { ...a, stage: w.to } : a }))
    setLater(l => l.filter(id => !ids.includes(id)))
    setFront(f => [...f.filter(id => !ids.includes(id)), ...ids])
    try {
      if (plan.unqueue) await send('/api/applications/unqueue', 'POST', { job_ids: plan.unqueue })
      else for (const r of plan.restore) await send(`/api/applications/${r.id}/transition`, 'POST', { to: r.to, note: 'Undo' })
      flash(true, `Back where it was: ${what}`)
    } catch (e) { flash(false, `Could not undo: ${e.message}`) }
    load()
  }
  const afterAct = async (ids, kind) => {
    const next = ids.length === 1 && ids[0] === (openId || cursor) ? neighbour(ids[0]) : openId
    const p = act(kind, ids)
    if (ids.length === 1 && view === 'list') { setCursor(next); setOpenId(next) }
    await p
  }
  const decide = (kind, id) => { setDecided(t => [...t, Date.now()]); setFront(f => f.filter(x => x !== id)); setKeep(''); act(kind, [id]) }
  const current = openId || cursor
  const reviewContext = view === 'review' || openApp?.stage === 'ready_for_review'
  useShortcuts({
    j: () => step(1), k: () => step(-1), Enter: () => { if (cursor) open(cursor) },
    q: () => { if (current && QUEUEABLE.includes(all.find(a => a.job_id === current)?.stage)) afterAct([current], 'queue') },
    x: () => { if (current && SKIPPABLE.includes(all.find(a => a.job_id === current)?.stage)) afterAct([current], 'skip') },
    o: () => { const a = all.find(x => x.job_id === current); const h = safeHref(a?.url); if (h) window.open(h, '_blank', 'noopener') },
    ' ': () => { if (current) setSelected(s => { const n = new Set(s); n.has(current) ? n.delete(current) : n.add(current); return n }) },
  }, !reviewContext && view !== 'cards')
  // The deck: arrows (and q / x / l), with the open details panel taking over only Escape.
  const topCard = deck[0]
  useShortcuts({
    ArrowRight: () => topCard && decide('queue', topCard.job_id), q: () => topCard && decide('queue', topCard.job_id),
    ArrowLeft: () => topCard && decide('skip', topCard.job_id), x: () => topCard && decide('skip', topCard.job_id),
    ArrowDown: () => topCard && putOff(topCard.job_id), l: () => topCard && putOff(topCard.job_id),
    ArrowUp: () => topCard && open(topCard.job_id), Enter: () => topCard && open(topCard.job_id),
    o: () => { const h = safeHref(topCard?.url); if (h) window.open(h, '_blank', 'noopener') },
  }, view === 'cards' && !openId)
  useShortcuts({ z: () => { if (msg?.undo) msg.undo() } })
  useShortcuts({ Escape: () => { if (panel) setPanel(false); else if (selected.size) setSelected(new Set()); else if (openId) close(); else setCursor('') } })

  const putOff = id => { setDecided(t => [...t, Date.now()]); setFront(f => f.filter(x => x !== id)); setKeep(''); setLater(l => [...l.filter(x => x !== id), id]) }

  const nextReview = useCallback(id => {
    const ready = (apps || []).filter(a => a.stage === 'ready_for_review' && a.job_id !== id).sort(byScore)
    if (ready.length) open(ready[0].job_id)
    else { close(); flash(true, 'The review queue is empty.') }
  }, [apps])

  const move = async (app, to) => {
    if (app.stage === to) return
    if (!POST_SUBMISSION.includes(app.stage) || !POST_SUBMISSION.includes(to)) {
      flash(false, `Cards can only be dragged between post-submission stages (${stageLabel(app.stage)} → ${stageLabel(to)} is not one). Use the panel's actions instead.`)
      return
    }
    try {
      await send(`/api/applications/${app.job_id}/transition`, 'POST', { to })
      flash(true, `${app.title}: moved to ${stageLabel(to)}`)
    } catch (e) { flash(false, e.message) }
    load()
    setPanelVersion(v => v + 1)
  }

  // Score now: every Inbox job gets the keyword match at once; with AI scoring on, the
  // model scores the rest in the background (progress bar), and the list reloads when it ends.
  const [scoring, setScoring] = useState(false)
  useOnSearchFinished(load)
  const scoreNow = async () => {
    if (!owner) { flash(false, 'Only the owner account can score jobs.'); return }
    setScoring(true)
    try {
      const r = await send('/api/pipeline/score', 'POST')
      const quick = `Scored ${r.quick} Inbox job${r.quick === 1 ? '' : 's'} by keywords`
      flash(true, r.ai === 'started' ? `${quick}. The AI is now scoring ${r.ai_total} — see the progress bar.`
        : r.ai === 'busy' ? `${quick}. AI scoring waits for the running search: press Score again when it ends.`
        : r.ai === 'none' ? `${quick}. Every Inbox job already has an AI score.`
        : `${quick}. For a closer look, turn on AI scoring in Settings → AI model.`)
    } catch (e) { flash(false, e.message) }
    setScoring(false)
    load()
  }

  const purge = async () => {
    setMenu(false)
    if (!confirm('Remove duplicate jobs?')) return
    try { const r = await api('/api/jobs/duplicates', { method: 'DELETE' }); flash(true, `Removed ${r.deleted} duplicate${r.deleted === 1 ? '' : 's'}`) } catch (e) { flash(false, e.message) }
    load()
  }
  const clearAll = async () => {
    setMenu(false)
    if (!confirm('Clear all pending/skipped/error jobs? Jobs already Applied or In Progress are kept so your Pipeline board is unaffected. This cannot be undone.')) return
    try { const r = await api('/api/jobs', { method: 'DELETE' }); flash(true, `Cleared ${r.deleted} job${r.deleted === 1 ? '' : 's'}`) } catch (e) { flash(false, e.message) }
    if (openId && !(await api(`/api/applications/${openId}`).catch(() => null))) close()
    load()
  }

  const set = (k, v) => setF(x => ({ ...x, [k]: v }))
  const selIds = rows.filter(a => selected.has(a.job_id)).map(a => a.job_id)
  const canQueueSel = selIds.length && rows.filter(a => selected.has(a.job_id)).every(a => QUEUEABLE.includes(a.stage))
  const canSkipSel = selIds.length && rows.filter(a => selected.has(a.job_id)).every(a => SKIPPABLE.includes(a.stage))
  const summary = apps ? [counts.inbox && `${counts.inbox} to triage`, counts.needs && `${counts.needs} need you`, counts.review && `${counts.review} to review`,
    counts.applied && `${counts.applied} applied`].filter(Boolean).join(' · ') || 'Nothing here yet' : 'Loading…'

  return (
    <div className="ws">
      <header className="ws-head">
        <div>
          <h1>Applications</h1>
          <p className="sub">{summary}
            <span className={`live ${liveMode}`} title={liveMode === 'live' ? 'Live updates on' : 'Live stream unavailable - refreshing every 20 s'}>
              <span className={`dot ${liveMode === 'live' ? 'g pulse' : liveMode === 'polling' ? 'y' : 'm'}`} />{liveMode === 'live' ? 'live' : liveMode === 'polling' ? 'polling' : '…'}
            </span></p>
        </div>
        <div className="ws-menu">
          <button className="btn sm" disabled={scoring || !owner || !counts.inbox} title={owner ? 'Score every Inbox job now' : 'Only the owner account can score jobs.'}
                  onClick={scoreNow} data-score-now>{scoring ? <span className="sp" /> : <Gauge className="lucide" />}Score Inbox</button>
          <button className="btn sm ghost" aria-label="More actions" aria-expanded={menu} onClick={() => setMenu(m => !m)}><MoreHorizontal className="lucide" /></button>
          {menu && <div className="pop" role="menu" onMouseLeave={() => setMenu(false)}>
            <button role="menuitem" onClick={purge}>Purge dupes</button>
            <button role="menuitem" className="danger" onClick={clearAll}>Clear all jobs</button>
          </div>}
        </div>
      </header>

      <nav className="tabs ws-tabs" role="tablist" aria-label="Stage">
        {TABS.map(t => (
          <button key={t.key} role="tab" aria-selected={tab === t.key} className={`tab ${tab === t.key ? 'on' : ''}`} onClick={() => setTab(t.key)}>
            {t.label}{apps && <span className="n" key={t.key === 'review' ? reviewRows.length : counts[t.key]}>{t.key === 'review' ? reviewRows.length : counts[t.key]}</span>}
          </button>))}
        {tab === 'custom' && <span className="tab on">{linkOnly ? 'Filtered: every stage' : `Filtered: ${f.stages.map(stageLabel).join(', ')}`}
          <button className="link-btn" onClick={() => { setF(x => ({ ...x, followups: '', within: '' })); setTab('inbox') }} aria-label="Clear the filter">✕</button></span>}
      </nav>

      <div className="ws-filters">
        <input className="in q" placeholder="Search title, company or place…" value={f.q} onChange={e => set('q', e.target.value)} aria-label="Search" />
        <button className={`btn filters-btn ${chips.length ? 'on' : ''}`} onClick={() => setPanel(p => !p)} aria-expanded={panel} data-filters-btn>
          <SlidersHorizontal className="lucide" />Filters{chips.length > 0 && <span className="fb-n">{chips.length}</span>}</button>
        {views.slice(0, 4).map(v => <button key={v.id} className="pill view-pill" onClick={() => applyView(v)} data-view-pill={v.name}>{v.name}</button>)}
        <label className={`chk-pill ${f.form ? 'on' : ''}`} title="Only jobs that apply on the company's own form (no job-board account needed)">
          <input type="checkbox" checked={!!f.form} onChange={e => set('form', e.target.checked ? '1' : '')} />Company form</label>
        {tab === 'applied' && !isNarrowed(f.stages) && <div className="pills" role="radiogroup" aria-label="View">
          {[['list', 'List'], ['board', 'Board']].map(([v, l]) => <button key={v} role="radio" aria-checked={view === v} className={`pill ${view === v ? 'on' : ''}`}
                                                                         onClick={() => set('view', v)}>{l}</button>)}</div>}
        {tab === 'inbox' && <div className="pills" role="radiogroup" aria-label="Inbox view">
          {[['', 'Cards', Rows3], ['list', 'List', LayoutList]].map(([v, l, Icon]) => (
            <button key={l} role="radio" aria-checked={(view === 'cards') === (v === '')} className={`pill ${(view === 'cards') === (v === '') ? 'on' : ''}`}
                    onClick={() => set('view', v)}><Icon className="lucide" /> {l}</button>))}</div>}
        <AnimatePresence>{fresh.size > 0 && (
          <motion.button key="fresh" className="fresh-pill" onClick={seeFresh} data-fresh={fresh.size}
            initial={{ opacity: 0, y: -6, scale: 0.9 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, scale: 0.9, transition: { duration: 0.12 } }}>
            ↑ {fresh.size} new job{fresh.size === 1 ? '' : 's'} found</motion.button>)}</AnimatePresence>
        {view === 'list' && <span className="keys" aria-hidden="true"><span>j/k move</span><span>q queue</span><span>x skip</span><span>z undo</span><span>o open</span><span>space select</span></span>}
        {chips.length > 0 && (
          <div className="ws-chips" aria-label="Active filters">
            {chips.map(c => (
              <span key={c.key} className="chip filter-chip" data-chip={c.key}>{c.label}
                <button className="link-btn" onClick={() => setF(x => withoutChip(x, c))} aria-label={`Remove the filter ${c.label}`}><X className="lucide" /></button></span>))}
            <button className="link-btn" onClick={() => setF(x => clearFilters(x))} data-clear-filters>Clear all</button>
          </div>)}
      </div>
      <AnimatePresence>{panel && <FilterPanel key="fp" f={f} set={set} sources={sources} ats={atsHosts} count={rows.length} total={unfiltered}
        onClose={() => setPanel(false)} onClear={() => setF(x => clearFilters(x))} views={views} onSaveView={saveView} onApplyView={applyView} onDeleteView={deleteView} />}</AnimatePresence>

      <AnimatePresence>{msg && <motion.div key="toast" className={`toast ${msg.ok ? 'ok' : 'err'} ${msg.undo ? 'has-undo' : ''}`} role="status"
        initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: 8, transition: { duration: 0.12 } }}>
        <span>{msg.text}</span>
        {msg.undo && <button className="undo" onClick={msg.undo} data-undo>Undo <kbd>Z</kbd></button>}
        {msg.undo && <motion.i key={msg.at} className="toast-timer" initial={{ scaleX: 1 }} animate={{ scaleX: 0 }} transition={{ duration: 6, ease: 'linear' }} aria-hidden="true" />}
      </motion.div>}</AnimatePresence>

      {err ? <div className="empty">{err}</div>
        : !apps ? <div className="ws-scroll"><SkeletonRows n={9} label="Loading applications" /></div>
        : view === 'review' ? <div className="ws-scroll"><ReviewQueue rows={reviewRows} onChanged={load} onOpen={open} flash={flash} /></div>
        : view === 'cards' ? (
          <div className="ws-scroll deck-scroll">
            <TriageDeck rows={deck} apps={all} done={decided.length} owner={owner} onDecide={decide} onLater={putOff} onOpen={open}
                        secsLeft={minutesLeft(deck.length, pace(decided))} />
            <AnimatePresence>{openId && <SidePanel key={openId} jobId={openId} version={panelVersion} onClose={close} owner={owner}
                                  onQueue={id => { close(); decide('queue', id) }} onSkip={id => { close(); decide('skip', id) }}
                                  onChanged={() => { load(); setPanelVersion(v => v + 1) }} onNextReview={nextReview} flash={flash} />}</AnimatePresence>
          </div>)
        : view === 'board' ? (
          <div className={`ws-body ${openId ? '' : 'solo'}`}>
            <div className="ws-scroll" style={{ overflow: 'hidden' }}><Board columns={columns} cursor={cursor} openId={openId} onOpen={open} onMove={move} /></div>
            {openId && <SidePanel key={openId} inline jobId={openId} version={panelVersion} onClose={close} owner={owner}
                                  onChanged={() => { load(); setPanelVersion(v => v + 1) }} onNextReview={nextReview} flash={flash} />}
          </div>)
        : (
          <div className={`ws-body ${openId ? '' : 'solo'}`}>
            <div className="il">
              <InboxList rows={rows} cursor={cursor} openId={openId} selected={selected} sort={sort} setSort={setSort} onOpen={open} fresh={fresh}
                         onSelect={(id, on) => setSelected(s => { const n = new Set(s); on ? n.add(id) : n.delete(id); return n })}
                         onSelectAll={on => setSelected(on ? new Set(rows.map(a => a.job_id)) : new Set())}
                         empty={EMPTY[tab] || EMPTY.custom} />
              {selIds.length > 0 && (
                <div className="bulk" role="toolbar" aria-label="Selected jobs">
                  <b>{selIds.length} selected</b>
                  <button className="btn sm primary" disabled={!canQueueSel || !owner} title={owner ? '' : OWNER_ONLY} onClick={() => afterAct(selIds, 'queue')}>Queue</button>
                  <button className="btn sm" disabled={!canSkipSel} onClick={() => afterAct(selIds, 'skip')}>Skip</button>
                  <button className="btn sm ghost" onClick={() => setSelected(new Set())}>Clear</button>
                </div>)}
            </div>
            {openId && <SidePanel key={openId} inline jobId={openId} version={panelVersion} onClose={close} owner={owner}
                                  onQueue={id => afterAct([id], 'queue')} onSkip={id => afterAct([id], 'skip')}
                                  onChanged={() => { load(); setPanelVersion(v => v + 1) }} onNextReview={nextReview} flash={flash} />}
          </div>)}
    </div>
  )
}
