"""
server/websocket.py — WebSocket endpoint for live progress updates.
"""

import asyncio
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)
router = APIRouter()

_queues: dict[str, asyncio.Queue] = {}


def get_queue(job_id: str) -> asyncio.Queue:
    if job_id not in _queues:
        _queues[job_id] = asyncio.Queue()
    return _queues[job_id]


def push_event(loop: asyncio.AbstractEventLoop, job_id: str, event: str, data):
    """Thread-safe push from background thread → WebSocket queue."""
    try:
        loop.call_soon_threadsafe(get_queue(job_id).put_nowait,
                                  {"event": event, "data": data})
    except Exception as e:
        logger.debug(f"Event push error {job_id}: {e}")


@router.websocket("/ws/{job_id}")
async def ws_endpoint(websocket: WebSocket, job_id: str):
    await websocket.accept()
    q = get_queue(job_id)
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
