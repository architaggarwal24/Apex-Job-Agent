// frontend/src/components/applications/FilterPanel.jsx
//
// The Applications filters, in one panel that slides in from the right. Everything applies
// as you change it (the list behind updates live and the footer counts the matches); every
// filter lives in the URL (lib/filters.js), so a combination can be saved as a named view
// (db/views_db.py, /api/views) and opened again with one click.
//
//   Saved views        apply · delete · save these filters as…
//   Where              location text · remote / hybrid / on-site
//   Money & freshness  minimum salary (LPA) · hide jobs that don't say · posted within
//   Fit                score range · scored by AI / quick match / not scored · seniority
//   Company & source   only / never these companies · boards · applies on (ATS or the board)
'use client'
import { useState } from 'react'
import { motion } from 'motion/react'
import { X } from 'lucide-react'
import { AGES, KINDS, LEVELS, WORK, listOf } from '@/lib/filters'

const LABEL = { remote: 'Remote', hybrid: 'Hybrid', onsite: 'On-site', ai: 'AI-scored', quick: 'Quick match', none: 'Not scored',
  intern: 'Intern', junior: 'Junior', mid: 'Mid', senior: 'Senior' }
const AGE_LABEL = { '': 'Any time', 1: '24 h', 3: '3 days', 7: '7 days', 30: '30 days' }

function Toggles({ name, values, value, onChange, labels = LABEL }) {
  const on = new Set(listOf(value))
  const flip = v => { const n = new Set(on); n.has(v) ? n.delete(v) : n.add(v); onChange(values.filter(x => n.has(x)).join(',')) }
  return (
    <div className="fp-toggles" role="group" aria-label={name}>
      {values.map(v => <button key={v} type="button" className={`fp-tog ${on.has(v) ? 'on' : ''}`} aria-pressed={on.has(v)} onClick={() => flip(v)}>{labels[v] || v}</button>)}
    </div>
  )
}

function Section({ title, children }) {
  return <section className="fp-sec"><h3>{title}</h3>{children}</section>
}

export default function FilterPanel({ f, set, sources, ats, count, total, onClose, onClear, views, onSaveView, onApplyView, onDeleteView }) {
  const [name, setName] = useState('')
  const num = (k, v, max) => set(k, v === '' ? '' : String(Math.max(0, Math.min(max, Number(v) || 0))))
  return (
    <motion.aside className="fpanel" role="dialog" aria-label="Filters" initial={{ x: 40, opacity: 0 }} animate={{ x: 0, opacity: 1 }}
      exit={{ x: 40, opacity: 0, transition: { duration: 0.12 } }} transition={{ type: 'spring', stiffness: 420, damping: 38 }}>
      <header className="fp-head"><h2>Filters</h2><button className="btn sm ghost" onClick={onClose} aria-label="Close the filters"><X className="lucide" /></button></header>
      <div className="fp-body">
        <Section title="Saved views">
          {views.length ? <div className="fp-views">{views.map(v => (
            <span key={v.id} className="fp-view" data-view={v.name}>
              <button className="link-btn" onClick={() => onApplyView(v)}>{v.name}</button>
              <button className="fp-x" onClick={() => onDeleteView(v)} aria-label={`Delete the view ${v.name}`}><X className="lucide" /></button>
            </span>))}</div> : <p className="m">None yet. Set some filters, name them below.</p>}
          <form className="fp-save" onSubmit={e => { e.preventDefault(); if (name.trim()) { onSaveView(name.trim()); setName('') } }}>
            <input className="in" value={name} onChange={e => setName(e.target.value)} placeholder="Name these filters, e.g. Remote AI ≥ 70" aria-label="View name" maxLength={60} />
            <button className="btn sm" disabled={!name.trim()}>Save view</button>
          </form>
        </Section>

        <Section title="Where">
          <label className="fp-lbl" htmlFor="fp-loc">Location contains</label>
          <input id="fp-loc" className="in" value={f.loc} onChange={e => set('loc', e.target.value.slice(0, 80))} placeholder="e.g. Pune, Bengaluru" />
          <span className="fp-lbl">Work mode</span>
          <Toggles name="Work mode" values={WORK} value={f.work} onChange={v => set('work', v)} />
        </Section>

        <Section title="Money & freshness">
          <label className="fp-lbl" htmlFor="fp-sal">Minimum salary (LPA)</label>
          <input id="fp-sal" className="in fp-num" type="number" min="0" max="500" value={f.sal} onChange={e => num('sal', e.target.value, 500)} placeholder="e.g. 12" />
          <label className="fp-check"><input type="checkbox" checked={!!f.nosal} onChange={e => set('nosal', e.target.checked ? '1' : '')} /> Hide jobs that don't state a salary</label>
          <span className="fp-lbl">Posted within</span>
          <div className="fp-toggles" role="radiogroup" aria-label="Posted within">
            {['', ...AGES].map(a => <button key={a || 'any'} type="button" role="radio" aria-checked={f.age === a} className={`fp-tog ${f.age === a ? 'on' : ''}`} onClick={() => set('age', a)}>{AGE_LABEL[a]}</button>)}
          </div>
        </Section>

        <Section title="Fit">
          <span className="fp-lbl">Score between</span>
          <div className="fp-range">
            <input className="in fp-num" type="number" min="0" max="100" value={f.min} onChange={e => num('min', e.target.value, 100)} placeholder="0" aria-label="Minimum fit" />
            <span>and</span>
            <input className="in fp-num" type="number" min="0" max="100" value={f.max} onChange={e => num('max', e.target.value, 100)} placeholder="100" aria-label="Maximum fit" />
          </div>
          <span className="fp-lbl">Scored by</span>
          <Toggles name="Scored by" values={KINDS} value={f.kind} onChange={v => set('kind', v)} />
          <span className="fp-lbl">Seniority (from the title)</span>
          <Toggles name="Seniority" values={LEVELS} value={f.level} onChange={v => set('level', v)} />
        </Section>

        <Section title="Company & source">
          <label className="fp-lbl" htmlFor="fp-co">Only these companies</label>
          <input id="fp-co" className="in" value={f.co} onChange={e => set('co', e.target.value)} placeholder="comma-separated, e.g. Postman, Razorpay" />
          <label className="fp-lbl" htmlFor="fp-xco">Never these companies</label>
          <input id="fp-xco" className="in" value={f.xco} onChange={e => set('xco', e.target.value)} placeholder="comma-separated" />
          {sources.length > 0 && <><span className="fp-lbl">Found on</span><Toggles name="Found on" values={sources} value={f.source} onChange={v => set('source', v)} labels={{}} /></>}
          <span className="fp-lbl">Applies on</span>
          <Toggles name="Applies on" values={[...ats, 'none']} value={f.ats} onChange={v => set('ats', v)} labels={{ none: 'The board itself' }} />
          <label className="fp-check"><input type="checkbox" checked={!!f.form} onChange={e => set('form', e.target.checked ? '1' : '')} /> Only jobs with a company form (no job-board login)</label>
        </Section>
      </div>
      <footer className="fp-foot">
        <button className="btn ghost" onClick={onClear}>Clear all</button>
        <button className="btn primary" onClick={onClose} data-fp-count={count}>Show {count} of {total} job{total === 1 ? '' : 's'}</button>
      </footer>
    </motion.aside>
  )
}
