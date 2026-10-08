# server/routes/answer_bank.py
"""
server/routes/answer_bank.py — your saved answers: list / add / edit / delete,
and "what would the agent answer to this?".

Per user: every handler builds AnswerBank(uid) from the session, so one user's
entries are simply not there for another. Managing the bank is your own data,
open to any signed-in user (only DRIVING the browser is owner-only). The
profile's facts appear, read-only, as 'profile:<key>' entries - for the
profile's owner only, because the machine has one profile and it is theirs.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from core.auth import get_current_user_id
from db import answer_bank_db as bankdb
from db.answer_bank_db import AnswerBank, DuplicateAnswer
from db.profile_db import ProfileDB

logger = logging.getLogger(__name__)
router = APIRouter()


def _profile_for(uid: int) -> dict | None:
    """The profile facts a person actually provided (not seeded placeholders),
    but only for the user the profile belongs to."""
    if uid != bankdb.PROFILE_OWNER_ID:
        return None
    try:
        return ProfileDB().answered_values()
    except Exception:
        logger.exception("[AnswerBank] could not read the profile")
        return None


def _text(data: dict, key: str, required: bool = False):
    v = data.get(key)
    if v is None:
        if required:
            raise HTTPException(400, f"{key} is required")
        return None
    if isinstance(v, bool) or not isinstance(v, (str, int, float)):
        raise HTTPException(400, f"{key} must be text")
    if required and not str(v).strip():
        raise HTTPException(400, f"{key} is required")
    return str(v)


def _entry_id(raw: str) -> int | str:
    """Path id -> int for a row, or the 'profile:<key>' string (read-only)."""
    if raw.startswith(bankdb.PROFILE_PREFIX):
        return raw
    try:
        n = int(raw)
    except ValueError:
        raise HTTPException(404, "Entry not found")
    return n


def _readonly(raw: str):
    raise HTTPException(403, "Profile answers are read-only; change them on the Profile page")


@router.get("/api/answer-bank")
async def list_entries(q: str = "", scope: str = "", source: str = "", company: str = "",
                       limit: int = 500, uid: int = Depends(get_current_user_id)):
    try:
        rows = AnswerBank(uid).entries_with_profile(_profile_for(uid), q=q, scope=scope,
                                                    source=source, company=company, limit=limit)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse(rows)


@router.post("/api/answer-bank")
async def add_entry(data: dict, uid: int = Depends(get_current_user_id)):
    """Body: question, answer, scope (global|company), company, policy
    (always|ask|once; default always for a new entry, unchanged for an
    existing one), source (user|ai). Saving a question that already has an
    answer at that scope updates it."""
    try:
        entry = AnswerBank(uid).add(
            _text(data, "question", True), _text(data, "answer", True),
            scope=_text(data, "scope") or "global", company=_text(data, "company") or "",
            policy=_text(data, "policy") or None, source=_text(data, "source") or "user")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse(entry)


@router.post("/api/answer-bank/resolve")
async def resolve(data: dict, uid: int = Depends(get_current_user_id)):
    """Body: {question, company}. What the agent would answer: {answer,
    confidence, entry_id} - or answer/entry_id null and confidence 0 if it
    has nothing. Looking is not using: nothing is counted."""
    question = _text(data, "question", True)
    m = AnswerBank(uid).resolve(question, _text(data, "company") or "", _profile_for(uid))
    if m is None:
        return JSONResponse({"answer": None, "confidence": 0.0, "entry_id": None})
    return JSONResponse({"answer": m["answer"], "confidence": m["confidence"],
                         "entry_id": m["entry_id"]})


@router.get("/api/answer-bank/{entry_id}")
async def get_entry(entry_id: str, uid: int = Depends(get_current_user_id)):
    eid = _entry_id(entry_id)
    if isinstance(eid, str):
        for e in bankdb.profile_entries(_profile_for(uid)):
            if e["id"] == eid:
                return JSONResponse({k: v for k, v in e.items() if k != "phrasings"})
        raise HTTPException(404, "Entry not found")
    entry = AnswerBank(uid).get(eid)
    if entry is None:
        raise HTTPException(404, "Entry not found")
    return JSONResponse(entry)


@router.put("/api/answer-bank/{entry_id}")
async def update_entry(entry_id: str, data: dict, uid: int = Depends(get_current_user_id)):
    """Partial update: any of question, answer, scope, company, policy."""
    eid = _entry_id(entry_id)
    if isinstance(eid, str):
        _readonly(entry_id)
    changes = {k: _text(data, k) for k in ("question", "answer", "scope", "company", "policy")
               if data.get(k) is not None}
    try:
        entry = AnswerBank(uid).update(eid, **changes)
    except DuplicateAnswer as e:
        raise HTTPException(409, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    if entry is None:
        raise HTTPException(404, "Entry not found")
    return JSONResponse(entry)


@router.delete("/api/answer-bank/{entry_id}")
async def delete_entry(entry_id: str, uid: int = Depends(get_current_user_id)):
    eid = _entry_id(entry_id)
    if isinstance(eid, str):
        _readonly(entry_id)
    if not AnswerBank(uid).delete(eid):
        raise HTTPException(404, "Entry not found")
    return JSONResponse({"status": "deleted"})
