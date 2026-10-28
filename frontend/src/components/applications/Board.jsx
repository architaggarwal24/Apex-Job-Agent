// frontend/src/components/applications/Board.jsx
//
// One column per lifecycle stage. Cards of a post-submission stage can be dragged to
// another post-submission column; whether the move is legal is the server's call - a
// refused drop shows its message (Workspace.move).
'use client'
import { useState } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { AlertTriangle } from 'lucide-react'
import { spring } from '@/lib/motion'
import { fitOf } from '@/lib/filters'
import { POST_SUBMISSION, STAGE_COLOR, applyHost, reasonLabel, scoreClass, stageLabel } from '@/lib/stages'

// The AI score as a small arc: the number stays readable, the arc shows at a glance.
export function Score({ n }) {
  const R = 15, C = 2 * Math.PI * R
  const pct = typeof n === 'number' ? Math.max(0, Math.min(100, n)) : 0
  return (
    <span className={`score ${scoreClass(n)}`} title="AI score" aria-label={`AI score ${n ?? 'not scored'}`}>
      <svg viewBox="0 0 36 36" aria-hidden="true">
        <circle className="trk" cx="18" cy="18" r={R} />
        <circle className="arc" cx="18" cy="18" r={R} strokeDasharray={C} strokeDashoffset={C * (1 - pct / 100)} />
      </svg>
      {n ?? '—'}
    </span>
  )
}

export function Card({ a, cursor, openId, onOpen, draggable }) {
  return (
    <motion.div id={`card-${a.job_id}`} className={`kb-card ${cursor === a.job_id ? 'cursor' : ''} ${openId === a.job_id ? 'open' : ''}`}
         layout="position" transition={spring} initial={{ opacity: 0, scale: 0.96 }} animate={{ opacity: 1, scale: 1 }} exit={{ opacity: 0, scale: 0.96, transition: { duration: 0.12 } }}
         draggable={draggable} onClick={() => onOpen(a.job_id)}
         onDragStart={e => { e.dataTransfer.setData('text/plain', a.job_id); e.dataTransfer.effectAllowed = 'move' }}
         role="button" tabIndex={0} onKeyDown={e => { if (e.key === 'Enter') onOpen(a.job_id) }}>
      <div className="t kb-title">{a.title}</div>
      <div className="m">{a.company}</div>
      <Score n={fitOf(a)} />
      <div className="kb-meta">
        {a.source && <span className="m">{a.source}</span>}
        {a.apply_url && a.apply_url !== a.url && <span className="m" title="Applies on the company's own form - no job-board account needed">→ {applyHost(a.apply_url)}</span>}
        {a.days_in_stage != null && <span className="m" title="Days in this stage">{a.days_in_stage}d</span>}
        {a.stale && <span className="badge b-wait" title="Waiting longer than this stage's limit">stale</span>}
      </div>
      {a.stage === 'needs_you' && a.blocked_reason && <div className="kb-reason"><AlertTriangle className="lucide" aria-hidden="true" />{reasonLabel(a.blocked_reason)}</div>}
    </motion.div>
  )
}

export default function Board({ columns, cursor, openId, onOpen, onMove }) {
  const [over, setOver] = useState('')
  const byId = id => columns.flatMap(c => c.cards).find(a => a.job_id === id)
  return (
    <div className="kb" aria-label="Board">
      {columns.map(({ stage, cards }) => {
        const post = POST_SUBMISSION.includes(stage)
        return (
          <section key={stage} className={`kb-col ${over === stage ? 'over' : ''} ${post ? 'post' : ''}`} data-stage={stage}
                   onDragOver={e => { e.preventDefault(); e.dataTransfer.dropEffect = post ? 'move' : 'none'; setOver(stage) }}
                   onDragLeave={() => setOver(o => (o === stage ? '' : o))}
                   onDrop={e => { e.preventDefault(); setOver(''); const a = byId(e.dataTransfer.getData('text/plain')); if (a) onMove(a, stage) }}>
            <header className="kb-head" style={{ '--stage': STAGE_COLOR[stage] }}>
              <span className="kb-name">{stageLabel(stage)}</span><span className="chip">{cards.length}</span>
            </header>
            <div className="kb-cards">
              <AnimatePresence initial={false}>
                {cards.map(a => <Card key={a.job_id} a={a} cursor={cursor} openId={openId} onOpen={onOpen}
                                      draggable={POST_SUBMISSION.includes(a.stage)} />)}
              </AnimatePresence>
              {!cards.length && <div className="kb-empty">No applications</div>}
            </div>
          </section>
        )
      })}
    </div>
  )
}
