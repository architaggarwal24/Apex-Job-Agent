// frontend/src/components/applications/Board.jsx
//
// One column per lifecycle stage. Cards of a post-submission stage can be dragged to
// another post-submission column; whether the move is legal is the server's call - a
// refused drop shows its message (Workspace.move).
'use client'
import { useState } from 'react'
import { POST_SUBMISSION, STAGE_COLOR, reasonLabel, scoreClass, stageLabel } from '@/lib/stages'

export function Card({ a, cursor, openId, onOpen, draggable }) {
  return (
    <div id={`card-${a.job_id}`} className={`kb-card ${cursor === a.job_id ? 'cursor' : ''} ${openId === a.job_id ? 'open' : ''}`}
         draggable={draggable} onClick={() => onOpen(a.job_id)}
         onDragStart={e => { e.dataTransfer.setData('text/plain', a.job_id); e.dataTransfer.effectAllowed = 'move' }}
         role="button" tabIndex={0} onKeyDown={e => { if (e.key === 'Enter') onOpen(a.job_id) }}>
      <div className="t kb-title">{a.title}</div>
      <div className="m">{a.company}</div>
      <div className="kb-meta">
        <span className={`chip ${scoreClass(a.ai_score)}`} title="AI score">{a.ai_score ?? '—'}</span>
        {a.source && <span className="m">{a.source}</span>}
        {a.days_in_stage != null && <span className="m" title="Days in this stage">{a.days_in_stage}d</span>}
        {a.stale && <span className="badge b-wait" title="Waiting longer than this stage's limit">stale</span>}
      </div>
      {a.stage === 'needs_you' && a.blocked_reason && <div className="kb-reason">⚠ {reasonLabel(a.blocked_reason)}</div>}
    </div>
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
            <header className="kb-head" style={{ borderTopColor: STAGE_COLOR[stage] }}>
              <span className="kb-name">{stageLabel(stage)}</span><span className="chip">{cards.length}</span>
            </header>
            <div className="kb-cards">
              {cards.length ? cards.map(a => <Card key={a.job_id} a={a} cursor={cursor} openId={openId} onOpen={onOpen}
                                                   draggable={POST_SUBMISSION.includes(a.stage)} />)
                : <div className="kb-empty">No applications</div>}
            </div>
          </section>
        )
      })}
    </div>
  )
}
