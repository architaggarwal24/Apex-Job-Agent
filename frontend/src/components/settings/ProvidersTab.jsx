// frontend/src/components/settings/ProvidersTab.jsx
'use client'
import { useEffect, useState } from 'react'
import { send } from '@/lib/api'
import { PRESETS } from '@/lib/presets'

// The server only ever returns a masked "••••1234" for a saved key, so there is
// nothing to reveal; the input shows the mask as a placeholder and typing replaces it.
export const isMasked = v => typeof v === 'string' && v.startsWith('••••')
const CUSTOM = 'custom-'

function ModelPicker({ id, p, current, onPick, reload }) {
  const [draft, setDraft] = useState('')
  const [err, setErr] = useState('')
  const [busy, setBusy] = useState(false)
  const builtin = p.models || [], mine = (p.extra_models || []).filter(m => !builtin.includes(m))

  const add = async name => {
    name = (name ?? draft).trim()
    if (!name) return
    setBusy(true); setErr('')
    try { await send('/api/settings/models', 'POST', { provider: id, model: name }); setDraft(''); await reload(); onPick(name) }
    catch (e) { setErr(e.message) }
    setBusy(false)
  }
  const remove = async name => {
    try { await send('/api/settings/models/remove', 'POST', { provider: id, model: name }); await reload() }
    catch (e) { setErr(e.message) }
  }

  return (
    <div className="fg">
      <label>Model</label>
      <select className="in" value={current} onChange={e => onPick(e.target.value)}>
        {!current && <option value="">— choose a model —</option>}
        {current && !builtin.includes(current) && !mine.includes(current) && <option value={current}>{current} (current)</option>}
        {builtin.length > 0 && <optgroup label="Suggested">{builtin.map(m => <option key={m} value={m}>{m}</option>)}</optgroup>}
        {mine.length > 0 && <optgroup label="Your models">{mine.map(m => <option key={m} value={m}>{m}</option>)}</optgroup>}
      </select>
      <div className="row" style={{ marginTop: 8, flexWrap: 'nowrap' }}>
        <input className="in" placeholder="Add any model name, e.g. my-model-v2" value={draft} onChange={e => setDraft(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && add()} aria-label="Add a custom model name" />
        <button className="btn" disabled={busy || !draft.trim()} onClick={() => add()}>Add</button>
      </div>
      {err && <div className="hint msg-err">{err}</div>}
      {mine.length > 0 && (
        <div className="mchips">
          {mine.map(m => (
            <span key={m} className="mchip pick" onClick={() => onPick(m)} title="Use this model">
              {m}<button aria-label={`Remove ${m}`} onClick={e => { e.stopPropagation(); remove(m) }}>×</button>
            </span>
          ))}
        </div>
      )}
    </div>
  )
}

function ProviderCard({ id, p, settings, val, setVal, isActive, reload, onDeleted }) {
  const slug = p.custom ? id.slice(CUSTOM.length) : null
  const keyField   = p.custom ? `custom_${slug}_api_key` : `${id}_api_key`
  const modelField = p.custom ? `custom_${slug}_model`   : `${id}_model`
  const urlField   = p.custom ? null : `${id}_base_url`
  const hasUrl     = p.custom || settings[urlField] !== undefined

  const [edit, setEdit] = useState({ label: p.label, base_url: p.base_url || '' })   // custom providers only
  const [probe, setProbe] = useState(null)       // {ok, text}
  const [busy, setBusy] = useState('')
  const [fetched, setFetched] = useState(null)
  const [filter, setFilter] = useState('')
  // Providers you haven't set up collapse to one line; active/configured ones start open.
  const [open, setOpen] = useState(isActive || p.configured)
  useEffect(() => { if (isActive) setOpen(true) }, [isActive])

  const typedKey = isMasked(val(keyField)) ? '' : val(keyField)
  const baseUrl  = p.custom ? edit.base_url : (hasUrl ? val(urlField) : undefined)
  // Exactly what's on screen, saved or not — so "Test" tests what you're looking at.
  const body = () => ({ provider: id, model: val(modelField) || undefined, api_key: typedKey || undefined, base_url: baseUrl || undefined })

  const test = async () => {
    setBusy('test'); setProbe(null)
    try {
      const r = await send('/api/settings/test-llm', 'POST', body())
      setProbe({ ok: true, text: `Connected to ${r.model} in ${r.latency_ms} ms${r.response ? ` — replied “${r.response}”` : ''}.${r.note ? ' ' + r.note : ''}` })
    } catch (e) { setProbe({ ok: false, text: e.message }) }
    setBusy('')
  }
  const fetchModels = async () => {
    setBusy('fetch'); setProbe(null)
    try { const r = await send('/api/settings/fetch-models', 'POST', body()); setFetched(r.models); setFilter('') }
    catch (e) { setFetched(null); setProbe({ ok: false, text: e.message }) }
    setBusy('')
  }
  const saveDetails = async () => {
    setBusy('details')
    try { await send(`/api/settings/custom-providers/${id}`, 'PATCH', edit); await reload(); setProbe({ ok: true, text: 'Details saved.' }) }
    catch (e) { setProbe({ ok: false, text: e.message }) }
    setBusy('')
  }
  const remove = async () => {
    if (!confirm(`Remove “${p.label}”? Its saved key and models are deleted.`)) return
    try { await send(`/api/settings/custom-providers/${id}`, 'DELETE'); onDeleted(id); await reload() }
    catch (e) { setProbe({ ok: false, text: e.message }) }
  }
  // Picking a fetched model also remembers it, so it stays in this provider's list.
  const useFetched = async name => {
    try { await send('/api/settings/models', 'POST', { provider: id, model: name }); await reload() } catch {}
    setVal(modelField, name)
  }

  const known = new Set([...(p.models || []), ...(p.extra_models || [])])
  const shown = (fetched || []).filter(m => m.toLowerCase().includes(filter.toLowerCase())).slice(0, 60)
  const detailsChanged = p.custom && (edit.label !== p.label || edit.base_url !== (p.base_url || ''))

  return (
    <div className={`provider-row ${isActive ? 'active' : ''}`}>
      <div className="row" style={{ marginBottom: open ? 14 : 0 }}>
        <span className={`dot ${p.configured ? 'g' : 'm'}`} title={p.configured ? 'Ready to use' : 'No API key yet'} />
        <b>{p.label}{p.custom && <span className="chip" style={{ marginLeft: 8 }}>Custom</span>}</b>
        {!open && <span className="m">{p.configured ? val(modelField) || 'no model chosen' : 'Not set up'}</span>}
        <span style={{ flex: 1 }} />
        {isActive ? <span className="chip hi">Active</span> : <button className="btn sm" onClick={() => setVal('llm_provider', id)}>Use this provider</button>}
        {p.custom && open && <button className="link-btn danger" onClick={remove}>Remove</button>}
        <button className="link-btn" aria-expanded={open} onClick={() => setOpen(o => !o)}>{open ? 'Hide' : p.configured ? 'Edit' : 'Set up'}</button>
      </div>
      {open && <>

      {p.custom && (
        <div className="grid2">
          <div className="fg"><label>Name</label><input className="in" value={edit.label} onChange={e => setEdit({ ...edit, label: e.target.value })} /></div>
          <div className="fg"><label>Base URL</label><input className="in" value={edit.base_url} onChange={e => setEdit({ ...edit, base_url: e.target.value })} placeholder="https://host/v1" /></div>
        </div>
      )}
      {p.custom && detailsChanged && <div className="row" style={{ marginBottom: 12 }}><button className="btn sm" disabled={busy === 'details'} onClick={saveDetails}>Save name &amp; URL</button></div>}

      <div className="grid2">
        {p.key_label && (
          <div className="fg"><label>{p.key_label}</label>
            <input className="in" type="password" autoComplete="off" placeholder={settings[keyField] || (p.custom ? 'Not set (optional for local servers)' : 'Not set')}
              value={typedKey} onChange={e => setVal(keyField, e.target.value)} /></div>
        )}
        {!p.custom && hasUrl && (
          <div className="fg"><label>Base URL</label><input className="in" value={val(urlField)} onChange={e => setVal(urlField, e.target.value)} /></div>
        )}
        <ModelPicker id={id} p={p} current={val(modelField)} onPick={m => setVal(modelField, m)} reload={reload} />
      </div>

      <div className="row">
        <button className="btn" disabled={!!busy} onClick={test}>{busy === 'test' ? 'Testing…' : 'Test'}</button>
        <button className="btn" disabled={!!busy} onClick={fetchModels}>{busy === 'fetch' ? 'Fetching…' : 'Fetch models'}</button>
        {probe && <span className={probe.ok ? 'msg-ok' : 'msg-err'} style={{ fontSize: '.8125rem' }}>{probe.ok ? '✓ ' : '✗ '}{probe.text}</span>}
      </div>

      {fetched && (
        <div style={{ marginTop: 14 }}>
          <div className="row" style={{ marginBottom: 6 }}>
            <span className="m">{fetched.length} model{fetched.length === 1 ? '' : 's'} available — click one to use it.</span>
            {fetched.length > 12 && <input className="in" style={{ maxWidth: 220 }} placeholder="Filter…" value={filter} onChange={e => setFilter(e.target.value)} />}
            <button className="link-btn" onClick={() => setFetched(null)}>Hide</button>
          </div>
          <div className="mchips">
            {shown.map(m => <span key={m} className="mchip pick" onClick={() => useFetched(m)}>{known.has(m) ? '✓ ' : ''}{m}</span>)}
          </div>
          {fetched.length > shown.length && <div className="hint">Showing {shown.length} of {fetched.length}. Use the filter to narrow down.</div>}
        </div>
      )}
      </>}
    </div>
  )
}

function AddProvider({ reload, onAdded }) {
  const [open, setOpen] = useState(false)
  const [preset, setPreset] = useState(PRESETS[0].id)
  const [f, setF] = useState({ label: PRESETS[0].label, base_url: PRESETS[0].base_url, api_key: '', models: '' })
  const [msg, setMsg] = useState(null)
  const [busy, setBusy] = useState(false)

  const pick = id => {
    const pr = PRESETS.find(x => x.id === id)
    setPreset(id); setF({ ...f, label: pr.id === 'custom' ? '' : pr.label, base_url: pr.base_url })
  }
  const submit = async () => {
    setBusy(true); setMsg(null)
    const models = f.models.split(/[,\n]/).map(s => s.trim()).filter(Boolean)
    try {
      const entry = await send('/api/settings/custom-providers', 'POST', { label: f.label, base_url: f.base_url, api_key: f.api_key, models, model: models[0] || '' })
      await reload(); onAdded(entry)
      setOpen(false); setF({ label: '', base_url: '', api_key: '', models: '' })
    } catch (e) { setMsg(e.message) }
    setBusy(false)
  }
  const hint = PRESETS.find(x => x.id === preset)?.hint

  if (!open) return <div style={{ marginBottom: 16 }}><button className="btn primary" onClick={() => setOpen(true)}>+ Add a provider</button>
    <span className="m" style={{ marginLeft: 12 }}>Any service that speaks the OpenAI API — CodeCraft, Groq, Together, a local LM Studio…</span></div>

  return (
    <div className="provider-row active" style={{ marginBottom: 20 }}>
      <b style={{ display: 'block', marginBottom: 12 }}>Add an OpenAI-compatible provider</b>
      <div className="fg" style={{ maxWidth: 360 }}><label>Start from</label>
        <select className="in" value={preset} onChange={e => pick(e.target.value)}>{PRESETS.map(x => <option key={x.id} value={x.id}>{x.label}</option>)}</select></div>
      {hint && <div className="hint" style={{ marginTop: -4, marginBottom: 12 }}>{hint}</div>}
      <div className="grid2">
        <div className="fg"><label>Name</label><input className="in" value={f.label} onChange={e => setF({ ...f, label: e.target.value })} placeholder="My provider" /></div>
        <div className="fg"><label>Base URL (usually ends in /v1)</label><input className="in" value={f.base_url} onChange={e => setF({ ...f, base_url: e.target.value })} placeholder="https://api.example.com/v1" /></div>
        <div className="fg"><label>API key</label><input className="in" type="password" autoComplete="off" value={f.api_key} onChange={e => setF({ ...f, api_key: e.target.value })} placeholder="Optional for local servers" /></div>
        <div className="fg"><label>Models (optional, comma-separated)</label><input className="in" value={f.models} onChange={e => setF({ ...f, models: e.target.value })} placeholder="Or leave blank and use “Fetch models”" /></div>
      </div>
      <div className="row">
        <button className="btn primary" disabled={busy || !f.label.trim() || !f.base_url.trim()} onClick={submit}>{busy ? 'Adding…' : 'Add provider'}</button>
        <button className="btn" onClick={() => { setOpen(false); setMsg(null) }}>Cancel</button>
        {msg && <span className="msg-err">{msg}</span>}
      </div>
    </div>
  )
}

export default function ProvidersTab({ settings, providers, val, setVal, reload, onDeleted }) {
  const active = val('llm_provider')
  return (
    <div className="card section">
      <div className="fg" style={{ maxWidth: 320 }}>
        <label>Temperature (0 = deterministic, 1 = creative)</label>
        <input className="in" type="number" step="0.1" min="0" max="1" value={val('llm_temperature')} onChange={e => setVal('llm_temperature', e.target.value)} />
      </div>
      <hr className="divider" />
      <AddProvider reload={reload} onAdded={entry => setVal('llm_provider', entry.id)} />
      {Object.entries(providers).sort(([a], [b]) => (b === active) - (a === active)).map(([id, p]) => (
        <ProviderCard key={id} id={id} p={p} settings={settings} val={val} setVal={setVal} isActive={active === id} reload={reload} onDeleted={onDeleted} />
      ))}
      <p className="hint">“Test” and “Fetch models” use what's on screen right now, saved or not. Nothing is applied until you press Save.</p>
    </div>
  )
}
