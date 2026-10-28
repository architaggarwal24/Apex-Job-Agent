// frontend/src/components/layout/CommandPalette.jsx
//
// Ctrl+K (⌘K on a Mac) from anywhere: type to jump to a page, an Applications tab, a Profile
// tab or a Settings section, to open any job by its title or company, or to run an action
// (search, quick search, cancel, score the Inbox, switch theme, sign out). "queue ai 80"
// queues every Inbox job matching "ai" with a fit of 80 or more. ↑ ↓ to move, Enter to run,
// Esc to close. Matching lives in lib/palette.js.
'use client'
import { useEffect, useMemo, useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import { AnimatePresence, motion } from 'motion/react'
import { ArrowRight, Briefcase, CornerDownLeft, Search, Zap } from 'lucide-react'
import { api, send } from '@/lib/api'
import { TABS, stagesForTab } from '@/lib/tabs'
import { SETTINGS_PLACES } from '@/lib/settingsNav'
import { stageLabel } from '@/lib/stages'
import { matchJobs, parseQueue, rank } from '@/lib/palette'
import { cancelSearch, refreshPipeline, startSearch, usePipeline } from '@/lib/pipeline'
import { toast } from '@/lib/toast'

const OWNER_ONLY = 'Only the owner account can do this.'
const tabHref = key => key === 'review' ? '/applications?ready_for_review=1&view=review'
  : key === 'inbox' ? '/applications' : `/applications?stage=${stagesForTab(key).join(',')}`

function places() {
  const go = (id, label, href, keywords = '') => ({ id, label, href, keywords, group: 'Go to' })
  return [
    go('p-today', 'Today', '/', 'home checklist follow-ups queue'),
    go('p-apps', 'Applications', '/applications', 'jobs inbox triage deck'),
    ...TABS.map(t => go(`p-tab-${t.key}`, `Applications › ${t.label}`, tabHref(t.key), 'jobs tab')),
    go('p-insights', 'Insights', '/insights', 'analytics outcomes rates calibration'),
    go('p-profile', 'Profile', '/profile', 'resume details'),
    ...['personal', 'professional', 'education', 'identity'].map(t => go(`p-pf-${t}`, `Profile › ${t[0].toUpperCase()}${t.slice(1)}`, `/profile?t=${t}`, 'profile')),
    go('p-settings', 'Settings', '/settings', 'preferences'),
    ...SETTINGS_PLACES.map(([href, label, kw]) => go(`p-s-${href}`, label, href, kw)),
  ]
}

export default function CommandPalette({ open, onClose, owner, theme, onTheme, onSignOut }) {
  const router = useRouter()
  const pipe = usePipeline()
  const [q, setQ] = useState('')
  const [at, setAt] = useState(0)
  const [apps, setApps] = useState(null)
  const [busy, setBusy] = useState(false)
  const input = useRef(null)
  const list = useRef(null)

  useEffect(() => {
    if (!open) return
    setQ(''); setAt(0)
    api('/api/applications?limit=2000').then(setApps).catch(() => setApps([]))
    setTimeout(() => input.current?.focus(), 0)
  }, [open])

  const actions = useMemo(() => {
    const act = (id, label, run, keywords = '', ownerOnly = false) => ({ id, label, run, keywords, ownerOnly, group: 'Actions' })
    const scored = async () => {
      const r = await send('/api/pipeline/score', 'POST')
      refreshPipeline()
      return `Scored ${r.quick} Inbox jobs by keywords${r.ai === 'started' ? `; the AI is scoring ${r.ai_total}` : ''}`
    }
    return [
      ...(pipe?.running ? [act('a-cancel', 'Cancel the running search', async () => { await cancelSearch(); return 'Cancel requested' }, 'stop', true)] : [
        act('a-run', 'Run a job search', async () => { await startSearch(false); return 'Search started — watch the bar at the top' }, 'start find new jobs', true),
        act('a-quick', 'Quick search (2 titles × 2 places)', async () => { await startSearch(true); return 'Quick search started' }, 'run fast test', true)]),
      act('a-score', 'Score the Inbox', scored, 'fit match rank rescore', true),
      act('a-theme', theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme', async () => { onTheme(theme === 'dark' ? 'light' : 'dark'); return '' }, 'appearance mode colour'),
      act('a-out', 'Sign out', async () => { onSignOut(); return '' }, 'log out logout'),
    ]
  }, [pipe?.running, theme, onTheme, onSignOut])

  const groups = useMemo(() => {
    const out = []
    const qc = parseQueue(q)
    if (qc && apps) {
      const hits = matchJobs(apps, qc.terms, qc.min)
      out.push(['Actions', [{
        id: 'a-queue-match', group: 'Actions', ownerOnly: true, disabled: !hits.length,
        label: hits.length ? `Queue ${hits.length} Inbox job${hits.length === 1 ? '' : 's'} matching “${qc.terms.join(' ')}” with fit ≥ ${qc.min}`
          : `No Inbox job matches “${qc.terms.join(' ')}” with fit ≥ ${qc.min}`,
        sub: hits.slice(0, 3).map(a => a.title).join(' · '),
        run: async () => {
          const r = await send('/api/applications/queue', 'POST', { job_ids: hits.slice(0, 50).map(a => a.job_id) })
          return `Queued ${r.queued.length}${r.rejected?.length ? ` — ${r.rejected.length} not queued: ${r.rejected[0].message}` : ''}`
        },
      }]])
    }
    const acts = rank(actions, q, 8)
    if (acts.length) out.push(['Actions', acts])
    if (q.trim() && apps) {
      const jobs = rank(apps.map(a => ({ id: `j-${a.job_id}`, label: a.title, keywords: `${a.company} ${a.location || ''}`, group: 'Jobs',
        sub: `${a.company} · ${stageLabel(a.stage)}`, href: `/applications?job=${encodeURIComponent(a.job_id)}` })), q, 6)
      if (jobs.length) out.push(['Jobs', jobs])
    }
    const go = rank(places(), q, q.trim() ? 10 : 40)
    if (go.length) out.push(['Go to', go])
    // two "Actions" groups (the queue command, then the rest) read as one
    return out.reduce((acc, [g, items]) => {
      const last = acc[acc.length - 1]
      if (last && last[0] === g) last[1] = [...last[1], ...items]; else acc.push([g, items])
      return acc
    }, [])
  }, [q, apps, actions])
  const flat = groups.flatMap(([, items]) => items)
  useEffect(() => { setAt(0) }, [q])
  useEffect(() => { list.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' }) }, [at])

  const run = async item => {
    if (!item || item.disabled || busy) return
    if (item.ownerOnly && !owner) { toast(false, OWNER_ONLY); return }
    if (item.href) { onClose(); router.push(item.href); return }
    setBusy(true)
    try { const text = await item.run(); onClose(); if (text) toast(true, text) }
    catch (e) { toast(false, e.message) }
    setBusy(false)
  }
  const onKey = e => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setAt(i => Math.min(flat.length - 1, i + 1)) }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setAt(i => Math.max(0, i - 1)) }
    else if (e.key === 'Enter') { e.preventDefault(); run(flat[at]) }
    else if (e.key === 'Escape') { e.preventDefault(); onClose() }
    else if (e.key === 'Tab') e.preventDefault()                       // focus stays in the palette
  }

  let n = -1
  return (
    <AnimatePresence>
      {open && (
        <motion.div className="cmdk-scrim" onMouseDown={e => { if (e.target === e.currentTarget) onClose() }}
          initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0, transition: { duration: 0.1 } }}>
          <motion.div className="cmdk" role="dialog" aria-modal="true" aria-label="Command palette"
            initial={{ opacity: 0, y: -12, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: -8, transition: { duration: 0.1 } }}
            transition={{ type: 'spring', stiffness: 520, damping: 38 }}>
            <div className="cmdk-in">
              <Search className="lucide" aria-hidden="true" />
              <input ref={input} autoFocus value={q} onChange={e => setQ(e.target.value)} onKeyDown={onKey} placeholder="Jump to a page or job, or type a command — try “queue ai 80”"
                role="combobox" aria-expanded="true" aria-controls="cmdk-list" aria-activedescendant={flat[at] ? `cmdk-${flat[at].id}` : undefined} aria-label="Command" />
              {busy ? <span className="sp" /> : <kbd>Esc</kbd>}
            </div>
            <div className="cmdk-list" id="cmdk-list" role="listbox" ref={list}>
              {!flat.length && <p className="cmdk-empty">Nothing matches “{q}”.</p>}
              {groups.map(([g, items]) => (
                <div key={g} role="group" aria-label={g}>
                  <h4>{g}</h4>
                  {items.map(it => {
                    n += 1
                    const i = n
                    const Icon = it.group === 'Jobs' ? Briefcase : it.group === 'Actions' ? Zap : ArrowRight
                    return (
                      <div key={it.id} id={`cmdk-${it.id}`} role="option" aria-selected={i === at} aria-disabled={it.disabled || (it.ownerOnly && !owner) || undefined}
                        className={`cmdk-it ${i === at ? 'on' : ''} ${it.disabled || (it.ownerOnly && !owner) ? 'off' : ''}`}
                        onMouseMove={() => setAt(i)} onClick={() => run(it)} data-cmd={it.id}>
                        <Icon className="lucide" aria-hidden="true" />
                        <span className="cmdk-l">{it.label}{it.sub && <small>{it.sub}</small>}</span>
                        {it.ownerOnly && !owner ? <em>owner only</em> : i === at && <CornerDownLeft className="lucide" aria-hidden="true" />}
                      </div>)
                  })}
                </div>))}
            </div>
            <div className="cmdk-foot"><span><kbd>↑</kbd><kbd>↓</kbd> move</span><span><kbd>Enter</kbd> run</span><span><kbd>Ctrl</kbd><kbd>K</kbd> open / close</span></div>
          </motion.div>
        </motion.div>)}
    </AnimatePresence>
  )
}
