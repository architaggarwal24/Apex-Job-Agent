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

Every page is the Next app. The old `/jobs`, `/apply`, `/tracking` and `/setup`
URLs redirect to Applications or Today (`server/static_shell.py`).

## Layout

```
src/app/                  routes: page.jsx (Today), applications/, insights/, profile/, settings/
src/components/
  layout/AppShell         auth gate, sidebar, theme
  auth/Login              sign-in / first-run account creation
  applications/           Workspace (board, list, review queue), SidePanel, and its tabs
                          (Hunt, Apply, Prep), ReviewPanel, Ghostwriter, Analytics
  settings/               General, Providers, Sessions, Agent, Automation tabs, plus the answer
                          bank, autonomy, notifications, resumes and runner-token sections
src/lib/
  api.js                  fetch wrapper (an expired session reloads into the login view)
  backend.js              backend origin for the apply WebSocket, which the dev proxy can't carry
  live.js                 live updates and keyboard shortcuts for the Applications workspace
  stages.js, filters.js   stage names/labels and the Applications filters (URL form)
  insights.js, me.js      helpers for Today/Insights, and who is signed in (owner or not)
  theme.js, prefs.js, presets.js   appearance and provider presets
next.config.mjs           dev proxy vs. static export, chosen by build phase
```

Optional env: `NEXT_PUBLIC_BACKEND_ORIGIN` (dev target for the apply WebSocket,
default `http://localhost:8000`), `APEX_BACKEND_ORIGIN` (dev proxy target,
default `http://127.0.0.1:8000`).

## License

Part of [Apex Job Agent](../README.MD), released under the [MIT License](../LICENSE).
