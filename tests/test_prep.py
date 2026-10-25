# tests/test_prep.py
"""
tests/test_prep.py — prep artifacts for a card that moves forward (db/prep_db.py,
intelligence/prep.py, server/routes/prep.py).

Real SQLite in a temp directory, a FAKE LLM and a fake clock; background work is run inline
(prep.spawn is replaced), so nothing waits, no thread outlives a test and nothing reaches a
model or the network. Covered:

  * the trigger on each of the three stages (technical_round, hiring_manager, offer);
  * one-time generation (the unique row; a repeat trigger does not call the LLM again);
  * regeneration (a new attempt, a superseded run cannot overwrite it);
  * failure isolation: a failing LLM, a failing listener and a failing trigger insert all
    leave the stage change committed; a failure stores the error TYPE only;
  * per-user scoping: no rows for other accounts, stores see only their own rows, the
    profile is used only for the profile owner;
  * offers: validation, the comparison table computed in code, per-user isolation;
  * the HTTP routes: shapes, 404s, owner-only regenerate.
"""

import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
os.environ.setdefault("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())

from db import jobs_db as jobs_module
from db import prep_db
from db.jobs_db import JobsDB
from db.prep_db import OfferStore, PrepStore
from intelligence import prep

START = datetime(2026, 10, 1, 9, 0, 0)
SECRET = "sk-live-SECRET-1234567890"

PROFILE = {"_flat": {"current_role": "Backend Engineer", "experience_years": "5",
                     "resume_summary": "Built payment systems in Go.", "full_name": "Alice Example",
                     "email": "alice@example.com", "expected_ctc_lpa": "40"},
           "skills": ["Go", "Postgres"]}


class Clock:
    def __init__(self):
        self.now = START

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


class FakeLLM:
    """chat_json answers per kind, recognised from the system prompt; records every prompt."""

    def __init__(self, fail: Exception | None = None):
        self.calls, self.fail = [], fail

    def chat_json(self, messages, system="", max_tokens=0):
        self.calls.append({"system": system, "prompt": messages[-1]["content"]})
        if self.fail is not None:
            raise self.fail
        if "TECHNICAL" in system:
            return {"topics": ["Go", "SQL"], "questions": [f"Q{i}" for i in range(1, 13)],
                    "gaps": ["Kubernetes is not on the profile"]}
        if "HIRING MANAGER" in system:
            return {"questions_to_ask": ["What does success look like?"],
                    "star_stories": [{"title": "Payments", "situation": "s", "task": "t", "action": "a", "result": "r"}],
                    "role_summary": "Owns the payments backend.", "missing": ["Team size"]}
        return {"talking_points": [f"Ask for more base (call {len(self.calls)})"]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "jobs.db"
        self.clock = Clock()
        self.llm = FakeLLM()
        self.n = 0
        patcher = mock.patch.object(prep, "spawn", lambda fn: fn())          # background work runs inline
        patcher.start()
        self.addCleanup(patcher.stop)
        self.profile_calls = []

        def generate(path, uid, job_id, kind, **kw):
            return self._real_generate(path, uid, job_id, kind, llm=self.llm, clock=self.clock,
                                       profile_loader=self.loader)
        self._real_generate = prep.generate
        p2 = mock.patch.object(prep, "generate", generate)
        p2.start()
        self.addCleanup(p2.stop)
        jobs_module.add_stage_listener(prep.on_stage_change)
        self.addCleanup(jobs_module.remove_stage_listener, prep.on_stage_change)

    def loader(self, uid):
        self.profile_calls.append(uid)
        return dict(PROFILE)

    def jobs(self, uid=1):
        return JobsDB(path=self.path, user_id=uid, clock=self.clock)

    def job(self, uid=1, **extra):
        self.n += 1
        return self.jobs(uid).add({"url": f"https://jobs.example/{uid}/{self.n}", "title": "Backend Engineer",
                                   "company": "Acme", "source": "linkedin",
                                   "description": "We need Go, SQL and Kubernetes.", **extra})

    def move(self, job_id, *stages, uid=1):
        for st in stages:
            self.jobs(uid).transition(job_id, st, actor="user")

    def rows(self, sql, params=()):
        with closing(sqlite3.connect(str(self.path))) as c:
            c.row_factory = sqlite3.Row
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def art(self, job_id, kind, uid=1):
        return PrepStore(uid, self.path, self.clock).for_job(job_id)[kind]


# ── the trigger ───────────────────────────────────────────────────

class TriggerTests(Base):
    def test_entering_technical_round_generates_technical_prep(self):
        j = self.job()
        self.move(j, "submitted", "technical_round")
        a = self.art(j, "technical")
        self.assertEqual(a["status"], "ready")
        self.assertEqual(a["content"]["topics"], ["Go", "SQL"])
        self.assertEqual(len(a["content"]["questions"]), 10)          # capped at ten
        self.assertEqual(a["content"]["gaps"], ["Kubernetes is not on the profile"])
        self.assertEqual(self.art(j, "hiring_manager")["status"], "not_generated")

    def test_entering_hiring_manager_generates_hiring_manager_prep(self):
        j = self.job()
        self.jobs().add_note(j, "Recruiter said the team is rebuilding billing")
        self.move(j, "submitted", "hiring_manager")
        a = self.art(j, "hiring_manager")
        self.assertEqual(a["status"], "ready")
        c = a["content"]
        self.assertEqual(c["role_summary"], "Owns the payments backend.")
        self.assertEqual(c["star_stories"][0]["title"], "Payments")
        self.assertIn("no web access", c["sources_note"])
        self.assertIn("Team size", c["missing"])
        prompt = self.llm.calls[-1]["prompt"]
        self.assertIn("rebuilding billing", prompt)                    # notes are an input
        self.assertIn("no web access", self.llm.calls[-1]["system"])

    def test_entering_offer_generates_talking_points_and_live_comparison(self):
        j = self.job()
        OfferStore(1, self.path, self.clock).create({"company": "Acme", "job_id": j, "base": 3000000, "variable": 300000})
        self.move(j, "submitted", "offer")
        a = self.art(j, "offer")
        self.assertEqual(a["status"], "ready")
        self.assertTrue(a["content"]["talking_points"])
        self.assertIn("base 3000000", self.llm.calls[-1]["prompt"])

    def test_offer_with_no_offers_entered_needs_no_llm_call(self):
        j = self.job()
        self.move(j, "submitted", "offer")
        a = self.art(j, "offer")
        self.assertEqual((a["status"], a["content"]["talking_points"]), ("ready", []))
        self.assertIn("Enter at least one offer", a["content"]["note"])
        self.assertEqual(self.llm.calls, [])

    def test_other_stages_record_nothing(self):
        j = self.job()
        self.move(j, "queued", "submitted", "recruiter_screen", "rejected")
        self.assertEqual(self.rows("SELECT * FROM prep_artifacts"), [])
        self.assertEqual(self.llm.calls, [])

    def test_skipping_ahead_only_prepares_the_stage_entered(self):
        j = self.job()
        self.move(j, "submitted", "technical_round", "offer")
        kinds = sorted(r["kind"] for r in self.rows("SELECT kind FROM prep_artifacts WHERE job_id=?", (j,)))
        self.assertEqual(kinds, ["offer", "technical"])


# ── once, and regenerate ──────────────────────────────────────────

class OnceAndRegenerateTests(Base):
    def test_one_row_per_user_job_kind_and_the_trigger_twice_is_one_row(self):
        j = self.job()
        self.move(j, "submitted", "technical_round")
        with closing(sqlite3.connect(str(self.path))) as c:
            c.row_factory = sqlite3.Row
            self.assertFalse(prep_db.on_transition(c, user_id=1, job_id=j, to="technical_round", now=START))
            c.commit()
        self.assertEqual(len(self.rows("SELECT * FROM prep_artifacts WHERE job_id=?", (j,))), 1)

    def test_a_repeat_stage_event_does_not_generate_again(self):
        j = self.job()
        self.move(j, "submitted", "technical_round")
        calls = len(self.llm.calls)
        prep.on_stage_change(self.path, 1, {"job_id": j, "to_stage": "technical_round"})
        self.assertEqual(len(self.llm.calls), calls)
        self.assertEqual(self.art(j, "technical")["attempt"], 1)

    def test_regenerate_makes_a_new_attempt_with_new_content(self):
        j = self.job()
        OfferStore(1, self.path, self.clock).create({"company": "Acme", "base": 100})
        self.move(j, "submitted", "offer")
        first = self.art(j, "offer")["content"]["talking_points"]
        row = PrepStore(1, self.path, self.clock).request(j, "offer")
        self.assertEqual((row["status"], row["attempt"]), ("pending", 2))
        self.assertEqual(row["content"]["talking_points"], first)        # old content kept until the new one lands
        self.assertEqual(prep.generate(self.path, 1, j, "offer"), "ready")
        a = self.art(j, "offer")
        self.assertEqual(a["attempt"], 2)
        self.assertNotEqual(a["content"]["talking_points"], first)

    def test_a_superseded_run_cannot_overwrite_the_newer_one(self):
        j = self.job()
        self.move(j, "submitted", "technical_round")
        store = PrepStore(1, self.path, self.clock)
        store.request(j, "technical")
        old = store.claim(j, "technical")                    # attempt 2 claimed...
        store.request(j, "technical")                        # ...then superseded by attempt 3
        self.assertFalse(store.save_ready(j, "technical", old, {"topics": ["stale"]}))
        self.assertEqual(prep.generate(self.path, 1, j, "technical"), "ready")
        a = self.art(j, "technical")
        self.assertEqual((a["attempt"], a["content"]["topics"]), (3, ["Go", "SQL"]))

    def test_claim_is_exclusive(self):
        j = self.job()
        self.move(j, "submitted")
        store = PrepStore(1, self.path, self.clock)
        store.request(j, "technical")
        self.assertEqual(store.claim(j, "technical"), 1)
        self.assertIsNone(store.claim(j, "technical"))

    def test_interrupted_rows_are_resumed(self):
        j = self.job()
        self.move(j, "submitted")
        store = PrepStore(1, self.path, self.clock)
        store.request(j, "technical")
        store.claim(j, "technical")                          # "generating" when the process died
        self.assertEqual(store.waiting(), [(j, "technical")])
        self.assertEqual(self.art(j, "technical")["status"], "pending")


# ── failure isolation ─────────────────────────────────────────────

class FailureIsolationTests(Base):
    def test_llm_failure_marks_failed_with_the_type_only_and_the_stage_change_stands(self):
        self.llm.fail = RuntimeError(f"provider said: bad key {SECRET}")
        j = self.job()
        event = self.jobs().transition(j, "submitted", actor="user")
        event = self.jobs().transition(j, "technical_round", actor="user")
        self.assertEqual(event["to_stage"], "technical_round")
        self.assertEqual(self.jobs().get_stage(j), "technical_round")
        a = self.art(j, "technical")
        self.assertEqual((a["status"], a["error"], a["content"]), ("failed", "RuntimeError", None))
        self.assertNotIn(SECRET, str(self.rows("SELECT * FROM prep_artifacts")))

    def test_unusable_model_answer_is_a_failure_not_a_crash(self):
        self.llm.chat_json = lambda **kw: {"topics": ["x"], "questions": []}
        j = self.job()
        self.move(j, "submitted", "technical_round")
        self.assertEqual(self.art(j, "technical")["status"], "failed")

    def test_a_failing_listener_never_fails_the_transition(self):
        def boom(path, uid, event):
            raise RuntimeError(SECRET)
        jobs_module.add_stage_listener(boom)
        self.addCleanup(jobs_module.remove_stage_listener, boom)
        j = self.job()
        with self.assertLogs("db.jobs_db", "WARNING") as logs:
            self.move(j, "submitted", "technical_round")
        self.assertEqual(self.jobs().get_stage(j), "technical_round")
        self.assertNotIn(SECRET, "\n".join(logs.output))

    def test_a_failing_trigger_insert_never_rolls_back_the_stage_change(self):
        j = self.job()
        self.move(j, "submitted")
        with closing(sqlite3.connect(str(self.path))) as c:
            c.execute("DROP TABLE prep_artifacts")
            c.commit()
        jobs_module.remove_stage_listener(prep.on_stage_change)
        db = JobsDB.__new__(JobsDB)                      # an open view, without re-creating the table
        db.path, db.user_id, db._clock = self.path, 1, self.clock
        db._stale_days = lambda: {"submitted": 7, "recruiter_screen": 5, "other": 7}
        db.transition(j, "technical_round", actor="user")
        self.assertEqual(db.get_stage(j), "technical_round")
        events = self.rows("SELECT to_stage FROM application_events WHERE job_id=? ORDER BY id", (j,))
        self.assertEqual(events[-1]["to_stage"], "technical_round")

    def test_generate_never_raises_even_when_the_job_is_gone(self):
        j = self.job()
        self.move(j, "submitted")
        PrepStore(1, self.path, self.clock).request(j, "technical")
        with closing(sqlite3.connect(str(self.path))) as c:
            c.execute("DELETE FROM jobs WHERE job_id=?", (j,))
            c.commit()
        self.assertEqual(prep.generate(self.path, 1, j, "technical"), "failed")
        self.assertEqual(self.rows("SELECT error FROM prep_artifacts")[0]["error"], "LookupError")


# ── per-user scoping and the profile rule ─────────────────────────

class ScopingTests(Base):
    def test_other_accounts_get_no_row_and_see_not_generated(self):
        j = self.job(uid=2)
        self.move(j, "submitted", "technical_round", "hiring_manager", "offer", uid=2)
        self.assertEqual(self.rows("SELECT * FROM prep_artifacts"), [])
        self.assertEqual(self.llm.calls, [])
        arts = PrepStore(2, self.path, self.clock).for_job(j)
        self.assertEqual({k: a["status"] for k, a in arts.items()},
                         {k: "not_generated" for k in prep_db.KINDS})

    def test_stores_only_see_their_own_rows(self):
        j = self.job()
        self.move(j, "submitted", "technical_round")
        self.assertEqual(PrepStore(2, self.path, self.clock).for_job(j)["technical"]["status"], "not_generated")
        with self.assertRaises(LookupError):
            PrepStore(2, self.path, self.clock).request(j, "technical")
        self.assertIsNone(PrepStore(2, self.path, self.clock).claim(j, "technical"))

    def test_start_ignores_anyone_but_the_owner(self):
        j = self.job(uid=2)
        self.move(j, "submitted", uid=2)
        PrepStore(2, self.path, self.clock).request(j, "technical")
        prep.start(self.path, 2, j, "technical")
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.art(j, "technical", uid=2)["status"], "pending")

    def test_the_profile_is_used_for_the_owner(self):
        j = self.job()
        self.move(j, "submitted", "technical_round")
        self.assertEqual(self.profile_calls, [1])
        prompt = self.llm.calls[-1]["prompt"]
        self.assertIn("Backend Engineer", prompt)
        self.assertIn("Built payment systems in Go.", prompt)
        for private in ("Alice Example", "alice@example.com"):          # no personal details
            self.assertNotIn(private, prompt)
        self.assertEqual(self.art(j, "technical")["content"]["gaps_note"], "")

    def test_the_profile_is_never_used_for_another_user(self):
        j = self.job(uid=2)
        self.move(j, "submitted", uid=2)
        PrepStore(2, self.path, self.clock).request(j, "technical")
        PrepStore(2, self.path, self.clock).request(j, "hiring_manager")
        for kind in ("technical", "hiring_manager"):
            self.assertEqual(self._real_generate(self.path, 2, j, kind, llm=self.llm, clock=self.clock,
                                                 profile_loader=self.loader), "ready")
        self.assertEqual(self.profile_calls, [])                         # never even loaded
        self.assertTrue(all("Built payment systems" not in c["prompt"] for c in self.llm.calls))
        tech = self.art(j, "technical", uid=2)["content"]
        self.assertEqual((tech["gaps"], tech["gaps_note"]), ([], prep.PROFILE_UNAVAILABLE))
        hm = self.art(j, "hiring_manager", uid=2)["content"]
        self.assertEqual((hm["star_stories"], hm["star_note"]), ([], prep.PROFILE_UNAVAILABLE))

    def test_load_owner_profile_refuses_other_users(self):
        self.assertIsNone(prep.load_owner_profile(2, self.path))


# ── offers ────────────────────────────────────────────────────────

class OfferTests(Base):
    def test_comparison_is_computed_from_the_components(self):
        s = OfferStore(1, self.path, self.clock)
        a = s.create({"company": "Acme", "base": 2000000, "variable": 200000, "joining_bonus": 300000,
                      "equity_per_year": 500000})
        b = s.create({"company": "Beta", "base": 2600000, "retention_bonus": 100000, "other": 50000})
        cmp_ = prep_db.compare_offers(s.list())
        rows = {r["company"]: r for r in cmp_["rows"]}
        self.assertEqual(rows["Acme"]["recurring"], 2700000)
        self.assertEqual(rows["Acme"]["one_time"], 300000)
        self.assertEqual(rows["Acme"]["first_year_total"], 3000000)
        self.assertEqual(rows["Beta"]["first_year_total"], 2750000)
        self.assertEqual(rows["Acme"]["fixed_share_pct"], 74.1)
        self.assertEqual([r["company"] for r in cmp_["rows"]], ["Acme", "Beta"])
        self.assertEqual(cmp_["best_offer_id"], a["id"])
        self.assertEqual(rows["Beta"]["first_year_vs_best"], -250000)
        self.assertEqual(rows["Acme"]["recurring_vs_best"], 0)
        self.assertEqual(rows["Beta"]["recurring_vs_best"], -50000)
        self.assertNotEqual(a["id"], b["id"])

    def test_mixed_currencies_are_not_ranked(self):
        s = OfferStore(1, self.path, self.clock)
        s.create({"company": "Acme", "base": 100, "currency": "inr"})
        s.create({"company": "Beta", "base": 1, "currency": "USD"})
        cmp_ = prep_db.compare_offers(s.list())
        self.assertFalse(cmp_["comparable"])
        self.assertIsNone(cmp_["best_offer_id"])
        self.assertNotIn("first_year_vs_best", cmp_["rows"][0])

    def test_validation(self):
        s = OfferStore(1, self.path, self.clock)
        for bad in ({}, {"company": " "}, {"company": "A", "base": -1}, {"company": "A", "base": True},
                    {"company": "A", "base": "lots"}, {"company": "A", "currency": "rupees"},
                    {"company": "A", "salary": 5}, {"company": "A", "job_id": "not-mine"}):
            with self.assertRaises(ValueError, msg=bad):
                s.create(bad)
        o = s.create({"company": "Acme", "base": 10})
        with self.assertRaises(ValueError):
            s.update(o["id"], {})
        self.assertEqual(s.update(o["id"], {"base": 20})["base"], 20.0)

    def test_offers_are_per_user(self):
        mine = OfferStore(1, self.path, self.clock).create({"company": "Acme", "base": 10})
        theirs = OfferStore(2, self.path, self.clock)
        self.assertEqual(theirs.list(), [])
        self.assertIsNone(theirs.get(mine["id"]))
        self.assertIsNone(theirs.update(mine["id"], {"base": 1}))
        self.assertFalse(theirs.delete(mine["id"]))
        bob_job = self.job(uid=2)
        with self.assertRaises(ValueError):                              # can't link to someone else's card
            OfferStore(1, self.path, self.clock).create({"company": "X", "job_id": bob_job})
        self.assertEqual(OfferStore(1, self.path, self.clock).get(mine["id"])["base"], 10.0)


# ── HTTP ──────────────────────────────────────────────────────────

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except Exception:
    HAVE_FASTAPI = False


@unittest.skipUnless(HAVE_FASTAPI, "fastapi + httpx not installed")
class PrepHttpTests(unittest.TestCase):
    def setUp(self):
        from core.config import cfg
        import core.auth as auth
        from db.users_db import UsersDB
        from db.settings_db import SettingsDB
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self._saved = (cfg.JOBS_DB_PATH, cfg.SESSIONS_DIR, cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE,
                       cfg.ALLOW_SIGNUP, UsersDB._instance, auth._serializer, SettingsDB._instance)
        SettingsDB._instance = SettingsDB(path=tmp / "settings.db")
        cfg.JOBS_DB_PATH, cfg.SESSIONS_DIR = tmp / "jobs.db", tmp / "sessions"
        cfg.SESSIONS_DIR.mkdir()
        cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE, cfg.ALLOW_SIGNUP = "test-signing-secret-" + "x" * 40, False, False
        UsersDB._instance, auth._serializer = UsersDB(path=tmp / "users.db"), None
        self.llm = FakeLLM()
        p = mock.patch.object(prep, "spawn", lambda fn: fn())             # background work runs inline
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch("core.llm.chat_json", self.llm.chat_json)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(prep, "load_owner_profile", lambda uid, path=None: dict(PROFILE) if uid == 1 else None)
        p.start()
        self.addCleanup(p.stop)
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
        (cfg.JOBS_DB_PATH, cfg.SESSIONS_DIR, cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE, cfg.ALLOW_SIGNUP,
         UsersDB._instance, auth._serializer, SettingsDB._instance) = self._saved
        self.tmp.cleanup()

    def add(self, uid):
        return JobsDB(user_id=uid).add({"url": f"https://x.example/{uid}", "title": "Dev", "company": "Co",
                                        "source": "linkedin", "description": "Go and SQL"})

    def test_routes(self):
        j = self.add(1)
        r = self.alice.get(f"/api/applications/{j}/prep")
        self.assertEqual(r.status_code, 200)
        self.assertEqual({k: a["status"] for k, a in r.json()["artifacts"].items()},
                         {"technical": "not_generated", "hiring_manager": "not_generated", "offer": "not_generated"})
        self.assertIn("comparison", r.json()["artifacts"]["offer"])

        r = self.alice.post(f"/api/applications/{j}/prep/technical/regenerate")
        self.assertEqual((r.status_code, r.json()["status"]), (202, "pending"))
        self.assertEqual(self.alice.get(f"/api/applications/{j}/prep").json()["artifacts"]["technical"]["status"], "ready")
        self.assertEqual(self.alice.post(f"/api/applications/{j}/prep/bogus/regenerate").status_code, 400)
        self.assertEqual(self.alice.post("/api/applications/nope/prep/technical/regenerate").status_code, 404)

        # bob: someone else's card is not found; regenerating is owner-only
        self.assertEqual(self.bob.get(f"/api/applications/{j}/prep").status_code, 404)
        b = self.add(2)
        self.assertEqual(self.bob.get(f"/api/applications/{b}/prep").status_code, 200)
        self.assertEqual(self.bob.post(f"/api/applications/{b}/prep/technical/regenerate").status_code, 403)
        anon = TestClient(self.app)
        self.assertEqual(anon.get(f"/api/applications/{j}/prep").status_code, 401)

    def test_offer_routes(self):
        r = self.alice.post("/api/offers", json={"company": "Acme", "base": 100, "variable": 10})
        self.assertEqual(r.status_code, 201)
        oid = r.json()["id"]
        self.assertEqual(self.alice.post("/api/offers", json={"company": ""}).status_code, 400)
        listing = self.alice.get("/api/offers").json()
        self.assertEqual(listing["comparison"]["rows"][0]["first_year_total"], 110)
        self.assertEqual(self.alice.put(f"/api/offers/{oid}", json={"base": 200}).json()["base"], 200.0)
        self.assertEqual(self.alice.put(f"/api/offers/{oid}", json={"base": -5}).status_code, 400)
        self.assertEqual(self.bob.get("/api/offers").json()["offers"], [])
        self.assertEqual(self.bob.put(f"/api/offers/{oid}", json={"base": 1}).status_code, 404)
        self.assertEqual(self.bob.delete(f"/api/offers/{oid}").status_code, 404)
        self.assertEqual(self.alice.delete(f"/api/offers/{oid}").status_code, 200)
        self.assertEqual(self.alice.delete(f"/api/offers/{oid}").status_code, 404)


if __name__ == "__main__":
    unittest.main()
