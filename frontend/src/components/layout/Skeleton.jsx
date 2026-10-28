// frontend/src/components/layout/Skeleton.jsx
//
// Shimmering placeholders shown while a page's data loads, in the shape of what is coming
// (rows, a card, a strip of numbers) instead of a "Loading…" line.
'use client'

export function SkeletonRows({ n = 6, label = 'Loading' }) {
  return (
    <div className="skel-rows" role="status" aria-label={label}>
      {Array.from({ length: n }, (_, i) => (
        <div key={i} className="skel-row" style={{ opacity: 1 - i * (0.7 / n) }}>
          <span className="skel" style={{ width: '2.75rem' }} />
          <span className="skel-col"><span className="skel" style={{ width: `${55 + ((i * 17) % 30)}%` }} /><span className="skel" style={{ width: `${30 + ((i * 11) % 20)}%`, height: '.75em' }} /></span>
        </div>))}
    </div>
  )
}

export function SkeletonCard({ lines = 4, label = 'Loading' }) {
  return (
    <div className="card section skel-card" role="status" aria-label={label}>
      <span className="skel" style={{ width: '35%', height: '1.25em' }} />
      {Array.from({ length: lines }, (_, i) => <span key={i} className="skel" style={{ width: `${90 - i * 12}%` }} />)}
    </div>
  )
}
