"""
server/routes/auth.py — signup / login / logout for multi-user deployment.
"""

from fastapi import APIRouter, Depends, Request, Response, HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from core.config import cfg
from core.throttle import login_throttle
from db.users_db import UsersDB
from core.auth import (
    set_session_cookie, clear_session_cookie,
    get_current_user_id, get_current_user_id_optional,
)

router = APIRouter()
ROOT   = cfg.ROOT_DIR


def _t(name: str) -> str:
    p = ROOT / "templates" / name
    return p.read_text(encoding="utf-8") if p.exists() else f"<h1>Missing: {name}</h1>"


class Credentials(BaseModel):
    email: str
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


@router.get("/login")
async def login_page():
    """Login now lives inside the React app itself (it renders a login
    view when GET /api/auth/me says unauthenticated, rather than being
    a separate page) — this route only exists so old bookmarks/links
    to /login still land somewhere sensible."""
    return RedirectResponse("/", status_code=303)


@router.post("/api/auth/signup")
async def signup(creds: Credentials, response: Response):
    db = UsersDB.get()
    # Closed after the first account unless explicitly opened. The very
    # first signup is always allowed so a fresh install is usable.
    if db.count() > 0 and not cfg.ALLOW_SIGNUP:
        raise HTTPException(
            status_code=403,
            detail="Sign-ups are closed on this instance. Ask the owner for access.",
        )
    try:
        user = db.create_user(creds.email, creds.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    set_session_cookie(response, user["id"])
    return {"id": user["id"], "email": user["email"]}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/api/auth/login")
async def login(creds: Credentials, request: Request, response: Response):
    ip   = _client_ip(request)
    wait = login_throttle.retry_after(ip, creds.email)
    if wait:
        # Checked BEFORE verifying the password, so a blocked client
        # learns nothing about whether its guesses were right.
        raise HTTPException(
            status_code=429,
            detail=f"Too many failed attempts. Try again in {wait // 60 + 1} minute(s).",
            headers={"Retry-After": str(wait)},
        )
    user = UsersDB.get().verify_login(creds.email, creds.password)
    if not user:
        login_throttle.record_failure(ip, creds.email)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    login_throttle.record_success(ip, creds.email)
    set_session_cookie(response, user["id"], user["session_version"])
    return {"id": user["id"], "email": user["email"]}


@router.post("/api/auth/logout")
async def logout(response: Response):
    clear_session_cookie(response)
    return {"status": "ok"}


@router.get("/api/auth/me")
async def me(request: Request):
    db  = UsersDB.get()
    uid = get_current_user_id_optional(request)
    user = db.get_by_id(uid) if uid is not None else None    # uid is None for a revoked session too
    if not user:
        # signup_open lets the login page hide "Create account" when
        # it would only lead to a 403.
        return JSONResponse({
            "authenticated": False,
            "signup_open": cfg.ALLOW_SIGNUP or db.count() == 0,
        })
    return {"authenticated": True, "id": user["id"], "email": user["email"]}


@router.post("/api/auth/change-password")
async def change_password(body: PasswordChange, request: Request, response: Response,
                          uid: int = Depends(get_current_user_id)):
    """
    Change the password AND revoke every other session (this one is re-issued
    at the new version so you stay signed in). Being logged in isn't enough to
    change it — the current password is required, and wrong guesses count
    against the same lockout as logins, so a stolen session can't be used to
    brute-force it.

    Wrong current password answers 403, deliberately NOT 401: the web app
    treats any 401 as "your session expired" and reloads.
    """
    db   = UsersDB.get()
    user = db.get_by_id(uid)
    ip   = _client_ip(request)
    wait = login_throttle.retry_after(ip, user["email"])
    if wait:
        raise HTTPException(status_code=429,
                            detail=f"Too many failed attempts. Try again in {wait // 60 + 1} minute(s).",
                            headers={"Retry-After": str(wait)})
    try:
        new_version = db.change_password(uid, body.current_password, body.new_password)
    except PermissionError as e:
        login_throttle.record_failure(ip, user["email"])
        raise HTTPException(status_code=403, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    login_throttle.record_success(ip, user["email"])
    set_session_cookie(response, uid, new_version)
    return {"status": "ok"}


@router.post("/api/auth/logout-everywhere")
async def logout_everywhere(response: Response, uid: int = Depends(get_current_user_id)):
    """Sign out every device, including this one's old cookie; this browser
    is re-issued a fresh session so the person isn't bounced to the login screen."""
    set_session_cookie(response, uid, UsersDB.get().bump_session_version(uid))
    return {"status": "ok"}
