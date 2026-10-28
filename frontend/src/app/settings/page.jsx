// frontend/src/app/settings/page.jsx
//
// Settings: five pages instead of sixteen sections, each a column of folds that say in
// plain words what is set now (lib/settingsNav.js has the map, the old ?s= links included).
//
//   Start here     the four things to set, in order (components/settings/StartHere.jsx)
//   AI model       AI providers (shared save bar: POST /api/settings) · AI job scoring
//   Job search     job boards · job feeds · company boards · API keys & Apify
//   Applying       who submits · practice & pace · how many a day · skip these · answer bank ·
//                  resumes · where it runs (+ runner tokens) · job-site logins · auto-submit
//   You & alerts   appearance · account · notifications
//
// ?s=<page>&open=<fold> opens a fold; one is open at a time. Search and applying settings
// are the OWNER's: other accounts see them read-only.
'use client'
import { Suspense, useCallback, useEffect, useState } from 'react'
import { usePathname, useRouter, useSearchParams } from 'next/navigation'
import { api, send } from '@/lib/api'
import { useMe } from '@/lib/me'
import { Account, Appearance } from '@/components/settings/GeneralTab'
import ProvidersTab, { isMasked } from '@/components/settings/ProvidersTab'
import SessionsTab from '@/components/settings/SessionsTab'
import { AiScoring, ApplyModes, Blacklist, Executor, Ledger, PaceAndPractice, QualityCaps } from '@/components/settings/AutomationTab'
import AutonomySection from '@/components/settings/AutonomySection'
import AnswerBankSection from '@/components/settings/AnswerBankSection'
import ResumesSection from '@/components/settings/ResumesSection'
import RunnerTokensSection from '@/components/settings/RunnerTokensSection'
import NotificationsSection from '@/components/settings/NotificationsSection'
import { Apify, Boards, Companies, Keys, SourceList } from '@/components/settings/SourcesTab'
import StartHere from '@/components/settings/StartHere'
import Fold from '@/components/settings/Fold'
import { SETTINGS_FOLDS, SETTINGS_PAGES, resolveSettings, settingsHref } from '@/lib/settingsNav'
import { SkeletonCard } from '@/components/layout/Skeleton'

const OWNER_PAGES = ['ai', 'search', 'applying']
const ENGINE = { jobspy: 'no login', apify: 'Apify', browser: 'your saved login' }
const BOARD = { linkedin: 'LinkedIn', indeed: 'Indeed', naukri: 'Naukri' }
const set_ = v => !!String(v || '').trim()
const plural = (n, w) => `${n} ${n === 1 ? w : w.replace(/y$/, 'ie')}${n === 1 ? '' : 's'}`

// One line per fold: what is set now, in words.
function summaries(s, status, sources) {
  if (!s) return {}
  const boards = Object.keys(BOARD).map(b => `${BOARD[b]}: ${ENGINE[s[`search_engine_${b}`]] || 'no login'}`).join(' · ')
  const feeds = sources ? sources.filter(x => x.kind !== 'ats' && x.enabled).length : null
  const followed = sources ? sources.filter(x => x.kind === 'ats').reduce((n, x) => n + (x.boards || 0), 0) : null
  const keys = [['adzuna_api_key', 'Adzuna'], ['jsearch_api_key', 'JSearch'], ['himalayas_api_key', 'Himalayas'], ['apify_api_token', 'Apify']].filter(([k]) => set_(s[k])).map(([, l]) => l)
  const black = String(s.company_blacklist || '').split(/[\n,]/).map(x => x.trim()).filter(Boolean).length
  return {
    providers: status?.llm ? `Using ${status.llm.label || status.llm.provider} / ${status.llm.model}` : 'No provider chosen',
    scoring: s.enable_llm_scoring === 'true' ? 'On: the AI scores each new job' : 'Off: jobs get the instant keyword match only',
    boards, sources: feeds == null ? 'Public job feeds, no login' : `${plural(feeds, 'feed')} on`,
    companies: followed == null ? 'Company job boards read directly' : `${plural(followed, 'company board')} followed`,
    keys: keys.length ? `Set: ${keys.join(', ')}` : 'None set (all optional)',
    modes: s.apply_mode === 'assist' ? 'You apply yourself; the agent prepares' : 'The agent fills it, you approve',
    pace: `${s.dry_run === 'true' ? 'Practice mode ON — nothing is sent' : 'Practice mode off'} · waits ${s.min_delay_between_applies || 0}–${s.max_delay_between_applies || 0} s`,
    limits: `${s.quality_first === 'true' ? 'Careful mode on' : 'Careful mode off'} · at most ${Number(s.daily_apply_limit) ? s.daily_apply_limit : 'unlimited'} a day · minimum fit ${s.auto_skip_score_threshold || 0}`,
    skip: `${black ? plural(black, 'company') : 'No companies'} blocked · ${Number(s.min_salary_lpa) ? `under ₹${s.min_salary_lpa} LPA skipped` : 'no salary floor'}`,
    answers: 'What the agent answers when a form asks the same thing again',
    resumes: 'Versions of your resume; the best fit is picked per job',
    runner: { runner: 'On your own computer', auto: 'Your computer when its runner is on, else this server', server: 'On this server' }[s.executor] || 'On this server',
    logins: 'Only for applying on Naukri and LinkedIn Easy Apply',
    autonomy: 'Off unless you turn it on: the agent never submits by itself',
    appearance: 'Theme, density, text size, motion', account: 'Sign-in and password', notifications: 'E-mail and Telegram when the agent needs you',
  }
}

