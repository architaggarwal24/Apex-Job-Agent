# tests/test_sources_keyed_apify.py
"""
tests/test_sources_keyed_apify.py — stage 3 of the login-free sources:

  * Adzuna and JSearch (keyed) against canned payloads; Himalayas sends its key
  * sources.run skips a keyed source until its key is set (skipped, not failed)
  * scraper/apify_source: per-actor input, dataset rows -> job dicts, the
    run-sync call, token/actor errors, no token
  * the pipeline's "apify" engine, and the actor / engine settings validation

No network: fetch and post are fakes. No model.

Run from the project root:   python3 -m unittest tests.test_sources_keyed_apify -v
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
os.environ.setdefault("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())

from db.sources_db import SourceStore
from scraper import apify_source, sources
from scraper.sources import aggregators
from tests.test_sources import FakeFetch, ctx

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except Exception:
    HAVE_FASTAPI = False


def kctx(fetch, **keys):
    c = ctx(fetch, titles=["Backend Engineer"], locations=["Bangalore", "Remote"])
    c.keys = keys
    c.hours_old = 72
    return c


class KeyedSourceTests(unittest.TestCase):

    def test_adzuna_asks_per_title_and_location_with_the_keys_and_country(self):
        f = FakeFetch({"https://api.adzuna.com/v1/api/jobs/in/search/1": {"results": [
            {"title": "Backend Engineer", "company": {"display_name": "Acme"}, "location": {"display_name": "Bengaluru, Karnataka"},
             "redirect_url": "https://www.adzuna.in/land/ad/1", "description": "Go…", "created": "2026-10-20T00:00:00Z",
             "salary_min": 1500000, "salary_max": 2000000, "salary_is_predicted": "0"}]}})
        jobs = aggregators.adzuna(kctx(f, adzuna_app_id="id1", adzuna_api_key="k1"))
        self.assertEqual(len(f.calls), 2)
        self.assertEqual((f.calls[0][1]["app_id"], f.calls[0][1]["app_key"], f.calls[0][1]["where"], f.calls[0][1]["max_days_old"]), ("id1", "k1", "Bangalore", 3))
        self.assertNotIn("where", f.calls[1][1], "the Remote query has no place")
        self.assertEqual((jobs[0]["source"], jobs[0]["url"], jobs[0]["salary"], jobs[0]["posted_at"]),
                         ("adzuna", "https://www.adzuna.in/land/ad/1", "1,500,000-2,000,000 /year", "2026-10-20"))

    def test_jsearch_sends_the_rapidapi_headers_and_prefers_the_ats_form(self):
        f = FakeFetch({"https://jsearch.p.rapidapi.com/search": {"data": [
            {"job_title": "Backend Engineer", "employer_name": "Acme", "job_city": "Bengaluru", "job_country": "IN", "job_is_remote": False,
             "job_apply_link": "https://in.linkedin.com/jobs/view/9", "job_description": "x", "job_posted_at_datetime_utc": "2026-10-21T10:00:00.000Z",
             "apply_options": [{"publisher": "LinkedIn", "apply_link": "https://in.linkedin.com/jobs/view/9", "is_direct": False},
                               {"publisher": "Acme careers", "apply_link": "https://careers.acme.example/apply/9", "is_direct": True},
                               {"publisher": "Greenhouse", "apply_link": "https://boards.greenhouse.io/acme/jobs/9", "is_direct": False}]}]}})
        calls_headers = []
        orig = f.get_json
        f.get_json = lambda url, params=None, headers=None: (calls_headers.append(headers), orig(url, params))[1]
        jobs = aggregators.jsearch(kctx(f, jsearch_api_key="rk"))
        self.assertEqual(calls_headers[0], {"X-RapidAPI-Key": "rk", "X-RapidAPI-Host": "jsearch.p.rapidapi.com"})
        self.assertEqual((f.calls[0][1]["query"], f.calls[0][1]["date_posted"]), ("Backend Engineer in Bangalore", "3days"))
        self.assertEqual(f.calls[1][1]["remote_jobs_only"], "true")
        self.assertEqual((jobs[0]["url"], jobs[0]["apply_url"]), ("https://in.linkedin.com/jobs/view/9", "https://boards.greenhouse.io/acme/jobs/9"))

    def test_himalayas_sends_a_bearer_key_when_it_has_one(self):
        seen = []
        f = FakeFetch({"https://himalayas.app/jobs/api/search": {"jobs": []}})
        orig = f.get_json
        f.get_json = lambda url, params=None, headers=None: (seen.append(headers), orig(url, params))[1]
        aggregators.himalayas(kctx(f, himalayas_api_key="h1"))
        aggregators.himalayas(kctx(f))
        self.assertEqual(seen, [{"Authorization": "Bearer h1"}, None])


class KeyGatingTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = SourceStore(1, Path(self.tmp.name) / "jobs.db", clock=lambda: 1000.0)

    def test_a_keyed_source_is_skipped_until_its_key_is_set(self):
        f = FakeFetch({"https://api.adzuna.com/v1/api/jobs/in/search/1": {"results": []}, "https://jsearch.p.rapidapi.com/search": {"data": []}})
        _, report = sources.run(self.store, ["Dev"], ["Remote"], read=lambda k, d="": d, fetch=f, only=["adzuna", "jsearch"], clock=lambda: 1000.0)
        self.assertIn("needs a key", report["adzuna"]["skipped"])
        self.assertIn("jsearch_api_key", report["jsearch"]["skipped"])
        self.assertEqual(f.calls, [])
        keys = {"adzuna_app_id": "a", "adzuna_api_key": "b", "jsearch_api_key": "c", "search_hours_old": "24"}
        _, report = sources.run(self.store, ["Dev"], ["Remote"], read=lambda k, d="": keys.get(k, d), fetch=f, only=["adzuna", "jsearch"], clock=lambda: 1000.0)
        self.assertEqual((report["adzuna"]["skipped"], report["jsearch"]["skipped"]), ("", ""))
        self.assertEqual(f.calls[0][1]["max_days_old"], 1)

    def test_the_api_reports_which_keys_are_missing(self):
        self.assertEqual(sources.SOURCES["adzuna"]["needs_key"], ["adzuna_app_id", "adzuna_api_key"])
        self.assertEqual(sources.SOURCES["greenhouse"]["needs_key"], [])


class ApifyTests(unittest.TestCase):

    def test_actor_ids(self):
        self.assertEqual(apify_source.clean_actor("bebity/linkedin-jobs-scraper"), "bebity~linkedin-jobs-scraper")
        for bad in ("", "nope", "a~b~c", "a b~c"):
            with self.assertRaises(ValueError):
                apify_source.clean_actor(bad)

    def test_each_board_gets_its_actors_input(self):
        li = apify_source.actor_input("linkedin", "x~y", "Dev", "Bangalore", 30, 24, "India")
        self.assertEqual((li["title"], li["location"], li["rows"], li["publishedAt"]), ("Dev", "Bangalore", 30, "r86400"))
        ind = apify_source.actor_input("indeed", "x~y", "Dev", "Bangalore", 30, 168, "India")
        self.assertEqual((ind["position"], ind["country"], ind["maxItemsPerSearch"]), ("Dev", "IN", 30))
        self.assertEqual(apify_source.actor_input("indeed", "x~y", "Dev", "Austin", 5, 0, "United States")["country"], "US")
        self.assertEqual(apify_source.actor_input("naukri", "x~y", "Dev", "Pune", 30, 72, "India")["freshness"], 3)

    def test_rows_from_the_known_actors_normalise(self):
        li = apify_source.normalize("linkedin", {"title": "Backend Engineer", "companyName": "Acme", "location": "Bengaluru",
                                                 "jobUrl": "https://www.linkedin.com/jobs/view/1?tracking=x", "applyUrl": "https://boards.greenhouse.io/acme/jobs/1",
                                                 "applyType": "EXTERNAL", "description": "Go", "postedTime": "2026-10-20"})
        self.assertEqual((li["url"], li["apply_url"], li["posted_at"]), ("https://www.linkedin.com/jobs/view/1", "https://boards.greenhouse.io/acme/jobs/1", "2026-10-20"))
        easy = apify_source.normalize("linkedin", {"title": "Dev", "companyName": "Acme", "jobUrl": "https://www.linkedin.com/jobs/view/2",
                                                   "applyUrl": "https://www.linkedin.com/jobs/view/2/apply"})
        self.assertEqual(easy["apply_url"], "")
        ind = apify_source.normalize("indeed", {"positionName": "Dev", "company": "Beta", "location": "Pune, Maharashtra",
                                                "url": "https://in.indeed.com/viewjob?jk=1", "externalApplyLink": "https://jobs.lever.co/beta/1",
                                                "salary": "₹10L - ₹15L a year", "postedAt": "2026-10-19T00:00:00.000Z"})
        self.assertEqual((ind["apply_url"], ind["salary"], ind["posted_at"]), ("https://jobs.lever.co/beta/1", "₹10L - ₹15L a year", "2026-10-19"))
        nk = apify_source.normalize("naukri", {"jobTitle": "Dev", "companyName": "Gamma", "jobUrl": "https://www.naukri.com/job-listings-1", "jdSnippet": "x"})
        self.assertEqual((nk["source"], nk["apply_url"]), ("naukri", ""))
        self.assertIsNone(apify_source.normalize("naukri", {"jobTitle": "Dev"}))
        self.assertIsNone(apify_source.normalize("naukri", "nope"))

    def test_search_posts_to_run_sync_with_the_token_and_collects_rows(self):
        posts = []
        def post(url, params, body):
            posts.append((url, params, body))
            return [{"title": "Dev", "companyName": "Acme", "jobUrl": f"https://www.linkedin.com/jobs/view/{len(posts)}"},
                    {"title": "Dev", "companyName": "Acme", "jobUrl": "https://www.linkedin.com/jobs/view/1"}]
        jobs = apify_source.search("linkedin", {"_flat": {}}, ["Dev"], ["Bangalore", "Remote"], token="tok", actor="bebity/linkedin-jobs-scraper",
                                   results_per_query=10, hours_old=168, post=post)
        self.assertEqual(posts[0][0], "https://api.apify.com/v2/acts/bebity~linkedin-jobs-scraper/run-sync-get-dataset-items")
        self.assertEqual(posts[0][1]["token"], "tok")
        self.assertEqual(posts[0][2]["location"], "Bangalore, India")      # a bare city names the country
        self.assertEqual(posts[1][2]["location"], "Remote")
        self.assertEqual([j["url"] for j in jobs], ["https://www.linkedin.com/jobs/view/1", "https://www.linkedin.com/jobs/view/2"])

    def test_results_in_another_country_are_dropped_unless_remote(self):
        rows = [{"title": "Dev", "companyName": "A", "jobUrl": "https://www.linkedin.com/jobs/view/1", "location": "Delhi, MI, USA"},
                {"title": "Dev", "companyName": "B", "jobUrl": "https://www.linkedin.com/jobs/view/2", "location": "Delhi, India"},
                {"title": "Dev", "companyName": "C", "jobUrl": "https://www.linkedin.com/jobs/view/3", "location": "Remote, United States"}]
        jobs = apify_source.search("linkedin", {"_flat": {"location_country": "India"}}, ["Dev"], ["Delhi"], token="t", actor="",
                                   post=lambda *a: rows)
        self.assertEqual([j["company"] for j in jobs], ["B", "C"])

    def test_errors(self):
        with self.assertRaises(apify_source.ApifyError):
            apify_source.search("indeed", {}, ["Dev"], ["X"], token="", actor="", post=lambda *a: [])
        def bad(url, params, body):
            return {"error": {"type": "actor-not-found", "message": "Actor was not found"}}
        jobs = apify_source.search("indeed", {}, ["Dev"], ["X"], token="t", actor="", post=bad)
        self.assertEqual(jobs, [], "a failed query is logged and skipped")
        with self.assertRaises(apify_source.ApifyError):
            apify_source.run_actor("t", "a~b", {}, post=bad)
        with self.assertRaises(ValueError):
            apify_source.search("glassdoor", {}, ["Dev"], ["X"], token="t", actor="", post=lambda *a: [])


class PipelineApifyTests(unittest.TestCase):

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
                  mock.patch("intelligence.scorer.score_jobs_batch", lambda jobs, profile, **kw: []),
                  mock.patch("intelligence.briefer.generate_and_save", lambda *a, **kw: None),
                  mock.patch("playwright.sync_api.sync_playwright", side_effect=AssertionError("a browser started")),
                  mock.patch("requests.request", side_effect=AssertionError("a test touched the network")),
                  mock.patch("scraper.jobspy_source.search", side_effect=AssertionError("jobspy ran"))):
            p.start()
            self.addCleanup(p.stop)
        ProfileDB().set_many({"search_positions": '["Backend Engineer"]', "search_locations": '["Bangalore"]'})
        self.settings = SettingsDB.get()

    def test_a_board_on_the_apify_engine_uses_the_token_and_actor_from_settings(self):
        import tracking.pipeline
        self.settings.write("search_engine_linkedin", "apify")
        self.settings.write("apify_api_token", "apify_tok")
        self.settings.write("apify_actor_linkedin", "someone~linkedin-actor")
        with mock.patch("scraper.apify_source.search", return_value=[]) as search:
            tracking.pipeline.run(platforms=["linkedin"], sources=[])
        self.assertEqual(search.call_args.args[0], "linkedin")
        self.assertEqual((search.call_args.kwargs["token"], search.call_args.kwargs["actor"]), ("apify_tok", "someone~linkedin-actor"))

    def test_no_token_fails_that_board_only(self):
        import tracking.pipeline
        self.settings.write("search_engine_naukri", "apify")
        logs = []
        stats = tracking.pipeline.run(platforms=["naukri"], sources=[], emit=lambda ev, msg: logs.append(msg))
        self.assertEqual(stats["total_found"], 0)
        self.assertTrue(any("naukri: FAILED" in m and "no Apify token" in m for m in logs), logs)


@unittest.skipUnless(HAVE_FASTAPI, "fastapi + httpx not installed")
class SettingsHttpTests(unittest.TestCase):

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
        self.client = TestClient(create_app(ui_dir=tmp / "no-build"), follow_redirects=False)
        self.client.post("/api/auth/signup", json={"email": "alice@example.com", "password": "correct-horse-battery"})

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

    def test_keys_are_secrets_and_the_actor_is_validated(self):
        r = self.client.post("/api/settings", json={"apify_api_token": "apify_api_abcdef123456", "adzuna_app_id": "abc12345",
                                                    "adzuna_api_key": "zz99zz99zz99", "jsearch_api_key": "rapid-key-0000",
                                                    "apify_actor_indeed": "misceres/indeed-scraper", "search_engine_indeed": "apify"})
        self.assertEqual(r.status_code, 200, r.text)
        s = self.client.get("/api/settings").json()["settings"]
        for k in ("apify_api_token", "adzuna_api_key", "jsearch_api_key"):
            self.assertTrue(s[k].startswith("••••"), k)
        self.assertEqual((s["adzuna_app_id"], s["apify_actor_indeed"], s["search_engine_indeed"]), ("abc12345", "misceres~indeed-scraper", "apify"))
        self.assertEqual(self.client.get("/api/settings/raw/apify_api_token").status_code, 403)
        self.assertEqual(self.client.post("/api/settings", json={"apify_actor_naukri": "not an actor"}).status_code, 400)
        src = {x["name"]: x for x in self.client.get("/api/sources").json()["sources"]}
        self.assertEqual((src["adzuna"]["key_set"], src["jsearch"]["key_set"], src["greenhouse"]["key_set"]), (True, True, True))


if __name__ == "__main__":
    unittest.main()
