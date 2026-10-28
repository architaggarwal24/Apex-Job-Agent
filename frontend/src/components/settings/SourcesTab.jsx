// frontend/src/components/settings/SourcesTab.jsx
//
// The search parts of Settings — where jobs come from (app/settings/page.jsx shows each in its
// own section). Each part saves on its own (no shared save bar).
//
//   Job boards       search_engine_<board> (jobspy | apify | browser), results per query,
//                    hours old                                               POST /api/settings
//   Apify            apify_api_token, apify_actor_<board>                    POST /api/settings
//   Keys             adzuna_app_id, adzuna_api_key, jsearch_api_key,
//                    himalayas_api_key                                       POST /api/settings
//   Sources          search_sources on/off per source, last run              GET /api/sources
//   Company boards   the ATS boards followed: add / pause / remove           /api/sources/companies
//
// Search settings are the OWNER's: other accounts see them read-only (a disabled fieldset).
'use client'
import { useEffect, useState } from 'react'
import { api, send } from '@/lib/api'
import { isMasked } from './ProvidersTab'
import { useSettingsForm } from './AutomationTab'
import { Msg, Section, Toggle } from './AutomationParts'

const BOARDS = [['linkedin', 'LinkedIn'], ['indeed', 'Indeed'], ['naukri', 'Naukri']]
const ENGINES = [['jobspy', 'JobSpy — no login, no browser'], ['apify', 'Apify actor — needs a token'],
                 ['browser', 'Browser — your saved login']]

// A saved secret comes back masked ("••••1234"); blank or still-masked means "keep it".
function SecretInput({ id, label, value, onChange, hint }) {
  return (
    <div className="fg"><label htmlFor={id}>{label} <span className={`badge ${isMasked(value) ? 'b-done' : 'b-idle'}`}>{isMasked(value) ? 'set' : value ? 'new' : 'not set'}</span></label>
      <input id={id} className="in" type="password" autoComplete="new-password" placeholder={isMasked(value) ? value : 'Not set'}
             value={isMasked(value) ? '' : value || ''} onChange={e => onChange(e.target.value)} />
      {hint && <div className="hint">{hint}</div>}
    </div>
  )
}

