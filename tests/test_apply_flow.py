# tests/test_apply_flow.py
"""
tests/test_apply_flow.py — the apply flow end to end, with the browser faked:

  * pre-flight runs before any browser work and a failed rule skips the job
    with an event saying why (applier/preflight.py through the Orchestrator);
  * every failure is classified and the class decides the stage: a blocker
    parks the job in needs_you with that reason, timeout/unknown fail it;
  * step events are recorded on the job's timeline while it works, never
    touch the job itself, and are per-user;
  * the real form code (applier/universal.py) emits those steps and detects
    validation errors, driven with a fake page.

Run from the project root:   python3 -m unittest discover -s tests -v

Real throwaway SQLite files; no browser, no network (urllib's urlopen is
booby-trapped in every test).
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

from applier import failures, preflight
from applier import universal
from core.config import cfg
from db import lifecycle
from db.jobs_db import JobsDB


class Settings:
    def __init__(self, **kw):
        self.values = {k: str(v) for k, v in kw.items()}

    def read(self, key, default=""):
        return self.values.get(key, default)


class FakeProfile:
    def __init__(self, pending=()):
        self.pending = list(pending)
    def summary(self): return {}
    def flag_missing(self, **kw): pass
    def get_pending_missing(self, job_id): return list(self.pending)
    def set_many(self, *a, **k): pass
    def resolve_missing(self, *a, **k): pass


OK_RESULT = {"success": True, "screenshots": [], "missing_fields": [], "error": ""}


def failing(error):
    return {"success": False, "screenshots": [], "missing_fields": [], "error": error}


class Base(unittest.TestCase):
    """A throwaway DB, an Orchestrator builder, and fakes for everything that
    would otherwise touch a browser or the network."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "jobs.db"
        self._sessions = cfg.SESSIONS_DIR
        cfg.SESSIONS_DIR = self.dir / "sessions"
        cfg.SESSIONS_DIR.mkdir()
        self.alice = JobsDB(path=self.path, user_id=1)
        self.bob = JobsDB(path=self.path, user_id=2)
        self.settings = Settings()
        self.calls = []                       # ordered log of "fetch" / "browser" / "run_application"
        self.fetch_status, self.fetch_body = 200, ""
        self.n = 0
        self.app_results = []                 # queue of results run_application returns
        self.app_hook = None                  # optional fn(page, job, profile, emit) run first
        self.app_kwargs = {}

        def fetch(url):
            self.calls.append("fetch")
            return self.fetch_status, self.fetch_body

        class Playwright:
            def __enter__(inner): self.calls.append("browser"); return object()
            def __exit__(inner, *a): return False

        def run_application(page, job, profile, emit=None, **kw):
            self.calls.append("run_application")
            self.app_kwargs = kw                 # e.g. the answer-bank context the Orchestrator passes
            if self.app_hook:
                self.app_hook(page, job, profile, emit)
            r = self.app_results.pop(0) if self.app_results else OK_RESULT
            if isinstance(r, BaseException):
                raise r
            return r

        for p in (mock.patch("applier.session.Orchestrator._settings", staticmethod(lambda: self.settings)),
                  mock.patch("applier.preflight.fetch_page", fetch),
                  mock.patch("playwright.sync_api.sync_playwright", lambda: Playwright()),
                  mock.patch("applier.universal.run_application", run_application),
                  mock.patch("urllib.request.urlopen",
                             side_effect=AssertionError("a test touched the network"))):
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        cfg.SESSIONS_DIR = self._sessions
        self.tmp.cleanup()

    def add(self, db=None, *, title=None, company=None, source="linkedin", salary=""):
        db = db or self.alice
        self.n += 1
        jid = db.add({"url": f"https://example.com/{db.user_id}/{self.n}",
                      "title": title if title is not None else f"Role {self.n}",
                      "company": company if company is not None else f"Company {self.n}",
                      "source": source, "salary": salary})
        self.assertIsNotNone(jid)
        return jid

    def orchestrator(self, jid, pending=(), open_browser=None):
        from applier.session import ApplicationSession, Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        o.job_id = jid
        o.job = self.alice.get_by_id(jid)
        o.profile_db = FakeProfile(pending)
        o.jobs_db = JobsDB(path=self.path)               # unscoped, like the real one
        o.session = ApplicationSession(job_id=jid, job_url="u", job_title="t",
                                       company="c", source="linkedin")
        o._open_browser = open_browser or (lambda pw: type("Ctx", (), {
            "pages": [], "new_page": lambda s: object(), "close": lambda s: None})())
        return o

    def events(self, jid, db=None):
        return (db or self.alice).get_application(jid)["events"]

    def transitions(self, jid):
        return [e for e in self.events(jid) if e["kind"] != "step"]

    def steps(self, jid):
        return [e for e in self.events(jid) if e["kind"] == "step"]


