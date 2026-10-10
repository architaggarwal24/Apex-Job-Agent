# server/routes/review.py
"""
server/routes/review.py — structured review of an application before it is
submitted: see every field the agent filled (with where each value came from,
how sure it is and what to double-check), correct values, then approve or
reject.

    GET  /api/applications/{id}/review          the review packet
    POST /api/applications/{id}/review/edit     {field_id, value, remember}
    POST /api/applications/{id}/review/approve  submit using the reviewed values
    POST /api/applications/{id}/review/reject   {reason} -> skipped

Everything is per-user: the packet, the job and the answer bank are looked up
through the caller's own user id, so someone else's application is simply not
found. Approving drives the browser, so it is owner-only like /api/apply/*; it
does not re-implement submitting - it hands over to the existing submit
handler, which holds the per-job lock, honours dry_run and enforces the daily
limit.
"""

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import JSONResponse

from applier import review
from applier.answers import parse_remember
from core.auth import get_current_user_id, require_owner
from db import lifecycle
from db.answer_bank_db import AnswerBank
from db.jobs_db import JobsDB
from db.review_db import ReviewStore
from server.routes import apply as apply_routes

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_REASON_CHARS = 500


def _job(job_id: str, uid: int) -> dict:
    job = JobsDB(user_id=uid).get_by_id(job_id)
    if not job:
        raise HTTPException(404, "Application not found")
    return job


def _packet(job_id: str, uid: int) -> dict:
    packet = ReviewStore(uid).get(job_id)
    if packet is None:
        raise HTTPException(404, "No review packet for this application")
    return packet


def _require_review_stage(job: dict):
    if job["stage"] != "ready_for_review":
        raise HTTPException(409, f"Not in review (this application is {job['stage']})")


def _view(packet: dict, job: dict) -> dict:
    out = review.public(packet)
    out["stage"] = job["stage"]
    out["title"], out["company"] = job["title"], job["company"]
    out["screenshot_urls"] = [f"/screenshots/{name}" for name in packet.get("screenshots", [])]
    return out


@router.get("/api/applications/{job_id}/review")
async def get_review(job_id: str, uid: int = Depends(get_current_user_id)):
    job = _job(job_id, uid)
    return JSONResponse(_view(_packet(job_id, uid), job))


@router.post("/api/applications/{job_id}/review/edit")
async def edit_field(job_id: str, data: dict, uid: int = Depends(get_current_user_id)):
    """Body {field_id, value, remember}. Correct one value. It becomes yours
    (source user, confidence 1.0) and, unless `remember` says "once", it is
    saved to your answer bank the way an answer given during the application
    is (default auto: global, or for this company if the question names it or is
    long free text; or global / company / once). 400 for a bad value or flag,
    404 unknown application / no packet / no such field, 409 not in review or
    busy."""
    job = _job(job_id, uid)
    field_id = data.get("field_id")
    if not isinstance(field_id, str) or not field_id.strip():
        raise HTTPException(400, "field_id is required")
    value = data.get("value")
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise HTTPException(400, "value must be text")
    try:
        mode = parse_remember(data.get("remember"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    _packet(job_id, uid)
    _require_review_stage(job)
    if not apply_routes._acquire(job_id):
        raise HTTPException(409, "An operation is already running for this application")
    try:
        try:
            packet, field, remembered = review.edit_and_remember(
                ReviewStore(uid), AnswerBank(uid), job, field_id.strip(), str(value), mode)
        except KeyError:                      # (a LookupError too: this one must come first)
            raise HTTPException(404, "No such field in the review")
        except review.NoReviewPacket:
            raise HTTPException(404, "No review packet for this application")
        except ValueError as e:
            raise HTTPException(400, str(e))
        try:
            JobsDB(user_id=uid).record_step(job_id, "review_edit", f"Edited \"{field['label']}\"",
                                            {"field_id": field["id"], "remember": mode})
        except Exception:
            logger.exception("[Review] could not record the edit")
    finally:
        apply_routes._release(job_id)
    return JSONResponse({"field": field, "remembered": remembered,
                         "flagged": sum(1 for f in packet["fields"] if f["flagged"])})


@router.post("/api/applications/{job_id}/review/approve")
async def approve(job_id: str, background_tasks: BackgroundTasks,
                  uid: int = Depends(require_owner)):
    """Submit the application using the REVIEWED values (Orchestrator.submit
    fills from the packet, not from a fresh analysis). dry_run and the daily
    limit apply exactly as for /api/apply/submit, whose handler this calls:
    a dry run fills and screenshots but never clicks submit, and a real
    submission past the daily limit is a 429."""
    job = _job(job_id, uid)
    _packet(job_id, uid)
    _require_review_stage(job)
    await apply_routes.submit(job_id, {}, background_tasks, uid)
    dry_run = apply_routes._settings_dry_run()
    try:
        JobsDB(user_id=uid).record_step(job_id, "review_approved", "Approved in review",
                                        {"dry_run": dry_run})
    except Exception:
        logger.exception("[Review] could not record the approval")
    return JSONResponse({"status": "submitting", "dry_run": dry_run})


@router.post("/api/applications/{job_id}/review/reject")
async def reject(job_id: str, data: dict, uid: int = Depends(get_current_user_id)):
    """Body {reason} (optional). Skip this application: ready_for_review ->
    skipped, with the reason on the timeline."""
    job = _job(job_id, uid)
    reason = data.get("reason") if isinstance(data, dict) else None
    if reason is not None and not isinstance(reason, str):
        raise HTTPException(400, "reason must be text")
    reason = (reason or "").strip()[:MAX_REASON_CHARS] or "Rejected in review"
    _packet(job_id, uid)
    _require_review_stage(job)
    if not apply_routes._acquire(job_id):
        raise HTTPException(409, "An operation is already running for this application")
    try:
        try:
            review.reject_application(JobsDB(user_id=uid), job_id, reason)
        except lifecycle.IllegalTransition as e:
            raise HTTPException(409, str(e))
    finally:
        apply_routes._release(job_id)
    return JSONResponse({"status": "skipped", "reason": reason})