export function Boards({ settings, reload, owner }) {
  const keys = [...BOARDS.map(([b]) => `search_engine_${b}`), 'search_results_per_query', 'search_hours_old']
  const f = useSettingsForm(settings, keys, reload)
  return (
    <Section id="boards" title="Job boards" owner={owner}
             hint="How LinkedIn, Indeed and Naukri are searched. JobSpy needs no account; Apify runs a Store actor with your token; Browser opens the old window with your saved login.">
      <table className="auto-table" aria-label="Search engine per board">
        <thead><tr><th>Board</th><th>Engine</th></tr></thead>
        <tbody>{BOARDS.map(([b, label]) => (
          <tr key={b} style={{ cursor: 'default' }}><td>{label}</td><td>
            <select className="in" style={{ width: 'auto' }} value={f.v[`search_engine_${b}`] || 'jobspy'} aria-label={`Engine for ${label}`}
                    onChange={e => f.set(`search_engine_${b}`, e.target.value)}>
              {ENGINES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
            </select></td></tr>))}</tbody>
      </table>
      <div className="grid2">
        <div className="fg"><label htmlFor="s-rpq">Results per title × location, per board</label>
          <input id="s-rpq" className="in" type="number" min="1" max="200" value={f.v.search_results_per_query} onChange={e => f.set('search_results_per_query', e.target.value)} /></div>
        <div className="fg"><label htmlFor="s-hours">Only postings newer than (hours, 0 = any age)</label>
          <input id="s-hours" className="in" type="number" min="0" max="8760" value={f.v.search_hours_old} onChange={e => f.set('search_hours_old', e.target.value)} /></div>
      </div>
      <div className="row auto-save"><button className="btn primary" disabled={!f.dirty.length} onClick={() => f.save()}>Save</button><Msg m={f.m} /></div>
    </Section>
  )
}

export function Apify({ settings, reload, owner }) {
  const f = useSettingsForm(settings, ['apify_api_token', ...BOARDS.map(([b]) => `apify_actor_${b}`)], reload)
  return (
    <Section id="apify" title="Apify" owner={owner}
             hint="Only used by boards set to the Apify engine. The free plan includes $5 of usage a month; a token is made at apify.com → Settings → Integrations.">
      <SecretInput id="apify_api_token" label="API token" value={f.v.apify_api_token} onChange={x => f.set('apify_api_token', x)} />
      <div className="grid2">{BOARDS.map(([b, label]) => (
        <div className="fg" key={b}><label htmlFor={`actor-${b}`}>{label} actor</label>
          <input id={`actor-${b}`} className="in" value={f.v[`apify_actor_${b}`] || ''} placeholder="owner~name" onChange={e => f.set(`apify_actor_${b}`, e.target.value)} /></div>))}</div>
      <div className="row auto-save"><button className="btn primary" disabled={!f.dirty.length} onClick={() => f.save()}>Save</button><Msg m={f.m} /></div>
    </Section>
  )
}

export function Keys({ settings, reload, owner }) {
  const f = useSettingsForm(settings, ['adzuna_app_id', 'adzuna_api_key', 'jsearch_api_key', 'himalayas_api_key'], reload)
  return (
    <Section id="keys" title="Keys for keyed sources" owner={owner}
             hint="Free keys: Adzuna (developer.adzuna.com, 250 calls a day), JSearch on RapidAPI (200 calls a month), Himalayas (optional, raises its limit). A keyed source is skipped until its key is here.">
      <div className="grid2">
        <div className="fg"><label htmlFor="adzuna_app_id">Adzuna app id</label>
          <input id="adzuna_app_id" className="in" value={f.v.adzuna_app_id} onChange={e => f.set('adzuna_app_id', e.target.value)} /></div>
        <SecretInput id="adzuna_api_key" label="Adzuna app key" value={f.v.adzuna_api_key} onChange={x => f.set('adzuna_api_key', x)} />
        <SecretInput id="jsearch_api_key" label="JSearch (RapidAPI) key" value={f.v.jsearch_api_key} onChange={x => f.set('jsearch_api_key', x)} />
        <SecretInput id="himalayas_api_key" label="Himalayas key (optional)" value={f.v.himalayas_api_key} onChange={x => f.set('himalayas_api_key', x)} />
      </div>
      <div className="row auto-save"><button className="btn primary" disabled={!f.dirty.length} onClick={() => f.save()}>Save</button><Msg m={f.m} /></div>
    </Section>
  )
}

const lastRun = r => {
  if (!r) return 'never run'
  const when = r.ran_at ? new Date(r.ran_at).toLocaleString() : ''
  return r.error ? `${when} — ${r.error}` : `${when} — ${r.found} found`
}

export function SourceList({ owner, sources, reload }) {
  const [flags, setFlags] = useState({})
  const [m, setM] = useState(null)
  useEffect(() => { setFlags(Object.fromEntries((sources || []).map(s => [s.name, s.enabled]))) }, [sources])
  if (!sources) return <Section id="sources" title="Sources" owner={owner}><div className="m">Loading…</div></Section>
  const dirty = sources.some(s => flags[s.name] !== undefined && flags[s.name] !== s.enabled)
  const save = async () => {
    setM(null)
    try {
      await send('/api/settings', 'POST', { search_sources: Object.fromEntries(sources.filter(s => flags[s.name] === false).map(s => [s.name, false])) })
      setM({ ok: true, text: 'Saved' }); await reload()
    } catch (e) { setM({ ok: false, text: e.message }) }
  }
  const group = (kind, title, hint) => (
    <div style={{ marginBottom: 'var(--sp2)' }}>
      <h3 style={{ margin: '.25rem 0' }}>{title}</h3><div className="m" style={{ marginBottom: '.25rem' }}>{hint}</div>
      {sources.filter(s => s.kind === kind).map(s => (
        <div key={s.name} className="row" data-source={s.name} style={{ alignItems: 'flex-start', gap: '.75rem' }}>
          <Toggle label={s.label} name={`source ${s.name}`} checked={flags[s.name] ?? s.enabled} onChange={x => setFlags(o => ({ ...o, [s.name]: x }))}
                  hint={[s.kind === 'ats' ? `${s.boards} board${s.boards === 1 ? '' : 's'} followed` : '',
                         s.needs_key.length ? (s.key_set ? 'key set' : 'needs a key') : '',
                         s.min_interval ? `at most once every ${Math.round(s.min_interval / 60)} min` : ''].filter(Boolean).join(' · ') || undefined} />
          <span className="m" style={{ marginLeft: 'auto', whiteSpace: 'nowrap' }} data-last-run>{lastRun(s.last_run)}</span>
        </div>))}
    </div>)
  return (
    <Section id="sources" title="Sources" owner={owner}
             hint="Read on every search, after the job boards. None needs a login; the job is applied for on the company's own form when the source gives one.">
      {group('aggregator', 'Job feeds', 'Public feeds searched by your titles and locations.')}
      {group('ats', 'Company boards by ATS', 'Every company board the agent follows on that ATS (next section). Turning one off skips all of its boards.')}
      <div className="row auto-save"><button className="btn primary" disabled={!dirty} onClick={save}>Save</button><Msg m={m} /></div>
    </Section>
  )
}

export function Companies({ owner, sources, reload }) {
  const [rows, setRows] = useState(null)
  const [m, setM] = useState(null)
  const [add, setAdd] = useState({ url: '', vendor: 'greenhouse', slug: '', name: '' })
  const [busy, setBusy] = useState(false)
  const load = () => api('/api/sources/companies').then(j => setRows(j.companies)).catch(e => setM({ ok: false, text: e.message }))
  useEffect(() => { load() }, [])
  const vendors = (sources || []).filter(s => s.kind === 'ats')
  const submit = async () => {
    setBusy(true); setM(null)
    try {
      const body = add.url.trim() ? { url: add.url.trim(), name: add.name } : { vendor: add.vendor, slug: add.slug.trim(), name: add.name }
      const r = await send('/api/sources/companies', 'POST', body)
      setM({ ok: true, text: `Following ${r.name} on ${r.vendor}` }); setAdd({ url: '', vendor: add.vendor, slug: '', name: '' }); await load(); await reload()
    } catch (e) { setM({ ok: false, text: e.message }) }
    setBusy(false)
  }
  const toggle = async c => { try { await send(`/api/sources/companies/${c.id}`, 'PUT', { enabled: !c.enabled }); await load(); await reload() } catch (e) { setM({ ok: false, text: e.message }) } }
  const remove = async c => {
    if (!confirm(`Stop following ${c.name} on ${c.vendor}?`)) return
    try { await send(`/api/sources/companies/${c.id}`, 'DELETE'); setM({ ok: true, text: `Removed ${c.name}` }); await load(); await reload() } catch (e) { setM({ ok: false, text: e.message }) }
  }
  return (
    <Section id="companies" title="Company boards" owner={owner}
             hint="Boards the agent reads directly. It learns them from any job link to a supported ATS; add one by pasting a link to the board or a job on it.">
      {rows === null ? <div className="m">Loading…</div> : !rows.length ? <div className="m" data-empty>No company boards yet: run a search, or add one below.</div> : (
        <div style={{ overflowX: 'auto' }}>
          <table className="auto-table" aria-label="Company boards">
            <thead><tr><th>Company</th><th>ATS</th><th>Board</th><th>From</th><th>Last run</th><th></th></tr></thead>
            <tbody>{rows.map(c => (
              <tr key={c.id} data-company={c.slug} style={{ cursor: 'default', opacity: c.enabled ? 1 : .55 }}>
                <td>{c.name}</td><td>{c.vendor}</td><td className="m">{c.slug}</td><td className="m">{c.origin === 'harvest' ? 'learnt' : 'you'}</td>
                <td className="m">{c.last_run_at ? (c.last_error ? `${c.last_error}` : `${c.last_count ?? 0} matching`) : '—'}</td>
                <td className="row" style={{ justifyContent: 'flex-end' }}>
                  <button className="btn sm" onClick={() => toggle(c)}>{c.enabled ? 'Pause' : 'Resume'}</button>
                  <button className="btn sm danger" onClick={() => remove(c)}>Remove</button></td>
              </tr>))}</tbody>
          </table>
        </div>)}
      <h3 style={{ margin: 'var(--sp2) 0 .25rem' }}>Follow a board</h3>
      <div className="grid2">
        <div className="fg"><label htmlFor="c-url">Link to the board or a job on it</label>
          <input id="c-url" className="in" placeholder="https://boards.greenhouse.io/company" value={add.url} onChange={e => setAdd(a => ({ ...a, url: e.target.value }))} /></div>
        <div className="fg"><label htmlFor="c-name">Company name (optional)</label>
          <input id="c-name" className="in" value={add.name} onChange={e => setAdd(a => ({ ...a, name: e.target.value }))} /></div>
        <div className="fg"><label htmlFor="c-vendor">…or the ATS</label>
          <select id="c-vendor" className="in" value={add.vendor} onChange={e => setAdd(a => ({ ...a, vendor: e.target.value }))}>
            {vendors.map(v => <option key={v.name} value={v.name}>{v.label}</option>)}</select></div>
        <div className="fg"><label htmlFor="c-slug">and its board slug</label>
          <input id="c-slug" className="in" placeholder="company" value={add.slug} onChange={e => setAdd(a => ({ ...a, slug: e.target.value }))} /></div>
      </div>
      <div className="row auto-save"><button className="btn primary" disabled={busy || !(add.url.trim() || add.slug.trim())} onClick={submit}>Follow</button><Msg m={m} /></div>
    </Section>
  )
}
