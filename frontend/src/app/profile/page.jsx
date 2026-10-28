// frontend/src/app/profile/page.jsx
//
// Profile: the answers the agent fills forms with. One section per tab (Personal,
// Professional, Education, Identity), the open tab in the URL (?t=education). Edits in every
// tab are kept until one Save (POST /api/profile); a tab with unsaved edits shows a dot.
'use client'
import { Suspense, useEffect, useRef, useState } from 'react'
import { usePathname, useRouter, useSearchParams } from 'next/navigation'
import { FileText, TriangleAlert, Upload } from 'lucide-react'
import { api } from '@/lib/api'
import { SkeletonCard } from '@/components/layout/Skeleton'

const CATEGORY_LABELS = { personal: 'Personal', professional: 'Professional', education: 'Education', identity: 'Identity' }
const CATEGORY_ORDER  = ['personal', 'professional', 'education', 'identity']

// The DB stores data_type per field ('text','email','phone','date','number',
// 'url','textarea','select') but not select OPTIONS — those are UI-only
// presentation info, so they're defined here (keyed by the DB's field keys).
const SELECT_OPTIONS = {
  gender: ['', 'Male', 'Female', 'Non-binary', 'Prefer not to say'],
  willing_to_relocate: ['Yes', 'No', 'Maybe'],
  work_mode_preference: ['Hybrid', 'Remote', 'Onsite', 'Any'],
  authorized_to_work: ['Yes', 'No'],
  require_sponsorship: ['No', 'Yes'],
  veteran_status: ['No', 'Yes', 'Prefer not to say'],
  disability_status: ['No', 'Yes', 'Prefer not to say'],
}
const HTML_TYPE = { email: 'email', phone: 'tel', date: 'text', number: 'number', url: 'url' }

// Links where only the user name varies: the page shows the fixed part and asks for
// the rest. Whatever is typed, the server stores the canonical URL (db.profile_db).
const PREFIXED = {
  github_url:   { label: 'GitHub', prefix: 'github.com/', hosts: ['github.com/'] },
  linkedin_url: { label: 'LinkedIn', prefix: 'linkedin.com/in/', hosts: ['linkedin.com/in/', 'linkedin.com/pub/'] },
}
const username = (key, value) => {
  let v = (value || '').trim()
  const low = v.toLowerCase()
  for (const h of PREFIXED[key].hosts) { const i = low.indexOf(h); if (i >= 0) { v = v.slice(i + h.length); break } }
  return v.replace(/^\/+|\/+$/g, '').split('/')[0].split('?')[0].replace(/^@/, '')
}

function Field({ f, value, set }) {
  const id = `pf-${f.key}`
  return (
    <div className="fg">
      <label htmlFor={id}>{PREFIXED[f.key]?.label || f.label}</label>
      {PREFIXED[f.key] ? (
        <div className="in-prefix">
          <span aria-hidden="true">{PREFIXED[f.key].prefix}</span>
          <input id={id} className="in" type="text" autoComplete="off" spellCheck={false}
            placeholder="username" value={username(f.key, value)} onChange={e => set(username(f.key, e.target.value))} />
        </div>
      ) : f.data_type === 'select' ? (
        <select id={id} className="in" value={value} onChange={e => set(e.target.value)}>
          {(SELECT_OPTIONS[f.key] || ['']).map(o => <option key={o} value={o}>{o || '—'}</option>)}
        </select>
      ) : f.data_type === 'textarea' ? (
        <textarea id={id} className="in" value={value} onChange={e => set(e.target.value)} />
      ) : (
        <input id={id} className="in" type={HTML_TYPE[f.data_type] || 'text'} value={value} onChange={e => set(e.target.value)} />
      )}
    </div>
  )
}

