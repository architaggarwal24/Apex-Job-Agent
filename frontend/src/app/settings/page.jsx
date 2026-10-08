// frontend/src/app/settings/page.jsx
'use client'
import { useCallback, useEffect, useState } from 'react'
import { api, send } from '@/lib/api'
import GeneralTab from '@/components/settings/GeneralTab'
import ProvidersTab, { isMasked } from '@/components/settings/ProvidersTab'
import AgentTab from '@/components/settings/AgentTab'
import SessionsTab from '@/components/settings/SessionsTab'

const TABS = ['General', 'Providers', 'Agent Behavior', 'Job Site Sessions']

export default function Settings() {
  const [tab, setTab] = useState('General')
  const [settings, setSettings] = useState(null)
  const [providers, setProviders] = useState(null)
  const [dirty, setDirty] = useState({})
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState(null)

  // Reloads the saved settings and the provider list (which also carries each
  // provider's custom models and "configured" state). Unsaved edits in `dirty` survive it.
  const reload = useCallback(async () => {
    const [s, p] = await Promise.all([api('/api/settings'), api('/api/settings/providers')])
    setSettings(s.settings); setProviders(p)
  }, [])
  useEffect(() => { reload().catch(() => {}) }, [reload])

  const val = k => dirty[k] ?? settings?.[k] ?? ''
  const setVal = (k, v) => { setDirty(d => ({ ...d, [k]: v })); setMsg(null) }
  // A deleted provider can't stay the pending "active" choice.
  const onDeleted = id => setDirty(d => { const n = { ...d }; if (n.llm_provider === id) delete n.llm_provider; return n })

  const pending = Object.entries(dirty).filter(([, v]) => !isMasked(v))
  const save = async () => {
    if (!pending.length) return
    setSaving(true); setMsg(null)
    try {
      const r = await send('/api/settings', 'POST', Object.fromEntries(pending))
      setMsg({ ok: true, text: `Saved ${r.saved} setting${r.saved === 1 ? '' : 's'}` })
      setDirty({}); await reload()
    } catch (e) { setMsg({ ok: false, text: e.message }) }
    setSaving(false)
  }

  const needsSave = tab === 'Providers' || tab === 'Agent Behavior'

  return (
    <>
      <h1>Settings</h1>
      <p className="sub">Configure the agent, its LLM providers, and how this looks.</p>

      <div className="tabs" role="tablist">
        {TABS.map(t => (
          <button key={t} role="tab" aria-selected={tab === t} className={`tab ${tab === t ? 'on' : ''}`} onClick={() => setTab(t)}>
            {t}{(t === 'Providers' || t === 'Agent Behavior') && pending.length > 0 && tab !== t ? ' •' : ''}
          </button>
        ))}
      </div>

      {tab === 'General' && <GeneralTab />}
      {tab === 'Providers' && (settings && providers
        ? <ProvidersTab settings={settings} providers={providers} val={val} setVal={setVal} reload={reload} onDeleted={onDeleted} />
        : <p className="m">Loading…</p>)}
      {tab === 'Agent Behavior' && (settings ? <AgentTab val={val} setVal={setVal} /> : <p className="m">Loading…</p>)}
      {tab === 'Job Site Sessions' && <SessionsTab />}

      {needsSave && (
        <div className="save-bar">
          {msg && <span className={msg.ok ? 'msg-ok' : 'msg-err'}>{msg.text}</span>}
          {!msg && pending.length > 0 && <span className="m">{pending.length} unsaved change{pending.length === 1 ? '' : 's'}</span>}
          <button className="btn primary" disabled={saving || pending.length === 0} onClick={save}>
            {saving ? 'Saving…' : 'Save changes'}
          </button>
        </div>
      )}
    </>
  )
}
