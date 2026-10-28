# tests/test_sources.py
"""
tests/test_sources.py — the login-free job sources (scraper/sources):

  * base: HTML to text, dates, salary, the job dict, title/location matching
  * every aggregator and every ATS vendor against a canned payload: the right
    URL is asked, the listing url vs the apply form, filtering by title/location
  * ats.parse_url + harvest: company boards learnt from job links
  * SourceStore: companies (add/dedupe/enable/remove/limit), run log
  * sources.run: enabled set, throttling, one bad source doesn't end the run,
    harvest between the aggregators and the boards
  * the pipeline reads the sources and stores their jobs
  * /api/sources and /api/sources/companies (reads any user, writes owner)

No network: `FakeFetch` answers from a dict of URL -> payload and records what
was asked. No model: the scorer and briefer are fakes.

Run from the project root:   python3 -m unittest tests.test_sources -v
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
os.environ.setdefault("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())

from db.sources_db import SourceStore, pretty_name
from scraper import sources
from scraper.sources import aggregators, ats, harvest
from scraper.sources.base import (SourceError, html_to_text, iso_date, job, location_matches, salary_text,
                                  title_matches)

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except Exception:
    HAVE_FASTAPI = False

TITLES = ["Backend Engineer", "AI Engineer"]
LOCATIONS = ["Bangalore", "Remote"]


class FakeFetch:
    """URL (without query) -> payload; a payload that is an Exception is raised.
    Every call is recorded as (url, params)."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def _answer(self, url, params):
        self.calls.append((url, params or {}))
        if url not in self.routes:
            raise SourceError(f"HTTP 404 from {url}")
        v = self.routes[url]
        if isinstance(v, Exception):
            raise v
        return v

    def get_json(self, url, params=None, headers=None):
        return self._answer(url, params)

    def get_text(self, url, params=None, headers=None):
        return self._answer(url, params)

    def post_json(self, url, body, headers=None):
        return self._answer(url, body)


def ctx(fetch, titles=TITLES, locations=LOCATIONS, names=None):
    c = sources.Context(titles=titles, locations=locations, fetch=fetch)
    c.names = names or {}
    return c


class BaseHelperTests(unittest.TestCase):

    def test_html_to_text_handles_tags_entities_and_escaped_html(self):
        self.assertEqual(html_to_text("<p>Hello &amp; <b>bye</b></p><li>x</li>"), "Hello & bye\n\nx")
        self.assertEqual(html_to_text("&lt;p&gt;Escaped &amp;amp; markup&lt;/p&gt;"), "Escaped & markup")
        self.assertEqual(html_to_text(""), "")

    def test_dates_and_salaries(self):
        self.assertEqual(iso_date("2026-10-27T10:00:00Z"), "2026-10-27")
        self.assertEqual(iso_date(1793059200000), "2026-10-27")
        self.assertEqual(iso_date("Tue, 27 Oct 2026 10:00:00 +0000"), "2026-10-27")
        self.assertEqual(iso_date("soon"), "")
        self.assertEqual(salary_text(1200000, 1800000, "INR", "year"), "INR 1,200,000-1,800,000 /year")
        self.assertEqual(salary_text(None, None), "")
        self.assertEqual(salary_text(0, 0, "USD"), "")

    def test_the_job_dict_needs_a_title_company_and_http_url(self):
        self.assertIsNone(job(source="x", title="Dev", company="", url="https://a.b/c"))
        self.assertIsNone(job(source="x", title="Dev", company="Acme", url="javascript:void(0)"))
        j = job(source="x", title=" Dev ", company="Acme", url="https://a.b/c", apply_url="https://a.b/c",
                description="<p>Hi</p>", posted_at="2026-10-01T00:00:00")
        self.assertEqual((j["title"], j["apply_url"], j["description"], j["posted_at"]), ("Dev", "", "Hi", "2026-10-01"))

    def test_title_matching(self):
        self.assertTrue(title_matches("Senior Backend Engineer (Python)", TITLES))
        self.assertTrue(title_matches("AI Engineer, Platform", TITLES))
        self.assertFalse(title_matches("Backend Support Specialist", TITLES))
        self.assertFalse(title_matches("Machine Learning Engineer", TITLES))
        self.assertFalse(title_matches("", TITLES))

    def test_location_matching(self):
        self.assertTrue(location_matches("Bengaluru, India", LOCATIONS))
        self.assertTrue(location_matches("Remote - APAC", LOCATIONS))
        self.assertTrue(location_matches("Berlin", LOCATIONS, is_remote=True))
        self.assertTrue(location_matches("", LOCATIONS), "an unknown location is left to the score")
        self.assertFalse(location_matches("Berlin, Germany", LOCATIONS))
        self.assertFalse(location_matches("Remote", ["Bangalore"]), "remote jobs only when Remote is wanted")


