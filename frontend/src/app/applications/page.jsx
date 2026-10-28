// frontend/src/app/applications/page.jsx
'use client'
import { Suspense } from 'react'
import Workspace from '@/components/applications/Workspace'
import { SkeletonRows } from '@/components/layout/Skeleton'

// The filters live in the URL query (useSearchParams), which a static export can
// only read inside a Suspense boundary.
export default function ApplicationsPage() {
  return <Suspense fallback={<SkeletonRows n={9} label="Loading applications" />}><Workspace /></Suspense>
}
