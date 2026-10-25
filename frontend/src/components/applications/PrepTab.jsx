// frontend/src/components/applications/PrepTab.jsx
//
// Prep artifacts (GET /api/applications/{id}/prep; generate/regenerate with POST
// .../prep/{kind}/regenerate, owner-only) with a Copy button each, and the offers you
// entered (/api/offers) whose comparison table the offer prep uses.
'use client'
import { useCallback, useEffect, useState } from 'react'
import { api, send } from '@/lib/api'
import { copyText } from '@/lib/live'

const KINDS = [['technical', 'Technical round'], ['hiring_manager', 'Hiring manager'], ['offer', 'Offer']]
const COMPONENTS = [['base', 'Base'], ['variable', 'Variable'], ['joining_bonus', 'Joining bonus'],
  ['equity_per_year', 'Equity / year'], ['retention_bonus', 'Retention bonus'], ['other', 'Other']]
const busyStates = ['pending', 'generating']

const List = ({ title, items }) => items?.length ? <><div className="pn-h">{title}</div><ul className="pn-list">{items.map((x, i) => <li key={i}>{x}</li>)}</ul></> : null

export function prepText(kind, c, comparison) {
  if (!c) return ''
  const sec = (t, xs) => (xs?.length ? `${t}\n${xs.map((x, i) => `${i + 1}. ${x}`).join('\n')}\n\n` : '')
  if (kind === 'technical') return sec('Topics', c.topics) + sec('Practice questions', c.questions) + sec('Gaps vs your profile', c.gaps) + (c.gaps_note ? `${c.gaps_note}\n` : '')
  if (kind === 'hiring_manager') return `Role summary\n${c.role_summary}\n\n` + sec('Questions to ask', c.questions_to_ask)
    + sec('STAR stories', (c.star_stories || []).map(s => `${s.title}: S ${s.situation} / T ${s.task} / A ${s.action} / R ${s.result}`))
    + sec('Missing', c.missing) + (c.star_note ? `${c.star_note}\n` : '') + (c.sources_note || '')
  const rows = (comparison?.rows || []).map(r => `${r.company} (${r.currency}): first year ${r.first_year_total}, recurring ${r.recurring}`)
  return sec('Offers', rows) + sec('Talking points', c.talking_points) + (c.note || '')
}

function Content({ kind, c, comparison }) {
  if (!c) return null
  if (kind === 'technical') return <>
    <List title="Topics" items={c.topics} /><List title="Practice questions" items={c.questions} />
    <List title="Gaps vs your profile" items={c.gaps} />{c.gaps_note && <div className="m">{c.gaps_note}</div>}
    <List title="Missing" items={c.missing} /></>
  if (kind === 'hiring_manager') return <>
    {c.role_summary && <><div className="pn-h">Role summary</div><div className="pn-text">{c.role_summary}</div></>}
    <List title="Questions to ask" items={c.questions_to_ask} />
    {c.star_stories?.length > 0 && <><div className="pn-h">STAR stories</div>{c.star_stories.map((s, i) =>
      <div key={i} className="pn-note"><b>{s.title}</b><div className="m">S: {s.situation}</div><div className="m">T: {s.task}</div>
        <div className="m">A: {s.action}</div><div className="m">R: {s.result}</div></div>)}</>}
    {c.star_note && <div className="m">{c.star_note}</div>}
    <List title="Missing" items={c.missing} />{c.sources_note && <div className="hint">{c.sources_note}</div>}</>
  return <><List title="Talking points" items={c.talking_points} />{c.note && <div className="m">{c.note}</div>}</>
}