# ── aggregators ───────────────────────────────────────────────────

class AggregatorTests(unittest.TestCase):

    def test_himalayas_searches_each_title_and_keeps_the_apply_link(self):
        f = FakeFetch({"https://himalayas.app/jobs/api/search": {"jobs": [
            {"title": "Backend Engineer", "companyName": "Acme", "url": "https://himalayas.app/companies/acme/jobs/be",
             "applicationLink": "https://boards.greenhouse.io/acme/jobs/9", "locationRestrictions": ["India", "Worldwide"],
             "description": "<p>Go.</p>", "minSalary": 100000, "maxSalary": 150000, "currency": "USD", "pubDate": "2026-10-20T00:00:00Z"},
            {"title": "No link", "companyName": "", "applicationLink": ""}]}})
        jobs = aggregators.himalayas(ctx(f))
        self.assertEqual([c[1]["q"] for c in f.calls], TITLES)
        self.assertEqual(len(jobs), 1)
        j = jobs[0]
        self.assertEqual((j["source"], j["url"], j["apply_url"], j["location"], j["salary"], j["posted_at"]),
                         ("himalayas", "https://himalayas.app/companies/acme/jobs/be", "https://boards.greenhouse.io/acme/jobs/9",
                          "India, Worldwide", "USD 100,000-150,000", "2026-10-20"))

    def test_jobicy_and_remotive_search_by_title(self):
        f = FakeFetch({"https://jobicy.com/api/v2/remote-jobs": {"jobs": [
                           {"url": "https://jobicy.com/jobs/1", "jobTitle": "Backend Engineer", "companyName": "Beta",
                            "jobGeo": "Anywhere", "jobDescription": "Rust", "pubDate": "2026-10-21 10:00:00"}]},
                       "https://remotive.com/api/remote-jobs": {"jobs": [
                           {"url": "https://remotive.com/remote-jobs/1", "title": "AI Engineer", "company_name": "Gamma",
                            "candidate_required_location": "India", "description": "LLMs", "publication_date": "2026-10-22T08:00:00"}]}})
        self.assertEqual([j["company"] for j in aggregators.jobicy(ctx(f))], ["Beta"])
        self.assertEqual(f.calls[0][1]["tag"], "backend engineer")
        self.assertEqual([j["company"] for j in aggregators.remotive(ctx(f))], ["Gamma"])
        self.assertEqual(f.calls[-1][1]["search"], "AI Engineer")

    def test_remoteok_skips_the_legal_notice_and_filters_by_title(self):
        f = FakeFetch({"https://remoteok.com/api": [
            {"legal": "Terms"},
            {"url": "https://remoteok.com/remote-jobs/1", "apply_url": "https://remoteok.com/l/1", "company": "Delta",
             "position": "Backend Engineer", "location": "Worldwide", "description": "Go", "date": "2026-10-23T00:00:00+00:00"},
            {"url": "https://remoteok.com/remote-jobs/2", "company": "Delta", "position": "Sales Lead", "location": "US"}]})
        jobs = aggregators.remoteok(ctx(f))
        self.assertEqual([j["title"] for j in jobs], ["Backend Engineer"])
        self.assertEqual(jobs[0]["apply_url"], "", "a bounce through remoteok is not the form")

    def test_wwr_parses_the_rss_feeds(self):
        rss = """<?xml version="1.0"?><rss><channel>
          <item><title>Epsilon: Backend Engineer</title><link>https://weworkremotely.com/remote-jobs/e-be</link>
                <description><![CDATA[<p>Build APIs</p>]]></description><pubDate>Tue, 27 Oct 2026 10:00:00 +0000</pubDate><region>Anywhere in the World</region></item>
          <item><title>Zeta: Office Manager</title><link>https://weworkremotely.com/remote-jobs/z-om</link><description>x</description></item>
        </channel></rss>"""
        f = FakeFetch({u: rss for u in aggregators.WWR_FEEDS})
        jobs = aggregators.wwr(ctx(f))
        self.assertEqual([(j["company"], j["title"], j["posted_at"]) for j in jobs], [("Epsilon", "Backend Engineer", "2026-10-27")])
        self.assertEqual(len(f.calls), len(aggregators.WWR_FEEDS))

    def test_wwr_bad_feed_is_a_source_error(self):
        f = FakeFetch({u: "<not xml" for u in aggregators.WWR_FEEDS})
        with self.assertRaises(SourceError):
            aggregators.wwr(ctx(f))

    def test_hn_hiring_reads_the_months_thread_and_takes_the_first_link(self):
        f = FakeFetch({"https://hn.algolia.com/api/v1/search": {"hits": [{"objectID": "500", "title": "Ask HN: Who is hiring? (October 2026)"}]},
                       "https://hn.algolia.com/api/v1/search_by_date": {"hits": [
                           {"objectID": "501", "parent_id": "500", "created_at": "2026-10-02T12:00:00Z",
                            "comment_text": "Acme Robotics | Backend Engineer | REMOTE (India ok) | Full-time<p>Apply: <a href=\"https://jobs.lever.co/acme/123\">https://jobs.lever.co/acme/123</a></p>"},
                           {"objectID": "502", "parent_id": "500", "created_at": "2026-10-02T12:00:00Z",
                            "comment_text": "Beta Co | Sales Director | New York | Onsite"},
                           {"objectID": "503", "parent_id": "501", "comment_text": "Backend Engineer here | REMOTE | applied, thanks!"}]}})
        jobs = aggregators.hn_hiring(ctx(f))
        self.assertEqual(len(jobs), 1)
        j = jobs[0]
        self.assertEqual((j["company"], j["title"], j["url"], j["apply_url"], j["location"]),
                         ("Acme Robotics", "Backend Engineer", "https://news.ycombinator.com/item?id=501", "https://jobs.lever.co/acme/123", "Remote"))

    def test_arbeitnow_pages_until_empty_and_filters(self):
        f = FakeFetch({"https://www.arbeitnow.com/api/job-board-api": {"data": [
            {"url": "https://www.arbeitnow.com/jobs/1", "company_name": "Eta", "title": "Backend Engineer", "remote": True,
             "location": "Berlin", "description": "x", "created_at": 1793059200},
            {"url": "https://www.arbeitnow.com/jobs/2", "company_name": "Eta", "title": "Backend Engineer", "remote": False, "location": "Berlin"}]}})
        jobs = aggregators.arbeitnow(ctx(f))
        self.assertEqual([j["url"] for j in jobs], ["https://www.arbeitnow.com/jobs/1"])
        self.assertEqual(jobs[0]["posted_at"], "2026-10-27")
        self.assertEqual(len(f.calls), 3, "three pages are read when none comes back empty")


