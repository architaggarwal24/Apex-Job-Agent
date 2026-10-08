"""
core/auth.py — session authentication for multi-user deployment.

A session is a signed, expiring token (itsdangerous) carried in an
httponly cookie — not a raw user ID, so a viewer can't forge or tamper
with "who am I" by editing the cookie value directly; any tampering
invalidates the signature and the session is rejected.

Signing key: APP_SECRET_KEY from .env, IF you've actually changed it
from the shipped placeholder. This project's .env.example has shipped
APP_SECRET_KEY=change_me since day one — every clone of this repo has
the same value until someone edits it, so trusting it as-is would mean
anyone could forge a valid session for any account. If it's still that
placeholder (or empty), a random secret is generated instead and
persisted at data/.session_secret — same "back this up, losing it logs
everyone out" logic as core/crypto.py's master key, and for the same
reason: the key has to live somewhere, and a generated-and-kept-local
file is the honest right-sized answer for a self-hosted/small
deployment. Set APP_SECRET_KEY explicitly for anything bigger.
"""

import os
import stat
import logging
import secrets

from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired
from fastapi import Request, HTTPException

from core.config import cfg

logger = logging.getLogger(__name__)

SESSION_COOKIE  = "apex_session"
SESSION_MAX_AGE = 60 * 60 * 24 * 14  # 14 days

_SECRET_PATH = cfg.DATA_DIR / ".session_secret"
_serializer  = None


# Every value that has ever shipped in this repo's config or example
# files. Any of these is public, so none may be used as a signing key.
_PLACEHOLDER_SECRETS = {"", "change_me", "change_this_to_random_string"}
_MIN_SECRET_LEN      = 32


def _is_usable_secret(value: str) -> bool:
    return value not in _PLACEHOLDER_SECRETS and len(value) >= _MIN_SECRET_LEN


def _load_or_create_secret() -> str:
    configured = cfg.APP_SECRET_KEY
    if _is_usable_secret(configured):
        return configured
    if configured not in _PLACEHOLDER_SECRETS:
        logger.warning(
            f"[Auth] APP_SECRET_KEY is set but shorter than {_MIN_SECRET_LEN} "
            "characters — ignoring it and using a generated key instead. "
            "Generate a proper one with: python3 -c \"import secrets; "
            "print(secrets.token_urlsafe(48))\""
        )

    if _SECRET_PATH.exists():
        return _SECRET_PATH.read_text().strip()

    secret = secrets.token_urlsafe(48)
    _SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SECRET_PATH.write_text(secret)
    try:
        os.chmod(_SECRET_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0600
    except Exception:
        pass
    logger.warning(
        f"[Auth] APP_SECRET_KEY is still the shipped placeholder ('change_me') "
        f"— generated a real signing key at {_SECRET_PATH} instead. BACK IT UP: "
        "losing it logs out every session at once (not otherwise harmful — "
        "just re-login — but worth knowing before it happens by surprise). "
        "Set APP_SECRET_KEY in the environment for deployments that should "
        "manage this externally rather than as a local file."
    )
    return secret


def _get_serializer() -> URLSafeTimedSerializer:
    global _serializer
    if _serializer is None:
        _serializer = URLSafeTimedSerializer(_load_or_create_secret(), salt="apex-session-v1")
    return _serializer


def create_session_token(user_id: int, session_version: int = 0) -> str:
    return _get_serializer().dumps({"uid": user_id, "sv": session_version})


def read_session_token(token: str) -> tuple[int, int] | None:
    """(user_id, session_version) if the signature and age are valid, else None.
    Tokens issued before versions existed carry no "sv"; they count as 0, which
    is what every pre-existing account starts at — so nobody is logged out by
    upgrading."""
    try:
        data = _get_serializer().loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    uid = data.get("uid") if isinstance(data, dict) else None
    if not isinstance(uid, int) or isinstance(uid, bool):
        return None
    sv = data.get("sv", 0)
    return uid, sv if isinstance(sv, int) else 0


def verify_session_token(token: str) -> int | None:
    """Signature-only check (does NOT consult the database). Prefer
    user_for_token() for anything that decides access."""
    parsed = read_session_token(token)
    return parsed[0] if parsed else None


def user_for_token(token: str | None, users=None) -> dict | None:
    """
    THE answer to "who is this session?" — used by the HTTP gate, /api/auth/me
    and the WebSocket, so they can never disagree. A token is honoured only if
    all three hold: the signature is valid and unexpired, the account still
    exists and is active, and the token's session version is still current.
    (If /me skipped the version check, a revoked browser would think it was
    logged in while every API call 401'd — a reload loop.)
    """
    if not token:
        return None
    parsed = read_session_token(token)
    if parsed is None:
        return None
    uid, sv = parsed
    if users is None:
        from db.users_db import UsersDB
        users = UsersDB.get()
    user = users.get_by_id(uid)
    if user is None or user.get("session_version", 0) != sv:
        return None
    return user


def set_session_cookie(response, user_id: int, session_version: int = 0):
    response.set_cookie(
        SESSION_COOKIE, create_session_token(user_id, session_version),
        max_age=SESSION_MAX_AGE, httponly=True, samesite="lax",
        secure=cfg.COOKIE_SECURE,
    )


def clear_session_cookie(response):
    response.delete_cookie(SESSION_COOKIE)


def get_current_user_id(request: Request) -> int:
    """FastAPI dependency for API routes that require login: 401s with
    a clear message if there's no valid session, rather than silently
    proceeding as some default/global user."""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Not logged in")
    user = user_for_token(token)
    if user is None:
        raise HTTPException(status_code=401, detail="Session expired or invalid — please log in again")
    return user["id"]


def get_current_user_id_optional(request: Request) -> int | None:
    """Same check, but returns None instead of raising — for places that
    need to behave differently when logged out rather than reject the
    request outright (e.g. the login page itself, or a page that should
    redirect-to-login rather than show a bare 401 JSON error)."""
    user = user_for_token(request.cookies.get(SESSION_COOKIE))
    return user["id"] if user else None


# ── Owner-only guard (transitional) ──────────────────────────────
#
# Scraping and applying still drive ONE shared browser session (the
# owner's) and read ONE global profile. Until browser sessions and
# profiles are per-user, letting any account trigger those actions would
# mean user 2 clicking "Run pipeline" drives user 1's LinkedIn login.
# So those specific actions fail closed for everyone except the first
# account (the person who was running this as a single-user tool).
# Remove this guard route-by-route as each is migrated to per-user
# browser profiles.

OWNER_USER_ID = 1


def require_owner(request: Request) -> int:
    uid = get_current_user_id(request)
    if uid != OWNER_USER_ID:
        raise HTTPException(
            status_code=403,
            detail="This action isn't available on shared accounts yet.",
        )
    return uid
