# tests/test_answer_fill.py
"""
tests/test_answer_fill.py — the answer bank at work (applier/answers.py and
its wiring through the Orchestrator and the form code):

  * the policies: always fills, ask pre-fills and asks you to confirm, once is
    used for one application only and not kept;
  * this application's own answers beat the bank; the bank beats the LLM's
    guess; profile facts only fill what the LLM left empty;
  * the remember flag: what an answer you give or edit is saved as;
  * per-user isolation, and a broken bank never stopping an application.

Run from the project root:   python3 -m unittest discover -s tests -v

Real throwaway SQLite files; no browser, no network.
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

from applier import answers as ans
from applier import universal
from applier.answers import AnswerContext, MemoryState
from core.config import cfg
from db.answer_bank_db import AnswerBank, normalize_question
from db.jobs_db import JobsDB


class Field:
    """A FormField look-alike."""
    def __init__(self, label, key="", field_type="text", options=None, db_value="", manual=False):
        self.label, self.field_key, self.field_type = label, key, field_type
        self.options, self.db_value, self.needs_manual, self.placeholder = options or [], db_value, manual, ""


class Analysis:
    def __init__(self, *fields):
        self.fields = list(fields)

    @property
    def missing(self):
        return [f for f in self.fields if f.needs_manual]

    @property
    def fillable(self):
        return [f for f in self.fields if f.db_value]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "jobs.db"
        self.alice = AnswerBank(1, self.path)
        self.bob = AnswerBank(2, self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def ctx(self, company="Acme Corp", profile=None, state=None, bank=None):
        return AnswerContext(bank or self.alice, company, profile=profile,
                             state=state if state is not None else MemoryState())


# ── the policies ──────────────────────────────────────────────────

class PolicyTests(Base):
    def test_always_fills_the_field_and_it_is_no_longer_missing(self):
        e = self.alice.add("Do you hold a valid passport?", "Yes")
        f = Field("Do you hold a valid passport?", "passport", manual=True)
        a = Analysis(f)
        hits = self.ctx().apply(a)
        self.assertEqual((f.db_value, f.needs_manual), ("Yes", False))
        self.assertEqual(a.missing, [])
        self.assertEqual(len(hits), 1)
        self.assertEqual({k: hits[0][k] for k in ("action", "source", "policy", "confidence", "entry_id")},
                         {"action": "filled", "source": "user", "policy": "always", "confidence": 1.0,
                          "entry_id": e["id"]})

    def test_a_use_is_counted_once_per_application_however_many_passes(self):
        e = self.alice.add("Valid passport", "Yes")
        state = MemoryState()
        for _ in range(3):                                   # first pass, refill, submit
            self.ctx(state=state).apply(Analysis(Field("Valid passport?", "p")))
        self.assertEqual(self.alice.get(e["id"])["uses"], 1)
        self.assertTrue(self.alice.get(e["id"])["last_used_at"])
        self.ctx(state=MemoryState()).apply(Analysis(Field("Valid passport?", "p")))   # another application
        self.assertEqual(self.alice.get(e["id"])["uses"], 2)

    def test_ask_prefills_the_answer_and_marks_it_as_needing_confirmation(self):
        e = self.alice.add("Comfortable with night shifts?", "Yes", policy="ask")
        f = Field("Are you comfortable with night shifts?", "night", manual=False)
        a = Analysis(f)
        state = MemoryState()
        hits = self.ctx(state=state).apply(a)
        self.assertEqual((f.db_value, f.needs_manual), ("Yes", True))
        self.assertEqual(a.missing, [f], "it becomes a question for you...")
        self.assertEqual(a.fillable, [f], "...with the suggestion already typed into the form")
        self.assertEqual(hits[0]["action"], "suggested")
        self.assertEqual(state.bank_confirm["night"],
                         {"question": "Are you comfortable with night shifts?", "suggested": "Yes",
                          "entry_id": e["id"]})
        self.assertEqual(self.alice.get(e["id"])["uses"], 1)

    def test_ask_invents_a_field_key_when_the_form_has_none(self):
        self.alice.add("Comfortable with night shifts?", "Yes", policy="ask")
        f = Field("Are you comfortable with night shifts?", "")
        state = MemoryState()
        self.ctx(state=state).apply(Analysis(f))
        self.assertEqual(f.field_key, "comfortable_night_shifts")
        self.assertIn("comfortable_night_shifts", state.bank_confirm)

    def test_once_is_used_for_this_application_only_and_not_kept_in_the_bank(self):
        e = self.alice.add("Referral code", "ABC123", policy="once")
        state = MemoryState()
        f = Field("Referral code", "ref", manual=True)
        hits = self.ctx(state=state).apply(Analysis(f))
        self.assertEqual((f.db_value, f.needs_manual, hits[0]["action"]), ("ABC123", False, "once"))
        self.assertIsNone(self.alice.get(e["id"]), "a once-answer is not stored")
        self.assertEqual(self.alice.entries(), [])
        self.assertEqual(state.application_answers, {"referral code": "ABC123"})

        # the refill and submit passes of THIS application still have it...
        f2 = Field("Referral code", "ref", manual=True)
        self.ctx(state=state).apply(Analysis(f2))
        self.assertEqual((f2.db_value, f2.needs_manual), ("ABC123", False))
        # ...no other application ever does
        f3 = Field("Referral code", "ref", manual=True)
        self.assertEqual(self.ctx(state=MemoryState()).apply(Analysis(f3)), [])
        self.assertEqual((f3.db_value, f3.needs_manual), ("", True))

    def test_a_once_answer_is_saved_with_the_application_before_the_entry_is_deleted(self):
        self.alice.add("Referral code", "ABC123", policy="once")
        order = []
        state = MemoryState()
        real_save, real_delete = state.save, self.alice.delete
        state.save = lambda: (order.append("save"), real_save())[1]
        self.alice.delete = lambda i: (order.append("delete"), real_delete(i))[1]
        self.ctx(state=state).apply(Analysis(Field("Referral code", "ref")))
        self.assertLess(order.index("save"), order.index("delete"),
                        "a crash between the two must not lose the answer")

    def test_a_once_entry_is_consumed_by_the_first_application_that_uses_it(self):
        self.alice.add("Referral code", "ABC123", policy="once")
        first, second = Field("Referral code", "r"), Field("Referral code", "r")
        self.ctx(state=MemoryState()).apply(Analysis(first))
        self.ctx(state=MemoryState()).apply(Analysis(second))
        self.assertEqual((first.db_value, second.db_value), ("ABC123", ""))

    def test_this_applications_own_answer_beats_any_saved_one(self):
        e = self.alice.add("Expected CTC", "20 LPA", policy="ask")
        state = MemoryState()
        state.application_answers = {"expected ctc": "25 LPA"}
        state.bank_confirm = {"ctc": {"question": "Expected CTC", "suggested": "20 LPA", "entry_id": e["id"]}}
        f = Field("Expected CTC", "ctc", manual=True)
        hits = self.ctx(state=state).apply(Analysis(f))
        self.assertEqual((f.db_value, f.needs_manual), ("25 LPA", False))
        self.assertEqual(hits[0]["source"], "application")
        self.assertNotIn("ctc", state.bank_confirm, "answering clears the confirmation")
        self.assertEqual(self.alice.get(e["id"])["uses"], 0)

    def test_the_bank_beats_the_llms_guess_and_saves_you_being_asked(self):
        self.alice.add("Expected CTC", "20 LPA")
        guess = Field("Expected CTC", "ctc", db_value="Negotiable")
        unknown = Field("Expected CTC", "ctc2", manual=True)
        self.ctx().apply(Analysis(guess, unknown))
        self.assertEqual((guess.db_value, unknown.db_value, unknown.needs_manual), ("20 LPA", "20 LPA", False))

    def test_a_profile_fact_only_fills_what_the_llm_left_empty(self):
        profile = {"notice_period_label": "30 days"}
        mapped = Field("Notice period", "np", db_value="Immediate")
        empty = Field("What is your notice period?", "np2", manual=True)
        hits = self.ctx(profile=profile).apply(Analysis(mapped, empty))
        self.assertEqual(mapped.db_value, "Immediate", "the LLM already mapped it from the profile")
        self.assertEqual((empty.db_value, empty.needs_manual), ("30 days", False))
        self.assertEqual([(h["source"], h["entry_id"]) for h in hits], [("profile", "profile:notice_period_label")])

    def test_your_own_answer_beats_the_profile_fact(self):
        self.alice.add("Notice period", "15 days")
        f = Field("Notice period", "np", manual=True)
        self.ctx(profile={"notice_period_label": "30 days"}).apply(Analysis(f))
        self.assertEqual(f.db_value, "15 days")

    def test_a_company_answer_applies_to_that_company_only(self):
        self.alice.add("Why this company?", "Because Acme", scope="company", company="Acme")
        for company, want in (("Acme Corp", "Because Acme"), ("Globex", "")):
            f = Field("Why this company?", "why", manual=True)
            self.ctx(company=company).apply(Analysis(f))
            self.assertEqual(f.db_value, want, company)

    def test_select_and_radio_answers_must_be_one_of_the_options(self):
        self.alice.add("Willing to relocate", "yes")
        self.alice.add("Work mode", "Maybe")
        sel = Field("Willing to relocate?", "reloc", "select", ["Yes", "No"], manual=True)
        rad = Field("Willing to relocate?", "reloc2", "radio", ["No", "YES"], manual=True)
        bad = Field("Work mode?", "mode", "select", ["Remote", "Hybrid"], manual=True)
        txt = Field("Work mode?", "mode2", "text", [], manual=True)
        hits = self.ctx().apply(Analysis(sel, rad, bad, txt))
        self.assertEqual((sel.db_value, rad.db_value), ("Yes", "YES"), "the option's own spelling is used")
        self.assertEqual((bad.db_value, bad.needs_manual), ("", True), "an answer the control can't express is not forced")
        self.assertEqual(txt.db_value, "Maybe")
        self.assertEqual(len(hits), 3)

    def test_fit_to_options(self):
        self.assertEqual(ans.fit_to_options("immediate", ["Immediately", "30 days"]), "Immediately")
        self.assertEqual(ans.fit_to_options(" no ", ["Yes", "No"]), "No")
        self.assertIsNone(ans.fit_to_options("Maybe", ["Yes", "No"]))
        self.assertIsNone(ans.fit_to_options("30 days", ["15 days", "60 days"]))
        self.assertIsNone(ans.fit_to_options("", ["Yes"]))

    def test_files_blank_labels_and_unknown_questions_are_left_alone(self):
        self.alice.add("Resume", "should not be used")
        self.alice.add("Referral", "x")
        f_file = Field("Resume", "cv", "file", manual=True)
        f_blank = Field("   ", "b", manual=True)
        f_unknown = Field("Something nobody has answered before", "u", manual=True)
        self.assertEqual(self.ctx().apply(Analysis(f_file, f_blank, f_unknown)), [])
        for f in (f_file, f_blank, f_unknown):
            self.assertEqual((f.db_value, f.needs_manual), ("", True), f.label)

    def test_one_failing_field_does_not_stop_the_others(self):
        self.alice.add("Valid passport", "Yes")
        self.alice.add("Expected CTC", "20 LPA")
        real = self.alice.resolve
        def flaky(question, *a, **k):
            if "passport" in question.lower():
                raise RuntimeError("database is locked")
            return real(question, *a, **k)
        self.alice.resolve = flaky
        a, b = Field("Valid passport", "p", manual=True), Field("Expected CTC", "c", manual=True)
        with self.assertLogs("applier.answers", "ERROR"):
            hits = self.ctx().apply(Analysis(a, b))
        self.assertEqual((a.db_value, b.db_value, len(hits)), ("", "20 LPA", 1))

    def test_the_state_is_saved_only_when_something_changed(self):
        state = MemoryState()
        self.ctx(state=state).apply(Analysis(Field("Nothing matches this", "x")))
        self.assertEqual(state.saves, 0)
        self.alice.add("Valid passport", "Yes")
        self.ctx(state=state).apply(Analysis(Field("Valid passport", "p")))
        self.assertGreaterEqual(state.saves, 1)

    def test_summarize(self):
        self.assertEqual(ans.summarize([]), {"filled": 0, "suggested": 0, "once": 0})
        hits = [{"action": "filled"}, {"action": "filled"}, {"action": "suggested"}, {"action": "once"}]
        self.assertEqual(ans.summarize(hits), {"filled": 2, "suggested": 1, "once": 1})

    def test_fuzzy_matches_are_applied_with_their_confidence_reported(self):
        self.alice.add("What is your expected salary?", "20 LPA")
        f = Field("What is your expected salry?", "s", manual=True)
        hit = self.ctx().apply(Analysis(f))[0]
        self.assertEqual(f.db_value, "20 LPA")
        self.assertLess(hit["confidence"], 1.0)
        self.assertGreaterEqual(hit["confidence"], 0.88)


# ── the remember flag ─────────────────────────────────────────────

class RememberFlagTests(Base):
    def test_parse_remember(self):
        for value, want in [(None, "auto"), (True, "auto"), ("auto", "auto"), ("Default", "auto"), ("yes", "auto"),
                            ("true", "auto"), (False, "once"), ("once", "once"), ("no", "once"),
                            ("NEVER", "once"), ("global", "global"), (" Company ", "company")]:
            with self.subTest(value=value):
                self.assertEqual(ans.parse_remember(value), want)
        for bad in ("sometimes", "", 3, [], {}, "globally"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                ans.parse_remember(bad)

    def test_remember_modes_for_one_flag_or_a_mapping(self):
        self.assertEqual(ans.remember_modes(None, ["a", "b"]), {"a": "auto", "b": "auto"})
        self.assertEqual(ans.remember_modes("once", ["a", "b"]), {"a": "once", "b": "once"})
        self.assertEqual(ans.remember_modes({"a": "company", "default": "global"}, ["a", "b"]),
                         {"a": "company", "b": "global"})
        self.assertEqual(ans.remember_modes({"a": False, "*": "once"}, ["a", "b"]), {"a": "once", "b": "once"})
        self.assertEqual(ans.remember_modes({"a": "global"}, ["a", "b"]), {"a": "global", "b": "auto"})
        self.assertEqual(ans.remember_modes("once", []), {})
        with self.assertRaises(ValueError):
            ans.remember_modes({"a": "global", "typo_key": "sometimes"}, ["a"])    # a bad value anywhere is refused

    def test_long_free_text(self):
        self.assertTrue(ans.is_long_free_text("textarea", "short"))
        self.assertTrue(ans.is_long_free_text("TEXTAREA", ""))
        self.assertTrue(ans.is_long_free_text("text", "x" * 200))
        self.assertFalse(ans.is_long_free_text("text", "x" * 199))
        self.assertFalse(ans.is_long_free_text("text", "  " + "x" * 150 + "  "))
        self.assertFalse(ans.is_long_free_text("", "30 days"))

    def test_choose_scope(self):
        c = ans.choose_scope
        # auto: global unless the question names the company or it is long free text
        self.assertEqual(c("auto", "Expected CTC", "20 LPA", "Acme Corp"), "global")
        self.assertEqual(c("auto", "Why do you want to join Acme?", "Because", "Acme Corp"), "company")
        self.assertEqual(c("auto", "Why do you want to join us?", "x" * 200, "Acme Corp"), "company")
        self.assertEqual(c("auto", "Describe a project", "short", "Acme Corp", "textarea"), "company")
        self.assertEqual(c("auto", "Describe a project", "x" * 199, "Acme Corp", "text"), "global")
        # explicit flags win
        self.assertEqual(c("global", "Why Acme?", "x" * 500, "Acme Corp", "textarea"), "global")
        self.assertEqual(c("company", "Expected CTC", "20", "Acme Corp"), "company")
        # nothing to scope to
        for mode in ("auto", "company"):
            self.assertEqual(c(mode, "Why Acme?", "x" * 500, "", "textarea"), "global")
            self.assertEqual(c(mode, "Why Acme?", "x", "   "), "global")

    def test_remember_answer_saves_per_the_flag(self):
        e = ans.remember_answer(self.alice, "Expected CTC", "20 LPA", "Acme Corp", "auto")
        self.assertEqual((e["scope"], e["company"], e["policy"], e["source"], e["answer"]),
                         ("global", "", "always", "user", "20 LPA"))
        e = ans.remember_answer(self.alice, "Why Acme?", "Because", "Acme Corp", "auto")
        self.assertEqual((e["scope"], e["company"]), ("company", "Acme Corp"))
        e = ans.remember_answer(self.alice, "Notice period", "15 days", "Acme Corp", "company")
        self.assertEqual((e["scope"], e["company"]), ("company", "Acme Corp"))
        e = ans.remember_answer(self.alice, "Why Acme?", "Global anyway", "Acme Corp", "global")
        self.assertEqual((e["scope"], e["company"]), ("global", ""))
        self.assertEqual(len(self.alice.entries()), 4)

    def test_remember_once_stores_nothing(self):
        self.assertIsNone(ans.remember_answer(self.alice, "Expected CTC", "20 LPA", "Acme", "once"))
        self.assertEqual(self.alice.entries(), [])

    def test_remembering_again_updates_in_place_and_edits_win(self):
        a = ans.remember_answer(self.alice, "Expected CTC", "20 LPA", "Acme", "auto")
        b = ans.remember_answer(self.alice, "expected ctc?", "22 LPA", "Globex", "auto")
        self.assertEqual((a["id"], b["id"], b["answer"]), (a["id"], a["id"], "22 LPA"))
        self.assertEqual(len(self.alice.entries()), 1)

    def test_what_cannot_be_stored_raises(self):
        for q, a in (("", "x"), ("?!", "x"), ("Expected CTC", "  ")):
            with self.subTest(q=q, a=a), self.assertRaises(ValueError):
                ans.remember_answer(self.alice, q, a, "Acme")
        with self.assertRaises(ValueError):
            ans.remember_answer(self.alice, "Expected CTC", "x", "Acme", "sometimes")


# ── through the Orchestrator ──────────────────────────────────────

class FakeProfile:
    """A profile that records what the Orchestrator does to it."""
    def __init__(self, flat=None):
        self.flat = flat or {}
        self.pending, self.set_many_calls, self.resolve_calls = [], [], []

    def summary(self): return {"_flat": dict(self.flat), "_answered": dict(self.flat)}
    def flag_missing(self, **kw):
        self.pending.append({"id": len(self.pending) + 1, "field_key": kw["field_key"],
                             "field_label": kw["field_label"], "field_type": kw.get("field_type", "text"),
                             "options": kw.get("options", ""), "placeholder_used": kw.get("placeholder", "")})
    def get_pending_missing(self, job_id): return list(self.pending)
    def set_many(self, answers, source=""): self.set_many_calls.append(dict(answers))
    def resolve_missing(self, field_id, value, store=True): self.resolve_calls.append((field_id, value, store))


class Ctx:
    pages = []
    def new_page(self): return object()
    def close(self): pass


class Playwright:
    def __enter__(self): return object()
    def __exit__(self, *a): return False


class QuietSettings:
    def read(self, key, default=""): return default


class OrchestratorTests(Base):
    """The real Orchestrator, with the browser and form code faked: the form
    'page' is a list of Field factories, run through the real
    universal._apply_answers exactly as the form code does."""

    def setUp(self):
        super().setUp()
        self._sessions = cfg.SESSIONS_DIR
        cfg.SESSIONS_DIR = self.dir / "sessions"
        cfg.SESSIONS_DIR.mkdir()
        self.jobs = JobsDB(path=self.path, user_id=1)
        self.bobs_jobs = JobsDB(path=self.path, user_id=2)
        self.form = lambda: []                       # () -> [Field]; set per test
        self.refill = lambda: []
        self.seen = []                               # fields as they stood when the form was filled
        self.n = 0

        def fake_run(page, job, profile, emit=None, answers=None, **kw):
            a = Analysis(*self.form())
            universal._apply_answers(answers, a, emit)
            self.seen.append([(f.label, f.db_value, f.needs_manual) for f in a.fields])
            return {"success": True, "screenshots": [], "missing_fields": a.missing, "error": ""}

        def fake_iter2(page, job, profile, keys, emit=None, answers=None, **kw):
            a = Analysis(*self.refill())
            universal._apply_answers(answers, a, emit)
            self.seen.append([(f.label, f.db_value, f.needs_manual) for f in a.fields])
            return {"success": True, "screenshots": [], "missing_fields": [], "error": ""}

        for p in (mock.patch("applier.session.Orchestrator._settings", staticmethod(lambda: QuietSettings())),
                  mock.patch("applier.preflight.fetch_page", lambda url: (200, "")),
                  mock.patch("playwright.sync_api.sync_playwright", lambda: Playwright()),
                  mock.patch("applier.universal.run_application", fake_run),
                  mock.patch("applier.universal.run_iteration2", fake_iter2),
                  mock.patch("urllib.request.urlopen", side_effect=AssertionError("network"))):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        cfg.SESSIONS_DIR = self._sessions
        super().tearDown()

    def job(self, db=None, company="Acme Corp"):
        db = db or self.jobs
        self.n += 1
        jid = db.add({"url": f"https://example.com/{self.n}", "title": f"Role {self.n}",
                      "company": company, "source": "linkedin"})
        self.assertIsNotNone(jid)
        return jid

    def orchestrator(self, jid, profile=None, db=None):
        from applier.session import ApplicationSession, Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        o.job_id = jid
        o.job = (db or self.jobs).get_by_id(jid)
        o.profile_db = profile or FakeProfile()
        o.jobs_db = JobsDB(path=self.path)
        o.session = ApplicationSession.load(jid) or ApplicationSession(
            job_id=jid, job_url="u", job_title="t", company=o.job["company"], source="linkedin")
        o._open_browser = lambda pw: Ctx()
        return o

    def park(self, jid, fields, profile=None, db=None):
        """Run the first pass over `fields` and return the orchestrator."""
        self.form = lambda: [Field(*f) if isinstance(f, tuple) else f for f in fields]
        o = self.orchestrator(jid, profile, db)
        o.run_iter1()
        return o

    def steps(self, jid, db=None):
        ev = (db or self.jobs).get_application(jid)["events"]
        return [e["detail"] for e in ev if e["kind"] == "step"]

    # first pass ----------------------------------------------------

    def test_an_always_answer_fills_silently_and_the_job_goes_straight_to_review(self):
        e = self.alice.add("Do you hold a valid passport?", "Yes")
        jid = self.job()
        o = self.park(jid, [("Do you hold a valid passport?", "passport", "text", [], "", True)])
        self.assertEqual(self.jobs.get_stage(jid), "ready_for_review")
        self.assertEqual(self.seen[0], [("Do you hold a valid passport?", "Yes", False)])
        step = [d for d in self.steps(jid) if d["step"] == "used_saved_answers"][0]
        self.assertEqual((step["message"], step["count"]), ("Filled 1 field from your saved answers", 1))
        self.assertEqual(self.alice.get(e["id"])["uses"], 1)
        self.assertEqual(o.session.missing_prompts, [])

    def test_an_ask_answer_parks_the_job_with_the_suggestion_to_confirm(self):
        e = self.alice.add("Comfortable with night shifts?", "Yes", policy="ask")
        jid = self.job()
        o = self.park(jid, [("Are you comfortable with night shifts?", "night", "text", [], "", False),
                            ("Expected CTC", "ctc", "text", [], "", True)])
        row = self.jobs.get_by_id(jid)
        self.assertEqual((row["stage"], row["blocked_reason"]), ("needs_you", "question"))
        night, ctc = o.session.missing_prompts
        self.assertEqual((night["needs_confirmation"], night["suggested"], night["entry_id"]),
                         (True, "Yes", e["id"]))
        self.assertNotIn("needs_confirmation", ctc, "an ordinary question is not a confirmation")
        self.assertNotIn("suggested", ctc)
        self.assertEqual(self.seen[0][0], ("Are you comfortable with night shifts?", "Yes", True),
                         "the form already holds the suggestion")
        step = [d for d in self.steps(jid) if d["step"] == "saved_answers_to_confirm"][0]
        self.assertEqual((step["message"], step["count"]), ("1 saved answer to confirm", 1))
        # the state survives in the application's own session file
        from applier.session import ApplicationSession
        self.assertEqual(ApplicationSession.load(jid).bank_confirm["night"]["suggested"], "Yes")

    def test_a_once_answer_is_spent_on_the_application_and_the_later_passes_still_have_it(self):
        e = self.alice.add("Referral code", "ABC123", policy="once")
        jid = self.job()
        o = self.park(jid, [("Referral code", "ref", "text", [], "", True)])
        self.assertEqual(self.jobs.get_stage(jid), "ready_for_review")
        self.assertIsNone(self.alice.get(e["id"]))
        # the submit pass: a fresh Orchestrator, session reloaded from disk
        o2 = self.orchestrator(jid)
        a = Analysis(Field("Referral code", "ref", manual=True))
        o2._answers_kw({"_flat": {}})["answers"].apply(a)
        self.assertEqual(a.fields[0].db_value, "ABC123")

    def test_profile_facts_are_offered_to_the_profile_owner_only(self):
        profile = {"notice_period_label": "30 days"}
        field = [("What is your notice period?", "np", "text", [], "", True)]
        mine = self.job(self.jobs)
        self.park(mine, field, FakeProfile(profile))
        self.assertEqual(self.seen[-1], [("What is your notice period?", "30 days", False)])
        theirs = self.job(self.bobs_jobs)
        self.park(theirs, field, FakeProfile(profile), self.bobs_jobs)
        self.assertEqual(self.seen[-1], [("What is your notice period?", "", True)],
                         "the machine's profile is the owner's, not Bob's")

    # whose bank ----------------------------------------------------

    def test_each_job_uses_its_owners_bank_only(self):
        self.alice.add("Do you hold a valid passport?", "alice says yes")
        self.bob.add("Do you hold a valid passport?", "bob says no")
        field = [("Do you hold a valid passport?", "p", "text", [], "", True)]
        self.park(self.job(self.jobs), field)
        self.assertEqual(self.seen[-1][0][1], "alice says yes")
        self.park(self.job(self.bobs_jobs), field, db=self.bobs_jobs)
        self.assertEqual(self.seen[-1][0][1], "bob says no")
        self.bob.delete(self.bob.entries()[0]["id"])
        self.park(self.job(self.bobs_jobs), field, db=self.bobs_jobs)
        self.assertEqual(self.seen[-1][0][1], "", "Bob does not fall back to Alice's answer")

    def test_a_legacy_job_with_no_owner_uses_the_first_accounts_bank(self):
        self.alice.add("Do you hold a valid passport?", "yes")
        legacy = JobsDB(path=self.path)                                    # unscoped: user_id stays NULL
        jid = legacy.add({"url": "https://example.com/legacy", "title": "Old", "company": "Acme", "source": "x"})
        self.assertIsNone(legacy.get_by_id(jid)["user_id"])
        self.form = lambda: [Field("Do you hold a valid passport?", "p", manual=True)]
        self.orchestrator(jid, db=legacy).run_iter1()
        self.assertEqual(self.seen[-1][0][1], "yes")

    def test_a_broken_bank_never_stops_an_application(self):
        jid = self.job()
        self.form = lambda: [Field("Expected CTC", "ctc", manual=False, db_value="18")]
        with mock.patch("db.answer_bank_db.AnswerBank", side_effect=RuntimeError("disk full")):
            with self.assertLogs("applier.session", "ERROR"):
                self.orchestrator(jid).run_iter1()
        self.assertEqual(self.jobs.get_stage(jid), "ready_for_review")
        self.assertEqual(self.seen[-1], [("Expected CTC", "18", False)])

    # giving / editing an answer: the remember flag ----------------

    def answered(self, fields, answers, remember=None, profile=None):
        """Park the job on `fields`, then answer; returns (orchestrator, profile, job id)."""
        profile = profile or FakeProfile()
        jid = self.job()
        o = self.park(jid, fields, profile)
        self.assertEqual(self.jobs.get_stage(jid), "needs_you")
        self.refill = lambda: [Field(f[0], f[1], f[2]) for f in fields]
        o.run_iter2(answers, remember=remember)
        return o, profile, jid

    def test_by_default_an_answer_is_remembered_globally(self):
        o, profile, jid = self.answered([("Expected CTC", "ctc", "text", [], "", True)], {"ctc": "20 LPA"})
        (e,) = self.alice.entries()
        self.assertEqual((e["question_raw"], e["answer"], e["scope"], e["company"], e["policy"], e["source"]),
                         ("Expected CTC", "20 LPA", "global", "", "always", "user"))
        self.assertEqual(profile.set_many_calls, [{"ctc": "20 LPA"}], "the existing profile write is unchanged")
        self.assertEqual(profile.resolve_calls, [(1, "20 LPA", True)])
        self.assertEqual(o.session.application_answers, {"expected ctc": "20 LPA"})
        self.assertEqual(self.jobs.get_stage(jid), "ready_for_review")
        # ...and it fills the same question on the next application, for any company
        self.park(self.job(company="Globex"), [("What is your expected CTC?", "c", "text", [], "", True)])
        self.assertEqual(self.seen[-1][0][1:], ("20 LPA", False))

    def test_a_question_that_names_the_company_is_remembered_for_that_company_only(self):
        self.answered([("Why do you want to join Acme?", "why", "text", [], "", True)], {"why": "Great product"})
        (e,) = self.alice.entries()
        self.assertEqual((e["scope"], e["company"]), ("company", "Acme Corp"))
        self.park(self.job(company="Acme Corporation"), [("Why do you want to join Acme?", "w", "text", [], "", True)])
        self.assertEqual(self.seen[-1][0][1], "Great product")
        self.park(self.job(company="Globex"), [("Why do you want to join Acme?", "w", "text", [], "", True)])
        self.assertEqual(self.seen[-1][0][1], "", "never offered to a different company")

    def test_long_free_text_is_remembered_for_the_company(self):
        essay = "I have built reliable systems for years. " * 6
        self.answered([("Tell us about a project you are proud of", "proj", "textarea", [], "", True),
                       ("Anything else", "else", "text", [], "", True)],
                      {"proj": "short answer", "else": essay})
        scopes = {e["question_raw"]: e["scope"] for e in self.alice.entries()}
        self.assertEqual(scopes, {"Tell us about a project you are proud of": "company", "Anything else": "company"})

    def test_an_explicit_flag_overrides_the_automatic_scope(self):
        self.answered([("Why do you want to join Acme?", "why", "text", [], "", True),
                       ("Expected CTC", "ctc", "text", [], "", True)],
                      {"why": "Great product", "ctc": "20 LPA"},
                      remember={"why": "global", "ctc": "company"})
        scopes = {e["question_raw"]: (e["scope"], e["company"]) for e in self.alice.entries()}
        self.assertEqual(scopes, {"Why do you want to join Acme?": ("global", ""),
                                  "Expected CTC": ("company", "Acme Corp")})

    def test_remember_once_uses_the_answer_here_and_stores_it_nowhere(self):
        fields = [("Referral code", "ref", "text", [], "", True), ("Expected CTC", "ctc", "text", [], "", True)]
        o, profile, jid = self.answered(fields, {"ref": "ABC123", "ctc": "20 LPA"},
                                        remember={"ref": "once", "default": "global"})
        self.assertEqual([e["question_raw"] for e in self.alice.entries()], ["Expected CTC"])
        self.assertEqual(profile.set_many_calls, [{"ctc": "20 LPA"}], "a once answer never reaches the profile")
        self.assertEqual(profile.resolve_calls, [(1, "ABC123", False), (2, "20 LPA", True)])
        self.assertEqual(o.session.application_answers, {"referral code": "ABC123", "expected ctc": "20 LPA"})
        self.assertEqual(self.seen[-1][0], ("Referral code", "ABC123", False), "this application's refill still has it")
        self.park(self.job(), [("Referral code", "ref", "text", [], "", True)])
        self.assertEqual(self.seen[-1][0], ("Referral code", "", True), "no other application does")

    def test_a_single_false_flag_means_once_for_every_answer(self):
        o, profile, _ = self.answered([("Expected CTC", "ctc", "text", [], "", True)], {"ctc": "20 LPA"}, remember=False)
        self.assertEqual((self.alice.entries(), profile.set_many_calls), ([], [{}]))
        self.assertEqual(o.session.application_answers, {"expected ctc": "20 LPA"})

    def test_confirming_a_suggestion_unchanged_adds_nothing_and_editing_updates_the_entry(self):
        e = self.alice.add("Comfortable with night shifts?", "Yes", policy="ask")
        fields = [("Are you comfortable with night shifts?", "night", "text", [], "", False)]
        self.answered(fields, {"night": "Yes"})
        got = self.alice.get(e["id"])
        self.assertEqual((len(self.alice.entries()), got["answer"], got["updated_at"]),
                         (1, "Yes", e["updated_at"]), "confirmed as it was: nothing new to save")

        e2 = self.alice.add("Comfortable with weekend work?", "Yes", policy="ask")
        self.answered([("Are you comfortable with weekend work?", "wk", "text", [], "", False)], {"wk": "Only Saturday"})
        got = self.alice.get(e2["id"])
        self.assertEqual((got["answer"], got["source"], got["policy"]), ("Only Saturday", "user", "ask"))
        self.assertEqual(len(self.alice.entries()), 2, "edited in place, not duplicated")

    def test_editing_a_global_suggestion_for_this_company_adds_a_company_answer_and_keeps_the_global_one(self):
        g = self.alice.add("Comfortable with weekend work?", "Yes", policy="ask")
        self.answered([("Are you comfortable with weekend work?", "wk", "text", [], "", False)],
                      {"wk": "No, not for Acme"}, remember="company")
        self.assertEqual(self.alice.get(g["id"])["answer"], "Yes")
        company = [x for x in self.alice.entries() if x["scope"] == "company"]
        self.assertEqual([(x["answer"], x["company"]) for x in company], [("No, not for Acme", "Acme Corp")])

    def test_a_bad_remember_flag_is_refused_before_anything_is_saved(self):
        jid = self.job()
        profile = FakeProfile()
        o = self.park(jid, [("Expected CTC", "ctc", "text", [], "", True)], profile)
        for bad in ("sometimes", {"ctc": "nope"}, 5):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                o.run_iter2({"ctc": "20 LPA"}, remember=bad)
        self.assertEqual((profile.set_many_calls, profile.resolve_calls, self.alice.entries()), ([], [], []))
        self.assertEqual(o.session.application_answers, {})
        self.assertEqual(self.jobs.get_stage(jid), "needs_you")

    def test_a_blank_answer_is_not_remembered(self):
        o, _, _ = self.answered([("Expected CTC", "ctc", "text", [], "", True),
                                 ("Referral", "ref", "text", [], "", True)], {"ctc": "20 LPA", "ref": ""})
        self.assertEqual([e["question_raw"] for e in self.alice.entries()], ["Expected CTC"])

    def test_an_answer_is_saved_to_the_job_owners_bank(self):
        profile = FakeProfile()
        jid = self.job(self.bobs_jobs)
        o = self.park(jid, [("Expected CTC", "ctc", "text", [], "", True)], profile, self.bobs_jobs)
        self.refill = lambda: [Field("Expected CTC", "ctc")]
        o.run_iter2({"ctc": "35 LPA"})
        self.assertEqual([e["answer"] for e in self.bob.entries()], ["35 LPA"])
        self.assertEqual(self.alice.entries(), [])

    def test_a_question_with_no_meaningful_words_is_answered_but_not_remembered(self):
        o, _, jid = self.answered([("? *", "q", "text", [], "", True)], {"q": "something"})
        self.assertEqual(self.alice.entries(), [])
        self.assertEqual(self.jobs.get_stage(jid), "ready_for_review")


# ── the real form code calls the context ──────────────────────────

class FakePage:
    url = "https://example.com/apply"
    def goto(self, *a, **k): pass
    def inner_text(self, sel): return "Step 1 of 1"
    def evaluate(self, js): return []


class FormCodeTests(Base):
    def setUp(self):
        super().setUp()
        self.filled, self.emitted = [], []
        self.fields = []
        def analyze(**k):
            self.analysis = Analysis(*self.fields)
            return self.analysis
        def execute(page, analysis, **k):
            self.filled.append([(f.label, f.db_value, f.needs_manual) for f in analysis.fields])
            return [f for f in analysis.fields if f.db_value], []
        for target, repl in [("detect_captcha", lambda p: False), ("is_login_wall", lambda p: False),
                             ("_page_has_form", lambda p: True), ("_wait_for_form", lambda p, *a: None),
                             ("extract_fields", lambda p: "form text"), ("analyze", analyze),
                             ("execute", execute), ("click_next", lambda p, a: False),
                             ("_screenshot", lambda *a: ""), ("_is_review_page", lambda p: False),
                             ("page_text_from_extraction", lambda t: t)]:
            p = mock.patch.object(universal, target, repl)
            p.start()
            self.addCleanup(p.stop)
        for p in (mock.patch("applier.universal.time.sleep", lambda s: None),
                  mock.patch("urllib.request.urlopen", side_effect=AssertionError("network"))):
            p.start()
            self.addCleanup(p.stop)

    JOB = {"job_id": "j1", "url": "https://example.com/job", "title": "Dev", "company": "Acme Corp",
           "source": "linkedin"}

    def emit(self, event, data):
        self.emitted.append((event, data))

    def test_the_bank_is_applied_to_the_analysed_page_before_anything_is_filled(self):
        self.alice.add("Valid passport", "Yes")
        self.alice.add("Night shifts", "Yes", policy="ask")
        self.fields = [Field("Valid passport?", "p", manual=True), Field("Night shifts?", "n"),
                       Field("Expected CTC", "c", manual=True)]
        result = universal.run_application(FakePage(), self.JOB, {}, emit=self.emit, answers=self.ctx())
        self.assertEqual(self.filled[0], [("Valid passport?", "Yes", False), ("Night shifts?", "Yes", True),
                                          ("Expected CTC", "", True)])
        self.assertEqual([f.label for f in result["missing_fields"]], ["Night shifts?", "Expected CTC"])
        steps = [(d["step"], d["message"]) for ev, d in self.emitted if ev == "step"]
        self.assertIn(("used_saved_answers", "Filled 1 field from your saved answers"), steps)
        self.assertIn(("saved_answers_to_confirm", "1 saved answer to confirm"), steps)
        self.assertLess([s for s, _ in steps].index("used_saved_answers"), [s for s, _ in steps].index("filled_fields"))

    def test_without_a_context_the_form_is_filled_exactly_as_before(self):
        self.alice.add("Valid passport", "Yes")
        self.fields = [Field("Valid passport?", "p", manual=True)]
        universal.run_application(FakePage(), self.JOB, {}, emit=self.emit)
        self.assertEqual(self.filled[0], [("Valid passport?", "", True)])
        self.assertFalse([d for ev, d in self.emitted if ev == "step" and d["step"] == "used_saved_answers"])

    def test_a_context_that_blows_up_is_ignored_and_the_form_still_gets_filled(self):
        self.fields = [Field("Expected CTC", "c", db_value="18")]
        bad = mock.Mock()
        bad.apply.side_effect = RuntimeError("bank exploded")
        with self.assertLogs("applier.universal", "ERROR"):
            result = universal.run_application(FakePage(), self.JOB, {}, emit=self.emit, answers=bad)
        self.assertTrue(result["success"])
        self.assertEqual(self.filled[0], [("Expected CTC", "18", False)])

    def test_nothing_is_reported_when_the_bank_had_nothing_to_say(self):
        self.fields = [Field("Nothing like this in the bank", "x", manual=True)]
        universal.run_application(FakePage(), self.JOB, {}, emit=self.emit, answers=self.ctx())
        self.assertFalse([d for ev, d in self.emitted if ev == "step"
                          and d["step"] in ("used_saved_answers", "saved_answers_to_confirm")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