# ── pre-flight, through the Orchestrator ──────────────────────────

class PreflightIntegrationTests(Base):
    def setup_rule(self, rule):
        """A job that fails exactly `rule`; returns its id."""
        if rule == "already_applied":
            other = self.add(source="naukri", title="Backend Engineer", company="Acme")
            self.alice.transition(other, "submitted", actor="user")
            return self.add(source="linkedin", title="Backend Engineer", company="Acme")
        if rule == "blacklisted_company":
            self.settings.values["company_blacklist"] = "Acme"
            return self.add(company="Acme Corp")
        if rule == "salary_below_floor":
            self.settings.values["min_salary_lpa"] = "10"
            return self.add(salary="6-8 LPA")
        if rule == "job_closed":
            self.fetch_status = 404
            return self.add()
        raise AssertionError(rule)

    def test_each_rule_skips_the_job_with_an_event_saying_why_and_opens_no_browser(self):
        expected = {"already_applied": "Already applied", "blacklisted_company": "blacklist",
                    "salary_below_floor": "below your floor", "job_closed": "no longer open"}
        for rule in preflight.RULES:
            with self.subTest(rule=rule):
                self.calls.clear()
                jid = self.setup_rule(rule)
                s = self.orchestrator(jid).run_iter1()
                self.assertEqual(self.alice.get_stage(jid), "skipped")
                self.assertEqual(s.state, "skipped")
                self.assertNotIn("browser", self.calls)
                self.assertNotIn("run_application", self.calls)

                skip = self.transitions(jid)[-1]
                self.assertEqual((skip["from_stage"], skip["to_stage"], skip["actor"]),
                                 ("preparing", "skipped", "agent"))
                self.assertEqual(skip["detail"]["preflight"], rule)
                self.assertIn(expected[rule].lower(), skip["detail"]["note"].lower())
                self.assertEqual(self.alice.get_by_id(jid)["error_message"], skip["detail"]["note"],
                                 "the reason is visible on the job itself too")
                self.assertEqual([t["to_stage"] for t in self.transitions(jid)],
                                 ["found", "preparing", "skipped"])

    def test_a_passing_job_is_checked_before_the_browser_opens_and_then_runs_normally(self):
        jid = self.add(salary="20 LPA")
        self.settings.values.update(min_salary_lpa="10", company_blacklist="Initech")
        self.orchestrator(jid).run_iter1()
        self.assertEqual(self.calls, ["fetch", "browser", "run_application"])
        self.assertEqual(self.alice.get_stage(jid), "ready_for_review")

    def test_settings_are_read_fresh_on_every_run(self):
        first = self.add(company="Acme One")
        self.orchestrator(first).run_iter1()
        self.assertEqual(self.alice.get_stage(first), "ready_for_review")
        self.settings.values["company_blacklist"] = "Acme"
        second = self.add(company="Acme Two")
        self.orchestrator(second).run_iter1()
        self.assertEqual(self.alice.get_stage(second), "skipped")

    def test_refilling_after_answers_is_checked_too(self):
        jid = self.add(company="Acme")
        self.alice.transition(jid, "preparing")
        self.alice.transition(jid, "needs_you", reason="question")
        self.settings.values["company_blacklist"] = "Acme"       # added while the job was parked
        self.calls.clear()
        o = self.orchestrator(jid)
        o.session.missing_prompts = []
        o.run_iter2({"ctc": "10 LPA"})
        self.assertEqual(self.alice.get_stage(jid), "skipped")
        self.assertNotIn("browser", self.calls)

    def test_a_submit_is_refused_when_the_same_role_went_out_another_way_meanwhile(self):
        jid = self.add(source="linkedin", title="Backend Engineer", company="Acme")
        self.alice.transition(jid, "preparing"); self.alice.transition(jid, "ready_for_review")
        other = self.add(source="naukri", title="Backend Engineer", company="Acme")
        self.alice.transition(other, "submitted", actor="user")
        self.calls.clear()
        with mock.patch("applier.session.Orchestrator._dry_run_enabled", staticmethod(lambda: False)), \
             mock.patch("applier.universal.run_submit",
                        side_effect=AssertionError("a duplicate application was submitted")):
            s = self.orchestrator(jid).submit()
        self.assertEqual(self.alice.get_stage(jid), "skipped")
        self.assertEqual(s.state, "skipped")
        self.assertEqual(self.calls, [], "a duplicate is caught locally: no request, no browser")
        self.assertEqual(self.alice.get_by_id(jid)["applied_at"], "")

    def test_a_job_that_cannot_be_worked_on_is_not_even_checked(self):
        jid = self.add()
        with self.alice._conn() as c:
            c.execute("UPDATE jobs SET stage='rejected' WHERE job_id=?", (jid,))
        with self.assertRaises(lifecycle.IllegalTransition):
            self.orchestrator(jid).run_iter1()
        self.assertEqual(self.calls, [])

    def test_a_crashing_preflight_never_blocks_the_job(self):
        jid = self.add()
        with mock.patch("applier.preflight.run", side_effect=RuntimeError("settings db locked")):
            with self.assertLogs("applier.session", "ERROR"):
                self.orchestrator(jid).run_iter1()
        self.assertEqual(self.alice.get_stage(jid), "ready_for_review")