# ── ATS vendors ───────────────────────────────────────────────────

class AtsTests(unittest.TestCase):

    def test_parse_url_knows_every_vendor_and_nothing_else(self):
        cases = {"https://boards.greenhouse.io/acme/jobs/1": ("greenhouse", "acme"),
                 "https://job-boards.greenhouse.io/acme": ("greenhouse", "acme"),
                 "https://boards.greenhouse.io/embed/job_app?for=acme": None,
                 "https://jobs.lever.co/acme/abc-def": ("lever", "acme"),
                 "https://jobs.eu.lever.co/acme/abc": ("lever", "acme"),
                 "https://jobs.ashbyhq.com/Acme/123": ("ashby", "Acme"),
                 "https://jobs.smartrecruiters.com/AcmeInc/743": ("smartrecruiters", "AcmeInc"),
                 "https://apply.workable.com/acme/j/ABC/": ("workable", "acme"),
                 "https://apply.workable.com/api/v1/x": None,
                 "https://acme.recruitee.com/o/dev": ("recruitee", "acme"),
                 "https://acme.teamtailor.com/jobs/1-dev": ("teamtailor", "acme"),
                 "https://acme.jobs.personio.de/job/1": ("personio", "acme"),
                 "https://acme.breezy.hr/p/1-dev": ("breezy", "acme"),
                 "https://acme.pinpointhq.com/postings/1": ("pinpoint", "acme"),
                 "https://acme.bamboohr.com/careers/12": ("bamboohr", "acme"),
                 "https://acme.bamboohr.com/login": None,
                 "https://jobs.polymer.co/acme/jobs/1": ("polymer", "acme"),
                 "https://www.linkedin.com/jobs/view/1": None, "": None, "not a url": None,
                 "https://a.b.teamtailor.com/jobs": None}
        for url, want in cases.items():
            self.assertEqual(ats.parse_url(url), want, url)

    def test_greenhouse(self):
        f = FakeFetch({"https://boards-api.greenhouse.io/v1/boards/acme/jobs": {"jobs": [
            {"id": 1, "title": "Backend Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1",
             "location": {"name": "Bengaluru"}, "content": "&lt;p&gt;Go &amp;amp; Python&lt;/p&gt;", "updated_at": "2026-10-20T10:00:00-04:00"},
            {"id": 2, "title": "Backend Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/2", "location": {"name": "Berlin"}},
            {"id": 3, "title": "Recruiter", "absolute_url": "https://boards.greenhouse.io/acme/jobs/3", "location": {"name": "Bengaluru"}}]}})
        jobs = ats.greenhouse("acme", ctx(f, names={("greenhouse", "acme"): "Acme Robotics"}))
        self.assertEqual(f.calls[0][1], {"content": "true"})
        self.assertEqual([(j["url"], j["company"], j["description"], j["posted_at"]) for j in jobs],
                         [("https://boards.greenhouse.io/acme/jobs/1", "Acme Robotics", "Go & Python", "2026-10-20")])

    def test_lever_json_and_the_html_answer(self):
        f = FakeFetch({"https://api.lever.co/v0/postings/acme": [
            {"id": "a", "text": "AI Engineer", "categories": {"location": "Remote"}, "workplaceType": "remote",
             "descriptionPlain": "LLMs", "hostedUrl": "https://jobs.lever.co/acme/a", "applyUrl": "https://jobs.lever.co/acme/a/apply",
             "createdAt": 1793059200000, "salaryRange": {"min": 50, "max": 70, "currency": "USD", "interval": "per-year-salary"}}]})
        jobs = ats.lever("acme", ctx(f))
        self.assertEqual((jobs[0]["apply_url"], jobs[0]["posted_at"], jobs[0]["salary"], jobs[0]["company"]),
                         ("https://jobs.lever.co/acme/a/apply", "2026-10-27", "USD 50-70 /per-year-salary", "Acme"))
        f2 = FakeFetch({"https://api.lever.co/v0/postings/acme": {"html": "<!doctype html>"}})
        with self.assertRaises(SourceError):
            ats.lever("acme", ctx(f2))

    def test_ashby_skips_unlisted(self):
        f = FakeFetch({"https://api.ashbyhq.com/posting-api/job-board/acme": {"jobs": [
            {"title": "Backend Engineer", "jobUrl": "https://jobs.ashbyhq.com/acme/1", "applyUrl": "https://jobs.ashbyhq.com/acme/1/application",
             "location": "Bangalore", "isRemote": False, "descriptionPlain": "x", "publishedAt": "2026-10-25T00:00:00Z", "isListed": True},
            {"title": "Backend Engineer", "jobUrl": "https://jobs.ashbyhq.com/acme/2", "location": "Bangalore", "isListed": False}]}})
        self.assertEqual([j["url"] for j in ats.ashby("acme", ctx(f))], ["https://jobs.ashbyhq.com/acme/1"])

    def test_smartrecruiters_lists_then_reads_the_matched_ad(self):
        f = FakeFetch({"https://api.smartrecruiters.com/v1/companies/Acme/postings": {"content": [
                           {"id": "7", "name": "Backend Engineer", "releasedDate": "2026-10-20T00:00:00.000Z",
                            "location": {"city": "Bengaluru", "country": "in", "remote": False}, "company": {"name": "Acme Inc"},
                            "ref": "https://api.smartrecruiters.com/v1/companies/Acme/postings/7"},
                           {"id": "8", "name": "Designer", "location": {"city": "Bengaluru"}, "ref": "https://api.smartrecruiters.com/v1/companies/Acme/postings/8"}]},
                       "https://api.smartrecruiters.com/v1/companies/Acme/postings/7": {"jobAd": {"sections": {"jobDescription": {"text": "<p>Build APIs</p>"}}}}})
        jobs = ats.smartrecruiters("Acme", ctx(f))
        self.assertEqual([(j["url"], j["company"], j["description"]) for j in jobs],
                         [("https://jobs.smartrecruiters.com/Acme/7", "Acme Inc", "Build APIs")])
        self.assertEqual(len(f.calls), 2, "only the matched posting's ad is fetched")

    def test_workable_recruitee_teamtailor(self):
        f = FakeFetch({"https://apply.workable.com/api/v1/widget/accounts/acme": {"name": "Acme", "jobs": [
                           {"title": "Backend Engineer", "url": "https://apply.workable.com/acme/j/1", "application_url": "https://apply.workable.com/acme/j/1/apply",
                            "location": {"location_str": "Bangalore, India", "telecommuting": False}, "full_description": "x", "published_on": "2026-10-10"}]},
                       "https://acme.recruitee.com/api/offers/": {"offers": [
                           {"title": "AI Engineer", "careers_url": "https://acme.recruitee.com/o/ai", "careers_apply_url": "https://acme.recruitee.com/o/ai/c/new",
                            "location": "Remote", "remote": True, "description": "<p>a</p>", "requirements": "<p>b</p>", "published_at": "2026-10-11T00:00:00Z"}]},
                       "https://acme.teamtailor.com/jobs.json": {"items": [
                           {"title": "Backend Engineer", "url": "https://acme.teamtailor.com/jobs/1", "content_html": "<p>x</p>", "date_published": "2026-10-12T00:00:00Z"}]}})
        self.assertEqual(ats.workable("acme", ctx(f))[0]["apply_url"], "https://apply.workable.com/acme/j/1/apply")
        r = ats.recruitee("acme", ctx(f))[0]
        self.assertEqual((r["apply_url"], r["description"]), ("https://acme.recruitee.com/o/ai/c/new", "a\n\nb"))
        self.assertEqual(ats.teamtailor("acme", ctx(f))[0]["location"], "", "no location given: kept for the score")

    def test_personio_xml(self):
        xml = """<?xml version="1.0"?><workzag-jobs><position><id>1</id><name>Backend Engineer</name><office>Remote</office>
                 <createdAt>2026-10-01</createdAt><seniority>senior</seniority>
                 <jobDescriptions><jobDescription><name>Role</name><value><![CDATA[<p>Build</p>]]></value></jobDescription></jobDescriptions></position>
                 <position><id>2</id><name>Office Manager</name><office>Munich</office></position></workzag-jobs>"""
        f = FakeFetch({"https://acme.jobs.personio.de/xml": xml})
        jobs = ats.personio("acme", ctx(f))
        self.assertEqual([(j["url"], j["description"], j["seniority"]) for j in jobs], [("https://acme.jobs.personio.de/job/1", "Build", "senior")])
        with self.assertRaises(SourceError):
            ats.personio("acme", ctx(FakeFetch({"https://acme.jobs.personio.de/xml": "<broken"})))

    def test_breezy_pinpoint_bamboohr_polymer(self):
        f = FakeFetch({"https://acme.breezy.hr/json": [
                           {"id": "b1", "friendly_id": "b1-be", "name": "Backend Engineer", "location": {"name": "Bangalore", "is_remote": False},
                            "published_date": "2026-10-01T00:00:00Z"}],
                       "https://acme.pinpointhq.com/postings.json": {"data": [
                           {"title": "Backend Engineer", "url": "https://acme.pinpointhq.com/postings/1", "application_url": "https://acme.pinpointhq.com/postings/1/applications/new",
                            "locations": [{"name": "Bengaluru"}], "description": "x", "published_at": "2026-10-02"}]},
                       "https://acme.bamboohr.com/careers/list": {"result": [
                           {"id": 12, "jobOpeningName": "AI Engineer", "location": {"city": "Bangalore", "state": "KA"}, "isRemote": False, "datePosted": "2026-10-03"},
                           {"id": 13, "jobOpeningName": "AI Engineer", "location": {"city": "Austin", "state": "TX"}, "isRemote": False}]},
                       "https://acme.bamboohr.com/careers/12/detail": {"result": {"jobOpening": {"description": "<p>Detail</p>"}}},
                       "https://api.polymer.co/v1/hire/organizations/acme/jobs": {"jobs": [
                           {"title": "Backend Engineer", "job_post_url": "https://jobs.polymer.co/acme/jobs/1", "display_location": "Remote",
                            "remoteness_pretty": "Remote", "published_at": "2026-10-04T00:00:00Z"}]}})
        self.assertEqual(ats.breezy("acme", ctx(f))[0]["url"], "https://acme.breezy.hr/p/b1-be")
        self.assertEqual(ats.pinpoint("acme", ctx(f))[0]["apply_url"], "https://acme.pinpointhq.com/postings/1/applications/new")
        b = ats.bamboohr("acme", ctx(f))
        self.assertEqual([(j["url"], j["description"]) for j in b], [("https://acme.bamboohr.com/careers/12", "Detail")])
        self.assertEqual(ats.polymer("acme", ctx(f))[0]["posted_at"], "2026-10-04")

    def test_every_vendor_has_a_search_function(self):
        self.assertEqual(set(ats.SEARCH), set(ats.VENDORS))
        self.assertEqual(set(sources.ATS_LABELS), set(ats.VENDORS))


