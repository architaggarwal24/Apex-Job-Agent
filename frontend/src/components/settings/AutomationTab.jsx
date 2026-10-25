// frontend/src/components/settings/AutomationTab.jsx
//
// Settings > Automation: everything that decides what the agent does on its own, each part
// reading and writing its real endpoint and saved on its own (no shared save bar).
//
//   Quality & caps          quality_first, daily_apply_limit, auto_skip_score_threshold  POST /api/settings
//   Apply mode & executor   apply_mode, apply_mode_overrides (per site type), executor   POST /api/settings
//   Autonomy                kill switch, rules, per-rule ledger stats + queue preview    /api/automation/*
//   Reliability ledger      per site type                                              GET /api/automation/ledger
//   Answer bank             search / edit / delete / scope / policy                    /api/answer-bank
//   Resume variants         upload / default / delete                                  /api/resumes
//   Runner tokens           create (shown once) / revoke / online                       /api/settings/runner-tokens
//   Notifications           channels, events, quiet hours, Test                        /api/notifications/*
//   Blacklist & salary      company_blacklist, min_salary_lpa                          POST /api/settings
//
// Settings and automation are the OWNER's: other accounts see those parts read-only (a
// disabled fieldset), and the parts only the owner may even read say so. The answer bank and
// resume variants are each account's own data, so every account can manage its own.
'use client'
import { useEffect, useState } from 'react'
import { api, send } from '@/lib/api'
import { useMe } from '@/lib/me'
import AutonomySection from './AutonomySection'
import AnswerBankSection from './AnswerBankSection'
import ResumesSection from './ResumesSection'
import RunnerTokensSection from './RunnerTokensSection'
import NotificationsSection from './NotificationsSection'
import { Msg, Section, Toggle } from './AutomationParts'

// One group of plain settings, saved together through POST /api/settings (owner-only there too).
function useSettingsForm(settings, keys, reload) {
  const pick = () => Object.fromEntries(keys.map(k => [k, settings?.[k] ?? '']))
  const [v, setV] = useState(pick)
  const [m, setM] = useState(null)
  useEffect(() => { setV(pick()) }, [settings])
  const dirty = keys.filter(k => String(v[k]) !== String(settings?.[k] ?? ''))
  const save = async (extra = {}) => {
    setM(null)
    try {
      const r = await send('/api/settings', 'POST', { ...Object.fromEntries(dirty.map(k => [k, v[k]])), ...extra })
      setM({ ok: true, text: `Saved ${r.saved} setting${r.saved === 1 ? '' : 's'}` }); await reload()
    } catch (e) { setM({ ok: false, text: e.message }) }
  }
  return { v, set: (k, x) => setV(o => ({ ...o, [k]: x })), dirty, save, m }
}

function QualityCaps({ settings, reload, owner }) {
  const f = useSettingsForm(settings, ['quality_first', 'daily_apply_limit', 'auto_skip_score_threshold'], reload)
  const [limits, setLimits] = useState(null)
  useEffect(() => { api('/api/apply/limits').then(setLimits).catch(() => {}) }, [settings])
  return (
    <Section id="quality" title="Quality & caps" owner={owner}
             hint="Quality first caps the day at 10 applications and only queues jobs scored at or above the threshold.">
      <Toggle label="Quality first" name="quality_first" hint="stricter daily cap and a score gate for the queue"
              checked={f.v.quality_first === 'true'} onChange={x => f.set('quality_first', String(x))} />
      <div className="grid2">
        <div className="fg"><label htmlFor="a-dl">Daily apply limit (0 = no limit)</label>
          <input id="a-dl" className="in" type="number" min="0" value={f.v.daily_apply_limit} onChange={e => f.set('daily_apply_limit', e.target.value)} /></div>
        <div className="fg"><label htmlFor="a-th">Auto-skip score threshold</label>
          <input id="a-th" className="in" type="number" min="0" max="100" value={f.v.auto_skip_score_threshold} onChange={e => f.set('auto_skip_score_threshold', e.target.value)} /></div>
      </div>
      {limits && <div className="m" data-effective-cap>In force now: {limits.daily_limit ? `${limits.applied_today} of ${limits.daily_limit} today` : `${limits.applied_today} today, no cap`}{limits.dry_run ? ' (dry run: the cap is not enforced)' : ''}</div>}
      <div className="row auto-save"><button className="btn primary" disabled={!f.dirty.length} onClick={() => f.save()}>Save</button><Msg m={f.m} /></div>
    </Section>
  )
}

