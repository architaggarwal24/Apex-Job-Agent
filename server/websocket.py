"""
server/websocket.py — WebSocket endpoint for live progress updates.
<<<<<<< HEAD
=======

_queues maps job_id -> asyncio.Queue, used to push apply-session
progress events from background threads (push_event) to the
connected browser tab (ws_endpoint).

Cleanup: a queue is removed when its WebSocket disconnects. Queues
can also be created by push_event() for a job_id that's never been
connected to via WebSocket (e.g. a pipeline scoring job before the
user opens /apply) — these "orphaned" queues are capped at
MAX_ORPHAN_QUEUES with FIFO eviction so _queues can't grow without
bound over a long-running server process.
>>>>>>> 4226f10 (.)
"""

import asyncio
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)
router = APIRouter()

<<<<<<< HEAD
_queues: dict[str, asyncio.Queue] = {}
=======
MAX_ORPHAN_QUEUES = 200

_queues: dict[str, asyncio.Queue] = {}
# job_ids with an actively-connected WebSocket — never evicted while connected.
_connected: set[str] = set()
>>>>>>> 4226f10 (.)


def get_queue(job_id: str) -> asyncio.Queue:
    if job_id not in _queues:
<<<<<<< HEAD
=======
        _evict_if_needed()
>>>>>>> 4226f10 (.)
        _queues[job_id] = asyncio.Queue()
    return _queues[job_id]


<<<<<<< HEAD
=======
def _evict_if_needed():
    """If we're at the orphan cap, drop the oldest non-connected queue."""
    if len(_queues) < MAX_ORPHAN_QUEUES:
        return
    for jid in list(_queues.keys()):
        if jid not in _connected:
            del _queues[jid]
            logger.debug(f"[WS] Evicted orphaned queue for job {jid}")
            return


>>>>>>> 4226f10 (.)
def push_event(loop: asyncio.AbstractEventLoop, job_id: str, event: str, data):
    """Thread-safe push from background thread → WebSocket queue."""
    try:
        loop.call_soon_threadsafe(get_queue(job_id).put_nowait,
                                  {"event": event, "data": data})
    except Exception as e:
        logger.debug(f"Event push error {job_id}: {e}")


@router.websocket("/ws/{job_id}")
async def ws_endpoint(websocket: WebSocket, job_id: str):
<<<<<<< HEAD
    await websocket.accept()
    q = get_queue(job_id)
=======
    # HTTP auth middleware never runs for WebSocket connections, so this
    # endpoint must authenticate itself: a valid session, for an account
    # that still exists, that actually OWNS this job. Without the
    # ownership check any logged-in user could subscribe to another
    # user's live apply session just by knowing/guessing a job_id.
    from core.auth   import SESSION_COOKIE, user_for_token
    from db.jobs_db  import JobsDB

    user = user_for_token(websocket.cookies.get(SESSION_COOKIE))
    if user is None or JobsDB(user_id=user["id"]).get_by_id(job_id) is None:
        # 1008 = policy violation. Same response whether the job doesn't
        # exist or belongs to someone else, so it can't be used to probe
        # which job IDs exist.
        await websocket.close(code=1008)
        return

    await websocket.accept()
    q = get_queue(job_id)
    _connected.add(job_id)
>>>>>>> 4226f10 (.)
    try:
        while True:
            try:
                msg = await asyncio.wait_for(q.get(), timeout=25.0)
                await websocket.send_json(msg)
            except asyncio.TimeoutError:
                await websocket.send_json({"event": "ping", "data": {}})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug(f"WS error {job_id}: {e}")
<<<<<<< HEAD
=======
    finally:
        _connected.discard(job_id)
        _queues.pop(job_id, None)
>>>>>>> 4226f10 (.)
