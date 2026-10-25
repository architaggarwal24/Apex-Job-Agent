// frontend/src/app/applications/page.jsx
'use client'
import { Suspense } from 'react'
import Workspace from '@/components/applications/Workspace'

// The filters live in the URL query (useSearchParams), which a static export can
// only read inside a Suspense boundary.
export default function ApplicationsPage() {
  return <Suspense fallback={<div className="empty">Loading…</div>}><Workspace /></Suspense>
}
