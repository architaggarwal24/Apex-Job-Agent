// frontend/src/lib/backend.js
//
// A URL the Next dev proxy can't carry — the apply session's WebSocket
// (/ws/{job_id}, components/applications/ApplyTab.jsx). In dev this app runs on
// :3000 and the backend on :8000, so it must be absolute; in production both
// are the same origin and a relative path is correct.
// (Cookies aren't port-scoped, so the login session is shared between them.)
const ORIGIN = process.env.NODE_ENV === 'development'
  ? (process.env.NEXT_PUBLIC_BACKEND_ORIGIN || 'http://localhost:8000')
  : ''

export const backendUrl = path => `${ORIGIN}${path}`
