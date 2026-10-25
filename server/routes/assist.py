# server/routes/assist.py
"""
server/routes/assist.py — assist mode: the agent prepares, you click Submit.

    GET  /api/applications/{id}/pack             the answer pack: ordered, copy-ready
                                                 fields (profile, answer bank, kit)
                                                 and the job's URL
    POST /api/applications/{id}/mark-applied     {note}    you submitted it -> submitted
    POST /api/applications/{id}/mark-unavailable {reason}  it cannot be applied to -> skipped

Both actions are recorded as YOU (actor "user") with your note or reason on the
event. See applier/apply_mode.py for the setting that puts an application in
assist mode, applier/pack.py for what is in a pack and in what order, and
applier/assist.py for the two actions.

Everything is per user: the application is looked up through the caller's own
user id, so someone else's is simply not found (404), and the pack is built from
the caller's own bank, kit and resumes (the profile only for the profile's
owner). These only read the stored data or record a decision of yours - they
never open a browser or the site - so they are not owner-only.
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from applier import assist
from applier.pack import build_pack, owner_profile
from core.auth import get_current_user_id
from db import lifecycle
from db.jobs_db import JobsDB

router = APIRouter()


def _job(job_id: str, uid: int) -> dict:
    job = JobsDB(user_id=uid).get_by_id(job_id)
    if not job:
        raise HTTPException(404, "Application not found")
    return job


@router.get("/api/applications/{job_id}/pack")
async def get_pack(job_id: str, uid: int = Depends(get_current_user_id)):
    return JSONResponse(build_pack(_job(job_id, uid), profile=owner_profile(uid)))


@router.post("/api/applications/{job_id}/mark-applied")
async def mark_applied(job_id: str, data: dict | None = None, uid: int = Depends(get_current_user_id)):
    return _record(assist.mark_applied, job_id, uid, (data or {}).get("note"), "submitted")


@router.post("/api/applications/{job_id}/mark-unavailable")
async def mark_unavailable(job_id: str, data: dict | None = None, uid: int = Depends(get_current_user_id)):
    return _record(assist.mark_unavailable, job_id, uid, (data or {}).get("reason"), "skipped")


def _record(action, job_id: str, uid: int, text, stage: str):
    try:
        event = action(JobsDB(user_id=uid), job_id, text)
    except LookupError:
        raise HTTPException(404, "Application not found")
    except lifecycle.IllegalTransition as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse({"status": stage, "event": event})
