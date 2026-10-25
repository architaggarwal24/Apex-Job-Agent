# server/routes/kits.py
"""
server/routes/kits.py — resume variants and per-application kits.

    POST   /api/resumes                   upload a PDF (multipart: file, label, tags, make_default)
    GET    /api/resumes                   this user's resumes (never the file or its text)
    PUT    /api/resumes/{id}/default      make it the default
    DELETE /api/resumes/{id}              delete it and its encrypted file

    GET    /api/kits/{job_id}             the current kit and every version
    PUT    /api/kits/{job_id}/resume      {"variant_id": n}: use this resume for the
                                          application (a new version)        (owner only)
    POST   /api/kits/{job_id}/regenerate  write the kit again, a new version; {"reselect": true}
                                          also drops a resume you chose        (owner only)

Everything is per user: each handler builds its stores from the session's
user, so another user's resume, job or kit is simply not found. The resume
library is each user's own data; changing which resume an application uploads
and spending LLM calls on a new kit are automation, so those two are owner-only.
Upload limits and the PDF check are db/resumes_db.py's: 413 for a file over
5 MB, 400 for anything that is not an acceptable PDF.
"""

import asyncio

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from applier import kit as kits
from core.auth import get_current_user_id, require_owner
from core.config import cfg
from core.llm import run_blocking
from db.jobs_db import JobsDB
from db.kits_db import KitStore
from db.resumes_db import MAX_BYTES, ResumeError, ResumeStore

router = APIRouter()

_OVERHEAD = 64 * 1024            # multipart framing allowed on top of the file itself


def _job_or_404(uid: int, job_id: str) -> dict:
    job = JobsDB(user_id=uid).get_by_id(job_id)
    if job is None:
        raise HTTPException(404, "Job not found")
    return job


@router.post("/api/resumes")
async def upload_resume(request: Request, file: UploadFile = File(...), label: str = Form(...),
                        tags: str = Form(""), make_default: bool = Form(False),
                        uid: int = Depends(get_current_user_id)):
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BYTES + _OVERHEAD:
        raise HTTPException(413, f"The file is larger than {MAX_BYTES // (1024 * 1024)} MB")
    data = await file.read(MAX_BYTES + 1)                  # one byte over is enough to know it is too big
    try:
        variant = await asyncio.to_thread(ResumeStore(uid).add, file.filename or "", data, label, tags, make_default)
    except ResumeError as e:
        raise HTTPException(e.status, str(e))
    return JSONResponse(variant)


@router.get("/api/resumes")
async def list_resumes(uid: int = Depends(get_current_user_id)):
    return JSONResponse({"resumes": ResumeStore(uid).list()})


@router.put("/api/resumes/{variant_id}/default")
async def set_default(variant_id: int, uid: int = Depends(get_current_user_id)):
    variant = ResumeStore(uid).set_default(variant_id)
    if variant is None:
        raise HTTPException(404, "Resume not found")
    return JSONResponse(variant)


@router.delete("/api/resumes/{variant_id}")
async def delete_resume(variant_id: int, uid: int = Depends(get_current_user_id)):
    if not ResumeStore(uid).delete(variant_id):
        raise HTTPException(404, "Resume not found")
    return JSONResponse({"status": "deleted"})


@router.get("/api/kits/{job_id}")
async def get_kit(job_id: str, uid: int = Depends(get_current_user_id)):
    _job_or_404(uid, job_id)
    store = KitStore(uid)
    return JSONResponse({"current": store.current(job_id), "versions": store.versions(job_id)})


@router.put("/api/kits/{job_id}/resume")
async def choose_resume(job_id: str, data: dict, uid: int = Depends(require_owner)):
    variant_id = data.get("variant_id")
    if isinstance(variant_id, bool) or not isinstance(variant_id, int):
        raise HTTPException(400, "variant_id must be a whole number")
    try:
        kit = kits.override_resume(job_id, uid, variant_id)
    except ValueError as e:
        raise HTTPException(404, str(e).capitalize())
    return JSONResponse(kit)


@router.post("/api/kits/{job_id}/regenerate")
async def regenerate(job_id: str, data: dict = None, uid: int = Depends(require_owner)):
    job = _job_or_404(uid, job_id)
    from db.profile_db import ProfileDB
    profile = ProfileDB().summary()
    kit = await run_blocking(kits.prepare_kit, job, path=cfg.JOBS_DB_PATH, profile=profile, reuse=False,
                             reselect=bool((data or {}).get("reselect")), created_by="user")
    if kit is None:
        raise HTTPException(500, "Could not prepare the kit")
    return JSONResponse(kit)
