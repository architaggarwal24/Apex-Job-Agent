// frontend/src/components/settings/AutomationParts.jsx
//
// Small building blocks shared by the Settings > Automation sections.
'use client'

// A card. Owner-only parts are wrapped in a <fieldset disabled> for other accounts, so every
// input and button inside is read-only at once (the server refuses those writes anyway).
export function Section({ id, title, hint, owner, ownerOnly = true, children }) {
  return (
    <section className="card section auto-sec" data-section={id}>
      <div className="row"><h2 style={{ padding: 0 }}>{title}</h2>
        {ownerOnly && !owner && <span className="badge" data-readonly>read-only — owner account only</span>}</div>
      {hint && <div className="m" style={{ margin: '.25rem 0 var(--sp2)' }}>{hint}</div>}
      <fieldset disabled={ownerOnly && !owner} className="auto-fs">{children}</fieldset>
    </section>
  )
}

export function Toggle({ label, hint, checked, onChange, name }) {
  return (
    <label className="row auto-toggle">
      <input type="checkbox" role="switch" checked={!!checked} onChange={e => onChange(e.target.checked)} aria-label={name || label} />
      <span><b>{label}</b>{hint && <span className="m"> — {hint}</span>}</span>
    </label>
  )
}

export function Msg({ m }) {
  return m ? <span className={m.ok ? 'msg-ok' : 'msg-err'} role="status">{m.text}</span> : null
}