const RECOMMENDED = {
  modes: 'Recommended: you approve', pace: 'Try practice mode first', limits: 'Recommended: careful mode', boards: 'Defaults need no login',
  runner: 'Recommended: my computer', autonomy: 'Advanced',
}

function SettingsPage() {
  const params = useSearchParams()
  const router = useRouter()
  const pathname = usePathname()
  const me = useMe()
  const owner = !!me?.owner
  const { page, fold } = resolveSettings(params.get('s'), params.get('open'))
  const go = (p, f = '') => router.replace(`${pathname}${settingsHref(p, f).replace('/settings', '')}`, { scroll: false })

  const [settings, setSettings] = useState(null)
  const [providers, setProviders] = useState(null)
  const [status, setStatus] = useState(null)
  const [dirty, setDirty] = useState({})
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState(null)
  const [sources, setSources] = useState(null)
  const [ledger, setLedger] = useState(null)

  // Reloads the saved settings and the provider list (which also carries each
  // provider's custom models and "configured" state). Unsaved edits in `dirty` survive it.
  const reload = useCallback(async () => {
    const [s, p] = await Promise.all([api('/api/settings'), api('/api/settings/providers')])
    setSettings(s.settings); setProviders(p)
    api('/api/status').then(setStatus).catch(() => {})
  }, [])
  const loadSources = useCallback(() => api('/api/sources').then(j => setSources(j.sources)).catch(() => {}), [])
  const loadLedger = useCallback(() => api('/api/automation/ledger').then(setLedger).catch(() => {}), [])
  useEffect(() => { reload().catch(() => {}) }, [reload])
  useEffect(() => { if (page === 'search') loadSources() }, [page, loadSources])
  useEffect(() => { if (page === 'applying') loadLedger() }, [page, loadLedger])
  const reloadAll = async () => { await reload(); await loadSources() }

  const val = k => dirty[k] ?? settings?.[k] ?? ''
  const setVal = (k, v) => { setDirty(d => ({ ...d, [k]: v })); setMsg(null) }
  // A deleted provider can't stay the pending "active" choice.
  const onDeleted = id => setDirty(d => { const n = { ...d }; if (n.llm_provider === id) delete n.llm_provider; return n })
  const pending = Object.entries(dirty).filter(([, v]) => !isMasked(v))
  const save = async () => {
    if (!pending.length) return
    setSaving(true); setMsg(null)
    try {
      const r = await send('/api/settings', 'POST', Object.fromEntries(pending))
      setMsg({ ok: true, text: `Saved ${r.saved} setting${r.saved === 1 ? '' : 's'}` })
      setDirty({}); await reload()
    } catch (e) { setMsg({ ok: false, text: e.message }) }
    setSaving(false)
  }

  const siteTypes = ledger ? Object.keys(ledger.site_types) : []
  const sum = summaries(settings, status, sources)
  const body = key => {
    if (!settings && !['appearance', 'account', 'logins', 'answers', 'resumes'].includes(key)) return <SkeletonCard label="Loading settings" />
    switch (key) {
      case 'providers': return providers ? <ProvidersTab settings={settings} providers={providers} val={val} setVal={setVal} reload={reload} onDeleted={onDeleted} /> : <SkeletonCard label="Loading providers" />
      case 'scoring': return <AiScoring settings={settings} reload={reload} owner={owner} />
      case 'boards': return <Boards settings={settings} reload={reloadAll} owner={owner} />
      case 'sources': return <SourceList owner={owner} sources={sources} reload={reloadAll} />
      case 'companies': return <Companies owner={owner} sources={sources} reload={loadSources} />
      case 'keys': return <><Keys settings={settings} reload={reloadAll} owner={owner} /><Apify settings={settings} reload={reloadAll} owner={owner} /></>
      case 'modes': return <ApplyModes settings={settings} reload={reload} owner={owner} siteTypes={siteTypes} />
      case 'pace': return <PaceAndPractice settings={settings} reload={reload} owner={owner} />
      case 'limits': return <QualityCaps settings={settings} reload={reload} owner={owner} />
      case 'skip': return <Blacklist settings={settings} reload={reload} owner={owner} />
      case 'answers': return <AnswerBankSection />
      case 'resumes': return <ResumesSection />
      case 'runner': return <><Executor settings={settings} reload={reload} owner={owner} /><RunnerTokensSection owner={owner} /></>
      case 'logins': return <SessionsTab />
      case 'autonomy': return <>
        <AutonomySection owner={owner} siteTypes={siteTypes} onChanged={loadLedger} />
        <section className="card section auto-sec" data-section="ledger">
          <h2 style={{ padding: 0, marginBottom: 'var(--sp2)' }}>Reliability ledger</h2>
          <div className="m" style={{ marginBottom: 'var(--sp2)' }}>Per kind of site: how often the agent's filled form was approved as it was. Success counts only applications a person decided.</div>
          <Ledger ledger={ledger} />
        </section></>
      case 'appearance': return <Appearance />
      case 'account': return <Account />
      case 'notifications': return <NotificationsSection owner={owner} />
      default: return null
    }
  }
  const [, label, hint] = SETTINGS_PAGES.find(([k]) => k === page)

  return (
    <div className="settings">
      <nav className="set-nav" aria-label="Settings pages">
        <h1>Settings</h1>
        {SETTINGS_PAGES.map(([k, l, d]) => (
          <button key={k} className={`set-link ${page === k ? 'on' : ''}`} aria-current={page === k ? 'page' : undefined} onClick={() => go(k)} title={d}>
            {l}{k === 'ai' && pending.length > 0 && page !== k ? <span className="dot y" title="Unsaved changes" /> : null}
          </button>))}
      </nav>
      <div className="set-body">
        <header className="set-head">
          <h2>{label}</h2>
          <p className="m">{hint}</p>
          {!owner && me && OWNER_PAGES.includes(page) &&
            <div className="alert" style={{ marginTop: 'var(--sp2)' }}>You are not the owner account: these settings are read-only here.</div>}
        </header>
        {page === 'start' ? <StartHere settings={settings} owner={owner} go={go} /> : (
          <div className="folds">
            {SETTINGS_FOLDS[page].map(([k, l]) => (
              <Fold key={k} id={k} title={l} summary={sum[k]} recommended={RECOMMENDED[k]} open={fold === k} onToggle={f => go(page, f)}>
                {body(k)}
              </Fold>))}
          </div>)}
        {page === 'ai' && fold === 'providers' && (
          <div className="save-bar">
            {msg && <span className={msg.ok ? 'msg-ok' : 'msg-err'}>{msg.text}</span>}
            {!msg && pending.length > 0 && <span className="m">{pending.length} unsaved change{pending.length === 1 ? '' : 's'}</span>}
            <button className="btn primary" disabled={saving || pending.length === 0} onClick={save}>
              {saving ? 'Saving…' : 'Save changes'}
            </button>
          </div>
        )}
      </div>
    </div>
  )
}

// useSearchParams needs a Suspense boundary in a static export.
export default function Settings() {
  return <Suspense fallback={null}><SettingsPage /></Suspense>
}
