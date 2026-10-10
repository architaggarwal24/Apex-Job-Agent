# tests/test_autonomy.py
"""
tests/test_autonomy.py — measured reliability and the autonomy rules built on it:

  * detect_site_type (pure);
  * every rule condition, at its boundary (pure);
  * the store: kill switch (default OFF), rules (defaults, validation, owner
    scoping), the ledger and its success-rate maths;
  * through the Orchestrator: nothing happens with the switch off or no rules,
    a matching rule auto-approves (honouring dry run and the daily limit and
    logging actor=autonomy with the rule id), a failure switches the site
    type's rules off until a person re-enables them, and people's decisions
    land in the ledger;
  * the HTTP routes (skipped without FastAPI, like tests/test_http_auth.py).

Run from the project root:   python3 -m unittest discover -s tests -v

Real throwaway SQLite files; no browser, no network.

NOTE: the HTTP class was written in a sandbox without FastAPI, so NOT yet run.
If one fails after `pip install -r requirements.txt`, read it as a real finding.
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
os.environ.setdefault("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())

from applier import autonomy
from applier.autonomy import answers_known, first_matching_rule, rule_failures
from applier.site_type import SITE_TYPES, detect_site_type
from core.config import cfg
from db.autonomy_db import WINDOW, AutonomyStore, clean_rule
from db.jobs_db import JobsDB
from db.review_db import ReviewStore

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except Exception:
    HAVE_FASTAPI = False


# ── site type detection ───────────────────────────────────────────

class DetectSiteTypeTests(unittest.TestCase):
    def check(self, expected, url, html=""):
        self.assertEqual(detect_site_type(url, html), expected, f"{url} {html[:40]!r}")

    def test_the_ats_hosts(self):
        self.check("greenhouse", "https://boards.greenhouse.io/acme/jobs/123")
        self.check("greenhouse", "https://job-boards.greenhouse.io/acme/jobs/123")
        self.check("lever", "https://jobs.lever.co/acme/abc-123")
        self.check("workday", "https://acme.wd5.myworkdayjobs.com/en-US/External/job/X")
        self.check("workday", "https://wd3.myworkdaysite.com/recruiting/acme/x")
        self.check("ashby", "https://jobs.ashbyhq.com/acme/uuid")
        self.check("smartrecruiters", "https://jobs.smartrecruiters.com/Acme/123-role")

    def test_a_host_is_matched_as_a_host_not_as_a_substring(self):
        self.check("generic", "https://evil.example.com/greenhouse.io/x")
        self.check("generic", "https://notgreenhouse.io/x")
        self.check("generic", "https://example.com/?next=https://jobs.lever.co/x")

    def test_www_port_and_case_do_not_matter(self):
        self.check("greenhouse", "HTTPS://WWW.Boards.Greenhouse.IO:443/acme/jobs/1")

    def test_linkedin_is_easy_apply_by_url_alone_or_when_the_page_shows_it(self):
        self.check("linkedin_easy_apply", "https://www.linkedin.com/jobs/view/1")
        self.check("linkedin_easy_apply", "https://www.linkedin.com/jobs/view/1",
                   '<button class="jobs-apply-button">Easy Apply</button>')

    def test_a_linkedin_page_without_easy_apply_is_generic(self):
        self.check("generic", "https://www.linkedin.com/jobs/view/1", "<button>Apply</button> on company website")

    def test_indeed_apply_by_url_alone_or_marker_otherwise_generic(self):
        self.check("indeed_apply", "https://in.indeed.com/viewjob?jk=1")
        self.check("indeed_apply", "https://www.indeed.com/viewjob?jk=1",
                   '<button data-testid="indeedApplyButton">Apply now</button>')
        self.check("generic", "https://www.indeed.com/viewjob?jk=1", "<a>Apply on company site</a>")

    def test_naukri_one_click_unless_the_page_has_the_chat_questionnaire(self):
        self.check("naukri_one_click", "https://www.naukri.com/job-listings-dev-acme-1")
        self.check("naukri_one_click", "https://www.naukri.com/job-listings-dev-acme-1", "<button>Apply</button>")
        self.check("naukri_chat", "https://www.naukri.com/job-listings-dev-acme-1",
                   '<div class="chatbot_DrawerContentWrapper">')
        self.check("naukri_chat", "https://www.naukri.com/job-listings-dev-acme-1", '<div id="webBotDrawer">')

    def test_an_ats_embedded_in_a_company_page_is_found_by_its_markers(self):
        self.check("greenhouse", "https://acme.com/careers",
                   '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>')
        self.check("lever", "https://acme.com/careers", '<iframe src="https://jobs.lever.co/acme"></iframe>')
        self.check("ashby", "https://acme.com/careers", '<script>window.ashby_jid = "x"</script>')
        self.check("workday", "https://acme.com/careers", '<a href="https://acme.myworkdayjobs.com/x">')
        self.check("smartrecruiters", "https://acme.com/careers", '<a href="https://careers.smartrecruiters.com/Acme">')

    def test_the_host_beats_the_html(self):
        self.check("lever", "https://jobs.lever.co/acme/1", '<script src="https://boards.greenhouse.io/x">')
        self.check("naukri_one_click", "https://www.naukri.com/j-1", '<a href="https://boards.greenhouse.io/x">')

    def test_anything_else_is_generic(self):
        self.check("generic", "https://acme.com/careers/engineer")
        self.check("generic", "")
        self.check("generic", "not a url at all")
        self.check("generic", "http://[broken")

    def test_garbage_input_never_raises(self):
        for url, html in ((None, None), (123, b"x"), ([], {}), ("https://acme.com", None)):
            self.assertIn(detect_site_type(url, html), SITE_TYPES)

    def test_it_is_pure_the_same_input_always_gives_the_same_answer(self):
        args = ("https://boards.greenhouse.io/a/jobs/1", "<html></html>")
        self.assertEqual({detect_site_type(*args) for _ in range(5)}, {"greenhouse"})

    def test_every_answer_is_one_of_the_ten_types(self):
        self.assertEqual(set(SITE_TYPES), {
            "linkedin_easy_apply", "indeed_apply", "naukri_one_click", "naukri_chat", "greenhouse",
            "lever", "workday", "ashby", "smartrecruiters", "generic"})


# ── rule conditions (pure) ────────────────────────────────────────

def pkt(*fields):
    return {"fields": [{"source": s, "confidence": c} for s, c in fields]}


GOOD_PACKET = pkt(("answer_bank", 1.0), ("profile", 0.95))
GOOD_STATS = {"reviewed": 25, "success_rate": 0.95}


def rule(**kw):
    base = {"id": 1, "site_type": "greenhouse", "min_score": 70, "require_all_answers_known": True,
            "min_attempts": 20, "min_success_rate": 0.9, "max_per_day": 5, "enabled": True}
    return {**base, **kw}


class RuleConditionTests(unittest.TestCase):
    def why(self, r=None, *, site_type="greenhouse", ai_score=80, packet=GOOD_PACKET, stats=GOOD_STATS, today=0):
        return rule_failures(r or rule(), site_type=site_type, ai_score=ai_score, packet=packet,
                             stats=stats, approved_today=today)

    def test_a_rule_whose_conditions_are_all_met_matches(self):
        self.assertEqual(self.why(), [])

    def test_a_disabled_rule_never_matches(self):
        self.assertTrue(self.why(rule(enabled=False)))

    def test_site_type_must_match_unless_the_rule_is_for_any(self):
        self.assertTrue(self.why(site_type="lever"))
        self.assertEqual(self.why(rule(site_type="*"), site_type="lever"), [])
        self.assertEqual(self.why(rule(site_type="*"), site_type="workday"), [])

    def test_min_score_equal_passes_below_fails_and_unscored_fails(self):
        self.assertEqual(self.why(ai_score=70), [])
        self.assertTrue(self.why(ai_score=69))
        self.assertTrue(self.why(ai_score=None))
        self.assertEqual(self.why(rule(min_score=0), ai_score=0), [])

    def test_min_attempts_counts_attempts_a_person_decided(self):
        self.assertEqual(self.why(stats={**GOOD_STATS, "reviewed": 20}), [])
        self.assertTrue(self.why(stats={**GOOD_STATS, "reviewed": 19}))
        self.assertEqual(self.why(rule(min_attempts=3), stats={**GOOD_STATS, "reviewed": 3}), [])

    def test_min_success_rate_equal_passes_below_fails_and_no_history_fails(self):
        self.assertEqual(self.why(stats={**GOOD_STATS, "success_rate": 0.9}), [])
        self.assertTrue(self.why(stats={**GOOD_STATS, "success_rate": 0.89}))
        self.assertTrue(self.why(stats={**GOOD_STATS, "success_rate": None}))
        self.assertEqual(self.why(rule(min_success_rate=0.5), stats={**GOOD_STATS, "success_rate": 0.5}), [])

    def test_max_per_day_is_a_cap_on_what_the_rule_already_did_today(self):
        self.assertEqual(self.why(today=4), [])
        self.assertTrue(self.why(today=5))
        self.assertTrue(self.why(today=6))

    def test_every_unmet_condition_is_reported_not_just_the_first(self):
        why = self.why(ai_score=10, stats={"reviewed": 1, "success_rate": 0.1}, packet=None, today=9)
        self.assertEqual(len(why), 5)

    def test_the_first_matching_rule_by_id_wins_and_unmet_ones_are_skipped(self):
        rules = [rule(id=3, max_per_day=9), rule(id=1, min_score=99), rule(id=2, min_score=50)]
        got = first_matching_rule(rules, site_type="greenhouse", ai_score=80, packet=GOOD_PACKET,
                                  stats=GOOD_STATS, approved_today=lambda rid: 0)
        self.assertEqual(got["id"], 2)

    def test_no_matching_rule_is_none(self):
        self.assertIsNone(first_matching_rule([], site_type="greenhouse", ai_score=80, packet=GOOD_PACKET,
                                              stats=GOOD_STATS, approved_today=lambda rid: 0))
        self.assertIsNone(first_matching_rule([rule(min_score=100)], site_type="greenhouse", ai_score=80,
                                              packet=GOOD_PACKET, stats=GOOD_STATS, approved_today=lambda rid: 0))


class AnswersKnownTests(unittest.TestCase):
    def test_bank_and_profile_at_0_9_or_more_are_known(self):
        self.assertTrue(answers_known(pkt(("answer_bank", 1.0), ("profile", 0.95), ("answer_bank", 0.9))))

    def test_confidence_just_below_0_9_is_not(self):
        self.assertFalse(answers_known(pkt(("answer_bank", 0.89))))

    def test_one_field_you_typed_or_the_ai_guessed_spoils_it(self):
        self.assertFalse(answers_known(pkt(("answer_bank", 1.0), ("user", 1.0))))
        self.assertFalse(answers_known(pkt(("answer_bank", 1.0), ("ai", 0.6))))
        self.assertFalse(answers_known(pkt(("profile", 0.95), ("ai", 0.0))))

    def test_no_packet_is_not_known(self):
        self.assertFalse(answers_known(None))
        self.assertFalse(answers_known({}))
        self.assertFalse(answers_known({"fields": "x"}))

    def test_a_packet_with_nothing_to_answer_is_known(self):
        self.assertTrue(answers_known({"fields": []}))

    def test_the_rule_can_switch_the_check_off(self):
        why = rule_failures(rule(require_all_answers_known=False), site_type="greenhouse", ai_score=80,
                            packet=None, stats=GOOD_STATS, approved_today=0)
        self.assertEqual(why, [])
        why = rule_failures(rule(require_all_answers_known=True), site_type="greenhouse", ai_score=80,
                            packet=None, stats=GOOD_STATS, approved_today=0)
        self.assertEqual(len(why), 1)


# ── the store ─────────────────────────────────────────────────────

class StoreBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "jobs.db"
        self.t0 = datetime(2026, 1, 1, 9, 0, 0)
        self.k = 0

    def tearDown(self):
        self.tmp.cleanup()

    def store(self, uid=1):
        return AutonomyStore(uid, self.path)

    def decide(self, store, site, outcome, rule_id=None, when=None):
        """One finished attempt on `site`, `when` (default: each call a second later)."""
        self.k += 1
        when = when or self.t0 + timedelta(seconds=self.k)
        job = f"h{self.k}"
        store.record_review_reached(job, site, now=when)
        store.close_attempt(job, outcome, rule_id=rule_id, now=when)

    def history(self, store, site, unchanged=0, edited=0, rejected=0):
        for outcome, n in (("approved_unchanged", unchanged), ("approved_edited", edited), ("rejected", rejected)):
            for _ in range(n):
                self.decide(store, site, outcome)


class KillSwitchTests(StoreBase):
    def test_it_is_off_by_default(self):
        self.assertFalse(self.store().is_enabled())

    def test_on_then_off(self):
        s = self.store()
        s.set_enabled(True)
        self.assertTrue(s.is_enabled())
        s.set_enabled(False)
        self.assertFalse(s.is_enabled())

    def test_it_is_per_user(self):
        self.store(1).set_enabled(True)
        self.assertFalse(self.store(2).is_enabled())

    def test_only_a_real_boolean_is_accepted(self):
        for bad in (1, "true", None):
            with self.assertRaises(ValueError):
                self.store().set_enabled(bad)

    def test_there_are_no_rules_by_default(self):
        self.assertEqual(self.store().list_rules(), [])


class RuleStoreTests(StoreBase):
    def test_defaults_are_20_attempts_and_a_90_percent_rate_and_answers_must_be_known(self):
        r = self.store().add_rule({"site_type": "greenhouse", "min_score": 70, "max_per_day": 3})
        self.assertEqual((r["min_attempts"], r["min_success_rate"], r["require_all_answers_known"],
                          r["enabled"], r["disabled_reason"]), (20, 0.9, True, True, ""))

    def test_a_rule_does_not_expose_the_user_id(self):
        r = self.store().add_rule({"site_type": "*", "min_score": 0, "max_per_day": 1})
        self.assertNotIn("user_id", r)

    def test_min_score_and_max_per_day_and_site_type_are_required(self):
        for missing in ("site_type", "min_score", "max_per_day"):
            data = {"site_type": "lever", "min_score": 70, "max_per_day": 3}
            del data[missing]
            with self.assertRaisesRegex(ValueError, missing):
                clean_rule(data)

    def test_validation(self):
        ok = {"site_type": "lever", "min_score": 70, "max_per_day": 3}
        bad = [{"site_type": "monster"}, {"site_type": 5}, {"min_score": -1}, {"min_score": 101},
               {"min_score": 70.5}, {"min_score": True}, {"max_per_day": 0}, {"min_attempts": 0},
               {"min_success_rate": 0}, {"min_success_rate": 1.1}, {"min_success_rate": "0.9"},
               {"min_success_rate": True}, {"require_all_answers_known": 1}, {"enabled": "yes"},
               {"nonsense": 1}]
        for patch in bad:
            with self.assertRaises(ValueError, msg=str(patch)):
                clean_rule({**ok, **patch})
        with self.assertRaises(ValueError):
            clean_rule("rule")
        self.assertEqual(clean_rule({**ok, "site_type": "*", "min_success_rate": 1})["min_success_rate"], 1.0)

    def test_update_changes_only_what_it_is_given(self):
        s = self.store()
        r = s.add_rule({"site_type": "lever", "min_score": 70, "max_per_day": 3})
        u = s.update_rule(r["id"], {"min_score": 80})
        self.assertEqual((u["min_score"], u["max_per_day"], u["site_type"]), (80, 3, "lever"))
        with self.assertRaises(ValueError):
            s.update_rule(r["id"], {})
        with self.assertRaises(ValueError):
            s.update_rule(r["id"], {"min_score": 500})

    def test_rules_are_per_user(self):
        a, b = self.store(1), self.store(2)
        r = a.add_rule({"site_type": "lever", "min_score": 70, "max_per_day": 3})
        self.assertEqual(b.list_rules(), [])
        self.assertIsNone(b.get_rule(r["id"]))
        self.assertIsNone(b.update_rule(r["id"], {"min_score": 1}))
        self.assertFalse(b.delete_rule(r["id"]))
        self.assertEqual(a.get_rule(r["id"])["min_score"], 70, "Bob changed nothing of Alice's")
        self.assertTrue(a.delete_rule(r["id"]))

    def test_a_store_needs_a_user_id(self):
        for bad in (None, "1", True):
            with self.assertRaises(ValueError):
                AutonomyStore(bad, self.path)


class AutoDisableTests(StoreBase):
    def setUp(self):
        super().setUp()
        self.s = self.store()
        add = lambda st: self.s.add_rule({"site_type": st, "min_score": 70, "max_per_day": 3})["id"]
        self.gh, self.gh2, self.lever, self.any = add("greenhouse"), add("greenhouse"), add("lever"), add("*")

    def enabled(self):
        return {r["id"]: r["enabled"] for r in self.s.list_rules()}

    def test_a_failure_switches_off_that_site_types_rules_and_the_wildcard_ones(self):
        ids = self.s.disable_rules_for("greenhouse", "captcha failure on greenhouse")
        self.assertEqual(ids, [self.gh, self.gh2, self.any])
        self.assertEqual(self.enabled(), {self.gh: False, self.gh2: False, self.lever: True, self.any: False})

    def test_the_reason_and_time_are_recorded(self):
        self.s.disable_rules_for("greenhouse", "captcha failure on greenhouse")
        r = self.s.get_rule(self.gh)
        self.assertEqual(r["disabled_reason"], "captcha failure on greenhouse")
        self.assertNotEqual(r["disabled_at"], "")

    def test_a_second_failure_has_nothing_left_to_disable(self):
        self.s.disable_rules_for("greenhouse", "x")
        self.assertEqual(self.s.disable_rules_for("greenhouse", "y"), [])
        self.assertEqual(self.s.get_rule(self.gh)["disabled_reason"], "x")

    def test_they_stay_off_until_a_person_re_enables_them_and_the_reason_is_cleared(self):
        self.s.disable_rules_for("greenhouse", "x")
        r = self.s.update_rule(self.gh, {"enabled": True})
        self.assertEqual((r["enabled"], r["disabled_reason"], r["disabled_at"]), (True, "", ""))
        self.assertFalse(self.enabled()[self.gh2], "re-enabling one rule leaves the others off")

    def test_another_users_rules_are_untouched(self):
        other = self.store(2)
        o = other.add_rule({"site_type": "greenhouse", "min_score": 70, "max_per_day": 3})
        self.s.disable_rules_for("greenhouse", "x")
        self.assertTrue(other.get_rule(o["id"])["enabled"])


class LedgerTests(StoreBase):
    def test_every_site_type_is_listed_even_when_nothing_happened(self):
        ledger = self.store().ledger()
        self.assertEqual(list(ledger), list(SITE_TYPES))
        z = ledger["greenhouse"]
        self.assertEqual((z["attempts"], z["reviewed"], z["success_rate"], z["failures"]), (0, 0, None, {}))

    def test_an_attempt_that_reached_review_is_pending_until_decided(self):
        s = self.store()
        s.record_review_reached("j1", "greenhouse")
        st = s.site_stats("greenhouse")
        self.assertEqual((st["attempts"], st["pending"], st["reviewed"], st["success_rate"]), (1, 1, 0, None))

    def test_each_outcome_is_counted_on_its_own(self):
        s = self.store()
        self.history(s, "lever", unchanged=3, edited=2, rejected=1)
        self.decide(s, "lever", "auto_approved", rule_id=1)
        st = s.site_stats("lever")
        self.assertEqual((st["attempts"], st["approved_unchanged"], st["approved_edited"], st["rejected"],
                          st["auto_approved"], st["reviewed"]), (7, 3, 2, 1, 1, 6))

    def test_success_rate_is_approved_unchanged_over_the_reviewed(self):
        s = self.store()
        self.history(s, "lever", unchanged=9, edited=1)
        self.assertEqual(s.site_stats("lever")["success_rate"], 0.9)

    def test_a_rejection_counts_against_the_rate(self):
        s = self.store()
        self.history(s, "lever", unchanged=9, rejected=1)
        self.assertEqual(s.site_stats("lever")["success_rate"], 0.9)
        self.history(s, "lever", unchanged=0, rejected=1)
        self.assertAlmostEqual(s.site_stats("lever")["success_rate"], 9 / 11, places=4)

    def test_only_the_last_20_reviewed_count(self):
        s = self.store()
        self.history(s, "lever", rejected=5)                 # old and bad ...
        self.history(s, "lever", unchanged=20)               # ... then 20 perfect ones
        st = s.site_stats("lever")
        self.assertEqual((st["reviewed"], st["window"], st["window_unchanged"], st["success_rate"]),
                         (25, WINDOW, 20, 1.0))
        self.decide(s, "lever", "approved_edited")           # the oldest perfect one drops out
        st = s.site_stats("lever")
        self.assertEqual((st["window"], st["window_unchanged"], st["success_rate"]), (20, 19, 0.95))

    def test_with_fewer_than_20_the_rate_is_over_what_there_is(self):
        s = self.store()
        self.history(s, "lever", unchanged=3, edited=1)
        st = s.site_stats("lever")
        self.assertEqual((st["window"], st["success_rate"]), (4, 0.75))

    def test_auto_approvals_are_not_evidence_of_reliability(self):
        s = self.store()
        self.history(s, "lever", unchanged=1, edited=1)
        for _ in range(30):
            self.decide(s, "lever", "auto_approved", rule_id=1)
        st = s.site_stats("lever")
        self.assertEqual((st["reviewed"], st["window"], st["success_rate"]), (2, 2, 0.5))

    def test_site_types_are_kept_apart(self):
        s = self.store()
        self.history(s, "lever", unchanged=4)
        self.history(s, "greenhouse", edited=4)
        self.assertEqual((s.site_stats("lever")["success_rate"], s.site_stats("greenhouse")["success_rate"]), (1.0, 0.0))

    def test_reaching_review_again_supersedes_the_undecided_attempt(self):
        s = self.store()
        s.record_review_reached("j1", "lever")
        s.record_review_reached("j1", "lever")
        st = s.site_stats("lever")
        self.assertEqual((st["attempts"], st["superseded"], st["pending"]), (2, 1, 1))

    def test_deciding_a_job_that_never_reached_review_records_nothing(self):
        s = self.store()
        self.assertFalse(s.close_attempt("nope", "approved_unchanged"))
        self.assertEqual(s.site_stats("lever")["reviewed"], 0)

    def test_a_decided_attempt_cannot_be_decided_twice(self):
        s = self.store()
        s.record_review_reached("j1", "lever")
        self.assertTrue(s.close_attempt("j1", "approved_unchanged"))
        self.assertFalse(s.close_attempt("j1", "rejected"))
        self.assertEqual(s.site_stats("lever")["approved_unchanged"], 1)

    def test_failures_are_counted_by_class(self):
        s = self.store()
        for cls in ("captcha", "captcha", "timeout", "validation_error"):
            s.record_failure("j", "greenhouse", cls)
        st = s.site_stats("greenhouse")
        self.assertEqual((st["failures"], st["failures_total"]),
                         ({"captcha": 2, "timeout": 1, "validation_error": 1}, 4))
        self.assertEqual(s.site_stats("lever")["failures"], {})

    def test_the_ledger_is_per_user(self):
        a, b = self.store(1), self.store(2)
        self.history(a, "lever", unchanged=5)
        a.record_failure("j", "lever", "captcha")
        st = b.site_stats("lever")
        self.assertEqual((st["attempts"], st["reviewed"], st["failures_total"]), (0, 0, 0))
        self.assertEqual(b.ledger()["lever"]["success_rate"], None)

    def test_an_unknown_site_type_is_refused(self):
        with self.assertRaises(ValueError):
            self.store().record_review_reached("j", "monster")
        with self.assertRaises(ValueError):
            self.store().record_failure("j", "monster", "captcha")

    def test_what_a_rule_auto_approved_today_does_not_include_yesterday(self):
        s = self.store()
        now = datetime(2026, 3, 10, 15, 0)
        self.decide(s, "lever", "auto_approved", rule_id=7, when=now - timedelta(days=1))
        self.decide(s, "lever", "auto_approved", rule_id=7, when=now - timedelta(hours=3))
        self.decide(s, "lever", "auto_approved", rule_id=8, when=now - timedelta(hours=1))
        self.assertEqual((s.auto_approved_today(7, now), s.auto_approved_today(8, now)), (1, 1))


# ── through the Orchestrator ──────────────────────────────────────

class Playwright:
    def __enter__(self): return object()
    def __exit__(self, *a): return False


class Ctx:
    pages = []
    def new_page(self): return object()
    def close(self): pass


class Settings:
    def __init__(self, **values): self.values = {k: str(v) for k, v in values.items()}
    def read(self, key, default=""): return self.values.get(key, default)


class OrchestratorBase(StoreBase):
    def setUp(self):
        super().setUp()
        self._sessions, self._resume = cfg.SESSIONS_DIR, cfg.RESUME_PATH
        cfg.SESSIONS_DIR = self.dir / "sessions"
        cfg.SESSIONS_DIR.mkdir()
        cfg.RESUME_PATH = self.dir / "resume.pdf"
        cfg.RESUME_PATH.write_bytes(b"%PDF-1.4")
        self.alice, self.bob = JobsDB(path=self.path, user_id=1), JobsDB(path=self.path, user_id=2)
        self.s = self.store(1)
        self.settings = Settings(quality_first="false", daily_apply_limit=0, min_delay_between_applies=0)
        self.submits = []                          # [{"dry_run"}] as the submit pass ran
        self.detected = None                       # what the first pass reports as the site type
        self.fail_submit = None                    # an error message for the submit pass to fail with
        self.n = 0

        def fake_run(page, job, profile, emit=None, answers=None, **kw):
            if self.detected and emit:
                emit("site_type", self.detected)
            return {"success": True, "screenshots": [], "missing_fields": [], "error": "",
                    "review": {"fields": [], "url": "https://example.com/apply"}}

        def fake_submit(page, job, profile, corrections=None, emit=None, dry_run=False, answers=None, **kw):
            self.submits.append({"dry_run": dry_run})
            if self.fail_submit:
                return {"success": False, "screenshots": [], "error": self.fail_submit}
            return {"success": True, "screenshots": [], "error": ""}

        for p in (mock.patch("applier.session.Orchestrator._settings", staticmethod(lambda: self.settings)),
                  mock.patch("applier.session.Orchestrator._dry_run_enabled",
                             staticmethod(lambda: self.settings.read("dry_run", "false") == "true")),
                  mock.patch("applier.preflight.fetch_page", lambda url: (200, "")),
                  mock.patch("playwright.sync_api.sync_playwright", lambda: Playwright()),
                  mock.patch("applier.universal.run_application", fake_run),
                  mock.patch("applier.universal.run_submit", fake_submit),
                  mock.patch("applier.adapters.adapter_for", lambda *a, **k: None),    # these tests drive the universal path
                  mock.patch("urllib.request.urlopen", side_effect=AssertionError("network"))):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        cfg.SESSIONS_DIR, cfg.RESUME_PATH = self._sessions, self._resume
        super().tearDown()

    def job(self, db=None, url=None, score=85, stage="preparing"):
        db = db or self.alice
        self.n += 1
        jid = db.add({"url": url or f"https://boards.greenhouse.io/acme/jobs/{db.user_id}{self.n}",
                      "title": f"Role {self.n}", "company": "Acme", "source": "linkedin"})
        self.assertIsNotNone(jid)
        if score is not None:
            db.update_score(jid, score, "r")
        if stage in ("preparing", "ready_for_review"):
            db.transition(jid, "preparing")
        if stage == "ready_for_review":
            db.transition(jid, "ready_for_review")
        return jid

    def orchestrator(self, jid, db=None):
        from applier.session import ApplicationSession, Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        o.job_id = jid
        o.job = (db or self.alice).get_by_id(jid)
        o.profile_db = mock.Mock(summary=lambda: {"_flat": {}, "_answered": {}},
                                 get_pending_missing=lambda job_id: [])
        o.jobs_db = JobsDB(path=self.path)
        o.session = ApplicationSession.load(jid) or ApplicationSession(
            job_id=jid, job_url="u", job_title="t", company="Acme", source="linkedin")
        o._open_browser = lambda pw: Ctx()
        return o

    def earn_trust(self, site="greenhouse", n=20):
        self.history(self.s, site, unchanged=n)

    def trusting_rule(self, **kw):
        data = {"site_type": "greenhouse", "min_score": 70, "max_per_day": 5, **kw}
        self.s.set_enabled(True)
        return self.s.add_rule(data)

    def bank_packet(self, jid, uid=1, *fields):
        ReviewStore(uid, self.path).save(jid, pkt(*(fields or (("answer_bank", 1.0),))))

    def reach_review(self, jid, packet=True, orch=None):
        """The job is ready_for_review (it already is, from job(stage=...)); run the hook."""
        if packet:
            self.bank_packet(jid)
        (orch or self.orchestrator(jid))._after_review()

    def events(self, jid, db=None):
        return (db or self.alice).get_application(jid)["events"]

    def autonomy_events(self, jid):
        return [e for e in self.events(jid) if e["actor"] == "autonomy"]

    def stage(self, jid):
        return self.alice.get_stage(jid)


class NothingByDefaultTests(OrchestratorBase):
    def test_with_no_rules_everything_is_reviewed_even_with_the_switch_on_and_a_perfect_record(self):
        self.s.set_enabled(True)
        self.earn_trust()
        jid = self.job(stage="ready_for_review")
        self.reach_review(jid)
        self.assertEqual((self.stage(jid), self.submits), ("ready_for_review", []))
        self.assertEqual(self.autonomy_events(jid), [])

    def test_with_the_switch_off_a_perfect_rule_does_nothing(self):
        self.trusting_rule()
        self.s.set_enabled(False)
        self.earn_trust()
        jid = self.job(stage="ready_for_review")
        self.reach_review(jid)
        self.assertEqual((self.stage(jid), self.submits), ("ready_for_review", []))
        self.assertEqual(self.autonomy_events(jid), [])

    def test_a_fresh_user_has_the_switch_off(self):
        jid = self.job(stage="ready_for_review")
        self.s.add_rule({"site_type": "*", "min_score": 0, "max_per_day": 9, "min_attempts": 1,
                         "min_success_rate": 0.1, "require_all_answers_known": False})
        self.reach_review(jid)
        self.assertEqual((self.stage(jid), self.submits), ("ready_for_review", []))


class AutoApproveTests(OrchestratorBase):
    def test_a_matching_rule_approves_and_submits(self):
        rule = self.trusting_rule()
        self.earn_trust()
        jid = self.job(stage="ready_for_review")
        self.reach_review(jid)
        row = self.alice.get_by_id(jid)
        self.assertEqual((row["stage"], row["status"]), ("submitted", "applied"))
        self.assertEqual(self.submits, [{"dry_run": False}])
        (ev,) = [e for e in self.autonomy_events(jid) if e["detail"]["step"] == "autonomy_approved"]
        self.assertEqual((ev["actor"], ev["detail"]["rule_id"], ev["detail"]["site_type"], ev["detail"]["dry_run"]),
                         ("autonomy", rule["id"], "greenhouse", False))

    def test_the_attempt_is_closed_as_auto_approved_with_the_rule(self):
        rule = self.trusting_rule()
        self.earn_trust()
        jid = self.job(stage="ready_for_review")
        self.s.record_review_reached(jid, "greenhouse")
        self.bank_packet(jid)
        self.orchestrator(jid)._after_review()
        st = self.s.site_stats("greenhouse")
        self.assertEqual(st["auto_approved"], 1)
        self.assertEqual(self.s.auto_approved_today(rule["id"]), 1)

    def test_a_dry_run_is_honoured_nothing_is_clicked_and_the_job_stays_in_review(self):
        self.trusting_rule()
        self.earn_trust()
        self.settings = Settings(quality_first="false", daily_apply_limit=0, dry_run="true")
        jid = self.job(stage="ready_for_review")
        self.reach_review(jid)
        self.assertEqual(self.submits, [{"dry_run": True}])
        row = self.alice.get_by_id(jid)
        self.assertEqual((row["stage"], row["applied_at"]), ("ready_for_review", ""))
        (ev,) = [e for e in self.autonomy_events(jid) if e["detail"]["step"] == "autonomy_approved"]
        self.assertTrue(ev["detail"]["dry_run"])

    def test_the_daily_limit_stops_it_and_says_so(self):
        rule = self.trusting_rule()
        self.earn_trust()
        self.settings = Settings(quality_first="false", daily_apply_limit=1)
        done = self.job(stage="found")
        self.alice.transition(done, "submitted")            # one application already went out today
        jid = self.job(stage="ready_for_review")
        self.reach_review(jid)
        self.assertEqual((self.stage(jid), self.submits), ("ready_for_review", []))
        (ev,) = self.autonomy_events(jid)
        self.assertEqual((ev["detail"]["step"], ev["detail"]["rule_id"], ev["detail"]["reason"]),
                         ("autonomy_blocked", rule["id"], "daily_limit"))
        self.assertEqual(self.s.site_stats("greenhouse")["auto_approved"], 0)

    def test_the_daily_limit_is_not_enforced_in_dry_run(self):
        self.trusting_rule()
        self.earn_trust()
        self.settings = Settings(quality_first="false", daily_apply_limit=1, dry_run="true")
        done = self.job(stage="found")
        self.alice.transition(done, "submitted")
        jid = self.job(stage="ready_for_review")
        self.reach_review(jid)
        self.assertEqual(self.submits, [{"dry_run": True}])

    def test_max_per_day_caps_the_rule(self):
        self.trusting_rule(max_per_day=2)
        self.earn_trust()
        outcomes = []
        for _ in range(3):
            jid = self.job(stage="ready_for_review")
            self.reach_review(jid)
            outcomes.append(self.stage(jid))
        self.assertEqual(outcomes, ["submitted", "submitted", "ready_for_review"])

    def test_below_min_score_it_is_reviewed(self):
        self.trusting_rule(min_score=90)
        self.earn_trust()
        jid = self.job(stage="ready_for_review", score=89)
        self.reach_review(jid)
        self.assertEqual(self.stage(jid), "ready_for_review")

    def test_an_unscored_job_is_reviewed(self):
        self.trusting_rule(min_score=0)
        self.earn_trust()
        jid = self.job(stage="ready_for_review", score=None)
        self.reach_review(jid)
        self.assertEqual(self.stage(jid), "ready_for_review")

    def test_too_little_reviewed_history_means_review(self):
        self.trusting_rule()
        self.earn_trust(n=19)
        jid = self.job(stage="ready_for_review")
        self.reach_review(jid)
        self.assertEqual(self.stage(jid), "ready_for_review")

    def test_a_success_rate_below_the_bar_means_review(self):
        self.trusting_rule()
        self.history(self.s, "greenhouse", unchanged=17, edited=3)          # 0.85 < 0.9
        jid = self.job(stage="ready_for_review")
        self.reach_review(jid)
        self.assertEqual(self.stage(jid), "ready_for_review")

    def test_an_answer_you_typed_or_the_ai_guessed_means_review(self):
        self.trusting_rule()
        self.earn_trust()
        for field in (("user", 1.0), ("ai", 0.6), ("answer_bank", 0.85)):
            jid = self.job(stage="ready_for_review")
            self.bank_packet(jid, 1, ("profile", 0.95), field)
            self.orchestrator(jid)._after_review()
            self.assertEqual(self.stage(jid), "ready_for_review", field)

    def test_no_packet_means_review_when_the_rule_needs_known_answers(self):
        self.trusting_rule()
        self.earn_trust()
        jid = self.job(stage="ready_for_review")
        self.reach_review(jid, packet=False)
        self.assertEqual(self.stage(jid), "ready_for_review")

    def test_a_rule_that_does_not_need_known_answers_ignores_the_packet(self):
        self.trusting_rule(require_all_answers_known=False)
        self.earn_trust()
        jid = self.job(stage="ready_for_review")
        self.reach_review(jid, packet=False)
        self.assertEqual(self.stage(jid), "submitted")

    def test_a_rule_for_another_site_type_does_not_apply_but_a_wildcard_does(self):
        self.trusting_rule(site_type="lever")
        self.earn_trust()
        jid = self.job(stage="ready_for_review")
        self.reach_review(jid)
        self.assertEqual(self.stage(jid), "ready_for_review")
        self.s.add_rule({"site_type": "*", "min_score": 70, "max_per_day": 5})
        jid = self.job(stage="ready_for_review")
        self.reach_review(jid)
        self.assertEqual(self.stage(jid), "submitted")

    def test_a_disabled_rule_does_not_approve(self):
        rule = self.trusting_rule()
        self.s.update_rule(rule["id"], {"enabled": False})
        self.earn_trust()
        jid = self.job(stage="ready_for_review")
        self.reach_review(jid)
        self.assertEqual(self.stage(jid), "ready_for_review")

    def test_it_never_looks_at_another_users_rules_or_history(self):
        other = self.store(2)
        other.set_enabled(True)
        other.add_rule({"site_type": "*", "min_score": 0, "max_per_day": 9, "min_attempts": 1,
                        "min_success_rate": 0.1, "require_all_answers_known": False})
        self.history(other, "greenhouse", unchanged=30)
        jid = self.job(stage="ready_for_review")                       # Alice's job
        self.reach_review(jid)
        self.assertEqual(self.stage(jid), "ready_for_review")
        bobs = self.job(self.bob, stage="ready_for_review")           # Bob's: his own rule does apply
        self.bank_packet(bobs, 2)
        self.orchestrator(bobs, self.bob)._after_review()
        self.assertEqual(self.bob.get_stage(bobs), "submitted")


class WiringTests(OrchestratorBase):
    """Through the real first pass, so the hook is proven to be reached."""

    def first_pass(self, jid):
        self.orchestrator(jid).run_iter1()

    def test_reaching_review_puts_the_attempt_in_the_ledger_under_the_detected_site_type(self):
        self.detected = "lever"
        jid = self.job(url="https://acme.com/careers/1")
        self.first_pass(jid)
        self.assertEqual(self.stage(jid), "ready_for_review")
        self.assertEqual((self.s.site_stats("lever")["attempts"], self.s.site_stats("lever")["pending"]), (1, 1))
        self.assertEqual(self.s.site_stats("generic")["attempts"], 0)

    def test_without_a_detection_the_url_decides(self):
        jid = self.job(url="https://boards.greenhouse.io/acme/jobs/9")
        self.first_pass(jid)
        self.assertEqual(self.s.site_stats("greenhouse")["attempts"], 1)

    def test_the_detected_site_type_is_kept_with_the_session(self):
        from applier.session import ApplicationSession
        self.detected = "ashby"
        jid = self.job(url="https://acme.com/careers/1")
        self.first_pass(jid)
        self.assertEqual(ApplicationSession.load(jid).site_type, "ashby")

    def test_a_nonsense_detection_is_ignored(self):
        from applier.session import ApplicationSession
        self.detected = "monster"
        jid = self.job(url="https://acme.com/careers/1")
        self.first_pass(jid)
        self.assertEqual(ApplicationSession.load(jid).site_type, "")
        self.assertEqual(self.s.site_stats("generic")["attempts"], 1)

    def test_a_first_pass_that_reaches_review_is_auto_approved_by_a_matching_rule(self):
        self.trusting_rule(require_all_answers_known=False)
        self.earn_trust()
        jid = self.job()
        self.first_pass(jid)
        self.assertEqual((self.stage(jid), self.submits), ("submitted", [{"dry_run": False}]))
        self.assertEqual([e["detail"]["step"] for e in self.autonomy_events(jid)], ["autonomy_approved"])

    def test_a_first_pass_with_the_switch_off_waits_for_you(self):
        self.trusting_rule(require_all_answers_known=False)
        self.s.set_enabled(False)
        self.earn_trust()
        jid = self.job()
        self.first_pass(jid)
        self.assertEqual((self.stage(jid), self.submits), ("ready_for_review", []))

    def second_pass(self, jid):
        self.alice.transition(jid, "needs_you", reason="question")
        with mock.patch("applier.universal.run_iteration2", lambda *a, **kw: {"screenshots": []}):
            self.orchestrator(jid).run_iter2({})

    def test_the_second_pass_reaching_review_is_an_attempt_too(self):
        jid = self.job(url="https://jobs.lever.co/acme/1")
        self.second_pass(jid)
        self.assertEqual(self.stage(jid), "ready_for_review")
        self.assertEqual(self.s.site_stats("lever")["pending"], 1)

    def test_the_second_pass_reaching_review_can_be_auto_approved(self):
        self.trusting_rule(site_type="lever", require_all_answers_known=False)
        self.earn_trust("lever")
        jid = self.job(url="https://jobs.lever.co/acme/1")
        self.second_pass(jid)
        self.assertEqual((self.stage(jid), self.submits), ("submitted", [{"dry_run": False}]))

    def test_a_job_waiting_for_answers_is_not_in_review_so_it_is_not_an_attempt(self):
        self.trusting_rule(require_all_answers_known=False)
        self.earn_trust()
        o_jid = self.job()
        o = self.orchestrator(o_jid)
        o.profile_db = mock.Mock(summary=lambda: {"_flat": {}, "_answered": {}},
                                 get_pending_missing=lambda job_id: [
                                     {"id": 1, "field_key": "k", "field_label": "Q", "field_type": "text",
                                      "options": "", "placeholder_used": ""}])
        o.run_iter1()
        self.assertEqual((self.stage(o_jid), self.submits), ("needs_you", []))
        self.assertEqual(self.s.site_stats("greenhouse")["pending"], 0)


class FailureTests(OrchestratorBase):
    def test_a_failure_switches_the_site_types_rules_off_and_says_so(self):
        rule = self.trusting_rule()
        other = self.s.add_rule({"site_type": "lever", "min_score": 70, "max_per_day": 5})
        self.earn_trust()
        jid = self.job(stage="ready_for_review")
        self.orchestrator(jid)._fail("CAPTCHA detected — skipping")
        self.assertFalse(self.s.get_rule(rule["id"])["enabled"])
        self.assertIn("captcha", self.s.get_rule(rule["id"])["disabled_reason"])
        self.assertTrue(self.s.get_rule(other["id"])["enabled"], "a different site type is unaffected")
        (ev,) = [e for e in self.autonomy_events(jid) if e["detail"]["step"] == "autonomy_disabled"]
        self.assertEqual((ev["detail"]["rule_ids"], ev["detail"]["failure_class"]), ([rule["id"]], "captcha"))

    def test_the_failure_is_in_the_ledger_by_class(self):
        jid = self.job(stage="ready_for_review")
        self.orchestrator(jid)._fail("CAPTCHA detected — skipping")
        self.orchestrator(self.job())._fail("Navigation failed: Timeout 30000ms exceeded")
        self.assertEqual(self.s.site_stats("greenhouse")["failures"], {"captcha": 1, "timeout": 1})

    def test_once_off_it_stays_off_even_when_the_record_is_perfect(self):
        rule = self.trusting_rule()
        self.earn_trust()
        self.orchestrator(self.job(stage="ready_for_review"))._fail("CAPTCHA detected — skipping")
        jid = self.job(stage="ready_for_review")
        self.reach_review(jid)
        self.assertEqual((self.stage(jid), self.submits), ("ready_for_review", []))
        self.s.update_rule(rule["id"], {"enabled": True})                     # until you re-enable it
        jid = self.job(stage="ready_for_review")
        self.reach_review(jid)
        self.assertEqual(self.stage(jid), "submitted")

    def test_a_failure_of_the_auto_approved_submit_itself_switches_the_rule_off(self):
        rule = self.trusting_rule()
        self.earn_trust()
        self.fail_submit = "Validation error — the site rejected the answers: Phone is required"
        jid = self.job(stage="ready_for_review")
        self.reach_review(jid)
        self.assertEqual(self.stage(jid), "needs_you")
        self.assertFalse(self.s.get_rule(rule["id"])["enabled"])
        self.assertEqual(self.s.site_stats("greenhouse")["failures"], {"validation_error": 1})
        nxt = self.job(stage="ready_for_review")
        self.fail_submit = None
        self.reach_review(nxt)
        self.assertEqual(self.stage(nxt), "ready_for_review", "the next one waits for a person")

    def test_a_wildcard_rule_is_switched_off_by_a_failure_on_any_site_type(self):
        rule = self.s.add_rule({"site_type": "*", "min_score": 70, "max_per_day": 5})
        self.orchestrator(self.job(url="https://jobs.lever.co/a/1", stage="ready_for_review"))._fail("CAPTCHA detected")
        self.assertFalse(self.s.get_rule(rule["id"])["enabled"])

    def test_a_failure_leaves_another_users_rules_alone(self):
        other = self.store(2)
        theirs = other.add_rule({"site_type": "greenhouse", "min_score": 70, "max_per_day": 5})
        self.trusting_rule()
        self.orchestrator(self.job(stage="ready_for_review"))._fail("CAPTCHA detected")
        self.assertTrue(other.get_rule(theirs["id"])["enabled"])
        self.assertEqual(other.site_stats("greenhouse")["failures_total"], 0)

    def test_a_ledger_fault_never_stops_the_failure_being_handled(self):
        jid = self.job(stage="ready_for_review")
        with mock.patch("applier.autonomy.AutonomyStore", side_effect=RuntimeError("db gone")):
            self.orchestrator(jid)._fail("CAPTCHA detected — skipping")
        self.assertEqual(self.stage(jid), "needs_you")


class DecisionsInTheLedgerTests(OrchestratorBase):
    def reviewed_job(self):
        jid = self.job(stage="ready_for_review")
        self.s.record_review_reached(jid, "greenhouse")
        return jid

    def test_approving_as_it_was_is_approved_unchanged(self):
        jid = self.reviewed_job()
        self.orchestrator(jid).submit()
        st = self.s.site_stats("greenhouse")
        self.assertEqual((st["approved_unchanged"], st["approved_edited"], st["success_rate"]), (1, 0, 1.0))

    def test_approving_after_an_edit_in_review_is_approved_with_edits(self):
        jid = self.reviewed_job()
        self.alice.record_step(jid, "review_edit", 'Edited "Expected CTC"', {"field_id": "ctc"})
        self.orchestrator(jid).submit()
        st = self.s.site_stats("greenhouse")
        self.assertEqual((st["approved_unchanged"], st["approved_edited"], st["success_rate"]), (0, 1, 0.0))

    def test_an_edit_from_before_it_reached_review_again_does_not_count(self):
        jid = self.job(stage="ready_for_review")
        self.alice.record_step(jid, "review_edit", "Edited", {"field_id": "ctc"})
        self.s.record_review_reached(jid, "greenhouse")          # a fresh attempt, after that edit
        self.orchestrator(jid).submit()
        self.assertEqual(self.s.site_stats("greenhouse")["approved_unchanged"], 1)

    def test_approving_with_corrections_is_approved_with_edits(self):
        jid = self.reviewed_job()
        with mock.patch("applier.session.Orchestrator._apply_corrections"):
            self.orchestrator(jid).submit(["phone is 555"])
        self.assertEqual(self.s.site_stats("greenhouse")["approved_edited"], 1)

    def test_a_dry_run_approval_is_still_a_decision(self):
        self.settings = Settings(quality_first="false", dry_run="true")
        jid = self.reviewed_job()
        self.orchestrator(jid).submit()
        self.assertEqual(self.s.site_stats("greenhouse")["approved_unchanged"], 1)

    def test_rejecting_in_review_is_rejected_and_counts_against_the_rate(self):
        from applier import review
        jid = self.reviewed_job()
        review.reject_application(self.alice, jid, "wrong role")
        st = self.s.site_stats("greenhouse")
        self.assertEqual((st["rejected"], st["reviewed"], st["success_rate"]), (1, 1, 0.0))

    def test_an_autonomy_approval_is_not_counted_as_a_person_approving(self):
        self.trusting_rule(require_all_answers_known=False)
        self.earn_trust()
        jid = self.job(stage="ready_for_review")
        self.s.record_review_reached(jid, "greenhouse")
        self.orchestrator(jid)._after_review()
        st = self.s.site_stats("greenhouse")
        self.assertEqual((st["approved_unchanged"], st["auto_approved"], st["reviewed"]), (20, 1, 20))

    def test_a_decision_is_recorded_in_the_owners_ledger_only(self):
        jid = self.reviewed_job()
        self.orchestrator(jid).submit()
        self.assertEqual(self.store(2).site_stats("greenhouse")["approved_unchanged"], 0)

    def test_the_ledger_survives_the_job_being_deleted(self):
        jid = self.reviewed_job()
        self.orchestrator(jid).submit()
        self.alice.clear_all(include_applied=True)
        self.assertEqual(self.s.site_stats("greenhouse")["approved_unchanged"], 1)


# ── over HTTP ─────────────────────────────────────────────────────

@unittest.skipUnless(HAVE_FASTAPI, "fastapi + httpx not installed")
class AutonomyHttpTests(unittest.TestCase):
    def setUp(self):
        import core.auth as auth
        from db.settings_db import SettingsDB
        from db.users_db import UsersDB

        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self._saved_settings = SettingsDB._instance
        SettingsDB._instance = SettingsDB(path=tmp / "settings.db")
        self._saved = (cfg.JOBS_DB_PATH, cfg.PROFILE_DB_PATH, cfg.SESSIONS_DIR, cfg.APP_SECRET_KEY,
                       cfg.COOKIE_SECURE, cfg.ALLOW_SIGNUP, UsersDB._instance, auth._serializer)
        cfg.JOBS_DB_PATH    = tmp / "jobs.db"
        cfg.PROFILE_DB_PATH = tmp / "profile.db"
        cfg.SESSIONS_DIR    = tmp / "sessions"
        cfg.SESSIONS_DIR.mkdir()
        cfg.APP_SECRET_KEY  = "test-signing-secret-" + "x" * 40
        cfg.COOKIE_SECURE   = False
        cfg.ALLOW_SIGNUP    = False
        UsersDB._instance   = UsersDB(path=tmp / "users.db")
        auth._serializer    = None
        self._saved_dev_origin = os.environ.pop("APEX_DEV_UI_ORIGIN", None)

        from server.app import create_app
        self.app    = create_app(ui_dir=tmp / "no-build")
        self.client = TestClient(self.app, follow_redirects=False)       # alice = owner (user 1)
        self.client.post("/api/auth/signup",
                         json={"email": "alice@example.com", "password": "correct-horse-battery"})
        cfg.ALLOW_SIGNUP = True
        self.bob = TestClient(self.app, follow_redirects=False)          # bob (user 2)
        self.bob.post("/api/auth/signup",
                      json={"email": "bob@example.com", "password": "correct-horse-battery"})
        self.anon = TestClient(self.app, follow_redirects=False)

    def tearDown(self):
        import core.auth as auth
        from db.settings_db import SettingsDB
        from db.users_db import UsersDB
        SettingsDB._instance = self._saved_settings
        (cfg.JOBS_DB_PATH, cfg.PROFILE_DB_PATH, cfg.SESSIONS_DIR, cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE,
         cfg.ALLOW_SIGNUP, UsersDB._instance, auth._serializer) = self._saved
        os.environ.pop("APEX_DEV_UI_ORIGIN", None)
        if self._saved_dev_origin is not None:
            os.environ["APEX_DEV_UI_ORIGIN"] = self._saved_dev_origin
        self.tmp.cleanup()

    RULE = {"site_type": "greenhouse", "min_score": 70, "max_per_day": 3}

    def test_everything_needs_a_login(self):
        for method, url in (("get", "/api/automation/ledger"), ("get", "/api/automation/rules"),
                            ("post", "/api/automation/rules"), ("put", "/api/automation/rules/1"),
                            ("delete", "/api/automation/rules/1"), ("put", "/api/automation/autonomy")):
            kw = {"json": {}} if method in ("post", "put") else {}
            self.assertEqual(getattr(self.anon, method)(url, **kw).status_code, 401, url)

    def test_the_ledger_lists_every_site_type(self):
        r = self.client.get("/api/automation/ledger")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual((body["window"], list(body["site_types"])), (20, list(SITE_TYPES)))
        self.assertEqual(body["site_types"]["lever"]["success_rate"], None)

    def test_the_ledger_shows_this_users_history_only(self):
        s = AutonomyStore(1, cfg.JOBS_DB_PATH)
        s.record_review_reached("j", "lever")
        s.close_attempt("j", "approved_unchanged")
        s.record_failure("j2", "lever", "captcha")
        mine = self.client.get("/api/automation/ledger").json()["site_types"]["lever"]
        self.assertEqual((mine["approved_unchanged"], mine["success_rate"], mine["failures"]), (1, 1.0, {"captcha": 1}))
        theirs = self.bob.get("/api/automation/ledger").json()["site_types"]["lever"]
        self.assertEqual((theirs["attempts"], theirs["failures"]), (0, {}))

    def test_default_is_off_with_no_rules(self):
        self.assertEqual(self.client.get("/api/automation/rules").json(), {"autonomy_enabled": False, "rules": []})

    def test_the_owner_creates_edits_and_deletes_a_rule(self):
        r = self.client.post("/api/automation/rules", json=self.RULE)
        self.assertEqual(r.status_code, 200)
        rule = r.json()
        self.assertEqual((rule["min_attempts"], rule["min_success_rate"], rule["enabled"]), (20, 0.9, True))
        self.assertEqual(self.client.put(f"/api/automation/rules/{rule['id']}", json={"min_score": 80}).json()["min_score"], 80)
        self.assertEqual(len(self.client.get("/api/automation/rules").json()["rules"]), 1)
        self.assertEqual(self.client.delete(f"/api/automation/rules/{rule['id']}").status_code, 200)
        self.assertEqual(self.client.get("/api/automation/rules").json()["rules"], [])

    def test_a_bad_rule_is_a_400_and_nothing_is_saved(self):
        for body in ({**self.RULE, "min_score": 500}, {**self.RULE, "site_type": "monster"},
                     {"site_type": "greenhouse"}, {**self.RULE, "extra": 1}):
            self.assertEqual(self.client.post("/api/automation/rules", json=body).status_code, 400, body)
        self.assertEqual(self.client.get("/api/automation/rules").json()["rules"], [])

    def test_a_non_owner_cannot_change_anything_but_can_read(self):
        rule = self.client.post("/api/automation/rules", json=self.RULE).json()
        self.assertEqual(self.bob.post("/api/automation/rules", json=self.RULE).status_code, 403)
        self.assertEqual(self.bob.put(f"/api/automation/rules/{rule['id']}", json={"min_score": 1}).status_code, 403)
        self.assertEqual(self.bob.delete(f"/api/automation/rules/{rule['id']}").status_code, 403)
        self.assertEqual(self.bob.put("/api/automation/autonomy", json={"autonomy_enabled": True}).status_code, 403)
        self.assertEqual(self.bob.get("/api/automation/rules").json(), {"autonomy_enabled": False, "rules": []})
        self.assertEqual(self.client.get("/api/automation/rules").json()["rules"][0]["min_score"], 70)

    def test_an_unknown_rule_is_a_404(self):
        self.assertEqual(self.client.put("/api/automation/rules/99", json={"min_score": 1}).status_code, 404)
        self.assertEqual(self.client.delete("/api/automation/rules/99").status_code, 404)

    def test_the_kill_switch(self):
        self.assertEqual(self.client.put("/api/automation/autonomy", json={"autonomy_enabled": True}).json(),
                         {"autonomy_enabled": True})
        self.assertTrue(self.client.get("/api/automation/rules").json()["autonomy_enabled"])
        self.assertEqual(self.client.put("/api/automation/autonomy", json={"autonomy_enabled": False}).json(),
                         {"autonomy_enabled": False})
        for bad in ({}, {"autonomy_enabled": "true"}, {"autonomy_enabled": 1}):
            self.assertEqual(self.client.put("/api/automation/autonomy", json=bad).status_code, 400)

    def test_an_auto_disabled_rule_comes_back_on_when_the_owner_re_enables_it(self):
        rule = self.client.post("/api/automation/rules", json=self.RULE).json()
        AutonomyStore(1, cfg.JOBS_DB_PATH).disable_rules_for("greenhouse", "captcha failure on greenhouse")
        got = self.client.get("/api/automation/rules").json()["rules"][0]
        self.assertEqual((got["enabled"], got["disabled_reason"]), (False, "captcha failure on greenhouse"))
        back = self.client.put(f"/api/automation/rules/{rule['id']}", json={"enabled": True}).json()
        self.assertEqual((back["enabled"], back["disabled_reason"]), (True, ""))


if __name__ == "__main__":
    unittest.main()
