// frontend/src/app/jobs/page.jsx
'use client'
import { useEffect } from 'react'
import { useRouter } from 'next/navigation'

// The Jobs list is now part of the Applications workspace; old links and bookmarks land there.
export default function Jobs() {
  const router = useRouter()
  useEffect(() => { router.replace(`/applications${window.location.search}`) }, [router])
  return <div className="empty">Jobs moved to <a href="/applications">Applications</a>…</div>
}
