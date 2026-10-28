"""
server/routes/jobs.py — Job details, stats, pipeline API.
"""

import logging
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import JSONResponse

from core.auth import get_current_user_id, require_owner
from db.jobs_db import JobsDB

logger = logging.getLogger(__name__)
router = APIRouter()


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
    sources  = (data or {}).get("sources", None)       # login-free sources (scraper/sources); None = every enabled one
    if sources is not None and not (isinstance(sources, list) and all(isinstance(x, str) for x in sources)):
        raise HTTPException(400, "'sources' must be a list of source names")

    import threading
    from core.shutdown import (reset_pipeline_cancel, combined_cancel_event,
                               register_thread)
    from tracking.progress import TRACKER

    # One search at a time: the progress bar describes THE run.
    try:
        run_id = TRACKER.start(uid)
    except ValueError as e:
        raise HTTPException(409, str(e))

    # Reset any previous cancel request — a Cancel click for a prior
    # run must not silently disable this new run.
    reset_pipeline_cancel()

    def _run():
        from tracking.pipeline import run
        try:
            run(platforms=platforms, quick_mode=quick,
                cancel_event=combined_cancel_event, user_id=uid, sources=sources,
                progress=TRACKER, run_id=run_id)
        except Exception as e:
            logger.error(f"Pipeline error: {e}")
            TRACKER.close(run_id, error=f"{type(e).__name__}: {e}")
            return
        TRACKER.close(run_id)
        _mark_search_run()

    if background_tasks:
        t = threading.Thread(target=_run, daemon=True, name="pipeline-run")
        background_tasks.add_task(_start_thread, t, register_thread)
    else:                                   # nothing will run: don't leave the bar spinning
        TRACKER.finish(error="not started")
    return JSONResponse({"status": "started", "quick": quick, "run_id": run_id})


@router.post("/api/pipeline/score")
async def score_inbox(background_tasks: BackgroundTasks = None, uid: int = Depends(require_owner)):
    """Score the Inbox now (the "Score now" button). Every Inbox job gets the
    instant keyword match right away (intelligence/fit.py). When AI scoring is
    on in Settings, the Inbox jobs without an AI score are then scored by the
    model in the background, shown by the same progress bar as a search (so it
    waits for a running search: "ai": "busy").
    Returns {"inbox", "quick", "ai": "off"|"none"|"busy"|"started", "ai_total"}."""
    import threading
    from db.profile_db import ProfileDB
    from intelligence.scorer import _llm_scoring_enabled, quick_fit_inbox, score_jobs_batch
    from tracking.progress import TRACKER

    profile = ProfileDB().summary()
    jobs = quick_fit_inbox(JobsDB(user_id=uid), profile)
    out = {"inbox": len(jobs), "quick": sum(1 for j in jobs if j.get("fit_score") is not None),
           "ai": "off", "ai_total": 0}
    todo = [j for j in jobs if j.get("ai_score") is None]
    if not _llm_scoring_enabled():
        return JSONResponse(out)
    if not todo:
        return JSONResponse({**out, "ai": "none"})
    try:
        run_id = TRACKER.start(uid)
    except ValueError:
        return JSONResponse({**out, "ai": "busy"})

    from core.shutdown import combined_cancel_event, register_thread, reset_pipeline_cancel
    reset_pipeline_cancel()

    def emit(event, data):
        if event == "log":
            TRACKER.log(data)
        elif event == "score_progress":
            TRACKER.set_done("score", data.get("done", 0), data.get("total", 0),
                             f"Scoring with AI · {data.get('done', 0)} of {data.get('total', 0)}")

    def _run():
        try:
            TRACKER.begin("score", len(todo), f"Scoring {len(todo)} Inbox jobs with AI")
            done = score_jobs_batch(todo, profile, jobs_db=JobsDB(user_id=uid),
                                    cancel_event=combined_cancel_event, emit=emit)
            cancelled = combined_cancel_event.is_set()
            TRACKER.finish({"kind": "score", "scored": sum(1 for j in done if j.get("ai_score") is not None),
                            "cancelled": cancelled})
        except Exception as e:
            logger.error(f"Scoring error: {e}")
            TRACKER.close(run_id, error=f"{type(e).__name__}: {e}")
            return
        TRACKER.close(run_id)

    if background_tasks:
        t = threading.Thread(target=_run, daemon=True, name="inbox-score")
        background_tasks.add_task(_start_thread, t, register_thread)
    else:
        TRACKER.finish(error="not started")
    return JSONResponse({**out, "ai": "started", "ai_total": len(todo)})


@router.get("/api/pipeline/status")
async def pipeline_status(uid: int = Depends(get_current_user_id)):
    """The current (or last) search as the progress bar shows it: running,
    percent, phase list with done/total, jobs found so far, the last log lines.
    Only the account that started the run sees its details."""
    from tracking.progress import TRACKER
    return JSONResponse(TRACKER.snapshot(uid), headers={"Cache-Control": "no-store"})


@router.post("/api/pipeline/cancel")
async def cancel_pipeline(uid: int = Depends(require_owner)):
    """
    Signal the CURRENTLY RUNNING pipeline to stop ASAP.
    This only affects the in-progress run — it's reset automatically
    at the start of the next /api/pipeline/run call, so it does not
    permanently disable future pipeline runs.
    """
    from core.shutdown import request_pipeline_cancel
    from tracking.progress import TRACKER
    request_pipeline_cancel()
    if TRACKER.is_running():
        TRACKER.note(cancelled=True, current="Cancelling — finishing the current step")
    return JSONResponse({"status": "cancel_requested"})


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


def _mark_search_run():
    """A search finished: the Today page's checklist ticks "first search" (core/checklist.py)."""
    try:
        from core import checklist
        from db.settings_db import SettingsDB
        checklist.mark_search_run(SettingsDB.get())
    except Exception as e:
        logger.warning(f"Could not record the search run: {type(e).__name__}")


async def _start_thread(t, register_fn):
    """Start a background thread, then register it (after start() so
    is_alive() is True and the registry doesn't immediately prune it)."""
    t.start()
    register_fn(t)
