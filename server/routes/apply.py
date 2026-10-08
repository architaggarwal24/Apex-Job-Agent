"""
server/routes/apply.py — Application session API.
<<<<<<< HEAD
=======

Includes:
  - A per-job in-memory lock preventing the same job_id from being
    started/answered/submitted concurrently (e.g. double-click on
    "Start Application" before the session state has updated), which
    could otherwise launch two Playwright contexts against the same
    Opera profile directory.
  - Enforcement of daily_apply_limit and min_delay_between_applies
    (read from SettingsDB) before a REAL (non-dry-run) submission.
    These settings previously existed in the UI but were never
    checked anywhere.
>>>>>>> 4226f10 (.)
"""

import asyncio
import logging
<<<<<<< HEAD
from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse

=======
import threading
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import JSONResponse

from core.auth import get_current_user_id, require_owner
from db import lifecycle
>>>>>>> 4226f10 (.)
from db.jobs_db import JobsDB

logger = logging.getLogger(__name__)
router = APIRouter()

<<<<<<< HEAD

def _get_job(job_id: str) -> dict:
    job = JobsDB().get_by_id(job_id)
=======
# ── Per-job concurrency lock ────────────────────────────────────────────────
_active_jobs: set[str] = set()
_active_lock = threading.Lock()


def _acquire(job_id: str) -> bool:
    """Returns True if lock acquired (job wasn't already active)."""
    with _active_lock:
        if job_id in _active_jobs:
            return False
        _active_jobs.add(job_id)
        return True


def _release(job_id: str):
    with _active_lock:
        _active_jobs.discard(job_id)


def _get_job(job_id: str, uid: int) -> dict:
    """The caller's own job, else 404 (same answer for someone else's)."""
    job = JobsDB(user_id=uid).get_by_id(job_id)
>>>>>>> 4226f10 (.)
    if not job:
        raise HTTPException(404, f"Job {job_id} not found")
    return job


<<<<<<< HEAD
@router.post("/api/apply/start/{job_id}")
async def start(job_id: str, background_tasks: BackgroundTasks):
    from applier.session import ApplicationSession, State, Orchestrator

    job     = _get_job(job_id)
    session = ApplicationSession.load(job_id)

    if session and session.state == State.DONE.value:
        return JSONResponse({"status": "already_done"})

    # Reset failed/skipped sessions
    if session and session.state in (State.FAILED.value, State.SKIPPED.value):
=======
def _settings_dry_run() -> bool:
    try:
        from db.settings_db import SettingsDB
        return SettingsDB.get().read("dry_run", "false").lower() == "true"
    except Exception:
        from core.config import cfg
        return cfg.DRY_RUN


@router.post("/api/apply/start/{job_id}")
async def start(job_id: str, background_tasks: BackgroundTasks,
                uid: int = Depends(require_owner)):
    from applier.session import ApplicationSession, State, Orchestrator

    job     = _get_job(job_id, uid)
    session = ApplicationSession.load(job_id)

    # Already submitted (or past it): nothing left to prepare.
    if job["stage"] in lifecycle.SUBMITTED_OR_LATER:
        return JSONResponse({"status": "already_done"})

    if not _acquire(job_id):
        raise HTTPException(409, "An operation is already running for this job")

    # Reset failed/skipped sessions
    if session and (session.state in (State.FAILED.value, State.SKIPPED.value)
                    or job["stage"] in ("failed", "skipped")):
>>>>>>> 4226f10 (.)
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
<<<<<<< HEAD
=======
        finally:
            _release(job_id)
>>>>>>> 4226f10 (.)

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
<<<<<<< HEAD
async def answer(job_id: str, data: dict, background_tasks: BackgroundTasks):
    """Submit missing field answers → trigger iteration 2."""
    from applier.session import ApplicationSession, State, Orchestrator

    s = ApplicationSession.load(job_id)
    if not s or s.state != State.AWAITING.value:
        raise HTTPException(400, "Session not awaiting input")

    answers = data.get("answers", {})
    job     = _get_job(job_id)
=======
async def answer(job_id: str, data: dict, background_tasks: BackgroundTasks,
                 uid: int = Depends(require_owner)):
    """Submit missing field answers → trigger iteration 2."""
    from applier.session import ApplicationSession, Orchestrator

    job = _get_job(job_id, uid)
    s   = ApplicationSession.load(job_id)
    if not s or job["stage"] != "needs_you" or job["blocked_reason"] != "question":
        raise HTTPException(400, "Session not awaiting input")

    if not _acquire(job_id):
        raise HTTPException(409, "An operation is already running for this job")

    answers = data.get("answers", {})
    try:                                    # refuse a bad flag before anything is saved or run
        from applier.answers import remember_modes
        remember_modes(data.get("remember"), answers)
    except ValueError as e:
        _release(job_id)
        raise HTTPException(400, str(e))
    remember = data.get("remember")
>>>>>>> 4226f10 (.)
    loop    = asyncio.get_event_loop()
    from server.websocket import push_event

    def run():
        def emit(event, d): push_event(loop, job_id, event, d)
        try:
<<<<<<< HEAD
            Orchestrator(job).run_iter2(answers, emit)
        except Exception as e:
            emit("error", str(e))
=======
            Orchestrator(job).run_iter2(answers, emit, remember=remember)
        except Exception as e:
            emit("error", str(e))
        finally:
            _release(job_id)
>>>>>>> 4226f10 (.)

    background_tasks.add_task(_thread, run)
    return JSONResponse({"status": "iter2_started"})


