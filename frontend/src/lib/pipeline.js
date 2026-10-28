// frontend/src/lib/pipeline.js
//
// The job search's live progress (GET /api/pipeline/status), shared by the bar on every
// page (AppShell) and the step list on Today. One poller for the whole app: every 1.5 s
// while a search runs, every 20 s otherwise (so a search started elsewhere - the schedule,
// another tab - still shows up). startSearch() posts the run and polls at once.
'use client'
import { useEffect, useState } from 'react'
import { api, send } from '@/lib/api'

const FAST = 1500
const SLOW = 20000
let state = null
let timer = null
let users = 0
const listeners = new Set()
const finishListeners = new Set()

function emit(next) {
  const wasRunning = !!state?.running
  state = next
  listeners.forEach(fn => fn(state))
  if (wasRunning && !next.running) finishListeners.forEach(fn => fn(next))
}

async function poll() {
  clearTimeout(timer)
  try { emit(await api('/api/pipeline/status')) } catch { /* keep the last state */ }
  if (users > 0) timer = setTimeout(poll, state?.running ? FAST : SLOW)
}

export function refreshPipeline() { poll() }

// Start a search; resolves with the server's answer, throws its error (409 = already running).
export async function startSearch(quick) {
  const r = await send('/api/pipeline/run', 'POST', { quick })
  setTimeout(poll, 150)
  return r
}

export async function cancelSearch() {
  const r = await send('/api/pipeline/cancel', 'POST')
  poll()
  return r
}

export function usePipeline() {
  const [s, setS] = useState(state)
  useEffect(() => {
    listeners.add(setS)
    users += 1
    if (users === 1) poll()
    return () => { listeners.delete(setS); users -= 1; if (!users) clearTimeout(timer) }
  }, [])
  return s
}

// Called once with the final status when a search that was running finishes.
export function useOnSearchFinished(fn) {
  useEffect(() => {
    finishListeners.add(fn)
    return () => finishListeners.delete(fn)
  }, [fn])
}

export const fmtElapsed = secs => {
  const s = Math.max(0, Math.round(secs || 0))
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, '0')}s`
}
