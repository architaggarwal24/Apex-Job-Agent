# tests/test_progress.py
"""
tests/test_progress.py — the live progress of a job search:

  * tracking.progress.Progress: phases, ticks, growing totals, the percent
    never going backwards, finish/close, per-user snapshots, one run at a time
  * the pipeline reports every phase (boards per query, feeds, company boards,
    save, score, briefs) and closes the run, also when it fails
  * GET /api/pipeline/status and the 409 for a second run
  * the checklist's login item is optional while no board needs a login

No network, no model: the engines, scorer and briefer are fakes.

Run from the project root:   python3 -m unittest tests.test_progress -v
"""

import json
import os
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

from core import checklist
from tracking.progress import PHASES, Progress

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except Exception:
    HAVE_FASTAPI = False


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TrackerTests(unittest.TestCase):

    def setUp(self):
        self.clock = Clock()
        self.p = Progress(clock=self.clock)

    def phase(self, snap, key):
        return next(p for p in snap["phases"] if p["key"] == key)

    def test_a_run_moves_through_its_phases(self):
        self.p.start(1)
        self.p.begin("boards", 4, "Searching job boards")
        self.p.tick("boards", "Linkedin · Dev in Pune", found=10)
        s = self.p.snapshot(1)
        self.assertTrue(s["running"])
        self.assertEqual((s["phase"], s["current"], s["found"], s["percent"]), ("boards", "Linkedin · Dev in Pune", 10, 25))
        self.assertEqual(self.phase(s, "boards"), {"key": "boards", "label": "Job boards", "status": "running", "done": 1, "total": 4, "found": 10})
        self.p.tick("boards", units=3, found=5)
        self.p.begin("sources", 2)
        s = self.p.snapshot(1)
        self.assertEqual(self.phase(s, "boards")["status"], "done", "beginning a phase closes the earlier ones")
        self.assertEqual(self.phase(s, "sources")["status"], "running")
        self.assertEqual(s["found"], 15)

    def test_the_percent_never_goes_backwards_when_a_total_grows(self):
        self.p.start(1)
        self.p.begin("boards", 2)
        self.p.tick("boards")
        self.assertEqual(self.p.snapshot(1)["percent"], 50)
        self.p.begin("companies", 20)
        self.assertEqual(self.p.snapshot(1)["percent"], 50)
        self.p.tick("companies", units=20)
        self.assertEqual(self.p.snapshot(1)["percent"], 95)

    def test_a_running_bar_stops_at_99_and_a_finished_one_shows_100(self):
        self.p.start(1)
        self.p.begin("boards", 1)
        self.p.tick("boards")
        self.assertEqual(self.p.snapshot(1)["percent"], 99)
        self.clock.t += 42
        self.p.finish({"total_found": 3})
        s = self.p.snapshot(1)
        self.assertEqual((s["running"], s["percent"], s["current"], s["elapsed"], s["stats"]), (False, 100, "Done", 42.0, {"total_found": 3}))
        self.assertEqual(self.phase(s, "boards")["status"], "done")
        self.assertEqual(self.phase(s, "briefs")["status"], "skipped")

    def test_failures_and_cancellation_are_reported(self):
        self.p.start(1)
        self.p.begin("boards", 1)
        self.p.finish(error="RuntimeError: x")
        s = self.p.snapshot(1)
        self.assertEqual((s["error"], s["current"], self.phase(s, "boards")["status"]), ("RuntimeError: x", "Failed", "failed"))
        self.assertLess(s["percent"], 100)
        self.p.start(1)
        self.p.note(cancelled=True)
        self.p.finish({"cancelled": True})
        self.assertEqual((self.p.snapshot(1)["cancelled"], self.p.snapshot(1)["current"]), (True, "Cancelled"))

    def test_one_run_at_a_time_and_close_is_a_safety_net(self):
        rid = self.p.start(1)
        with self.assertRaises(ValueError):
            self.p.start(1)
        self.p.close(rid + 1)
        self.assertTrue(self.p.is_running(), "closing another run id does nothing")
        self.p.close(rid)
        self.assertFalse(self.p.is_running())
        self.p.close(rid)                                 # twice is fine
        self.assertEqual(self.p.start(1), rid + 1)

    def test_only_the_user_who_started_it_sees_the_run(self):
        self.p.start(1)
        self.p.begin("boards", 3)
        self.p.log("Searching linkedin")
        other = self.p.snapshot(2)
        self.assertFalse(other["running"])
        self.assertEqual((other["found"], other["log"]), (0, []))
        self.assertEqual(self.p.snapshot(1)["log"][0]["msg"], "Searching linkedin")

    def test_the_log_is_bounded(self):
        self.p.start(1)
        for i in range(200):
            self.p.log(f"line {i}")
        s = self.p.snapshot(1)
        self.assertEqual(len(s["log"]), 20)
        self.assertEqual(s["log"][-1]["msg"], "line 199")

    def test_phase_list_is_stable(self):
        self.assertEqual([k for k, _ in PHASES], ["boards", "sources", "companies", "save", "score", "briefs"])