@router.post("/api/apply/submit/{job_id}")
<<<<<<< HEAD
async def submit(job_id: str, data: dict, background_tasks: BackgroundTasks):
    """User approved screenshot → submit application."""
    from applier.session import ApplicationSession, State, Orchestrator

    s = ApplicationSession.load(job_id)
    if not s or s.state != State.REVIEW.value:
        raise HTTPException(400, "Session not in review state")

    corrections = data.get("corrections", [])
    job  = _get_job(job_id)
=======
async def submit(job_id: str, data: dict, background_tasks: BackgroundTasks,
                 uid: int = Depends(require_owner)):
    """User approved screenshot → submit application.

    Enforces daily_apply_limit and min_delay_between_applies (from
    Settings → Agent Behaviour) before allowing a REAL submission.
    Dry-run submissions (which don't actually apply anywhere) bypass
    these checks since they don't count against either limit.
    """
    from applier.session import ApplicationSession, Orchestrator

    job = _get_job(job_id, uid)
    s   = ApplicationSession.load(job_id)
    if not s or job["stage"] != "ready_for_review":
        raise HTTPException(400, "Session not in review state")

    if not _acquire(job_id):
        raise HTTPException(409, "An operation is already running for this job")

    try:
        if not _settings_dry_run():
            _enforce_rate_limits()
    except HTTPException:
        _release(job_id)
        raise

    corrections = data.get("corrections", [])
>>>>>>> 4226f10 (.)
    loop = asyncio.get_event_loop()
    from server.websocket import push_event

    def run():
        def emit(event, d): push_event(loop, job_id, event, d)
        try:
            Orchestrator(job).submit(corrections or None, emit)
        except Exception as e:
            emit("error", str(e))
<<<<<<< HEAD
=======
        finally:
            _release(job_id)
>>>>>>> 4226f10 (.)

    background_tasks.add_task(_thread, run)
    return JSONResponse({"status": "submitting"})


<<<<<<< HEAD
@router.post("/api/apply/skip/{job_id}")
async def skip(job_id: str):
    from applier.session import ApplicationSession, State
    JobsDB().update_status(job_id, "skipped")
=======
def _enforce_rate_limits():
    """
    Raises HTTPException(429) if the configured daily_apply_limit
    has been reached, or if min_delay_between_applies hasn't elapsed
    since the last successful application.
    """
    from applier.queue_worker import daily_limit as effective_daily_limit, quality_first
    from db.settings_db import SettingsDB
    settings = SettingsDB.get()

    daily_limit = effective_daily_limit(settings)      # quality_first caps it at 10
    try:
        min_delay = int(settings.read("min_delay_between_applies", "45"))
    except ValueError:
        min_delay = 45

    jobs_db = JobsDB()

    if daily_limit > 0:
        applied_today = jobs_db.count_applied_today()
        if applied_today >= daily_limit:
            hint = ("Turn off Quality First in Settings → Agent Behaviour to use your own limit, "
                    if quality_first(settings) else
                    "Increase the limit in Settings → Agent Behaviour, ")
            raise HTTPException(
                429,
                f"Daily apply limit reached ({applied_today}/{daily_limit}). "
                f"{hint}or try again tomorrow."
            )

    if min_delay > 0:
        last_applied = jobs_db.get_last_applied_at()
        if last_applied:
            try:
                last_dt = datetime.fromisoformat(last_applied)
                elapsed = (datetime.now() - last_dt).total_seconds()
                if elapsed < min_delay:
                    wait_left = int(min_delay - elapsed)
                    raise HTTPException(
                        429,
                        f"Please wait {wait_left}s before submitting another "
                        f"application (min delay between applies: {min_delay}s)."
                    )
            except ValueError:
                pass  # malformed timestamp — don't block on it


@router.post("/api/apply/skip/{job_id}")
async def skip(job_id: str, uid: int = Depends(get_current_user_id)):
    from applier.session import ApplicationSession, State
    _get_job(job_id, uid)
    try:
        JobsDB(user_id=uid).update_status(job_id, "skipped", actor="user")
    except lifecycle.IllegalTransition as e:
        raise HTTPException(409, str(e))
>>>>>>> 4226f10 (.)
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


<<<<<<< HEAD
=======
@router.get("/api/apply/limits")
async def get_limits():
    """Expose current rate-limit status to the UI (e.g. to disable
    the Submit button or show a countdown)."""
    from applier.queue_worker import daily_limit as effective_daily_limit
    from db.settings_db import SettingsDB
    settings = SettingsDB.get()
    jobs_db  = JobsDB()

    daily_limit = effective_daily_limit(settings)      # quality_first caps it at 10
    try:
        min_delay = int(settings.read("min_delay_between_applies", "45"))
    except ValueError:
        min_delay = 45

    applied_today = jobs_db.count_applied_today()
    last_applied  = jobs_db.get_last_applied_at()

    wait_seconds = 0
    if min_delay > 0 and last_applied:
        try:
            elapsed = (datetime.now() - datetime.fromisoformat(last_applied)).total_seconds()
            wait_seconds = max(0, int(min_delay - elapsed))
        except ValueError:
            pass

    return JSONResponse({
        "daily_limit":       daily_limit,
        "applied_today":     applied_today,
        "daily_limit_reached": daily_limit > 0 and applied_today >= daily_limit,
        "min_delay":         min_delay,
        "wait_seconds":      wait_seconds,
        "dry_run":           _settings_dry_run(),
    })


>>>>>>> 4226f10 (.)
async def _thread(fn):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, fn)
