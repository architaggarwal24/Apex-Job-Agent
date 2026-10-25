# server/routes/notifications.py
"""
server/routes/notifications.py — the notification channels' settings and their Test buttons
(the notifier itself: core/notifier.py; its outbox: db/notifications_db.py).

    GET  /api/notifications/settings          the notify_* settings; secrets only as a mask    (owner only)
    PUT  /api/notifications/settings          change some of them, all validated or none saved (owner only)
    POST /api/notifications/test/{channel}    send a test message: email | telegram            (owner only)
                                              body (optional): unsaved values from the form

Owner-only throughout: the channels deliver the OWNER's news to the owner's own inbox, and
their addresses are personal. These keys are managed only here - the generic POST
/api/settings refuses notify_* so the SMTP host check and the format checks can't be skipped.
A failed test answers 400 with the error TYPE only, never the server's reply or a credential.
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from core import notifier
from core.auth import require_owner
from db.settings_db import SettingsDB

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/api/notifications/settings")
async def get_settings(_owner: int = Depends(require_owner)):
    return JSONResponse(notifier.public_config(SettingsDB.get()))


@router.put("/api/notifications/settings")
async def save_settings(data: dict, _owner: int = Depends(require_owner)):
    db = SettingsDB.get()
    try:
        updates = notifier.clean_updates(data, notifier.read_config(db))
    except ValueError as e:
        raise HTTPException(400, str(e))
    for key, value in updates.items():
        db.write(key, value)
    logger.info("[Notify] saved %d notification setting(s)", len(updates))
    return JSONResponse(notifier.public_config(db))


@router.post("/api/notifications/test/{channel}")
async def test_channel(channel: str, data: dict | None = None, _owner: int = Depends(require_owner)):
    if channel not in notifier.TRANSPORTS:
        raise HTTPException(404, "Unknown channel")
    loop = asyncio.get_running_loop()
    try:
        await asyncio.wait_for(loop.run_in_executor(
            None, lambda: notifier.test_channel(channel, SettingsDB.get(), data or None)),
            timeout=notifier.SEND_TIMEOUT * 3)
    except notifier.NotifyError as e:
        raise HTTPException(400, str(e))
    except asyncio.TimeoutError:
        raise HTTPException(504, "Timed out sending the test message")
    return JSONResponse({"status": "ok", "channel": channel})
