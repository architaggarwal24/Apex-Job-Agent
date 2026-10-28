# tests/test_old_frontend_removed.py
"""
tests/test_old_frontend_removed.py — the old frontend and the dead backend wrappers are gone,
and nothing still points at them.

  * the server-rendered template pages are no longer served, and no route module keeps a
    template helper; the leftover Vite app next to the Next one is deleted;
  * the old /api/jobs list, /api/jobs/{id}/score and /brief, /api/apply/sessions,
    /api/tracking/pipeline and /api/tracking/stage are no longer routes, while every
    endpoint the Next app still calls (and the apply session WebSocket) is;
  * the old page URLs are redirects, and the sidebar is exactly Today, Applications,
    Insights, Profile, Settings, each of which is a real page of the Next app;
  * no file in the frontend, the backend or the tests refers to a removed route or file.

The redirects themselves are exercised over HTTP in tests/test_http_auth.py. No network, no LLM.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "frontend" / "src"

REMOVED_ROUTES = {
    ("GET", "/api/jobs"), ("POST", "/api/jobs/{job_id}/score"), ("POST", "/api/jobs/{job_id}/brief"),
    ("GET", "/api/apply/sessions"), ("GET", "/api/tracking/pipeline"), ("POST", "/api/tracking/stage/{job_id}"),
}
STILL_USED = {      # what frontend/src calls (or main.py / the runner rely on)
    ("GET", "/api/jobs/{job_id}"), ("DELETE", "/api/jobs"), ("DELETE", "/api/jobs/duplicates"),
    ("GET", "/api/stats"), ("POST", "/api/pipeline/run"), ("POST", "/api/pipeline/cancel"),
    ("POST", "/api/apply/start/{job_id}"), ("GET", "/api/apply/status/{job_id}"),
    ("POST", "/api/apply/answer/{job_id}"), ("POST", "/api/apply/submit/{job_id}"),
    ("POST", "/api/apply/skip/{job_id}"), ("GET", "/api/apply/limits"),
    ("POST", "/api/tracking/note/{job_id}"), ("GET", "/api/tracking/analytics"),
    ("POST", "/api/setup/upload-resume"), ("GET", "/api/setup/checklist"),
}
OLD_URLS = {"/apply": "/applications/", "/tracking": "/applications/", "/jobs": "/applications/", "/setup": "/"}
SIDEBAR = [("/", "Today"), ("/applications", "Applications"), ("/insights", "Insights"),
           ("/profile", "Profile"), ("/settings", "Settings")]

# A reference to something this change removed. Each pattern is written so that the
# routes and files that remain (GET /api/jobs/{id}, DELETE /api/jobs, lib/theme.js, the
# old URLs as redirects) do not match.
REFERENCES = [
    r"/api/apply/sessions", r"/api/tracking/(?:pipeline|stage)\b",
    r"/api/jobs/(?:\{[^}]*\}|\$\{[^}]*\}|[\w-]+)/(?:score|brief)\b",
    r"""\.get\(\s*f?["']/api/jobs["'?]""",                       # a Python GET of the old list
    r"""\bapi\(\s*['"`]/api/jobs(?:\?[^'"`]*)?['"`]\s*\)""",      # a frontend GET of the old list
    r"\b(?:apply|dashboard|jobs|profile|settings|tracking)\.html\b",
    r"\bApp\.jsx\b", r"\bmain\.jsx\b", r"\bvite\b", r"\btheme\.css\b", r"src/pages\b", r"src/api\.js\b",
    r"frontend/index\.html", r"(?<![\w.])app/jobs\b", r"def _t\(", r"""["']templates["']""", r"react-router",
    # app/jobs: the route folder, not himalayas.app/jobs
]
SCANNED = ["frontend/src", "frontend/next.config.mjs", "frontend/README.md", "frontend/package.json",
           "frontend/jsconfig.json", "server", "core", "db", "applier", "runner", "intelligence",
           "scraper", "tracking", "main.py", "tests"]


def _app_routes():
    from server.app import create_app
    app = create_app(ui_dir=None)
    http, ws = set(), set()

    def flat(routes):          # newer FastAPI wraps each include_router() instead of copying its routes
        for r in routes:
            inner = getattr(r, "original_router", None)
            yield from (flat(inner.routes) if inner is not None else [r])

    for r in flat(app.routes):
        methods = getattr(r, "methods", None)
        if methods:
            http |= {(m, r.path) for m in methods}
        elif type(r).__name__ in ("APIWebSocketRoute", "WebSocketRoute"):
            ws.add(r.path)
    return http, ws


def _files():
    for rel in SCANNED:
        p = ROOT / rel
        paths = [p] if p.is_file() else sorted(p.rglob("*"))
        for f in paths:
            if (f.is_file() and f.suffix in (".py", ".js", ".jsx", ".mjs", ".json", ".md", ".css", ".html")
                    and "__pycache__" not in f.parts and f.resolve() != Path(__file__).resolve()):
                yield f


class RoutesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http, cls.ws = _app_routes()

    def test_the_unused_wrappers_are_no_longer_routes(self):
        self.assertEqual(REMOVED_ROUTES & self.http, set())

    def test_everything_the_app_still_calls_is_still_a_route(self):
        self.assertEqual(STILL_USED - self.http, set())
        self.assertIn("/ws/{job_id}", self.ws)          # the apply tab's live log

    def test_old_page_urls_are_redirects_with_and_without_a_trailing_slash(self):
        from server.static_shell import LEGACY_REDIRECTS
        self.assertEqual(LEGACY_REDIRECTS, OLD_URLS)
        for old in OLD_URLS:
            self.assertIn(("GET", old), self.http)
            self.assertIn(("GET", old + "/"), self.http)
            self.assertNotIn(("POST", old), self.http)

    def test_no_route_module_serves_a_template(self):
        for f in sorted((ROOT / "server").rglob("*.py")):
            text = f.read_text(encoding="utf-8")
            self.assertNotRegex(text, r"def _t\(", f.name)
            self.assertNotIn("HTMLResponse", text, f.name)
            self.assertNotRegex(text, r"""["']templates["']""", f.name)

    def test_only_the_unported_setup_page_is_left_in_templates(self):
        # templates/setup.html stays (unserved: /setup redirects to Today) because it is the
        # only screen that edits the search titles/locations once the checklist is done.
        left = sorted(p.name for p in (ROOT / "templates").glob("*")) if (ROOT / "templates").is_dir() else []
        self.assertEqual(left, ["setup.html"])


class FrontendTests(unittest.TestCase):
    def test_the_vite_app_is_gone(self):
        for rel in ("src/App.jsx", "src/main.jsx", "src/api.js", "src/theme.css", "src/pages",
                    "vite.config.js", "index.html", "src/app/jobs"):
            self.assertFalse((ROOT / "frontend" / rel).exists(), rel)

    def test_sidebar_is_today_applications_insights_profile_settings_and_each_is_a_page(self):
        shell = (SRC / "components" / "layout" / "AppShell.jsx").read_text(encoding="utf-8")
        nav = shell[shell.index("const NAV"):shell.index("]", shell.index("const NAV"))]
        self.assertEqual(re.findall(r"\{ href: '([^']*)', label: '([^']*)' \}", nav), SIDEBAR)
        self.assertNotIn("legacy", nav)
        for href, _ in SIDEBAR:
            page = SRC / "app" / href.strip("/") / "page.jsx"
            self.assertTrue(page.is_file(), f"{href} would 404: no {page.relative_to(ROOT)}")

    def test_nothing_refers_to_a_removed_route_or_file(self):
        hits = []
        for f in _files():
            for n, line in enumerate(f.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                hits += [f"{f.relative_to(ROOT)}:{n}: {line.strip()}" for pat in REFERENCES if re.search(pat, line)]
        self.assertEqual(hits, [])

    def test_the_reference_patterns_do_catch_what_they_are_for(self):
        # Guards the scan above against patterns that can never match.
        samples = ["api('/api/apply/sessions')", "fetch('/api/tracking/pipeline')", "/api/tracking/stage/${id}",
                   "send(`/api/jobs/${jobId}/score`, 'POST')", '@router.post("/api/jobs/{job_id}/brief")',
                   'self.client.get("/api/jobs")', "c.get(\"/api/jobs?status=applied\")", "api('/api/jobs')",
                   "templates/apply.html", "import App from './App.jsx'", "vite.config.js",
                   "import './theme.css'", "frontend/src/pages/Jobs.jsx", "def _t(name):",
                   'ROOT / "templates" / name', "src/app/jobs/page.jsx", "from 'react-router-dom'"]
        for s in samples:
            self.assertTrue(any(re.search(p, s) for p in REFERENCES), s)
        for ok in ["api(`/api/jobs/${jobId}`)", "api('/api/jobs', { method: 'DELETE' })", 'self.client.delete("/api/jobs")',
                   "import { THEMES } from '@/lib/theme'", "/api/tracking/note/x", "/api/apply/limits",
                   '"/jobs": "/applications/"', "index.html", "/api/stats"]:
            self.assertFalse(any(re.search(p, ok) for p in REFERENCES), ok)


if __name__ == "__main__":
    unittest.main()
