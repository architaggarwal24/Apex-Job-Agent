# server/routes/applications.py
"""
server/routes/applications.py — the lifecycle API: list/inspect applications,
move one between stages, and stream stage changes live.

Everything is per-user: each handler builds JobsDB(user_id=uid) from the
session, so a job or event belonging to someone else is simply not found.
The id in the paths is the job_id. PUT .../hunt edits the card's hunt fields
(next step, contact, last contact; db/hunt.py).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from core.auth import get_current_user_id
from db import lifecycle
from db.jobs_db import JobsDB
from server import sse

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/applications")
async def list_applications(stage: str = "", source: str = "", title: str = "",
                            q: str = "", limit: int = 500,
                            uid: int = Depends(get_current_user_id)):
    try:
        rows = JobsDB(user_id=uid).list_applications(
            stage=stage, source=source, title=title, q=q, limit=limit)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse(rows)


@router.get("/api/applications/{job_id}")
async def get_application(job_id: str, uid: int = Depends(get_current_user_id)):
    app = JobsDB(user_id=uid).get_application(job_id)
    if app is None:
        raise HTTPException(404, "Application not found")
    return JSONResponse(app)


@router.post("/api/applications/{job_id}/transition")
async def transition_application(job_id: str, data: dict,
                                 uid: int = Depends(get_current_user_id)):
    """Body: {"to": <stage>, "note": <text, optional>, "reason": <needs_you
    reason, optional - defaults to "other" when moving to needs_you>}.
    400 unknown stage/reason, 404 not yours, 409 not a legal move."""
    to = data.get("to")
    if not isinstance(to, str) or to not in lifecycle.STAGES:
        raise HTTPException(400, f"'to' must be one of {list(lifecycle.STAGES)}")
    reason = data.get("reason")
    if to == "needs_you" and not reason:
        reason = "other"
    db = JobsDB(user_id=uid)
    try:
        db.transition(job_id, to, actor="user", reason=reason,
                      note=str(data.get("note") or ""))
    except LookupError:
        raise HTTPException(404, "Application not found")
    except lifecycle.IllegalTransition as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse(db.get_application(job_id))


@router.put("/api/applications/{job_id}/hunt")
async def set_hunt(job_id: str, data: dict, uid: int = Depends(get_current_user_id)):
    """Body: any of next_step_text, next_step_at, contact_name, contact_email,
    last_contact_at (db/hunt.py); "" or null clears one, fields left out are unchanged.
    Returns the application. 400 a bad field, 404 not yours. Setting next_step_at also
    schedules its follow-up nudge."""
    try:
        return JSONResponse(JobsDB(user_id=uid).set_hunt(job_id, data))
    except LookupError:
        raise HTTPException(404, "Application not found")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/api/events/stream")
async def event_stream(request: Request, uid: int = Depends(get_current_user_id)):
    """Server-sent events: this user's stage changes as they happen.
    Resumable: send Last-Event-ID (browsers do this automatically on
    reconnect) to replay everything after that id."""
    gen = sse.stream(JobsDB(user_id=uid),
                     request.headers.get("last-event-id"),
                     is_disconnected=request.is_disconnected)
    return StreamingResponse(gen, media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
