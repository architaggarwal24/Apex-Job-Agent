"""
server/routes/tracking.py — Application notes + analytics.
"""

import logging
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from core.auth import get_current_user_id
from db.jobs_db import JobsDB

logger = logging.getLogger(__name__)
router = APIRouter()


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
