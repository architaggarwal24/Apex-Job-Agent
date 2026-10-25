// frontend/src/components/applications/ListView.jsx
'use client'
import { reasonLabel, scoreClass, stageLabel } from '@/lib/stages'

const COLS = [
  ['ai_score', 'Score'], ['title', 'Job'], ['source', 'Source'], ['stage', 'Stage'],
  ['days_in_stage', 'Days in stage'], ['found_at', 'Found'], ['applied_at', 'Applied'],
]

export default function ListView({ rows, sort, setSort, cursor, openId, onOpen }) {
  const by = key => setSort(s => ({ key, dir: s.key === key ? -s.dir : (key === 'ai_score' || key === 'days_in_stage' ? -1 : 1) }))
  if (!rows.length) return <div className="card empty">No applications match. Run the pipeline from the dashboard to scrape some.</div>
  return (
    <div className="card" style={{ overflowX: 'auto' }}>
      <table aria-label="Applications">
        <thead><tr>{COLS.map(([k, l]) => (
          <th key={k} aria-sort={sort.key === k ? (sort.dir > 0 ? 'ascending' : 'descending') : 'none'}>
            <button className="th-sort" onClick={() => by(k)}>{l}{sort.key === k ? (sort.dir > 0 ? ' ▲' : ' ▼') : ''}</button>
          </th>))}</tr></thead>
        <tbody>{rows.map(a => (
          <tr key={a.job_id} id={`card-${a.job_id}`} className={`${cursor === a.job_id ? 'cursor' : ''} ${openId === a.job_id ? 'open' : ''}`} onClick={() => onOpen(a.job_id)}>
            <td><span className={`chip ${scoreClass(a.ai_score)}`}>{a.ai_score ?? '—'}</span></td>
            <td><div className="t">{a.title}</div><div className="m">{a.company}{a.location ? ` · ${a.location}` : ''}</div></td>
            <td className="m">{a.source}</td>
            <td><span className="chip">{stageLabel(a.stage)}</span>
              {a.stage === 'needs_you' && a.blocked_reason && <div className="m">⚠ {reasonLabel(a.blocked_reason)}</div>}</td>
            <td className="m">{a.days_in_stage ?? '—'}{a.stale && <span className="badge b-wait" style={{ marginLeft: 6 }}>stale</span>}</td>
            <td className="m">{(a.found_at || '').slice(0, 10)}</td>
            <td className="m">{(a.applied_at || '').slice(0, 10)}</td>
          </tr>))}</tbody>
      </table>
    </div>
  )
}
