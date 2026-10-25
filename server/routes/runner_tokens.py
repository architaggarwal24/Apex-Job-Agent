# server/routes/runner_tokens.py
"""
server/routes/runner_tokens.py — the owner creates and revokes runner tokens.

    GET    /api/settings/runner-tokens        the tokens: label, created, last seen, active
                                              (never the token, never its hash)
    POST   /api/settings/runner-tokens        {"label"} -> the token, ONCE
    DELETE /api/settings/runner-tokens/{id}   revoke it (it stops working at once)

Behind the normal session gate and owner-only. A token belongs to the user who
created it, runs applications only as them, and is stored only as a SHA-256
(db/runner_db.py): the response to POST is the one and only time it can be seen.
"""

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse

from core.auth import require_owner
from db.runner_db import RunnerTokenStore

router = APIRouter()


@router.get("/api/settings/runner-tokens")
async def list_tokens(uid: int = Depends(require_owner)):
    return JSONResponse({"tokens": RunnerTokenStore(uid).list()})


@router.post("/api/settings/runner-tokens")
async def create_token(data: dict, uid: int = Depends(require_owner)):
    try:
        created = RunnerTokenStore(uid).create(data.get("label"))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return JSONResponse({**created, "note": "Copy this token now: it is shown only once."},
                        headers={"Cache-Control": "no-store"})


@router.delete("/api/settings/runner-tokens/{token_id}")
async def revoke_token(token_id: int, uid: int = Depends(require_owner)):
    if not RunnerTokenStore(uid).revoke(token_id):
        raise HTTPException(404, "Token not found")
    return JSONResponse({"status": "revoked"})
