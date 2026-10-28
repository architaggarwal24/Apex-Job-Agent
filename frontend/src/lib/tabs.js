// frontend/src/lib/tabs.js
//
// The Applications workspace's tabs: each is a set of lifecycle stages. The URL keeps
// speaking stages (?stage=a,b, ?needs_you=1 - lib/filters.js), and the tab shown is the
// one those stages belong to; no stage at all is the Inbox (new jobs to triage).
// Plain JS with no React, so tests can run it under Node.
import { POST_SUBMISSION } from '@/lib/stages'

export const TABS = [
  { key: 'inbox',    label: 'Inbox',       stages: ['found'] },
  { key: 'queued',   label: 'Queued',      stages: ['queued'] },
  { key: 'working',  label: 'In progress', stages: ['preparing'] },
  { key: 'needs',    label: 'Needs you',   stages: ['needs_you'] },
  { key: 'review',   label: 'Review',      stages: ['ready_for_review'] },
  { key: 'applied',  label: 'Applied',     stages: POST_SUBMISSION },
  { key: 'archive',  label: 'Archive',     stages: ['skipped', 'failed'] },
]

// The tab for a filter's stages: an exact or narrower set picks its tab; anything that
// spans tabs is "custom" (the list then shows exactly those stages).
export function tabFor(stages) {
  if (!stages || !stages.length) return 'inbox'
  const t = TABS.find(t => stages.every(s => t.stages.includes(s)))
  return t ? t.key : 'custom'
}

// The stages a tab stands for, in the form the URL keeps: the Inbox is "no stage".
export function stagesForTab(key) {
  if (key === 'inbox') return []
  const t = TABS.find(t => t.key === key)
  return t ? [...t.stages] : []
}

// The stages actually shown for a filter (the Inbox's "no stage" is found).
export const shownStages = stages => (stages && stages.length ? stages : ['found'])

// Is this tab narrower than the whole tab (e.g. Applied filtered to two stages)?
export function isNarrowed(stages) {
  const key = tabFor(stages)
  const t = TABS.find(t => t.key === key)
  return !!t && stages.length > 0 && stages.length < t.stages.length
}
