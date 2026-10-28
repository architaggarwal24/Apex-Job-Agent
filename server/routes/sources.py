# server/routes/sources.py
"""
server/routes/sources.py — the login-free job sources and the company boards.

    GET    /api/sources                       every source: label, kind, enabled, last run
    GET    /api/sources/companies             the ATS company boards followed (per user)
    POST   /api/sources/companies             {"vendor", "slug" | "url", "name"?} add one (owner)
    PUT    /api/sources/companies/{id}        {"enabled": bool} (owner)
    DELETE /api/sources/companies/{id}        stop following it (owner)

Which sources are on is the `search_sources` setting (POST /api/settings).
Everything is per user: a company board followed by the owner is the owner's.
Reads need a session; writes are owner-only like every other search setting.
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from core.auth import get_current_user_id, require_owner
from db.settings_db import SettingsDB
from db.sources_db import SourceStore
from scraper import sources as job_sources
from scraper.sources.ats import parse_url

router = APIRouter()


@router.get("/api/sources")
async def list_sources(uid: int = Depends(get_current_user_id)):
    store = SourceStore(uid)
    settings = SettingsDB.get()
    on = set(job_sources.enabled_sources(settings.read))
    runs = store.runs()
    boards = {}
    for c in store.companies():
        boards[c["vendor"]] = boards.get(c["vendor"], 0) + (1 if c["enabled"] else 0)
    return JSONResponse({"sources": [
        {"name": name, "label": meta["label"], "kind": meta["kind"], "enabled": name in on,
         "min_interval": meta["min_interval"], "boards": boards.get(name, 0) if meta["kind"] == "ats" else None,
         "needs_key": meta["needs_key"], "key_set": all((settings.read(k, "") or "").strip() for k in meta["needs_key"]),
         "last_run": runs.get(name)}
        for name, meta in job_sources.SOURCES.items()]})


@router.get("/api/sources/companies")
async def list_companies(uid: int = Depends(get_current_user_id)):
    return JSONResponse({"companies": SourceStore(uid).companies()})


@router.post("/api/sources/companies")
async def add_company(data: dict, uid: int = Depends(require_owner)):
    """Body: {"vendor", "slug"} or {"url": a link to the board or one of its jobs}, plus "name" (optional)."""
    vendor, slug = (data.get("vendor") or "").strip().lower(), (data.get("slug") or "").strip()
    url = (data.get("url") or "").strip()
    if url and not (vendor and slug):
        hit = parse_url(url)
        if hit is None:
            raise HTTPException(400, "That link is not a company board on a supported ATS "
                                     f"({', '.join(job_sources.ATS_LABELS.values())})")
        vendor, slug = hit
    if not (vendor and slug):
        raise HTTPException(400, "Give the ATS vendor and the board slug, or a link to the board")
    try:
        row = SourceStore(uid).add(vendor, slug, data.get("name") or "", origin="user")
    except ValueError as e:
        raise HTTPException(400, str(e))
    if row is None:
        raise HTTPException(409, "You already follow that board")
    return JSONResponse(row, status_code=201)


@router.put("/api/sources/companies/{company_id}")
async def set_company(company_id: int, data: dict, uid: int = Depends(require_owner)):
    enabled = data.get("enabled")
    if not isinstance(enabled, bool):
        raise HTTPException(400, "enabled must be true or false")
    if not SourceStore(uid).set_enabled(company_id, enabled):
        raise HTTPException(404, "Company board not found")
    return JSONResponse({"status": "ok", "enabled": enabled})


@router.delete("/api/sources/companies/{company_id}")
async def remove_company(company_id: int, uid: int = Depends(require_owner)):
    if not SourceStore(uid).remove(company_id):
        raise HTTPException(404, "Company board not found")
    return JSONResponse({"status": "removed"})
