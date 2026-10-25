"""
server/static_shell.py — which URLs belong to the Next.js app's static export.

Pure path logic with no FastAPI import, so it's unit-testable on its own
(tests/test_static_shell.py).

Why this exists: the auth gate (server/app.py) blocks every URL without a
session. But the login screen is part of the Next app itself, so the app's
static files — HTML shells, JS, CSS — must load for a logged-out visitor.
Rather than a hand-maintained allow-list of page routes (which silently breaks
the day someone adds a page), a URL is public exactly when it maps to a real
file inside the exported build directory. Those files contain no user data;
everything with data is under /api/ and still needs a session. Anything the
export doesn't contain — /screenshots, /docs — stays protected by default.
"""

import re
from pathlib import Path

# Always public: the auth endpoints themselves, the legacy static dir, and
# /login (a redirect kept for old bookmarks — see server/routes/auth.py).
PUBLIC_EXACT    = {"/login", "/favicon.ico"}
PUBLIC_PREFIXES = ("/api/auth/", "/static/")

# The runner endpoints (server/routes/runner.py) are public to the SESSION gate
# because a runner has no session: it proves itself with a bearer token that each
# of those routes checks itself (server/routes/runner.py require_runner,
# core/runner_auth.py). That makes this the one
# deliberate hole in the gate, so it is as small as it can be: EXACTLY these
# paths and nothing that merely starts with /api/runner/ - a task id is 32 hex
# characters, no dot-segments, no trailing anything. A new runner endpoint must be
# added here on purpose. (Token management for the owner lives elsewhere, under
# /api/settings/runner-tokens, behind the normal session gate.)
RUNNER_PATH = re.compile(r"/api/runner/(?:claim|[0-9a-f]{32}/(?:events|screenshot|result|resume))")


def is_runner_path(path: str) -> bool:
    return RUNNER_PATH.fullmatch(path) is not None


# The pages the Next app owns. Only used for the DEV redirect (when there is no
# build to serve, the backend sends these to the Next dev server). Production
# doesn't depend on this list — it goes by which files exist in the export.
SHELL_ROUTES = ("/", "/applications", "/insights", "/profile", "/settings")

# Old page URLs and the page that replaced each (server/routes/onboarding.py
# redirects them, with or without a trailing slash). Public: a redirect carries
# no data, and a logged-out bookmark should still end up on the right page.
LEGACY_REDIRECTS = {"/apply": "/applications/", "/tracking": "/applications/",
                    "/jobs": "/applications/", "/setup": "/"}


def normalize(path: str) -> str:
    """'/jobs/' → '/jobs', '//' → '/'"""
    stripped = path.strip("/")
    return "/" + stripped if stripped else "/"


def resolve_static_file(ui_dir: Path, url_path: str) -> Path | None:
    """
    The file inside ui_dir that would serve url_path, or None.

    Mirrors what Starlette's StaticFiles(html=True) will serve (the file
    itself, dir/index.html, or a bare .html), and — the security-relevant
    part — NEVER returns a path outside ui_dir, however the URL is written
    (../ segments, symlinks, absolute-looking paths, NUL bytes).
    """
    try:
        root = ui_dir.resolve()
        rel = url_path.strip("/")
        candidates = [rel, f"{rel}/index.html" if rel else "index.html", f"{rel}.html"] if rel \
            else ["index.html"]
        for cand in candidates:
            p = (root / cand).resolve()
            if p.is_relative_to(root) and p.is_file():
                return p
    except (ValueError, OSError):
        pass
    return None


def is_public_path(path: str, ui_dir: Path | None) -> bool:
    if path in PUBLIC_EXACT or path.startswith(PUBLIC_PREFIXES) or is_runner_path(path):
        return True
    if normalize(path) in SHELL_ROUTES or normalize(path) in LEGACY_REDIRECTS:
        return True
    return ui_dir is not None and resolve_static_file(ui_dir, path) is not None
