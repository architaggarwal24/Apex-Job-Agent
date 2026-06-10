"""
server/routes/jobs.py — Job listing, stats, pipeline API.
"""

import asyncio
import logging
from fastapi import APIRouter, BackgroundTasks
from fastapi.responses import JSONResponse

from db.jobs_db import JobsDB

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/jobs")
async def get_jobs(status: str = "", limit: int = 500):
    db = JobsDB()
    jobs = db.get_all(limit=limit, status=status)
    return JSONResponse(jobs)


@router.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    db  = JobsDB()
    job = db.get_by_id(job_id)
    if not job:
        from fastapi import HTTPException
        raise HTTPException(404, "Job not found")
    return JSONResponse(job)


@router.get("/api/stats")
async def get_stats():
    db    = JobsDB()
    stats = db.get_stats()

    from applier.session import ApplicationSession
    sessions = ApplicationSession.load_all()
    stats["sessions"] = {
        "awaiting_input":    sum(1 for s in sessions if s.state == "awaiting_input"),
        "screenshot_review": sum(1 for s in sessions if s.state == "screenshot_review"),
        "in_progress":       sum(1 for s in sessions
                                 if s.state not in ("done","failed","skipped","idle")),
        "done":              sum(1 for s in sessions if s.state == "done"),
    }
    return JSONResponse(stats)


@router.post("/api/pipeline/run")
async def run_pipeline(data: dict = None, background_tasks: BackgroundTasks = None):
    quick    = (data or {}).get("quick", False)
    platforms= (data or {}).get("platforms", None)

    def _run():
        from tracking.pipeline import run
        try:
            run(platforms=platforms, quick_mode=quick)
        except Exception as e:
            logger.error(f"Pipeline error: {e}")

    if background_tasks:
        background_tasks.add_task(_run_in_thread, _run)
    return JSONResponse({"status": "started", "quick": quick})


@router.post("/api/jobs/{job_id}/score")
async def rescore_job(job_id: str):
    """Manually trigger AI scoring for a specific job."""
    db  = JobsDB()
    job = db.get_by_id(job_id)
    if not job:
        from fastapi import HTTPException
        raise HTTPException(404, "Job not found")

    from db.profile_db import ProfileDB
    from intelligence.scorer import score_job
    profile = ProfileDB().summary()
    result  = score_job(job, profile)
    db.update_score(job_id, result["score"], result["reason"])
    return JSONResponse(result)


@router.post("/api/jobs/{job_id}/brief")
async def regenerate_brief(job_id: str):
    """Manually trigger brief + resume tailoring for a job."""
    db  = JobsDB()
    job = db.get_by_id(job_id)
    if not job:
        from fastapi import HTTPException
        raise HTTPException(404, "Job not found")

    from db.profile_db import ProfileDB
    from intelligence.briefer import generate_and_save
    profile = ProfileDB().summary()
    result  = generate_and_save(job, profile, db)
    return JSONResponse(result)


@router.delete("/api/jobs/duplicates")
async def purge_duplicates():
    deleted = JobsDB().purge_duplicates()
    return JSONResponse({"deleted": deleted})


async def _run_in_thread(fn):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, fn)
