# server/routes/queue.py
"""
server/routes/queue.py — approve jobs for the background queue worker and
watch it. The worker itself is applier/queue_worker.py; this is its HTTP face
and the one process-wide instance (get_worker), which main.py starts.

Everything that drives the browser (queue, unqueue, pause, resume) is
owner-only, like /api/apply/*. The status endpoint is open to any signed-in
user but only ever describes THEIR OWN jobs: counts and the current job come
from a JobsDB scoped to the caller.
"""

import logging
import threading

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from applier import queue_worker
from applier.queue_worker import QueueWorker
from core.auth import OWNER_USER_ID, get_current_user_id, require_owner
from db.jobs_db import JobsDB
from db.settings_db import SettingsDB

logger = logging.getLogger(__name__)
router = APIRouter()

_worker: QueueWorker | None = None
_worker_lock = threading.Lock()


def get_worker() -> QueueWorker:
    """The process's queue worker (created on first use, started by main.py).
    It shares /api/apply's per-job lock, so a job can't be run by the queue
    and by hand at the same moment."""
    global _worker
    with _worker_lock:
        if _worker is None:
            from server.routes import apply as apply_routes
            _worker = QueueWorker(OWNER_USER_ID,
                                  job_lock=(apply_routes._acquire, apply_routes._release))
        return _worker


def reset_worker() -> None:
    """Stop and forget the process worker (tests)."""
    global _worker
    with _worker_lock:
        w, _worker = _worker, None
    if w is not None:
        w.stop()


def _job_ids(data: dict) -> list[str]:
    ids = data.get("job_ids") if isinstance(data, dict) else None
    if (not isinstance(ids, list) or not ids
            or not all(isinstance(i, str) and i.strip() for i in ids)):
        raise HTTPException(400, "'job_ids' must be a non-empty list of job id strings")
    ids = list(dict.fromkeys(i.strip() for i in ids))
    if len(ids) > queue_worker.MAX_QUEUE_REQUEST:
        raise HTTPException(400, f"At most {queue_worker.MAX_QUEUE_REQUEST} jobs at a time")
    return ids


@router.post("/api/applications/queue")
async def queue(data: dict, uid: int = Depends(require_owner)):
    """Body {"job_ids": [...]}. Queues what it can; the rest come back in
    "rejected" with a reason (not_found, already_queued, illegal_stage,
    not_scored, below_threshold, changed)."""
    result = queue_worker.queue_jobs(JobsDB(user_id=uid), SettingsDB.get(), _job_ids(data))
    if result["queued"]:
        get_worker().wake()
    return JSONResponse(result)


@router.post("/api/applications/unqueue")
async def unqueue(data: dict, uid: int = Depends(require_owner)):
    """Body {"job_ids": [...]}. Puts queued jobs back to "found"."""
    return JSONResponse(queue_worker.unqueue_jobs(JobsDB(user_id=uid), _job_ids(data)))


@router.post("/api/queue/pause")
async def pause(uid: int = Depends(require_owner)):
    """Stop starting new jobs. The one in progress finishes."""
    worker = get_worker()
    worker.pause()
    return JSONResponse(worker.status(JobsDB(user_id=uid)))


@router.post("/api/queue/resume")
async def resume(uid: int = Depends(require_owner)):
    worker = get_worker()
    worker.resume()
    return JSONResponse(worker.status(JobsDB(user_id=uid)))


@router.get("/api/queue/status")
async def status(uid: int = Depends(get_current_user_id)):
    return JSONResponse(get_worker().status(JobsDB(user_id=uid)))
