// frontend/src/components/settings/ThemePreview.jsx
'use client'

// A miniature of the real UI drawn from the theme's own tokens (type, radius,
// border weight, shadow), so the gallery shows what each theme actually feels
// like — not just three colour chips.
export default function ThemePreview({ t }) {
  const line = (w, c, h = 5) => <div style={{ width: w, height: h, borderRadius: Math.min(t.radius, 3), background: c, opacity: c === t.mut ? 0.7 : 1 }} />
  return (
    <div style={{
      display: 'flex', height: 92, overflow: 'hidden', border: `${t.bw}px solid ${t.bdr}`, borderRadius: Math.min(t.radius, 14),
      background: t.mesh ? `${t.mesh}, ${t.bg}` : t.bg, fontFamily: t.font,
    }}>
      <div style={{ width: 26, background: t.bg2, borderRight: `${t.bw}px solid ${t.bdr}`, padding: '8px 5px', display: 'grid', alignContent: 'start', gap: 5 }}>
        {line('100%', t.acc, 4)}{line('100%', t.mut, 4)}{line('100%', t.mut, 4)}
      </div>
      <div style={{ flex: 1, padding: 8, display: 'grid', alignContent: 'start', gap: 6 }}>
        <div style={{ fontFamily: t.head, fontWeight: t.headWeight, fontSize: 11, color: t.txt, lineHeight: 1 }}>Dashboard</div>
        <div style={{ background: t.panel, border: `${t.bw}px solid ${t.bdr}`, borderRadius: Math.min(t.radius, 10), boxShadow: t.shadow,
          padding: '6px 7px', display: 'flex', alignItems: 'center', gap: 7 }}>
          <div style={{ fontFamily: t.head, fontWeight: t.headWeight, fontSize: 17, color: t.txt, lineHeight: 1 }}>Aa</div>
          <div style={{ display: 'grid', gap: 4, flex: 1 }}>{line('80%', t.mut)}{line('55%', t.mut)}</div>
          <div style={{ width: 22, height: 11, borderRadius: Math.min(t.radius, 6), background: t.acc, border: `${t.bw}px solid ${t.bdr === t.acc ? t.acc : t.bdr}` }} />
        </div>
      </div>
    </div>
  )
}
