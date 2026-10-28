// frontend/src/lib/toast.js
//
// A toast any part of the app can raise (the command palette's actions), shown by AppShell.
'use client'
import { useEffect, useState } from 'react'

const listeners = new Set()

export function toast(ok, text) {
  const t = { ok, text, at: Date.now() }
  listeners.forEach(fn => fn(t))
}

export function useToast(ms = 5000) {
  const [t, setT] = useState(null)
  useEffect(() => { listeners.add(setT); return () => listeners.delete(setT) }, [])
  useEffect(() => { if (!t) return undefined; const h = setTimeout(() => setT(null), ms); return () => clearTimeout(h) }, [t, ms])
  return t
}
