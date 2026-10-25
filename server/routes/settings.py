"""
server/routes/settings.py — Settings API + page route.
"""

import asyncio
import json
import logging
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from applier import apply_mode, executors
from core.auth import require_owner
from core.urlcheck import validate_base_url
from db import hunt
from db.settings_db import (
    DEFAULT_PROVIDER, SettingsDB, PROVIDERS, is_secret_key, looks_like_placeholder, normalize_model_id,
)

logger = logging.getLogger(__name__)
router = APIRouter()
ROOT   = __import__("pathlib").Path(__file__).resolve().parent.parent.parent


def _t(name: str) -> str:
    p = ROOT / "templates" / name
    return p.read_text(encoding="utf-8") if p.exists() else f"<h1>Missing: {name}</h1>"


# GET /settings is the Next.js app's page now — see server/static_shell.py.


@router.get("/api/settings")
async def get_settings():
    db   = SettingsDB.get()
    data = db.read_all()
    masked = {}
    for k, v in data.items():
        if is_secret_key(k) and v:
            masked[k] = "••••••••" + v[-4:] if len(v) > 4 else "••••"
        else:
            masked[k] = v
    return JSONResponse({"settings": masked, "provider_info": db.provider_info()})


@router.get("/api/settings/providers")
async def get_providers():
    """Built-in providers + the user's custom ones, each with the user's own
    added model names and a `configured` flag (is a real key set?) — the
    Providers screen renders straight from this."""
    return JSONResponse(SettingsDB.get().all_providers())


@router.get("/api/settings/raw/{key}")
async def get_setting_raw(key: str):
    """
    SECURITY: This endpoint intentionally does NOT return API key values,
    even unmasked-key names won't be returned in plaintext. It previously
    returned any setting value verbatim, which combined with permissive
    CORS would let any origin exfiltrate API keys. Non-secret settings
    can be read via GET /api/settings instead.
    """
    if is_secret_key(key):
        raise HTTPException(403, "API key values cannot be read back via this endpoint")
    val = SettingsDB.get().read(key, "")
    return JSONResponse({"key": key, "value": val})


# Settings the generic save must not touch: they have their own endpoints that
# validate them (an unchecked base URL or a hand-written provider list would
# bypass the SSRF check).
_MANAGED_KEYS = ("custom_providers", "queue_paused")      # the latter: /api/queue/pause|resume
_MANAGED_PREFIXES = ("extra_models_", "notify_")          # the latter: /api/notifications/settings


@router.post("/api/settings")
async def save_settings(data: dict, _owner: int = Depends(require_owner)):
    db      = SettingsDB.get()
    updates = {}
    for key, value in data.items():
        if key in _MANAGED_KEYS or key.startswith(_MANAGED_PREFIXES):
            raise HTTPException(400, f"'{key}' is managed through its own endpoint")
        if key == "apply_mode_overrides" and isinstance(value, dict):
            value = json.dumps(value)                  # an object is fine; it is stored as JSON text
        if not isinstance(value, (str, int, float, bool)):
            raise HTTPException(400, f"Invalid value for '{key}'")
        value = str(value)
        if is_secret_key(key):
            if value == "" or value.startswith("••••"):
                continue                      # blank / still-masked = "leave it alone"
            if looks_like_placeholder(value):
                raise HTTPException(400, "That looks like placeholder text, not a real API key")
        elif value.startswith("••••"):
            continue
        try:
            if key.endswith("_base_url") and value:
                value = validate_base_url(value)
            elif key.endswith("_model") and value:
                value = normalize_model_id(value)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if key == "quality_first":
            value = value.strip().lower()
            if value not in ("true", "false"):
                raise HTTPException(400, "quality_first must be true or false")
        if key == "apply_mode":
            try:
                value = apply_mode.clean_mode(value)
            except ValueError as e:
                raise HTTPException(400, str(e))
        if key == "executor":
            try:
                value = executors.clean_mode(value)
            except ValueError as e:
                raise HTTPException(400, str(e))
        if key == "apply_mode_overrides":
            try:
                value = apply_mode.dump_overrides(apply_mode.parse_overrides(value))
            except ValueError as e:
                raise HTTPException(400, str(e))
        if key in hunt.STALE_KEYS.values():
            try:
                value = hunt.clean_stale_days(key, value)
            except ValueError as e:
                raise HTTPException(400, str(e))
        if key == "min_salary_lpa":
            value = value.strip()
            try:
                lpa = float(value) if value else 0.0
            except ValueError:
                lpa = -1.0
            if not 0 <= lpa <= 10000:
                raise HTTPException(400, "min_salary_lpa must be a number of lakhs per "
                                         "annum, 0 or more (0 turns the check off)")
            value = f"{lpa:g}"
        if key == "company_blacklist":
            lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
            value = "\n".join(l.strip() for l in lines if l.strip())
            if len(value) > 5000:
                raise HTTPException(400, "company_blacklist is too long (5000 characters at most)")
        if key == "llm_provider" and not db.known_provider(value.lower().strip()):
            raise HTTPException(400, "Unknown provider")
        updates[key] = value
    # Nothing is written unless EVERYTHING validated — no half-saved settings.
    if updates:
        db.write_many(updates)
        logger.info(f"[Settings] Saved {len(updates)} keys")
    return JSONResponse({"status": "ok", "saved": len(updates),
                         "provider_info": db.provider_info()})


