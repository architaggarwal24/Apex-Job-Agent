// frontend/src/components/applications/TriageDeck.jsx
//
// The Inbox as a deck: one job at a time, best fit first. Drag the card right (or →, Q)
// to queue it, left (or ←, X) to skip it, ↓ (L) puts it at the back, ↑ / Enter opens the
// details. Every decision shows at once and can be undone from the toast (Z) - the
// Workspace owns the actions; this component only shows the deck and reports gestures.
//
//   left   up next: the following five jobs
//   centre the card (company, title, where it applies, the fit and why, the posting's start)
//   right  the live search, what is queued, what needs you
'use client'
import { useEffect, useRef, useState } from 'react'
import Link from 'next/link'
import { AnimatePresence, motion, useMotionValue, useTransform } from 'motion/react'
import { ArrowDown, ArrowLeft, ArrowRight, ArrowUp } from 'lucide-react'
import { api } from '@/lib/api'
import { applyHost, reasonLabel, scoreClass } from '@/lib/stages'
import { fitOf } from '@/lib/filters'
import { usePipeline } from '@/lib/pipeline'
import { age } from './InboxList'

const THROW = 140                      // px of drag that counts as a decision
const cache = new Map()               // job_id -> detail (reason, description), shared across renders

function useDetail(id) {
  const [d, setD] = useState(() => (id ? cache.get(id) || null : null))
  useEffect(() => {
    if (!id) return undefined
    if (cache.has(id)) { setD(cache.get(id)); return undefined }
    let live = true
    setD(null)
    api(`/api/applications/${id}`).then(x => { cache.set(id, x); if (live) setD(x) }).catch(() => {})
    return () => { live = false }
  }, [id])
  return d
}

const prefetch = ids => ids.forEach(id => { if (id && !cache.has(id)) api(`/api/applications/${id}`).then(x => cache.set(id, x)).catch(() => {}) })
const snippet = t => String(t || '').replace(/[#*_>`]/g, '').replace(/\s+/g, ' ').trim().slice(0, 320)
const hue = s => [...String(s || '?')].reduce((h, c) => (h * 31 + c.charCodeAt(0)) % 360, 7)

function Card({ a, top, dir, onDecide, onOpen }) {
  const x = useMotionValue(0)
  const rotate = useTransform(x, [-320, 0, 320], [-14, 0, 14])
  const queueInk = useTransform(x, [30, THROW], [0, 1])
  const skipInk = useTransform(x, [-THROW, -30], [1, 0])
  const d = useDetail(a.job_id)
  const fit = fitOf(a)
  const form = a.apply_url && a.apply_url !== a.url ? applyHost(a.apply_url) : ''
  const reason = d ? (d.ai_score_reason || d.fit_reason || 'Not scored yet: press Score Inbox for a quick match.') : null
  return (
    <motion.article className="deck-card" data-deck-card={top ? a.job_id : undefined} style={top ? { x, rotate } : undefined}
      drag={top ? 'x' : false} dragSnapToOrigin dragElastic={0.9}
      onDragEnd={(_, info) => { if (info.offset.x > THROW) onDecide('queue'); else if (info.offset.x < -THROW) onDecide('skip') }}
      initial={{ opacity: 0, y: 26, scale: 0.96 }} animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ x: dir.current * 720, rotate: dir.current * 22, opacity: 0, transition: { duration: 0.32, ease: [0.4, 0, 0.6, 1] } }}
      transition={{ type: 'spring', stiffness: 380, damping: 32 }}>
      {top && <>
        <motion.span className="stamp q" style={{ opacity: queueInk }} aria-hidden="true">Queue →</motion.span>
        <motion.span className="stamp s" style={{ opacity: skipInk }} aria-hidden="true">← Skip</motion.span>
      </>}
      <div className="dc-co">
        <span className="dc-logo" style={{ '--h': hue(a.company) }} aria-hidden="true">{(a.company || '?').trim()[0]?.toUpperCase()}</span>
        <span>{a.company}{a.location ? ` · ${a.location}` : ''}{(a.posted_at || a.found_at) ? ` · ${age(a.posted_at || a.found_at)}` : ''}</span>
      </div>
      <h2 className="dc-title">{a.title}</h2>
      <div className="dc-pills">
        {form ? <span className="dc-pill g">Applies on {form} — no login</span> : <span className="dc-pill">Applies on {a.source || 'the job board'}</span>}
        {a.salary && <span className="dc-pill">{a.salary}</span>}
        <span className="dc-pill">{a.source}</span>
      </div>
      <div className={`dc-fit ${scoreClass(fit)}`}>
        <span className="dc-n" data-fit-kind={a.fit_kind || ''}>{fit ?? '—'}</span>
        <div>
          <b>{a.fit_kind === 'ai' ? 'AI fit' : a.fit_kind === 'quick' ? 'Quick match' : 'Not scored'}</b>
          {reason == null ? <span className="skel" style={{ display: 'block', height: '2.5em', marginTop: '.375rem' }} /> : <p>{reason}</p>}
        </div>
      </div>
      {top && <p className="dc-desc">{d ? (snippet(d.description) || 'No description saved for this posting.') : <span className="skel" style={{ display: 'block', height: '3.5em' }} />}</p>}
      <div className="dc-foot">
        <span>{a.source}{a.posted_at ? ` · posted ${a.posted_at.slice(0, 10)}` : ''}</span>
        <button className="link-btn" onClick={() => onOpen(a.job_id)}>Details</button>
      </div>
    </motion.article>
  )
}

