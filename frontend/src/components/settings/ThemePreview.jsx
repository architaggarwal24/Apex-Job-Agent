// frontend/src/components/settings/ThemePreview.jsx
'use client'

// A miniature of the real Applications screen in one variant's colours: sidebar, a few
// list rows with scores, the detail pane and its primary button.
export default function ThemePreview({ t }) {
  const line = (w, c, h = 5) => <div style={{ width: w, height: h, borderRadius: 2, background: c, opacity: c === t.mut ? 0.75 : 1 }} />
  return (
    <div style={{ display: 'flex', height: 96, overflow: 'hidden', border: `1px solid ${t.bdr}`, borderRadius: 8, background: t.bg }} aria-hidden="true">
      <div style={{ width: 28, background: t.bg2, borderRight: `1px solid ${t.bdr}`, padding: '8px 6px', display: 'grid', alignContent: 'start', gap: 6 }}>
        {line('100%', t.txt, 4)}{line('100%', t.mut, 4)}{line('100%', t.mut, 4)}
      </div>
      <div style={{ flex: 1, display: 'grid', alignContent: 'start' }}>
        {[86, 74, 51].map((n, i) => (
          <div key={n} style={{ display: 'flex', gap: 6, alignItems: 'center', padding: '7px 8px', borderBottom: `1px solid ${t.bdr}`, background: i === 0 ? t.panel : 'transparent' }}>
            <span style={{ fontSize: 9, fontWeight: 700, color: i === 2 ? t.mut : t.acc, fontFamily: 'var(--font-mono)' }}>{n}</span>
            <div style={{ display: 'grid', gap: 3, flex: 1 }}>{line('80%', t.txt, 4)}{line('50%', t.mut, 3)}</div>
          </div>))}
      </div>
      <div style={{ width: 54, background: t.panel, borderLeft: `1px solid ${t.bdr}`, padding: 7, display: 'grid', alignContent: 'start', gap: 5 }}>
        {line('90%', t.txt, 5)}{line('60%', t.mut, 3)}
        <div style={{ width: 26, height: 10, borderRadius: 3, background: t.acc, marginTop: 2 }} />
      </div>
    </div>
  )
}
