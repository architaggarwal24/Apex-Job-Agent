// frontend/src/lib/me.js
//
// Who is signed in (GET /api/auth/me): {id, email, owner}. `owner` is the first account,
// the only one allowed the owner-only actions; pages use it to DISABLE those controls for
// other accounts instead of letting a click fail with a 403. null while loading.
'use client'
import { useEffect, useState } from 'react'
import { api } from '@/lib/api'

export function useMe() {
  const [me, setMe] = useState(null)
  useEffect(() => { api('/api/auth/me').then(setMe).catch(() => setMe({ authenticated: false, owner: false })) }, [])
  return me
}

export const OWNER_ONLY = 'Only the owner account can do this.'
