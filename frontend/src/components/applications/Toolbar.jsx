// frontend/src/components/applications/Toolbar.jsx
'use client'
import { POST_SUBMISSION } from '@/lib/stages'

// Quick stage filters: the old Jobs page's status pills and the old Apply page's queue filters.
const PILLS = [
  ['', 'All'], ['found,queued', 'Pending'], ['preparing', 'Preparing'], ['needs_you', 'Needs you'],
  ['ready_for_review', 'Review'], [POST_SUBMISSION.join(','), 'Applied'], ['skipped', 'Skipped'], ['failed', 'Errors'],
]
const VIEWS = [['board', 'Board'], ['list', 'List'], ['review', 'Review queue']]

export default function Toolbar({ f, setF, titles, sources, onPurge, onClearAll, reviewCount }) {
  const set = (k, v) => setF(x => ({ ...x, [k]: v }))
  const stageKey = f.stages.join(',')
  const score = (k, v) => set(k, v === '' ? '' : String(Math.max(0, Math.min(100, Math.round(Number(v)) || 0))))
  return (
    <div className="ws-toolbar">
      <div className="bar">
        <input className="in grow" placeholder="Search title, company or location…" value={f.q} onChange={e => set('q', e.target.value)} aria-label="Search" />
        <select className="in" value={f.title} onChange={e => set('title', e.target.value)} style={{ maxWidth: 240 }} aria-label="Title">
          <option value="">All titles (as scraped)</option>
          {titles.map(t => <option key={t}>{t}</option>)}
        </select>
        <select className="in" value={f.source} onChange={e => set('source', e.target.value)} aria-label="Source">
          <option value="">All sources</option>
          {sources.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <span className="row score-range" title="Score range">
          <input className="in" type="number" min="0" max="100" placeholder="Score ≥" value={f.min} onChange={e => score('min', e.target.value)} aria-label="Minimum score" />
          <span className="m">–</span>
          <input className="in" type="number" min="0" max="100" placeholder="≤" value={f.max} onChange={e => score('max', e.target.value)} aria-label="Maximum score" />
        </span>
      </div>
      <div className="bar">
        <div className="pills" role="radiogroup" aria-label="Stage">
          {PILLS.map(([v, l]) => <button key={l} role="radio" aria-checked={stageKey === v} className={`pill ${stageKey === v ? 'on' : ''}`}
                                         onClick={() => set('stages', v ? v.split(',') : [])}>{l}</button>)}
        </div>
        <div className="pills" role="radiogroup" aria-label="View">
          {VIEWS.map(([v, l]) => <button key={v} role="radio" aria-checked={f.view === v} className={`pill ${f.view === v ? 'on' : ''}`}
                                         onClick={() => set('view', v)}>{l}{v === 'review' && reviewCount ? ` (${reviewCount})` : ''}</button>)}
        </div>
        <span className="spacer" />
        <button className="btn" onClick={onPurge}>Purge dupes</button>
        <button className="btn danger" onClick={onClearAll}>Clear all jobs</button>
      </div>
    </div>
  )
}
