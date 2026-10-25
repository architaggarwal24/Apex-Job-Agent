// frontend/src/components/settings/AutonomySection.jsx
//
// Autonomy: the kill switch (PUT /api/automation/autonomy) and the rules editor
// (GET/POST /api/automation/rules, PUT/DELETE /api/automation/rules/{id}). Each rule shows its
// ledger stats and a preview of which queued jobs it would auto-submit right now
// (GET /api/automation/rules/{id}/preview - read-only). Writes are owner-only.
'use client'
import { useCallback, useEffect, useState } from 'react'
import { api, send } from '@/lib/api'
import { Msg, Section, Toggle } from './AutomationParts'

const NEW_RULE = { site_type: '*', min_score: 70, max_per_day: 5, min_attempts: 20, min_success_rate: 0.9, require_all_answers_known: true }
const pct = r => (r === null || r === undefined ? '—' : `${Math.round(r * 100)}%`)

// The editable fields of a rule, as numbers/booleans the API accepts.
function clean(r) {
  return { site_type: r.site_type, min_score: Number(r.min_score), max_per_day: Number(r.max_per_day),
           min_attempts: Number(r.min_attempts), min_success_rate: Number(r.min_success_rate),
           require_all_answers_known: !!r.require_all_answers_known }
}

function RuleFields({ r, set, siteTypes, idp }) {
  return (
    <div className="rule-grid">
      <div className="fg"><label htmlFor={`${idp}-st`}>Site type</label>
        <select id={`${idp}-st`} className="in" value={r.site_type} onChange={e => set('site_type', e.target.value)}>
          <option value="*">* (every site type)</option>{siteTypes.map(s => <option key={s}>{s}</option>)}</select></div>
      <div className="fg"><label htmlFor={`${idp}-ms`}>Min score</label>
        <input id={`${idp}-ms`} className="in" type="number" min="0" max="100" value={r.min_score} onChange={e => set('min_score', e.target.value)} /></div>
      <div className="fg"><label htmlFor={`${idp}-mpd`}>Max per day</label>
        <input id={`${idp}-mpd`} className="in" type="number" min="1" value={r.max_per_day} onChange={e => set('max_per_day', e.target.value)} /></div>
      <div className="fg"><label htmlFor={`${idp}-ma`}>Min reviewed attempts</label>
        <input id={`${idp}-ma`} className="in" type="number" min="1" value={r.min_attempts} onChange={e => set('min_attempts', e.target.value)} /></div>
      <div className="fg"><label htmlFor={`${idp}-sr`}>Min success rate (0–1)</label>
        <input id={`${idp}-sr`} className="in" type="number" min="0.01" max="1" step="0.05" value={r.min_success_rate} onChange={e => set('min_success_rate', e.target.value)} /></div>
      <Toggle label="Every answer known" name={`${idp} every answer known`} hint="all from your answer bank or profile at 90%+"
              checked={r.require_all_answers_known} onChange={x => set('require_all_answers_known', x)} />
    </div>
  )
}

