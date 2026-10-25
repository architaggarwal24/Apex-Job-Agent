// frontend/src/components/applications/Ghostwriter.jsx
//
// The old Apply page's AI assistant for one job: chat with the job + profile as context,
// a cover letter in a chosen tone (with Copy), and likely interview questions.
'use client'
import { useState } from 'react'
import { send } from '@/lib/api'
import { copyText } from '@/lib/live'

export default function Ghostwriter({ app }) {
  const [tab, setTab] = useState('chat')
  const [history, setHistory] = useState([])
  const [msg, setMsg] = useState('')
  const [busy, setBusy] = useState('')
  const [tone, setTone] = useState('professional')
  const [letter, setLetter] = useState('')
  const [prep, setPrep] = useState(null)
  const [err, setErr] = useState('')
  const id = app.job_id

  const run = async (what, fn) => { setBusy(what); setErr(''); try { await fn() } catch (e) { setErr(e.message) } setBusy('') }
  const chat = () => {
    const text = msg.trim()
    if (!text) return
    setMsg('')
    const before = history
    setHistory([...before, { role: 'user', content: text }])
    run('chat', async () => {
      try {
        const r = await send(`/api/ghostwriter/chat/${id}`, 'POST', { message: text, history: before })
        setHistory([...before, { role: 'user', content: text }, { role: 'assistant', content: r.response }])
      } catch (e) { setHistory(before); setMsg(text); throw e }
    })
  }
  return (
    <>
      <div className="pills" style={{ marginBottom: 'var(--sp2)' }}>
        {[['chat', 'Chat'], ['cover', 'Cover letter'], ['prep', 'Interview']].map(([k, l]) =>
          <button key={k} className={`pill ${tab === k ? 'on' : ''}`} onClick={() => setTab(k)}>{l}</button>)}
      </div>
      {err && <div className="msg-err" style={{ marginBottom: '.5rem' }}>{err}</div>}
      {tab === 'chat' && <>
        <div className="gw-msgs">
          <div className="gw-msg ai">Hi! I'm your assistant for {app.title} at {app.company}. I know the job description and your profile. How can I help?</div>
          {history.map((h, i) => <div key={i} className={`gw-msg ${h.role === 'user' ? 'user' : 'ai'}`}>{h.content}</div>)}
          {busy === 'chat' && <div className="gw-msg ai"><span className="sp" /></div>}
        </div>
        <div className="row"><input className="in grow" placeholder="Ask anything about this job…" value={msg} onChange={e => setMsg(e.target.value)}
                                    onKeyDown={e => { if (e.key === 'Enter') chat() }} />
          <button className="btn primary" disabled={busy === 'chat' || !msg.trim()} onClick={chat}>Send</button></div>
      </>}
      {tab === 'cover' && <>
        <div className="row"><select className="in" style={{ width: 'auto' }} value={tone} onChange={e => setTone(e.target.value)} aria-label="Tone">
          <option value="professional">Professional</option><option value="enthusiastic">Enthusiastic</option><option value="concise">Concise</option></select>
          <button className="btn primary" disabled={busy === 'cover'} onClick={() => run('cover', async () => setLetter((await send(`/api/ghostwriter/cover-letter/${id}`, 'POST', { tone })).cover_letter))}>
            {busy === 'cover' ? 'Writing…' : 'Generate cover letter'}</button></div>
        {letter && <><pre className="pn-draft">{letter}</pre><button className="btn sm" onClick={() => copyText(letter)}>Copy</button></>}
      </>}
      {tab === 'prep' && <>
        <button className="btn primary" disabled={busy === 'prep'} onClick={() => run('prep', async () => setPrep(await send(`/api/ghostwriter/interview-prep/${id}`, 'POST')))}>
          {busy === 'prep' ? 'Generating…' : 'Generate interview prep'}</button>
        {prep && Object.entries({ Technical: prep.technical, Behavioural: prep.behavioural, Company: prep.company }).map(([cat, qs]) => qs?.length ? (
          <div key={cat} className="pn-sec"><div className="pn-h">{cat}</div>{qs.map((q, i) =>
            <div key={i} className="pn-note"><b>Q: {q.question}</b><div className="m">{q.answer}</div></div>)}</div>) : null)}
        {prep && !['technical', 'behavioural', 'company'].some(k => prep[k]?.length) && <div className="m">No questions generated.</div>}
      </>}
    </>
  )
}