# ── classification, through the Orchestrator ──────────────────────

NAUKRI_CHAT = ("Naukri opened a follow-up screening chat after Apply — this isn't automated yet. "
               "Please finish answering it manually in the browser window; the job has NOT been "
               "marked as applied.")


class ClassificationIntegrationTests(Base):
    BLOCKERS = [
        ("CAPTCHA detected — skipping", "captcha"),
        ("CAPTCHA after clicking apply", "captcha"),
        ("Login wall — page requires authentication and no login was detected", "login_required"),
        (NAUKRI_CHAT, "naukri_chat_screening"),
        ("No Apply button found — job may already be applied to, expired, or removed", "job_closed"),
        ("Could not find submit button", "unsupported_form"),
        ("Validation error — the site rejected the answers: Enter a valid email", "validation_error"),
    ]
    RETRYABLE = [
        ("Navigation failed: Page.goto: Timeout 30000ms exceeded.", "timeout"),
        ("Something odd happened", "unknown"),
    ]

    def run_failure(self, message):
        jid = self.add()
        self.app_results = [failing(message)]
        s = self.orchestrator(jid).run_iter1()
        return jid, s

    def test_a_blocker_parks_the_job_in_needs_you_with_its_class_as_the_reason(self):
        for message, cat in self.BLOCKERS:
            with self.subTest(cat=cat, message=message[:50]):
                jid, s = self.run_failure(message)
                row = self.alice.get_by_id(jid)
                self.assertEqual((row["stage"], row["blocked_reason"]), ("needs_you", cat))
                move = self.transitions(jid)[-1]
                self.assertEqual((move["from_stage"], move["to_stage"]), ("preparing", "needs_you"))
                self.assertEqual(move["detail"]["classified"], cat)
                self.assertEqual(move["detail"]["error"], message[:300])
                self.assertEqual((s.state, s.error), ("failed", message))

    def test_a_parked_job_says_it_is_waiting_for_you(self):
        for message, cat in self.BLOCKERS:
            with self.subTest(cat=cat):
                jid, _ = self.run_failure(message)
                last = self.events(jid)[-1]
                self.assertEqual((last["kind"], last["detail"]["step"], last["detail"]["reason"]),
                                 ("step", "waiting_for_you", cat))
                self.assertEqual(last["detail"]["message"], failures.WAITING_MESSAGES[cat])
                self.assertEqual((last["from_stage"], last["to_stage"]), ("needs_you", "needs_you"))

    def test_timeout_and_unknown_fail_the_job_and_record_the_class_for_the_retry(self):
        for message, cat in self.RETRYABLE:
            with self.subTest(cat=cat):
                jid, _ = self.run_failure(message)
                row = self.alice.get_by_id(jid)
                self.assertEqual((row["stage"], row["error_message"], row["blocked_reason"]),
                                 ("failed", message, ""))
                move = self.transitions(jid)[-1]
                self.assertEqual(move["detail"]["classified"], cat)
                self.assertFalse([e for e in self.steps(jid) if e["detail"]["step"] == "waiting_for_you"])

    def test_exceptions_are_classified_too(self):
        cases = [(TimeoutError("Timeout 30000ms exceeded"), "failed", "timeout"),
                 (TimeoutError(), "failed", "timeout"),         # no message at all: only the type says so
                 (ValueError("boom"), "failed", "unknown"),
                 (RuntimeError("CAPTCHA wall"), "needs_you", "captcha")]
        for exc, stage, cat in cases:
            with self.subTest(exc=type(exc).__name__):
                jid = self.add()
                self.app_results = [exc]
                with self.assertLogs("applier.session", "ERROR"):          # the crash is logged
                    self.orchestrator(jid).run_iter1()
                self.assertEqual(self.alice.get_stage(jid), stage)
                self.assertEqual(self.transitions(jid)[-1]["detail"]["classified"], cat)

    def test_a_browser_that_will_not_launch_is_classified_as_well(self):
        for message, cat in (("Executable doesn't exist at /x", "unknown"),
                             ("Timeout 180000ms exceeded", "timeout")):
            with self.subTest(cat=cat):
                jid = self.add()
                def broken(pw, message=message):
                    raise RuntimeError(message)
                self.orchestrator(jid, open_browser=broken).run_iter1()
                move = self.transitions(jid)[-1]
                self.assertEqual((move["to_stage"], move["detail"]["classified"]), ("failed", cat))
                self.assertIn("Browser launch failed", move["detail"]["error"])

    def test_a_browser_launch_that_dies_with_a_bare_timeout_is_a_timeout(self):
        jid = self.add()
        def broken(pw):
            raise TimeoutError()                                  # message is empty: "Browser launch failed: "
        self.orchestrator(jid, open_browser=broken).run_iter1()
        self.assertEqual(self.transitions(jid)[-1]["detail"]["classified"], "timeout")

    def test_a_submit_that_hits_a_naukri_chat_is_parked_not_failed(self):
        jid = self.add(source="naukri")
        self.alice.transition(jid, "preparing"); self.alice.transition(jid, "ready_for_review")
        with mock.patch("applier.session.Orchestrator._dry_run_enabled", staticmethod(lambda: False)), \
             mock.patch("applier.universal.run_submit", lambda *a, **k: failing(NAUKRI_CHAT)):
            self.orchestrator(jid).submit()
        row = self.alice.get_by_id(jid)
        self.assertEqual((row["stage"], row["blocked_reason"], row["applied_at"]),
                         ("needs_you", "naukri_chat_screening", ""))

    def test_a_blocked_job_can_be_started_again_once_you_have_fixed_it(self):
        jid, _ = self.run_failure("Login wall — page requires authentication and no login was detected")
        self.assertEqual(self.alice.get_stage(jid), "needs_you")
        self.app_results = [OK_RESULT]                   # you logged in; this time it works
        self.orchestrator(jid).run_iter1()
        row = self.alice.get_by_id(jid)
        self.assertEqual((row["stage"], row["blocked_reason"]), ("ready_for_review", ""))

    def test_missing_fields_still_park_the_job_as_a_question(self):
        jid = self.add()
        ff = mock.Mock(label="Expected CTC", field_key="ctc", field_type="text", options="", placeholder="")
        self.app_results = [{"success": True, "screenshots": [], "missing_fields": [ff], "error": ""}]
        pending = [{"id": 1, "field_key": "ctc", "field_label": "Expected CTC", "field_type": "text",
                    "options": "", "placeholder_used": ""}]
        self.orchestrator(jid, pending).run_iter1()
        row = self.alice.get_by_id(jid)
        self.assertEqual((row["stage"], row["blocked_reason"]), ("needs_you", "question"))
        last = self.events(jid)[-1]
        self.assertEqual((last["detail"]["step"], last["detail"]["reason"], last["detail"]["count"]),
                         ("waiting_for_you", "question", 1))


