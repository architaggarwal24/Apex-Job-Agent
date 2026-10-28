# tests/test_search_engines.py
"""
tests/test_search_engines.py — searching the boards without a login:

  * scraper/jobspy_source: JobSpy rows -> job dicts (listing url vs the
    company's apply form, salary, posted date), one query per title x location,
    a failed query doesn't sink the others, LinkedIn asks for descriptions
  * scraper/engines: the per-board engine setting and its validation
  * tracking/pipeline: the engine decides whether a board is searched by
    JobSpy or the saved-login browser scraper, no browser starts when no board
    needs one, and apply_url reaches the database
  * POST /api/settings validates the search keys (owner only)

Nothing here reaches the network or a model: scrape_jobs is a fake, the
scorer is a fake, Playwright is trapped.

Run from the project root:   python3 -m unittest tests.test_search_engines -v
"""

import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
os.environ.setdefault("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())

from scraper import engines, jobspy_source

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except Exception:
    HAVE_FASTAPI = False


def row(**kw):
    base = {"job_url": "https://www.linkedin.com/jobs/view/123?refId=abc", "job_url_direct": None,
            "title": "Backend Engineer", "company": "Acme", "location": "Bengaluru, Karnataka, India",
            "description": "Build services.", "min_amount": math.nan, "max_amount": math.nan,
            "currency": None, "interval": None, "job_level": "mid-senior level", "date_posted": "2026-10-27",
            "skills": None}
    base.update(kw)
    return base


LI_PAGE = """<div class="show-more-less-html__markup show-more-less-html__markup--clamp"><p>Build <b>things</b></p><ul><li>Python</li></ul></div>
<code id="applyUrl" style="display: none"><!--"https://www.linkedin.com/jobs/view/externalApply/4012345?url=https%3A%2F%2Fboards%2Egreenhouse%2Eio%2Facme%2Fjobs%2F9%3Fgh_src%3Dlinkedin&urlHash=AbCd"--></code>
<ul class="description__job-criteria-list"><li class="description__job-criteria-item"><h3 class="description__job-criteria-subheader">Seniority level</h3>
<span class="description__job-criteria-text">Mid-Senior level</span></li></ul>"""


class FakeFrame:
    def __init__(self, rows):
        self.rows = rows

    def to_dict(self, orient):
        assert orient == "records"
        return list(self.rows)


class NormalizeTests(unittest.TestCase):

    def test_a_company_apply_link_becomes_apply_url_and_the_card_stays_the_listing(self):
        job = jobspy_source.normalize("linkedin", row(job_url_direct="https://boards.greenhouse.io/acme/jobs/9"))
        self.assertEqual(job["url"], "https://www.linkedin.com/jobs/view/123")       # tracking id stripped
        self.assertEqual(job["apply_url"], "https://boards.greenhouse.io/acme/jobs/9")
        self.assertEqual((job["source"], job["posted_at"], job["seniority"]), ("linkedin", "2026-10-27", "mid-senior level"))

    def test_a_direct_link_back_to_the_same_board_is_not_an_apply_form(self):
        self.assertEqual(jobspy_source.normalize("linkedin", row(job_url_direct="https://www.linkedin.com/jobs/view/123/apply"))["apply_url"], "")
        self.assertEqual(jobspy_source.normalize("indeed", row(job_url="https://in.indeed.com/viewjob?jk=1",
                                                               job_url_direct="https://in.indeed.com/applystart?jk=1"))["apply_url"], "")
        self.assertEqual(jobspy_source.normalize("naukri", row(job_url="https://www.naukri.com/job-listings-x-1"))["apply_url"], "")

    def test_salary_skills_and_missing_cells(self):
        job = jobspy_source.normalize("naukri", row(job_url="https://www.naukri.com/job-listings-x-1", min_amount=1200000.0,
                                                    max_amount=1800000.0, currency="INR", interval="yearly", skills="python, django"))
        self.assertEqual(job["salary"], "INR 1,200,000-1,800,000 /yearly")
        self.assertTrue(job["description"].endswith("Skills: python, django"))
        bare = jobspy_source.normalize("indeed", row(job_url="https://in.indeed.com/viewjob?jk=2", description=math.nan,
                                                     job_level=math.nan, date_posted=None))
        self.assertEqual((bare["description"], bare["seniority"], bare["posted_at"], bare["salary"]), ("", "", "", ""))

    def test_rows_without_a_title_company_or_url_are_dropped(self):
        self.assertIsNone(jobspy_source.normalize("linkedin", row(title="")))
        self.assertIsNone(jobspy_source.normalize("linkedin", row(company=math.nan)))
        self.assertIsNone(jobspy_source.normalize("linkedin", row(job_url=None)))


class SearchTests(unittest.TestCase):

    def test_one_query_per_title_and_location_with_the_boards_own_options(self):
        calls = []
        def fake(**kw):
            calls.append(kw)
            return FakeFrame([row(job_url=f"https://www.linkedin.com/jobs/view/{len(calls)}")])
        pages = []
        jobs = jobspy_source.search("linkedin", {"_flat": {"location_country": "India"}},
                                    ["AI Engineer", "Backend Engineer"], ["Bangalore", "Remote"],
                                    results_per_query=7, hours_old=48, scrape_jobs=fake,
                                    get=lambda url: (pages.append(url), (200, LI_PAGE))[1])
        self.assertEqual(len(calls), 4)
        # a bare city names the profile's country; "Remote" is remote jobs in that country
        self.assertEqual([(c["search_term"], c["location"], c["is_remote"]) for c in calls],
                         [("AI Engineer", "Bangalore, India", False), ("AI Engineer", "India", True),
                          ("Backend Engineer", "Bangalore, India", False), ("Backend Engineer", "India", True)])
        self.assertEqual((calls[0]["site_name"], calls[0]["results_wanted"], calls[0]["hours_old"], calls[0]["country_indeed"]),
                         (["linkedin"], 7, 48, "india"))
        self.assertFalse(calls[0]["fetch_description"])           # LinkedIn's job pages are read by us, once each
        self.assertEqual(len(jobs), 4)
        self.assertEqual(pages, [f"https://www.linkedin.com/jobs/view/{n}" for n in (1, 2, 3, 4)])
        self.assertEqual((jobs[0]["apply_url"], jobs[0]["description"], jobs[0]["seniority"]),
                         ("https://boards.greenhouse.io/acme/jobs/9?gh_src=linkedin", "Build things\n\nPython", "mid-senior level"))

    def test_linkedin_page_parsing(self):
        self.assertEqual(jobspy_source.parse_linkedin_page(LI_PAGE)["apply_url"], "https://boards.greenhouse.io/acme/jobs/9?gh_src=linkedin")
        easy = LI_PAGE.replace('<code id="applyUrl"', '<code id="other"')
        self.assertEqual(jobspy_source.parse_linkedin_page(easy)["apply_url"], "", "Easy Apply: no off-site link")
        self.assertEqual(jobspy_source.parse_linkedin_page(""), {"description": "", "apply_url": "", "seniority": ""})
        back = LI_PAGE.replace("https%3A%2F%2Fboards%2Egreenhouse%2Eio", "https%3A%2F%2Fwww%2Elinkedin%2Ecom")
        self.assertEqual(jobspy_source.parse_linkedin_page(back)["apply_url"], "", "a link back to LinkedIn is not a company form")

    def test_a_429_stops_page_reads_and_keeps_the_cards(self):
        pages = []
        def get(url):
            pages.append(url)
            return (429, "") if len(pages) == 2 else (200, LI_PAGE)
        def fake(**kw):
            return FakeFrame([row(job_url=f"https://www.linkedin.com/jobs/view/{kw['location']}-{i}") for i in range(3)])
        jobs = jobspy_source.search("linkedin", {}, ["Dev"], ["A", "B"], scrape_jobs=fake, get=get)
        self.assertEqual(len(jobs), 6)
        self.assertEqual(len(pages), 2, "no page is read after the 429")
        self.assertEqual([bool(j["apply_url"]) for j in jobs], [True, False, False, False, False, False])

    def test_a_failed_page_read_keeps_the_card(self):
        jobs = jobspy_source.search("linkedin", {}, ["Dev"], ["A"], scrape_jobs=lambda **kw: FakeFrame([row()]),
                                    get=lambda url: (_ for _ in ()).throw(OSError("reset")))
        self.assertEqual((len(jobs), jobs[0]["apply_url"], jobs[0]["description"]), (1, "", "Build services."))

    def test_progress_is_reported_per_query(self):
        seen = []
        jobspy_source.search("indeed", {}, ["Dev"], ["A", "B"], on_query=lambda t, l, n: seen.append((t, l, n)),
                             scrape_jobs=lambda **kw: (_ for _ in ()).throw(RuntimeError("x")) if kw["location"].startswith("B")
                             else FakeFrame([row(job_url="https://in.indeed.com/viewjob?jk=1")]))
        self.assertEqual(seen, [("Dev", "A", 1), ("Dev", "B", 0)])

    def test_indeed_does_not_fetch_pages_one_by_one_and_follows_the_profile_country(self):
        calls = []
        def fake(**kw):
            calls.append(kw); return FakeFrame([])
        jobspy_source.search("indeed", {"_flat": {"location_country": "United States"}}, ["Dev"], ["Austin"], scrape_jobs=fake)
        self.assertFalse(calls[0]["fetch_description"])
        self.assertEqual(calls[0]["country_indeed"], "usa")
        jobspy_source.search("indeed", {"_flat": {"location_country": "Atlantis"}}, ["Dev"], ["X"], scrape_jobs=fake)
        self.assertEqual(calls[1]["country_indeed"], "india")

    def test_a_failing_query_is_skipped_and_the_same_posting_twice_is_kept_once(self):
        def fake(**kw):
            if kw["location"].startswith("Pune"):
                raise RuntimeError("429 too many requests")
            return FakeFrame([row(job_url="https://www.linkedin.com/jobs/view/1"), row(job_url="https://www.linkedin.com/jobs/view/1")])
        jobs = jobspy_source.search("linkedin", {}, ["Dev"], ["Pune", "Remote"], scrape_jobs=fake)
        self.assertEqual([j["url"] for j in jobs], ["https://www.linkedin.com/jobs/view/1"])

    def test_where_to_search_names_the_country(self):
        q = jobspy_source.query_location
        self.assertEqual(q("Delhi", "India"), ("Delhi, India", False))              # not Delhi, Michigan
        self.assertEqual(q("Pune, Maharashtra", "India"), ("Pune, Maharashtra, India", False))
        self.assertEqual(q("remote", "India"), ("India", True))
        for kept in ("Berlin, Germany", "Austin, TX", "India", "USA"):
            self.assertEqual(q(kept, "India"), (kept, False), kept)

    def test_which_country_a_result_is_in(self):
        c = jobspy_source.location_country
        self.assertEqual([c(x) for x in ("Ann Arbor, MI, USA", "Lansing, MI", "Bengaluru, Karnataka, India",
                                          "London, England, United Kingdom", "United States (Remote)", "Bengaluru", "Remote", "")],
                         ["usa", "usa", "india", "uk", "usa", None, None, None])

    def test_results_in_another_country_are_dropped_unless_remote_or_asked_for(self):
        rows = [row(job_url="https://www.linkedin.com/jobs/view/1", location="Delhi, MI, USA"),
                row(job_url="https://www.linkedin.com/jobs/view/2", location="New Delhi, Delhi, India"),
                row(job_url="https://www.linkedin.com/jobs/view/3", location="United States (Remote)"),
                row(job_url="https://www.linkedin.com/jobs/view/4", location="Gurugram"),
                row(job_url="https://www.linkedin.com/jobs/view/5", location="Munich, Bavaria, Germany")]
        jobs = jobspy_source.search("linkedin", {"_flat": {"location_country": "India"}}, ["Dev"], ["Delhi"],
                                    scrape_jobs=lambda **kw: FakeFrame(rows), get=lambda url: (404, ""))
        self.assertEqual([j["url"][-1] for j in jobs], ["2", "3", "4"])
        jobs = jobspy_source.search("linkedin", {"_flat": {"location_country": "India"}}, ["Dev"], ["Delhi", "Berlin, Germany"],
                                    scrape_jobs=lambda **kw: FakeFrame(rows), get=lambda url: (404, ""))
        self.assertEqual(sorted({j["url"][-1] for j in jobs}), ["2", "3", "4", "5"])

    def test_an_unknown_board_is_refused(self):
        with self.assertRaises(ValueError):
            jobspy_source.search("glassdoor", {}, ["Dev"], ["X"], scrape_jobs=lambda **kw: FakeFrame([]))


class EngineSettingTests(unittest.TestCase):

    def test_valid_values_are_normalised_and_bad_ones_refused(self):
        self.assertEqual(engines.clean_engine(" Browser "), "browser")
        self.assertEqual(engines.clean_engine("jobspy"), "jobspy")
        self.assertEqual(engines.clean_engine("apify"), "apify")
        for bad in ("", None, "selenium"):
            with self.assertRaises(ValueError):
                engines.clean_engine(bad)
        self.assertEqual(engines.clean_results_per_query("40"), "40")
        self.assertEqual(engines.clean_hours_old("0"), "0")
        for fn, bad in ((engines.clean_results_per_query, "0"), (engines.clean_results_per_query, "201"),
                        (engines.clean_results_per_query, "ten"), (engines.clean_hours_old, "-1"), (engines.clean_hours_old, "x")):
            with self.assertRaises(ValueError):
                fn(bad)

    def test_engine_for_reads_the_boards_key_and_falls_back_to_the_default(self):
        store = {"search_engine_naukri": "browser", "search_engine_indeed": "garbage"}
        read = lambda k, d="": store.get(k, d)
        self.assertEqual(engines.engine_for("naukri", read), "browser")
        self.assertEqual(engines.engine_for("indeed", read), "jobspy")
        self.assertEqual(engines.engine_for("linkedin", read), "jobspy")


class PipelineBase(unittest.TestCase):
    """Real throwaway databases; the search engines, the scorer and Playwright are fakes."""

    def setUp(self):
        from core.config import cfg
        from db.settings_db import SettingsDB
        from db.profile_db import ProfileDB
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self._saved_settings = SettingsDB._instance
        SettingsDB._instance = SettingsDB(path=d / "settings.db")
        self.addCleanup(lambda: setattr(SettingsDB, "_instance", self._saved_settings))
        for p in (mock.patch.object(cfg, "DATA_DIR", d), mock.patch.object(cfg, "JOBS_DB_PATH", d / "jobs.db"),
                  mock.patch.object(cfg, "PROFILE_DB_PATH", d / "profile.db"),
                  mock.patch("tracking.pipeline.time.sleep", lambda s: None),
                  mock.patch("urllib.request.urlopen", side_effect=AssertionError("a test touched the network")),
                  mock.patch("intelligence.scorer.score_jobs_batch", lambda jobs, profile, **kw: []),
                  mock.patch("intelligence.briefer.generate_and_save", lambda *a, **kw: None),
                  mock.patch("playwright.sync_api.sync_playwright", side_effect=AssertionError("a browser started")),
                  mock.patch("requests.request", side_effect=AssertionError("a test touched the network"))):
            p.start()
            self.addCleanup(p.stop)
        ProfileDB().set_many({"search_positions": '["Backend Engineer"]', "search_locations": '["Bangalore"]'})
        self.settings = SettingsDB.get()

    def run_pipeline(self, platforms):
        import tracking.pipeline
        return tracking.pipeline.run(platforms=platforms, sources=[])      # the login-free sources have their own tests


class PipelineEngineTests(PipelineBase):

    def test_jobspy_boards_are_searched_without_a_browser_and_the_apply_form_is_stored(self):
        from db.jobs_db import JobsDB
        def fake_search(site, profile, titles, locations, **kw):
            return [{"title": "Backend Engineer", "company": "Acme", "location": "Bengaluru",
                     "url": f"https://www.{site}.com/jobs/view/1", "apply_url": "https://boards.greenhouse.io/acme/jobs/9",
                     "source": site, "description": "Python.", "salary": "", "seniority": "", "applicants": "",
                     "posted_at": "2026-10-27"}]
        with mock.patch("scraper.jobspy_source.search", side_effect=fake_search) as search:
            stats = self.run_pipeline(["linkedin", "indeed"])
        self.assertEqual([c.args[0] for c in search.call_args_list], ["linkedin", "indeed"])
        self.assertEqual({k: v for k, v in search.call_args_list[0].kwargs.items() if k != "on_query"},
                         {"results_per_query": 25, "hours_old": 168})
        self.assertEqual(stats["total_found"], 2)
        apps = JobsDB(user_id=1).list_applications()
        self.assertEqual(stats["new_added"], 1, "the same company form seen on two boards is one application")
        self.assertEqual((apps[0]["apply_url"], apps[0]["posted_at"]), ("https://boards.greenhouse.io/acme/jobs/9", "2026-10-27"))

    def test_the_search_knobs_reach_jobspy(self):
        self.settings.write("search_results_per_query", "40")
        self.settings.write("search_hours_old", "24")
        with mock.patch("scraper.jobspy_source.search", return_value=[]) as search:
            self.run_pipeline(["naukri"])
        self.assertEqual({k: v for k, v in search.call_args.kwargs.items() if k != "on_query"},
                         {"results_per_query": 40, "hours_old": 24})

    def test_a_board_set_to_browser_uses_the_saved_login_scraper_with_a_browser(self):
        import scraper.naukri
        self.settings.write("search_engine_naukri", "browser")
        seen = {}
        class PW:
            def __enter__(self): return "pw-handle"
            def __exit__(self, *a): return False
        def browser_scrape(pw, profile, titles, locations):
            seen["pw"], seen["titles"] = pw, titles
            return []
        with mock.patch("playwright.sync_api.sync_playwright", lambda: PW()), \
             mock.patch.object(scraper.naukri, "scrape", browser_scrape), \
             mock.patch("scraper.jobspy_source.search", side_effect=AssertionError("jobspy ran for a browser board")):
            self.run_pipeline(["naukri"])
        self.assertEqual((seen["pw"], seen["titles"]), ("pw-handle", ["Backend Engineer"]))

    def test_one_failing_board_does_not_stop_the_others(self):
        def fake_search(site, *a, **kw):
            if site == "linkedin":
                raise RuntimeError("blocked")
            return [{"title": "Dev", "company": "Beta", "url": "https://in.indeed.com/viewjob?jk=1", "source": site}]
        with mock.patch("scraper.jobspy_source.search", side_effect=fake_search):
            stats = self.run_pipeline(["linkedin", "indeed"])
        self.assertEqual((stats["total_found"], stats["new_added"]), (1, 1))


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

    def test_the_search_settings_are_validated_and_saved(self):
        r = self.client.post("/api/settings", json={"search_engine_linkedin": "Browser", "search_results_per_query": 50,
                                                    "search_hours_old": "72"})
        self.assertEqual(r.status_code, 200, r.text)
        s = self.client.get("/api/settings").json()["settings"]
        self.assertEqual((s["search_engine_linkedin"], s["search_results_per_query"], s["search_hours_old"]), ("browser", "50", "72"))
        self.assertEqual(s["search_engine_indeed"], "jobspy")
        for body, text in (({"search_engine_indeed": "selenium"}, "search engine must be one of"),
                           ({"search_results_per_query": 0}, "1 to 200"),
                           ({"search_hours_old": "-5"}, "0 to 8760")):
            r = self.client.post("/api/settings", json=body)
            self.assertEqual(r.status_code, 400, body)
            self.assertIn(text, r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
