"""
server/routes/ghostwriter.py — AI assistant API routes.

All routes that call into intelligence/ghostwriter.py use
core.llm.run_blocking() since those functions make blocking LLM
calls internally (60-90s on some providers) — without this, an
`async def` route calling them directly would freeze the entire
FastAPI event loop, including WebSocket pings and other requests.
"""

import asyncio
import logging
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from core.llm   import run_blocking
from db.jobs_db    import JobsDB
from db.profile_db import ProfileDB

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_HISTORY_MESSAGES = 40
MAX_MESSAGE_CHARS    = 8_000


def _get_job_and_profile(job_id: str):
    db  = JobsDB()
    job = db.get_by_id(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    profile = ProfileDB().summary()
    return job, profile, db


def _sanitize_history(raw) -> list[dict]:
    """
    Validate and sanitize chat history sent by the client before it's
    forwarded into the LLM's `messages` list.

    Previously raw `history` from the request body was passed straight
    through with no validation: a malformed payload (wrong roles, huge
    strings, non-dict entries, hundreds of turns) could break providers
    that require strict alternating user/assistant roles (Anthropic),
    blow up token usage/cost, or include attacker-controlled "system"-
    like role values.

    This keeps only the last MAX_HISTORY_MESSAGES entries with
    role in {"user","assistant"} and string content capped at
    MAX_MESSAGE_CHARS.
    """
    if not isinstance(raw, list):
        return []

    clean = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        role    = entry.get("role")
        content = entry.get("content")
        if role not in ("user", "assistant"):
            continue
        if not isinstance(content, str):
            continue
        clean.append({"role": role, "content": content[:MAX_MESSAGE_CHARS]})

    return clean[-MAX_HISTORY_MESSAGES:]


@router.post("/api/ghostwriter/cover-letter/{job_id}")
async def cover_letter(job_id: str, data: dict = None):
    """Generate a tailored cover letter for this job."""
    job, profile, db = _get_job_and_profile(job_id)
    tone = (data or {}).get("tone", "professional")

    from intelligence.ghostwriter import cover_letter as gw_cl
    try:
        letter = await run_blocking(gw_cl, job, profile, jobs_db=db, tone=tone)
    except asyncio.TimeoutError:
        raise HTTPException(504, "Cover letter generation timed out")
    if not letter:
        raise HTTPException(500, "Cover letter generation failed")
    return JSONResponse({"cover_letter": letter})


@router.post("/api/ghostwriter/interview-prep/{job_id}")
async def interview_prep(job_id: str):
    """Generate likely interview questions + suggested answers."""
    job, profile, db = _get_job_and_profile(job_id)

    from intelligence.ghostwriter import interview_prep as gw_prep
    try:
        result = await run_blocking(gw_prep, job, profile, jobs_db=db)
    except asyncio.TimeoutError:
        raise HTTPException(504, "Interview prep generation timed out")
    return JSONResponse(result)


@router.post("/api/ghostwriter/chat/{job_id}")
async def chat(job_id: str, data: dict):
    """Free-form chat with job + profile context."""
    message = data.get("message", "").strip()
    history = _sanitize_history(data.get("history", []))

    if not message:
        raise HTTPException(400, "Message cannot be empty")
    if len(message) > MAX_MESSAGE_CHARS:
        message = message[:MAX_MESSAGE_CHARS]

    job, profile, db = _get_job_and_profile(job_id)

    from intelligence.ghostwriter import chat as gw_chat
    try:
        response = await run_blocking(gw_chat, message, job, profile,
                                       history=history, jobs_db=db)
    except asyncio.TimeoutError:
        raise HTTPException(504, "Chat response timed out")
    return JSONResponse({"response": response})


@router.get("/api/ghostwriter/context/{job_id}")
async def get_context(job_id: str):
    """Return the full context that the ghostwriter uses for this job.
    Pure local computation — no LLM call, no run_blocking needed."""
    job, profile, db = _get_job_and_profile(job_id)

    from intelligence.ghostwriter import build_context
    ctx = build_context(job, profile, jobs_db=db)
    return JSONResponse({"context": ctx})
