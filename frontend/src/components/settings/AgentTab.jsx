// frontend/src/components/settings/AgentTab.jsx
'use client'

function Toggle({ label, hint, checked, onChange }) {
  return (
    <div className="row" style={{ marginBottom: 16, alignItems: 'flex-start', flexWrap: 'nowrap' }}>
      <div className={`toggle-sw ${checked ? 'on' : ''}`} role="switch" aria-checked={checked} tabIndex={0}
        onClick={() => onChange(!checked)} onKeyDown={e => (e.key === ' ' || e.key === 'Enter') && (e.preventDefault(), onChange(!checked))}><i /></div>
      <div><div style={{ fontWeight: 650, fontSize: '.875rem' }}>{label}</div><div className="m">{hint}</div></div>
    </div>
  )
}

function NumberField({ label, k, val, setVal, step, min, hint }) {
  return (
    <div className="fg"><label>{label}</label>
      <input className="in" type="number" step={step} min={min} value={val(k)} onChange={e => setVal(k, e.target.value)} />
      {hint && <div className="m" style={{ marginTop: 6 }}>{hint}</div>}</div>
  )
}

export default function AgentTab({ val, setVal }) {
  return (
    <div className="card section">
      <Toggle label="Dry Run Mode" hint="Fill forms but never actually submit — for safely testing the whole flow."
        checked={val('dry_run') === 'true'} onChange={v => setVal('dry_run', String(v))} />
      <Toggle label="Enable AI Job Scoring" hint="Use the LLM to score and rank jobs. Uses more API calls."
        checked={val('enable_llm_scoring') === 'true'} onChange={v => setVal('enable_llm_scoring', String(v))} />
      <div className="grid2" style={{ marginTop: 18 }}>
        <NumberField label="Min Delay Between Applies (s)" k="min_delay_between_applies" val={val} setVal={setVal} />
        <NumberField label="Max Delay Between Applies (s)" k="max_delay_between_applies" val={val} setVal={setVal} />
      </div>
      <div className="m" style={{ marginTop: 6 }}>The daily limit, score threshold, company blacklist and minimum salary are under Applying → Limits &amp; filters.</div>
    </div>
  )
}