function Preview({ rule, version }) {
  const [p, setP] = useState(null)
  const [err, setErr] = useState('')
  useEffect(() => { api(`/api/automation/rules/${rule.id}/preview`).then(x => { setP(x); setErr('') }).catch(e => setErr(e.message)) }, [rule.id, version])
  if (err) return <div className="msg-err">{err}</div>
  if (!p) return <div className="m">Loading the preview…</div>
  const s = p.stats
  return (
    <div className="rule-preview">
      <div className="row m" data-rule-stats>
        {s ? <>Ledger for {rule.site_type}: {s.reviewed} reviewed · success {pct(s.success_rate)} over the last {s.window} · {s.auto_approved} auto-approved · {s.failures_total} failures</>
           : <>Applies to every site type — each job is judged on its own site type's ledger (table below).</>}
        <span>· {p.auto_approved_today} auto-approved today</span>
      </div>
      <div className="pn-h">Queued now: {p.would_submit} of {p.jobs.length} would auto-submit{p.dry_run ? ' (dry run: nothing is really sent)' : ''}</div>
      {p.jobs.length === 0 ? <div className="m">Nothing is queued.</div> : (
        <ul className="rule-jobs">{p.jobs.map(j => (
          <li key={j.job_id} className={j.would_submit ? 'yes' : 'no'} data-preview-job={j.job_id}>
            <b>{j.would_submit ? '✓' : '✕'}</b> {j.title} · {j.company} <span className="m">({j.site_type}, score {j.ai_score ?? '—'})</span>
            {j.would_submit ? (j.condition && <div className="m">{j.condition}</div>) : <div className="m">{j.reasons.join('; ')}</div>}
          </li>))}</ul>)}
    </div>
  )
}

function Rule({ rule, siteTypes, reload, version }) {
  const [r, setR] = useState(rule)
  const [m, setM] = useState(null)
  useEffect(() => { setR(rule) }, [rule])
  const dirty = JSON.stringify(clean(r)) !== JSON.stringify(clean(rule))
  const act = async (fn, ok) => { setM(null); try { await fn(); setM({ ok: true, text: ok }); reload() } catch (e) { setM({ ok: false, text: e.message }) } }
  return (
    <div className={`rule ${rule.enabled ? '' : 'off'}`} data-rule={rule.id}>
      <div className="row">
        <b>Rule #{rule.id}</b>
        <span className={`badge ${rule.enabled ? 'b-done' : 'b-wait'}`}>{rule.enabled ? 'enabled' : 'disabled'}</span>
        {!rule.enabled && rule.disabled_reason && <span className="m">— {rule.disabled_reason}</span>}
        <span className="spacer" />
        <button className="btn sm" onClick={() => act(() => send(`/api/automation/rules/${rule.id}`, 'PUT', { enabled: !rule.enabled }), rule.enabled ? 'Disabled' : 'Enabled')}>
          {rule.enabled ? 'Disable' : 'Enable'}</button>
        <button className="btn sm danger" onClick={() => { if (confirm(`Delete rule #${rule.id}?`)) act(() => send(`/api/automation/rules/${rule.id}`, 'DELETE'), 'Deleted') }}>Delete</button>
      </div>
      <RuleFields r={r} set={(k, v) => setR(x => ({ ...x, [k]: v }))} siteTypes={siteTypes} idp={`rule-${rule.id}`} />
      <div className="row auto-save"><button className="btn primary sm" disabled={!dirty} onClick={() => act(() => send(`/api/automation/rules/${rule.id}`, 'PUT', clean(r)), 'Saved')}>Save rule</button><Msg m={m} /></div>
      <Preview rule={rule} version={version} />
    </div>
  )
}

export default function AutonomySection({ owner, siteTypes, onChanged }) {
  const [data, setData] = useState(null)
  const [draft, setDraft] = useState(NEW_RULE)
  const [m, setM] = useState(null)
  const [version, setVersion] = useState(0)
  const load = useCallback(() => api('/api/automation/rules').then(d => { setData(d); setVersion(v => v + 1) }).catch(e => setM({ ok: false, text: e.message })), [])
  useEffect(() => { load() }, [load])
  const reload = () => { load(); onChanged?.() }
  const kill = async on => {
    setM(null)
    try { await send('/api/automation/autonomy', 'PUT', { autonomy_enabled: on }); setM({ ok: true, text: on ? 'Autonomy is ON' : 'Autonomy is OFF' }); reload() }
    catch (e) { setM({ ok: false, text: e.message }) }
  }
  const add = async () => {
    setM(null)
    try { await send('/api/automation/rules', 'POST', clean(draft)); setDraft(NEW_RULE); setM({ ok: true, text: 'Rule added' }); reload() }
    catch (e) { setM({ ok: false, text: e.message }) }
  }
  return (
    <Section id="autonomy" title="Autonomy" owner={owner}
             hint="Let the agent approve and submit an application itself — only where a rule allows it and the ledger shows it has earned it. Off until you turn it on.">
      {!data ? <div className="m">Loading…</div> : <>
        <Toggle label="Autonomy (kill switch)" name="autonomy kill switch" hint={data.autonomy_enabled ? 'ON - rules may auto-submit' : 'OFF - everything waits for you'}
                checked={data.autonomy_enabled} onChange={kill} />
        <Msg m={m} />
        {data.rules.length === 0 && <div className="m" style={{ margin: '.5rem 0' }}>No rules: everything is reviewed by you.</div>}
        {data.rules.map(rule => <Rule key={rule.id} rule={rule} siteTypes={siteTypes} reload={reload} version={version} />)}
        <div className="rule new-rule" data-new-rule>
          <b>New rule</b>
          <RuleFields r={draft} set={(k, v) => setDraft(x => ({ ...x, [k]: v }))} siteTypes={siteTypes} idp="new-rule" />
          <div className="row auto-save"><button className="btn" onClick={add}>Add rule</button></div>
        </div>
      </>}
    </Section>
  )
}