function ApplyModes({ settings, reload, owner, siteTypes }) {
  const f = useSettingsForm(settings, ['apply_mode', 'executor'], reload)
  const parse = () => { try { const o = JSON.parse(settings?.apply_mode_overrides || '{}'); return o && typeof o === 'object' ? o : {} } catch { return {} } }
  const [over, setOver] = useState(parse)
  useEffect(() => { setOver(parse()) }, [settings])
  const changedOver = JSON.stringify(Object.fromEntries(Object.entries(over).sort())) !== JSON.stringify(Object.fromEntries(Object.entries(parse()).sort()))
  return (
    <Section id="modes" title="Apply mode & executor" owner={owner}
             hint="Auto: the agent fills and submits the form. Assist: it prepares everything and you apply on the site yourself. The executor decides where the browser runs.">
      <div className="grid2">
        <div className="fg"><label htmlFor="a-mode">Default apply mode</label>
          <select id="a-mode" className="in" value={f.v.apply_mode} onChange={e => f.set('apply_mode', e.target.value)}>
            <option value="auto">auto</option><option value="assist">assist</option></select></div>
        <div className="fg"><label htmlFor="a-exec">Executor</label>
          <select id="a-exec" className="in" value={f.v.executor} onChange={e => f.set('executor', e.target.value)}>
            <option value="server">server — this machine</option><option value="runner">runner — your own computer</option>
            <option value="auto">auto — a runner when one is online</option></select></div>
      </div>
      <table className="auto-table" aria-label="Apply mode per site type">
        <thead><tr><th>Site type</th><th>Apply mode</th></tr></thead>
        <tbody>{siteTypes.map(st => (
          <tr key={st} style={{ cursor: 'default' }}><td>{st}</td><td>
            <select className="in" style={{ width: 'auto' }} value={over[st] || ''} aria-label={`Apply mode for ${st}`}
                    onChange={e => setOver(o => { const n = { ...o }; if (e.target.value) n[st] = e.target.value; else delete n[st]; return n })}>
              <option value="">default ({f.v.apply_mode || 'auto'})</option><option value="auto">auto</option><option value="assist">assist</option>
            </select></td></tr>))}</tbody>
      </table>
      <div className="row auto-save"><button className="btn primary" disabled={!f.dirty.length && !changedOver}
             onClick={() => f.save(changedOver ? { apply_mode_overrides: over } : {})}>Save</button><Msg m={f.m} /></div>
    </Section>
  )
}

function Ledger({ ledger }) {
  if (!ledger) return <div className="m">Loading…</div>
  const pct = r => (r === null || r === undefined ? '—' : `${Math.round(r * 100)}%`)
  return (
    <div style={{ overflowX: 'auto' }}>
      <table className="auto-table" aria-label="Reliability ledger">
        <thead><tr><th>Site type</th><th>Reached review</th><th>Reviewed</th><th>Unchanged</th><th>Edited</th><th>Rejected</th>
          <th>Auto-approved</th><th>Success (last {ledger.window})</th><th>Failures</th></tr></thead>
        <tbody>{Object.entries(ledger.site_types).map(([st, s]) => (
          <tr key={st} data-site={st} style={{ cursor: 'default' }}>
            <td>{st}</td><td>{s.attempts}</td><td>{s.reviewed}</td><td>{s.approved_unchanged}</td><td>{s.approved_edited}</td>
            <td>{s.rejected}</td><td>{s.auto_approved}</td><td>{pct(s.success_rate)}</td>
            <td title={Object.entries(s.failures).map(([k, n]) => `${k}: ${n}`).join(', ')}>{s.failures_total}</td>
          </tr>))}</tbody>
      </table>
    </div>
  )
}

function Blacklist({ settings, reload, owner }) {
  const f = useSettingsForm(settings, ['company_blacklist', 'min_salary_lpa'], reload)
  return (
    <Section id="blacklist" title="Company blacklist & minimum salary" owner={owner}
             hint="Checked before the agent opens a browser: a match is skipped.">
      <div className="fg"><label htmlFor="a-bl">Company blacklist</label>
        <textarea id="a-bl" className="in" rows={4} value={f.v.company_blacklist} onChange={e => f.set('company_blacklist', e.target.value)}
                  placeholder="One company per line, or comma-separated" />
        <div className="hint">"Tata" also covers "Tata Consultancy Services"; capitals and "Pvt Ltd" don't matter.</div></div>
      <div className="fg" style={{ maxWidth: '16rem' }}><label htmlFor="a-sal">Minimum salary (LPA, 0 = off)</label>
        <input id="a-sal" className="in" type="number" min="0" step="0.5" value={f.v.min_salary_lpa} onChange={e => f.set('min_salary_lpa', e.target.value)} />
        <div className="hint">Jobs with no readable salary are never skipped.</div></div>
      <div className="row auto-save"><button className="btn primary" disabled={!f.dirty.length} onClick={() => f.save()}>Save</button><Msg m={f.m} /></div>
    </Section>
  )
}

export default function AutomationTab({ settings, reload }) {
  const me = useMe()
  const owner = !!me?.owner
  const [ledger, setLedger] = useState(null)
  const loadLedger = () => api('/api/automation/ledger').then(setLedger).catch(() => {})
  useEffect(() => { loadLedger() }, [])
  if (!me || !settings) return <p className="m">Loading…</p>
  const siteTypes = ledger ? Object.keys(ledger.site_types) : []
  return (
    <div className="auto">
      {!owner && <div className="alert">You are not the owner account: automation settings are read-only here. Your answer bank and resumes are yours to manage.</div>}
      <QualityCaps settings={settings} reload={reload} owner={owner} />
      <ApplyModes settings={settings} reload={reload} owner={owner} siteTypes={siteTypes} />
      <AutonomySection owner={owner} siteTypes={siteTypes} onChanged={loadLedger} />
      <section className="card section auto-sec" data-section="ledger">
        <h2 style={{ padding: 0, marginBottom: 'var(--sp2)' }}>Reliability ledger</h2>
        <div className="m" style={{ marginBottom: 'var(--sp2)' }}>Per site type: how often the agent's filled form was approved as it was. Success counts only applications a person decided.</div>
        <Ledger ledger={ledger} />
      </section>
      <AnswerBankSection />
      <ResumesSection />
      <RunnerTokensSection owner={owner} />
      <NotificationsSection owner={owner} />
      <Blacklist settings={settings} reload={reload} owner={owner} />
    </div>
  )
}
