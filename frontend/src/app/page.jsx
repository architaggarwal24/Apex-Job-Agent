// frontend/src/app/page.jsx
'use client'
import { useEffect, useState } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { api } from '@/lib/api'
import { backendUrl } from '@/lib/backend'


const FUNNEL = [['applied', 'Applied'], ['recruiter_screen', 'Recruiter'],
  ['technical_round', 'Technical'], ['hiring_manager', 'HM Round'], ['offer', 'Offer']]
const badgeClass = s => ({ done: 'b-done', awaiting_input: 'b-wait', screenshot_review: 'b-rev',
  filling: 'b-fill', refilling: 'b-fill', failed: 'b-fail' }[s] || 'b-idle')

export default function Dashboard() {
  const router = useRouter()
  const [stats, setStats] = useState(null)
  const [status, setStatus] = useState(null)
  const [sessions, setSessions] = useState([])
  const [running, setRunning] = useState(false)
  const [pipeMsg, setPipeMsg] = useState('')

  const loadStats = () => { api('/api/stats').then(setStats).catch(() => {}); api('/api/status').then(st => {
    setStatus(st)
    if (st.profile_complete === false) window.location.href = backendUrl('/setup')
  }).catch(() => {}) }
  const loadSessions = () => api('/api/apply/sessions').then(setSessions).catch(() => {})

  useEffect(() => {
    loadStats(); loadSessions()
    const a = setInterval(loadStats, 12000), b = setInterval(loadSessions, 15000)
    return () => { clearInterval(a); clearInterval(b) }
  }, [])

  const runPipeline = async (quick = false) => {
    setRunning(true); setPipeMsg('Running…')
    try {
      await api('/api/pipeline/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ quick }) })
      setPipeMsg('✓ Pipeline running in background — check logs for progress')
    } catch (e) { setPipeMsg(e.status === 403 ? 'Only the owner account can run the pipeline' : '✗ Failed to start') }
    setTimeout(() => { setRunning(false); loadStats() }, 3000)
  }
  const cancelPipeline = async () => {
    setPipeMsg('Cancelling…')
    try { await api('/api/pipeline/cancel', { method: 'POST' }); setPipeMsg('⏹ Cancel requested — finishing current step then stopping') }
    catch { setPipeMsg('✗ Cancel failed') }
  }

  const awaiting = stats?.sessions?.awaiting_input ?? 0

  return (
    <>
      <h1>Dashboard</h1>
      <p className="sub">
        Autonomous job application agent
        {status?.llm && <span className="chip" style={{ marginLeft: 10 }}>{status.llm.label || status.llm.provider} / {status.llm.model}</span>}
        {status?.dry_run && <span className="chip mid" style={{ marginLeft: 6 }}>Dry run</span>}
      </p>

      {awaiting > 0 && (
        <div className="alert">
          ⚠️ <b>{awaiting}</b> application{awaiting > 1 ? 's' : ''} waiting on a question only you can answer —
          <Link href="/applications?needs_you=1" style={{ color: 'var(--acc2)', marginLeft: 4 }}>answer in Applications →</Link>
        </div>
      )}

      <div className="stats">
        <div className="card stat"><div className="l">Total Jobs</div><div className="n">{stats?.total ?? '—'}</div></div>
        <div className="card stat"><div className="l">Applied</div><div className="n" style={{ color: 'var(--grn)' }}>{stats?.applied ?? '—'}</div></div>
        <div className="card stat"><div className="l">Avg AI Score</div><div className="n">{stats?.avg_score ? `${stats.avg_score}/100` : '—'}</div></div>
        <div className="card stat"><div className="l">Awaiting Input</div><div className="n" style={{ color: 'var(--ylw)' }}>{awaiting}</div></div>
        <div className="card stat"><div className="l">Pending Review</div><div className="n">{stats?.sessions?.screenshot_review ?? 0}</div></div>
        <div className="card stat"><div className="l">Errors</div><div className="n" style={{ color: 'var(--red)' }}>{stats?.error ?? 0}</div></div>
      </div>

      <div className="actions">
        <Link className="card action" href="/applications" style={{ textDecoration: 'none' }}>
          <div className="ic">🔍</div><div><div className="t">Search Jobs</div><div className="d">Scrape LinkedIn, Indeed & Naukri, score with AI</div></div>
        </Link>
        <Link className="card action" href="/applications?stage=found,queued" style={{ textDecoration: 'none' }}>
          <div className="ic">📄</div><div><div className="t">Apply to Jobs</div><div className="d">Universal form filler — any URL</div></div>
        </Link>
        <Link className="card action" href="/applications?stage=submitted,recruiter_screen,technical_round,hiring_manager,offer,rejected,withdrawn" style={{ textDecoration: 'none' }}>
          <div className="ic">📊</div><div><div className="t">Pipeline Board</div><div className="d">Track application stages</div></div>
        </Link>
        <Link className="card action" href="/profile" style={{ textDecoration: 'none' }}>
          <div className="ic">👤</div><div><div className="t">Edit Profile</div><div className="d">Update data used in form filling</div></div>
        </Link>
      </div>

      <div className="card" style={{ marginBottom: 20, padding: 18 }}>
        <div className="row">
          <button className="btn primary" disabled={running} onClick={() => runPipeline(false)}>
            {running ? '…' : 'Run Pipeline'}
          </button>
          <button className="btn" onClick={() => runPipeline(true)} disabled={running}>Quick Run</button>
          {running && <button className="btn danger" onClick={cancelPipeline}>Cancel</button>}
          <span className="m">{pipeMsg}</span>
        </div>
      </div>

      <div className="card" style={{ marginBottom: 20, overflow: 'hidden' }}>
        <div className="bar" style={{ padding: '18px 22px 0' }}><b>Application Pipeline</b></div>
        <div className="funnel">
          {FUNNEL.map(([key, label]) => (
            <div key={key}><div className="n">{stats?.pipeline?.[key] ?? 0}</div><div className="l">{label}</div></div>
          ))}
        </div>
      </div>

      <div className="card" style={{ overflow: 'hidden' }}>
        <div className="bar" style={{ padding: '18px 22px 0' }}><b>Recent Applications</b></div>
        {sessions.length === 0 ? <div className="empty">No applications yet</div> : (
          <table>
            <thead><tr><th>Job</th><th>Company</th><th>AI Score</th><th>Status</th><th>Updated</th></tr></thead>
            <tbody>{sessions.slice(0, 10).map(s => (
              <tr key={s.job_id} onClick={() => router.push(`/applications?job=${encodeURIComponent(s.job_id)}`)}>
                <td className="t">{s.job_title}</td>
                <td className="m">{s.company}</td>
                <td className="m">—</td>
                <td><span className={`badge ${badgeClass(s.state)}`}>{s.state.replace(/_/g, ' ')}</span></td>
                <td className="m">{(s.updated_at || '').slice(0, 16).replace('T', ' ')}</td>
              </tr>))}</tbody>
          </table>
        )}
      </div>
    </>
  )
}
