// frontend/src/pages/Jobs.jsx
import { useEffect, useMemo, useState } from 'react'
import { api } from '../api.js'

const STATUSES = [['', 'All'], ['found', 'Pending'], ['applied', 'Applied'], ['skipped', 'Skipped'], ['error', 'Errors']]
const chipClass = s => s == null ? '' : s >= 70 ? 'hi' : s >= 40 ? 'mid' : 'lo'

export default function Jobs() {
  const [jobs, setJobs] = useState(null)
  const [q, setQ] = useState('')
  const [title, setTitle] = useState('')
  const [status, setStatus] = useState('')
  const [err, setErr] = useState('')

  const load = () => api('/api/jobs?limit=2000').then(setJobs).catch(e => setErr(e.message))
  useEffect(() => { load() }, [])

  // Titles present right now — changes whenever a scrape adds jobs or Clear All empties the list.
  const titles = useMemo(() => [...new Set((jobs || []).map(j => j.title).filter(Boolean))].sort((a, b) => a.localeCompare(b)), [jobs])
  useEffect(() => { if (title && !titles.includes(title)) setTitle('') }, [titles, title])

  const rows = useMemo(() => (jobs || []).filter(j =>
    (!status || j.status === status) && (!title || j.title === title) &&
    (!q || `${j.title} ${j.company}`.toLowerCase().includes(q.toLowerCase()))), [jobs, q, title, status])

  const clearAll = async () => {
    if (!confirm('Clear all pending/skipped/error jobs? Jobs already Applied or In Progress are kept so your Pipeline board is unaffected. This cannot be undone.')) return
    await api('/api/jobs', { method: 'DELETE' }); load()
  }
  const purge = async () => { if (confirm('Remove duplicate jobs?')) { await api('/api/jobs/duplicates', { method: 'DELETE' }); load() } }

  return (
    <>
      <h1>Jobs</h1>
      <p className="sub">{jobs ? `${rows.length} of ${jobs.length} jobs` : 'Loading…'}</p>
      <div className="bar">
        <input className="in grow" placeholder="Search title or company…" value={q} onChange={e => setQ(e.target.value)} />
        <select className="in" value={title} onChange={e => setTitle(e.target.value)} style={{ maxWidth: 260 }}>
          <option value="">All titles (as scraped)</option>
          {titles.map(t => <option key={t}>{t}</option>)}
        </select>
        <div className="pills">{STATUSES.map(([v, l]) => <button key={v} className={`pill ${status === v ? 'on' : ''}`} onClick={() => setStatus(v)}>{l}</button>)}</div>
        <button className="btn" onClick={purge}>Purge dupes</button>
        <button className="btn danger" onClick={clearAll}>Clear all jobs</button>
      </div>
      <div className="card" style={{ overflow: 'hidden' }}>
        {err ? <div className="empty">{err}</div> : rows.length === 0 ? <div className="empty">{jobs ? 'No jobs match. Run the pipeline from the dashboard to scrape some.' : ''}</div> : (
          <table>
            <thead><tr><th>Score</th><th>Job</th><th>Source</th><th>Status</th><th>Found</th></tr></thead>
            <tbody>{rows.map(j => (
              <tr key={j.job_id} onClick={() => window.open(j.url, '_blank', 'noopener')}>
                <td><span className={`chip ${chipClass(j.ai_score)}`}>{j.ai_score ?? '—'}</span></td>
                <td><div className="t">{j.title}</div><div className="m">{j.company}{j.location ? ` · ${j.location}` : ''}</div></td>
                <td className="m">{j.source}</td>
                <td><span className="chip">{j.status}</span></td>
                <td className="m">{(j.found_at || '').slice(0, 10)}</td>
              </tr>))}</tbody>
          </table>)}
      </div>
    </>
  )
}