# ── store + harvest + run ─────────────────────────────────────────

class StoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = [1_000_000.0]
        self.store = SourceStore(1, Path(self.tmp.name) / "jobs.db", clock=lambda: self.now[0])
        self.bob = SourceStore(2, Path(self.tmp.name) / "jobs.db", clock=lambda: self.now[0])


class StoreTests(StoreBase):

    def test_add_list_enable_remove_are_per_user(self):
        row = self.store.add("greenhouse", "acme", "Acme Robotics")
        self.assertEqual((row["vendor"], row["slug"], row["name"], row["origin"], row["enabled"]), ("greenhouse", "acme", "Acme Robotics", "user", True))
        self.assertIsNone(self.store.add("greenhouse", "acme"), "a second add is a no-op")
        self.assertEqual(self.bob.companies(), [])
        self.assertTrue(self.store.set_enabled(row["id"], False))
        self.assertFalse(self.bob.set_enabled(row["id"], True), "not bob's row")
        self.assertEqual(self.store.companies(enabled_only=True), [])
        self.assertFalse(self.bob.remove(row["id"]))
        self.assertTrue(self.store.remove(row["id"]))
        self.assertEqual(self.store.companies(), [])

    def test_bad_input_and_the_limit(self):
        for vendor, slug in (("workday", "acme"), ("lever", "a/b"), ("lever", ""), ("lever", "x" * 90)):
            with self.assertRaises(ValueError):
                self.store.add(vendor, slug)
        with mock.patch("db.sources_db.MAX_COMPANIES", 2):
            self.store.add("lever", "a"); self.store.add("lever", "b")
            with self.assertRaises(ValueError):
                self.store.add("lever", "c")

    def test_a_harvested_row_takes_a_name_later_and_pretty_names_the_slug(self):
        self.store.add("lever", "acme-robotics", "", origin="harvest")
        self.assertEqual(self.store.companies()[0]["name"], "Acme Robotics")
        self.assertIsNone(self.store.add("lever", "acme-robotics", "ACME Robotics Ltd"))
        self.assertEqual(self.store.name_for("lever", "acme-robotics"), "ACME Robotics Ltd")
        self.assertEqual(pretty_name("the_big.co"), "The Big Co")

    def test_run_log(self):
        self.store.record_run("remotive", 5)
        self.now[0] += 60
        self.store.record_run("remotive", 0, "HTTP 429")
        self.assertEqual(self.store.last_run("remotive"), {"source": "remotive", "ran_at": 1_000_060.0, "found": 0, "error": "HTTP 429"})
        self.assertIsNone(self.store.last_run("jobicy"))
        self.assertEqual(self.bob.runs(), {})
        self.store.add("ashby", "acme"); self.store.record_company_run("ashby", "acme", 3, "")
        self.assertEqual(self.store.companies()[0]["last_count"], 3)


