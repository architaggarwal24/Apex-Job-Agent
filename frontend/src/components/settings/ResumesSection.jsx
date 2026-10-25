// frontend/src/components/settings/ResumesSection.jsx
//
// Resume variants (/api/resumes): upload a PDF with a label and tags, make one the default,
// delete one. The kit picks one per application (applier/kit.py). Each account manages its own.
'use client'
import { useCallback, useEffect, useRef, useState } from 'react'
import { api, send } from '@/lib/api'
import { Msg, Section } from './AutomationParts'

const kb = n => (n >= 1024 * 1024 ? `${(n / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(n / 1024))} KB`)

export default function ResumesSection() {
  const [rows, setRows] = useState(null)
  const [label, setLabel] = useState('')
  const [tags, setTags] = useState('')
  const [makeDefault, setMakeDefault] = useState(false)
  const [busy, setBusy] = useState(false)
  const [m, setM] = useState(null)
  const file = useRef(null)
  const load = useCallback(() => api('/api/resumes').then(r => setRows(r.resumes)).catch(e => setM({ ok: false, text: e.message })), [])
  useEffect(() => { load() }, [load])

  const upload = async () => {
    const f = file.current?.files?.[0]
    if (!f) return
    const fd = new FormData()
    fd.append('file', f); fd.append('label', label); fd.append('tags', tags); fd.append('make_default', String(makeDefault))
    setBusy(true); setM(null)
    try { await api('/api/resumes', { method: 'POST', body: fd }); setM({ ok: true, text: `Uploaded "${label}"` }); setLabel(''); setTags(''); setMakeDefault(false); file.current.value = ''; load() }
    catch (e) { setM({ ok: false, text: e.message }) }
    setBusy(false)
  }
  const act = async (fn, ok) => { setM(null); try { await fn(); setM({ ok: true, text: ok }); load() } catch (e) { setM({ ok: false, text: e.message }) } }

  return (
    <Section id="resumes" title="Resume variants" ownerOnly={false}
             hint="Different versions of your resume. For each application the kit picks the one that fits the job best; the default is used when it can't choose. Your own data.">
      {rows === null ? <div className="m">Loading…</div> : rows.length === 0 ? <div className="m">No resume variants yet: the single resume.pdf is used.</div> : (
        <table className="auto-table" aria-label="Resume variants">
          <thead><tr><th>Label</th><th>Tags</th><th>File</th><th /></tr></thead>
          <tbody>{rows.map(r => (
            <tr key={r.id} data-resume={r.id} style={{ cursor: 'default' }}>
              <td><b>{r.label}</b> {r.is_default && <span className="badge b-done">default</span>}</td>
              <td className="m">{r.tags.join(', ') || '—'}</td>
              <td className="m">{r.filename} · {kb(r.size)}</td>
              <td><div className="row" style={{ justifyContent: 'flex-end' }}>
                {!r.is_default && <button className="btn sm" onClick={() => act(() => send(`/api/resumes/${r.id}/default`, 'PUT'), `"${r.label}" is the default`)}>Make default</button>}
                <button className="btn sm danger" onClick={() => { if (confirm(`Delete "${r.label}"?`)) act(() => send(`/api/resumes/${r.id}`, 'DELETE'), 'Deleted') }}>Delete</button>
              </div></td>
            </tr>))}</tbody>
        </table>)}
      <div className="rule new-rule" data-new-resume>
        <b>Upload a variant (PDF, up to 5 MB)</b>
        <div className="grid2" style={{ marginTop: '.5rem' }}>
          <input className="in" placeholder="Label, e.g. Backend" value={label} onChange={e => setLabel(e.target.value)} aria-label="Resume label" />
          <input className="in" placeholder="Tags, comma-separated" value={tags} onChange={e => setTags(e.target.value)} aria-label="Resume tags" />
          <input ref={file} className="in" type="file" accept="application/pdf" aria-label="Resume file" />
          <label className="row chk"><input type="checkbox" checked={makeDefault} onChange={e => setMakeDefault(e.target.checked)} /> make it the default</label>
        </div>
        <div className="row auto-save"><button className="btn" disabled={busy || !label.trim()} onClick={upload}>{busy ? 'Uploading…' : 'Upload'}</button><Msg m={m} /></div>
      </div>
    </Section>
  )
}
