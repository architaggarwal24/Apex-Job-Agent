// frontend/src/lib/live.js
//
// Live updates and keyboard shortcuts for the Applications workspace.
//
// useLiveEvents(onEvent): this user's stage changes from GET /api/events/stream
// (server-sent events; server/sse.py). If the stream can't be held open - it keeps
// failing, or EventSource is missing - it falls back to calling onEvent(null) every
// POLL_MS so the page reloads by polling instead. Returns "live" | "polling" | "connecting".
//
// useShortcuts(map, enabled): keydown -> map[key](event). Ignored while typing in an
// input, textarea, select or contenteditable, and with Ctrl/Alt/Meta held.
import { useEffect, useRef, useState } from 'react'

export const POLL_MS = 20000
const MAX_FAILURES = 3

export function useLiveEvents(onEvent) {
  const cb = useRef(onEvent)
  cb.current = onEvent
  const [mode, setMode] = useState('connecting')

  useEffect(() => {
    let es = null, timer = null, failures = 0, stopped = false
    const poll = () => {
      setMode('polling')
      if (!timer) timer = setInterval(() => cb.current(null), POLL_MS)
    }
    if (typeof window === 'undefined' || !window.EventSource) { poll(); return () => clearInterval(timer) }
    try {
      es = new EventSource('/api/events/stream', { withCredentials: true })
    } catch { poll(); return () => clearInterval(timer) }
    es.onopen = () => { failures = 0; setMode('live'); if (timer) { clearInterval(timer); timer = null } }
    es.addEventListener('application_event', e => {
      try { cb.current(JSON.parse(e.data)) } catch { cb.current(null) }
    })
    es.onerror = () => {
      if (stopped) return
      failures += 1
      if (failures >= MAX_FAILURES || es.readyState === 2) { es.close(); poll() }
    }
    return () => { stopped = true; if (es) es.close(); if (timer) clearInterval(timer) }
  }, [])
  return mode
}

const typing = el => el && (el.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(el.tagName))

export function useShortcuts(map, enabled = true) {
  const ref = useRef(map)
  ref.current = map
  useEffect(() => {
    if (!enabled) return undefined
    const onKey = e => {
      if (e.ctrlKey || e.metaKey || e.altKey || typing(e.target)) return
      if (e.key === 'Enter' && e.target.closest?.('button,a,[role="tab"]')) return   // let a focused control have its Enter
      const fn = ref.current[e.key] || ref.current[e.key.toLowerCase()]
      if (fn) { e.preventDefault(); fn(e) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [enabled])
}

// A link that is meant to be opened: plain http(s) only (job URLs are scraped, untrusted text).
export const safeHref = url => (/^https?:\/\//i.test(url || '') ? url : '')

export async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true } catch {
    const ta = document.createElement('textarea')
    ta.value = text; document.body.appendChild(ta); ta.select()
    let ok = false
    try { ok = document.execCommand('copy') } catch { ok = false }
    ta.remove(); return ok
  }
}
