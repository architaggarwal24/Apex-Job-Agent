"""
server/routes/settings.py — Settings API + page route.

GET  /settings                → settings page HTML
GET  /api/settings            → all settings (keys masked)
GET  /api/settings/providers  → provider definitions for UI
POST /api/settings            → save settings
POST /api/settings/test-llm  → test current LLM connection
"""

import logging
from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

from db.settings_db import SettingsDB, PROVIDERS

logger = logging.getLogger(__name__)
router = APIRouter()
ROOT   = __import__("pathlib").Path(__file__).resolve().parent.parent.parent


def _t(name: str) -> str:
    p = ROOT / "templates" / name
    return p.read_text(encoding="utf-8") if p.exists() else f"<h1>Missing: {name}</h1>"


@router.get("/settings", response_class=HTMLResponse)
async def settings_page():
    return HTMLResponse(_t("settings.html"))


@router.get("/api/settings")
async def get_settings():
    """Return all settings. API keys are masked for display."""
    db   = SettingsDB.get()
    data = db.read_all()

    # Mask API keys — show only last 4 chars
    masked = {}
    for k, v in data.items():
        if "api_key" in k and v:
            masked[k] = "••••••••" + v[-4:] if len(v) > 4 else "••••"
        else:
            masked[k] = v

    return JSONResponse({
        "settings": masked,
        "provider_info": db.provider_info(),
    })


@router.get("/api/settings/providers")
async def get_providers():
    """Return full provider definitions for building the UI."""
    return JSONResponse(PROVIDERS)


@router.get("/api/settings/raw/{key}")
async def get_setting_raw(key: str):
    """Get a single setting value (unmasked). Used when editing."""
    db  = SettingsDB.get()
    val = db.read(key, "")
    return JSONResponse({"key": key, "value": val})


@router.post("/api/settings")
async def save_settings(data: dict):
    """
    Save settings. Handles partial updates.
    Pass only the keys you want to change.
    Empty string for an API key means 'keep existing value'.
    """
    db = SettingsDB.get()

    updates = {}
    for key, value in data.items():
        # Don't overwrite an existing API key with empty string
        if "api_key" in key and value == "":
            continue
        # Don't save masked values back
        if isinstance(value, str) and value.startswith("••••"):
            continue
        updates[key] = str(value)

    if updates:
        db.write_many(updates)
        logger.info(f"[Settings] Saved {len(updates)} settings: {list(updates.keys())}")

    return JSONResponse({
        "status":  "ok",
        "saved":   len(updates),
        "provider_info": db.provider_info(),
    })


@router.post("/api/settings/test-llm")
async def test_llm():
    """Test the currently configured LLM with a simple ping."""
    try:
        from core import llm as LLM
        info = LLM.provider_info()

        response = LLM.chat(
            messages=[{"role": "user", "content": "Reply with exactly: OK"}],
            max_tokens=10,
            temperature=0.0,
        )
        return JSONResponse({
            "status":   "ok",
            "provider": info["provider"],
            "model":    info["model"],
            "response": response.strip(),
        })
    except Exception as e:
        return JSONResponse({
            "status":  "error",
            "error":   str(e),
        }, status_code=400)
