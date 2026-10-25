# tests/test_assist.py
"""
tests/test_assist.py — assist mode: the agent prepares everything, you click
Submit on the site yourself.

  * the setting: apply_mode (auto | assist) and per-site-type overrides, the
    built-in default of assist for LinkedIn and Indeed, what happens to values
    that cannot be read;
  * NO BROWSER CODE RUNS in assist mode: every browser entry point (Playwright,
    the Orchestrator's browser, the universal filler, the site adapters) is
    instrumented and must never be called - not for the first pass, not for a
    second pass, not when somebody tries to submit, and autonomy cannot approve
    an assist job; the kit is still prepared and the job lands in ready_for_review;
  * the answer pack: contents, ordering (profile, answer bank, kit; the order
    inside each), what is left out, per-user isolation;
  * the two actions: mark-applied -> submitted and mark-unavailable -> skipped,
    both as actor "user" with one event each carrying the note / reason.

The LLM is a fake; no network.  The HTTP routes' tests are in the last class and
skip without FastAPI.

Run from the project root:   python3 -m unittest discover -s tests -v
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

from applier import apply_mode, assist, pack
from applier.apply_mode import DEFAULT_OVERRIDES, clean_mode, dump_overrides, mode_for, parse_overrides
from applier.pack import MAX_ANSWERS, PROFILE_KEYS, build_pack, safe_url
from applier.session import ApplicationSession, State
from core.config import cfg
from db import lifecycle
from db.answer_bank_db import AnswerBank
from db.autonomy_db import AutonomyStore
from db.jobs_db import JobsDB
from db.kits_db import KitStore
from db.resumes_db import ResumeStore
from db.settings_db import SettingsDB

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except Exception:
    HAVE_FASTAPI = False


class Settings:
    """Anything with read(key, default), like SettingsDB."""
    def __init__(self, **values):
        self.values = {k: (v if isinstance(v, str) else str(v)) for k, v in values.items()}

    def read(self, key, default=""):
        return self.values.get(key, default)


PDF = b"%PDF-1.4\n% a resume\n"
PROFILE = {"_flat": {
    "full_name": "Priya Sharma", "first_name": "Priya", "last_name": "Sharma", "email": "priya@example.com",
    "phone": "+91 98765 43210", "location_city": "Bengaluru", "location_country": "India",
    "linkedin_url": "https://linkedin.com/in/priya", "github_url": "https://github.com/priya",
    "current_role": "Backend engineer", "experience_years": "6", "notice_period_label": "30 days",
    "expected_ctc_lpa": "40", "willing_to_relocate": "Yes", "highest_degree": "B.Tech",
    "authorized_to_work": "Yes", "require_sponsorship": "No",
    # never put in a pack:
    "gender": "Female", "date_of_birth": "01/02/1990", "veteran_status": "No", "disability_status": "No",
    "ethnicity": "Asian"}}


# ── the setting ───────────────────────────────────────────────────

class ApplyModeTests(unittest.TestCase):
    def test_linkedin_and_indeed_are_assist_by_default_everything_else_auto(self):
        s = Settings()
        self.assertEqual(mode_for(s, "linkedin_easy_apply"), "assist")
        self.assertEqual(mode_for(s, "indeed_apply"), "assist")
        for site in ("greenhouse", "lever", "workday", "ashby", "smartrecruiters", "naukri_one_click",
                     "naukri_chat", "generic"):
            self.assertEqual(mode_for(s, site), "auto", site)

    def test_the_built_in_default_is_what_is_seeded(self):
        self.assertEqual(DEFAULT_OVERRIDES, {"linkedin_easy_apply": "assist", "indeed_apply": "assist"})
        with tempfile.TemporaryDirectory() as tmp:
            db = SettingsDB(path=Path(tmp) / "settings.db")
            self.assertEqual(db.read("apply_mode"), "auto")
            self.assertEqual(db.read("apply_mode_overrides"), dump_overrides(DEFAULT_OVERRIDES))
            self.assertEqual(mode_for(db, "linkedin_easy_apply"), "assist")
            self.assertEqual(mode_for(db, "greenhouse"), "auto")

    def test_the_global_mode_applies_to_sites_without_an_override(self):
        s = Settings(apply_mode="assist")
        self.assertEqual(mode_for(s, "greenhouse"), "assist")
        self.assertEqual(mode_for(s, "naukri_one_click"), "assist")

    def test_an_override_beats_the_global_mode_both_ways(self):
        s = Settings(apply_mode="assist", apply_mode_overrides='{"greenhouse": "auto"}')
        self.assertEqual((mode_for(s, "greenhouse"), mode_for(s, "lever")), ("auto", "assist"))
        s = Settings(apply_mode="auto", apply_mode_overrides='{"lever": "assist"}')
        self.assertEqual((mode_for(s, "lever"), mode_for(s, "greenhouse")), ("assist", "auto"))

    def test_stored_overrides_replace_the_builtin_default_so_empty_puts_linkedin_on_auto(self):
        s = Settings(apply_mode_overrides="{}")
        self.assertEqual(mode_for(s, "linkedin_easy_apply"), "auto")
        self.assertEqual(mode_for(s, "indeed_apply"), "auto")
        s = Settings(apply_mode="assist", apply_mode_overrides="{}")
        self.assertEqual(mode_for(s, "linkedin_easy_apply"), "assist")

    def test_values_that_cannot_be_read_fail_safe(self):
        self.assertEqual(mode_for(Settings(apply_mode="maybe"), "greenhouse"), "assist")
        self.assertEqual(mode_for(Settings(apply_mode="  AUTO "), "greenhouse"), "auto")
        for bad in ("not json", "[]", '{"nope": "assist"}', '{"greenhouse": "sometimes"}', "5"):
            s = Settings(apply_mode_overrides=bad)
            self.assertEqual(mode_for(s, "indeed_apply"), "assist", f"{bad!r} falls back to the built-in overrides")
            self.assertEqual(mode_for(s, "greenhouse"), "auto")

    def test_settings_that_cannot_be_read_at_all_mean_assist(self):
        class Broken:
            def read(self, *a):
                raise RuntimeError("db gone")
        self.assertEqual(mode_for(Broken(), "greenhouse"), "assist")

    def test_validation(self):
        self.assertEqual(clean_mode(" Assist "), "assist")
        for bad in ("", "manual", None, 1, True):
            with self.assertRaises(ValueError):
                clean_mode(bad)
        self.assertEqual(parse_overrides('{"lever": "AUTO"}'), {"lever": "auto"})
        self.assertEqual(parse_overrides({"lever": "assist"}), {"lever": "assist"})
        for bad in ("not json", "[]", "null", '{"monster": "auto"}', '{"lever": "x"}', 3, None):
            with self.assertRaises(ValueError, msg=repr(bad)):
                parse_overrides(bad)
        self.assertEqual(dump_overrides({"b": "assist", "a": "auto"}), '{"a":"auto","b":"assist"}')


# ── shared fixtures ───────────────────────────────────────────────

class FakeLLM:
    def __init__(self):
        self.calls = []

    def chat_json(self, messages, system="", max_tokens=4096):
        self.calls.append("select")
        return {"variant_id": 0}

    def chat(self, messages, system="", max_tokens=4096, temperature=None, expect_json=False):
        kind = "summary" if "three sentences" in messages[0]["content"] else "letter"
        self.calls.append(kind)
        return "One. Two. Three." if kind == "summary" else "Dear Acme team, hello."


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "jobs.db"
        self.root = self.dir / "resumes"
        self.llm = FakeLLM()
        self.n = 0
        for p in (mock.patch("core.llm.chat", self.llm.chat), mock.patch("core.llm.chat_json", self.llm.chat_json),
                  mock.patch.object(cfg, "DATA_DIR", self.dir),
                  mock.patch.object(tempfile, "tempdir", str(self.dir)),
                  mock.patch("urllib.request.urlopen", side_effect=AssertionError("network"))):
            p.start()
            self.addCleanup(p.stop)

    def db(self, uid=1):
        return JobsDB(path=self.path, user_id=uid)

    def job(self, uid=1, url=None, company="Acme", stage="found", title="Backend Engineer"):
        self.n += 1
        db = self.db(uid)
        jid = db.add({"url": url or f"https://boards.greenhouse.io/acme/jobs/{uid}{self.n}", "title": f"{title} {self.n}",
                      "company": company, "source": "linkedin", "description": "Build Python services."})
        self.assertIsNotNone(jid)
        path = {"found": (), "queued": ("queued",), "preparing": ("preparing",),
                "needs_you": ("preparing", "needs_you"), "ready_for_review": ("preparing", "ready_for_review"),
                "submitted": ("preparing", "ready_for_review", "submitted"), "failed": ("preparing", "failed"),
                "skipped": ("skipped",)}[stage]
        for to in path:
            db.transition(jid, to, reason="question" if to == "needs_you" else None)
        return db.get_by_id(jid)

    def events(self, job, uid=None):
        return self.db(uid or job["user_id"]).get_application(job["job_id"])["events"]

    def transitions(self, job):
        return [e for e in self.events(job) if e["kind"] == "transition"]

    def steps(self, job):
        return [e["detail"].get("step") for e in self.events(job) if e["kind"] == "step"]


# ── no browser code runs in assist mode ───────────────────────────

class Playwright:
    def __init__(self, log): self.log = log
    def __enter__(self):
        self.log.append("sync_playwright")
        return object()
    def __exit__(self, *a): return False


class Ctx:
    pages = []
    def new_page(self): return object()
    def close(self): pass


class OrchestratorBase(Base):
    """A real Orchestrator whose every browser entry point is instrumented: each one
    appends to self.browser and otherwise behaves (a form that fills fine)."""

    def setUp(self):
        super().setUp()
        self._sessions = cfg.SESSIONS_DIR
        cfg.SESSIONS_DIR = self.dir / "sessions"
        cfg.SESSIONS_DIR.mkdir()
        self.addCleanup(lambda: setattr(cfg, "SESSIONS_DIR", self._sessions))
        self.browser = []                          # every browser-ish thing that ran, in order
        self.settings = Settings(quality_first="false", daily_apply_limit=0, min_delay_between_applies=0)
        ok = {"success": True, "screenshots": [], "missing_fields": [], "error": "", "needs_review": True,
              "review": {"fields": [], "url": "u"}, "dry_run": False}

        def instrumented(where):
            def run(*a, **kw):
                self.browser.append(where)
                return dict(ok)
            return run
        patches = [mock.patch("applier.session.Orchestrator._settings", staticmethod(lambda: self.settings)),
                   mock.patch("applier.session.Orchestrator._dry_run_enabled", staticmethod(lambda: False)),
                   mock.patch("applier.preflight.fetch_page", lambda url: (200, "")),
                   mock.patch("playwright.sync_api.sync_playwright", lambda: Playwright(self.browser))]
        for name in ("run_application", "run_iteration2", "run_submit"):
            patches.append(mock.patch(f"applier.universal.{name}", instrumented(f"universal.{name}")))
            patches.append(mock.patch(f"applier.adapters.runner.{name}", instrumented(f"adapters.{name}")))
        patches.append(mock.patch("applier.universal._site_type", instrumented("universal._site_type")))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def orchestrator(self, job):
        from applier.session import Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        o.job_id, o.job = job["job_id"], self.db(job["user_id"]).get_by_id(job["job_id"])
        o.profile_db = mock.Mock(summary=lambda: PROFILE, get_pending_missing=lambda job_id: [])
        o.jobs_db = JobsDB(path=self.path)
        o.session = ApplicationSession.load(job["job_id"]) or ApplicationSession(
            job_id=job["job_id"], job_url=job["url"], job_title="t", company=job["company"], source="linkedin")

        def open_browser(pw):
            self.browser.append("Orchestrator._open_browser")
            return Ctx()
        o._open_browser = open_browser
        return o

    def stage(self, job):
        return self.db(job["user_id"]).get_stage(job["job_id"])


class NoBrowserInAssistTests(OrchestratorBase):
    LINKEDIN = "https://www.linkedin.com/jobs/view/3700000001"
    INDEED   = "https://in.indeed.com/viewjob?jk=abc123"

    def assist_pass(self, url):
        job = self.job(url=url, stage="preparing")
        self.orchestrator(job).run_iter1()
        return job

    def test_the_instrumentation_does_catch_browser_code_in_auto_mode(self):
        job = self.job(stage="preparing")                      # a Greenhouse URL: auto
        self.orchestrator(job).run_iter1()
        self.assertEqual(self.browser[:3], ["sync_playwright", "Orchestrator._open_browser", "adapters.run_application"])

    def test_linkedin_runs_no_browser_code_and_lands_in_ready_for_review(self):
        job = self.assist_pass(self.LINKEDIN)
        self.assertEqual(self.browser, [])
        self.assertEqual(self.stage(job), "ready_for_review")

    def test_indeed_runs_no_browser_code_and_lands_in_ready_for_review(self):
        job = self.assist_pass(self.INDEED)
        self.assertEqual(self.browser, [])
        self.assertEqual(self.stage(job), "ready_for_review")

    def test_no_adapter_and_no_universal_filler_either(self):
        for url in (self.LINKEDIN, self.INDEED):
            self.assist_pass(url)
        self.assertEqual([b for b in self.browser if "universal" in b or "adapters" in b], [])

    def test_a_site_on_auto_still_gets_the_browser(self):
        self.settings = Settings(quality_first="false", daily_apply_limit=0)
        job = self.job(url="https://jobs.lever.co/acme/ab12cd34-0000-4aaa-8bbb-1234567890ab", stage="preparing")
        self.orchestrator(job).run_iter1()
        self.assertIn("sync_playwright", self.browser)

    def test_the_global_setting_puts_every_site_on_assist(self):
        self.settings = Settings(apply_mode="assist", quality_first="false", daily_apply_limit=0)
        job = self.job(stage="preparing")                      # Greenhouse
        self.orchestrator(job).run_iter1()
        self.assertEqual((self.browser, self.stage(job)), ([], "ready_for_review"))

    def test_an_override_can_put_one_site_type_on_auto_under_a_global_assist(self):
        self.settings = Settings(apply_mode="assist", apply_mode_overrides='{"greenhouse": "auto"}',
                                 quality_first="false", daily_apply_limit=0)
        self.orchestrator(self.job(stage="preparing")).run_iter1()
        self.assertIn("sync_playwright", self.browser)
        self.browser.clear()
        self.orchestrator(self.job(url="https://jobs.lever.co/a/1", stage="preparing")).run_iter1()
        self.assertEqual(self.browser, [])

    def test_clearing_the_overrides_puts_linkedin_back_on_auto(self):
        self.settings = Settings(apply_mode_overrides="{}", quality_first="false", daily_apply_limit=0)
        self.assist_pass(self.LINKEDIN)
        self.assertIn("sync_playwright", self.browser)

    def test_the_mode_is_read_when_the_pass_runs(self):
        a = self.job(url=self.LINKEDIN, stage="preparing")
        self.settings = Settings(apply_mode_overrides='{"linkedin_easy_apply": "auto"}', quality_first="false",
                                 daily_apply_limit=0)
        self.orchestrator(a).run_iter1()
        self.assertIn("sync_playwright", self.browser)
        self.browser.clear()
        self.settings = Settings(quality_first="false", daily_apply_limit=0)
        b = self.job(url=self.INDEED, stage="preparing")
        self.orchestrator(b).run_iter1()
        self.assertEqual(self.browser, [])

    def test_unreadable_settings_mean_assist_not_a_browser(self):
        class Broken:
            def read(self, *a): raise RuntimeError("settings db gone")
        self.settings = Broken()
        job = self.job(stage="preparing")
        self.orchestrator(job).run_iter1()
        self.assertEqual((self.browser, self.stage(job)), ([], "ready_for_review"))

    def test_settings_that_cannot_even_be_opened_mean_assist_not_a_browser(self):
        job = self.job(stage="preparing")
        with mock.patch("applier.session.Orchestrator._settings", staticmethod(lambda: (_ for _ in ()).throw(RuntimeError("no db")))), \
             self.assertLogs("applier.session", "ERROR"):
            self.orchestrator(job).run_iter1()
        self.assertEqual((self.browser, self.stage(job)), ([], "ready_for_review"))

    def test_the_timeline_says_what_happened_and_shows_no_browsing(self):
        job = self.assist_pass(self.LINKEDIN)
        steps = self.steps(job)
        self.assertIn("assist_ready", steps)
        for browsing in ("opened_page", "filled_fields", "found_fields", "screenshot"):
            self.assertNotIn(browsing, steps)
        last = self.transitions(job)[-1]
        self.assertEqual((last["from_stage"], last["to_stage"], last["actor"]), ("preparing", "ready_for_review", "agent"))
        self.assertTrue(last["detail"]["assist"])
        (ready,) = [e for e in self.events(job) if e["detail"].get("step") == "assist_ready"]
        self.assertIn("apply on the site yourself", ready["detail"]["message"])
        self.assertGreater(ready["detail"]["fields"], 0)

    def test_the_apply_session_is_parked_for_review_with_no_screenshots(self):
        job = self.assist_pass(self.LINKEDIN)
        s = ApplicationSession.load(job["job_id"])
        self.assertEqual((s.state, s.screenshots), (State.REVIEW.value, []))

    def test_the_kit_is_still_prepared_without_a_browser(self):
        job = self.assist_pass(self.LINKEDIN)
        kit = KitStore(1, self.path).current(job["job_id"])
        self.assertEqual((kit["status"], kit["summary"], kit["cover_letter"]), ("ready", "One. Two. Three.", "Dear Acme team, hello."))
        self.assertEqual(self.browser, [])

    def test_a_kit_that_fails_does_not_stop_the_job_reaching_review(self):
        with mock.patch("applier.kit.prepare_kit", side_effect=RuntimeError("kit exploded")), \
             self.assertLogs("applier.session", "ERROR"):
            job = self.assist_pass(self.LINKEDIN)
        self.assertEqual((self.browser, self.stage(job)), ([], "ready_for_review"))

    def test_a_pack_that_cannot_be_built_does_not_stop_it_either(self):
        with mock.patch("applier.pack.build_pack", side_effect=RuntimeError("pack exploded")), \
             self.assertLogs("applier.session", "ERROR"):
            job = self.assist_pass(self.LINKEDIN)
        self.assertEqual(self.stage(job), "ready_for_review")

    def test_a_second_pass_in_assist_mode_runs_no_browser_either(self):
        job = self.job(url=self.LINKEDIN, stage="needs_you")
        self.orchestrator(job).run_iter2({"some_question": "an answer"})
        self.assertEqual((self.browser, self.stage(job)), ([], "ready_for_review"))

    def test_nobody_can_make_the_agent_submit_in_assist_mode(self):
        job = self.job(url=self.LINKEDIN, stage="ready_for_review")
        o = self.orchestrator(job)
        o.submit()
        self.assertEqual(self.browser, [])
        self.assertEqual(self.stage(job), "ready_for_review")
        self.assertIn("assist_submit_blocked", self.steps(job))
        self.assertIsNone(self.db().get_by_id(job["job_id"])["applied_at"] or None)

    def test_a_submit_that_slips_in_with_actor_autonomy_is_refused_too(self):
        job = self.job(url=self.LINKEDIN, stage="ready_for_review")
        self.orchestrator(job).submit(None, None, actor="autonomy")
        self.assertEqual((self.browser, self.stage(job)), ([], "ready_for_review"))

    def test_autonomy_cannot_approve_an_assist_job_and_the_ledger_stays_empty(self):
        store = AutonomyStore(1, self.path)
        store.set_enabled(True)
        store.add_rule({"site_type": "*", "min_score": 0, "max_per_day": 9, "min_attempts": 1,
                        "min_success_rate": 0.1, "require_all_answers_known": False})
        for i in range(3):
            store.record_review_reached(f"h{i}", "linkedin_easy_apply")
            store.close_attempt(f"h{i}", "approved_unchanged")
        job = self.assist_pass(self.LINKEDIN)
        self.assertEqual((self.browser, self.stage(job)), ([], "ready_for_review"))
        st = store.site_stats("linkedin_easy_apply")
        self.assertEqual((st["attempts"], st["auto_approved"], st["pending"]), (3, 0, 0), "no attempt was recorded")
        self.assertEqual([e for e in self.events(job) if e["actor"] == "autonomy"], [])

    def test_the_queue_worker_runs_an_assist_job_without_a_browser_and_without_pacing(self):
        from applier.queue_worker import QueueWorker
        job = self.job(url=self.LINKEDIN, stage="queued")
        db = self.db()

        def worker():
            return QueueWorker(1, db_factory=lambda: self.db(), settings_factory=lambda: self.settings,
                               orchestrator_factory=self.orchestrator, clock=lambda: 1000.0, rng=lambda lo, hi: 77.0)
        w = worker()
        w._run_job(db, self.settings, db.get_by_id(job["job_id"]))
        self.assertEqual((self.browser, self.stage(job)), ([], "ready_for_review"))
        self.assertIsNone(w._not_before, "no pacing delay after a job that never touched the site")
        auto = self.job(stage="queued")                       # Greenhouse: auto
        w2 = worker()
        w2._run_job(db, self.settings, db.get_by_id(auto["job_id"]))
        self.assertIn("sync_playwright", self.browser)
        self.assertEqual(w2._not_before, 1077.0, "an automated job still waits before the next one")


# ── the pack ──────────────────────────────────────────────────────

class PackTests(Base):
    def setUp(self):
        super().setUp()
        self.job_ = self.job(company="Acme Robotics")
        self.bank = AnswerBank(1, self.path)
        self.resumes = ResumeStore(1, self.path, self.root)

    def build(self, job=None, profile=PROFILE):
        return build_pack(job or self.job_, path=self.path, profile=profile, root=self.root)

    def keys(self, p):
        return [(f["group"], f["key"]) for f in p["fields"]]

    def make_kit(self, **kw):
        v = self.resumes.add("Priya_Backend.pdf", PDF, "Backend")
        data = dict(variant_id=v["id"], variant_label="Backend", selection="llm", reason="fits",
                    summary="S1. S2. S3.", cover_letter="Dear Acme.", status="ready")
        data.update(kw)
        return v, KitStore(1, self.path).add_version(self.job_["job_id"], **data)

    def test_the_url_is_the_jobs(self):
        p = self.build()
        self.assertEqual((p["url"], p["job_id"], p["company"]), (self.job_["url"], self.job_["job_id"], "Acme Robotics"))

    def test_groups_come_in_the_order_profile_answer_bank_kit(self):
        self.bank.add("Why this role?", "Because.")
        self.make_kit()
        groups = [f["group"] for f in self.build()["fields"]]
        self.assertEqual(groups, sorted(groups, key=["profile", "answer_bank", "kit"].index))
        self.assertEqual([g for g in dict.fromkeys(groups)], ["profile", "answer_bank", "kit"])

    def test_fields_are_numbered_one_to_n_without_gaps(self):
        self.bank.add("Why this role?", "Because.")
        self.make_kit()
        p = self.build()
        self.assertEqual([f["order"] for f in p["fields"]], list(range(1, len(p["fields"]) + 1)))
        self.assertEqual(sum(p["counts"].values()), len(p["fields"]))

    def test_profile_facts_are_in_form_order_and_only_those_filled_in(self):
        profile = self.build()["fields"]
        got = [f["key"] for f in profile if f["group"] == "profile"]
        expected = [k for k in PROFILE_KEYS if PROFILE["_flat"].get(k)]
        self.assertEqual(got, expected)
        self.assertEqual(got[:4], ["full_name", "first_name", "last_name", "email"])
        by = {f["key"]: f for f in profile}
        self.assertEqual((by["email"]["value"], by["email"]["label"], by["email"]["kind"]),
                         ("priya@example.com", "Email Address", "text"))
        self.assertNotIn("phone_alternate", by, "a fact that is not filled in is not listed")

    def test_the_sensitive_equal_opportunity_facts_are_never_in_a_pack(self):
        keys = {f["key"] for f in self.build()["fields"]}
        for private in ("gender", "date_of_birth", "veteran_status", "disability_status", "ethnicity"):
            self.assertNotIn(private, keys)
        self.assertNotIn("Female", json.dumps(self.build()))

    def test_blank_profile_values_are_skipped(self):
        p = self.build(profile={"_flat": {"full_name": "  ", "email": "a@b.co", "phone": ""}})
        self.assertEqual(self.keys(p), [("profile", "email")])

    def test_answers_company_ones_first_then_global_each_alphabetical(self):
        self.bank.add("zebra question", "z", scope="global")
        self.bank.add("apple question", "a", scope="global")
        self.bank.add("Middle question", "m", scope="global")
        self.bank.add("Notice period at Acme?", "30 days", scope="company", company="Acme Robotics")
        self.bank.add("Another Acme thing", "yes", scope="company", company="Acme Robotics")
        got = [(f["label"], f["scope"]) for f in self.build()["fields"] if f["group"] == "answer_bank"]
        self.assertEqual(got, [("Another Acme thing", "company"), ("Notice period at Acme?", "company"),
                               ("apple question", "global"), ("Middle question", "global"), ("zebra question", "global")])

    def test_another_companys_answers_are_not_in_this_pack(self):
        self.bank.add("About Globex", "secret sauce", scope="company", company="Globex")
        self.assertNotIn("secret sauce", json.dumps(self.build()))
        self.assertEqual(self.build()["counts"]["answer_bank"], 0)

    def test_an_answer_field_carries_the_exact_text_to_paste(self):
        entry = self.bank.add("Expected CTC?", "  40 LPA, negotiable  ")
        f = next(f for f in self.build()["fields"] if f["group"] == "answer_bank")
        self.assertEqual((f["label"], f["value"], f["kind"], f["entry_id"], f["key"]),
                         ("Expected CTC?", "40 LPA, negotiable", "text", entry["id"], f"answer_{entry['id']}"))

    def test_an_entry_with_no_answer_is_not_listed(self):
        entries = [{"id": 1, "question_raw": "Blank one", "answer": "   ", "scope": "global", "company": ""},
                   {"id": 2, "question_raw": "Real one", "answer": "yes", "scope": "global", "company": ""},
                   {"id": 3, "question_raw": "None one", "answer": None, "scope": "global", "company": ""}]
        with mock.patch.object(pack, "AnswerBank") as bank:
            bank.return_value.entries.return_value = entries
            labels = [f["label"] for f in self.build()["fields"] if f["group"] == "answer_bank"]
        self.assertEqual(labels, ["Real one"])

    def test_the_answer_list_is_capped(self):
        for i in range(MAX_ANSWERS + 5):
            self.bank.add(f"Question number {i:03d}", f"answer {i}")
        self.assertEqual(self.build()["counts"]["answer_bank"], MAX_ANSWERS)

    def test_the_kit_group_is_resume_then_summary_then_cover_letter(self):
        v, kit = self.make_kit()
        fields = [f for f in self.build()["fields"] if f["group"] == "kit"]
        self.assertEqual([f["key"] for f in fields], ["resume", "summary", "cover_letter"])
        resume, summary, letter = fields
        self.assertEqual((resume["kind"], resume["value"], resume["variant_id"]), ("file", "Priya_Backend.pdf", v["id"]))
        self.assertIn("Backend", resume["label"])
        self.assertEqual((summary["value"], summary["kind"]), ("S1. S2. S3.", "text"))
        self.assertEqual((letter["value"], letter["label"]), ("Dear Acme.", "Cover letter"))

    def test_the_pack_says_which_kit_it_used(self):
        self.make_kit()
        self.assertEqual(self.build()["kit"], {"version": 1, "status": "ready", "selection": "llm", "reason": "fits",
                                               "created_by": "agent"})

    def test_a_kit_without_text_gives_just_the_resume(self):
        self.make_kit(summary="", cover_letter="", status="partial")
        self.assertEqual([f["key"] for f in self.build()["fields"] if f["group"] == "kit"], ["resume"])

    def test_with_no_kit_the_users_default_resume_is_still_named(self):
        v = self.resumes.add("Default.pdf", PDF, "Default")
        p = self.build()
        (resume,) = [f for f in p["fields"] if f["group"] == "kit"]
        self.assertEqual((resume["value"], resume["variant_id"], p["kit"]), ("Default.pdf", v["id"], None))

    def test_with_neither_kit_nor_resume_the_kit_group_is_empty(self):
        p = self.build()
        self.assertEqual((p["counts"]["kit"], p["kit"]), (0, None))

    def test_a_kit_whose_resume_was_deleted_falls_back_to_the_default(self):
        other = self.resumes.add("Other.pdf", PDF, "Other")
        v, _ = self.make_kit()
        self.resumes.delete(v["id"])
        (resume,) = [f for f in self.build()["fields"] if f["key"] == "resume"]
        self.assertEqual(resume["variant_id"], other["id"])

    def test_the_latest_kit_version_is_the_one_used(self):
        self.make_kit()
        KitStore(1, self.path).add_version(self.job_["job_id"], variant_id=None, selection="none",
                                           summary="NEW. Two. Three.", cover_letter="NEW letter", status="ready",
                                           created_by="user")
        p = self.build()
        self.assertEqual({f["key"]: f["value"] for f in p["fields"] if f["group"] == "kit"}["summary"], "NEW. Two. Three.")
        self.assertEqual((p["kit"]["version"], p["kit"]["created_by"]), (2, "user"))

    def test_the_pack_is_current_not_a_snapshot(self):
        self.assertEqual(self.build()["counts"]["answer_bank"], 0)
        self.bank.add("Just saved", "this one")
        self.assertEqual(self.build()["counts"]["answer_bank"], 1)

    def test_building_twice_gives_the_same_pack(self):
        self.bank.add("Bravo question here", "b")
        self.bank.add("Alpha question here", "a")
        self.make_kit()
        self.assertEqual(self.build(), self.build())

    def test_without_a_profile_the_other_groups_are_still_there(self):
        self.bank.add("Why?", "because")
        self.make_kit()
        p = self.build(profile=None)
        self.assertEqual((p["counts"]["profile"], p["counts"]["answer_bank"], p["counts"]["kit"]), (0, 1, 3))

    def test_a_group_that_cannot_be_read_is_left_out_and_the_rest_is_returned(self):
        self.bank.add("Why?", "because")
        with mock.patch.object(pack, "KitStore", side_effect=RuntimeError("db gone")), self.assertLogs("applier.pack", "ERROR"):
            p = self.build()
        self.assertEqual((p["counts"]["profile"] > 0, p["counts"]["answer_bank"], p["counts"]["kit"]), (True, 1, 0))
        with mock.patch.object(pack, "AnswerBank", side_effect=RuntimeError("db gone")), self.assertLogs("applier.pack", "ERROR"):
            self.assertEqual(self.build()["counts"]["answer_bank"], 0)

    def test_only_a_plain_http_link_is_offered_as_the_url(self):
        self.assertEqual(safe_url(" https://x.com/a?b=1 "), "https://x.com/a?b=1")
        self.assertEqual(safe_url("http://x.com"), "http://x.com")
        for bad in ("javascript:alert(1)", "file:///etc/passwd", "data:text/html,x", "//x.com", "", None, "https://", "x.com"):
            self.assertEqual(safe_url(bad), "", repr(bad))
        evil = dict(self.job_, url="javascript:alert(1)")
        self.assertEqual(self.build(evil)["url"], "")

    def test_the_profile_is_only_the_profile_owners(self):
        with mock.patch("db.profile_db.ProfileDB") as P:
            P.return_value.summary.return_value = PROFILE
            self.assertEqual(pack.owner_profile(1), PROFILE)
            self.assertIsNone(pack.owner_profile(2))
        P.assert_called_once()


class PackIsolationTests(Base):
    def test_one_users_bank_kit_and_resumes_never_reach_anothers_pack(self):
        alice_job, bob_job = self.job(uid=1, company="Acme"), self.job(uid=2, company="Acme")
        AnswerBank(1, self.path).add("Alice only question", "alice secret answer")
        AnswerBank(2, self.path).add("Bob only question", "bob secret answer")
        va = ResumeStore(1, self.path, self.root).add("alice_cv.pdf", PDF, "Alice CV")
        KitStore(1, self.path).add_version(alice_job["job_id"], variant_id=va["id"], variant_label="Alice CV",
                                           selection="default", summary="Alice summary.", cover_letter="Alice letter",
                                           status="ready")
        for_bob = json.dumps(build_pack(bob_job, path=self.path, profile=None, root=self.root))
        for private in ("alice secret answer", "Alice only question", "alice_cv.pdf", "Alice summary", "Alice letter"):
            self.assertNotIn(private, for_bob)
        for_alice = json.dumps(build_pack(alice_job, path=self.path, profile=None, root=self.root))
        self.assertIn("alice secret answer", for_alice)
        self.assertNotIn("bob secret answer", for_alice)

    def test_a_pack_for_a_job_without_an_owner_belongs_to_the_first_account(self):
        job = dict(self.job(uid=1), user_id=None)
        AnswerBank(1, self.path).add("Q", "mine")
        self.assertEqual(build_pack(job, path=self.path, profile=None, root=self.root)["counts"]["answer_bank"], 1)


# ── the two actions ───────────────────────────────────────────────

class MarkAppliedTests(Base):
    def test_ready_for_review_to_submitted_as_the_user_with_one_event(self):
        job = self.job(stage="ready_for_review")
        before = len(self.events(job))
        event = assist.mark_applied(self.db(), job["job_id"], "Applied on LinkedIn, Easy Apply")
        row = self.db().get_by_id(job["job_id"])
        self.assertEqual((row["stage"], row["status"]), ("submitted", "applied"))
        self.assertTrue(row["applied_at"])
        self.assertEqual(len(self.events(job)), before + 1, "exactly one new event")
        (e,) = [t for t in self.transitions(job) if t["to_stage"] == "submitted"]
        self.assertEqual((e["from_stage"], e["actor"], e["id"]), ("ready_for_review", "user", event["id"]))
        self.assertEqual((e["detail"]["note"], e["detail"]["via"]), ("Applied on LinkedIn, Easy Apply", "assist"))

    def test_the_note_is_optional(self):
        for note in (None, "", "   "):
            job = self.job(stage="ready_for_review")
            e = assist.mark_applied(self.db(), job["job_id"], note)
            self.assertNotIn("note", e["detail"])
            self.assertEqual(e["actor"], "user")

    def test_it_works_from_every_stage_before_submission(self):
        for stage in ("found", "queued", "preparing", "needs_you", "ready_for_review"):
            job = self.job(stage=stage)
            e = assist.mark_applied(self.db(), job["job_id"], "done")
            self.assertEqual((e["from_stage"], e["to_stage"], e["actor"]), (stage, "submitted", "user"), stage)

    def test_it_cannot_be_recorded_twice_or_after_a_failure_or_a_skip(self):
        for stage in ("submitted", "failed", "skipped"):
            job = self.job(stage=stage)
            before = len(self.events(job))
            with self.assertRaises(lifecycle.IllegalTransition):
                assist.mark_applied(self.db(), job["job_id"], "again")
            self.assertEqual((self.db().get_stage(job["job_id"]), len(self.events(job))), (stage, before))

    def test_a_bad_note_changes_nothing(self):
        job = self.job(stage="ready_for_review")
        for bad in ("x" * 501, 5, ["a"], {"a": 1}):
            with self.assertRaises(ValueError):
                assist.mark_applied(self.db(), job["job_id"], bad)
        self.assertEqual(self.db().get_stage(job["job_id"]), "ready_for_review")
        self.assertEqual(len([t for t in self.transitions(job) if t["to_stage"] == "submitted"]), 0)

    def test_someone_elses_application_is_not_found_and_untouched(self):
        job = self.job(uid=1, stage="ready_for_review")
        with self.assertRaises(LookupError):
            assist.mark_applied(self.db(2), job["job_id"], "mine now")
        with self.assertRaises(LookupError):
            assist.mark_applied(self.db(), "no-such-job", "x")
        self.assertEqual(self.db().get_stage(job["job_id"]), "ready_for_review")

    def test_it_counts_as_applied_today(self):
        job = self.job(stage="ready_for_review")
        before = self.db().count_applied_today()
        assist.mark_applied(self.db(), job["job_id"], "")
        self.assertEqual(self.db().count_applied_today(), before + 1)

    def test_the_apply_session_is_closed_as_done(self):
        job = self.job(stage="ready_for_review")
        with mock.patch.object(cfg, "SESSIONS_DIR", self.dir / "sessions"):
            (self.dir / "sessions").mkdir()
            s = ApplicationSession(job_id=job["job_id"], job_url="u", job_title="t", company="Acme", source="linkedin")
            s.transition(State.REVIEW, "assist")
            assist.mark_applied(self.db(), job["job_id"], "")
            self.assertEqual(ApplicationSession.load(job["job_id"]).state, State.DONE.value)

    def test_a_session_problem_never_undoes_the_record(self):
        job = self.job(stage="ready_for_review")
        with mock.patch.object(ApplicationSession, "load", side_effect=RuntimeError("disk")), \
             self.assertLogs("applier.assist", "ERROR"):
            assist.mark_applied(self.db(), job["job_id"], "")
        self.assertEqual(self.db().get_stage(job["job_id"]), "submitted")

    def test_no_browser_and_no_network(self):
        job = self.job(stage="ready_for_review")
        with mock.patch("playwright.sync_api.sync_playwright", side_effect=AssertionError("browser")):
            assist.mark_applied(self.db(), job["job_id"], "x")


class MarkUnavailableTests(Base):
    def test_ready_for_review_to_skipped_as_the_user_with_the_reason(self):
        job = self.job(stage="ready_for_review")
        before = len(self.events(job))
        event = assist.mark_unavailable(self.db(), job["job_id"], "The posting was removed")
        row = self.db().get_by_id(job["job_id"])
        self.assertEqual((row["stage"], row["status"], row["error_message"]), ("skipped", "skipped", "The posting was removed"))
        self.assertEqual(len(self.events(job)), before + 1)
        (e,) = [t for t in self.transitions(job) if t["to_stage"] == "skipped"]
        self.assertEqual((e["from_stage"], e["actor"], e["id"]), ("ready_for_review", "user", event["id"]))
        self.assertEqual((e["detail"]["note"], e["detail"]["via"], e["detail"]["unavailable"]),
                         ("The posting was removed", "assist", True))

    def test_a_reason_is_required(self):
        job = self.job(stage="ready_for_review")
        for bad in (None, "", "   ", 5):
            with self.assertRaises(ValueError):
                assist.mark_unavailable(self.db(), job["job_id"], bad)
        with self.assertRaises(ValueError):
            assist.mark_unavailable(self.db(), job["job_id"], "x" * 501)
        self.assertEqual(self.db().get_stage(job["job_id"]), "ready_for_review")

    def test_it_works_from_every_stage_that_can_still_be_skipped(self):
        for stage in ("found", "queued", "preparing", "needs_you", "ready_for_review"):
            job = self.job(stage=stage)
            e = assist.mark_unavailable(self.db(), job["job_id"], "gone")
            self.assertEqual((e["from_stage"], e["to_stage"], e["actor"]), (stage, "skipped", "user"), stage)

    def test_an_application_already_submitted_cannot_be_marked_unavailable(self):
        job = self.job(stage="submitted")
        with self.assertRaises(lifecycle.IllegalTransition):
            assist.mark_unavailable(self.db(), job["job_id"], "gone")
        self.assertEqual(self.db().get_stage(job["job_id"]), "submitted")

    def test_someone_elses_application_is_not_found_and_untouched(self):
        job = self.job(uid=1, stage="ready_for_review")
        with self.assertRaises(LookupError):
            assist.mark_unavailable(self.db(2), job["job_id"], "x")
        self.assertEqual(self.db().get_stage(job["job_id"]), "ready_for_review")

    def test_a_skipped_job_can_be_reopened_by_the_existing_lifecycle(self):
        job = self.job(stage="ready_for_review")
        assist.mark_unavailable(self.db(), job["job_id"], "gone")
        self.db().transition(job["job_id"], "found", actor="user")
        self.assertEqual(self.db().get_stage(job["job_id"]), "found")

    def test_the_apply_session_is_skipped_with_the_reason(self):
        job = self.job(stage="ready_for_review")
        with mock.patch.object(cfg, "SESSIONS_DIR", self.dir / "sessions"):
            (self.dir / "sessions").mkdir()
            s = ApplicationSession(job_id=job["job_id"], job_url="u", job_title="t", company="Acme", source="linkedin")
            s.transition(State.REVIEW, "assist")
            assist.mark_unavailable(self.db(), job["job_id"], "closed")
            loaded = ApplicationSession.load(job["job_id"])
            self.assertEqual((loaded.state, loaded.error), (State.SKIPPED.value, "closed"))


# ── over HTTP ─────────────────────────────────────────────────────

@unittest.skipUnless(HAVE_FASTAPI, "fastapi + httpx not installed")
class AssistHttpTests(unittest.TestCase):
    def setUp(self):
        import core.auth as auth
        from db.users_db import UsersDB

        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self._saved_settings = SettingsDB._instance
        SettingsDB._instance = SettingsDB(path=tmp / "settings.db")
        self._saved = (cfg.JOBS_DB_PATH, cfg.PROFILE_DB_PATH, cfg.SESSIONS_DIR, cfg.DATA_DIR, cfg.APP_SECRET_KEY,
                       cfg.COOKIE_SECURE, cfg.ALLOW_SIGNUP, UsersDB._instance, auth._serializer)
        cfg.JOBS_DB_PATH, cfg.PROFILE_DB_PATH, cfg.DATA_DIR = tmp / "jobs.db", tmp / "profile.db", tmp
        cfg.SESSIONS_DIR = tmp / "sessions"
        cfg.SESSIONS_DIR.mkdir()
        cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE, cfg.ALLOW_SIGNUP = "test-signing-secret-" + "x" * 40, False, False
        UsersDB._instance = UsersDB(path=tmp / "users.db")
        auth._serializer = None
        self._dev = os.environ.pop("APEX_DEV_UI_ORIGIN", None)
        from server.app import create_app
        self.app = create_app(ui_dir=tmp / "no-build")
        self.client = TestClient(self.app, follow_redirects=False)            # alice = owner
        self.client.post("/api/auth/signup", json={"email": "alice@example.com", "password": "correct-horse-battery"})
        cfg.ALLOW_SIGNUP = True
        self.bob = TestClient(self.app, follow_redirects=False)
        self.bob.post("/api/auth/signup", json={"email": "bob@example.com", "password": "correct-horse-battery"})
        self.anon = TestClient(self.app, follow_redirects=False)
        self.jobs = JobsDB(user_id=1)

    def tearDown(self):
        import core.auth as auth
        from db.users_db import UsersDB
        SettingsDB._instance = self._saved_settings
        (cfg.JOBS_DB_PATH, cfg.PROFILE_DB_PATH, cfg.SESSIONS_DIR, cfg.DATA_DIR, cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE,
         cfg.ALLOW_SIGNUP, UsersDB._instance, auth._serializer) = self._saved
        if self._dev is not None:
            os.environ["APEX_DEV_UI_ORIGIN"] = self._dev
        self.tmp.cleanup()

    def review_job(self):
        jid = self.jobs.add({"url": "https://www.linkedin.com/jobs/view/1", "title": "Dev", "company": "Acme",
                             "source": "linkedin", "description": "Python."})
        self.jobs.transition(jid, "preparing")
        self.jobs.transition(jid, "ready_for_review")
        return jid

    def test_everything_needs_a_login(self):
        for method, url in (("get", "/api/applications/x/pack"), ("post", "/api/applications/x/mark-applied"),
                            ("post", "/api/applications/x/mark-unavailable")):
            kw = {"json": {}} if method == "post" else {}
            self.assertEqual(getattr(self.anon, method)(url, **kw).status_code, 401, url)

    def test_the_pack(self):
        jid = self.review_job()
        AnswerBank(1, cfg.JOBS_DB_PATH).add("Why Acme?", "Robots.")
        r = self.client.get(f"/api/applications/{jid}/pack")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["url"], "https://www.linkedin.com/jobs/view/1")
        self.assertEqual([f["order"] for f in body["fields"]], list(range(1, len(body["fields"]) + 1)))
        self.assertIn(("answer_bank", "Why Acme?"), [(f["group"], f["label"]) for f in body["fields"]])

    def test_someone_elses_application_is_a_404_everywhere(self):
        jid = self.review_job()
        self.assertEqual(self.bob.get(f"/api/applications/{jid}/pack").status_code, 404)
        self.assertEqual(self.bob.post(f"/api/applications/{jid}/mark-applied", json={}).status_code, 404)
        self.assertEqual(self.bob.post(f"/api/applications/{jid}/mark-unavailable", json={"reason": "x"}).status_code, 404)
        self.assertEqual(self.jobs.get_stage(jid), "ready_for_review")

    def test_mark_applied(self):
        jid = self.review_job()
        r = self.client.post(f"/api/applications/{jid}/mark-applied", json={"note": "did it"})
        self.assertEqual((r.status_code, r.json()["status"], r.json()["event"]["actor"]), (200, "submitted", "user"))
        self.assertEqual(self.jobs.get_stage(jid), "submitted")
        self.assertEqual(self.client.post(f"/api/applications/{jid}/mark-applied", json={}).status_code, 409)

    def test_mark_applied_without_a_body(self):
        jid = self.review_job()
        self.assertEqual(self.client.post(f"/api/applications/{jid}/mark-applied").status_code, 200)

    def test_mark_unavailable(self):
        jid = self.review_job()
        self.assertEqual(self.client.post(f"/api/applications/{jid}/mark-unavailable", json={}).status_code, 400)
        self.assertEqual(self.client.post(f"/api/applications/{jid}/mark-unavailable", json={"reason": "x" * 501}).status_code, 400)
        r = self.client.post(f"/api/applications/{jid}/mark-unavailable", json={"reason": "closed"})
        self.assertEqual((r.status_code, r.json()["status"]), (200, "skipped"))
        self.assertEqual(self.client.post(f"/api/applications/{jid}/mark-unavailable", json={"reason": "again"}).status_code, 409)

    def test_the_setting_is_validated_and_owner_only(self):
        post = lambda c, body: c.post("/api/settings", json=body)
        self.assertEqual(post(self.client, {"apply_mode": "assist"}).status_code, 200)
        self.assertEqual(SettingsDB.get().read("apply_mode"), "assist")
        self.assertEqual(post(self.client, {"apply_mode": "Auto "}).status_code, 200)
        self.assertEqual(SettingsDB.get().read("apply_mode"), "auto")
        self.assertEqual(post(self.client, {"apply_mode": "manual"}).status_code, 400)
        self.assertEqual(SettingsDB.get().read("apply_mode"), "auto", "a rejected value is not stored")
        self.assertEqual(post(self.client, {"apply_mode_overrides": {"lever": "ASSIST"}}).status_code, 200)
        self.assertEqual(SettingsDB.get().read("apply_mode_overrides"), '{"lever":"assist"}')
        self.assertEqual(post(self.client, {"apply_mode_overrides": '{"greenhouse":"auto"}'}).status_code, 200)
        for bad in ('{"monster": "auto"}', "not json", '{"lever": "x"}', "[]"):
            self.assertEqual(post(self.client, {"apply_mode_overrides": bad}).status_code, 400, bad)
        self.assertEqual(SettingsDB.get().read("apply_mode_overrides"), '{"greenhouse":"auto"}')
        self.assertEqual(post(self.bob, {"apply_mode": "assist"}).status_code, 403)

    def test_a_fresh_install_has_the_documented_defaults(self):
        shown = self.client.get("/api/settings").json()["settings"]           # a flat {key: value}
        self.assertEqual(shown["apply_mode"], "auto")
        self.assertEqual(SettingsDB.get().read("apply_mode"), "auto")
        self.assertEqual(json.loads(SettingsDB.get().read("apply_mode_overrides")), DEFAULT_OVERRIDES)
