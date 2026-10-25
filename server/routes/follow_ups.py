# server/routes/follow_ups.py
"""
server/routes/follow_ups.py — the follow-up engine's API (rules and storage: db/follow_ups_db.py,
drafts: intelligence/followups.py). It NEVER sends anything: a follow-up is a reminder with a
draft you read and send yourself.

    GET  /api/follow-ups/due                what needs you now, with its draft
    POST /api/follow-ups/{id}/snooze        {"days": 1-365} or {"until": "<date-time>"}
    POST /api/follow-ups/{id}/done          you sent it (or dealt with it)
    POST /api/follow-ups/{id}/dismiss       not needed
    POST /api/follow-ups/{id}/regenerate    write the draft again                    (owner only)

Everything is per user: each handler builds its stores from the session's user, so another
user's follow-up is simply "not found" (404). 409: the follow-up is already done or dismissed.
Marking one done, snoozing or dismissing is your own record and open to any account; writing
drafts spends the owner's LLM calls, so regenerating is owner-only - and the due list only
writes missing drafts for the owner too (a few per request; for another account they are empty
for you to write yourself).
"""

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from core.auth import OWNER_USER_ID, get_current_user_id, require_owner
from core.config import cfg
from core.llm import run_blocking
from db import hunt
from db.follow_ups_db import Closed, FollowUpStore
from db.jobs_db import JobsDB
from intelligence import followups

logger = logging.getLogger(__name__)
router = APIRouter()

DRAFT_FAILED = "The language model could not write a draft right now. Check the LLM settings and try again."


def _answer(result: dict | None) -> JSONResponse:
    if result is None:
        raise HTTPException(404, "Follow-up not found")
    return JSONResponse(result)


def _act(fn):
    try:
        return _answer(fn())
    except Closed as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.get("/api/follow-ups/due")
async def due(uid: int = Depends(get_current_user_id)):
    store = FollowUpStore(uid, cfg.JOBS_DB_PATH)
    if uid == OWNER_USER_ID:
        try:
            await run_blocking(followups.draft_missing, store, JobsDB(path=cfg.JOBS_DB_PATH, user_id=uid))
        except Exception as e:                       # a slow or missing LLM must never hide the list itself
            logger.warning("[FollowUps] Drafting skipped: %s", type(e).__name__)
    return JSONResponse(store.due())


@router.post("/api/follow-ups/{follow_up_id}/snooze")
async def snooze(follow_up_id: int, data: dict, uid: int = Depends(get_current_user_id)):
    store = FollowUpStore(uid, cfg.JOBS_DB_PATH)
    has_days, has_until = "days" in data, "until" in data
    if has_days == has_until:
        raise HTTPException(400, "Give either 'days' or 'until'")
    if has_days:
        days = data["days"]
        if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
            raise HTTPException(400, "'days' must be a whole number from 1 to 365")
        return _act(lambda: store.snooze(follow_up_id, days=days))
    until = hunt.parse_when(data["until"]) if isinstance(data["until"], str) else None
    if until is None:
        raise HTTPException(400, "'until' must be a date-time such as 2026-11-03T09:00")
    return _act(lambda: store.snooze(follow_up_id, until=until))


@router.post("/api/follow-ups/{follow_up_id}/done")
async def done(follow_up_id: int, uid: int = Depends(get_current_user_id)):
    return _act(lambda: FollowUpStore(uid, cfg.JOBS_DB_PATH).done(follow_up_id))


@router.post("/api/follow-ups/{follow_up_id}/dismiss")
async def dismiss(follow_up_id: int, uid: int = Depends(get_current_user_id)):
    return _act(lambda: FollowUpStore(uid, cfg.JOBS_DB_PATH).dismiss(follow_up_id))


@router.post("/api/follow-ups/{follow_up_id}/regenerate")
async def regenerate(follow_up_id: int, uid: int = Depends(require_owner)):
    store = FollowUpStore(uid, cfg.JOBS_DB_PATH)
    jobs = JobsDB(path=cfg.JOBS_DB_PATH, user_id=uid)
    try:
        result = await run_blocking(followups.regenerate, store, jobs, follow_up_id)
    except Closed as e:
        raise HTTPException(409, str(e))
    except asyncio.TimeoutError:
        raise HTTPException(504, "The language model took too long to write the draft")
    except Exception as e:
        logger.warning("[FollowUps] Regenerate failed: %s", type(e).__name__)
        raise HTTPException(502, DRAFT_FAILED)
    return _answer(result)