class HarvestTests(StoreBase):

    def test_boards_are_learnt_from_apply_links_and_listing_urls(self):
        jobs = [{"url": "https://www.linkedin.com/jobs/view/1", "apply_url": "https://boards.greenhouse.io/acme/jobs/1", "company": "Acme"},
                {"url": "https://jobs.lever.co/beta/abc", "apply_url": "", "company": "Beta"},
                {"url": "https://www.naukri.com/x", "apply_url": "", "company": "Gamma"},
                {"url": "https://www.linkedin.com/jobs/view/2", "apply_url": "https://boards.greenhouse.io/acme/jobs/2", "company": "Acme"}]
        self.assertEqual(harvest.boards_in(jobs), {("greenhouse", "acme"): "Acme", ("lever", "beta"): "Beta"})
        self.assertEqual(harvest.harvest(self.store, jobs), 2)
        self.assertEqual(harvest.harvest(self.store, jobs), 0)
        rows = self.store.companies()
        self.assertEqual([(r["vendor"], r["slug"], r["name"], r["origin"]) for r in rows],
                         [("greenhouse", "acme", "Acme", "harvest"), ("lever", "beta", "Beta", "harvest")])


class RunTests(StoreBase):

    def read_with(self, **values):
        return lambda k, d="": values.get(k, d)

    def gh(self, slug="acme", title="Backend Engineer"):
        return {"jobs": [{"id": 1, "title": title, "absolute_url": f"https://boards.greenhouse.io/{slug}/jobs/1", "location": {"name": "Remote"}}]}

    def test_aggregators_then_harvest_then_boards_in_one_run(self):
        f = FakeFetch({"https://himalayas.app/jobs/api/search": {"jobs": [
                           {"title": "Backend Engineer", "companyName": "Acme", "url": "https://himalayas.app/j/1",
                            "applicationLink": "https://boards.greenhouse.io/acme/jobs/1", "locationRestrictions": []}]},
                       "https://boards-api.greenhouse.io/v1/boards/acme/jobs": self.gh()})
        logs = []
        jobs, report = sources.run(self.store, ["Backend Engineer"], ["Remote"], read=self.read_with(),
                                   fetch=f, only=["himalayas", "greenhouse"], log=logs.append, clock=lambda: self.now[0])
        self.assertEqual([j["source"] for j in jobs], ["himalayas", "greenhouse"])
        self.assertEqual(report["himalayas"]["found"], 1)
        self.assertEqual(report["greenhouse"], {"found": 1, "error": "", "skipped": ""})
        self.assertEqual([(c["vendor"], c["slug"], c["origin"]) for c in self.store.companies()], [("greenhouse", "acme", "harvest")])
        self.assertIn("Learnt 1 new company board(s) from job links", logs)
        self.assertEqual(self.store.last_run("greenhouse")["found"], 1)
        self.assertEqual(self.store.companies()[0]["last_count"], 1)

    def test_the_setting_turns_sources_off(self):
        f = FakeFetch({"https://remoteok.com/api": [{"legal": 1}], "https://jobicy.com/api/v2/remote-jobs": {"jobs": []}})
        read = self.read_with(search_sources='{"remoteok": false, "wwr": false, "hn_hiring": false, "arbeitnow": false, "remotive": false, "himalayas": false}')
        sources.run(self.store, ["Dev"], ["Remote"], read=read, fetch=f, clock=lambda: self.now[0])
        self.assertEqual({c[0] for c in f.calls}, {"https://jobicy.com/api/v2/remote-jobs"})
        self.assertEqual(sources.enabled_sources(read)[:1], ["jobicy"])
        self.assertEqual(sources.enabled_sources(self.read_with(search_sources="not json")), list(sources.SOURCES))

    def test_a_throttled_source_waits_its_turn(self):
        f = FakeFetch({"https://remotive.com/api/remote-jobs": {"jobs": []}})
        kw = dict(read=self.read_with(), fetch=f, only=["remotive"], clock=lambda: self.now[0])
        _, r1 = sources.run(self.store, ["Dev"], ["Remote"], **kw)
        self.now[0] += 3600
        _, r2 = sources.run(self.store, ["Dev"], ["Remote"], **kw)
        self.now[0] += 6 * 3600
        _, r3 = sources.run(self.store, ["Dev"], ["Remote"], **kw)
        self.assertEqual((r1["remotive"]["skipped"], bool(r2["remotive"]["skipped"]), r3["remotive"]["skipped"]), ("", True, ""))
        self.assertEqual(len(f.calls), 2)

    def test_one_failing_source_or_board_does_not_end_the_run(self):
        f = FakeFetch({"https://remoteok.com/api": SourceError("HTTP 503 from remoteok"),
                       "https://www.arbeitnow.com/api/job-board-api": {"data": [{"url": "https://www.arbeitnow.com/jobs/1", "company_name": "Eta",
                                                                                  "title": "Backend Engineer", "remote": True}]},
                       "https://boards-api.greenhouse.io/v1/boards/good/jobs": self.gh("good"),
                       "https://boards-api.greenhouse.io/v1/boards/bad/jobs": RuntimeError("boom")})
        self.store.add("greenhouse", "good"); self.store.add("greenhouse", "bad")
        jobs, report = sources.run(self.store, ["Backend Engineer"], ["Remote"], read=self.read_with(), fetch=f,
                                   only=["remoteok", "arbeitnow", "greenhouse"], clock=lambda: self.now[0])
        self.assertEqual(sorted(j["source"] for j in jobs), ["arbeitnow", "greenhouse"])
        self.assertEqual(report["remoteok"]["error"], "HTTP 503 from remoteok")
        self.assertEqual(report["greenhouse"], {"found": 1, "error": "1 board(s) failed", "skipped": ""})
        bad = next(c for c in self.store.companies() if c["slug"] == "bad")
        self.assertEqual(bad["last_error"], "RuntimeError: boom")
        self.assertEqual(self.store.last_run("remoteok")["error"], "HTTP 503 from remoteok")

    def test_disabled_boards_and_cancellation(self):
        f = FakeFetch({"https://boards-api.greenhouse.io/v1/boards/acme/jobs": self.gh()})
        row = self.store.add("greenhouse", "acme"); self.store.set_enabled(row["id"], False)
        jobs, _ = sources.run(self.store, ["Dev"], ["Remote"], read=self.read_with(), fetch=f, only=["greenhouse"], clock=lambda: self.now[0])
        self.assertEqual((jobs, f.calls), ([], []))
        self.store.set_enabled(row["id"], True)
        jobs, _ = sources.run(self.store, ["Dev"], ["Remote"], read=self.read_with(), fetch=f, only=["greenhouse"],
                              cancelled=lambda: True, clock=lambda: self.now[0])
        self.assertEqual(f.calls, [])

    def test_the_sources_setting_parses_and_dumps(self):
        self.assertEqual(sources.parse_sources_setting('{"remotive": false, "lever": true}'), {"remotive": False, "lever": True})
        self.assertEqual(sources.dump_sources_setting({"wwr": False, "ashby": True}), '{"ashby":true,"wwr":false}')
        for bad in ("[]", "{\"nope\": true}", "{\"remotive\": \"no\"}", "{"):
            with self.assertRaises(ValueError):
                sources.parse_sources_setting(bad)