class PipelineProgressTests(unittest.TestCase):

    def setUp(self):
        from core.config import cfg
        from db.settings_db import SettingsDB
        from db.profile_db import ProfileDB
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        saved = SettingsDB._instance
        SettingsDB._instance = SettingsDB(path=d / "settings.db")
        self.addCleanup(lambda: setattr(SettingsDB, "_instance", saved))
        for p in (mock.patch.object(cfg, "DATA_DIR", d), mock.patch.object(cfg, "JOBS_DB_PATH", d / "jobs.db"),
                  mock.patch.object(cfg, "PROFILE_DB_PATH", d / "profile.db"),
                  mock.patch("tracking.pipeline.time.sleep", lambda s: None),
                  mock.patch("urllib.request.urlopen", side_effect=AssertionError("a test touched the network")),
                  mock.patch("requests.request", side_effect=AssertionError("a test touched the network")),
                  mock.patch("playwright.sync_api.sync_playwright", side_effect=AssertionError("a browser started"))):
            p.start()
            self.addCleanup(p.stop)
        ProfileDB().set_many({"search_positions": '["Backend Engineer", "AI Engineer"]', "search_locations": '["Pune"]'})
        self.p = Progress()

    def fake_search(self, site, profile, titles, locations, on_query=None, **kw):
        jobs = []
        for t in titles:
            for l in locations:
                j = {"title": t, "company": f"{site} co", "url": f"https://{site}.example/{t}/{l}".replace(" ", "-"), "source": site}
                jobs.append(j)
                if on_query:
                    on_query(t, l, 1)
        return jobs

    def test_every_phase_is_reported_and_the_run_is_closed(self):
        import tracking.pipeline
        briefs = []

        def scorer(jobs, profile, emit=None, **kw):
            for i in range(1, len(jobs) + 1):
                emit("score_progress", {"done": i, "total": len(jobs)})
            for j in jobs:
                j["ai_score"] = 80
            return jobs

        with mock.patch("scraper.jobspy_source.search", side_effect=self.fake_search), \
             mock.patch("intelligence.scorer.score_jobs_batch", side_effect=scorer), \
             mock.patch("intelligence.briefer.generate_and_save", lambda job, *a: briefs.append(job["title"])):
            stats = tracking.pipeline.run(platforms=["linkedin", "indeed"], sources=[], progress=self.p, user_id=1)
        s = self.p.snapshot(1)
        phases = {p["key"]: p for p in s["phases"]}
        self.assertFalse(s["running"])
        self.assertEqual(s["percent"], 100)
        self.assertEqual((phases["boards"]["done"], phases["boards"]["total"], phases["boards"]["found"]), (8, 8, 8),
                         "2 boards x 4 queries (2 titles x Pune + Remote)")
        self.assertEqual(phases["save"]["status"], "done")
        self.assertEqual((phases["score"]["status"], phases["briefs"]["done"], phases["briefs"]["total"]), ("done", 4, 4),
                         "the same title at the same company in two places is one job: 4 saved, 4 briefed")
        self.assertEqual(phases["sources"]["status"], "skipped")
        self.assertEqual((s["found"], s["new"], s["stats"]["new_added"]), (8, 4, stats["new_added"]))
        self.assertTrue(any("Scraping linkedin" in l["msg"] for l in s["log"]))

    def test_jobs_are_saved_as_each_board_returns_them(self):
        """The Inbox fills while the search runs: by the time the second board is searched,
        the first board's jobs are saved, with their quick match, and counted as new."""
        import tracking.pipeline
        from db.jobs_db import JobsDB
        mid = {}

        def search(site, profile, titles, locations, on_query=None, **kw):
            if site == "indeed":
                mid["rows"] = [(r["title"], r["fit_kind"]) for r in JobsDB().list_applications()]
                mid["new"] = self.p.snapshot(1)["new"]
            return self.fake_search(site, profile, titles, locations, on_query=on_query)

        with mock.patch("scraper.jobspy_source.search", side_effect=search), \
             mock.patch("intelligence.scorer.score_jobs_batch", lambda jobs, profile, **kw: []), \
             mock.patch("intelligence.briefer.generate_and_save", lambda *a: None):
            stats = tracking.pipeline.run(platforms=["linkedin", "indeed"], sources=[], progress=self.p, user_id=1)
        self.assertEqual(sorted(mid["rows"]), [("AI Engineer", "quick"), ("Backend Engineer", "quick")])
        self.assertEqual(mid["new"], 2)
        self.assertEqual((stats["total_found"], stats["new_added"]), (8, 4))

    def test_obvious_mismatches_are_skipped_as_they_are_saved(self):
        import tracking.pipeline
        from db.jobs_db import JobsDB
        from db.profile_db import ProfileDB
        ProfileDB().set_many({"search_positions": '["Senior Backend Engineer"]', "experience_years": "0"})
        with mock.patch("scraper.jobspy_source.search", side_effect=self.fake_search), \
             mock.patch("intelligence.scorer.score_jobs_batch", lambda jobs, profile, **kw: []), \
             mock.patch("intelligence.briefer.generate_and_save", lambda *a: None):
            tracking.pipeline.run(platforms=["naukri"], sources=[], progress=self.p, user_id=1)
        rows = JobsDB().list_applications()
        self.assertEqual([(r["stage"], r["fit_kind"]) for r in rows], [("skipped", "ai")], "pre-filtered: 15/100 and skipped, no quick match")

    def test_a_board_engine_without_per_query_reports_still_completes_its_units(self):
        import tracking.pipeline
        with mock.patch("scraper.jobspy_source.search", lambda *a, **kw: [{"title": "Dev", "company": "X", "url": "https://x.example/1", "source": "naukri"}]), \
             mock.patch("intelligence.scorer.score_jobs_batch", lambda jobs, profile, **kw: []), \
             mock.patch("intelligence.briefer.generate_and_save", lambda *a: None):
            tracking.pipeline.run(platforms=["naukri"], sources=[], progress=self.p)
        b = next(p for p in self.p.snapshot(1)["phases"] if p["key"] == "boards")
        self.assertEqual((b["done"], b["total"], b["found"]), (4, 4, 1))

    def test_a_crash_closes_the_run_with_the_error(self):
        import tracking.pipeline
        with mock.patch("tracking.pipeline._run", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                tracking.pipeline.run(platforms=["naukri"], sources=[], progress=self.p)
        s = self.p.snapshot(1)
        self.assertEqual((s["running"], s["error"]), (False, "RuntimeError: disk full"))

    def test_no_titles_ends_the_run_with_a_message(self):
        import tracking.pipeline
        from db.profile_db import ProfileDB
        ProfileDB().set("search_positions", "[]")
        stats = tracking.pipeline.run(platforms=["naukri"], sources=[], progress=self.p)
        self.assertIn("No job titles saved yet", stats["error"])
        self.assertEqual((self.p.snapshot(1)["running"], self.p.snapshot(1)["current"]), (False, "Failed"))

    def test_feeds_and_company_boards_report_too(self):
        import tracking.pipeline
        from db.sources_db import SourceStore
        from tests.test_sources import FakeFetch
        SourceStore(1).add("greenhouse", "acme")
        f = FakeFetch({"https://remoteok.com/api": [{"legal": 1}],
                       "https://boards-api.greenhouse.io/v1/boards/acme/jobs": {"jobs": [
                           {"id": 1, "title": "Backend Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1", "location": {"name": "Pune"}}]}})
        with mock.patch("intelligence.scorer.score_jobs_batch", lambda jobs, profile, **kw: []), \
             mock.patch("intelligence.briefer.generate_and_save", lambda *a: None):
            tracking.pipeline.run(platforms=[], sources=["remoteok", "greenhouse"], fetch=f, progress=self.p)
        ph = {p["key"]: p for p in self.p.snapshot(1)["phases"]}
        self.assertEqual((ph["sources"]["done"], ph["sources"]["total"]), (1, 1))
        self.assertEqual((ph["companies"]["done"], ph["companies"]["total"], ph["companies"]["found"]), (1, 1, 1))


class ChecklistLoginTests(unittest.TestCase):

    class Settings:
        def read(self, k, d=""):
            return d

    def build(self, **kw):
        base = dict(settings=self.Settings(), profile_flat={}, profile_missing=[], resume_file=True, resume_variants=0,
                    logged_in={}, job_count=0)
        base.update(kw)
        return checklist.build(**base)

    def test_login_is_required_only_when_a_board_searches_with_the_browser(self):
        needed = self.build(search_needs_login=True)
        item = next(i for i in needed["items"] if i["key"] == "login")
        self.assertFalse(item["optional"])
        self.assertEqual(needed["total"], 6)
        free = self.build(search_needs_login=False)
        item = next(i for i in free["items"] if i["key"] == "login")
        self.assertTrue(item["optional"])
        self.assertIn("searching needs no login", item["detail"])
        self.assertEqual(free["total"], 5, "the optional item doesn't count")
        self.assertTrue(all(not i["optional"] for i in free["items"] if i["key"] != "login"))

    def test_an_optional_login_does_not_hold_the_list_open(self):
        flat = {"search_positions": json.dumps(["SRE"]), "search_locations": json.dumps(["Pune"])}
        s = self.Settings()
        s.read = lambda k, d="": {"setup_llm_tested": json.dumps({"provider": "p", "model": "m", "at": "2026-10-01"}),
                                  "setup_search_run": "2026-10-01T09:00:00"}.get(k, d)
        self.assertTrue(self.build(settings=s, profile_flat=flat, search_needs_login=False)["complete"])
        self.assertFalse(self.build(settings=s, profile_flat=flat, search_needs_login=True)["complete"])


@unittest.skipUnless(HAVE_FASTAPI, "fastapi + httpx not installed")
class StatusHttpTests(unittest.TestCase):

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
        tracker = mock.patch.object(tracking.progress, "TRACKER", Progress())
        tracker.start()
        self.addCleanup(tracker.stop)
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

    def wait_for_thread(self):
        deadline = time.time() + 10
        while time.time() < deadline and any(t.name == "pipeline-run" and t.is_alive() for t in threading.enumerate()):
            time.sleep(0.02)

    def test_status_follows_a_run_and_a_second_run_is_refused(self):
        import tracking.pipeline
        gate = threading.Event()

        def slow_run(progress=None, **kw):
            progress.begin("boards", 2, "Searching job boards")
            progress.tick("boards", "Linkedin · Dev in Pune", 7)
            gate.wait(5)
            progress.finish({"total_found": 7})
            return {"total_found": 7}

        with mock.patch.object(tracking.pipeline, "run", slow_run):
            r = self.alice.post("/api/pipeline/run", json={})
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["run_id"], 1)
            deadline = time.time() + 5
            while time.time() < deadline and self.alice.get("/api/pipeline/status").json()["found"] != 7:
                time.sleep(0.02)
            s = self.alice.get("/api/pipeline/status").json()
            self.assertEqual((s["running"], s["found"], s["percent"], s["current"]), (True, 7, 50, "Linkedin · Dev in Pune"))
            self.assertEqual(self.alice.post("/api/pipeline/run", json={}).status_code, 409)
            self.assertFalse(self.bob.get("/api/pipeline/status").json()["running"], "another account sees no details")
            c = self.alice.post("/api/pipeline/cancel").json()
            self.assertEqual(c["status"], "cancel_requested")
            self.assertTrue(self.alice.get("/api/pipeline/status").json()["cancelled"])
            gate.set()
            self.wait_for_thread()
        s = self.alice.get("/api/pipeline/status").json()
        self.assertEqual((s["running"], s["stats"]), (False, {"total_found": 7}))
        self.assertEqual(self.bob.get("/api/pipeline/status").status_code, 200)

    def test_a_run_that_returns_without_closing_is_closed_by_the_route(self):
        import tracking.pipeline
        with mock.patch.object(tracking.pipeline, "run", lambda **kw: {"total_found": 0}):
            self.assertEqual(self.alice.post("/api/pipeline/run", json={}).status_code, 200)
            self.wait_for_thread()
        self.assertFalse(self.alice.get("/api/pipeline/status").json()["running"])
        with mock.patch.object(tracking.pipeline, "run", lambda **kw: (_ for _ in ()).throw(RuntimeError("boom"))):
            self.assertEqual(self.alice.post("/api/pipeline/run", json={}).status_code, 200)
            self.wait_for_thread()
        s = self.alice.get("/api/pipeline/status").json()
        self.assertEqual((s["running"], s["error"]), (False, "RuntimeError: boom"))

    def test_status_needs_a_session(self):
        anon = TestClient(self.app, follow_redirects=False)
        self.assertEqual(anon.get("/api/pipeline/status").status_code, 401)


if __name__ == "__main__":
    unittest.main()
