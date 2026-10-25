# tests/test_applications_http.py
"""
tests/test_applications_http.py — the lifecycle API over HTTP: auth, per-user
isolation, status codes, and that the old /api/jobs/{id}, /api/apply/* and
/api/tracking/* endpoints the UI still uses answer in their old shapes.

Run from the project root:   python3 -m unittest discover -s tests -v

NOTE: like tests/test_http_auth.py this skips itself when FastAPI/httpx aren't
installed. It was written in a sandbox without them, so it has NOT been run
yet; if one of these fails after `pip install -r requirements.txt`, read the
failure as a real finding first. The stream itself (frames, resume, isolation)
is tested without HTTP in tests/test_lifecycle.py; only its 401 is checked
here, because an endless response can't be read to completion by TestClient.
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
os.environ["APP_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except Exception:
    HAVE_FASTAPI = False


@unittest.skipUnless(HAVE_FASTAPI, "fastapi + httpx not installed")
class ApplicationsHttpTests(unittest.TestCase):

    def setUp(self):
        from core.config import cfg
        import core.auth as auth
        from db.users_db import UsersDB
        from db.settings_db import SettingsDB

        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self._saved_settings = SettingsDB._instance
        SettingsDB._instance = SettingsDB(path=tmp / "settings.db")
        self._saved = (cfg.JOBS_DB_PATH, cfg.SESSIONS_DIR, cfg.APP_SECRET_KEY,
                       cfg.COOKIE_SECURE, cfg.ALLOW_SIGNUP, UsersDB._instance,
                       auth._serializer)
        cfg.JOBS_DB_PATH   = tmp / "jobs.db"
        cfg.SESSIONS_DIR   = tmp / "sessions"
        cfg.SESSIONS_DIR.mkdir()
        cfg.APP_SECRET_KEY = "test-signing-secret-" + "x" * 40
        cfg.COOKIE_SECURE  = False
        cfg.ALLOW_SIGNUP   = False
        UsersDB._instance  = UsersDB(path=tmp / "users.db")
        auth._serializer   = None
        self._saved_dev_origin = os.environ.pop("APEX_DEV_UI_ORIGIN", None)

        from server.app import create_app
        self.app    = create_app(ui_dir=tmp / "no-build")
        self.client = TestClient(self.app, follow_redirects=False)      # alice (user 1)
        self.client.post("/api/auth/signup",
                         json={"email": "alice@example.com", "password": "correct-horse-battery"})
        cfg.ALLOW_SIGNUP = True
        self.bob = TestClient(self.app, follow_redirects=False)         # bob (user 2)
        self.bob.post("/api/auth/signup",
                      json={"email": "bob@example.com", "password": "correct-horse-battery"})

    def tearDown(self):
        from core.config import cfg
        import core.auth as auth
        from db.users_db import UsersDB
        from db.settings_db import SettingsDB
        SettingsDB._instance = self._saved_settings
        (cfg.JOBS_DB_PATH, cfg.SESSIONS_DIR, cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE,
         cfg.ALLOW_SIGNUP, UsersDB._instance, auth._serializer) = self._saved
        os.environ.pop("APEX_DEV_UI_ORIGIN", None)
        if self._saved_dev_origin is not None:
            os.environ["APEX_DEV_UI_ORIGIN"] = self._saved_dev_origin
        self.tmp.cleanup()

    def add(self, uid, n, **kw):
        from db.jobs_db import JobsDB
        j = {"url": f"https://x.com/{uid}/{n}", "title": f"Job {n}", "company": f"Co {n}",
             "source": "linkedin", "location": "Pune"}
        j.update(kw)
        return JobsDB(user_id=uid).add(j)

    # ── auth ──────────────────────────────────────────────────────

    def test_new_endpoints_require_login(self):
        anon = TestClient(self.app, follow_redirects=False)
        for method, path in [("get", "/api/applications"), ("get", "/api/applications/x"),
                             ("post", "/api/applications/x/transition"),
                             ("get", "/api/events/stream")]:
            r = getattr(anon, method)(path) if method == "get" else anon.post(path, json={})
            self.assertEqual(r.status_code, 401, path)

    # ── list / detail / isolation ─────────────────────────────────

    def test_list_filters_detail_and_per_user_isolation(self):
        a1 = self.add(1, 1, title="Python Dev", source="linkedin")
        a2 = self.add(1, 2, title="Go Dev", source="indeed")
        b1 = self.add(2, 1, title="Bobs Job")
        self.assertEqual(self.client.post(f"/api/applications/{a2}/transition",
                                          json={"to": "queued"}).status_code, 200)

        titles = lambda r: sorted(x["title"] for x in r.json())
        self.assertEqual(titles(self.client.get("/api/applications")), ["Go Dev", "Python Dev"])
        self.assertEqual(titles(self.bob.get("/api/applications")), ["Bobs Job"])
        self.assertEqual(titles(self.client.get("/api/applications?stage=queued")), ["Go Dev"])
        self.assertEqual(titles(self.client.get("/api/applications?source=LinkedIn")), ["Python Dev"])
        self.assertEqual(titles(self.client.get("/api/applications?title=python")), ["Python Dev"])
        self.assertEqual(titles(self.client.get("/api/applications?q=pune&stage=found")), ["Python Dev"])
        self.assertEqual(self.client.get("/api/applications?stage=bogus").status_code, 400)

        d = self.client.get(f"/api/applications/{a2}")
        self.assertEqual(d.status_code, 200)
        self.assertEqual([e["to_stage"] for e in d.json()["events"]], ["found", "queued"])
        self.assertEqual(self.bob.get(f"/api/applications/{a1}").status_code, 404)
        self.assertEqual(self.client.get(f"/api/applications/{b1}").status_code, 404)
        self.assertEqual(self.client.get("/api/applications/nope").status_code, 404)

    # ── transition ────────────────────────────────────────────────

    def test_transition_status_codes_and_audit_event(self):
        j = self.add(1, 1)
        r = self.client.post(f"/api/applications/{j}/transition",
                             json={"to": "queued", "note": "after standup"})
        self.assertEqual(r.status_code, 200)
        last = r.json()["events"][-1]
        self.assertEqual((last["from_stage"], last["to_stage"], last["actor"], last["detail"]),
                         ("found", "queued", "user", {"note": "after standup"}))
        self.assertEqual(r.json()["stage"], "queued")

        self.assertEqual(self.client.post(f"/api/applications/{j}/transition",
                                          json={"to": "offer"}).status_code, 409)
        for bad in ({}, {"to": "applied"}, {"to": 5}, {"to": None}):
            self.assertEqual(self.client.post(f"/api/applications/{j}/transition",
                                              json=bad).status_code, 400, bad)
        self.assertEqual(self.client.get(f"/api/applications/{j}").json()["stage"], "queued")

        self.client.post(f"/api/applications/{j}/transition", json={"to": "preparing"})
        r = self.client.post(f"/api/applications/{j}/transition", json={"to": "needs_you"})
        self.assertEqual((r.status_code, r.json()["blocked_reason"]), (200, "other"))
        self.client.post(f"/api/applications/{j}/transition", json={"to": "preparing"})
        self.assertEqual(self.client.post(f"/api/applications/{j}/transition",
                                          json={"to": "needs_you", "reason": "nope"}).status_code, 400)
        r = self.client.post(f"/api/applications/{j}/transition",
                             json={"to": "needs_you", "reason": "captcha"})
        self.assertEqual(r.json()["blocked_reason"], "captcha")

    def test_cannot_transition_someone_elses_application(self):
        j = self.add(1, 1)
        self.assertEqual(self.bob.post(f"/api/applications/{j}/transition",
                                       json={"to": "skipped"}).status_code, 404)
        self.assertEqual(self.client.get(f"/api/applications/{j}").json()["stage"], "found")

    # ── the old endpoints, now wrappers ───────────────────────────

    def test_tracking_endpoints_keep_their_shape_and_scope_to_the_caller(self):
        a = self.add(1, 1)
        b = self.add(2, 1)
        self.client.post(f"/api/applications/{a}/transition", json={"to": "submitted"})
        r = self.client.post(f"/api/tracking/note/{a}", json={"note": "sent"})
        self.assertEqual((r.status_code, r.json()), (200, {"status": "ok"}))
        self.assertEqual(self.client.post(f"/api/tracking/note/{a}", json={"note": " "}).status_code, 400)
        self.assertEqual(self.bob.post(f"/api/tracking/note/{a}", json={"note": "x"}).status_code, 404)
        self.assertEqual(self.client.get("/api/tracking/analytics").status_code, 200)

        job = self.client.get(f"/api/jobs/{a}").json()
        self.assertEqual(job["status"], "applied")
        self.assertEqual(self.bob.get(f"/api/jobs/{b}").json()["stage"], "found")
        self.assertEqual(self.bob.get(f"/api/jobs/{a}").status_code, 404)

    def test_job_details_and_stats_keep_old_fields_and_add_the_new_ones(self):
        a = self.add(1, 1)
        self.client.post(f"/api/applications/{a}/transition", json={"to": "submitted"})
        row = self.client.get(f"/api/jobs/{a}").json()
        for k in ("job_id", "status", "error_message", "applied_at", "ai_score", "title"):
            self.assertIn(k, row)
        self.assertEqual((row["status"], row["stage"]), ("applied", "submitted"))
        stats = self.client.get("/api/stats").json()
        self.assertEqual(stats["applied"], 1)
        self.assertEqual(stats["pipeline"], {"applied": 1})
        self.assertIn("sessions", stats)

    def test_skip_wrapper(self):
        from applier.session import ApplicationSession
        a = self.add(1, 1)
        ApplicationSession(job_id=a, job_url="u", job_title="t", company="c",
                           source="linkedin", state="awaiting_input").save()
        r = self.client.post(f"/api/apply/skip/{a}")
        self.assertEqual((r.status_code, r.json()), (200, {"status": "skipped"}))
        self.assertEqual(self.client.get(f"/api/applications/{a}").json()["stage"], "skipped")
        self.assertEqual(ApplicationSession.load(a).state, "skipped")
        self.assertEqual(self.client.post(f"/api/apply/skip/{a}").status_code, 200)    # idempotent
        self.assertEqual(self.bob.post(f"/api/apply/skip/{a}").status_code, 404)
        sub = self.add(1, 2)
        self.client.post(f"/api/applications/{sub}/transition", json={"to": "submitted"})
        self.assertEqual(self.client.post(f"/api/apply/skip/{sub}").status_code, 409)

    def test_apply_flow_is_owner_only_and_gated_by_stage_not_by_the_session_file(self):
        from applier.session import ApplicationSession
        b = self.add(2, 1)
        for path in ("start", "answer", "submit"):
            self.assertEqual(self.bob.post(f"/api/apply/{path}/{b}", json={}).status_code, 403, path)

        a = self.add(1, 1)
        # the session file claims "awaiting input", but the lifecycle says found: the stage wins
        ApplicationSession(job_id=a, job_url="u", job_title="t", company="c",
                           source="linkedin", state="awaiting_input").save()
        self.assertEqual(self.client.post(f"/api/apply/answer/{a}", json={"answers": {}}).status_code, 400)
        ApplicationSession(job_id=a, job_url="u", job_title="t", company="c",
                           source="linkedin", state="screenshot_review").save()
        self.assertEqual(self.client.post(f"/api/apply/submit/{a}", json={}).status_code, 400)

        done = self.add(1, 2)
        self.client.post(f"/api/applications/{done}/transition", json={"to": "submitted"})
        r = self.client.post(f"/api/apply/start/{done}")
        self.assertEqual((r.status_code, r.json()), (200, {"status": "already_done"}))
        self.assertEqual(self.client.post("/api/apply/start/nope").status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
