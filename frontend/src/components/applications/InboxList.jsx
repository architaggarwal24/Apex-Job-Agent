// frontend/src/components/applications/InboxList.jsx
//
// The workspace's list: one dense row per job (score, title, company · place, the
// company form it applies on, source, age). The list scrolls inside its pane, never the
// page, and renders in pages of PAGE rows so a few thousand jobs stay quick.
// Checkboxes select rows for the bulk bar; a click opens the job in the detail pane.
'use client'
import { useEffect, useState } from 'react'
import { applyHost, reasonLabel, scoreClass, stageLabel } from '@/lib/stages'
import { fitOf } from '@/lib/filters'

const PAGE = 150

// The fit score as a number and a five-segment meter. `kind` "quick" is the instant keyword
// match (outlined meter); "ai" the model's score; anything else is not scored yet.
export function Sc({ n, kind }) {
  const on = typeof n === 'number' ? Math.round(Math.max(0, Math.min(100, n)) / 20) : 0
  const what = kind === 'quick' ? 'Quick match from keywords' : n == null ? 'Not scored yet' : 'AI fit score'
  return (
    <span className={`sc ${scoreClass(n)} ${kind === 'quick' ? 'quick' : ''}`} title={what} aria-label={`${what}${n == null ? '' : ` ${n}`}`} data-fit-kind={kind || ''}>
      <span style={{ minWidth: '1.5em' }}>{n ?? '—'}</span>
      <span className="meter" aria-hidden="true">{[1, 2, 3, 4, 5].map(i => <i key={i} className={i <= on ? 'on' : ''} />)}</span>
    </span>
  )
}

// "2h", "3d", "5w" since a timestamp (or a YYYY-MM-DD date).
export function age(at, now = Date.now()) {
  if (!at) return ''
  const t = new Date(/^\d{4}-\d{2}-\d{2}$/.test(at) ? `${at}T00:00:00` : at).getTime()
  if (Number.isNaN(t)) return ''
  const h = Math.max(0, (now - t) / 36e5)
  return h < 1 ? 'now' : h < 24 ? `${Math.floor(h)}h` : h < 24 * 14 ? `${Math.floor(h / 24)}d` : `${Math.floor(h / 168)}w`
}

const COLS = [['fit', 'Fit'], ['title', 'Job'], ['apply_url', 'Applies on', 'c-form'], ['source', 'Source', 'c-src'], ['found_at', 'Age']]

export default function InboxList({ rows, cursor, openId, selected, onSelect, onSelectAll, onOpen, sort, setSort, empty, fresh }) {
  const [limit, setLimit] = useState(PAGE)
  useEffect(() => { setLimit(PAGE) }, [rows.length === 0])
  useEffect(() => {                      // keep the cursor's row rendered when keys walk past the page
    const i = rows.findIndex(a => a.job_id === cursor)
    if (i >= limit) setLimit(Math.ceil((i + 1) / PAGE) * PAGE)
  }, [cursor, rows, limit])
  if (!rows.length) return <div className="il-empty">{empty}</div>
  const by = key => setSort(s => ({ key, dir: s.key === key ? -s.dir : (key === 'fit' || key === 'found_at' ? -1 : 1) }))
  const all = rows.length > 0 && rows.every(a => selected.has(a.job_id))
  return (
    <div role="grid" aria-label="Applications" aria-rowcount={rows.length}>
      <div className="il-head" role="row">
        <input type="checkbox" aria-label="Select all" checked={all} onChange={e => onSelectAll(e.target.checked)} />
        {COLS.map(([k, l, cls]) => (
          <button key={k} className={cls || ''} role="columnheader" aria-sort={sort.key === k ? (sort.dir > 0 ? 'ascending' : 'descending') : 'none'}
                  onClick={() => by(k)} style={k === 'found_at' ? { textAlign: 'right' } : undefined}>
            {l}{sort.key === k ? (sort.dir > 0 ? ' ↑' : ' ↓') : ''}</button>))}
      </div>
      {rows.slice(0, limit).map(a => {
        const form = a.apply_url && a.apply_url !== a.url ? applyHost(a.apply_url) : ''
        return (
          <div key={a.job_id} id={`card-${a.job_id}`} role="row" aria-selected={openId === a.job_id}
               className={`il-row ${cursor === a.job_id ? 'cursor' : ''} ${openId === a.job_id ? 'open' : ''} ${selected.has(a.job_id) ? 'sel' : ''} ${fresh?.has(a.job_id) ? 'fresh' : ''}`}
               onClick={() => onOpen(a.job_id)}>
            <input type="checkbox" aria-label={`Select ${a.title}`} checked={selected.has(a.job_id)}
                   onClick={e => e.stopPropagation()} onChange={e => onSelect(a.job_id, e.target.checked)} />
            <Sc n={fitOf(a)} kind={a.fit_kind} />
            <div className="il-main">
              <div className="il-t">{a.title}</div>
              <div className="il-c">{a.company}{a.location ? ` · ${a.location}` : ''}
                {a.stage === 'needs_you' && a.blocked_reason && <span className="why"> · ⚠ {reasonLabel(a.blocked_reason)}</span>}
                {!['found', 'needs_you'].includes(a.stage) && <span> · {stageLabel(a.stage)}</span>}
                {a.stale && <span className="why"> · stale</span>}</div>
            </div>
            <span className="il-form" title={form ? 'Applies on the company’s own form: no job-board account needed' : ''}>{form ? `→ ${form}` : ''}</span>
            <span className="il-src">{a.source}</span>
            <span className="il-age" title={a.posted_at ? `posted ${a.posted_at}` : `found ${String(a.found_at).slice(0, 10)}`}>{age(a.posted_at || a.found_at)}</span>
          </div>)
      })}
      {rows.length > limit && <div className="il-more"><button className="btn sm" onClick={() => setLimit(l => l + PAGE)}>Show {Math.min(PAGE, rows.length - limit)} more of {rows.length - limit}</button></div>}
    </div>
  )
}
