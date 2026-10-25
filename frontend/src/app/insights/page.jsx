// frontend/src/app/insights/page.jsx
//
// Insights: how your applications turned out (GET /api/analytics/outcomes - db/outcomes_db.py).
//   * reply / interview / offer rates by source, title, score band and resume variant, as
//     plain CSS bars (no chart library). A group below the sample-size threshold shows
//     "not enough data" - never a number, never a bar;
//   * the calibration: each score band's reply rate against the overall rate, and the
//     recommended auto_skip_score_threshold with its reason. "Apply this threshold" saves it
//     (POST /api/settings - owner only; disabled for other accounts).
'use client'
import { useCallback, useEffect, useState } from 'react'
import { api, send } from '@/lib/api'
import { DIMENSIONS, RATES, medianView, rateView } from '@/lib/insights'
import { OWNER_ONLY, useMe } from '@/lib/me'

function Rate({ value, label }) {
  const r = rateView(value)
  return (
    <div className="ib-rate">
      <span className="ib-lbl">{label}</span>
      {r.pct === null ? <span className="ib-nodata">{r.text}</span>
        : <><span className="ib-bar"><i style={{ width: `${r.pct}%` }} /></span><span className="ib-n">{r.text}</span></>}
    </div>
  )
}

function Group({ g, min }) {
  const small = g.outcomes < min
  return (
    <div className={`ib-group ${small ? 'small' : ''}`} data-group={g.key}>
      <div className="row">
        <b className="ib-key">{g.key}</b><span className="m">{g.outcomes} submitted · {g.replies} replies · {g.interviews} interviews · {g.offers} offers</span>
      </div>
      {small ? <div className="ib-warn">⚠ Not enough data — {g.outcomes} of the {min} applications needed before rates are shown.</div>
        : <>{RATES.map(([k, l]) => <Rate key={k} label={l} value={g[k]} />)}
            <div className="m">Median time to first response: {medianView(g.median_days_to_first_response)}</div></>}
    </div>
  )
}

function Calibration({ cal, owner, onApplied, flash }) {
  const [busy, setBusy] = useState(false)
  if (cal.status !== 'ok') return (
    <div className="ib-warn" data-calibration="not-enough">⚠ {cal.reason}</div>
  )
  const rec = cal.recommended_auto_skip_score_threshold
  const apply = async () => {
    if (!confirm(`Set the auto-skip score threshold to ${rec}? Jobs scoring below it will be skipped.`)) return
    setBusy(true)
    try { await send('/api/settings', 'POST', { auto_skip_score_threshold: String(rec) }); flash(true, `Auto-skip threshold set to ${rec}`); onApplied() }
    catch (e) { flash(false, e.message) }
    setBusy(false)
  }
  return (
    <div data-calibration="ok">
      <p className="ib-reason">{cal.reason}</p>
      <div className="row" style={{ marginBottom: 'var(--sp2)' }}>
        <span className="m">Current threshold</span><b data-current-threshold>{cal.current_threshold}</b>
        <span className="m">Recommended</span><b data-recommended>{rec}</b>
        <span className="spacer" />
        <button className="btn primary" disabled={!owner || busy || rec === cal.current_threshold}
                title={!owner ? OWNER_ONLY : rec === cal.current_threshold ? 'Already in use' : ''} onClick={apply}>Apply this threshold</button>
      </div>
      {!owner && <div className="hint">{OWNER_ONLY}</div>}
      <div className="m" style={{ marginBottom: '.5rem' }}>Reply rate by score band (overall {cal.overall_reply_rate}% — the line)</div>
      {cal.bands.map(b => {
        const r = rateView(b.reply_rate)
        return (
          <div key={b.band} className="ib-rate" data-band={b.band}>
            <span className="ib-lbl">{b.band}</span>
            {r.pct === null ? <span className="ib-nodata">not enough data ({b.outcomes})</span> : <>
              <span className={`ib-bar ${b.underperforms ? 'bad' : ''}`}><i style={{ width: `${r.pct}%` }} />
                <em style={{ left: `${Math.min(100, cal.overall_reply_rate)}%` }} title={`overall ${cal.overall_reply_rate}%`} /></span>
              <span className="ib-n">{r.text}</span></>}
          </div>)
      })}
    </div>
  )
}

export default function Insights() {
  const me = useMe()
  const [d, setD] = useState(null)
  const [err, setErr] = useState('')
  const [msg, setMsg] = useState(null)
  const flash = (ok, text) => setMsg({ ok, text })
  const load = useCallback(() => api('/api/analytics/outcomes').then(x => { setD(x); setErr('') }).catch(e => setErr(e.message)), [])
  useEffect(() => { load() }, [load])
  useEffect(() => { if (!msg) return undefined; const t = setTimeout(() => setMsg(null), 6000); return () => clearTimeout(t) }, [msg])

  return (
    <>
      <h1>Insights</h1>
      <p className="sub">{d ? `${d.outcomes} submitted application${d.outcomes === 1 ? '' : 's'} so far. Rates appear once a group has ${d.min_group}.` : 'Loading…'}</p>
      {msg && <div className={`toast ${msg.ok ? 'ok' : 'err'}`} role="status">{msg.text}</div>}
      {err && <div className="card empty">{err}</div>}
      {d && <>
        <div className="card section">
          <h2 style={{ padding: 0, marginBottom: 'var(--sp2)' }}>Auto-skip calibration</h2>
          <Calibration cal={d.calibration} owner={!!me?.owner} onApplied={load} flash={flash} />
        </div>
        <div className="card section"><h2 style={{ padding: 0, marginBottom: 'var(--sp2)' }}>Overall</h2><Group g={d.overall} min={d.min_group} /></div>
        <div className="ib-grid">
          {DIMENSIONS.map(([k, label]) => (
            <div key={k} className="card section" data-dimension={k}>
              <h2 style={{ padding: 0, marginBottom: 'var(--sp2)' }}>By {label.toLowerCase()}</h2>
              {d.by[k].length ? d.by[k].map(g => <Group key={g.key} g={g} min={d.min_group} />) : <div className="m">No submitted applications yet.</div>}
            </div>))}
        </div>
      </>}
    </>
  )
}