# ── step events ───────────────────────────────────────────────────

class StepEventTests(Base):
    def emit_script(self, page, job, profile, emit):
        emit("log", "Opening application")
        emit("step", {"step": "opened_page", "message": "Opened the job page"})
        emit("step", {"step": "found_apply_button", "message": "Found the Apply button"})
        emit("step", {"step": "form_page", "message": "Form page 2 of 4", "page": 2, "total": 4})
        emit("step", {"step": "filled_fields", "message": "Filled 7 fields", "count": 7, "unfilled": 1})

    def test_steps_arrive_on_the_timeline_in_order_while_the_job_is_preparing(self):
        jid = self.add()
        self.app_hook = self.emit_script
        self.orchestrator(jid).run_iter1()
        steps = self.steps(jid)
        self.assertEqual([e["detail"]["step"] for e in steps],
                         ["opened_page", "found_apply_button", "form_page", "filled_fields"])
        self.assertEqual([e["detail"]["message"] for e in steps],
                         ["Opened the job page", "Found the Apply button", "Form page 2 of 4", "Filled 7 fields"])
        self.assertEqual((steps[2]["detail"]["page"], steps[2]["detail"]["total"]), (2, 4))
        self.assertEqual((steps[3]["detail"]["count"], steps[3]["detail"]["unfilled"]), (7, 1))
        for e in steps:
            self.assertEqual((e["from_stage"], e["to_stage"], e["actor"]), ("preparing", "preparing", "agent"))
        order = [(e["kind"], e["to_stage"]) for e in self.events(jid)]
        self.assertEqual(order, [("created", "found"), ("transition", "preparing")]
                         + [("step", "preparing")] * 4 + [("transition", "ready_for_review")])

    def test_steps_are_forwarded_to_the_live_progress_channel_too(self):
        jid = self.add()
        self.app_hook = self.emit_script
        seen = []
        self.orchestrator(jid).run_iter1(emit=lambda ev, data: seen.append((ev, data)))
        self.assertIn(("step", {"step": "form_page", "message": "Form page 2 of 4", "page": 2, "total": 4}), seen)

    def test_a_step_never_changes_the_job_or_the_old_stage_history(self):
        jid = self.add()
        self.alice.transition(jid, "preparing")
        before = self.alice.get_by_id(jid)
        self.alice.record_step(jid, "opened_page", "Opened the job page")
        self.alice.record_step(jid, "filled_fields", "Filled 3 fields", {"count": 3})
        after = self.alice.get_by_id(jid)
        for key in ("stage", "stage_entered_at", "blocked_reason", "status", "updated_at",
                    "error_message", "applied_at"):
            self.assertEqual(before[key], after[key], key)
        self.assertEqual(before["stages"], after["stages"])
        self.assertEqual(self.alice.list_applications()[0]["stage_entered_at"], before["stage_entered_at"])

    def test_a_step_event_cannot_be_mistaken_for_a_pipeline_stage_in_the_old_history(self):
        jid = self.add()
        self.alice.transition(jid, "submitted", actor="user")
        self.alice.record_step(jid, "note_to_self", "A step recorded while at a pipeline stage")
        stages = self.alice.get_by_id(jid)["stages"]
        self.assertEqual([s["stage"] for s in stages], ["applied"], "the step leaked into the old stages list")
        self.assertEqual(self.alice.get_pipeline()["applied"][0]["job_id"], jid)

    def test_steps_are_per_user_and_visible_in_the_live_stream_feed(self):
        a = self.add(self.alice); b = self.add(self.bob)
        self.alice.record_step(a, "opened_page", "Opened the job page")
        self.assertEqual([e["kind"] for e in self.alice.events_since(0)], ["created", "step"])
        self.assertEqual([e["kind"] for e in self.bob.events_since(0)], ["created"])
        with self.assertRaises(LookupError):
            self.bob.record_step(a, "opened_page", "x")              # someone else's job
        with self.assertRaises(LookupError):
            self.alice.record_step("nope", "opened_page", "x")
        self.assertEqual(len(self.events(b, self.bob)), 1)

    def test_bad_input_is_refused_and_long_messages_are_cut(self):
        jid = self.add()
        for bad in ("", "Opened Page", "1abc", "has space", "x" * 41, None, 5):
            with self.subTest(step=bad), self.assertRaises(ValueError):
                self.alice.record_step(jid, bad, "m")
        with self.assertRaises(ValueError):
            self.alice.record_step(jid, "opened_page", "m", actor="root")
        e = self.alice.record_step(jid, "opened_page", "m" * 1000)
        self.assertEqual(len(e["detail"]["message"]), 300)
        self.assertEqual(self.alice.record_step(jid, "opened_page", "x", actor="system")["actor"], "system")
        # the step code and message always win over same-named extra data
        e = self.alice.record_step(jid, "opened_page", "real", {"step": "evil", "message": "evil"})
        self.assertEqual((e["detail"]["step"], e["detail"]["message"]), ("opened_page", "real"))

    def test_a_failure_to_record_a_step_never_breaks_the_run(self):
        jid = self.add()
        self.app_hook = self.emit_script
        with mock.patch.object(JobsDB, "record_step", side_effect=RuntimeError("database is locked")):
            with self.assertLogs("applier.session", "WARNING"):
                self.orchestrator(jid).run_iter1()
        self.assertEqual(self.alice.get_stage(jid), "ready_for_review")

    def test_junk_step_payloads_from_the_form_code_are_ignored_safely(self):
        jid = self.add()
        def junk(page, job, profile, emit):
            emit("step", "not a dict")
            emit("step", {"message": "no code"})
            emit("step", {"step": "BAD CODE", "message": "x"})
        self.app_hook = junk
        with self.assertLogs("applier.session", "WARNING"):
            self.orchestrator(jid).run_iter1()
        self.assertEqual(self.steps(jid), [])
        self.assertEqual(self.alice.get_stage(jid), "ready_for_review")


