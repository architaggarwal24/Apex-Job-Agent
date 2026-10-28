# tests/test_apply_target.py
"""
tests/test_apply_target.py — a job found on a board but applied on the
company's own form (jobs.apply_url, applier/target.py):

  * JobsDB stores apply_url / posted_at, shows them on applications, and
    refuses a second job for the same form (seen through another source)
  * applier.target: the page to apply on is the form when there is one
  * the Orchestrator, the preflight "still open" check, the site-type choice
    and the runner task all use the form, never the board card

Real throwaway databases; no network, no browser, no model.

Run from the project root:   python3 -m unittest tests.test_apply_target -v
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

from applier import target
from db.jobs_db import JobsDB

LISTING = "https://www.linkedin.com/jobs/view/123"
FORM = "https://boards.greenhouse.io/acme/jobs/9"


class TargetHelperTests(unittest.TestCase):

    def test_the_form_wins_over_the_listing(self):
        self.assertEqual(target.apply_url({"url": LISTING, "apply_url": FORM}), FORM)
        self.assertEqual(target.apply_url({"url": LISTING, "apply_url": ""}), LISTING)
        self.assertEqual(target.apply_url({"url": LISTING}), LISTING)
        self.assertEqual(target.apply_url(None), "")

    def test_with_apply_target_swaps_url_and_keeps_the_listing(self):
        job = {"job_id": "a", "url": LISTING, "apply_url": FORM}
        out = target.with_apply_target(job)
        self.assertEqual((out["url"], out["listing_url"]), (FORM, LISTING))
        self.assertEqual(job["url"], LISTING, "the caller's dict is untouched")
        plain = {"job_id": "b", "url": LISTING}
        self.assertIs(target.with_apply_target(plain), plain)
        self.assertIsNone(target.with_apply_target(None))


def _own_settings(test, d):
    """A throwaway settings.db for the duration of a test (stale thresholds read it)."""
    from db.settings_db import SettingsDB
    saved = SettingsDB._instance
    SettingsDB._instance = SettingsDB(path=d / "settings.db")
    test.addCleanup(lambda: setattr(SettingsDB, "_instance", saved))


class DbTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        _own_settings(self, Path(self.tmp.name))
        self.db = JobsDB(path=Path(self.tmp.name) / "jobs.db", user_id=1)
        self.bob = JobsDB(path=Path(self.tmp.name) / "jobs.db", user_id=2)

    def job(self, **kw):
        base = {"url": LISTING, "apply_url": FORM, "title": "Backend Engineer", "company": "Acme",
                "source": "linkedin", "description": "Python.", "posted_at": "2026-10-27T10:00:00"}
        base.update(kw)
        return base

    def test_the_form_and_posted_date_are_stored_and_shown(self):
        jid = self.db.add(self.job())
        row = self.db.get_by_id(jid)
        self.assertEqual((row["url"], row["apply_url"], row["posted_at"]), (LISTING, FORM, "2026-10-27"))
        app = self.db.get_application(jid)
        self.assertEqual((app["apply_url"], app["posted_at"]), (FORM, "2026-10-27"))
        self.assertEqual(self.db.list_applications()[0]["apply_url"], FORM)

    def test_a_form_equal_to_the_listing_is_not_kept_twice(self):
        jid = self.db.add(self.job(url=FORM, apply_url=FORM, source="greenhouse"))
        self.assertEqual(self.db.get_by_id(jid)["apply_url"], "")

    def test_the_same_form_through_another_source_is_one_application(self):
        self.assertIsNotNone(self.db.add(self.job()))
        self.assertIsNone(self.db.add(self.job(url="https://in.indeed.com/viewjob?jk=77", source="indeed",
                                               title="Backend Engineer (Platform)")))
        self.assertIsNone(self.db.add(self.job(url=FORM, apply_url="", source="greenhouse", title="Backend Eng")),
                          "the form itself, found later on the company's board")
        self.assertIsNotNone(self.db.add(self.job(url="https://in.indeed.com/viewjob?jk=78", source="indeed",
                                                  apply_url="https://boards.greenhouse.io/acme/jobs/10", title="SRE")))

    def test_another_users_job_on_the_same_form_does_not_block(self):
        self.assertIsNotNone(self.db.add(self.job()))
        self.assertIsNotNone(self.bob.add(self.job()))


class ApplierUsesTheFormTests(unittest.TestCase):

    def setUp(self):
        from core.config import cfg
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        for p in (mock.patch.object(cfg, "DATA_DIR", d), mock.patch.object(cfg, "JOBS_DB_PATH", d / "jobs.db"),
                  mock.patch.object(cfg, "PROFILE_DB_PATH", d / "profile.db"),
                  mock.patch.object(cfg, "SESSIONS_DIR", d / "sessions"),
                  mock.patch("urllib.request.urlopen", side_effect=AssertionError("a test touched the network"))):
            p.start()
            self.addCleanup(p.stop)
        (d / "sessions").mkdir()
        _own_settings(self, d)
        self.db = JobsDB(user_id=1)
        self.jid = self.db.add({"url": LISTING, "apply_url": FORM, "title": "Backend Engineer", "company": "Acme",
                                "source": "linkedin", "description": "Python."})
        self.job = self.db.get_by_id(self.jid)

    def test_the_orchestrator_opens_the_form_and_remembers_the_listing(self):
        from applier.session import Orchestrator
        o = Orchestrator(self.job)
        self.assertEqual((o.job["url"], o.job["listing_url"], o.session.job_url), (FORM, LISTING, FORM))

    def test_preflight_checks_that_the_form_is_still_open(self):
        from applier import preflight
        calls = []
        def fetch(url):
            calls.append(url); return 200, "<html>Apply now</html>"
        class Settings:
            def read(self, k, d=""): return d
        self.assertTrue(preflight.run(self.job, self.db, Settings(), fetch).ok)
        self.assertEqual(calls, [FORM])

    def test_the_site_type_comes_from_the_form(self):
        from applier import autonomy
        self.assertEqual(autonomy.site_type_for(self.job), "greenhouse")
        self.assertEqual(autonomy.site_type_for({"url": LISTING}), "linkedin_easy_apply")

    def test_a_runner_task_sends_the_form(self):
        from applier.runner_hub import RunnerHub
        from db.runner_db import RunnerTokenStore
        self.db.transition(self.jid, "preparing")
        tok = RunnerTokenStore(1, Path(self.tmp.name) / "jobs.db").create("laptop", now=1000.0)
        hub = RunnerHub(1, Path(self.tmp.name) / "jobs.db", clock=lambda: 1000.0, waiter=lambda s: None)
        hub.enqueue(self.db.get_by_id(self.jid), "apply", {})
        task = hub.claim(tok["id"], 0)
        self.assertEqual((task["application"]["url"], task["application"]["site_type"]), (FORM, "greenhouse"))


if __name__ == "__main__":
    unittest.main()
