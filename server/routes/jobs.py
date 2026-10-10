"""
server/routes/jobs.py — Job listing, stats, pipeline API.
"""

import logging
from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import JSONResponse

from core.auth import get_current_user_id, require_owner
from db.jobs_db import JobsDB

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/jobs")
async def get_jobs(status: str = "", limit: int = 500,
                   uid: int = Depends(get_current_user_id)):
    db = JobsDB(user_id=uid)
    jobs = db.get_all(limit=limit, status=status)
    return JSONResponse(jobs)


@router.get("/api/jobs/{job_id}")
async def get_job(job_id: str, uid: int = Depends(get_current_user_id)):
    db  = JobsDB(user_id=uid)
    job = db.get_by_id(job_id)
    if not job:
        from fastapi import HTTPException
        raise HTTPException(404, "Job not found")
    return JSONResponse(job)


@router.get("/api/stats")
async def get_stats(uid: int = Depends(get_current_user_id)):
    db    = JobsDB(user_id=uid)
    stats = db.get_stats()

    from applier.session import ApplicationSession
    # Session files are global; count only the ones for this user's jobs.
    mine     = db.job_ids()
    sessions = [s for s in ApplicationSession.load_all() if s.job_id in mine]
    stats["sessions"] = {
        "awaiting_input":    sum(1 for s in sessions if s.state == "awaiting_input"),
        "screenshot_review": sum(1 for s in sessions if s.state == "screenshot_review"),
        "in_progress":       sum(1 for s in sessions
                                 if s.state not in ("done","failed","skipped","idle")),
        "done":              sum(1 for s in sessions if s.state == "done"),
    }
    return JSONResponse(stats)


@router.post("/api/pipeline/run")
async def run_pipeline(data: dict = None, background_tasks: BackgroundTasks = None,
                       uid: int = Depends(require_owner)):
    quick    = (data or {}).get("quick", False)
    platforms= (data or {}).get("platforms", None)

    import threading
    from core.shutdown import (reset_pipeline_cancel, combined_cancel_event,
                               register_thread)

    # Reset any previous cancel request — a Cancel click for a prior
    # run must not silently disable this new run.
    reset_pipeline_cancel()

    def _run():
        from tracking.pipeline import run
        try:
            run(platforms=platforms, quick_mode=quick,
                cancel_event=combined_cancel_event)
        except Exception as e:
            logger.error(f"Pipeline error: {e}")

    if background_tasks:
        t = threading.Thread(target=_run, daemon=True, name="pipeline-run")
        background_tasks.add_task(_start_thread, t, register_thread)
    return JSONResponse({"status": "started", "quick": quick})


@router.post("/api/pipeline/cancel")
async def cancel_pipeline(uid: int = Depends(require_owner)):
    """
    Signal the CURRENTLY RUNNING pipeline to stop ASAP.
    This only affects the in-progress run — it's reset automatically
    at the start of the next /api/pipeline/run call, so it does not
    permanently disable future pipeline runs.
    """
    from core.shutdown import request_pipeline_cancel
    request_pipeline_cancel()
    return JSONResponse({"status": "cancel_requested"})


@router.post("/api/jobs/{job_id}/score")
async def rescore_job(job_id: str, uid: int = Depends(get_current_user_id)):
    """
    Manually trigger AI scoring for a specific job.

    score_job() makes a blocking LLM call internally. Run it via
    run_blocking() so this doesn't freeze the event loop for other
    requests/WebSocket connections — observed LLM latency on some
    providers (NVIDIA NIM) is 60-90s per call.
    """
    db  = JobsDB(user_id=uid)
    job = db.get_by_id(job_id)
    if not job:
        from fastapi import HTTPException
        raise HTTPException(404, "Job not found")

    from db.profile_db import ProfileDB
    from intelligence.scorer import score_job
    from core.llm import run_blocking
    import asyncio

    profile = ProfileDB().summary()
    try:
        result = await run_blocking(score_job, job, profile)
    except asyncio.TimeoutError:
        from fastapi import HTTPException
        raise HTTPException(504, "Scoring timed out — the configured LLM provider "
                                  "took too long to respond")
    db.update_score(job_id, result["score"], result["reason"])
    return JSONResponse(result)


@router.post("/api/jobs/{job_id}/brief")
async def regenerate_brief(job_id: str, uid: int = Depends(get_current_user_id)):
    """
    Manually trigger brief + resume tailoring for a job.
    Makes blocking LLM calls internally — see rescore_job() above.
    """
    db  = JobsDB(user_id=uid)
    job = db.get_by_id(job_id)
    if not job:
        from fastapi import HTTPException
        raise HTTPException(404, "Job not found")

    from db.profile_db import ProfileDB
    from intelligence.briefer import generate_and_save
    from core.llm import run_blocking
    import asyncio

    profile = ProfileDB().summary()
    try:
        result = await run_blocking(generate_and_save, job, profile, db)
    except asyncio.TimeoutError:
        from fastapi import HTTPException
        raise HTTPException(504, "Brief generation timed out — the configured "
                                  "LLM provider took too long to respond")
    return JSONResponse(result)


@router.delete("/api/jobs/duplicates")
async def purge_duplicates(uid: int = Depends(get_current_user_id)):
    deleted = JobsDB(user_id=uid).purge_duplicates()
    return JSONResponse({"deleted": deleted})


@router.delete("/api/jobs")
async def clear_all_jobs(include_applied: bool = False,
                         uid: int = Depends(get_current_user_id)):
    """
    Clear scraped jobs so the Jobs tab (and its title filter) starts
    fresh before the next pipeline run. Applied/in-progress jobs are
    kept by default — see JobsDB.clear_all() — pass ?include_applied=true
    for a full wipe including Pipeline history.
    """
    deleted = JobsDB(user_id=uid).clear_all(include_applied=include_applied)
    return JSONResponse({"deleted": deleted, "include_applied": include_applied})


async def _start_thread(t, register_fn):
    """Start a background thread, then register it (after start() so
    is_alive() is True and the registry doesn't immediately prune it)."""
    t.start()
    register_fn(t)
