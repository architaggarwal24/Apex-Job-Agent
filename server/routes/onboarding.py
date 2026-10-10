"""
server/routes/onboarding.py — Setup + profile routes.
"""

import json
import logging
import shutil

from fastapi import APIRouter, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from core.config   import cfg
from db.profile_db import ProfileDB

logger = logging.getLogger(__name__)
router = APIRouter()
ROOT   = cfg.ROOT_DIR


def _t(name: str) -> str:
    p = ROOT / "templates" / name
    return p.read_text(encoding="utf-8") if p.exists() else f"<h1>Missing: {name}</h1>"


# ── Pages ─────────────────────────────────────────────────────────

# Dashboard (/), Profile, Jobs and Settings are the Next.js app in frontend/ —
# it owns those URLs (and does the "profile incomplete → /setup" redirect
# itself, from its Dashboard). This backend still serves its own pages only
# for first-run Setup, Apply and Pipeline, which aren't ported yet.
@router.get("/setup",      response_class=HTMLResponse)
async def setup_page():    return HTMLResponse(_t("setup.html"))

@router.get("/apply",      response_class=HTMLResponse)
async def apply_page():    return HTMLResponse(_t("apply.html"))

@router.get("/tracking",   response_class=HTMLResponse)
async def tracking_page(): return HTMLResponse(_t("tracking.html"))


# ── Resume upload ─────────────────────────────────────────────────

@router.post("/api/setup/upload-resume")
async def upload_resume(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted")

    with open(cfg.RESUME_PATH, "wb") as f:
        shutil.copyfileobj(file.file, f)

    logger.info(f"[Onboarding] Resume saved ({cfg.RESUME_PATH.stat().st_size} bytes)")

    try:
        from core.resume_parser import parse
        parsed = parse(cfg.RESUME_PATH)
        db = ProfileDB()
        db.import_from_parsed(parsed)
        return JSONResponse({
            "status":   "ok",
            "name":     parsed.get("full_name", ""),
            "email":    parsed.get("email", ""),
            "skills":   parsed.get("skills", [])[:10],
            "target_roles": parsed.get("target_roles", []),
        })
    except Exception as e:
        logger.error(f"[Onboarding] Parse failed: {e}")
        return JSONResponse({"status": "parse_failed", "error": str(e)}, status_code=500)


# ── Profile API ───────────────────────────────────────────────────

@router.get("/api/profile")
async def get_profile():
    return JSONResponse(ProfileDB().get_by_category())


@router.get("/api/profile/flat")
async def get_profile_flat():
    return JSONResponse(ProfileDB().get_all())


@router.post("/api/profile")
async def update_profile(data: dict):
    """
    Update profile fields.

    Validation:
      - Only accepts keys that are either a STANDARD_FIELDS key, or
        already exist in the DB (i.e. a previously-created custom
        field). This prevents a client from injecting arbitrary new
        field_keys with no schema/category, which previously went
        straight into SQLite via an ON CONFLICT upsert.
      - Values must be strings, capped at 10,000 chars (generous for
        a cover letter / resume summary, but bounds worst case).
    """
    from db.profile_db import STANDARD_FIELDS

    db = ProfileDB()
    known_keys = {k for k, *_ in STANDARD_FIELDS} | set(db.get_all().keys())

    MAX_LEN  = 10_000
    accepted = {}
    rejected = []

    for key, value in data.items():
        if key not in known_keys:
            rejected.append(key)
            continue
        if not isinstance(value, (str, int, float, bool)):
            rejected.append(key)
            continue
        value = str(value)
        if len(value) > MAX_LEN:
            value = value[:MAX_LEN]
        accepted[key] = value

    if rejected:
        logger.warning(f"[Onboarding] /api/profile rejected unknown/invalid keys: {rejected}")

    db.set_many(accepted, source="web_ui")
    return JSONResponse({"status": "ok", "updated": len(accepted), "rejected": rejected})


# ── Search preferences ────────────────────────────────────────────

@router.get("/api/search-prefs")
async def get_prefs():
    db  = ProfileDB()
    def jl(k):
        try: return json.loads(db.get(k, "[]"))
        except Exception: return []
    return JSONResponse({"positions": jl("search_positions"),
                         "locations": jl("search_locations")})


@router.post("/api/search-prefs")
async def save_prefs(data: dict):
    db = ProfileDB()
    if "positions" in data:
        db.set("search_positions",
               json.dumps([p.strip() for p in data["positions"] if p.strip()]),
               source="setup")
    if "locations" in data:
        db.set("search_locations",
               json.dumps([l.strip() for l in data["locations"] if l.strip()]),
               source="setup")
    return JSONResponse({"status": "ok"})


# ── Status ────────────────────────────────────────────────────────

@router.get("/api/status")
async def get_status():
    from core.llm import provider_info
    db = ProfileDB()
    ok, missing = db.is_complete()

    # dry_run is read from SettingsDB (the Settings page toggle), which
    # is what applier/session.Orchestrator._dry_run_enabled() actually
    # checks before submitting. cfg.DRY_RUN (.env) is only the initial
    # seed value — reading it here would show a stale badge if the user
    # toggled Dry Run in Settings without restarting the server.
    try:
        from db.settings_db import SettingsDB
        dry_run = SettingsDB.get().read("dry_run", "false").lower() == "true"
    except Exception:
        dry_run = cfg.DRY_RUN

    return JSONResponse({
        "profile_complete": ok,
        "missing_required": missing,
        "llm":              provider_info(),
        "resume_exists":    cfg.RESUME_PATH.exists(),
        "dry_run":          dry_run,
    })
