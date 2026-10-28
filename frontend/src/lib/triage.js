// frontend/src/lib/triage.js
//
// Instant triage with Undo, as plain functions (no React, so tests run them under Node):
//   * the jobs move on screen the moment you act (withStages); the server is asked after,
//     and only what it refused is put back (revertRefused)
//   * every queue / skip can be undone from its toast (undoFor -> what to call)
//   * the deck's "about N min left" comes from your own pace (pace / minutesLeft)

export const QUEUEABLE = ['found', 'skipped', 'failed']          // stages a job can be queued from
export const SKIPPABLE = ['found', 'queued', 'needs_you']             // failed can only be re-queued (db/lifecycle.py)

// `apps` with every id in `ids` moved to stage `to` (a new array; untouched rows are the same objects).
export function withStages(apps, ids, to, now = new Date()) {
  const set = new Set(ids)
  return apps.map(a => (set.has(a.job_id) ? { ...a, stage: to, stage_entered_at: now.toISOString() } : a))
}

// Put the jobs the server refused back where they were (`before`: [{job_id, stage}]).
export function revertRefused(apps, before, refusedIds) {
  const was = new Map(before.map(b => [b.job_id, b.stage]))
  const set = new Set(refusedIds)
  return apps.map(a => (set.has(a.job_id) && was.has(a.job_id) ? { ...a, stage: was.get(a.job_id) } : a))
}

// What Undo calls for an action that went through. `before`: the jobs' stages before it.
//   queue -> {unqueue: [ids]}       POST /api/applications/unqueue (back to the Inbox)
//   skip  -> {restore: [{id, to}]}  POST /api/applications/{id}/transition, only for jobs that
//            were in the Inbox or the queue (skipped -> found | queued are legal moves)
// null when nothing can be undone.
export function undoFor(kind, before, doneIds) {
  const done = new Set(doneIds)
  const rows = before.filter(b => done.has(b.job_id))
  if (kind === 'queue') return rows.length ? { unqueue: rows.map(b => b.job_id) } : null
  if (kind === 'skip') {
    const restore = rows.filter(b => b.stage === 'found' || b.stage === 'queued').map(b => ({ id: b.job_id, to: b.stage }))
    return restore.length ? { restore } : null
  }
  return null
}

// Seconds per decision from the times (ms) of your last decisions: the median gap of the
// last 10, ignoring breaks over 5 minutes. null until there are two decisions.
export function pace(times) {
  const gaps = []
  for (let i = Math.max(1, times.length - 10); i < times.length; i += 1) {
    const g = (times[i] - times[i - 1]) / 1000
    if (g > 0 && g <= 300) gaps.push(g)
  }
  if (!gaps.length) return null
  gaps.sort((a, b) => a - b)
  return gaps[Math.floor(gaps.length / 2)]
}

export function minutesLeft(remaining, secsPerJob) {
  if (!secsPerJob || remaining <= 0) return null
  return Math.max(1, Math.round((remaining * secsPerJob) / 60))
}

// The jobs that arrived since `known` (job ids seen before) among `rows` (the Inbox).
export function arrivals(rows, known) {
  return known ? rows.filter(a => !known.has(a.job_id)).map(a => a.job_id) : []
}

// A deck order: the Inbox rows best first, with the ones put off ("Later") at the end in
// the order they were put off, and the ones just brought back by Undo on top (latest first).
// `keep`: the card on screen stays on top even when a better job streams in mid-search.
export function deckOrder(rows, later, front = [], keep = '') {
  const back = new Map(later.map((id, i) => [id, i]))
  const top = new Map(front.map((id, i) => [id, i]))
  const held = keep && !top.has(keep) && !back.has(keep) ? rows.find(a => a.job_id === keep) : null
  const first = [...rows.filter(a => top.has(a.job_id)).sort((a, b) => top.get(b.job_id) - top.get(a.job_id)), ...(held ? [held] : [])]
  const mid = rows.filter(a => !back.has(a.job_id) && !top.has(a.job_id) && a !== held)
  const end = rows.filter(a => back.has(a.job_id) && !top.has(a.job_id)).sort((a, b) => back.get(a.job_id) - back.get(b.job_id))
  return [...first, ...mid, ...end]
}