class PipelineSourcesTests(unittest.TestCase):

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
                  mock.patch("scraper.jobspy_source.search", return_value=[])):
            p.start()
            self.addCleanup(p.stop)
        ProfileDB().set_many({"search_positions": '["Backend Engineer"]', "search_locations": '["Bangalore"]'})

    def test_source_jobs_are_inserted_and_counted(self):
        import tracking.pipeline
        from db.jobs_db import JobsDB
        f = FakeFetch({"https://himalayas.app/jobs/api/search": {"jobs": [
            {"title": "Backend Engineer", "companyName": "Acme", "url": "https://himalayas.app/j/1",
             "applicationLink": "https://boards.greenhouse.io/acme/jobs/1", "locationRestrictions": ["India"]}]},
            "https://boards-api.greenhouse.io/v1/boards/acme/jobs": {"jobs": [
                {"id": 1, "title": "Backend Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/1", "location": {"name": "Remote"}},
                {"id": 2, "title": "Backend Engineer", "absolute_url": "https://boards.greenhouse.io/acme/jobs/2", "location": {"name": "Remote"}}]}})
        stats = tracking.pipeline.run(platforms=[], sources=["himalayas", "greenhouse"], fetch=f, user_id=1)
        self.assertEqual(stats["by_source"], {"himalayas": 1, "greenhouse": 2})
        apps = JobsDB(user_id=1).list_applications()
        self.assertEqual(stats["new_added"], 2, "the Himalayas listing and the board's job 1 are the same form")
        self.assertEqual(sorted(a["source"] for a in apps), ["greenhouse", "himalayas"])
        self.assertEqual(SourceStore(1).companies()[0]["slug"], "acme")

    def test_sources_can_be_switched_off_for_a_run(self):
        import tracking.pipeline
        f = FakeFetch({})
        stats = tracking.pipeline.run(platforms=[], sources=[], fetch=f)
        self.assertEqual((stats["by_source"], f.calls), ({}, []))


