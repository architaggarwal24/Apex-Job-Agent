# server/routes/runner.py
"""
server/routes/runner.py — what a RUNNER on your own computer talks to.

    POST /api/runner/claim              long poll (up to 25 s) for ONE task: the
                                        application, a profile subset, answers, the kit
                                        and where to download the resume - or {"task": null}
    POST /api/runner/{task}/events      progress: timeline steps, the site type
    POST /api/runner/{task}/screenshot  a PNG (multipart field "file", at most 3 MB,
                                        signature checked; the file name is never used)
    POST /api/runner/{task}/result      the pass is over: success / error, what could not
                                        be answered, the review snapshot
    GET  /api/runner/{task}/resume      the resume to upload (decrypted for this download)

EVERY route here is authenticated by a bearer token (require_runner) and by
nothing else - no cookie, no session - and these are the only routes that
accept one. They are let past the session gate by server/static_shell.py's
RUNNER_PATH, an exact match of precisely these paths; tests/test_runner.py
audits that every route in this file depends on require_runner and that no other
route module mentions it. The token's user is the only user it can act as
(applier/runner_hub.py scopes everything by it). Creating and revoking tokens is
the owner's job, in server/routes/runner_tokens.py behind the normal session.
"""

import asyncio

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from applier.runner_hub import MAX_PNG_BYTES, MAX_WAIT_SECONDS, RunnerError, RunnerHub
from core.runner_auth import RunnerAuth, authenticate_header

router = APIRouter()

_OVERHEAD = 64 * 1024            # multipart framing allowed on top of the PNG itself


def require_runner(authorization: str | None = Header(None)) -> RunnerAuth:
    """The runner behind the bearer token; 401 for anything else."""
    auth = authenticate_header(authorization)
    if auth is None:
        raise HTTPException(401, "Invalid or revoked runner token", headers={"WWW-Authenticate": "Bearer"})
    return auth


def _fail(e: RunnerError):
    raise HTTPException(e.status, str(e))


@router.post("/api/runner/claim")
async def claim(data: dict | None = None, runner: RunnerAuth = Depends(require_runner)):
    wait = (data or {}).get("wait", MAX_WAIT_SECONDS)
    try:
        task = await asyncio.to_thread(RunnerHub(runner.user_id).claim, runner.token_id, wait)
    except RunnerError as e:
        _fail(e)
    return JSONResponse({"task": task})


@router.post("/api/runner/{task_id}/events")
async def events(task_id: str, data: dict, runner: RunnerAuth = Depends(require_runner)):
    try:
        out = await asyncio.to_thread(RunnerHub(runner.user_id).events, runner.token_id, task_id, data.get("events"))
    except RunnerError as e:
        _fail(e)
    return JSONResponse(out)


@router.post("/api/runner/{task_id}/screenshot")
async def screenshot(task_id: str, request: Request, file: UploadFile = File(...), label: str = Form(""),
                     runner: RunnerAuth = Depends(require_runner)):
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_PNG_BYTES + _OVERHEAD:
        raise HTTPException(413, "The screenshot is larger than 3 MB")
    data = await file.read(MAX_PNG_BYTES + 1)             # one byte over is enough to know; file.filename is never read
    try:
        out = await asyncio.to_thread(RunnerHub(runner.user_id).screenshot, runner.token_id, task_id, data, label)
    except RunnerError as e:
        _fail(e)
    return JSONResponse(out)


@router.post("/api/runner/{task_id}/result")
async def result(task_id: str, data: dict, runner: RunnerAuth = Depends(require_runner)):
    try:
        out = await asyncio.to_thread(RunnerHub(runner.user_id).result, runner.token_id, task_id, data)
    except RunnerError as e:
        _fail(e)
    return JSONResponse(out)


@router.get("/api/runner/{task_id}/resume")
async def resume(task_id: str, runner: RunnerAuth = Depends(require_runner)):
    try:
        filename, data = await asyncio.to_thread(RunnerHub(runner.user_id).resume, runner.token_id, task_id)
    except RunnerError as e:
        _fail(e)
    return Response(content=data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})
