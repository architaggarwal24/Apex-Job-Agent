# core/runner_auth.py
"""
How a RUNNER proves who it is: a bearer token in the Authorization header, and
NOTHING else. No cookie is read, no session, no query parameter, so

  * a runner token is useless on every endpoint that wants a signed-in user
    (those read the session cookie - core/auth.py - and never this header), and
  * a browser session is useless on the runner endpoints, which accept only this.

Plain Python on purpose (no FastAPI import): the FastAPI dependency that calls
it is server/routes/runner.py's require_runner, and the exact set of paths that
are let past the session gate for it is server/static_shell.py's RUNNER_PATH.
"""

from dataclasses import dataclass

from db import runner_db


@dataclass(frozen=True)
class RunnerAuth:
    token_id: int
    user_id: int
    label: str


def parse_bearer(header) -> str | None:
    """The token from `Authorization: Bearer <token>`, or None for anything
    else: no header, another scheme (Basic, Token...), a missing or empty token,
    or extra words. The scheme is case-insensitive, as HTTP says."""
    if not isinstance(header, str):
        return None
    parts = header.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        return None
    return parts[1]


def authenticate_header(header, path=None, **kw) -> RunnerAuth | None:
    """Who is behind this Authorization header? A live (not revoked, user still
    active) runner token's owner, or None. Marks the token as seen. Extra
    keyword arguments go to db.runner_db.authenticate (tests inject the clock)."""
    token = parse_bearer(header)
    if token is None:
        return None
    found = runner_db.authenticate(token, path, **kw)
    return RunnerAuth(found["token_id"], found["user_id"], found["label"]) if found else None
