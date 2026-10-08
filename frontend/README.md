# Apex UI — Next.js 16 + React 19

Lives next to the Python backend and talks to it over `/api/*`.

## Run it (one terminal)

```bash
# once
cd frontend && npm install && cd ..

# development — Next dev server + FastAPI together, hot reload:
python main.py --dev-ui          # then open http://localhost:3000

# production-style — build once, then Python serves everything itself:
cd frontend && npm run build && cd ..
python main.py                   # http://localhost:8000
```

## How the two halves are joined

| | dev (`--dev-ui`) | production (`npm run build`) |
|---|---|---|
| Pages | Next dev server, `:3000` | static export in `frontend/out/`, served by FastAPI at `/` |
| `/api/*` | proxied by Next to FastAPI (`next.config.mjs`) | same origin, direct |
| Node needed at runtime | yes | **no** — only to build |

The browser only ever talks to one origin, so the session cookie just works and
there is no CORS (which this project deliberately doesn't enable).

Apply, Pipeline and first-run Setup are still served by the backend's own pages;
the sidebar links to them (`src/lib/backend.js`).

## Layout

```
src/app/            routes — page.jsx (Dashboard), jobs/, profile/, settings/
src/components/     layout/AppShell (auth, sidebar, theme), auth/Login
src/lib/            api.js (fetch wrapper), theme.js, backend.js
next.config.mjs     dev proxy vs. static export, chosen by build phase
```

Optional env: `NEXT_PUBLIC_BACKEND_ORIGIN` (dev link target for legacy pages,
default `http://localhost:8000`), `APEX_BACKEND_ORIGIN` (dev proxy target,
default `http://127.0.0.1:8000`).
