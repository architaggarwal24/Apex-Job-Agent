# server/sse.py
"""
Server-sent events for the application timeline. Plain Python (no FastAPI
import) so the stream logic can be tested on its own; the route in
server/routes/applications.py just wraps stream() in a StreamingResponse.

The stream polls the caller's own application_events rows. That is
deliberate: events are written from background threads (the apply
Orchestrator, the scorer) and possibly other processes, and the table is
already the durable, ordered record - so there is no second in-memory
queue to keep consistent, and resuming is just "events with id > N".
Every query goes through a JobsDB scoped to one user, so a stream can only
ever carry that user's events, whatever Last-Event-ID it was given.
"""

import asyncio
import json
import time

POLL_SECONDS      = 1.0
HEARTBEAT_SECONDS = 15.0
RETRY_MS          = 3000
SSE_EVENT_NAME    = "application_event"


def format_event(ev: dict) -> str:
    """One SSE frame. The id is the event's table id, so the browser's
    automatic reconnect (Last-Event-ID) resumes exactly where it stopped."""
    return (f"id: {ev['id']}\nevent: {SSE_EVENT_NAME}\n"
            f"data: {json.dumps(ev, ensure_ascii=False)}\n\n")


def parse_last_event_id(raw) -> int | None:
    """The Last-Event-ID header as a non-negative int, else None (absent or junk)."""
    try:
        n = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


async def stream(db, last_event_id=None, *, poll: float = POLL_SECONDS,
                 heartbeat: float = HEARTBEAT_SECONDS, is_disconnected=None):
    """Async generator of SSE text for db's user.

    With a valid Last-Event-ID it first replays everything newer than it.
    Without one it starts from "now": history is what GET /api/applications/{id}
    is for, and replaying it on every page load would be a flood. An id
    beyond the user's newest event (a replaced database, a stale client) is
    clamped to the newest so the client isn't left waiting for ids that
    will never come.

    `is_disconnected` is an optional async callable; when it returns True
    the generator ends."""
    head = await asyncio.to_thread(db.event_head)
    last = parse_last_event_id(last_event_id)
    last = head if last is None else min(last, head)

    yield f"retry: {RETRY_MS}\n\n"
    quiet_since = time.monotonic()
    while True:
        if is_disconnected is not None and await is_disconnected():
            return
        events = await asyncio.to_thread(db.events_since, last)
        for ev in events:
            last = ev["id"]
            yield format_event(ev)
        if events:
            quiet_since = time.monotonic()
            continue                      # there may be more than one batch
        if time.monotonic() - quiet_since >= heartbeat:
            yield ": keep-alive\n\n"      # SSE comment: keeps proxies from idling the connection out
            quiet_since = time.monotonic()
        await asyncio.sleep(poll)
