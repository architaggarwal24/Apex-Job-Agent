// frontend/src/components/settings/AutomationTab.jsx
//
// The applying parts of Settings (app/settings/page.jsx shows each in a fold), each reading
// and writing its real endpoint and saved on its own (no shared save bar). Plain words on
// screen; the setting each one writes is named here.
//
//   How many a day         quality_first ("careful mode"), daily_apply_limit, auto_skip_score_threshold
//   Who submits            apply_mode (auto = the agent fills, you approve; assist = you apply),
//                          apply_mode_overrides (per kind of site, under "Different for some kinds of site")
//   Where it runs          executor (runner | auto | server)
//   Practice mode & pace   dry_run, min/max_delay_between_applies
//   AI job scoring         enable_llm_scoring
//   Skip these             company_blacklist, min_salary_lpa
//   Reliability ledger     per site type (GET /api/automation/ledger)
// All POST /api/settings except the ledger.
//
// These settings are the OWNER's: other accounts see them read-only (a disabled fieldset).
'use client'
import { useEffect, useState } from 'react'
import { api, send } from '@/lib/api'
import { Msg, Section, Toggle } from './AutomationParts'

// One group of plain settings, saved together through POST /api/settings (owner-only there too).
export function useSettingsForm(settings, keys, reload) {
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

export function QualityCaps({ settings, reload, owner }) {
  const f = useSettingsForm(settings, ['quality_first', 'daily_apply_limit', 'auto_skip_score_threshold'], reload)
  const [limits, setLimits] = useState(null)
  useEffect(() => { api('/api/apply/limits').then(setLimits).catch(() => {}) }, [settings])
  return (
    <Section id="quality" title="How many a day" owner={owner}
             hint="Careful mode keeps the day small and only lets well-matched jobs into the queue.">
      <Toggle label="Careful mode (recommended)" name="quality_first" hint="at most 10 applications a day, and only jobs that score at least the minimum below can be queued"
              checked={f.v.quality_first === 'true'} onChange={x => f.set('quality_first', String(x))} />
      <div className="grid2">
        <div className="fg"><label htmlFor="a-dl">Most applications per day (0 = no limit)</label>
          <input id="a-dl" className="in" type="number" min="0" value={f.v.daily_apply_limit} onChange={e => f.set('daily_apply_limit', e.target.value)} />
          <div className="hint">With careful mode on, the lower of this and 10.</div></div>
        <div className="fg"><label htmlFor="a-th">Minimum fit score</label>
          <input id="a-th" className="in" type="number" min="0" max="100" value={f.v.auto_skip_score_threshold} onChange={e => f.set('auto_skip_score_threshold', e.target.value)} />
          <div className="hint">AI scores below this are skipped after a search (recommended 30–40). Insights suggests a value from your results.</div></div>
      </div>
      {limits && <div className="m" data-effective-cap>In force now: {limits.daily_limit ? `${limits.applied_today} of ${limits.daily_limit} today` : `${limits.applied_today} today, no cap`}{limits.dry_run ? ' (dry run: the cap is not enforced)' : ''}</div>}
      <div className="row auto-save"><button className="btn primary" disabled={!f.dirty.length} onClick={() => f.save()}>Save</button><Msg m={f.m} /></div>
    </Section>
  )
}

const MODE_TEXT = { auto: 'The agent fills the form; you check it and press Submit', assist: 'You apply on the site yourself; the agent prepares the answers and cover letter' }

export function ApplyModes({ settings, reload, owner, siteTypes }) {
  const f = useSettingsForm(settings, ['apply_mode'], reload)
  const parse = () => { try { const o = JSON.parse(settings?.apply_mode_overrides || '{}'); return o && typeof o === 'object' ? o : {} } catch { return {} } }
  const [over, setOver] = useState(parse)
  useEffect(() => { setOver(parse()) }, [settings])
  const changedOver = JSON.stringify(Object.fromEntries(Object.entries(over).sort())) !== JSON.stringify(Object.fromEntries(Object.entries(parse()).sort()))
  return (
    <Section id="modes" title="Who submits" owner={owner}
             hint="Nothing is ever sent without you: either you approve what the agent filled, or you apply yourself.">
      <div className="choice" role="radiogroup" aria-label="Who submits">
        {['auto', 'assist'].map(m => (
          <label key={m} className={`choice-it ${f.v.apply_mode === m ? 'on' : ''}`}>
            <input type="radio" name="apply_mode" value={m} checked={f.v.apply_mode === m} onChange={() => f.set('apply_mode', m)} />
            <span><span><b>{m === 'auto' ? 'The agent fills it, you approve' : 'I apply myself'}</b>{m === 'auto' && <em className="rec">recommended</em>}</span><small>{MODE_TEXT[m]}</small></span>
          </label>))}
      </div>
      <details className="adv">
        <summary>Different for some kinds of site</summary>
        <p className="m">E.g. apply yourself on LinkedIn Easy Apply but let the agent fill Greenhouse forms.</p>
        <table className="auto-table" aria-label="Apply mode per site type">
          <thead><tr><th>Kind of site</th><th>Who submits</th></tr></thead>
          <tbody>{siteTypes.map(st => (
            <tr key={st} style={{ cursor: 'default' }}><td>{st.replace(/_/g, ' ')}</td><td>
              <select className="in" style={{ width: 'auto' }} value={over[st] || ''} aria-label={`Apply mode for ${st}`}
                      onChange={e => setOver(o => { const n = { ...o }; if (e.target.value) n[st] = e.target.value; else delete n[st]; return n })}>
                <option value="">as above</option><option value="auto">the agent fills it, you approve</option><option value="assist">I apply myself</option>
              </select></td></tr>))}</tbody>
        </table>
      </details>
      <div className="row auto-save"><button className="btn primary" disabled={!f.dirty.length && !changedOver}
             onClick={() => f.save(changedOver ? { apply_mode_overrides: over } : {})}>Save</button><Msg m={f.m} /></div>
    </Section>
  )
}

// Where the browser runs (setting `executor`): here, your own computer (the runner), or
// your computer when its runner is online.
export function Executor({ settings, reload, owner }) {
  const f = useSettingsForm(settings, ['executor'], reload)
  const opts = [['runner', 'My computer', 'Your own browser and logins, in a window you can watch (needs the runner below)'],
                ['auto', 'My computer when it is on, else this server', 'Falls back to the server when no runner is online'],
                ['server', 'This server', 'A browser on the machine Apex runs on']]
  return (
    <Section id="executor" title="Where it runs" owner={owner} hint="Which computer opens the job site and fills the form.">
      <div className="choice" role="radiogroup" aria-label="Where it runs">
        {opts.map(([v, l, h]) => (
          <label key={v} className={`choice-it ${f.v.executor === v ? 'on' : ''}`}>
            <input type="radio" name="executor" value={v} checked={f.v.executor === v} onChange={() => f.set('executor', v)} />
            <span><b>{l}</b><small>{h}</small></span>
          </label>))}
      </div>
      <div className="row auto-save"><button className="btn primary" disabled={!f.dirty.length} onClick={() => f.save()}>Save</button><Msg m={f.m} /></div>
    </Section>
  )
}

// Practice mode (dry_run) and the pause between applications.
export function PaceAndPractice({ settings, reload, owner }) {
  const f = useSettingsForm(settings, ['dry_run', 'min_delay_between_applies', 'max_delay_between_applies'], reload)
  return (
    <Section id="pace" title="Practice mode & pace" owner={owner} hint="Try everything safely first; then set how long the agent waits between applications.">
      <Toggle label="Practice mode" name="dry_run" hint="fill every form but never send one — the daily cap is not counted"
              checked={f.v.dry_run === 'true'} onChange={x => f.set('dry_run', String(x))} />
      <div className="grid2">
        <div className="fg"><label htmlFor="a-mind">Wait at least (seconds)</label>
          <input id="a-mind" className="in" type="number" min="0" value={f.v.min_delay_between_applies} onChange={e => f.set('min_delay_between_applies', e.target.value)} /></div>
        <div className="fg"><label htmlFor="a-maxd">…and at most (seconds)</label>
          <input id="a-maxd" className="in" type="number" min="0" value={f.v.max_delay_between_applies} onChange={e => f.set('max_delay_between_applies', e.target.value)} /></div>
      </div>
      <div className="hint">A random pause in this range between two applications looks human to job sites (recommended 45–120).</div>
      <div className="row auto-save"><button className="btn primary" disabled={!f.dirty.length} onClick={() => f.save()}>Save</button><Msg m={f.m} /></div>
    </Section>
  )
}

// AI job scoring (enable_llm_scoring): the model scores new jobs after each search.
export function AiScoring({ settings, reload, owner }) {
  const f = useSettingsForm(settings, ['enable_llm_scoring'], reload)
  return (
    <Section id="scoring" title="AI job scoring" owner={owner}
             hint="Every job always gets an instant keyword match. With this on, the AI also reads each new job and scores it against your profile.">
      <Toggle label="Score new jobs with the AI" name="enable_llm_scoring" hint="slower and uses your provider's tokens; needed for careful mode's score gate and for auto-skip"
              checked={f.v.enable_llm_scoring === 'true'} onChange={x => f.set('enable_llm_scoring', String(x))} />
      <div className="row auto-save"><button className="btn primary" disabled={!f.dirty.length} onClick={() => f.save()}>Save</button><Msg m={f.m} /></div>
    </Section>
  )
}

export function Ledger({ ledger }) {
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

export function Blacklist({ settings, reload, owner }) {
  const f = useSettingsForm(settings, ['company_blacklist', 'min_salary_lpa'], reload)
  return (
    <Section id="blacklist" title="Skip these" owner={owner}
             hint="Checked before the agent opens a browser: a match is skipped and moved to the Archive.">
      <div className="fg"><label htmlFor="a-bl">Never apply to these companies</label>
        <textarea id="a-bl" className="in" rows={4} value={f.v.company_blacklist} onChange={e => f.set('company_blacklist', e.target.value)}
                  placeholder="One company per line, or comma-separated" />
        <div className="hint">"Tata" also covers "Tata Consultancy Services"; capitals and "Pvt Ltd" don't matter.</div></div>
      <div className="fg" style={{ maxWidth: '16rem' }}><label htmlFor="a-sal">Skip jobs paying less than (LPA, 0 = off)</label>
        <input id="a-sal" className="in" type="number" min="0" step="0.5" value={f.v.min_salary_lpa} onChange={e => f.set('min_salary_lpa', e.target.value)} />
        <div className="hint">Jobs with no readable salary are never skipped.</div></div>
      <div className="row auto-save"><button className="btn primary" disabled={!f.dirty.length} onClick={() => f.save()}>Save</button><Msg m={f.m} /></div>
    </Section>
  )
}
