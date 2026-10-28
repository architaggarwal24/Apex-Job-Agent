// frontend/src/lib/palette.js
//
// The command palette's matching, as plain functions (no React, so tests run them under Node):
//   rank(items, q)     which commands / pages / jobs fit what was typed, best first
//   parseQueue(q)      "queue ai engineer 80" -> {terms: ["ai", "engineer"], min: 80}
//   matchJobs(apps, …) the Inbox jobs such a command would queue
//
// An item is {id, label, group, keywords?}. Every word typed must be found at the start of a
// word in the label or keywords; labels that start with the text rank first.

const words = s => String(s || '').toLowerCase().split(/[^a-z0-9+#.]+/).filter(Boolean)

export function score(item, q) {
  const terms = words(q)
  if (!terms.length) return 1
  const label = String(item.label || '').toLowerCase()
  const hay = words(`${item.label} ${item.keywords || ''}`)
  let s = 0
  for (const t of terms) {
    const exact = hay.includes(t)
    const prefix = hay.some(w => w.startsWith(t))
    if (!prefix) return null
    s += exact ? 3 : 2
  }
  if (label.startsWith(String(q).trim().toLowerCase())) s += 5
  return s
}

// Items that match, best first; ties keep their given order (the groups' order).
export function rank(items, q, limit = 50) {
  return items
    .map((it, i) => ({ it, i, s: score(it, q) }))
    .filter(x => x.s !== null)
    .sort((a, b) => b.s - a.s || a.i - b.i)
    .slice(0, limit)
    .map(x => x.it)
}

// "queue <words> [<min fit>]" (also "q <words>"): the words to match and the lowest fit
// to queue (default 80, clamped 0-100). null when the text is not a queue command.
export function parseQueue(q) {
  const m = /^\s*(?:queue|q)\s+(.+)$/i.exec(q || '')
  if (!m) return null
  const parts = m[1].trim().split(/\s+/)
  let min = 80
  const last = parts[parts.length - 1].replace(/^(?:≥|>=|fit)?/i, '').replace(/\+$/, '')
  if (/^\d{1,3}$/.test(last)) { min = Math.max(0, Math.min(100, Number(last))); parts.pop() }
  const terms = parts.map(p => p.toLowerCase()).filter(p => p && !['fit', '≥', '>='].includes(p))
  return terms.length ? { terms, min } : null
}

// Inbox jobs whose title or company has a word starting with every term ("ai" finds
// "AI Engineer", not "Retail") and whose fit (AI score, else quick
// match) is at least `min`, best first.
export function matchJobs(apps, terms, min) {
  const fit = a => (a.fit !== undefined ? a.fit : a.ai_score)
  return apps
    .filter(a => a.stage === 'found' && fit(a) != null && fit(a) >= min)
    .filter(a => { const hay = words(`${a.title} ${a.company}`); return terms.every(t => hay.some(w => w.startsWith(t))) })
    .sort((a, b) => fit(b) - fit(a))
}
