// frontend/src/app/profile/page.jsx
'use client'
import { useEffect, useRef, useState } from 'react'
import { api } from '@/lib/api'

const CATEGORY_LABELS = { personal: 'Personal', professional: 'Professional', education: 'Education', identity: 'Identity' }
const CATEGORY_ORDER  = ['personal', 'professional', 'education', 'identity']

// The DB stores data_type per field ('text','email','phone','date','number',
// 'url','textarea','select') but not select OPTIONS — those are UI-only
// presentation info, so they're defined here, matching the old
// templates/profile.html exactly (same field keys, same choices).
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

export default function Profile() {
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

  const setField = (key, value) => setDirty(d => ({ ...d, [key]: value }))

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
        setResumeMsg(`✓ Parsed "${file.name}" — ${j.name || 'name'}, ${(j.skills || []).length} skills found. Fields below were overwritten — review before saving.`)
        setResumeExists(true); load()
      } else setResumeMsg('✗ ' + (j.error || 'Upload failed — check the PDF and try again'))
    } catch (e) { setResumeMsg('✗ Upload failed: ' + e.message) }
    if (fileInput.current) fileInput.current.value = ''
  }

  if (!byCategory) return <><h1>Profile</h1><p className="sub">Loading…</p></>

  return (
    <>
      <h1>Profile</h1>
      <p className="sub">This data is what gets used to fill out job application forms.</p>

      <div className="card section" style={{ marginBottom: 20 }}>
        <div className="row">
          <input ref={fileInput} type="file" accept="application/pdf" style={{ display: 'none' }}
            onChange={e => uploadResume(e.target.files[0])} />
          <button className="btn" onClick={() => fileInput.current.click()}>📤 Upload New Resume</button>
          {resumeExists === false && !resumeMsg && <span className="m" style={{ color: 'var(--ylw)' }}>⚠ No resume on file yet</span>}
          {resumeMsg && <span className="m">{resumeMsg}</span>}
        </div>
      </div>

      {CATEGORY_ORDER.filter(c => byCategory[c]?.length).map(cat => (
        <div key={cat} className="card section">
          <h2 style={{ padding: 0, marginBottom: 16 }}>{CATEGORY_LABELS[cat]}</h2>
          <div className="grid2">
            {byCategory[cat].map(f => (
              <div className="fg" key={f.key}>
                <label>{f.label}</label>
                {f.data_type === 'select' ? (
                  <select className="in" value={fieldValue(cat, f.key)} onChange={e => setField(f.key, e.target.value)}>
                    {(SELECT_OPTIONS[f.key] || ['']).map(o => <option key={o} value={o}>{o || '—'}</option>)}
                  </select>
                ) : f.data_type === 'textarea' ? (
                  <textarea className="in" value={fieldValue(cat, f.key)} onChange={e => setField(f.key, e.target.value)} />
                ) : (
                  <input className="in" type={HTML_TYPE[f.data_type] || 'text'}
                    value={fieldValue(cat, f.key)} onChange={e => setField(f.key, e.target.value)} />
                )}
              </div>
            ))}
          </div>
        </div>
      ))}

      <div className="save-bar">
        {saveMsg && <span className="m" style={{ marginRight: 14, alignSelf: 'center' }}>{saveMsg}</span>}
        <button className="btn primary"
          disabled={saving || Object.keys(dirty).length === 0} onClick={save}>
          {saving ? 'Saving…' : `Save changes${Object.keys(dirty).length ? ` (${Object.keys(dirty).length})` : ''}`}
        </button>
      </div>
    </>
  )
}
