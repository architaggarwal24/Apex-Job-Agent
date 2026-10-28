// frontend/src/app/insights/page.jsx
//
// Insights: how your applications turned out (GET /api/analytics/outcomes - db/outcomes_db.py).
//   * the overall numbers as one strip at the top;
//   * reply / interview / offer rates by source, title, score band or resume variant (one at a
//     time, picked above the table), as plain CSS bars (no chart library). A group below the
//     sample-size threshold shows "not enough data" - never a number, never a bar;
//   * the calibration: each score band's reply rate against the overall rate, and the
//     recommended auto_skip_score_threshold with its reason. "Apply this threshold" saves it
//     (POST /api/settings - owner only; disabled for other accounts);
//   * the pipeline charts (funnel, sources, score distribution) below.
'use client'
import { useCallback, useEffect, useState } from 'react'
import { api, send } from '@/lib/api'
import { DIMENSIONS, RATES, medianView, rateView } from '@/lib/insights'
import { OWNER_ONLY, useMe } from '@/lib/me'
import { motion } from 'motion/react'
import { EASE } from '@/lib/motion'
import Analytics from '@/components/applications/Analytics'
import { SkeletonCard, SkeletonRows } from '@/components/layout/Skeleton'

const COUNT = { reply_rate: 'replies', interview_rate: 'interviews', offer_rate: 'offers' }
const Bar = ({ pct }) => <motion.i initial={{ scaleX: 0 }} animate={{ scaleX: 1 }} transition={{ duration: 0.6, ease: EASE }} style={{ width: `${pct}%` }} />

function Rate({ value, label, title }) {
  const r = rateView(value)
  return (
    <div className="ib-rate" role="cell" title={title}>
      <span className="ib-lbl">{label}</span>
      {r.pct === null ? <span className="ib-nodata">{r.text}</span>
        : <><span className="ib-bar"><Bar pct={r.pct} /></span><span className="ib-n">{r.text}</span></>}
    </div>
  )
}

// One outcome group as a row: counts, then each rate as a mini bar. A group below the
// sample-size threshold gets the warning across the rate columns - no number, no bar.
function Group({ g, min }) {
  const small = g.outcomes < min
  return (
    <div className={`ib-row ib-group ${small ? 'small' : ''}`} data-group={g.key} role="row">
      <b className="ib-key" role="cell">{g.key}</b>
      <span className="ib-sent num" role="cell">{g.outcomes}</span>
      {small ? <div className="ib-warn" role="cell">⚠ Not enough data — {g.outcomes} of the {min} applications needed before rates are shown.</div>
        : <>{RATES.map(([k, l]) => <Rate key={k} label={l} value={g[k]} title={`${g[COUNT[k]]} of ${g.outcomes} submitted`} />)}
            <span className="ib-med m" role="cell"><span className="ib-lbl">First response</span>{medianView(g.median_days_to_first_response)}</span></>}
    </div>
  )
}

// The overall numbers as a strip. Rates the server holds back stay "not enough data".
function Overall({ g, min }) {
  const small = g.outcomes < min
  const cell = (label, value, key) => (
    <div className="metric" data-overall={key}>
      <span className="l">{label}</span>
      <span className="n">{value}</span>
    </div>)
  return (
    <div className="metrics ib-overall" data-group={g.key}>
      {cell('Submitted', g.outcomes, 'outcomes')}
      {RATES.map(([k, l]) => { const r = rateView(g[k]); return cell(`${l} rate`, small || r.pct === null ? '—' : r.text, k) })}
      {cell('First response (median)', small ? '—' : medianView(g.median_days_to_first_response), 'median')}
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
              <span className={`ib-bar ${b.underperforms ? 'bad' : ''}`}><Bar pct={r.pct} />
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
  const [by, setBy] = useState(DIMENSIONS[0][0])
  const flash = (ok, text) => setMsg({ ok, text })
  const load = useCallback(() => api('/api/analytics/outcomes').then(x => { setD(x); setErr('') }).catch(e => setErr(e.message)), [])
  useEffect(() => { load() }, [load])
  useEffect(() => { if (!msg) return undefined; const t = setTimeout(() => setMsg(null), 6000); return () => clearTimeout(t) }, [msg])
  const label = (DIMENSIONS.find(([k]) => k === by) || [])[1] || ''

  return (
    <>
      <div className="page-head"><div>
        <h1>Insights</h1>
        <p className="sub">{d ? `${d.outcomes} submitted application${d.outcomes === 1 ? '' : 's'} so far. Rates appear once a group has ${d.min_group}.` : 'Loading…'}</p>
      </div></div>
      {msg && <div className={`toast ${msg.ok ? 'ok' : 'err'}`} role="status">{msg.text}</div>}
      {err && <div className="card empty">{err}</div>}
      {!d && !err && <><SkeletonRows n={1} label="Loading insights" /><SkeletonCard lines={5} label="Loading outcomes" /></>}
      {d && <>
        <Overall g={d.overall} min={d.min_group} />
        <div className="ib-cols">
          <section className="card section ib-break" data-dimension={by}>
            <div className="ib-break-head">
              <h2>Outcomes by {label.toLowerCase()}</h2>
              <div className="pills" role="tablist" aria-label="Group outcomes by">
                {DIMENSIONS.map(([k, l]) => (
                  <button key={k} role="tab" aria-selected={by === k} className={`pill ${by === k ? 'on' : ''}`} onClick={() => setBy(k)} data-by={k}>{l}</button>))}
              </div>
            </div>
            {d.by[by].length ? (
              <div className="ib-table" role="table" aria-label={`Outcomes by ${label.toLowerCase()}`}>
                <div className="ib-row ib-th" role="row">
                  <span role="columnheader">{label}</span><span role="columnheader">Sent</span>
                  {RATES.map(([k, l]) => <span key={k} role="columnheader">{l}</span>)}<span role="columnheader">First response</span>
                </div>
                {d.by[by].map(g => <Group key={g.key} g={g} min={d.min_group} />)}
              </div>)
              : <div className="m">No submitted applications yet.</div>}
          </section>
          <section className="card section ib-cal">
            <h2>Auto-skip calibration</h2>
            <Calibration cal={d.calibration} owner={!!me?.owner} onApplied={load} flash={flash} />
          </section>
        </div>
      </>}
      <Analytics version={0} />
    </>
  )
}
