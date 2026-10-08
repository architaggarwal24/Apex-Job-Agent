# tests/test_preflight.py
"""
tests/test_preflight.py — the checks that run before any browser work
(applier/preflight.py): already applied, blacklisted company, salary below the
floor, job no longer open; plus the salary parser and the blacklist matcher.

Run from the project root:   python3 -m unittest discover -s tests -v

Real throwaway SQLite files; the page fetch is always a fake, and
urllib.request.urlopen is booby-trapped so a test can never reach the network.
"""

import io
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
os.environ.setdefault("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())

from applier import preflight
from db import lifecycle
from db.jobs_db import JobsDB


class Settings:
    def __init__(self, **kw):
        self.values = {k: str(v) for k, v in kw.items()}

    def read(self, key, default=""):
        return self.values.get(key, default)


class Fetch:
    """Fake page fetch that records its calls."""
    def __init__(self, status=200, body=""):
        self.status, self.body, self.calls = status, body, []

    def __call__(self, url):
        self.calls.append(url)
        return self.status, self.body


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "jobs.db"
        self.alice = JobsDB(path=self.path, user_id=1)
        self.bob = JobsDB(path=self.path, user_id=2)
        self.n = 0
        trap = mock.patch("urllib.request.urlopen",
                          side_effect=AssertionError("a test touched the network"))
        trap.start()
        self.addCleanup(trap.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, db=None, *, title=None, company=None, source="linkedin",
            salary="", description="", url=None):
        """A job. Title and company default to something unique, because the
        database refuses a second job with the same title+company+source."""
        db = db or self.alice
        self.n += 1
        jid = db.add({"url": url or f"https://example.com/{db.user_id}/{self.n}",
                      "title": title if title is not None else f"Role {self.n}",
                      "company": company if company is not None else f"Company {self.n}",
                      "source": source, "salary": salary, "description": description})
        self.assertIsNotNone(jid, "the test's own job was refused as a duplicate")
        return jid

    def pre(self, jid, settings=None, fetch=None, db=None):
        db = db or self.alice
        return preflight.run(db.get_by_id(jid), db, settings or Settings(), fetch or Fetch())


# ── salary parser ─────────────────────────────────────────────────

class SalaryParserTests(unittest.TestCase):
    CASES = [
        ("12-18 LPA", (12, 18)), ("12 LPA", (12, 12)), ("INR 12.5 LPA", (12.5, 12.5)),
        ("₹12L - ₹18L / yr", (12, 18)), ("₹12L/yr - ₹18L/yr", (12, 18)),
        ("10-15 Lacs PA", (10, 15)), ("10 - 15 Lakhs per annum", (10, 15)),
        ("Rs. 8 lakhs per annum", (8, 8)), ("Rs 6L", (6, 6)),
        ("₹ 10,00,000 - 15,00,000 PA", (10, 15)),
        ("₹1,20,000 - ₹1,80,000 a year", (1.2, 1.8)),
        ("₹30,000 - ₹40,000 a month", (3.6, 4.8)), ("₹25K - 30K/month", (3.0, 3.6)),
        ("₹2L per month", (24, 24)),
        ("1.2 Cr", (120, 120)), ("1-2 Crore PA", (100, 200)),
        ("1200000", (12, 12)), ("12,00,000", (12, 12)),
        ("10-15 Lacs PA (5 years experience)", (5, 15)),      # stray number only widens the low end
    ]
    UNREADABLE = ["", "   ", "Not disclosed", "Competitive", "negotiable", "Best in the industry",
                  "$120,000 a year", "£50k", "USD 100k", "€70,000", "AED 20,000 per month",
                  "8,00,000",                                  # monthly or yearly? can't tell
                  "12-18",                                     # no unit anywhere
                  "₹30K a month (₹3.6L per annum)",            # says both
                  "up to ten lakhs", "no salary listed"]

    def test_readable_salaries(self):
        for text, (lo, hi) in self.CASES:
            with self.subTest(text=text):
                got = preflight.parse_salary_lpa(text)
                self.assertIsNotNone(got)
                self.assertAlmostEqual(got[0], lo, places=6)
                self.assertAlmostEqual(got[1], hi, places=6)

    def test_unreadable_or_ambiguous_salaries_are_none(self):
        for text in self.UNREADABLE:
            with self.subTest(text=text):
                self.assertIsNone(preflight.parse_salary_lpa(text))

    def test_none_input_is_none(self):
        self.assertIsNone(preflight.parse_salary_lpa(None))


# ── blacklist matcher ─────────────────────────────────────────────

class BlacklistMatcherTests(unittest.TestCase):
    def test_matches(self):
        m = preflight.blacklisted_entry
        cases = [
            ("Acme", "Acme", "Acme"),
            ("ACME corp", "acme", "acme"),                           # case
            ("Tata Consultancy Services Pvt. Ltd.", "Tata", "Tata"),  # a leading word
            ("The Meta Platforms Inc", "Meta", "Meta"),              # a middle word
            ("Infosys", "Infosys Ltd", "Infosys Ltd"),               # suffix on the entry only
            ("Infosys Limited", "Infosys Pvt Ltd", "Infosys Pvt Ltd"),
            ("AT&T", "AT&T", "AT&T"),
            ("Globex", "Initech, Globex ,Hooli", "Globex"),          # comma list
            ("Globex", "Initech\nGlobex\nHooli", "Globex"),          # one per line
            ("Globex", "Initech; Globex", "Globex"),
            ("Tata Consultancy Services", "Tata Consultancy", "Tata Consultancy"),
            ("Foo-Bar Labs", "foo bar", "foo bar"),                  # punctuation
        ]
        for company, text, want in cases:
            with self.subTest(company=company, text=text):
                self.assertEqual(m(company, text), want)

    def test_non_matches(self):
        m = preflight.blacklisted_entry
        cases = [
            ("Metals Inc", "Meta"),                                  # a word, not a substring
            ("Acmeco", "Acme"),
            ("Tata Consultancy Services", "Tata Services"),          # words must be side by side
            ("Tata", "Tata Consultancy"),                            # entry longer than the name
            ("Acme", ""), ("Acme", "   \n , ; "), ("Acme", "Ltd, Inc"),   # nothing usable listed
            ("", "Acme"), ("Ltd", "Acme"),                           # nothing to match against
            ("Globex", "Initech, Hooli"),
        ]
        for company, text in cases:
            with self.subTest(company=company, text=text):
                self.assertIsNone(m(company, text))

    def test_none_inputs(self):
        self.assertIsNone(preflight.blacklisted_entry(None, None))


# ── already applied ───────────────────────────────────────────────

class AlreadyAppliedTests(Base):
    def test_the_same_role_from_another_source_that_went_out_is_caught(self):
        old = self.add(source="naukri", title="Backend Engineer", company="Acme Corp")
        self.alice.transition(old, "submitted", actor="user")
        new = self.add(source="linkedin", title="backend engineer!", company="ACME Corp.")
        res = self.pre(new)
        self.assertFalse(res.ok)
        self.assertEqual(res.rule, "already_applied")
        self.assertEqual((res.detail["duplicate_job_id"], res.detail["duplicate_source"],
                          res.detail["duplicate_stage"]), (old, "naukri", "submitted"))
        self.assertIn("naukri", res.message)
        self.assertIn("Acme Corp", res.message)

    def test_every_stage_after_the_application_went_out_counts_and_none_before(self):
        for stage in lifecycle.STAGES:
            with self.subTest(stage=stage):
                other = self.add(source="indeed", title=f"Role {stage}", company="Co")
                with self.alice._conn() as c:
                    c.execute("UPDATE jobs SET stage=? WHERE job_id=?", (stage, other))
                mine = self.add(source="linkedin", title=f"Role {stage}", company="Co")
                went_out = stage in lifecycle.SUBMITTED_OR_LATER
                self.assertEqual(self.pre(mine).ok, not went_out, stage)

    def test_another_users_application_does_not_count(self):
        theirs = self.add(self.bob, source="naukri", title="Backend Engineer", company="Acme")
        self.bob.transition(theirs, "submitted", actor="user")
        mine = self.add(self.alice, source="linkedin", title="Backend Engineer", company="Acme")
        self.assertTrue(self.pre(mine).ok)
        # ...also through the UNSCOPED view the Orchestrator uses
        unscoped = JobsDB(path=self.path)
        self.assertTrue(preflight.run(unscoped.get_by_id(mine), unscoped, Settings(), Fetch()).ok)

    def test_a_job_is_not_a_duplicate_of_itself(self):
        j = self.add()
        self.alice.transition(j, "submitted", actor="user")
        self.assertTrue(self.pre(j).ok)

    def test_a_different_company_or_title_is_not_a_duplicate(self):
        self.alice.transition(self.add(title="Backend Engineer", company="Acme"), "submitted")
        self.assertTrue(self.pre(self.add(title="Backend Engineer", company="Globex")).ok)
        self.assertTrue(self.pre(self.add(title="Frontend Engineer", company="Acme")).ok)

    def test_a_blank_title_or_company_never_matches(self):
        a = self.add(title="Engineer", company="", source="indeed")
        self.alice.transition(a, "submitted")
        self.assertTrue(self.pre(self.add(title="Engineer", company="", source="naukri")).ok)


# ── blacklist, salary, open ───────────────────────────────────────

class BlacklistRuleTests(Base):
    def test_a_blacklisted_company_fails_with_the_entry_named(self):
        j = self.add(company="Tata Consultancy Services")
        res = self.pre(j, Settings(company_blacklist="Infosys\nTata"))
        self.assertEqual((res.ok, res.rule), (False, "blacklisted_company"))
        self.assertEqual(res.detail, {"entry": "Tata"})
        self.assertIn("Tata", res.message)

    def test_an_empty_or_unrelated_blacklist_passes(self):
        j = self.add(company="Globex")
        self.assertTrue(self.pre(j, Settings(company_blacklist="")).ok)
        self.assertTrue(self.pre(j, Settings()).ok)
        self.assertTrue(self.pre(j, Settings(company_blacklist="Acme, Initech")).ok)


class SalaryRuleTests(Base):
    def run_with(self, salary, floor):
        return self.pre(self.add(salary=salary), Settings(min_salary_lpa=floor))

    def test_below_the_floor_fails_with_the_numbers(self):
        res = self.run_with("6-8 LPA", "10")
        self.assertEqual((res.ok, res.rule), (False, "salary_below_floor"))
        self.assertEqual((res.detail["salary"], res.detail["salary_high_lpa"], res.detail["floor_lpa"]),
                         ("6-8 LPA", 8.0, 10.0))
        self.assertIn("8 LPA", res.message)
        self.assertIn("10 LPA", res.message)

    def test_boundaries(self):
        self.assertTrue(self.run_with("10 LPA", "10").ok, "exactly the floor is fine")
        self.assertFalse(self.run_with("9.9 LPA", "10").ok)
        self.assertTrue(self.run_with("8-14 LPA", "10").ok, "a range that reaches the floor is fine")
        self.assertFalse(self.run_with("₹30,000 a month", "10").ok, "monthly figures are annualised")
        self.assertTrue(self.run_with("₹1,00,000 a month", "10").ok)

    def test_when_it_cannot_tell_the_job_passes(self):
        for salary in ("", "Not disclosed", "$90,000 a year", "8,00,000", "12-18"):
            with self.subTest(salary=salary):
                self.assertTrue(self.run_with(salary, "50").ok)

    def test_the_rule_is_off_without_a_usable_floor(self):
        for floor in ("", "0", "abc", "-5", None):
            with self.subTest(floor=floor):
                settings = Settings() if floor is None else Settings(min_salary_lpa=floor)
                self.assertTrue(self.pre(self.add(salary="1 LPA"), settings).ok)


class JobOpenRuleTests(Base):
    def test_the_listing_itself_says_closed_and_no_request_is_made(self):
        f = Fetch()
        j = self.add(description="Senior role. No longer accepting applications. Apply by...")
        res = self.pre(j, fetch=f)
        self.assertEqual((res.ok, res.rule, res.detail), (False, "job_closed", {"how": "listing"}))
        self.assertEqual(f.calls, [])

    def test_http_404_and_410_mean_closed(self):
        for status in (404, 410):
            with self.subTest(status=status):
                j = self.add()
                res = self.pre(j, fetch=Fetch(status))
                self.assertEqual((res.ok, res.rule), (False, "job_closed"))
                self.assertEqual(res.detail, {"how": "http_status", "status": status})
                self.assertIn(str(status), res.message)

    def test_closed_phrases_on_the_page_mean_closed(self):
        for phrase in ("No longer accepting applications", "This job is no longer available",
                       "This job has expired", "This position has been filled",
                       "The job you were looking for is no longer available"):
            with self.subTest(phrase=phrase):
                body = f"<html><body><h1>Engineer</h1><p>{phrase}.</p></body></html>"
                res = self.pre(self.add(), fetch=Fetch(200, body))
                self.assertEqual((res.ok, res.rule, res.detail), (False, "job_closed", {"how": "page_text"}))

    def test_a_phrase_that_only_lives_in_a_script_or_template_does_not_count(self):
        body = ('<html><script>var t = {closed: "No longer accepting applications"};</script>'
                '<style>.x{}</style><template>This job has expired</template>'
                '<!-- This job is no longer available --><body>Apply now</body></html>')
        self.assertTrue(self.pre(self.add(), fetch=Fetch(200, body)).ok)

    def test_when_it_cannot_tell_the_job_passes(self):
        for status, body in [(None, ""), (200, "<html>Apply now</html>"), (403, "Forbidden"),
                             (429, ""), (999, ""), (500, "oops"), (301, "")]:
            with self.subTest(status=status):
                self.assertTrue(self.pre(self.add(), fetch=Fetch(status, body)).ok)

    def test_a_job_without_a_url_makes_no_request(self):
        f = Fetch(404)
        res = preflight.run({"job_id": "x", "title": "t", "company": "c", "url": ""},
                            self.alice, Settings(), f)
        self.assertTrue(res.ok)
        self.assertEqual(f.calls, [])


# ── order, short-circuit, safety ──────────────────────────────────

class RunTests(Base):
    def test_cheap_rules_run_first_and_a_ruled_out_job_costs_no_request(self):
        f = Fetch(404)
        j = self.add(company="Acme")
        res = self.pre(j, Settings(company_blacklist="Acme"), f)
        self.assertEqual(res.rule, "blacklisted_company")
        self.assertEqual(f.calls, [], "the network is the LAST check")
        res = self.pre(self.add(salary="1 LPA"), Settings(min_salary_lpa="10"), f)
        self.assertEqual(res.rule, "salary_below_floor")
        self.assertEqual(f.calls, [])

    def test_rule_precedence(self):
        old = self.add(source="naukri", company="Acme", title="Dev")
        self.alice.transition(old, "submitted")
        j = self.add(source="linkedin", company="Acme", title="Dev", salary="1 LPA")
        s = Settings(company_blacklist="Acme", min_salary_lpa="10")
        self.assertEqual(self.pre(j, s, Fetch(404)).rule, "already_applied")
        self.alice.transition(old, "withdrawn")                 # still "went out" - still counts
        self.assertEqual(self.pre(j, s, Fetch(404)).rule, "already_applied")
        with self.alice._conn() as c:
            c.execute("UPDATE jobs SET stage='failed' WHERE job_id=?", (old,))
        self.assertEqual(self.pre(j, s, Fetch(404)).rule, "blacklisted_company")
        self.assertEqual(self.pre(j, Settings(min_salary_lpa="10"), Fetch(404)).rule, "salary_below_floor")
        self.assertEqual(self.pre(j, Settings(), Fetch(404)).rule, "job_closed")
        self.assertTrue(self.pre(j, Settings(), Fetch(200)).ok)

    def test_a_clean_job_passes_and_the_result_is_structured(self):
        res = self.pre(self.add(salary="20 LPA", company="Globex"),
                       Settings(company_blacklist="Acme", min_salary_lpa="10"))
        self.assertEqual((res.ok, res.rule, res.message, res.detail), (True, "", "", {}))

    def test_a_crashing_check_is_logged_and_treated_as_a_pass(self):
        def boom(*a):
            raise RuntimeError("db exploded")
        j = self.add(company="Acme Corp")
        with mock.patch.object(preflight, "_CHECKS", (boom, preflight._blacklist)):
            with self.assertLogs("applier.preflight", "ERROR"):
                res = self.pre(j, Settings(company_blacklist="Acme Corp"))
        self.assertEqual((res.ok, res.rule), (False, "blacklisted_company"),
                         "the checks after the crash must still run")
        with mock.patch.object(preflight, "_CHECKS", (boom,)):
            with self.assertLogs("applier.preflight", "ERROR"):
                self.assertTrue(self.pre(j).ok)

    def test_the_default_fetch_is_looked_up_at_call_time(self):
        f = Fetch(404)
        with mock.patch("applier.preflight.fetch_page", f):
            res = preflight.run(self.alice.get_by_id(self.add()), self.alice, Settings())
        self.assertEqual((res.rule, len(f.calls)), ("job_closed", 1))

    def test_every_rule_code_is_one_the_module_declares(self):
        seen = {self.pre(self.add(company="Acme"), Settings(company_blacklist="Acme")).rule,
                self.pre(self.add(salary="1 LPA"), Settings(min_salary_lpa="9")).rule,
                self.pre(self.add(), fetch=Fetch(404)).rule}
        old = self.add(source="naukri", title="X", company="Y")
        self.alice.transition(old, "submitted")
        seen.add(self.pre(self.add(title="X", company="Y")).rule)
        self.assertEqual(seen, set(preflight.RULES))


class FetchPageTests(unittest.TestCase):
    """fetch_page's own safety, with urlopen faked (never the network)."""
    class Resp:
        def __init__(self, status, body):
            self.status, self._body = status, body
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1): return self._body[:n] if n and n > 0 else self._body

    def test_bad_or_dangerous_urls_are_never_requested(self):
        # fetch_page swallows every error, so "it raised" proves nothing: check the call count
        with mock.patch("urllib.request.urlopen") as urlopen:
            for url in ("file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)", "",
                        "http://169.254.169.254/latest/meta-data/", "https://user:pw@example.com/",
                        "http://example.com/has space"):
                with self.subTest(url=url):
                    self.assertEqual(preflight.fetch_page(url), (None, ""))
            self.assertEqual(urlopen.call_count, 0, "a bad URL was requested")

    def test_a_good_response_returns_status_and_text(self):
        with mock.patch("urllib.request.urlopen", return_value=self.Resp(200, "héllo".encode())):
            self.assertEqual(preflight.fetch_page("https://example.com/job/1"), (200, "héllo"))

    def test_http_errors_keep_their_status_and_body(self):
        err = urllib.error.HTTPError("https://e.com/x", 404, "Not Found", {}, io.BytesIO(b"gone"))
        with mock.patch("urllib.request.urlopen", side_effect=err):
            self.assertEqual(preflight.fetch_page("https://example.com/job/1"), (404, "gone"))

    def test_network_failures_are_just_unknown(self):
        for exc in (urllib.error.URLError("dns"), TimeoutError("slow"), ConnectionResetError(), OSError("x")):
            with self.subTest(exc=type(exc).__name__):
                with mock.patch("urllib.request.urlopen", side_effect=exc):
                    self.assertEqual(preflight.fetch_page("https://example.com/job/1"), (None, ""))

    def test_the_body_read_is_capped(self):
        resp = self.Resp(200, b"x" * 1000)
        with mock.patch("urllib.request.urlopen", return_value=resp):
            status, text = preflight.fetch_page("https://example.com/job/1", max_bytes=100)
        self.assertEqual((status, len(text)), (200, 100))


if __name__ == "__main__":
    unittest.main(verbosity=2)
