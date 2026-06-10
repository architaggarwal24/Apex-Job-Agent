"""
server/app.py — FastAPI application factory.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def create_app() -> FastAPI:
    app = FastAPI(title="Apex Job Agent", version="3.0.0")

    app.add_middleware(CORSMiddleware, allow_origins=["*"],
                       allow_methods=["*"], allow_headers=["*"])

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
    from server.routes.ghostwriter import router as gw_router
    from server.routes.settings    import router as settings_router
    from server.websocket          import router as ws_router

    for r in [on_router, jobs_router, apply_router,
              track_router, gw_router, settings_router, ws_router]:
        app.include_router(r)

    return app
