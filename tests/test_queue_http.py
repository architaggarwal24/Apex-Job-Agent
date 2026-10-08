# tests/test_queue_http.py
"""
tests/test_queue_http.py — the queue API over HTTP: auth, owner-only writes,
per-user scoping, request validation, the quality_first setting, and that the
daily cap the apply routes enforce follows it.

Run from the project root:   python3 -m unittest discover -s tests -v

Like tests/test_http_auth.py this skips itself when FastAPI/httpx aren't
installed. The worker's own behaviour (ordering, limits, pause, recovery,
gating) is tested without HTTP in tests/test_queue_worker.py; these tests
never start the background thread.

NOTE: written in a sandbox without FastAPI, so NOT yet run. If one fails after
`pip install -r requirements.txt`, read it as a real finding first.
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
class QueueHttpTests(unittest.TestCase):

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

        from server.routes.queue import reset_worker
        reset_worker()

        from server.app import create_app
        self.app    = create_app(ui_dir=tmp / "no-build")
        self.client = TestClient(self.app, follow_redirects=False)      # alice = owner (user 1)
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
        from server.routes.queue import reset_worker
        reset_worker()
        SettingsDB._instance = self._saved_settings
        (cfg.JOBS_DB_PATH, cfg.SESSIONS_DIR, cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE,
         cfg.ALLOW_SIGNUP, UsersDB._instance, auth._serializer) = self._saved
        os.environ.pop("APEX_DEV_UI_ORIGIN", None)
        if self._saved_dev_origin is not None:
            os.environ["APEX_DEV_UI_ORIGIN"] = self._saved_dev_origin
        self.tmp.cleanup()

    # ── helpers ───────────────────────────────────────────────────

    def add(self, uid, n, score=80, **kw):
        from db.jobs_db import JobsDB
        db = JobsDB(user_id=uid)
        jid = db.add({"url": f"https://x.com/{uid}/{n}", "title": f"Job {n}",
                      "company": f"Co {n}", "source": "linkedin", **kw})
        if score is not None:
            db.update_score(jid, score, "t")
        return jid

    def stage(self, jid, uid=1):
        from db.jobs_db import JobsDB
        return JobsDB(user_id=uid).get_stage(jid)

    def settings(self):
        from db.settings_db import SettingsDB
        return SettingsDB.get()

    def queue(self, ids, client=None):
        return (client or self.client).post("/api/applications/queue", json={"job_ids": ids})

    # ── auth ──────────────────────────────────────────────────────

    def test_every_queue_endpoint_requires_login(self):
        anon = TestClient(self.app, follow_redirects=False)
        for method, path in [("post", "/api/applications/queue"), ("post", "/api/applications/unqueue"),
                             ("post", "/api/queue/pause"), ("post", "/api/queue/resume"),
                             ("get", "/api/queue/status")]:
            r = anon.get(path) if method == "get" else anon.post(path, json={"job_ids": ["x"]})
            self.assertEqual(r.status_code, 401, path)

    def test_driving_the_queue_is_owner_only_but_status_is_open_to_everyone_signed_in(self):
        j = self.add(2, 1)
        for path in ("/api/applications/queue", "/api/applications/unqueue"):
            self.assertEqual(self.bob.post(path, json={"job_ids": [j]}).status_code, 403, path)
        for path in ("/api/queue/pause", "/api/queue/resume"):
            self.assertEqual(self.bob.post(path).status_code, 403, path)
        self.assertEqual(self.stage(j, 2), "found")
        self.assertFalse(self.settings().read("queue_paused", "false") == "true")
        self.assertEqual(self.bob.get("/api/queue/status").status_code, 200)

    # ── queue / unqueue ───────────────────────────────────────────

    def test_queue_and_unqueue_round_trip_with_per_job_results(self):
        a = self.add(1, 1); b = self.add(1, 2, score=5); c = self.add(1, 3, score=None)
        r = self.queue([a, b, c, "nope", a])
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["queued"], [a])
        self.assertEqual({(x["job_id"], x["reason"]) for x in body["rejected"]},
                         {(b, "below_threshold"), (c, "not_scored"), ("nope", "not_found")})
        self.assertEqual((self.stage(a), self.stage(b), self.stage(c)), ("queued", "found", "found"))

        again = self.queue([a]).json()
        self.assertEqual((again["queued"], again["rejected"][0]["reason"]), ([], "already_queued"))

        r = self.client.post("/api/applications/unqueue", json={"job_ids": [a, b]})
        self.assertEqual(r.json()["unqueued"], [a])
        self.assertEqual(r.json()["rejected"][0]["reason"], "not_queued")
        self.assertEqual(self.stage(a), "found")
        events = self.client.get(f"/api/applications/{a}").json()["events"]
        self.assertEqual([(e["to_stage"], e["actor"]) for e in events],
                         [("found", "system"), ("queued", "user"), ("found", "user")])

    def test_you_cannot_queue_or_unqueue_someone_elses_job(self):
        theirs = self.add(2, 1)
        self.assertEqual(self.queue([theirs]).json()["rejected"][0]["reason"], "not_found")
        self.assertEqual(self.stage(theirs, 2), "found")
        from db.jobs_db import JobsDB
        JobsDB(user_id=2).transition(theirs, "queued")
        r = self.client.post("/api/applications/unqueue", json={"job_ids": [theirs]})
        self.assertEqual(r.json()["rejected"][0]["reason"], "not_found")
        self.assertEqual(self.stage(theirs, 2), "queued")

    def test_bad_request_bodies_are_400(self):
        for body in ({}, {"job_ids": []}, {"job_ids": "abc"}, {"job_ids": [1, 2]},
                     {"job_ids": ["a", ""]}, {"job_ids": None},
                     {"job_ids": [f"j{i}" for i in range(201)]}):
            for path in ("/api/applications/queue", "/api/applications/unqueue"):
                self.assertEqual(self.client.post(path, json=body).status_code, 400, (path, body))

    def test_quality_first_off_lets_low_and_unscored_jobs_in(self):
        low = self.add(1, 1, score=3); none = self.add(1, 2, score=None)
        self.assertEqual(self.client.post("/api/settings", json={"quality_first": False}).status_code, 200)
        self.assertEqual(self.queue([low, none]).json()["queued"], [low, none])

    def test_queueing_wakes_the_worker_only_when_something_was_queued(self):
        from server.routes.queue import get_worker
        w = get_worker()
        self.queue([self.add(1, 1, score=1)])                  # rejected: below threshold
        self.assertFalse(w._wake.is_set())
        self.queue([self.add(1, 2)])
        self.assertTrue(w._wake.is_set())

    # ── pause / resume / status ───────────────────────────────────

    def test_status_shape_and_values(self):
        self.add(1, 1)
        a = self.add(1, 2, score=90); self.queue([a])
        st = self.client.get("/api/queue/status").json()
        self.assertEqual(set(st), {"running", "paused", "queue_length", "current_job",
                                   "applied_today", "daily_limit", "next_at"})
        self.assertEqual((st["running"], st["paused"], st["queue_length"], st["current_job"],
                          st["applied_today"], st["daily_limit"], st["next_at"]),
                         (False, False, 1, None, 0, 10, None))

    def test_status_counts_only_the_callers_queue(self):
        from db.jobs_db import JobsDB
        self.queue([self.add(1, 1)])
        JobsDB(user_id=2).transition(self.add(2, 1), "queued")
        self.assertEqual(self.client.get("/api/queue/status").json()["queue_length"], 1)
        self.assertEqual(self.bob.get("/api/queue/status").json()["queue_length"], 1)

    def test_pause_and_resume_flip_the_flag_and_it_persists(self):
        r = self.client.post("/api/queue/pause")
        self.assertEqual((r.status_code, r.json()["paused"]), (200, True))
        self.assertTrue(self.client.get("/api/queue/status").json()["paused"])
        self.assertTrue(self.bob.get("/api/queue/status").json()["paused"])
        from server.routes.queue import reset_worker
        reset_worker()                                          # a "restart": new worker, same settings
        self.assertTrue(self.client.get("/api/queue/status").json()["paused"])
        r = self.client.post("/api/queue/resume")
        self.assertEqual((r.status_code, r.json()["paused"]), (200, False))

    def test_the_pause_flag_cannot_be_written_through_the_settings_form(self):
        r = self.client.post("/api/settings", json={"queue_paused": "true"})
        self.assertEqual(r.status_code, 400)

    # ── the quality_first setting ─────────────────────────────────

    def test_quality_first_defaults_on_and_only_the_owner_can_change_it(self):
        self.assertEqual(self.client.get("/api/settings").json()["settings"]["quality_first"], "true")
        self.assertEqual(self.bob.post("/api/settings", json={"quality_first": "false"}).status_code, 403)
        self.assertEqual(self.settings().read("quality_first"), "true")
        self.assertEqual(self.client.post("/api/settings", json={"quality_first": False}).status_code, 200)
        self.assertEqual(self.settings().read("quality_first"), "false")
        self.assertEqual(self.client.post("/api/settings", json={"quality_first": "TRUE"}).status_code, 200)
        self.assertEqual(self.settings().read("quality_first"), "true", "stored normalised")

    def test_quality_first_rejects_anything_but_a_boolean(self):
        for bad in ("maybe", "", "1", "yes", 7):
            r = self.client.post("/api/settings", json={"quality_first": bad})
            self.assertEqual(r.status_code, 400, bad)
        self.assertEqual(self.settings().read("quality_first"), "true")

    # ── pre-flight settings: company_blacklist, min_salary_lpa ────

    def test_preflight_settings_exist_with_off_defaults_and_are_owner_only(self):
        got = self.client.get("/api/settings").json()["settings"]
        self.assertEqual((got["min_salary_lpa"], got["company_blacklist"]), ("0", ""))
        for body in ({"min_salary_lpa": "12"}, {"company_blacklist": "Acme"}):
            self.assertEqual(self.bob.post("/api/settings", json=body).status_code, 403, body)
        self.assertEqual((self.settings().read("min_salary_lpa"), self.settings().read("company_blacklist")),
                         ("0", ""))

    def test_min_salary_is_validated_and_normalised(self):
        for sent, stored in (("12.5", "12.5"), (12, "12"), ("", "0"), ("  8  ", "8"), ("0", "0"), ("1e2", "100")):
            r = self.client.post("/api/settings", json={"min_salary_lpa": sent})
            self.assertEqual(r.status_code, 200, sent)
            self.assertEqual(self.settings().read("min_salary_lpa"), stored, sent)
        self.client.post("/api/settings", json={"min_salary_lpa": "9"})
        for bad in ("abc", "-1", "nan", "inf", "20000", "12 LPA"):
            r = self.client.post("/api/settings", json={"min_salary_lpa": bad})
            self.assertEqual(r.status_code, 400, bad)
        self.assertEqual(self.settings().read("min_salary_lpa"), "9", "a rejected value must not be stored")

    def test_the_blacklist_is_cleaned_up_and_length_limited(self):
        r = self.client.post("/api/settings", json={"company_blacklist": " Acme \r\n\r\n  Globex Pvt Ltd ,Hooli\n"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.settings().read("company_blacklist"), "Acme\nGlobex Pvt Ltd ,Hooli")
        self.assertEqual(self.client.post("/api/settings", json={"company_blacklist": "x" * 5001}).status_code, 400)
        self.assertEqual(self.client.post("/api/settings", json={"company_blacklist": "x" * 5000}).status_code, 200)
        self.assertEqual(self.client.post("/api/settings", json={"company_blacklist": ""}).status_code, 200)
        self.assertEqual(self.settings().read("company_blacklist"), "")

    def test_one_bad_value_saves_nothing_from_the_same_request(self):
        r = self.client.post("/api/settings", json={"company_blacklist": "Acme", "min_salary_lpa": "oops"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.settings().read("company_blacklist"), "")

    # ── the daily cap the apply routes enforce ────────────────────

    def test_apply_limits_follow_quality_first(self):
        self.assertEqual(self.client.get("/api/apply/limits").json()["daily_limit"], 10)
        self.client.post("/api/settings", json={"quality_first": False})
        self.assertEqual(self.client.get("/api/apply/limits").json()["daily_limit"], 30)
        self.client.post("/api/settings", json={"quality_first": True, "daily_apply_limit": 4})
        self.assertEqual(self.client.get("/api/apply/limits").json()["daily_limit"], 4)

    def test_a_real_submit_is_refused_at_the_quality_first_cap_with_a_helpful_message(self):
        from applier.session import ApplicationSession
        from db.jobs_db import JobsDB
        db = JobsDB(user_id=1)
        for i in range(10):
            jid = self.add(1, 100 + i, score=None)
            db.transition(jid, "submitted", actor="user")
        target = self.add(1, 1)
        db.transition(target, "preparing"); db.transition(target, "ready_for_review")
        ApplicationSession(job_id=target, job_url="u", job_title="t", company="c",
                           source="linkedin", state="screenshot_review").save()
        r = self.client.post(f"/api/apply/submit/{target}", json={})
        self.assertEqual(r.status_code, 429)
        self.assertIn("10/10", r.json()["detail"])
        self.assertIn("Quality First", r.json()["detail"])
        self.assertEqual(self.stage(target), "ready_for_review")


if __name__ == "__main__":
    unittest.main(verbosity=2)