function ProfilePage() {
  const params = useSearchParams()
  const router = useRouter()
  const pathname = usePathname()
  const [byCategory, setByCategory] = useState(null)
  const [dirty, setDirty] = useState({})
  const [saving, setSaving] = useState(false)
  const [saveMsg, setSaveMsg] = useState('')
  const [resumeMsg, setResumeMsg] = useState('')
  const [resumeExists, setResumeExists] = useState(null)
  const fileInput = useRef(null)

  const load = () => api('/api/profile').then(setByCategory).catch(() => {})
  useEffect(() => {
    load()
    api('/api/status').then(j => setResumeExists(j.resume_exists)).catch(() => {})
  }, [])

  const fieldValue = (cat, key) =>
    dirty[key] ?? byCategory?.[cat]?.find(f => f.key === key)?.value ?? ''

  const setField = (key, value) => { setDirty(d => ({ ...d, [key]: value })); setSaveMsg('') }

  const save = async () => {
    if (Object.keys(dirty).length === 0) return
    setSaving(true); setSaveMsg('')
    try {
      const r = await api('/api/profile', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(dirty) })
      setSaveMsg(`✓ Saved ${r.updated} field${r.updated === 1 ? '' : 's'}`)
      setDirty({}); load()
    } catch (e) { setSaveMsg('✗ ' + e.message) }
    setSaving(false)
  }

  const uploadResume = async (file) => {
    if (!file) return
    if (!file.name.toLowerCase().endsWith('.pdf')) { setResumeMsg('Only PDF files are accepted'); return }
    setResumeMsg('Uploading & parsing…')
    const fd = new FormData(); fd.append('file', file)
    try {
      const r = await fetch('/api/setup/upload-resume', { method: 'POST', body: fd })
      const j = await r.json()
      if (r.ok && j.status === 'ok') {
        setResumeMsg(`✓ Parsed "${file.name}" — ${j.name || 'name'}, ${(j.skills || []).length} skills found. Fields were overwritten — review every tab before saving.`)
        setResumeExists(true); load()
      } else setResumeMsg('✗ ' + (j.error || 'Upload failed — check the PDF and try again'))
    } catch (e) { setResumeMsg('✗ Upload failed: ' + e.message) }
    if (fileInput.current) fileInput.current.value = ''
  }

  const cats = byCategory ? CATEGORY_ORDER.filter(c => byCategory[c]?.length) : []
  const tab = cats.includes(params.get('t')) ? params.get('t') : cats[0]
  const open = t => router.replace(`${pathname}?t=${t}`, { scroll: false })
  // "To fill": empty fields, not counting the ones marked optional.
  const empty = cat => byCategory[cat].filter(f => !String(fieldValue(cat, f.key)).trim() && !/optional/i.test(f.label)).length
  const unsaved = cat => byCategory[cat].some(f => f.key in dirty)
  const n = Object.keys(dirty).length

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Profile</h1>
          <p className="sub">What the agent writes into application forms.</p>
        </div>
        <input ref={fileInput} type="file" accept="application/pdf" style={{ display: 'none' }} aria-label="Resume PDF"
          onChange={e => uploadResume(e.target.files[0])} />
        <button className="btn" onClick={() => fileInput.current.click()}><Upload className="lucide" />Upload new resume</button>
      </div>

      <div className={`pf-resume ${resumeExists === false ? 'missing' : ''}`} data-resume-state={resumeExists === null ? '' : resumeExists ? 'on-file' : 'missing'}>
        {resumeExists === false ? <TriangleAlert className="lucide" aria-hidden="true" /> : <FileText className="lucide" aria-hidden="true" />}
        {resumeMsg ? <span>{resumeMsg}</span>
          : resumeExists === false ? <span>No resume on file yet. Upload a PDF and these fields are filled from it.</span>
          : resumeExists ? <span>Resume on file. Uploading a new one re-reads it and overwrites these fields.</span>
          : <span className="m">Checking for a resume…</span>}
      </div>

      {!byCategory ? <SkeletonCard lines={7} label="Loading your profile" /> : <>
        <nav className="tabs pf-tabs" role="tablist" aria-label="Profile sections">
          {cats.map(c => {
            const left = empty(c)
            return (
              <button key={c} role="tab" aria-selected={tab === c} className={`tab ${tab === c ? 'on' : ''}`} onClick={() => open(c)} data-tab={c}>
                {CATEGORY_LABELS[c]}
                {left > 0 && <span className="n" title={`${left} field${left === 1 ? '' : 's'} to fill`}>{left}</span>}
                {unsaved(c) && <span className="dot y" title="Unsaved changes" />}
              </button>)
          })}
        </nav>
        <section className="card section" role="tabpanel" aria-label={CATEGORY_LABELS[tab]} data-category={tab}>
          <div className="grid2">
            {byCategory[tab].map(f => <Field key={f.key} f={f} value={fieldValue(tab, f.key)} set={v => setField(f.key, v)} />)}
          </div>
        </section>
      </>}

      <div className="save-bar">
        {saveMsg ? <span className="m">{saveMsg}</span>
          : n > 0 && <span className="m">{n} unsaved change{n === 1 ? '' : 's'}{cats.filter(unsaved).length > 1 ? ` across ${cats.filter(unsaved).length} tabs` : ''}</span>}
        <button className="btn primary" disabled={saving || n === 0} onClick={save}>
          {saving ? 'Saving…' : `Save changes${n ? ` (${n})` : ''}`}
        </button>
      </div>
    </>
  )
}

// useSearchParams needs a Suspense boundary in a static export.
export default function Profile() {
  return <Suspense fallback={null}><ProfilePage /></Suspense>
}