function Offers({ comparison, reload, flash, jobId }) {
  const blank = { company: '', currency: 'INR', ...Object.fromEntries(COMPONENTS.map(([k]) => [k, ''])) }
  const [d, setD] = useState(blank)
  const add = async () => {
    const body = { company: d.company, currency: d.currency, job_id: jobId }
    COMPONENTS.forEach(([k]) => { body[k] = d[k] === '' ? 0 : Number(d[k]) })
    try { await send('/api/offers', 'POST', body); setD(blank); flash(true, 'Offer added'); reload() } catch (e) { flash(false, e.message) }
  }
  const del = async id => { try { await send(`/api/offers/${id}`, 'DELETE'); reload() } catch (e) { flash(false, e.message) } }
  const rows = comparison?.rows || []
  return (
    <div className="pn-sec">
      <div className="pn-h">Your offers (annual)</div>
      {rows.length > 0 && <div style={{ overflowX: 'auto' }}><table className="pn-table">
        <thead><tr><th>Company</th>{COMPONENTS.map(([k, l]) => <th key={k}>{l}</th>)}<th>Recurring</th><th>First year</th><th>vs best</th><th /></tr></thead>
        <tbody>{rows.map(r => <tr key={r.offer_id} style={{ cursor: 'default' }}>
          <td>{r.company} <span className="m">{r.currency}</span></td>{COMPONENTS.map(([k]) => <td key={k}>{r[k]}</td>)}
          <td>{r.recurring}</td><td><b>{r.first_year_total}</b></td><td>{r.first_year_vs_best ?? '—'}</td>
          <td><button className="link-btn danger" onClick={() => del(r.offer_id)}>Delete</button></td></tr>)}</tbody>
      </table>{comparison.note && <div className="hint">{comparison.note}</div>}</div>}
      <div className="grid2" style={{ marginTop: '.5rem' }}>
        <input className="in" placeholder="Company" value={d.company} onChange={e => setD({ ...d, company: e.target.value })} />
        <input className="in" placeholder="Currency (INR)" value={d.currency} onChange={e => setD({ ...d, currency: e.target.value })} />
        {COMPONENTS.map(([k, l]) => <input key={k} className="in" type="number" min="0" placeholder={l} value={d[k]} onChange={e => setD({ ...d, [k]: e.target.value })} />)}
      </div>
      <div className="row" style={{ marginTop: '.5rem' }}><button className="btn" disabled={!d.company.trim()} onClick={add}>Add offer</button></div>
    </div>
  )
}

export default function PrepTab({ app, flash }) {
  const [data, setData] = useState(null)
  const load = useCallback(() => api(`/api/applications/${app.job_id}/prep`).then(setData).catch(e => flash(false, e.message)), [app.job_id])
  useEffect(() => { load() }, [load, app.stage])
  const waiting = data && Object.values(data.artifacts).some(a => busyStates.includes(a.status))
  useEffect(() => { if (!waiting) return undefined; const t = setInterval(load, 3000); return () => clearInterval(t) }, [waiting, load])

  const regen = async kind => {
    try { await send(`/api/applications/${app.job_id}/prep/${kind}/regenerate`, 'POST'); load() }
    catch (e) { flash(false, e.status === 403 ? 'Generating prep uses the owner\'s LLM and is only available on the owner account.' : e.message) }
  }
  if (!data) return <div className="m">Loading prep…</div>
  return KINDS.map(([kind, label]) => {
    const a = data.artifacts[kind]
    const busy = busyStates.includes(a.status)
    return (
      <div key={kind} className="pn-sec">
        <div className="row">
          <b>{label}</b>
          <span className={`badge ${a.status === 'ready' ? 'b-done' : a.status === 'failed' ? 'b-fail' : busy ? 'b-fill' : 'b-idle'}`}>
            {a.status === 'not_generated' ? 'not generated' : a.status}</span>
          {a.status === 'failed' && <span className="m">({a.error})</span>}
          <span className="spacer" />
          {a.content && <button className="btn sm" onClick={async () => { const ok = await copyText(prepText(kind, a.content, a.comparison)); flash(ok, ok ? 'Copied' : 'Could not copy') }}>Copy</button>}
          <button className="btn sm" disabled={busy} onClick={() => regen(kind)}>{a.status === 'not_generated' ? 'Generate' : 'Regenerate'}</button>
        </div>
        {busy && <div className="m">Writing… this can take a minute.</div>}
        <Content kind={kind} c={a.content} comparison={a.comparison} />
        {kind === 'offer' && <Offers comparison={a.comparison} reload={load} flash={flash} jobId={app.job_id} />}
      </div>)
  })
}
