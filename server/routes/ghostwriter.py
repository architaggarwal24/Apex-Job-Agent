"""
server/routes/ghostwriter.py — AI assistant API routes.
"""

import logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from db.jobs_db    import JobsDB
from db.profile_db import ProfileDB

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_job_and_profile(job_id: str):
    db  = JobsDB()
    job = db.get_by_id(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    profile = ProfileDB().summary()
    return job, profile, db


@router.post("/api/ghostwriter/cover-letter/{job_id}")
async def cover_letter(job_id: str, data: dict = None):
    """Generate a tailored cover letter for this job."""
    job, profile, db = _get_job_and_profile(job_id)
    tone = (data or {}).get("tone", "professional")

    from intelligence.ghostwriter import cover_letter as gw_cl
    letter = gw_cl(job, profile, jobs_db=db, tone=tone)
    if not letter:
        raise HTTPException(500, "Cover letter generation failed")
    return JSONResponse({"cover_letter": letter})


@router.post("/api/ghostwriter/interview-prep/{job_id}")
async def interview_prep(job_id: str):
    """Generate likely interview questions + suggested answers."""
    job, profile, db = _get_job_and_profile(job_id)

    from intelligence.ghostwriter import interview_prep as gw_prep
    result = gw_prep(job, profile, jobs_db=db)
    return JSONResponse(result)


@router.post("/api/ghostwriter/chat/{job_id}")
async def chat(job_id: str, data: dict):
    """Free-form chat with job + profile context."""
    message = data.get("message", "").strip()
    history = data.get("history", [])

    if not message:
        raise HTTPException(400, "Message cannot be empty")

    job, profile, db = _get_job_and_profile(job_id)

    from intelligence.ghostwriter import chat as gw_chat
    response = gw_chat(message, job, profile, history=history, jobs_db=db)
    return JSONResponse({"response": response})


@router.get("/api/ghostwriter/context/{job_id}")
async def get_context(job_id: str):
    """Return the full context that the ghostwriter uses for this job."""
    job, profile, db = _get_job_and_profile(job_id)

    from intelligence.ghostwriter import build_context
    ctx = build_context(job, profile, jobs_db=db)
    return JSONResponse({"context": ctx})