@unittest.skipUnless(HAVE_FASTAPI, "fastapi + httpx not installed")
class SourcesHttpTests(unittest.TestCase):

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

    def test_sources_list_reflects_the_setting(self):
        r = self.alice.get("/api/sources")
        self.assertEqual(r.status_code, 200)
        names = {s["name"]: s for s in r.json()["sources"]}
        self.assertEqual(set(names), set(sources.SOURCES))
        self.assertTrue(all(s["enabled"] for s in names.values()))
        self.assertEqual(self.alice.post("/api/settings", json={"search_sources": {"remotive": False}}).status_code, 200)
        names = {s["name"]: s for s in self.alice.get("/api/sources").json()["sources"]}
        self.assertFalse(names["remotive"]["enabled"])
        self.assertEqual(self.alice.post("/api/settings", json={"search_sources": {"nope": True}}).status_code, 400)
        self.assertEqual(self.alice.post("/api/settings", json={"search_sources": "[1]"}).status_code, 400)
        self.assertEqual(self.alice.get("/api/settings").json()["settings"]["search_sources"], '{"remotive":false}')

    def test_companies_are_owner_written_and_per_user(self):
        r = self.alice.post("/api/sources/companies", json={"url": "https://boards.greenhouse.io/acme/jobs/1", "name": "Acme"})
        self.assertEqual(r.status_code, 201, r.text)
        row = r.json()
        self.assertEqual((row["vendor"], row["slug"], row["name"], row["origin"]), ("greenhouse", "acme", "Acme", "user"))
        self.assertEqual(self.alice.post("/api/sources/companies", json={"vendor": "greenhouse", "slug": "acme"}).status_code, 409)
        self.assertEqual(self.alice.post("/api/sources/companies", json={"url": "https://www.linkedin.com/jobs/view/1"}).status_code, 400)
        self.assertEqual(self.alice.post("/api/sources/companies", json={"vendor": "workday", "slug": "x"}).status_code, 400)
        self.assertEqual(self.alice.post("/api/sources/companies", json={}).status_code, 400)
        self.assertEqual(self.bob.post("/api/sources/companies", json={"vendor": "lever", "slug": "beta"}).status_code, 403)
        self.assertEqual(self.bob.get("/api/sources/companies").json()["companies"], [])
        self.assertEqual(self.alice.put(f"/api/sources/companies/{row['id']}", json={"enabled": False}).status_code, 200)
        self.assertEqual(self.alice.put(f"/api/sources/companies/{row['id']}", json={"enabled": "no"}).status_code, 400)
        self.assertFalse(self.alice.get("/api/sources/companies").json()["companies"][0]["enabled"])
        self.assertEqual({s["name"]: s["boards"] for s in self.alice.get("/api/sources").json()["sources"]}["greenhouse"], 0)
        self.assertEqual(self.bob.delete(f"/api/sources/companies/{row['id']}").status_code, 403)
        self.assertEqual(self.alice.delete(f"/api/sources/companies/{row['id']}").status_code, 200)
        self.assertEqual(self.alice.delete(f"/api/sources/companies/{row['id']}").status_code, 404)

    def test_pipeline_run_validates_sources(self):
        with mock.patch("tracking.pipeline.run", lambda **kw: {"total_found": 0}):
            self.assertEqual(self.alice.post("/api/pipeline/run", json={"quick": True, "sources": ["himalayas"]}).status_code, 200)
            self.assertEqual(self.alice.post("/api/pipeline/run", json={"sources": "himalayas"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
