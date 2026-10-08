// frontend/next.config.mjs
//
// Two modes, chosen by Next's build phase (cross-platform — no env vars to
// set, which matters on Windows where `VAR=1 cmd` doesn't work):
//
//   next dev    → normal dev server. /api/* is proxied to the FastAPI backend,
//                 so the browser only ever talks to ONE origin (localhost:3000):
//                 the session cookie just works and there's no CORS to configure.
//
//   next build  → static export into frontend/out/. FastAPI serves those files
//                 itself at "/", so production is a single Python process and the
//                 server needs no Node runtime. (Rewrites don't exist in an
//                 export — not needed, since the pages and the API are then
//                 already on the same origin.)
import { PHASE_PRODUCTION_BUILD } from 'next/constants.js'

const BACKEND = process.env.APEX_BACKEND_ORIGIN || 'http://127.0.0.1:8000'

export default function config(phase) {
  if (phase === PHASE_PRODUCTION_BUILD) {
    // trailingSlash → /jobs/index.html rather than /jobs.html, which is what
    // Starlette's StaticFiles(html=True) resolves cleanly. Deliberately NOT
    // set in dev: it would 308-redirect /api/jobs → /api/jobs/ and break the proxy.
    return { output: 'export', trailingSlash: true }
  }
  return {
    // Resume parsing can take a minute or more on a slow LLM; the dev proxy's
    // default timeout is far shorter and would surface as a spurious failure.
    experimental: { proxyTimeout: 180_000 },
    async rewrites() {
      return [{ source: '/api/:path*', destination: `${BACKEND}/api/:path*` }]
    },
  }
}
