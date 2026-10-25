"""
server/app.py — FastAPI application factory.

SECURITY NOTE: No CORS middleware is configured. This app is served
and consumed same-origin (the UI is served by this same FastAPI app),
so cross-origin requests are not needed. Enabling permissive CORS
(allow_origins=["*"]) would let ANY website the user has open in
another tab make authenticated requests to this server — including
reading API keys via settings endpoints and triggering job
applications. Do not re-add CORS without a specific, narrow reason
and an explicit allow-list of origins.

The same-origin rule holds in development too: the Next dev server
(:3000) proxies /api/* to this app (see frontend/next.config.mjs), so
the browser only ever talks to one origin there as well.
"""

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path

from server.static_shell import SHELL_ROUTES, is_public_path

ROOT = Path(__file__).resolve().parent.parent

# Where `cd frontend && npm run build` puts the static export.
DEFAULT_UI_DIR = ROOT / "frontend" / "out"


def _install_ui(app: FastAPI, ui_dir: Path | None) -> Path | None:
    """
    Serve the Next.js app.

      * Built (frontend/out exists): mounted at "/" — production, one process.
        Called AFTER the API routers are registered so /api/*, /apply, etc.
        match first and this catch-all only receives what's left.
      * Not built, but `python main.py --dev-ui` started the Next dev server
        (it sets APEX_DEV_UI_ORIGIN): redirect the app's pages to that server,
        so links from the legacy pages (Apply/Pipeline) still land somewhere.
      * Neither: nothing is mounted and those paths 404.

    Returns the directory being served (None if not built) for the auth gate.
    """
    if ui_dir is not None and ui_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(ui_dir), html=True), name="ui")
        return ui_dir

    dev_origin = os.getenv("APEX_DEV_UI_ORIGIN", "").rstrip("/")
    if dev_origin:
        def make(path: str):
            async def go():
                return RedirectResponse(dev_origin + path, status_code=307)
            return go
        for route in SHELL_ROUTES:
            app.add_api_route(route, make(route), methods=["GET"], include_in_schema=False)
    return None


def _install_auth_gate(app: FastAPI, ui_dir: Path | None) -> None:
    """
    One gate in front of the entire app, rather than trusting every
    present and future route to remember to check for a session. A new
    route added next month is protected by default; forgetting is the
    failure mode this exists to remove.

    What's public is decided in server/static_shell.py: the auth endpoints,
    and exactly the files that exist in the Next.js export (the login screen
    is part of that app, so its shell has to load for logged-out visitors).

    Note this middleware only sees HTTP. WebSockets bypass it, so
    server/websocket.py performs its own session + ownership check.
    """
    from core.auth import get_current_user_id_optional

    @app.middleware("http")
    async def auth_gate(request: Request, call_next):
        path = request.url.path
        if is_public_path(path, ui_dir):
            return await call_next(request)

        # A valid signature isn't enough: core.auth.user_for_token() also
        # requires the account to still exist/be active and the session
        # version to be current — so deactivating a user, changing a
        # password or "sign out everywhere" ends sessions now, not whenever
        # the 14-day token happens to expire.
        uid = get_current_user_id_optional(request)
        if uid is None:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "Not logged in"}, status_code=401)
            return RedirectResponse("/", status_code=303)

        request.state.user_id = uid
        return await call_next(request)


def create_app(ui_dir: Path | None = DEFAULT_UI_DIR) -> FastAPI:
    app = FastAPI(title="Apex Job Agent", version="3.0.0")

    static_dir = ROOT / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    ss_dir = ROOT / "data" / "screenshots"
    ss_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/screenshots", StaticFiles(directory=str(ss_dir)), name="screenshots")

    from server.routes.onboarding  import router as on_router
    from server.routes.jobs        import router as jobs_router
    from server.routes.apply       import router as apply_router
    from server.routes.tracking    import router as track_router
    from server.routes.applications import router as apps_router
    from server.routes.review      import router as review_router
    from server.routes.queue       import router as queue_router
    from server.routes.answer_bank import router as bank_router
    from server.routes.autonomy    import router as autonomy_router
    from server.routes.kits        import router as kits_router
    from server.routes.assist      import router as assist_router
    from server.routes.follow_ups  import router as follow_ups_router
    from server.routes.prep        import router as prep_router
    from server.routes.analytics   import router as analytics_router
    from server.routes.notifications import router as notifications_router
    from server.routes.runner      import router as runner_router
    from server.routes.runner_tokens import router as runner_tokens_router
    from server.routes.ghostwriter import router as gw_router
    from server.routes.settings    import router as settings_router
    from server.routes.auth        import router as auth_router
    from server.websocket          import router as ws_router

    for r in [auth_router, on_router, jobs_router, apply_router,
              track_router, apps_router, review_router, queue_router, bank_router, autonomy_router,
              kits_router, assist_router, follow_ups_router, prep_router, analytics_router, notifications_router, runner_router, runner_tokens_router, gw_router, settings_router, ws_router]:
        app.include_router(r)

    served = _install_ui(app, ui_dir)      # must come after the routers (catch-all mount)
    _install_auth_gate(app, served)
    return app