function Rail({ apps, onOpen }) {
  const s = usePipeline()
  const queued = apps.filter(a => a.stage === 'queued' || a.stage === 'preparing').sort((a, b) => (fitOf(b) ?? -1) - (fitOf(a) ?? -1))
  const needs = apps.filter(a => a.stage === 'needs_you')
  const row = (a, badge, cls, sub) => (
    <button key={a.job_id} className="rail-row" onClick={() => onOpen(a.job_id)}>
      <span className={`rail-d ${cls}`}>{badge}</span>
      <span className="rail-t"><b>{a.title}</b><small>{sub}</small></span>
    </button>)
  return (
    <aside className="rail" aria-label="Queue and search">
      <section className="rail-box" data-rail="search">
        {s?.running
          ? <div className="rail-live"><span className="dot r pulse" />{s.current || 'Searching'}<small>{s.percent}% · {s.new ?? 0} new</small></div>
          : <div className="rail-live idle"><span className="dot m" />No search running<Link className="link-btn" href="/">Run one</Link></div>}
      </section>
      <section className="rail-box" data-rail="queued">
        <h3>Queued <em>{queued.length}</em></h3>
        {queued.length ? queued.slice(0, 5).map(a => row(a, fitOf(a) ?? '—', 'g', `${a.company} · ${a.stage === 'preparing' ? 'applying now…' : 'waiting'}`))
          : <p className="m">Queue a card and it lands here.</p>}
      </section>
      <section className="rail-box" data-rail="needs">
        <h3>Needs you <em>{needs.length}</em></h3>
        {needs.length ? needs.slice(0, 4).map(a => row(a, '!', 'r', `${a.company} · ${reasonLabel(a.blocked_reason) || 'waiting for you'}`))
          : <p className="m">Nothing is stuck.</p>}
      </section>
    </aside>
  )
}

export default function TriageDeck({ rows, apps, done, secsLeft, owner, onDecide, onLater, onOpen }) {
  const dir = useRef(1)
  const top = rows[0]
  useEffect(() => { prefetch(rows.slice(1, 3).map(a => a.job_id)) }, [rows])
  const decide = kind => {
    if (!top) return
    dir.current = kind === 'skip' ? -1 : kind === 'later' ? 0 : 1
    if (kind === 'later') onLater(top.job_id)
    else onDecide(kind, top.job_id)
  }
  const total = done + rows.length
  const pct = total ? Math.round((done / total) * 100) : 0
  return (
    <div className="deck-wrap">
      <nav className="upnext" aria-label="Up next">
        <h3>Up next</h3>
        {rows.slice(1, 7).map(a => (
          <button key={a.job_id} className="upnext-row" onClick={() => onOpen(a.job_id)}>
            <span className={`rail-d ${scoreClass(fitOf(a))} ${a.fit_kind === 'quick' ? 'quick' : ''}`}>{fitOf(a) ?? '—'}</span><span>{a.title}</span>
          </button>))}
        {rows.length <= 1 && <p className="m">That is the last one.</p>}
      </nav>

      <main className="deck-stage">
        <div className="deck-head">
          <span data-deck-left>{rows.length ? `${rows.length} to go` : 'Inbox zero'}{done ? ` · ${done} decided` : ''}{secsLeft ? ` · about ${secsLeft} min left at your pace` : ''}</span>
        </div>
        <div className="deck-track" aria-hidden="true"><motion.i initial={false} animate={{ width: `${pct}%` }} transition={{ duration: 0.4 }} /></div>
        <div className="deck" aria-live="polite">
          {rows[2] && <div className="deck-card ghost g2" aria-hidden="true" />}
          {rows[1] && <div className="deck-behind" aria-hidden="true" key={rows[1].job_id}><Card a={rows[1]} top={false} dir={dir} onDecide={() => {}} onOpen={() => {}} /></div>}
          <AnimatePresence initial={false}>
            {top && <Card key={top.job_id} a={top} top dir={dir} onDecide={decide} onOpen={onOpen} />}
          </AnimatePresence>
          {!top && (
            <div className="deck-empty">
              <b>Inbox zero.</b>
              <p>Every job has been queued or skipped. New ones land here as a search finds them.</p>
              <Link className="btn primary" href="/">Run a search from Today</Link>
            </div>)}
        </div>
        {top && (
          <div className="deck-ctl" role="toolbar" aria-label="Decide">
            <button className="dk skip" onClick={() => decide('skip')} aria-label="Skip (left arrow or X)"><ArrowLeft className="lucide" /><small>Skip</small></button>
            <button className="dk later" onClick={() => decide('later')} aria-label="Later (down arrow or L)"><ArrowDown className="lucide" /><small>Later</small></button>
            <button className="dk queue" onClick={() => decide('queue')} disabled={!owner} title={owner ? '' : 'Only the owner account can queue applications.'}
                    aria-label="Queue (right arrow or Q)"><ArrowRight className="lucide" /><small>Queue</small></button>
            <button className="dk open" onClick={() => onOpen(top.job_id)} aria-label="Details (up arrow or Enter)"><ArrowUp className="lucide" /><small>Details</small></button>
          </div>)}
        {top && <p className="deck-keys" aria-hidden="true">← skip · → queue · ↓ later · ↑ details · Z undo · drag the card</p>}
      </main>

      <Rail apps={apps} onOpen={onOpen} />
    </div>
  )
}
