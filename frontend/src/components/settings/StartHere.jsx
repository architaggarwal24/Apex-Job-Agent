// frontend/src/components/settings/StartHere.jsx
//
// Settings › Start here: the four things that matter, in order, each with whether it is
// done (GET /api/setup/checklist), what is set now, and one button to the place to set it.
// Search titles and locations can be set right here (POST /api/search-prefs).
'use client'
import { useEffect, useState } from 'react'
import Link from 'next/link'
import { Check } from 'lucide-react'
import { api, send } from '@/lib/api'
import { SETTINGS_PAGES } from '@/lib/settingsNav'
import { SkeletonCard } from '@/components/layout/Skeleton'

const split = s => s.split(',').map(x => x.trim()).filter(Boolean)

function Step({ n, done, title, now, children }) {
  return (
    <li className={`sh-step ${done ? 'done' : ''}`} data-step={n}>
      <span className="sh-n" aria-label={done ? 'done' : 'to do'}>{done ? <Check className="lucide" /> : n}</span>
      <div className="sh-body"><b>{title}</b>{now && <p className="m">{now}</p>}<div className="sh-act">{children}</div></div>
    </li>
  )
}

export default function StartHere({ settings, owner, go }) {
  const [list, setList] = useState(null)
  const [status, setStatus] = useState(null)
  const [titles, setTitles] = useState('')
  const [places, setPlaces] = useState('')
  const [msg, setMsg] = useState(null)
  const load = () => api('/api/setup/checklist').then(l => {
    setList(l)
    const sp = l.items.find(i => i.key === 'search_prefs')
    setTitles((sp?.titles || []).join(', ')); setPlaces((sp?.locations || []).join(', '))
  }).catch(() => {})
  useEffect(() => { load(); api('/api/status').then(setStatus).catch(() => {}) }, [])
  if (!list || !settings) return <SkeletonCard lines={6} label="Loading the setup steps" />
  const item = k => list.items.find(i => i.key === k) || {}
  const savePrefs = async () => {
    try { await send('/api/search-prefs', 'POST', { positions: split(titles), locations: split(places) }); setMsg({ ok: true, text: 'Saved' }); load() }
    catch (e) { setMsg({ ok: false, text: e.message }) }
  }
  const mode = settings.apply_mode === 'assist' ? 'You apply on each site yourself; the agent prepares the answers.' : 'The agent fills each form; you check it and press Submit.'
  const where = { runner: 'on your own computer', auto: 'on your computer when its runner is on, else on this server', server: 'on this server' }[settings.executor] || 'on this server'
  return (
    <div className="start-here">
      <ol className="sh-steps">
        <Step n={1} done={item('llm').done} title="Choose the AI model"
              now={status?.llm ? `Using ${status.llm.label || status.llm.provider} / ${status.llm.model}. ${item('llm').done ? 'Tested and working.' : 'Not tested yet — press Test on its card.'}` : 'No provider yet.'}>
          <button className="btn sm primary" onClick={() => go('ai', 'providers')}>Open AI providers</button>
        </Step>
        <Step n={2} done={item('resume').done && item('profile').done} title="Add your resume and details"
              now={`${item('resume').done ? 'Resume on file' : 'No resume yet'} · ${item('profile').done ? 'details complete' : `details ${(item('profile').detail || 'missing').replace(/_/g, ' ')}`}`}>
          <Link className="btn sm" href="/profile">Open Profile</Link>
        </Step>
        <Step n={3} done={item('search_prefs').done} title="Say what to search for" now="Job titles and places, comma-separated. “Remote” is always searched too.">
          <div className="sh-prefs">
            <input className="in" value={titles} onChange={e => { setTitles(e.target.value); setMsg(null) }} placeholder="Job titles, e.g. AI Engineer, Backend Engineer" aria-label="Job titles" />
            <input className="in" value={places} onChange={e => { setPlaces(e.target.value); setMsg(null) }} placeholder="Places, e.g. Bengaluru, Pune" aria-label="Places" />
            <button className="btn sm" disabled={!split(titles).length || !split(places).length} onClick={savePrefs}>Save</button>
            {msg && <span className={msg.ok ? 'msg-ok' : 'msg-err'}>{msg.text}</span>}
          </div>
        </Step>
        <Step n={4} done title="Decide who sends applications" now={`${mode} It runs ${where}.`}>
          <button className="btn sm" onClick={() => go('applying', 'modes')}>Change who submits</button>
          <button className="btn sm ghost" onClick={() => go('applying', 'runner')}>Change where it runs</button>
          {!owner && <span className="m">Only the owner account can change these.</span>}
        </Step>
      </ol>
      <h3 className="sh-h">Everything else is optional</h3>
      <div className="sh-more">
        {SETTINGS_PAGES.filter(([k]) => k !== 'start').map(([k, l, d]) => (
          <button key={k} className="sh-card" onClick={() => go(k)}><b>{l}</b><span className="m">{d}</span></button>))}
      </div>
    </div>
  )
}