# ── Custom OpenAI-compatible providers ───────────────────────────────────────

class CustomProviderIn(BaseModel):
    label:    str
    base_url: str
    api_key:  str = ""
    model:    str = ""
    models:   list[str] = Field(default_factory=list, max_length=100)


class CustomProviderPatch(BaseModel):
    label:    str | None = None
    base_url: str | None = None


@router.post("/api/settings/custom-providers")
async def add_custom_provider(body: CustomProviderIn, _owner: int = Depends(require_owner)):
    try:
        entry = SettingsDB.get().add_custom_provider(
            body.label, body.base_url, body.api_key, body.model, body.models)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse(entry)


@router.patch("/api/settings/custom-providers/{pid}")
async def update_custom_provider(pid: str, body: CustomProviderPatch,
                                 _owner: int = Depends(require_owner)):
    try:
        return JSONResponse(SettingsDB.get().update_custom_provider(pid, body.label, body.base_url))
    except LookupError:
        raise HTTPException(404, "Provider not found")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.delete("/api/settings/custom-providers/{pid}")
async def delete_custom_provider(pid: str, _owner: int = Depends(require_owner)):
    try:
        SettingsDB.get().delete_custom_provider(pid)
    except LookupError:
        raise HTTPException(404, "Provider not found")
    return JSONResponse({"status": "ok", "provider_info": SettingsDB.get().provider_info()})


# ── User-added model names (any provider) ────────────────────────────────────

class ModelIn(BaseModel):
    provider: str
    model:    str


@router.post("/api/settings/models")
async def add_model(body: ModelIn, _owner: int = Depends(require_owner)):
    try:
        return JSONResponse({"extra_models": SettingsDB.get().add_extra_model(body.provider, body.model)})
    except LookupError:
        raise HTTPException(404, "Unknown provider")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/settings/models/remove")
async def remove_model(body: ModelIn, _owner: int = Depends(require_owner)):
    try:
        return JSONResponse({"extra_models": SettingsDB.get().remove_extra_model(body.provider, body.model)})
    except LookupError:
        raise HTTPException(404, "Unknown provider")


# ── Test a provider / discover its models — using what's ON SCREEN ───────────

class ProbeIn(BaseModel):
    """What the Providers screen currently shows. Every field is optional; a
    blank or still-masked api_key means "use the saved one"."""
    provider: str | None = None
    model:    str | None = None
    api_key:  str | None = None
    base_url: str | None = None


