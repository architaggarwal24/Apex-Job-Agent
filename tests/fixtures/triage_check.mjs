// tests/fixtures/triage_check.mjs
//
// Run by tests/test_applications_ui.py under Node, next to a copy of frontend/src/lib/triage.js.
// Prints one JSON object of results; the Python test asserts on it.
import { QUEUEABLE, SKIPPABLE, arrivals, deckOrder, minutesLeft, pace, revertRefused, undoFor, withStages } from './triage.mjs'

const apps = [{ job_id: 'a', stage: 'found' }, { job_id: 'b', stage: 'found' }, { job_id: 'c', stage: 'needs_you' }, { job_id: 'd', stage: 'queued' }]
const moved = withStages(apps, ['a', 'c'], 'skipped', new Date('2026-10-28T10:00:00Z'))
const before = apps.map(({ job_id, stage }) => ({ job_id, stage }))
const back = revertRefused(moved, before, ['c'])
console.log(JSON.stringify({
  moved: moved.map(a => a.stage), moved_at: moved[0].stage_entered_at, same_object: moved[1] === apps[1], input_untouched: apps[0].stage,
  reverted: back.map(a => a.stage),
  undo_queue: undoFor('queue', before, ['a', 'b']),
  undo_queue_none: undoFor('queue', before, []),
  undo_skip: undoFor('skip', before, ['a', 'c', 'd']),
  undo_skip_none: undoFor('skip', before, ['c']),
  undo_other: undoFor('move', before, ['a']),
  pace_none: pace([]), pace_one: pace([1000]),
  pace: pace([0, 10000, 22000, 30000, 1000000, 1012000]),    // gaps 10, 12, 8, (break), 12 -> median 12
  left: minutesLeft(30, 12), left_small: minutesLeft(1, 5), left_none: minutesLeft(5, null), left_done: minutesLeft(0, 10),
  deck: deckOrder([{ job_id: 'a' }, { job_id: 'b' }, { job_id: 'c' }, { job_id: 'd' }], ['c', 'a']).map(a => a.job_id),
  deck_gone: deckOrder([{ job_id: 'b' }], ['x']).map(a => a.job_id),
  deck_front: deckOrder([{ job_id: 'a' }, { job_id: 'b' }, { job_id: 'c' }, { job_id: 'd' }], ['c', 'b'], ['b', 'd']).map(a => a.job_id),
  deck_keep: deckOrder([{ job_id: 'new', fit: 99 }, { job_id: 'a' }, { job_id: 'b' }], [], [], 'a').map(a => a.job_id),
  deck_keep_gone: deckOrder([{ job_id: 'b' }], [], [], 'a').map(a => a.job_id),
  deck_keep_undo: deckOrder([{ job_id: 'a' }, { job_id: 'b' }, { job_id: 'c' }], [], ['c'], 'b').map(a => a.job_id),
  arrived: arrivals([{ job_id: 'a' }, { job_id: 'n1' }, { job_id: 'n2' }], new Set(['a', 'gone'])),
  arrived_first_load: arrivals([{ job_id: 'a' }], null),
  queueable: QUEUEABLE, skippable: SKIPPABLE,
}))
