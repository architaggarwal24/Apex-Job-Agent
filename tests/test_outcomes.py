# tests/test_outcomes.py
"""
tests/test_outcomes.py — outcome analytics and threshold calibration (db/outcomes_db.py,
server/routes/analytics.py).

Constructed data in a temp SQLite file, moved through the real lifecycle with a fake clock.
No LLM, no network. Covered:

  * what an outcome is (submitted or later, judged from the HISTORY, not the current stage);
  * the math: reply / interview / offer rates and the median time to a first response;
  * grouping by source, title (lower-cased, whitespace collapsed), score band, resume variant
    and apply mode - with "unknown" for what was never recorded;
  * the sample-size guards: < 10 in a group, < 30 overall;
  * the recommended auto_skip_score_threshold on constructed data (raise, lower, keep, and a
    band without enough data stopping the walk);
  * per-user scoping and the HTTP route.
"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
os.environ.setdefault("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())

from applier import assist
from db import outcomes_db
from db.jobs_db import JobsDB
from db.kits_db import KitStore
from db.outcomes_db import NOT_ENOUGH, OutcomeStats

START = datetime(2030, 1, 1, 9, 0, 0)          # after "now", so kits made in the test predate the submissions


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "jobs.db"
        self.clock = Clock()
        self.dbs = {}
        self.n = 0

    def db(self, uid=1):
        if uid not in self.dbs:
            self.dbs[uid] = JobsDB(path=self.path, user_id=uid, clock=self.clock)
        return self.dbs[uid]

    def app(self, path=(), *, uid=1, score=50, source="linkedin", title="Engineer",
            reply_after_days=None, actor="agent"):
        """One application: submitted at START (by `actor`), then each stage of `path`,
        the first one `reply_after_days` after the submission."""
        self.n += 1
        db = self.db(uid)
        j = db.add({"url": f"https://jobs.example/{uid}/{self.n}", "title": title,
                    "company": f"Co {self.n}", "source": source})
        if score is not None:
            db.update_score(j, score, "")
        self.clock.now = START
        db.transition(j, "submitted", actor=actor)
        for i, st in enumerate(path):
            self.clock.now = START + timedelta(days=(reply_after_days or 1) + i)
            db.transition(j, st, actor="user")
        self.clock.now = START
        return j

    def report(self, uid=1, threshold=30):
        return OutcomeStats(uid, self.path).report(threshold)

    def group(self, rep, dim, key):
        return next(g for g in rep["by"][dim] if g["key"] == key)

    def sql(self, q, params=()):
        with closing(sqlite3.connect(str(self.path))) as c:
            c.execute(q, params)
            c.commit()


# ── what counts ───────────────────────────────────────────────────

class OutcomeDefinitionTests(Base):
    def test_only_submitted_applications_are_outcomes(self):
        db = self.db()
        j = db.add({"url": "https://jobs.example/x", "title": "T", "company": "C", "source": "s"})
        db.transition(j, "queued", actor="user")
        self.app()
        self.assertEqual(self.report()["outcomes"], 1)

    def test_reached_is_judged_from_history_not_the_current_stage(self):
        self.app(["recruiter_screen", "rejected"])
        self.app(["technical_round", "withdrawn"])
        self.app(["offer", "rejected"])
        recs = OutcomeStats(1, self.path).records()
        self.assertEqual([(r["reply"], r["interview"], r["offer"]) for r in recs],
                         [(True, False, False), (True, True, False), (True, True, True)])

    def test_a_straight_rejection_is_not_a_reply_but_is_a_first_response(self):
        self.app(["rejected"], reply_after_days=4)
        r = OutcomeStats(1, self.path).records()[0]
        self.assertFalse(r["reply"])
        self.assertEqual(r["days_to_first_response"], 4.0)

    def test_withdrawing_is_not_a_response(self):
        self.app(["withdrawn"])
        self.assertIsNone(OutcomeStats(1, self.path).records()[0]["days_to_first_response"])


# ── the math and the guards ───────────────────────────────────────

class MathTests(Base):
    def test_rates_and_median_for_a_group_of_ten(self):
        for d in (1, 2, 3):
            self.app(["recruiter_screen"], reply_after_days=d)
        self.app(["technical_round", "hiring_manager"], reply_after_days=5)
        self.app(["offer"], reply_after_days=9)
        for _ in range(5):
            self.app()
        rep = self.report()
        o = rep["overall"]
        self.assertEqual((o["outcomes"], o["replies"], o["interviews"], o["offers"]), (10, 5, 2, 1))
        self.assertEqual((o["reply_rate"], o["interview_rate"], o["offer_rate"]), (50.0, 20.0, 10.0))
        self.assertEqual(o["median_days_to_first_response"], 3.0)          # median of 1,2,3,5,9
        self.assertEqual(self.group(rep, "source", "linkedin")["reply_rate"], 50.0)

    def test_a_group_under_ten_says_not_enough_data(self):
        for _ in range(10):
            self.app(["recruiter_screen"], source="linkedin")
        for _ in range(9):
            self.app(["recruiter_screen"], source="indeed")
        rep = self.report()
        small = self.group(rep, "source", "indeed")
        self.assertEqual(small["outcomes"], 9)
        self.assertEqual(small["replies"], 9)                              # counts are still shown
        for k in ("reply_rate", "interview_rate", "offer_rate", "median_days_to_first_response"):
            self.assertEqual(small[k], NOT_ENOUGH, k)
        self.assertEqual(self.group(rep, "source", "linkedin")["reply_rate"], 100.0)
        self.assertEqual(rep["overall"]["reply_rate"], 100.0)

    def test_overall_under_ten_says_not_enough_data(self):
        self.app(["recruiter_screen"])
        self.assertEqual(self.report()["overall"]["reply_rate"], NOT_ENOUGH)

    def test_titles_are_grouped_lower_cased_with_whitespace_collapsed(self):
        self.app(title="  Senior   Engineer ")
        self.app(title="senior engineer")
        self.app(title="SENIOR\tENGINEER")
        keys = [g["key"] for g in self.report()["by"]["title"]]
        self.assertEqual(keys, ["senior engineer"])

    def test_score_bands(self):
        for s in (0, 39, 40, 59, 60, 79, 80, 100, None):
            self.app(score=s)
        bands = {g["key"]: g["outcomes"] for g in self.report()["by"]["score_band"]}
        self.assertEqual(bands, {"0-39": 2, "40-59": 2, "60-79": 2, "80-100": 2, "unscored": 1})
        self.assertEqual([g["key"] for g in self.report()["by"]["score_band"]],
                         ["0-39", "40-59", "60-79", "80-100", "unscored"])


# ── resume variant and apply mode ─────────────────────────────────

class RecordedDimensionTests(Base):
    def test_apply_mode_comes_from_the_submitted_event(self):
        self.app(actor="agent")
        self.app(actor="autonomy")
        self.app(actor="user")
        db = self.db()
        j = db.add({"url": "https://jobs.example/assist", "title": "T", "company": "C", "source": "s"})
        assist.mark_applied(db, j, "applied on the site")
        modes = sorted(r["apply_mode"] for r in OutcomeStats(1, self.path).records())
        self.assertEqual(modes, ["assist", "auto", "auto", "manual"])

    def test_a_migrated_legacy_submission_is_unknown(self):
        db = self.db()
        j = db.add({"url": "https://jobs.example/legacy", "title": "T", "company": "C", "source": "s"})
        self.sql("UPDATE jobs SET stage='recruiter_screen' WHERE job_id=?", (j,))
        for st in ("submitted", "recruiter_screen"):
            self.sql("INSERT INTO application_events (user_id, job_id, at, kind, from_stage, to_stage, actor, detail) "
                     "VALUES (1, ?, '2025-01-01T00:00:00', 'migrated', NULL, ?, 'system', ?)",
                     (j, st, json.dumps({"legacy_status": "applied"})))
        r = OutcomeStats(1, self.path).records()[0]
        self.assertEqual((r["apply_mode"], r["resume_variant"], r["reply"]), ("unknown", "unknown", True))

    def test_resume_variant_is_the_kit_current_at_submission(self):
        db = self.db()
        kits = KitStore(1, self.path)
        with_kit = db.add({"url": "https://jobs.example/k1", "title": "T", "company": "C1", "source": "s"})
        kits.add_version(with_kit, variant_id=7, variant_label="Backend CV", selection="llm", status="ready")
        later = db.add({"url": "https://jobs.example/k2", "title": "T", "company": "C2", "source": "s"})
        no_variants = db.add({"url": "https://jobs.example/k3", "title": "T", "company": "C3", "source": "s"})
        kits.add_version(no_variants, selection="none", status="partial")
        no_kit = db.add({"url": "https://jobs.example/k4", "title": "T", "company": "C4", "source": "s"})
        for j in (with_kit, later, no_variants, no_kit):
            db.transition(j, "submitted", actor="agent")
        kits.add_version(with_kit, variant_id=8, variant_label="Changed later", selection="override", status="ready")
        kits.add_version(later, variant_id=9, variant_label="Made after", selection="llm", status="ready")
        self.sql("UPDATE application_kits SET created_at='2031-01-01T00:00:00' WHERE version=2 OR job_id=?", (later,))
        by_job = {}
        for j in (with_kit, later, no_variants, no_kit):
            ev = [e for e in db.get_application(j)["events"]]
            kit_rows = [k for k in kits.versions(j)]
            by_job[j] = outcomes_db.outcome_for({"stage": "submitted"}, ev, kit_rows)["resume_variant"]
        self.assertEqual(by_job, {with_kit: "Backend CV", later: "unknown",
                                  no_variants: "default resume file", no_kit: "unknown"})
        self.assertEqual(sorted(g["key"] for g in self.report()["by"]["resume_variant"]),
                         ["Backend CV", "default resume file", "unknown"])


# ── calibration ───────────────────────────────────────────────────

class CalibrationTests(Base):
    def band(self, score, n, replies):
        for i in range(n):
            self.app(["recruiter_screen"] if i < replies else [], score=score)

    def test_under_thirty_outcomes_is_not_enough_data(self):
        self.band(10, 10, 0)
        self.band(50, 10, 5)
        self.band(90, 9, 9)
        cal = self.report()["calibration"]
        self.assertEqual(cal["status"], NOT_ENOUGH)
        self.assertEqual((cal["outcomes"], cal["needed"]), (29, 30))
        self.assertIsNone(cal["recommended_auto_skip_score_threshold"])

    def test_recommends_raising_past_an_underperforming_low_band(self):
        self.band(20, 10, 0)            # 0%
        self.band(50, 10, 3)            # 30%
        self.band(70, 10, 5)            # 50%
        self.band(90, 10, 6)            # 60%   -> overall 14/40 = 35%
        cal = self.report(threshold=30)["calibration"]
        self.assertEqual(cal["status"], "ok")
        self.assertEqual(cal["overall_reply_rate"], 35.0)
        self.assertEqual(cal["recommended_auto_skip_score_threshold"], 40)
        self.assertTrue(cal["reason"].startswith("Raise to 40"), cal["reason"])
        self.assertIn("0-39", cal["reason"])
        self.assertEqual(len(cal["reason"].splitlines()), 1)
        bands = {b["band"]: b for b in cal["bands"]}
        self.assertEqual(bands["0-39"]["vs_overall"], -35.0)
        self.assertTrue(bands["0-39"]["underperforms"])
        self.assertFalse(bands["40-59"]["underperforms"])

    def test_two_bad_bands_raise_it_further(self):
        self.band(20, 10, 0)
        self.band(50, 10, 1)            # 10% < half of 32.5%
        self.band(70, 10, 6)
        self.band(90, 10, 6)
        cal = self.report(threshold=30)["calibration"]
        self.assertEqual(cal["recommended_auto_skip_score_threshold"], 60)

    def test_recommends_lowering_when_the_band_below_the_threshold_does_well(self):
        self.band(20, 10, 0)
        self.band(50, 10, 4)
        self.band(70, 10, 4)
        self.band(90, 10, 4)
        cal = self.report(threshold=60)["calibration"]
        self.assertEqual(cal["recommended_auto_skip_score_threshold"], 40)
        self.assertTrue(cal["reason"].startswith("Lower to 40"), cal["reason"])

    def test_keeps_the_threshold_when_no_band_underperforms(self):
        for score in (20, 50, 70, 90):
            self.band(score, 10, 4)
        cal = self.report(threshold=30)["calibration"]
        self.assertEqual(cal["recommended_auto_skip_score_threshold"], 30)
        self.assertTrue(cal["reason"].startswith("Keep 30"), cal["reason"])

    def test_a_band_without_enough_data_stops_the_walk(self):
        self.band(20, 9, 0)             # too few to judge, however bad
        self.band(50, 11, 0)            # bad, but above an unjudged band
        self.band(70, 10, 8)
        self.band(90, 10, 8)
        cal = self.report(threshold=30)["calibration"]
        self.assertEqual(cal["bands"][0]["reply_rate"], NOT_ENOUGH)
        self.assertEqual(cal["bands"][0]["vs_overall"], NOT_ENOUGH)
        self.assertEqual(cal["recommended_auto_skip_score_threshold"], 30)


# ── per user + HTTP ───────────────────────────────────────────────

class ScopingTests(Base):
    def test_other_users_applications_are_not_counted(self):
        self.app(["recruiter_screen"], uid=2)
        self.app(uid=1)
        self.assertEqual(self.report(uid=1)["overall"]["replies"], 0)
        self.assertEqual(self.report(uid=2)["outcomes"], 1)
        with self.assertRaises(ValueError):
            OutcomeStats(None, self.path)


try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except Exception:
    HAVE_FASTAPI = False


@unittest.skipUnless(HAVE_FASTAPI, "fastapi + httpx not installed")
class OutcomesHttpTests(unittest.TestCase):
    def setUp(self):
        from core.config import cfg
        import core.auth as auth
        from db.users_db import UsersDB
        from db.settings_db import SettingsDB
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self._saved = (cfg.JOBS_DB_PATH, cfg.SESSIONS_DIR, cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE,
                       cfg.ALLOW_SIGNUP, UsersDB._instance, auth._serializer, SettingsDB._instance)
        SettingsDB._instance = SettingsDB(path=tmp / "settings.db")
        SettingsDB._instance.write("auto_skip_score_threshold", "45")
        cfg.JOBS_DB_PATH, cfg.SESSIONS_DIR = tmp / "jobs.db", tmp / "sessions"
        cfg.SESSIONS_DIR.mkdir()
        cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE, cfg.ALLOW_SIGNUP = "test-signing-secret-" + "x" * 40, False, False
        UsersDB._instance, auth._serializer = UsersDB(path=tmp / "users.db"), None
        from server.app import create_app
        self.app = create_app(ui_dir=tmp / "no-build")
        self.alice = TestClient(self.app, follow_redirects=False)
        self.alice.post("/api/auth/signup", json={"email": "alice@example.com", "password": "correct-horse-battery"})

    def tearDown(self):
        from core.config import cfg
        import core.auth as auth
        from db.users_db import UsersDB
        from db.settings_db import SettingsDB
        (cfg.JOBS_DB_PATH, cfg.SESSIONS_DIR, cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE, cfg.ALLOW_SIGNUP,
         UsersDB._instance, auth._serializer, SettingsDB._instance) = self._saved
        self.tmp.cleanup()

    def test_route(self):
        db = JobsDB(user_id=1)
        j = db.add({"url": "https://x.example/1", "title": "Dev", "company": "Co", "source": "LinkedIn"})
        db.transition(j, "submitted", actor="user")
        r = self.alice.get("/api/analytics/outcomes")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["outcomes"], 1)
        self.assertEqual(set(body["by"]), {"source", "title", "score_band", "resume_variant", "apply_mode"})
        self.assertEqual(body["by"]["source"][0]["key"], "linkedin")
        self.assertEqual(body["calibration"]["current_threshold"], 45)
        self.assertEqual(TestClient(self.app).get("/api/analytics/outcomes").status_code, 401)


if __name__ == "__main__":
    unittest.main()