def _candidate_config(body: ProbeIn) -> dict:
    db  = SettingsDB.get()
    pid = (body.provider or db.read("llm_provider", DEFAULT_PROVIDER)).lower().strip()
    if not db.known_provider(pid):
        raise HTTPException(404, "Unknown provider")
    cfg = db.llm_config(pid)
    try:
        if body.model:
            cfg["model"] = normalize_model_id(body.model)
        if body.base_url and cfg["type"] == "openai_compat":
            cfg["base_url"] = validate_base_url(body.base_url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    key = (body.api_key or "").strip()
    if key and not key.startswith("••••") and not looks_like_placeholder(key):
        cfg["api_key"] = key
    return cfg


async def _run_blocking(fn, cfg: dict, timeout: float = 35.0):
    """Blocking SDK call in a worker thread, so a slow provider can't freeze the
    event loop; LLMTestError already carries a message fit for the user."""
    from core.llm import LLMTestError
    try:
        return await asyncio.wait_for(
            asyncio.get_running_loop().run_in_executor(None, fn, cfg), timeout=timeout)
    except LLMTestError as e:
        raise HTTPException(400, str(e))
    except asyncio.TimeoutError:
        raise HTTPException(408, "Timed out waiting for the provider. Check the URL and your connection.")


@router.post("/api/settings/test-llm")
async def test_llm(body: ProbeIn | None = None, _owner: int = Depends(require_owner)):
    """Round-trip test of a provider using the values currently entered in the
    form — saved or not. (Before, this always tested whatever was SAVED, so
    "Test connection" on an edited-but-unsaved card tested the wrong thing.)"""
    from core import llm as LLM
    cfg = _candidate_config(body or ProbeIn())
    result = await _run_blocking(LLM.test_connection, cfg)
    return JSONResponse({"status": "ok", "provider": cfg["provider"],
                         "label": cfg.get("label", cfg["provider"]), "model": cfg["model"], **result})


@router.post("/api/settings/fetch-models")
async def fetch_models(body: ProbeIn, _owner: int = Depends(require_owner)):
    """Ask the provider which model ids it offers (GET {base_url}/models)."""
    from core import llm as LLM
    cfg = _candidate_config(body)
    if not cfg.get("api_key"):
        raise HTTPException(400, "Enter the API key first — most providers only list models for a valid key.")
    models = await _run_blocking(LLM.list_models, cfg)
    return JSONResponse({"models": models})


# ── Login status ──────────────────────────────────────────────────────────────

@router.get("/api/settings/login-status")
async def login_status():
    """
    Check cookie freshness for LinkedIn and Indeed.
    Does NOT launch a browser — just checks if saved cookies exist and are recent.
    """
    from pathlib import Path
    import json, time
    from core.config import cfg

    def check_cookies(path: Path, domain: str) -> dict:
        if not path.exists():
            return {"status": "not_logged_in", "message": "No saved session"}
        try:
            # Cookie files are encrypted at rest (core/browser.py's
            # save_cookies) — decrypt_bytes() passes pre-existing
            # plaintext files through unchanged, so this reads both old
            # and new files correctly. Reading with plain json.loads()
            # here (as this used to) would fail on every encrypted file
            # and silently report every real session as "unknown".
            from core.crypto import decrypt_bytes
            cookies = json.loads(decrypt_bytes(path.read_bytes()).decode())
            domain_cookies = [c for c in cookies if domain in c.get("domain","")]
            if not domain_cookies:
                return {"status": "not_logged_in", "message": "No session cookies"}
            # Check expiry on session cookies
            now = time.time()
            valid = [c for c in domain_cookies
                     if c.get("expires", now + 1) > now or c.get("expires", -1) == -1]
            if not valid:
                return {"status": "expired", "message": "Session expired — will re-login on next run"}
            mod_time = path.stat().st_mtime
            hours_ago = (now - mod_time) / 3600
            return {
                "status":  "logged_in",
                "message": f"Session saved {hours_ago:.0f}h ago",
                "count":   len(valid),
            }
        except Exception as e:
            return {"status": "unknown", "message": str(e)}

    li_path     = cfg.COOKIES_DIR / "linkedin.json"
    indeed_path = cfg.COOKIES_DIR / "indeed.json"
    naukri_path = cfg.COOKIES_DIR / "naukri.json"

    return JSONResponse({
        "linkedin": check_cookies(li_path,     "linkedin.com"),
        "indeed":   check_cookies(indeed_path, "indeed.com"),
        "naukri":   check_cookies(naukri_path, "naukri.com"),
    })


@router.post("/api/settings/clear-cookies/{platform}")
async def clear_cookies(platform: str, _owner: int = Depends(require_owner)):
    """Clear saved cookies for a platform (forces re-login on next run)."""
    from core.config import cfg
    paths = {"linkedin": cfg.COOKIES_DIR / "linkedin.json",
             "indeed":   cfg.COOKIES_DIR / "indeed.json",
             "naukri":   cfg.COOKIES_DIR / "naukri.json"}
    path = paths.get(platform)
    if not path:
        return JSONResponse({"error": "Unknown platform"}, status_code=400)
    if path.exists():
        path.unlink()
    return JSONResponse({"status": "ok", "message": f"{platform} session cleared"})
