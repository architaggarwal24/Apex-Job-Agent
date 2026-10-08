// frontend/src/api.js
//
// Every request goes through here so an expired session reloads the app
// instead of leaving a page silently empty. There's no separate /login
// page to send the browser to — reloading at /ui/ re-runs App.jsx's own
// GET /api/auth/me check, which renders the login view in place.
export async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: 'same-origin', ...opts })
  if (r.status === 401) { window.location.href = '/ui/'; throw new Error('Not logged in') }
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `Request failed (${r.status})`)
  return r.json()
}
