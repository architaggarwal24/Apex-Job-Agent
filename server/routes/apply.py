"""
server/routes/apply.py — Application session API.
"""

import asyncio
import logging
from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse

from db.jobs_db import JobsDB

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_job(job_id: str) -> dict:
    job = JobsDB().get_by_id(job_id)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")
    return job


@router.post("/api/apply/start/{job_id}")
async def start(job_id: str, background_tasks: BackgroundTasks):
    from applier.session import ApplicationSession, State, Orchestrator

    job     = _get_job(job_id)
    session = ApplicationSession.load(job_id)

    if session and session.state == State.DONE.value:
        return JSONResponse({"status": "already_done"})

    # Reset failed/skipped sessions
    if session and session.state in (State.FAILED.value, State.SKIPPED.value):
        session.state       = State.IDLE.value
        session.error       = ""
        session.screenshots = []
        session.missing_prompts = []
        session.log         = []
        session.save()

    loop = asyncio.get_event_loop()
    from server.websocket import push_event

    def run():
        def emit(event, data):
            push_event(loop, job_id, event, data)
        try:
            Orchestrator(job).run_iter1(emit)
        except Exception as e:
            emit("error", str(e))

    background_tasks.add_task(_thread, run)
    return JSONResponse({"status": "started", "job_id": job_id})


@router.get("/api/apply/status/{job_id}")
async def status(job_id: str):
    from applier.session import ApplicationSession
    s = ApplicationSession.load(job_id)
    if not s:
        raise HTTPException(404, "No session")
    return JSONResponse(s.to_dict())


@router.post("/api/apply/answer/{job_id}")
async def answer(job_id: str, data: dict, background_tasks: BackgroundTasks):
    """Submit missing field answers → trigger iteration 2."""
    from applier.session import ApplicationSession, State, Orchestrator

    s = ApplicationSession.load(job_id)
    if not s or s.state != State.AWAITING.value:
        raise HTTPException(400, "Session not awaiting input")

    answers = data.get("answers", {})
    job     = _get_job(job_id)
    loop    = asyncio.get_event_loop()
    from server.websocket import push_event

    def run():
        def emit(event, d): push_event(loop, job_id, event, d)
        try:
            Orchestrator(job).run_iter2(answers, emit)
        except Exception as e:
            emit("error", str(e))

    background_tasks.add_task(_thread, run)
    return JSONResponse({"status": "iter2_started"})


@router.post("/api/apply/submit/{job_id}")
async def submit(job_id: str, data: dict, background_tasks: BackgroundTasks):
    """User approved screenshot → submit application."""
    from applier.session import ApplicationSession, State, Orchestrator

    s = ApplicationSession.load(job_id)
    if not s or s.state != State.REVIEW.value:
        raise HTTPException(400, "Session not in review state")

    corrections = data.get("corrections", [])
    job  = _get_job(job_id)
    loop = asyncio.get_event_loop()
    from server.websocket import push_event

    def run():
        def emit(event, d): push_event(loop, job_id, event, d)
        try:
            Orchestrator(job).submit(corrections or None, emit)
        except Exception as e:
            emit("error", str(e))

    background_tasks.add_task(_thread, run)
    return JSONResponse({"status": "submitting"})


@router.post("/api/apply/skip/{job_id}")
async def skip(job_id: str):
    from applier.session import ApplicationSession, State
    JobsDB().update_status(job_id, "skipped")
    s = ApplicationSession.load(job_id)
    if s:
        s.state = State.SKIPPED.value
        s.save()
    return JSONResponse({"status": "skipped"})


@router.get("/api/apply/sessions")
async def sessions():
    from applier.session import ApplicationSession
    all_s = ApplicationSession.load_all()
    return JSONResponse([s.to_dict() for s in all_s[:100]])


async def _thread(fn):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, fn)
