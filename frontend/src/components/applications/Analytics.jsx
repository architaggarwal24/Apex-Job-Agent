// frontend/src/components/applications/Analytics.jsx
//
// The old Pipeline page's charts: conversion funnel, applications by source and the
// AI score distribution (GET /api/tracking/analytics).
'use client'
import { useEffect, useState } from 'react'
import { api } from '@/lib/api'

function Bars({ rows, label, value }) {
  const max = Math.max(1, ...rows.map(value))
  if (!rows.length) return <div className="m">No data yet</div>
  return rows.map((r, i) => (
    <div className="an-row" key={i}>
      <span className="an-lbl">{label(r)}</span>
      <span className="an-bar"><i style={{ width: `${Math.round(value(r) / max * 100)}%` }} /></span>
      <span className="an-n">{value(r)}</span>
    </div>))
}

export default function Analytics({ version }) {
  const [d, setD] = useState(null)
  const [open, setOpen] = useState(true)
  useEffect(() => { if (open) api('/api/tracking/analytics').then(setD).catch(() => setD(null)) }, [open, version])
  return (
    <details className="card section an" open={open} onToggle={e => setOpen(e.currentTarget.open)}>
      <summary className="t">Pipeline analytics</summary>
      {open && !d && <div className="m">Loading…</div>}
      {d && <div className="an-grid">
        <div><div className="an-title">Conversion funnel</div><Bars rows={d.funnel} label={r => r.stage} value={r => r.count} /></div>
        <div><div className="an-title">Applications by source</div><Bars rows={d.sources} label={r => r.source || '—'} value={r => r.n} /></div>
        <div><div className="an-title">AI score distribution</div><Bars rows={d.score_dist} label={r => r.range} value={r => r.count} /></div>
      </div>}
    </details>
  )
}
