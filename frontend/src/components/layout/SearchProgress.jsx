// frontend/src/components/layout/SearchProgress.jsx
//
// Job-search progress, two ways:
//   SearchBar    a thin bar across the top of every page plus a status pill while a search
//                runs; a toast with the result when it finishes
//   SearchSteps  the full picture on Today: every phase with its done/total and jobs found,
//                what it is doing right now, elapsed time, and the last log lines
// Both read lib/pipeline (one shared poller of GET /api/pipeline/status).
'use client'
import { useCallback, useState } from 'react'
import Link from 'next/link'
import { AnimatePresence, motion } from 'motion/react'
import { Check, Loader2, Minus, X } from 'lucide-react'
import { fmtElapsed, useOnSearchFinished, usePipeline } from '@/lib/pipeline'

const summary = s => {
  const st = s?.stats || {}
  if (st.kind === 'score') return s?.error ? `Scoring failed: ${s.error}` : `Scoring ${s?.cancelled ? 'cancelled' : 'finished'} — ${st.scored ?? 0} jobs scored by AI`
  if (s?.error) return `Search failed: ${s.error}`
  if (s?.cancelled) return `Search cancelled — ${st.new_added ?? s.new ?? 0} new jobs saved`
  return `Search finished — ${st.total_found ?? s?.found ?? 0} found, ${st.new_added ?? s?.new ?? 0} new`
}

export function SearchBar() {
  const s = usePipeline()
  const [done, setDone] = useState(null)
  const onFinish = useCallback(f => { setDone(f); setTimeout(() => setDone(null), 9000) }, [])
  useOnSearchFinished(onFinish)
  const running = !!s?.running
  return (
    <>
      <AnimatePresence>
        {running && (
          <motion.div className="search-bar" role="progressbar" aria-label="Job search progress"
                      aria-valuemin={0} aria-valuemax={100} aria-valuenow={s.percent}
                      initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
            <motion.i initial={{ width: 0 }} animate={{ width: `${Math.max(3, s.percent)}%` }} transition={{ duration: 0.5, ease: 'easeOut' }} />
          </motion.div>
        )}
      </AnimatePresence>
      <AnimatePresence mode="wait">
        {running && (
          <motion.div key="pill" className="search-pill" role="status" initial={{ opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -6 }}>
            <Loader2 className="lucide spin" aria-hidden="true" />
            <span className="sp-now">{s.cancelled ? 'Cancelling…' : s.current || 'Searching'}</span>
            <span className="m">{s.percent}% · {s.found} found · {fmtElapsed(s.elapsed)}</span>
            <Link href="/" className="m" style={{ textDecoration: 'underline' }}>details</Link>
          </motion.div>
        )}
        {!running && done && (
          <motion.div key="done" className={`search-pill ${done.error ? 'err' : 'ok'}`} role="status" initial={{ opacity: 0, y: -6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -6 }}>
            {done.error ? <X className="lucide" aria-hidden="true" /> : <Check className="lucide" aria-hidden="true" />}
            <span className="sp-now">{summary(done)}</span>
            {!done.error && <Link href="/applications" className="m" style={{ textDecoration: 'underline' }}>open</Link>}
            <button className="btn sm ghost" aria-label="Dismiss" onClick={() => setDone(null)}><X className="lucide" /></button>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  )
}

const ICON = { done: Check, running: Loader2, failed: X, skipped: Minus }

export function SearchSteps() {
  const s = usePipeline()
  if (!s || (!s.running && !s.finished_at)) return <p className="m" data-search-idle>No search has run since the server started.</p>
  return (
    <div className="search-steps" data-running={s.running ? 'true' : 'false'}>
      <div className="row">
        <b>{s.running ? (s.cancelled ? 'Cancelling…' : s.current) : summary(s)}</b>
        <span className="spacer" />
        <span className="m">{s.percent}% · {fmtElapsed(s.elapsed)}</span>
      </div>
      <div className="search-track" aria-hidden="true"><motion.i initial={false} animate={{ width: `${s.percent}%` }} transition={{ duration: 0.5 }} /></div>
      <ol>{s.phases.map(p => {
        const Icon = ICON[p.status]
        return (
          <li key={p.key} data-phase={p.key} data-status={p.status}>
            <span className="ph-ic">{Icon ? <Icon className={`lucide ${p.status === 'running' ? 'spin' : ''}`} aria-hidden="true" /> : null}</span>
            <span>{p.label}</span>
            <span className="spacer" />
            {p.total > 0 && <span className="m">{p.done}/{p.total}</span>}
            {['boards', 'sources', 'companies'].includes(p.key) && p.status !== 'pending' && <span className="m">{p.found} found</span>}
          </li>)
      })}</ol>
      {s.log?.length > 0 && (
        <details className="search-log"><summary>Log</summary>
          <pre>{s.log.map(l => l.msg).join('\n')}</pre>
        </details>)}
    </div>
  )
}
