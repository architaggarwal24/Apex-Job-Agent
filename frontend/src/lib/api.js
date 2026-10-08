// frontend/src/lib/api.js
//
// Every request goes through here so an expired session reloads the app
// (which re-runs AppShell's GET /api/auth/me and renders the login view)
// instead of leaving a page silently empty.
//
// Errors carry .status so callers can react to a specific case (e.g. 403 = not
// the owner account) instead of string-matching a message. The message is read
// from whichever field the backend used — FastAPI's `detail` (a string, or a
// list of validation problems), or `error` / `message`. Reading only `detail`
// is why a failed "Test connection" used to show a bare "Request failed (400)".
function describe(body, status) {
  const d = body && (body.detail ?? body.error ?? body.message)
  if (typeof d === 'string' && d) return d
  if (Array.isArray(d) && d.length) return d.map(x => x?.msg || String(x)).join('; ')
  return `Request failed (${status})`
}

export async function api(path, opts = {}) {
  const r = await fetch(path, { credentials: 'same-origin', ...opts })
  if (r.status === 401) { window.location.href = '/'; throw new Error('Not logged in') }
  if (!r.ok) {
    const err = new Error(describe(await r.json().catch(() => ({})), r.status))
    err.status = r.status
    throw err
  }
  return r.json()
}

// POST/PATCH/DELETE helper for JSON bodies.
export const send = (path, method, body) =>
  api(path, { method, headers: { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) })
