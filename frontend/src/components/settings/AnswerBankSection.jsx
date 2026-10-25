// frontend/src/components/settings/AnswerBankSection.jsx
//
// The answer bank: your saved answers to application questions (/api/answer-bank). Search,
// filter by scope, edit the question / answer / scope (global or one company) / policy
// (always use, ask first, use once), delete, add. Answers that come from your profile are
// shown read-only (change them on the Profile page). Each account manages its own bank.
'use client'
import { useCallback, useEffect, useState } from 'react'
import { api, send } from '@/lib/api'
import { Msg, Section } from './AutomationParts'

const POLICIES = [['always', 'always use'], ['ask', 'ask me first'], ['once', 'use once']]
const blank = { question: '', answer: '', scope: 'global', company: '', policy: 'always' }

function Fields({ e, set, idp }) {
  return (
    <div className="ab-fields">
      <div className="fg"><label htmlFor={`${idp}-q`}>Question</label><input id={`${idp}-q`} className="in" value={e.question} onChange={x => set('question', x.target.value)} /></div>
      <div className="fg"><label htmlFor={`${idp}-a`}>Answer</label><textarea id={`${idp}-a`} className="in" rows={2} value={e.answer} onChange={x => set('answer', x.target.value)} /></div>
      <div className="row">
        <select className="in" style={{ width: 'auto' }} value={e.scope} onChange={x => set('scope', x.target.value)} aria-label={`${idp} scope`}>
          <option value="global">every company</option><option value="company">one company</option></select>
        {e.scope === 'company' && <input className="in" style={{ width: '14rem' }} placeholder="Company" value={e.company} onChange={x => set('company', x.target.value)} aria-label={`${idp} company`} />}
        <select className="in" style={{ width: 'auto' }} value={e.policy} onChange={x => set('policy', x.target.value)} aria-label={`${idp} policy`}>
          {POLICIES.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
      </div>
    </div>
  )
}

function Entry({ entry, reload }) {
  const [edit, setEdit] = useState(null)
  const [m, setM] = useState(null)
  const save = async () => {
    setM(null)
    try { await send(`/api/answer-bank/${entry.id}`, 'PUT', edit); setEdit(null); reload() } catch (e) { setM({ ok: false, text: e.message }) }
  }
  const del = async () => {
    if (!confirm(`Delete the saved answer to "${entry.question_raw}"?`)) return
    try { await send(`/api/answer-bank/${entry.id}`, 'DELETE'); reload() } catch (e) { setM({ ok: false, text: e.message }) }
  }
  return (
    <li className="ab-entry" data-entry={entry.id}>
      {edit ? <>
        <Fields e={edit} set={(k, v) => setEdit(x => ({ ...x, [k]: v }))} idp={`entry-${entry.id}`} />
        <div className="row"><button className="btn sm primary" onClick={save}>Save</button><button className="btn sm" onClick={() => setEdit(null)}>Cancel</button><Msg m={m} /></div>
      </> : <>
        <div className="row"><b className="ab-q">{entry.question_raw}</b><span className="spacer" />
          <span className="badge">{entry.scope === 'company' ? entry.company : 'every company'}</span>
          <span className="badge">{(POLICIES.find(p => p[0] === entry.policy) || [, entry.policy])[1]}</span>
          <span className="badge">{entry.source}</span>
          {entry.read_only ? <span className="m">from your profile</span> : <>
            <button className="btn sm" onClick={() => setEdit({ question: entry.question_raw, answer: entry.answer, scope: entry.scope, company: entry.company || '', policy: entry.policy })}>Edit</button>
            <button className="btn sm danger" onClick={del}>Delete</button></>}
        </div>
        <div className="pn-text">{entry.answer}</div>
        {entry.uses > 0 && <div className="m">used {entry.uses}×</div>}
        <Msg m={m} />
      </>}
    </li>
  )
}

export default function AnswerBankSection() {
  const [rows, setRows] = useState(null)
  const [q, setQ] = useState('')
  const [scope, setScope] = useState('')
  const [draft, setDraft] = useState(blank)
  const [m, setM] = useState(null)
  const load = useCallback(() => {
    const p = new URLSearchParams({ q, scope, limit: '500' })
    return api(`/api/answer-bank?${p}`).then(setRows).catch(e => setM({ ok: false, text: e.message }))
  }, [q, scope])
  useEffect(() => { const t = setTimeout(load, 200); return () => clearTimeout(t) }, [load])
  const add = async () => {
    setM(null)
    try { await send('/api/answer-bank', 'POST', draft); setDraft(blank); setM({ ok: true, text: 'Saved' }); load() } catch (e) { setM({ ok: false, text: e.message }) }
  }
  return (
    <Section id="answers" title="Answer bank" ownerOnly={false}
             hint="What the agent answers when a form asks these questions. Your own data: every account manages its own.">
      <div className="bar">
        <input className="in grow" placeholder="Search questions and answers…" value={q} onChange={e => setQ(e.target.value)} aria-label="Search answers" />
        <select className="in" value={scope} onChange={e => setScope(e.target.value)} aria-label="Scope filter">
          <option value="">every scope</option><option value="global">every company</option><option value="company">one company</option></select>
      </div>
      {rows === null ? <div className="m">Loading…</div> : rows.length === 0 ? <div className="m">No saved answers{q ? ' match' : ' yet'}.</div>
        : <ul className="ab-list">{rows.map(e => <Entry key={e.id} entry={e} reload={load} />)}</ul>}
      <div className="rule new-rule" data-new-answer>
        <b>Add an answer</b>
        <Fields e={draft} set={(k, v) => setDraft(x => ({ ...x, [k]: v }))} idp="new-answer" />
        <div className="row auto-save"><button className="btn" disabled={!draft.question.trim() || !draft.answer.trim()} onClick={add}>Add answer</button><Msg m={m} /></div>
      </div>
    </Section>
  )
}
