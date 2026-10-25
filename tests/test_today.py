# tests/test_today.py
"""
tests/test_today.py — the Today and Insights pages' backend and plain-JS parts (the pages
themselves were exercised in a real headless browser against the real API).

  * the get-started checklist (core/checklist.py, GET /api/setup/checklist): each item's
    rule, per-user parts, and the two recorded markers - a SUCCESSFUL provider test, a
    FINISHED search - with the failure cases recording nothing;
  * /api/auth/me tells the UI whether this is the owner account;
  * the generic settings save cannot forge the setup_ markers;
  * the new workspace filters the Today links use (next_step_within, followups=due) and the
    Insights helpers - above all that "not enough data" is never turned into a number - under Node;
  * /insights is a Next route, and the sidebar reads Today / Applications / Insights.

No network, no LLM: the provider test and the search pipeline are replaced by fakes.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
os.environ["APP_ENCRYPTION_KEY"] = Fernet.generate_key().decode()

from core import checklist
from db.settings_db import SettingsDB
from server.static_shell import SHELL_ROUTES

ROOT = Path(__file__).resolve().parent.parent
LIB = ROOT / "frontend" / "src" / "lib"


class FakeSettings:
    def __init__(self, **v):
        self.v = dict(v)

    def read(self, key, default=""):
        return self.v.get(key, default)

    def write(self, key, value):
        self.v[key] = value


def build(settings=None, **kw):
    args = dict(settings=settings or FakeSettings(), profile_flat={}, profile_missing=["full_name"],
                resume_file=False, resume_variants=0, logged_in={"linkedin": False}, job_count=0)
    args.update(kw)
    out = checklist.build(**args)
    return out, {i["key"]: i["done"] for i in out["items"]}


class ChecklistRuleTests(unittest.TestCase):
    def test_nothing_done(self):
        out, done = build()
        self.assertEqual(list(done), ["resume", "profile", "search_prefs", "llm", "login", "search"])
        self.assertFalse(any(done.values()))
        self.assertEqual((out["done"], out["total"], out["complete"]), (0, 6, False))

    def test_each_rule(self):
        self.assertTrue(build(resume_file=True)[1]["resume"])
        self.assertTrue(build(resume_variants=2)[1]["resume"])
        self.assertTrue(build(profile_missing=[])[1]["profile"])
        both = {"search_positions": json.dumps(["SRE"]), "search_locations": json.dumps(["Pune"])}
        self.assertTrue(build(profile_flat=both)[1]["search_prefs"])
        self.assertFalse(build(profile_flat={"search_positions": json.dumps(["SRE"]), "search_locations": "[]"})[1]["search_prefs"])
        self.assertFalse(build(profile_flat={"search_positions": "not json", "search_locations": json.dumps([" "])})[1]["search_prefs"])
        self.assertTrue(build(logged_in={"linkedin": False, "naukri": True})[1]["login"])
        self.assertTrue(build(job_count=3)[1]["search"])
        s = FakeSettings()
        checklist.mark_search_run(s, datetime(2026, 10, 1, 9, 0))
        self.assertTrue(build(settings=s)[1]["search"])

    def test_llm_needs_a_recorded_successful_test(self):
        s = FakeSettings()
        self.assertFalse(build(settings=s)[1]["llm"])
        checklist.mark_llm_tested(s, "openai", "gpt-x", datetime(2026, 10, 1, 9, 0))
        out, done = build(settings=s)
        self.assertTrue(done["llm"])
        self.assertIn("openai / gpt-x", next(i for i in out["items"] if i["key"] == "llm")["detail"])
        for junk in ("not json", "[]", json.dumps({"provider": "x"})):
            self.assertFalse(build(settings=FakeSettings(setup_llm_tested=junk))[1]["llm"], junk)

    def test_complete(self):
        s = FakeSettings()
        checklist.mark_llm_tested(s, "p", "m")
        out, done = build(settings=s, resume_file=True, profile_missing=[], logged_in={"indeed": True}, job_count=1,
                          profile_flat={"search_positions": '["a"]', "search_locations": '["b"]'})
        self.assertTrue(out["complete"], done)


@unittest.skipUnless(shutil.which("node"), "Node.js not installed")
class TodayJsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            for name in ("stages", "filters", "insights"):
                text = (LIB / f"{name}.js").read_text(encoding="utf-8").replace("'@/lib/stages'", "'./stages.mjs'")
                (tmp / f"{name}.mjs").write_text(text, encoding="utf-8")
            (tmp / "check.mjs").write_text((ROOT / "tests" / "fixtures" / "today_check.mjs").read_text(encoding="utf-8"), encoding="utf-8")
            r = subprocess.run(["node", str(tmp / "check.mjs")], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise AssertionError(f"node failed: {r.stderr[-2000:]}")
        cls.o = json.loads(r.stdout)

    def test_next_step_within(self):
        o = self.o
        self.assertEqual((o["within_soon"], o["within_far"], o["within_past"], o["within_empty"]), (True, False, False, False))
        self.assertEqual((o["within_today_date"], o["within_day_after"], o["within_3days"]), (True, True, False))

    def test_today_links_land_on_exactly_those_cards(self):
        o = self.o
        self.assertEqual(o["interviews_link"], "soon,day")              # interview stage AND next step within 48 h
        self.assertEqual(o["followups_link"], "far,other")
        self.assertEqual(o["followups_link_none_loaded"], "")
        self.assertEqual(o["needs_you_link"], ["needs_you"])
        self.assertEqual(o["review_link"], [["ready_for_review"], "review"])
        self.assertEqual(o["within_bad"], ["", "", ""])
        self.assertEqual(o["roundtrip"], "?next_step_within=48&followups=due")

    def test_not_enough_data_is_never_a_number(self):
        o = self.o
        self.assertEqual(o["rate_num"], {"text": "40.9%", "pct": 40.9})
        for k in ("rate_small", "rate_null", "rate_numeric_string"):
            self.assertIsNone(o[k]["pct"], k)
            self.assertNotIn("%", o[k]["text"], k)
        self.assertEqual(o["rate_small"]["text"], "not enough data")
        self.assertEqual(o["median"], ["3 days", "1 day", "no responses yet", "not enough data"])

    def test_budget_and_countdown(self):
        self.assertEqual(self.o["budget"], [{"text": "3 / 10 today", "pct": 30, "full": False},
                                            {"text": "12 / 10 today", "pct": 100, "full": True},
                                            {"text": "4 today · no daily limit", "pct": None, "full": False}])
        self.assertEqual(self.o["countdown"], ["in 1h 01m 05s", "in 3m 00s", "now", "", ""])


try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except Exception:
    HAVE_FASTAPI = False


@unittest.skipUnless(HAVE_FASTAPI, "fastapi + httpx not installed")
class TodayHttpTests(unittest.TestCase):
    def setUp(self):
        from core.config import cfg
        import core.auth as auth
        from db.users_db import UsersDB
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self._saved = (cfg.JOBS_DB_PATH, cfg.SESSIONS_DIR, cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE, cfg.ALLOW_SIGNUP,
                       cfg.COOKIES_DIR, cfg.RESUME_PATH, cfg.DATA_DIR, UsersDB._instance, auth._serializer, SettingsDB._instance)
        SettingsDB._instance = SettingsDB(path=tmp / "settings.db")
        cfg.JOBS_DB_PATH, cfg.SESSIONS_DIR, cfg.COOKIES_DIR = tmp / "jobs.db", tmp / "sessions", tmp / "cookies"
        cfg.RESUME_PATH, cfg.DATA_DIR = tmp / "resume.pdf", tmp
        cfg.SESSIONS_DIR.mkdir(); cfg.COOKIES_DIR.mkdir()
        cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE, cfg.ALLOW_SIGNUP = "test-signing-secret-" + "x" * 40, False, False
        UsersDB._instance, auth._serializer = UsersDB(path=tmp / "users.db"), None
        profile_path = tmp / "profile.db"

        def profile_init(db, path=None):                       # every ProfileDB() in this test uses the temp file
            from db.profile_db import ProfileDB
            db.path = profile_path
            ProfileDB._init_schema(db)
            ProfileDB._seed(db)
        self.profile = mock.patch("db.profile_db.ProfileDB.__init__", profile_init)
        self.profile.start()
        self.addCleanup(self.profile.stop)
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
        (cfg.JOBS_DB_PATH, cfg.SESSIONS_DIR, cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE, cfg.ALLOW_SIGNUP, cfg.COOKIES_DIR,
         cfg.RESUME_PATH, cfg.DATA_DIR, UsersDB._instance, auth._serializer, SettingsDB._instance) = self._saved
        self.tmp.cleanup()

    def items(self, client):
        r = client.get("/api/setup/checklist")
        self.assertEqual(r.status_code, 200, r.text)
        return {i["key"]: i["done"] for i in r.json()["items"]}

    def test_me_says_who_is_the_owner(self):
        self.assertIs(self.alice.get("/api/auth/me").json()["owner"], True)
        self.assertIs(self.bob.get("/api/auth/me").json()["owner"], False)

    def test_checklist_reads_the_real_sources(self):
        self.assertEqual(self.items(self.alice), {k: False for k in ("resume", "profile", "search_prefs", "llm", "login", "search")})
        from core.config import cfg
        from core.crypto import encrypt_bytes
        from db.jobs_db import JobsDB
        cfg.RESUME_PATH.write_bytes(b"%PDF-1.4")
        from db.profile_db import ProfileDB
        ProfileDB().set_many({"full_name": "A", "email": "a@example.com", "phone": "1"})
        self.assertEqual(self.alice.post("/api/search-prefs", json={"positions": ["SRE"], "locations": ["Pune"]}).status_code, 200)
        cookies = [{"name": "li_at", "domain": ".linkedin.com", "expires": time.time() + 3600}]
        (cfg.COOKIES_DIR / "linkedin.json").write_bytes(encrypt_bytes(json.dumps(cookies).encode()))
        JobsDB(user_id=1).add({"url": "https://x.example/1", "title": "T", "company": "C", "source": "linkedin"})
        done = self.items(self.alice)
        self.assertEqual({k: v for k, v in done.items() if k != "llm"},
                         {"resume": True, "profile": True, "search_prefs": True, "login": True, "search": True})
        self.assertFalse(done["llm"])
        self.assertEqual(self.items(self.bob)["search"], False)          # jobs are per user
        self.assertEqual(TestClient(self.app).get("/api/setup/checklist").status_code, 401)

    def test_expired_login_is_not_logged_in(self):
        from core.config import cfg
        (cfg.COOKIES_DIR / "indeed.json").write_text(json.dumps([{"name": "x", "domain": ".indeed.com", "expires": time.time() - 60}]))
        self.assertFalse(self.items(self.alice)["login"])

    def test_resume_variants_count_per_user(self):
        from db.resumes_db import ResumeStore
        with mock.patch.object(ResumeStore, "list", lambda store: [{"id": 1}] if store.user_id == 2 else []):
            self.assertTrue(self.items(self.bob)["resume"])
            self.assertFalse(self.items(self.alice)["resume"])

    def test_a_successful_provider_test_is_recorded_and_a_failed_one_is_not(self):
        from core import llm
        with mock.patch.object(llm, "test_connection", side_effect=llm.LLMTestError("No API key set for this provider yet.")):
            self.assertEqual(self.alice.post("/api/settings/test-llm", json={}).status_code, 400)
        self.assertFalse(self.items(self.alice)["llm"])
        with mock.patch.object(llm, "test_connection", return_value={"response": "OK", "latency_ms": 5, "note": ""}):
            self.assertEqual(self.alice.post("/api/settings/test-llm", json={}).status_code, 200)
            self.assertEqual(self.bob.post("/api/settings/test-llm", json={}).status_code, 403)
        self.assertTrue(self.items(self.alice)["llm"])
        tested = checklist.llm_tested(SettingsDB.get())
        self.assertEqual(tested["provider"], SettingsDB.get().llm_config()["provider"])

    def test_the_markers_cannot_be_written_through_the_settings_save(self):
        for key in ("setup_llm_tested", "setup_search_run"):
            r = self.alice.post("/api/settings", json={key: "2026-01-01"})
            self.assertEqual(r.status_code, 400, key)
        self.assertEqual(SettingsDB.get().read("setup_search_run", ""), "")

    def _run_pipeline(self, fake):
        import tracking.pipeline
        with mock.patch.object(tracking.pipeline, "run", fake):
            self.assertEqual(self.alice.post("/api/pipeline/run", json={"quick": True}).status_code, 200)
            deadline = time.time() + 10
            import threading
            while time.time() < deadline and any(t.name == "pipeline-run" and t.is_alive() for t in threading.enumerate()):
                time.sleep(0.05)

    def test_a_finished_search_is_recorded(self):
        self._run_pipeline(lambda **kw: {"total_found": 0})
        self.assertTrue(SettingsDB.get().read("setup_search_run", ""))
        self.assertTrue(self.items(self.alice)["search"])

    def test_a_crashed_search_is_not(self):
        def boom(**kw):
            raise RuntimeError("scraper fell over")
        self._run_pipeline(boom)
        self.assertEqual(SettingsDB.get().read("setup_search_run", ""), "")
        self.assertFalse(self.items(self.alice)["search"])


class WiringTests(unittest.TestCase):
    def test_insights_is_a_shell_route(self):
        self.assertIn("/insights", SHELL_ROUTES)
        page = ROOT / "frontend" / "src" / "app" / "insights" / "page.jsx"
        self.assertIn("/api/analytics/outcomes", page.read_text(encoding="utf-8"))

    def test_sidebar(self):
        shell = (ROOT / "frontend" / "src" / "components" / "layout" / "AppShell.jsx").read_text(encoding="utf-8")
        nav = shell[shell.index("const NAV"):shell.index("]", shell.index("const NAV"))]
        order = [nav.index(f"'{label}'") for label in ("Today", "Applications", "Insights")]
        self.assertEqual(order, sorted(order))
        self.assertNotIn("'Dashboard'", nav)

    def test_today_no_longer_redirects_to_setup(self):
        today = (ROOT / "frontend" / "src" / "app" / "page.jsx").read_text(encoding="utf-8")
        self.assertNotRegex(today, r"backendUrl\(|location\.href\s*=")         # the old Dashboard's redirect to /setup
        self.assertIn("/api/setup/checklist", today)


if __name__ == "__main__":
    unittest.main()
