# tests/test_saved_views.py
"""
tests/test_saved_views.py — saved filter views of the Applications page
(db/views_db.py, server/routes/views.py):

  * a view is a name and the page's own query; saving under the same name replaces it
  * the query is checked like a bookmark (starts with "?", plain characters, bounded),
    and an open job (?job=) is not saved as part of a view
  * names are 1-60 characters; at most MAX_VIEWS per account
  * per user: one account never sees, replaces or deletes another's views
  * the routes need a session (any account, not only the owner)

No network, no LLM.
"""

import contextlib
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
os.environ.setdefault("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())

from db.views_db import MAX_VIEWS, ViewStore, clean_query

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except Exception:
    HAVE_FASTAPI = False


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "jobs.db"
        self.alice, self.bob = ViewStore(1, self.path), ViewStore(2, self.path)

    def test_save_list_replace_delete(self):
        v = self.alice.save("  Remote   AI ≥ 70 ", "?work=remote&min=70&kind=ai")
        self.assertEqual((v["name"], v["query"]), ("Remote AI ≥ 70", "?work=remote&min=70&kind=ai"))
        self.alice.save("Remote AI ≥ 70", "?work=remote&min=80")
        self.assertEqual([(x["name"], x["query"]) for x in self.alice.list()], [("Remote AI ≥ 70", "?work=remote&min=80")])
        self.assertTrue(self.alice.delete(v["id"]))
        self.assertEqual(self.alice.list(), [])
        self.assertFalse(self.alice.delete(v["id"]))

    def test_the_query_is_a_plain_bookmark(self):
        self.assertEqual(clean_query(""), "")
        self.assertEqual(clean_query("?job=abc&min=70"), "?min=70", "an open job is not a filter")
        self.assertEqual(clean_query("?min=70&job=abc"), "?min=70")
        self.assertEqual(clean_query("?job=abc"), "")
        self.assertEqual(clean_query("?co=Acme%20AI,KLA&loc=pune"), "?co=Acme%20AI,KLA&loc=pune")
        for bad in ("min=70", "?a=<script>", "?a=1\n2", "?" + "a" * 1001, "javascript:alert(1)", '?x="y"'):
            with self.assertRaises(ValueError, msg=bad):
                clean_query(bad)

    def test_names(self):
        for bad in ("", "   ", "x" * 61, None):
            with self.assertRaises(ValueError):
                self.alice.save(bad, "?min=1")

    def test_at_most_max_views(self):
        for i in range(MAX_VIEWS):
            self.alice.save(f"v{i}", "")
        with self.assertRaises(ValueError):
            self.alice.save("one more", "")
        self.alice.save("v0", "?min=5")                    # replacing one is still fine
        self.bob.save("mine", "")                          # the limit is per account

    def test_per_user(self):
        a = self.alice.save("Mine", "?min=70")
        self.bob.save("Mine", "?min=10")
        self.assertEqual([x["query"] for x in self.alice.list()], ["?min=70"])
        self.assertFalse(self.bob.delete(a["id"]))
        self.assertEqual(len(self.alice.list()), 1)
        with contextlib.closing(sqlite3.connect(self.path)) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM saved_views").fetchone()[0], 2)


@unittest.skipUnless(HAVE_FASTAPI, "fastapi + httpx not installed")
class RouteTests(unittest.TestCase):
    def setUp(self):
        from core.config import cfg
        import core.auth as auth
        from db.users_db import UsersDB
        from db.settings_db import SettingsDB
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        self._saved_settings = SettingsDB._instance
        SettingsDB._instance = SettingsDB(path=tmp / "settings.db")
        self._saved = (cfg.JOBS_DB_PATH, cfg.PROFILE_DB_PATH, cfg.SESSIONS_DIR, cfg.APP_SECRET_KEY,
                       cfg.COOKIE_SECURE, cfg.ALLOW_SIGNUP, UsersDB._instance, auth._serializer)
        cfg.JOBS_DB_PATH, cfg.PROFILE_DB_PATH, cfg.SESSIONS_DIR = tmp / "jobs.db", tmp / "profile.db", tmp / "sessions"
        cfg.SESSIONS_DIR.mkdir()
        cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE, cfg.ALLOW_SIGNUP = "test-signing-secret-" + "x" * 40, False, False
        UsersDB._instance, auth._serializer = UsersDB(path=tmp / "users.db"), None
        self._saved_dev_origin = os.environ.pop("APEX_DEV_UI_ORIGIN", None)
        from server.app import create_app
        self.app = create_app(ui_dir=tmp / "no-build")
        self.alice = TestClient(self.app, follow_redirects=False)
        self.alice.post("/api/auth/signup", json={"email": "alice@example.com", "password": "correct-horse-battery"})
        cfg.ALLOW_SIGNUP = True
        self.bob = TestClient(self.app, follow_redirects=False)
        self.bob.post("/api/auth/signup", json={"email": "bob@example.com", "password": "correct-horse-battery"})

    def tearDown(self):
        from core.config import cfg
        import core.auth as auth
        from db.users_db import UsersDB
        from db.settings_db import SettingsDB
        SettingsDB._instance = self._saved_settings
        (cfg.JOBS_DB_PATH, cfg.PROFILE_DB_PATH, cfg.SESSIONS_DIR, cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE,
         cfg.ALLOW_SIGNUP, UsersDB._instance, auth._serializer) = self._saved
        os.environ.pop("APEX_DEV_UI_ORIGIN", None)
        if self._saved_dev_origin is not None:
            os.environ["APEX_DEV_UI_ORIGIN"] = self._saved_dev_origin

    def test_round_trip_and_isolation(self):
        r = self.alice.post("/api/views", json={"name": "Remote", "query": "?work=remote&job=x"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["query"], "?work=remote")
        self.assertEqual([v["name"] for v in self.alice.get("/api/views").json()["views"]], ["Remote"])
        self.assertEqual(self.bob.get("/api/views").json()["views"], [], "another account's views stay theirs")
        vid = r.json()["id"]
        self.assertEqual(self.bob.delete(f"/api/views/{vid}").status_code, 404)
        self.assertEqual(self.bob.post("/api/views", json={"name": "Bob's", "query": ""}).status_code, 200, "not owner-only")
        self.assertEqual(self.alice.delete(f"/api/views/{vid}").status_code, 200)
        self.assertEqual(self.alice.get("/api/views").json()["views"], [])

    def test_bad_input_is_a_400_and_no_session_is_a_401(self):
        self.assertEqual(self.alice.post("/api/views", json={"name": "", "query": ""}).status_code, 400)
        self.assertEqual(self.alice.post("/api/views", json={"name": "x", "query": "<b>"}).status_code, 400)
        self.assertIn(self.alice.post("/api/views", json=["x"]).status_code, (400, 422))       # not an object: refused by the body schema
        anon = TestClient(self.app)
        self.assertEqual(anon.get("/api/views").status_code, 401)
        self.assertEqual(anon.post("/api/views", json={"name": "x", "query": ""}).status_code, 401)


if __name__ == "__main__":
    unittest.main()
