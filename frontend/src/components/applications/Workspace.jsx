// frontend/src/components/applications/Workspace.jsx
//
// The Applications workspace: every job in one place (it replaces the old Jobs page and
// the backend's Apply and Pipeline pages). A board (one column per lifecycle stage), a
// sortable list, or the Review queue; a side panel for one application.
//
// Filters come from - and are written back to - the URL query, so other pages can link
// in filtered: ?stage=a,b  ?needs_you=1  ?ready_for_review=1  ?source=  ?search= (or q=)
// ?title=  ?min= ?max= (score)  ?view=board|list|review  ?job=<job_id> (opens its panel).
//
// Keys: j / k move between cards, Enter opens, Esc closes. In the Review queue, and on a
// ready_for_review card in the panel, A / E / S / N / ? take over instead (ReviewPanel).
// Nothing reacts while you are typing in a field.
'use client'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { usePathname, useRouter, useSearchParams } from 'next/navigation'
import { api, send } from '@/lib/api'
import { POST_SUBMISSION, STAGE_KEYS, stageLabel } from '@/lib/stages'
import { applyFilters, byScore, filtersFrom, queryFrom } from '@/lib/filters'
import { useLiveEvents, useShortcuts } from '@/lib/live'
import Toolbar from './Toolbar'
import Board from './Board'
import ListView from './ListView'
import ReviewQueue from './ReviewQueue'
import SidePanel from './SidePanel'
import Analytics from './Analytics'

export default function Workspace() {
  const params = useSearchParams()
  const router = useRouter()
  const pathname = usePathname()
  const [f, setF] = useState(() => filtersFrom(params))
  const [openId, setOpenId] = useState(() => params.get('job') || '')
  const [apps, setApps] = useState(null)
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState(null)
  const [cursor, setCursor] = useState('')
  const [panelVersion, setPanelVersion] = useState(0)
  const [sort, setSort] = useState({ key: 'ai_score', dir: -1 })
  const reloadTimer = useRef(null)

  const load = useCallback(() => api('/api/applications?limit=2000').then(rows => { setApps(rows); setErr('') })
    .catch(e => setErr(e.message)), [])
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
  const rows = useMemo(() => applyFilters(all, f), [all, f])
  const titles = useMemo(() => [...new Set(all.map(a => a.title).filter(Boolean))].sort((a, b) => a.localeCompare(b)), [all])
  const sources = useMemo(() => [...new Set(all.map(a => (a.source || '').toLowerCase()).filter(Boolean))].sort(), [all])
  // A title that is no longer present (Clear all, a purge) stops filtering.
  useEffect(() => { if (apps && f.title && !titles.includes(f.title)) setF(x => ({ ...x, title: '' })) }, [apps, titles, f.title])

  const listRows = useMemo(() => {
    const k = sort.key
    return [...rows].sort((a, b) => {
      const x = a[k], y = b[k]
      if (x == null && y == null) return 0
      if (x == null) return 1
      if (y == null) return -1
      return (typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y))) * sort.dir
    })
  }, [rows, sort])
  const columns = useMemo(() => STAGE_KEYS.map(stage => ({ stage, cards: rows.filter(a => a.stage === stage).sort(byScore) })), [rows])
  const reviewRows = useMemo(() => rows.filter(a => a.stage === 'ready_for_review').sort(byScore), [rows])
  const order = f.view === 'list' ? listRows : f.view === 'review' ? reviewRows : columns.flatMap(c => c.cards)

  const openApp = all.find(a => a.job_id === openId) || null
  const open = id => { setOpenId(id); setCursor(id) }
  const close = () => setOpenId('')

  const step = d => {
    if (!order.length) return
    const i = order.findIndex(a => a.job_id === (cursor || openId))
    const next = order[Math.max(0, Math.min(order.length - 1, i < 0 ? 0 : i + d))]
    setCursor(next.job_id)
    if (openId) setOpenId(next.job_id)
    document.getElementById(`card-${next.job_id}`)?.scrollIntoView({ block: 'nearest', inline: 'nearest' })
  }
  const reviewContext = f.view === 'review' || openApp?.stage === 'ready_for_review'
  useShortcuts({ j: () => step(1), k: () => step(-1), Enter: () => { if (cursor) open(cursor) } }, !reviewContext)
  useShortcuts({ Escape: () => { if (openId) close(); else setCursor('') } })

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

  const purge = async () => {
    if (!confirm('Remove duplicate jobs?')) return
    try { const r = await api('/api/jobs/duplicates', { method: 'DELETE' }); flash(true, `Removed ${r.deleted} duplicate${r.deleted === 1 ? '' : 's'}`) } catch (e) { flash(false, e.message) }
    load()
  }
  const clearAll = async () => {
    if (!confirm('Clear all pending/skipped/error jobs? Jobs already Applied or In Progress are kept so your Pipeline board is unaffected. This cannot be undone.')) return
    try { const r = await api('/api/jobs', { method: 'DELETE' }); flash(true, `Cleared ${r.deleted} job${r.deleted === 1 ? '' : 's'}`) } catch (e) { flash(false, e.message) }
    if (openId && !(await api(`/api/applications/${openId}`).catch(() => null))) close()
    load()
  }

  return (
    <div className={`ws ${openId ? 'with-panel' : ''}`}>
      <h1>Applications</h1>
      <p className="sub">
        {apps ? `${rows.length} of ${all.length} applications` : 'Loading…'}
        <span className={`live ${liveMode}`} title={liveMode === 'live' ? 'Live updates on' : 'Live stream unavailable - refreshing every 20 s'}>
          {liveMode === 'live' ? '● live' : liveMode === 'polling' ? '○ polling' : '…'}
        </span>
      </p>
      <Toolbar f={f} setF={setF} titles={titles} sources={sources} onPurge={purge} onClearAll={clearAll}
               reviewCount={all.filter(a => a.stage === 'ready_for_review').length} />
      {msg && <div className={`toast ${msg.ok ? 'ok' : 'err'}`} role="status">{msg.text}</div>}
      {err ? <div className="card empty">{err}</div>
        : !apps ? null
        : f.view === 'review' ? <ReviewQueue rows={reviewRows} onChanged={load} onOpen={open} flash={flash} />
        : f.view === 'list' ? <ListView rows={listRows} sort={sort} setSort={setSort} cursor={cursor} openId={openId} onOpen={open} />
        : <Board columns={columns} cursor={cursor} openId={openId} onOpen={open} onMove={move} />}
      {apps && f.view !== 'review' && <Analytics version={all.length} />}
      {openId && <SidePanel key={openId} jobId={openId} version={panelVersion} onClose={close}
                            onChanged={() => { load(); setPanelVersion(v => v + 1) }} onNextReview={nextReview} flash={flash} />}
    </div>
  )
}