# ── the real form code, with a fake page ──────────────────────────

class FakeButton:
    def is_visible(self): return True
    def click(self, **k): pass


class FakeContext:
    def expect_page(self, timeout=0):
        raise RuntimeError("no new tab opened")


class FakePage:
    """Just enough page for universal.py's real helpers. `texts` is the body
    text of each form page (what "Step 2 of 4" detection reads); `errors`
    maps a page index to the messages the site shows after we move to it."""
    url = "https://example.com/apply"

    def __init__(self, texts, errors=None):
        self.texts, self.errors, self.cur = texts, errors or {}, 0
        self.context = FakeContext()

    def goto(self, *a, **k): pass
    def query_selector(self, sel): return FakeButton()
    def inner_text(self, sel): return self.texts[min(self.cur, len(self.texts) - 1)]

    def evaluate(self, js):
        if js is universal._VALIDATION_JS:
            return self.errors.get(self.cur, [])
        return True


class Analysis:
    fields, fillable, missing = [], [], []
    def __init__(self): self.fields, self.fillable, self.missing = [], [], []


class UniversalStepTests(unittest.TestCase):
    def setUp(self):
        self.emitted = []
        self.filled_counts = [3, 5, 1]
        self.page_index = {"n": 0}
        state = {"first": True}

        def page_has_form(page):                 # a listing page first, a form after Apply is clicked
            if state["first"]:
                state["first"] = False
                return False
            return True

        def execute(page, analysis, **k):
            n = self.filled_counts[min(page.cur, len(self.filled_counts) - 1)]
            return [f"f{i}" for i in range(n)], ["x"] if page.cur == 0 else []

        def click_next(page, analysis):
            if page.cur < len(page.texts) - 1:
                page.cur += 1
                return True
            return False

        for target, repl in [("detect_captcha", lambda p: False), ("is_login_wall", lambda p: False),
                             ("_page_has_form", page_has_form), ("_wait_for_form", lambda p, *a: None),
                             ("extract_fields", lambda p: "some form text"),
                             ("analyze", lambda **k: Analysis()), ("execute", execute),
                             ("click_next", click_next), ("_screenshot", lambda *a: ""),
                             ("_is_review_page", lambda p: False),
                             ("page_text_from_extraction", lambda t: t),
                             ("_has_apply_button", lambda p: True)]:
            p = mock.patch.object(universal, target, repl)
            p.start()
            self.addCleanup(p.stop)
        sleep = mock.patch("applier.universal.time.sleep", lambda s: None)
        sleep.start()
        self.addCleanup(sleep.stop)
        trap = mock.patch("urllib.request.urlopen", side_effect=AssertionError("network"))
        trap.start()
        self.addCleanup(trap.stop)

    def emit(self, event, data):
        self.emitted.append((event, data))

    def steps(self):
        return [(d["step"], d["message"]) for ev, d in self.emitted if ev == "step"]

    def drive(self, page, source="linkedin", fn=universal.run_application):
        job = {"job_id": "j1", "url": "https://example.com/job", "title": "Dev", "company": "Acme",
               "source": source}
        return fn(page, job, {}, emit=self.emit) if fn is universal.run_application else \
            fn(page, job, {}, [], emit=self.emit)

    def test_a_three_page_form_reports_every_step_in_order_with_the_pages_own_numbering(self):
        page = FakePage(["Step 1 of 3 — your details", "Step 2 of 3 — experience", "Step 3 of 3 — review"])
        result = self.drive(page)
        self.assertTrue(result["success"])
        self.assertEqual(self.steps(), [
            ("opened_page", "Opened the job page"),
            ("found_apply_button", "Found the Apply button"),
            ("form_page", "Form page 1 of 3"), ("filled_fields", "Filled 3 fields"),
            ("form_page", "Form page 2 of 3"), ("filled_fields", "Filled 5 fields"),
            ("form_page", "Form page 3 of 3"), ("filled_fields", "Filled 1 field")])

    def test_step_payloads_carry_the_numbers(self):
        self.drive(FakePage(["Step 1 of 2", "Step 2 of 2"]))
        data = [d for ev, d in self.emitted if ev == "step"]
        self.assertEqual((data[2]["page"], data[2]["total"]), (1, 2))
        self.assertEqual((data[3]["count"], data[3]["unfilled"]), (3, 1))

    def test_without_a_step_x_of_y_marker_it_counts_pages_itself_and_omits_the_total(self):
        self.drive(FakePage(["Your details", "Experience"]))
        self.assertEqual([m for s, m in self.steps() if s == "form_page"], ["Form page 1", "Form page 2"])
        first = [d for ev, d in self.emitted if ev == "step" and d["step"] == "form_page"][0]
        self.assertIsNone(first["total"])

    def test_a_nonsense_progress_marker_is_not_trusted(self):
        for text in ("Step 9 of 3", "Step 0 of 3", "page 2 of 99"):
            with self.subTest(text=text):
                self.assertEqual(universal._form_progress(FakePage([text]), 0), (1, None))

    def test_naukri_reports_that_it_opened_the_page_and_found_the_button_and_nothing_more(self):
        self.drive(FakePage(["x"]), source="naukri")
        self.assertEqual(self.steps(), [("opened_page", "Opened the job page"),
                                        ("found_apply_button", "Found the Apply button")])

    def test_a_site_that_rejects_the_answers_fails_with_a_validation_message(self):
        page = FakePage(["Step 1 of 2", "Step 2 of 2"],
                        errors={1: ["Enter a valid email address", "Phone is required"]})
        result = self.drive(page)
        self.assertFalse(result["success"])
        self.assertEqual(result["error"], "Validation error — the site rejected the answers: "
                                          "Enter a valid email address; Phone is required")
        self.assertEqual(failures.classify(result["error"]), "validation_error")
        self.assertEqual(self.steps()[-1][0], "filled_fields", "it stopped at the page that failed")

    def test_validation_errors_are_read_defensively(self):
        class P:
            def __init__(self, v): self.v = v
            def evaluate(self, js):
                if isinstance(self.v, Exception):
                    raise self.v
                return self.v
        self.assertEqual(universal._validation_errors(P(["a", "b"])), ["a", "b"])
        self.assertEqual(universal._validation_errors(P([str(i) for i in range(9)])), list("01234"))
        for junk in (None, "oops", {"a": 1}, 5, RuntimeError("page closed")):
            self.assertEqual(universal._validation_errors(P(junk)), [], repr(junk))
        self.assertEqual(universal._validation_message(["a", "b", "c", "d"]),
                         "Validation error — the site rejected the answers: a; b; c")

    def test_no_errors_means_the_form_carries_on(self):
        self.assertTrue(self.drive(FakePage(["Step 1 of 2", "Step 2 of 2"], errors={0: ["stale"]}))["success"])

    def test_the_second_pass_and_the_final_submit_report_steps_too(self):
        page = FakePage(["Step 1 of 2", "Step 2 of 2"])
        with mock.patch.object(universal, "click_submit", lambda *a, **k: True):
            universal.run_iteration2(page, {"job_id": "j1", "url": "u", "title": "t", "company": "c",
                                            "source": "linkedin"}, {}, ["ctc"], emit=self.emit)
        names = [s for s, _ in self.steps()]
        self.assertEqual(names[0], "opened_page")
        self.assertEqual(self.steps()[0][1], "Re-opened the job page")
        self.assertIn("form_page", names)
        self.assertIn("filled_fields", names)

    def test_emitting_never_raises_even_if_the_listener_does(self):
        def bad(event, data):
            raise RuntimeError("listener broke")
        universal._step(bad, "opened_page", "x")                      # must not raise
        universal._step(None, "opened_page", "x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
