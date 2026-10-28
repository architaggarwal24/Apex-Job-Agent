# tests/test_quick_fit.py
"""
tests/test_quick_fit.py — the instant keyword match every Inbox job gets
(intelligence/fit.py, jobs.fit_score) and the "Score now" route:

  * quick_fit: title, skills, seniority and location parts; the reason text;
    no description -> the other parts are rescaled; no titles or skills -> None
  * score_jobs_batch saves a fit_score for every job that passes the pre-filter,
    with AI scoring off (ai_score stays None: the gates still see "not scored")
    and on (the model's score is the ai_score, the fit stays too)
  * JobsDB: fit / fit_kind on every application (the AI score wins), fit_reason
    in the detail, update_fit / inbox_jobs scoped per user
  * POST /api/pipeline/score: quick scores at once; AI scoring in the background
    on the progress bar when it is on; "busy" during a search; owner only

No network, no model: core.llm.chat_json is a fake.

Run from the project root:   python3 -m unittest tests.test_quick_fit -v
"""

import contextlib
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
os.environ.setdefault("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())

from intelligence.fit import quick_fit
from tracking.progress import Progress

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except Exception:
    HAVE_FASTAPI = False

PROFILE = {"search_positions": ["AI Engineer", "Backend Engineer"], "search_locations": ["Delhi", "Remote"],
           "skills": ["Python", "PyTorch", "SQL", "Docker", "C++", "React"],
           "_flat": {"experience_years": "1", "current_role": ""}}


def job(title, description="", location="", url=None):
    return {"title": title, "company": "Acme", "location": location, "description": description,
            "url": url or f"https://example.com/{abs(hash((title, location)))}", "source": "linkedin"}


class QuickFitTests(unittest.TestCase):

    def test_a_matching_job_scores_high_and_says_why(self):
        r = quick_fit(job("AI Engineer [AQ-1]", "We use python, pytorch and docker.", "New Delhi, India"), PROFILE)
        self.assertGreaterEqual(r["score"], 70)
        self.assertTrue(r["reason"].startswith("Quick match (keywords): "))
        self.assertIn("title matches “AI Engineer”", r["reason"])
        self.assertIn("3 of your skills mentioned (Python, PyTorch, Docker)", r["reason"])
        self.assertIn("in Delhi", r["reason"])

    def test_an_unrelated_job_scores_low(self):
        good = quick_fit(job("AI/ML Engineer (100 % remote) (m/f/d)", "C++ and SQL needed", "Remote"), PROFILE)["score"]
        bad = quick_fit(job("Specialist, Measurement", "Excel and reports", "Lansing, MI, USA"), PROFILE)["score"]
        self.assertLess(bad, 25)
        self.assertGreater(good, bad + 40)

    def test_synonyms_and_word_order(self):
        self.assertIn("title matches “Backend Engineer”", quick_fit(job("Back-end Developer"), PROFILE)["reason"])
        self.assertIn("title matches “AI Engineer”", quick_fit(job("Engineer, Artificial Intelligence"), PROFILE)["reason"])

    def test_skills_match_whole_words_only(self):
        r = quick_fit(job("Backend Engineer", "Reactive streams in Javascript, sql server"), PROFILE)
        self.assertIn("1 of your skills mentioned (SQL)", r["reason"])          # not "React" inside "Reactive"

    def test_no_description_rescales_instead_of_counting_zero_skills(self):
        r = quick_fit(job("AI Engineer", "", "Delhi, India"), PROFILE)
        self.assertIn("no description to check skills against", r["reason"])
        self.assertGreaterEqual(r["score"], 85)

    def test_seniority_against_experience(self):
        fresher = quick_fit(job("Senior AI Engineer", "python"), PROFILE)["score"]
        junior = quick_fit(job("Junior AI Engineer", "python"), PROFILE)["score"]
        self.assertGreater(junior, fresher)
        veteran = {**PROFILE, "_flat": {"experience_years": "8"}}
        self.assertGreater(quick_fit(job("Senior AI Engineer", "python"), veteran)["score"],
                           quick_fit(job("AI Engineering Intern", "python"), veteran)["score"])

    def test_nothing_to_compare_with(self):
        self.assertIsNone(quick_fit(job("AI Engineer"), {"_flat": {}}))
        only_role = quick_fit(job("Data Analyst"), {"_flat": {"current_role": "Data Analyst"}})
        self.assertIn("title matches “Data Analyst”", only_role["reason"])

    def test_scores_stay_in_range(self):
        for j in (job(""), job("x" * 500, "y" * 5000), job("AI Engineer", "python " * 200, "Remote")):
            self.assertTrue(0 <= quick_fit(j, PROFILE)["score"] <= 100)


class TempJobs(unittest.TestCase):
    def setUp(self):
        from db.jobs_db import JobsDB
        from db.settings_db import SettingsDB
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        saved = SettingsDB._instance
        SettingsDB._instance = SettingsDB(path=d / "settings.db")
        self.addCleanup(lambda: setattr(SettingsDB, "_instance", saved))
        self.path = d / "jobs.db"
        self.alice = JobsDB(path=self.path, user_id=1)
        self.bob = JobsDB(path=self.path, user_id=2)

    def add(self, db, j):
        jid = db.add(j)
        j["job_id"] = jid
        return jid

    def raw(self, jid):
        with contextlib.closing(sqlite3.connect(self.path)) as c:
            return c.execute("SELECT ai_score, fit_score, fit_reason, stage FROM jobs WHERE job_id=?", (jid,)).fetchone()


class ScorerTests(TempJobs):

    def test_ai_off_every_job_gets_a_quick_fit_and_no_ai_score(self):
        from intelligence import scorer
        jobs = [job("AI Engineer", "python pytorch", "Delhi, India"), job("Specialist, Measurement", "excel", "Lansing, MI")]
        for j in jobs:
            self.add(self.alice, j)
        with mock.patch("core.llm.chat_json", side_effect=AssertionError("no model call with AI scoring off")):
            scorer.score_jobs_batch(jobs, PROFILE, jobs_db=self.alice)
        a, b = self.raw(jobs[0]["job_id"]), self.raw(jobs[1]["job_id"])
        self.assertIsNone(a[0])                                  # the gates still see "not scored"
        self.assertGreater(a[1], b[1])
        self.assertTrue(a[2].startswith("Quick match"))
        self.assertEqual((a[3], b[3]), ("found", "found"))       # a low quick match never skips a job

    def test_ai_on_the_model_score_is_the_ai_score_and_the_fit_stays(self):
        from db.settings_db import SettingsDB
        from intelligence import scorer
        SettingsDB.get().write("enable_llm_scoring", "true")
        j = job("AI Engineer", "python", "Remote")
        self.add(self.alice, j)
        with mock.patch("core.llm.chat_json", return_value={"score": 88, "reason": "Strong fit", "pros": [], "cons": []}):
            scorer.score_jobs_batch([j], PROFILE, jobs_db=self.alice)
        ai, fit, _reason, stage = self.raw(j["job_id"])
        self.assertEqual((ai, stage), (88, "found"))
        self.assertIsNotNone(fit)

    def test_pre_filtered_jobs_get_no_quick_fit(self):
        from intelligence import scorer
        j = job("Senior Staff Engineer", "", "Delhi")
        self.add(self.alice, j)
        scorer.score_jobs_batch([j], PROFILE, jobs_db=self.alice)
        ai, fit, _r, stage = self.raw(j["job_id"])
        self.assertEqual((ai, fit, stage), (15, None, "skipped"))


class JobsDbFitTests(TempJobs):

    def test_fit_is_the_ai_score_when_there_is_one_else_the_quick_match(self):
        a, b, c = (self.add(self.alice, job(t)) for t in ("A", "B", "C"))
        self.alice.update_fit(a, 40, "Quick match (keywords): x.")
        self.alice.update_fit(b, 40, "Quick match (keywords): y.")
        self.alice.update_score(b, 90, "AI says yes")
        rows = {r["job_id"]: r for r in self.alice.list_applications()}
        self.assertEqual((rows[a]["fit"], rows[a]["fit_kind"]), (40, "quick"))
        self.assertEqual((rows[b]["fit"], rows[b]["fit_kind"]), (90, "ai"))
        self.assertEqual((rows[c]["fit"], rows[c]["fit_kind"]), (None, None))
        self.assertEqual(self.alice.get_application(a)["fit_reason"], "Quick match (keywords): x.")

    def test_scoped_per_user(self):
        mine = self.add(self.alice, job("Mine"))
        theirs = self.add(self.bob, job("Theirs"))
        self.alice.update_fit(theirs, 99, "nope")
        self.assertIsNone(self.raw(theirs)[1])
        self.assertEqual([j["job_id"] for j in self.alice.inbox_jobs()], [mine])
        self.alice.transition(mine, "queued", actor="user")
        self.assertEqual(self.alice.inbox_jobs(), [], "only the Inbox (stage found)")


@unittest.skipUnless(HAVE_FASTAPI, "fastapi + httpx not installed")
class ScoreNowHttpTests(unittest.TestCase):

    def setUp(self):
        from core.config import cfg
        import core.auth as auth
        from db.users_db import UsersDB
        from db.settings_db import SettingsDB
        import tracking.progress
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
        self.tracker = Progress()
        patch = mock.patch.object(tracking.progress, "TRACKER", self.tracker)
        patch.start()
        self.addCleanup(patch.stop)
        from server.app import create_app
        self.app = create_app(ui_dir=tmp / "no-build")
        self.alice = TestClient(self.app, follow_redirects=False)
        self.alice.post("/api/auth/signup", json={"email": "alice@example.com", "password": "correct-horse-battery"})
        cfg.ALLOW_SIGNUP = True
        self.bob = TestClient(self.app, follow_redirects=False)
        self.bob.post("/api/auth/signup", json={"email": "bob@example.com", "password": "correct-horse-battery"})
        from db.jobs_db import JobsDB
        from db.profile_db import ProfileDB
        ProfileDB().set_many({"search_positions": json.dumps(PROFILE["search_positions"]),
                              "search_locations": json.dumps(PROFILE["search_locations"]),
                              "skills_raw": json.dumps(PROFILE["skills"]), "experience_years": "1"})
        self.uid = self.alice.get("/api/auth/me").json()["id"]
        self.db = JobsDB(user_id=self.uid)
        self.good = self.db.add(job("AI Engineer", "python pytorch", "Delhi, India"))
        self.bad = self.db.add(job("Specialist, Measurement", "excel", "Lansing, MI"))

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

    def wait_for_thread(self):
        deadline = time.time() + 10
        while time.time() < deadline and any(t.name == "inbox-score" and t.is_alive() for t in threading.enumerate()):
            time.sleep(0.02)

    def fits(self):
        return {r["job_id"]: (r["fit"], r["fit_kind"]) for r in self.alice.get("/api/applications").json()}

    def test_quick_scores_at_once_with_ai_off(self):
        with mock.patch("core.llm.chat_json", side_effect=AssertionError("no model call")):
            r = self.alice.post("/api/pipeline/score")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"inbox": 2, "quick": 2, "ai": "off", "ai_total": 0})
        f = self.fits()
        self.assertEqual((f[self.good][1], f[self.bad][1]), ("quick", "quick"))
        self.assertGreater(f[self.good][0], f[self.bad][0])

    def test_ai_on_scores_in_the_background_on_the_progress_bar(self):
        from db.settings_db import SettingsDB
        SettingsDB.get().write("enable_llm_scoring", "true")
        with mock.patch("core.llm.chat_json", return_value={"score": 77, "reason": "ok", "pros": [], "cons": []}):
            r = self.alice.post("/api/pipeline/score").json()
            self.assertEqual((r["ai"], r["ai_total"], r["quick"]), ("started", 2, 2))
            self.wait_for_thread()
        s = self.alice.get("/api/pipeline/status").json()
        self.assertEqual((s["running"], s["stats"]["kind"], s["stats"]["scored"]), (False, "score", 2))
        self.assertEqual(self.fits()[self.good], (77, "ai"))
        with mock.patch("core.llm.chat_json", side_effect=AssertionError("nothing left to score")):
            self.assertEqual(self.alice.post("/api/pipeline/score").json()["ai"], "none")

    def test_waits_for_a_running_search(self):
        from db.settings_db import SettingsDB
        SettingsDB.get().write("enable_llm_scoring", "true")
        self.tracker.start(self.uid)
        r = self.alice.post("/api/pipeline/score").json()
        self.assertEqual((r["ai"], r["quick"]), ("busy", 2), "the quick match still happens")

    def test_owner_only_and_needs_a_session(self):
        self.assertEqual(self.bob.post("/api/pipeline/score").status_code, 403)
        self.assertEqual(TestClient(self.app).post("/api/pipeline/score").status_code, 401)


if __name__ == "__main__":
    unittest.main()
