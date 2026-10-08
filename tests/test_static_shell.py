"""
tests/test_static_shell.py — which URLs are public. Pure logic, no FastAPI needed.

Run from the project root:   python3 -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.static_shell import is_public_path, normalize, resolve_static_file


class StaticShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.out = base / "out"
        (self.out / "jobs").mkdir(parents=True)
        (self.out / "_next" / "static").mkdir(parents=True)
        (self.out / "index.html").write_text("home")
        (self.out / "jobs" / "index.html").write_text("jobs")
        (self.out / "flat.html").write_text("flat")
        (self.out / "_next" / "static" / "app.js").write_text("js")
        (self.out / "404.html").write_text("nf")
        # Things OUTSIDE the export that must never be reachable:
        (base / "secret.txt").write_text("secret")
        (base / "users.db").write_text("db")
        self.base = base

    def tearDown(self):
        self.tmp.cleanup()

    def public(self, p):
        return is_public_path(p, self.out)

    def test_normalize(self):
        for raw, want in [("/", "/"), ("//", "/"), ("/jobs", "/jobs"), ("/jobs/", "/jobs"), ("", "/")]:
            self.assertEqual(normalize(raw), want, raw)

    def test_exported_files_are_public(self):
        for p in ["/", "/jobs", "/jobs/", "/flat", "/_next/static/app.js", "/404.html", "/index.html"]:
            self.assertTrue(self.public(p), p)

    def test_things_the_export_does_not_contain_stay_protected(self):
        for p in ["/apply", "/tracking", "/setup", "/screenshots/x.png", "/docs", "/openapi.json",
                  "/api/jobs", "/api/settings", "/ws/abc", "/nonexistent", "/_next/static/missing.js"]:
            self.assertFalse(self.public(p), p)

    def test_auth_endpoints_and_login_redirect_always_public(self):
        for p in ["/api/auth/login", "/api/auth/me", "/login", "/static/x.css"]:
            self.assertTrue(is_public_path(p, None), p)
        self.assertFalse(is_public_path("/api/auth", None))          # prefix needs the trailing slash
        self.assertFalse(is_public_path("/api/authx/login", None))

    def test_directory_itself_is_not_a_file(self):
        self.assertIsNone(resolve_static_file(self.out, "/_next"))
        self.assertFalse(self.public("/_next"))

    def test_path_traversal_can_never_escape_the_export(self):
        attacks = ["/../secret.txt", "/../users.db", "/jobs/../../secret.txt",
                   "/../../../../etc/passwd", "/..%2fsecret.txt", "//..//secret.txt",
                   "/_next/../../secret.txt", "/jobs/../../users.db", "/\x00", "/jobs\x00.html",
                   "/..\\secret.txt", "/../out/../secret.txt"]
        for a in attacks:
            self.assertIsNone(resolve_static_file(self.out, a), repr(a))
            self.assertFalse(self.public(a), repr(a))

    def test_absolute_looking_paths_are_treated_as_relative(self):
        self.assertIsNone(resolve_static_file(self.out, "/etc/passwd"))
        self.assertIsNone(resolve_static_file(self.out, str(self.base / "secret.txt")))

    @unittest.skipIf(os.name == "nt", "symlinks need privileges on Windows")
    def test_symlink_out_of_the_export_is_refused(self):
        (self.out / "leak").symlink_to(self.base / "secret.txt")
        self.assertIsNone(resolve_static_file(self.out, "/leak"))
        self.assertFalse(self.public("/leak"))

    def test_no_build_directory(self):
        self.assertFalse(is_public_path("/anything", None))
        self.assertFalse(is_public_path("/anything", self.base / "does-not-exist"))
        # dev redirect targets stay public even with no build, so an anonymous
        # visitor is sent to the dev server rather than into a redirect loop
        for p in ["/", "/jobs", "/profile", "/settings"]:
            self.assertTrue(is_public_path(p, None), p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
