"""
server/routes/tracking.py — Application pipeline tracking + analytics.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from core.auth import get_current_user_id
from db.jobs_db import JobsDB, PIPELINE_STAGES
from db.lifecycle import IllegalTransition

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/tracking/pipeline")
async def get_pipeline(uid: int = Depends(get_current_user_id)):
    """Return jobs grouped by pipeline stage for Kanban view."""
    return JSONResponse(JobsDB(user_id=uid).get_pipeline())


@router.post("/api/tracking/stage/{job_id}")
async def update_stage(job_id: str, data: dict,
                       uid: int = Depends(get_current_user_id)):
    stage = data.get("stage", "")
    note  = str(data.get("note") or "")
    if stage not in PIPELINE_STAGES:
        raise HTTPException(400, f"Invalid stage. Valid: {PIPELINE_STAGES}")
    db = JobsDB(user_id=uid)
    if not db.get_by_id(job_id):
        raise HTTPException(404, "Job not found")
    try:
        db.add_stage(job_id, stage, note)
    except IllegalTransition as e:
        raise HTTPException(409, str(e))
    return JSONResponse({"status": "ok", "stage": stage})


@router.post("/api/tracking/note/{job_id}")
async def add_note(job_id: str, data: dict,
                   uid: int = Depends(get_current_user_id)):
    note = data.get("note", "").strip()
    if not note:
        raise HTTPException(400, "Note cannot be empty")
    db = JobsDB(user_id=uid)
    if not db.get_by_id(job_id):
        raise HTTPException(404, "Job not found")
    db.add_note(job_id, note)
    return JSONResponse({"status": "ok"})


@router.get("/api/tracking/analytics")
async def get_analytics(uid: int = Depends(get_current_user_id)):
    """
    Return analytics data for charts:
    - Applications per day (last 30 days)
    - Stage conversion funnel
    - Top companies
    - Score distribution

    Backed by JobsDB.get_analytics(), which uses 6 GROUP BY queries
    instead of the ~72 individual per-day queries this previously ran.
    """
    return JSONResponse(JobsDB(user_id=uid).get_analytics())
