# tests/test_lifecycle.py
"""
tests/test_lifecycle.py — the one-lifecycle model: legal/illegal transitions,
the application_events audit trail, per-user isolation of jobs/events/stream,
migration of every old status, and that the pre-lifecycle read shapes survive.

Run from the project root:   python3 -m unittest discover -s tests -v

Real throwaway SQLite files (no mocks of SQL). The HTTP routes are covered in
tests/test_applications_http.py, which needs FastAPI.
"""

import asyncio
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
os.environ.setdefault("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())

from core.config import cfg
from db import lifecycle
from db.jobs_db import JobsDB
from server import sse


def job(n=1, **kw):
    d = {"url": f"https://x.com/job/{n}", "title": f"Title {n}", "company": f"Co {n}",
         "source": "linkedin", "location": "Bengaluru"}
    d.update(kw)
    return d


def force(db, job_id, stage, reason=""):
    """Put a job at `stage` directly, bypassing transition() (test setup only)."""
    with db._conn() as c:
        c.execute("UPDATE jobs SET stage=?, blocked_reason=? WHERE job_id=?",
                  (stage, reason, job_id))


def count_events(db, job_id=None):
    with db._conn() as c:
        if job_id:
            return c.execute("SELECT COUNT(*) n FROM application_events WHERE job_id=?",
                             (job_id,)).fetchone()["n"]
        return c.execute("SELECT COUNT(*) n FROM application_events").fetchone()["n"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "jobs.db"
        self._sessions_dir = cfg.SESSIONS_DIR
        cfg.SESSIONS_DIR = self.dir / "sessions"       # never read the real session files
        cfg.SESSIONS_DIR.mkdir()
        self.alice = JobsDB(path=self.path, user_id=1)
        self.bob = JobsDB(path=self.path, user_id=2)

    def tearDown(self):
        cfg.SESSIONS_DIR = self._sessions_dir
        self.tmp.cleanup()


# ── the table of legal transitions ────────────────────────────────

class TransitionTableTests(Base):
    def test_the_stages_are_exactly_the_specified_ones_in_order(self):
        self.assertEqual(lifecycle.STAGES[:10], (
            "found", "queued", "preparing", "needs_you", "ready_for_review",
            "submitted", "recruiter_screen", "technical_round", "hiring_manager", "offer"))
        self.assertEqual(set(lifecycle.STAGES[10:]),
                         {"rejected", "withdrawn", "skipped", "failed"})
        self.assertEqual(set(lifecycle.TERMINAL), {"rejected", "withdrawn", "skipped", "failed"})
        self.assertEqual(set(lifecycle.BLOCKED_REASONS), {
            "question", "captcha", "login_required", "unsupported_form", "naukri_chat_screening",
            "validation_error", "job_closed", "other"})

    def test_specific_legal_moves_work_and_specific_illegal_moves_raise(self):
        legal = [("found", "queued"), ("queued", "preparing"), ("preparing", "needs_you"),
                 ("needs_you", "preparing"), ("preparing", "ready_for_review"),
                 ("ready_for_review", "submitted"), ("submitted", "recruiter_screen"),
                 ("recruiter_screen", "technical_round"), ("technical_round", "hiring_manager"),
                 ("hiring_manager", "offer"), ("found", "skipped"), ("preparing", "failed"),
                 ("failed", "preparing"), ("skipped", "found"), ("found", "submitted"),
                 ("queued", "found"), ("preparing", "queued"),
                 ("recruiter_screen", "hiring_manager"), ("offer", "withdrawn"),
                 ("submitted", "rejected")]
        illegal = [("found", "offer"), ("found", "needs_you"), ("found", "ready_for_review"),
                   ("found", "found"), ("queued", "queued"), ("submitted", "preparing"),
                   ("submitted", "found"), ("offer", "hiring_manager"), ("offer", "found"),
                   ("submitted", "queued"), ("needs_you", "queued"),
                   ("rejected", "offer"), ("rejected", "found"), ("withdrawn", "submitted"),
                   ("failed", "submitted"), ("skipped", "submitted"),
                   ("hiring_manager", "recruiter_screen"), ("technical_round", "submitted"),
                   ("needs_you", "needs_you"), ("rejected", "withdrawn")]
        for frm, to in legal:
            with self.subTest(legal=(frm, to)):
                j = self.alice.add(job(f"{frm}-{to}"))
                force(self.alice, j, frm, "question" if frm == "needs_you" else "")
                self.alice.transition(j, to, reason="question" if to == "needs_you" else None)
                self.assertEqual(self.alice.get_stage(j), to)
        for frm, to in illegal:
            with self.subTest(illegal=(frm, to)):
                j = self.alice.add(job(f"x-{frm}-{to}"))
                force(self.alice, j, frm, "question" if frm == "needs_you" else "")
                with self.assertRaises(lifecycle.IllegalTransition):
                    self.alice.transition(j, to, reason="question" if to == "needs_you" else None)
                self.assertEqual(self.alice.get_stage(j), frm)

    def test_every_pair_follows_the_table_and_an_illegal_move_changes_nothing(self):
        for frm in lifecycle.STAGES:
            for to in lifecycle.STAGES:
                j = self.alice.add(job(f"{frm}>{to}"))
                force(self.alice, j, frm, "other" if frm == "needs_you" else "")
                before = (count_events(self.alice, j), self.alice.get_by_id(j)["status"])
                reason = "other" if to == "needs_you" else None
                if lifecycle.can_transition(frm, to):
                    self.alice.transition(j, to, reason=reason)
                    self.assertEqual(self.alice.get_stage(j), to, (frm, to))
                    self.assertEqual(count_events(self.alice, j), before[0] + 1)
                else:
                    with self.assertRaises(lifecycle.IllegalTransition, msg=(frm, to)):
                        self.alice.transition(j, to, reason=reason)
                    self.assertEqual(self.alice.get_stage(j), frm)
                    self.assertEqual((count_events(self.alice, j),
                                      self.alice.get_by_id(j)["status"]), before)

    def test_rejected_and_withdrawn_are_final(self):
        self.assertEqual(lifecycle.allowed_from("rejected"), ())
        self.assertEqual(lifecycle.allowed_from("withdrawn"), ())

    def test_unknown_stage_or_actor_is_a_plain_value_error_not_an_illegal_move(self):
        j = self.alice.add(job())
        with self.assertRaises(ValueError) as cm:
            self.alice.transition(j, "applied")
        self.assertNotIsInstance(cm.exception, lifecycle.IllegalTransition)
        with self.assertRaises(ValueError):
            self.alice.transition(j, "queued", actor="root")
        self.assertEqual(self.alice.get_stage(j), "found")

    def test_needs_you_requires_a_valid_reason_and_other_stages_forbid_one(self):
        j = self.alice.add(job())
        self.alice.transition(j, "preparing")
        for bad in (None, "", "nope"):
            with self.assertRaises(ValueError, msg=bad):
                self.alice.transition(j, "needs_you", reason=bad)
        with self.assertRaises(ValueError):
            self.alice.transition(j, "ready_for_review", reason="captcha")
        self.assertEqual(self.alice.get_stage(j), "preparing")

        for reason in lifecycle.BLOCKED_REASONS:
            j2 = self.alice.add(job(f"r-{reason}"))
            self.alice.transition(j2, "preparing")
            self.alice.transition(j2, "needs_you", reason=reason)
            row = self.alice.get_by_id(j2)
            self.assertEqual((row["stage"], row["blocked_reason"]), ("needs_you", reason))

        # leaving needs_you clears the reason
        self.alice.transition(j2, "preparing")
        self.assertEqual(self.alice.get_by_id(j2)["blocked_reason"], "")


# ── the audit trail ───────────────────────────────────────────────

class EventTests(Base):
    def test_adding_a_job_records_its_creation(self):
        j = self.alice.add(job())
        ev = self.alice.get_application(j)["events"]
        self.assertEqual(len(ev), 1)
        self.assertEqual((ev[0]["kind"], ev[0]["from_stage"], ev[0]["to_stage"], ev[0]["actor"]),
                         ("created", None, "found", "system"))

    def test_every_transition_writes_exactly_one_event_with_all_fields(self):
        j = self.alice.add(job())
        e = self.alice.transition(j, "queued", actor="user", note="  later  ")
        self.assertEqual(count_events(self.alice, j), 2)
        self.assertEqual((e["kind"], e["from_stage"], e["to_stage"], e["actor"], e["job_id"]),
                         ("transition", "found", "queued", "user", j))
        self.assertEqual(e["detail"], {"note": "later"})
        self.assertEqual(e["screenshot"], "")
        self.assertTrue(e["at"])
        self.assertNotIn("user_id", e)

        self.alice.transition(j, "preparing")
        e = self.alice.transition(j, "ready_for_review", detail={"pages": 2},
                                  screenshot="/data/screenshots/a b/review.png")
        self.assertEqual(e["detail"], {"pages": 2})
        self.assertEqual(e["screenshot"], "review.png", "only the file NAME is kept")
        self.assertEqual(count_events(self.alice, j), 4)
        self.assertEqual([x["to_stage"] for x in self.alice.get_application(j)["events"]],
                         ["found", "queued", "preparing", "ready_for_review"])

    def test_all_four_actors_are_accepted(self):
        for i, actor in enumerate(lifecycle.ACTORS):
            j = self.alice.add(job(i))
            self.assertEqual(self.alice.transition(j, "queued", actor=actor)["actor"], actor)

    def test_stage_bookkeeping_columns_follow_the_transition(self):
        j = self.alice.add(job())
        t0 = self.alice.get_by_id(j)["stage_entered_at"]
        self.assertTrue(t0)
        self.alice.transition(j, "preparing")
        self.alice.transition(j, "needs_you", reason="login_required")
        row = self.alice.get_by_id(j)
        self.assertEqual((row["stage"], row["blocked_reason"], row["status"]),
                         ("needs_you", "login_required", "in_progress"))
        self.assertGreaterEqual(row["stage_entered_at"], t0)
        self.alice.transition(j, "failed", note="site timed out")
        row = self.alice.get_by_id(j)
        self.assertEqual((row["status"], row["error_message"], row["blocked_reason"]),
                         ("error", "site timed out", ""))
        self.assertEqual(row["applied_at"], "")
        self.alice.transition(j, "preparing")
        self.assertEqual(self.alice.get_by_id(j)["error_message"], "")
        self.alice.transition(j, "submitted")
        self.assertNotEqual(self.alice.get_by_id(j)["applied_at"], "")

    def test_a_job_that_moves_under_us_is_refused_and_leaves_no_event(self):
        j = self.alice.add(job())
        real_check = lifecycle.check

        def check_then_race(frm, to, reason=None):
            real_check(frm, to, reason)
            other = sqlite3.connect(str(self.path))             # someone else moves it first
            try:
                with other:                                      # commits; does NOT close
                    other.execute("UPDATE jobs SET stage='skipped' WHERE job_id=?", (j,))
            finally:
                other.close()                                    # Windows can't delete an open db file

        with mock.patch.object(lifecycle, "check", check_then_race):
            with self.assertRaises(lifecycle.IllegalTransition):
                self.alice.transition(j, "queued")
        self.assertEqual(self.alice.get_stage(j), "skipped")
        self.assertEqual(count_events(self.alice, j), 1, "only the creation event")

    def test_unserialisable_detail_fails_before_anything_is_written(self):
        j = self.alice.add(job())
        with self.assertRaises(TypeError):
            self.alice.transition(j, "queued", detail={"x": object()})
        with self.assertRaises(ValueError):
            self.alice.transition(j, "queued", detail=["not", "a", "dict"])
        self.assertEqual((self.alice.get_stage(j), count_events(self.alice, j)), ("found", 1))

    def test_events_since_resumes_exactly_after_the_given_id(self):
        j = self.alice.add(job())
        first = self.alice.events_since(0)
        self.assertEqual(len(first), 1)
        e2 = self.alice.transition(j, "queued")
        e3 = self.alice.transition(j, "preparing")
        self.assertEqual([e["id"] for e in self.alice.events_since(first[0]["id"])],
                         [e2["id"], e3["id"]])
        self.assertEqual(self.alice.events_since(e3["id"]), [])
        self.assertEqual(self.alice.event_head(), e3["id"])
        self.assertEqual(len(self.alice.events_since(0, limit=2)), 2)


# ── per-user isolation ────────────────────────────────────────────

class IsolationTests(Base):
    def test_one_user_cannot_move_another_users_job(self):
        a = self.alice.add(job())
        before = count_events(self.alice)
        with self.assertRaises(LookupError):
            self.bob.transition(a, "skipped")
        with self.assertRaises(LookupError):
            self.bob.add_stage(a, "applied")
        self.bob.update_status(a, "skipped")            # old behaviour: silently ignored
        self.assertEqual(self.alice.get_stage(a), "found")
        self.assertEqual(count_events(self.alice), before)

    def test_applications_events_and_head_are_scoped(self):
        a = self.alice.add(job(1))
        b = self.bob.add(job(2))
        self.alice.transition(a, "queued")
        self.bob.transition(b, "queued"); self.bob.transition(b, "preparing")

        self.assertEqual([x["job_id"] for x in self.alice.list_applications()], [a])
        self.assertEqual([x["job_id"] for x in self.bob.list_applications()], [b])
        self.assertIsNone(self.bob.get_application(a))
        self.assertIsNone(self.alice.get_application(b))
        self.assertTrue({e["job_id"] for e in self.alice.events_since(0)} == {a})
        self.assertTrue({e["job_id"] for e in self.bob.events_since(0)} == {b})
        self.assertEqual(len(self.alice.events_since(0)), 2)
        self.assertEqual(len(self.bob.events_since(0)), 3)
        self.assertEqual(self.alice.event_head(), self.alice.events_since(0)[-1]["id"])

    def test_the_same_posting_has_an_independent_lifecycle_per_user(self):
        a = self.alice.add(job())
        b = self.bob.add(job())
        self.alice.transition(a, "skipped")
        self.assertEqual(self.bob.get_stage(b), "found")
        self.assertEqual(len(self.bob.get_application(b)["events"]), 1)

    def test_bulk_deletes_remove_only_the_callers_events(self):
        a = self.alice.add(job(1)); b = self.bob.add(job(2))
        self.alice.clear_all()
        self.assertEqual(count_events(self.alice, a), 0)
        self.assertEqual(count_events(self.bob, b), 1)

        a2 = self.alice.add(job(3)); self.alice.transition(a2, "submitted")
        self.assertEqual(self.alice.clear_all(), 0)                 # submitted is kept
        self.assertEqual(count_events(self.alice, a2), 2)
        self.alice.clear_all(include_applied=True)
        self.assertEqual(count_events(self.alice, a2), 0)
        self.assertEqual(count_events(self.bob, b), 1)

    def test_purge_duplicates_removes_events_of_purged_rows_only(self):
        with self.alice._conn() as c:
            for i in range(2):
                c.execute("""INSERT INTO jobs (job_id,url_hash,title_co_key,title,company,url,
                    source,found_at,updated_at,user_id,stage,stage_entered_at)
                    VALUES (?,?,?,?,?,?,?,?,?,1,'found','t')""",
                    (f"dup{i}", f"h{i}", "dup|co", "Dup", "Co", f"u{i}", "linkedin", "t", "t"))
                c.execute("""INSERT INTO application_events (user_id,job_id,at,kind,to_stage,actor)
                    VALUES (1,?,?,'created','found','system')""", (f"dup{i}", "t"))
        b = self.bob.add(job(2))
        self.assertEqual(self.alice.purge_duplicates(), 1)
        self.assertEqual(count_events(self.alice, "dup0") + count_events(self.alice, "dup1"), 1)
        self.assertEqual(count_events(self.bob, b), 1)


# ── the applications list ─────────────────────────────────────────

class ApplicationListTests(Base):
    def setUp(self):
        super().setUp()
        self.ids = {}
        rows = [("Python Developer", "Acme", "linkedin", "Delhi"),
                ("Go Engineer", "Globex", "indeed", "Remote"),
                ("100% Remote Role", "Initech", "LinkedIn", "Pune"),
                ("Data_Analyst", "Umbrella", "naukri", "Mumbai")]
        for i, (t, co, src, loc) in enumerate(rows):
            self.ids[t] = self.alice.add({"url": f"https://x.com/{i}", "title": t,
                                          "company": co, "source": src, "location": loc})
        self.alice.transition(self.ids["Go Engineer"], "queued")
        self.alice.transition(self.ids["Data_Analyst"], "skipped")

    def titles(self, **kw):
        return sorted(r["title"] for r in self.alice.list_applications(**kw))

    def test_filters(self):
        self.assertEqual(len(self.titles()), 4)
        self.assertEqual(self.titles(stage="queued"), ["Go Engineer"])
        self.assertEqual(self.titles(stage="queued,skipped"), ["Data_Analyst", "Go Engineer"])
        self.assertEqual(self.titles(source="LINKEDIN"), ["100% Remote Role", "Python Developer"])
        self.assertEqual(self.titles(title="engineer"), ["Go Engineer"])
        self.assertEqual(self.titles(q="globex"), ["Go Engineer"])
        self.assertEqual(self.titles(q="pune"), ["100% Remote Role"])
        self.assertEqual(self.titles(stage="found", q="remote"), ["100% Remote Role"])
        self.assertEqual(self.titles(stage="queued", q="python"), [])

    def test_like_wildcards_in_the_search_are_literal(self):
        self.assertEqual(self.titles(title="100%"), ["100% Remote Role"])
        self.assertEqual(self.titles(title="%"), ["100% Remote Role"])
        self.assertEqual(self.titles(title="_"), ["Data_Analyst"])
        self.assertEqual(self.titles(q="a_a"), ["Data_Analyst"])

    def test_unknown_stage_filter_raises(self):
        with self.assertRaises(ValueError):
            self.alice.list_applications(stage="queued,bogus")

    def test_rows_carry_the_lifecycle_fields_and_most_recent_move_comes_first(self):
        rows = self.alice.list_applications()
        for k in ("job_id", "title", "company", "source", "stage", "stage_entered_at",
                  "blocked_reason", "error_message"):
            self.assertIn(k, rows[0])
        self.assertEqual(rows[0]["title"], "Data_Analyst")        # moved last
        self.assertNotIn("description", rows[0])
        self.assertEqual(len(self.alice.list_applications(limit=2)), 2)
        self.assertEqual(len(self.alice.list_applications(limit=0)), 1, "limit is clamped to >= 1")

    def test_detail_includes_events(self):
        d = self.alice.get_application(self.ids["Go Engineer"])
        self.assertEqual([e["to_stage"] for e in d["events"]], ["found", "queued"])
        self.assertIn("description", d)


# ── migration of the three old concepts ───────────────────────────

def load_legacy():
    spec = importlib.util.spec_from_file_location(
        "jobs_db_legacy", Path(__file__).parent / "fixtures" / "jobs_db_legacy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class MigrationTests(Base):
    """Old database -> lifecycle. Built with the ORIGINAL implementation
    (tests/fixtures/jobs_db_legacy.py), then opened with the new one."""

    def setUp(self):
        super().setUp()
        self.legacy_path = self.dir / "legacy.db"
        self.old = load_legacy().JobsDB(path=self.legacy_path)
        self.n = 0
        self.expect = {}        # job_id -> (stage, blocked_reason, legacy status after)

    def make(self, status, *, stages=(), session=None, expect, status_after=None):
        self.n += 1
        jid = self.old.add({"url": f"https://old.com/{self.n}", "title": f"Old {self.n}",
                            "company": f"OldCo {self.n}", "source": "linkedin"})
        for st in stages:
            self.old.add_stage(jid, st, note=f"note-{st}")
        self.old.update_status(jid, status)             # legacy accepts any value
        if session:
            from applier.session import ApplicationSession
            ApplicationSession(job_id=jid, job_url="u", job_title="t", company="c",
                               source="linkedin", state=session).save()
        self.expect[jid] = (*expect, status_after if status_after is not None else status)
        return jid

    def build_every_old_status(self):
        make = self.make
        make("found",                  expect=("found", ""))
        make("in_progress", session="analyzing",         expect=("preparing", ""))
        make("in_progress", session="filling",           expect=("preparing", ""))
        make("in_progress", session="refilling",         expect=("preparing", ""))
        make("in_progress", session="awaiting_input",    expect=("needs_you", "question"))
        make("in_progress", session="screenshot_review", expect=("ready_for_review", ""))
        make("in_progress", session="submitting",        expect=("ready_for_review", ""))
        make("in_progress", session="failed",            expect=("failed", ""), status_after="error")
        # no usable session: the latest job_stages row decides, else back to found
        make("in_progress", session="done", stages=["applied", "recruiter_screen"],
             expect=("recruiter_screen", ""))
        make("in_progress", stages=["applied", "technical_round", "rejected"],
             expect=("rejected", ""))
        make("in_progress", session="idle",              expect=("found", ""), status_after="found")
        make("in_progress",                              expect=("found", ""), status_after="found")
        # applied -> latest job_stages stage (applied -> submitted)
        make("applied", stages=["applied"],              expect=("submitted", ""))
        make("applied", stages=["applied", "offer"],     expect=("offer", ""), status_after="in_progress")
        make("applied",                                  expect=("submitted", ""))
        make("skipped",                                  expect=("skipped", ""))
        make("error",                                    expect=("failed", ""))
        make("captcha_blocked",                          expect=("needs_you", "captcha"))
        make("manual",                                   expect=("needs_you", "unsupported_form"))
        make("some_garbage_status",                      expect=("found", ""), status_after="found")

    def test_every_old_status_lands_in_the_right_stage(self):
        self.build_every_old_status()
        db = JobsDB(path=self.legacy_path, user_id=1)          # opening it migrates
        for jid, (stage, reason, status_after) in self.expect.items():
            with self.subTest(job=jid, expect=stage):
                row = db.get_by_id(jid)
                self.assertEqual((row["stage"], row["blocked_reason"]), (stage, reason))
                self.assertEqual(row["status"], status_after,
                                 "jobs.status must be the projection of the new stage")
                self.assertTrue(row["stage_entered_at"])
                events = db.get_application(jid)["events"]
                self.assertTrue(events)
                self.assertTrue(all(e["kind"] == "migrated" and e["actor"] == "system"
                                    for e in events))
                self.assertEqual(events[-1]["to_stage"], stage)
                self.assertIsNone(events[0]["from_stage"])
                for prev, nxt in zip(events, events[1:]):
                    self.assertEqual(nxt["from_stage"], prev["to_stage"])

    def test_old_stage_history_survives_as_events_and_in_the_old_stages_shape(self):
        jid = self.make("in_progress", stages=["applied", "recruiter_screen"],
                        session="done", expect=("recruiter_screen", ""))
        db = JobsDB(path=self.legacy_path, user_id=1)
        ev = db.get_application(jid)["events"]
        self.assertEqual([e["to_stage"] for e in ev], ["submitted", "recruiter_screen"])
        self.assertEqual(ev[0]["detail"]["note"], "note-applied")
        stages = db.get_by_id(jid)["stages"]
        self.assertEqual([(s["stage"], s["note"]) for s in stages],
                         [("applied", "note-applied"), ("recruiter_screen", "note-recruiter_screen")])

    def test_migration_is_idempotent(self):
        self.build_every_old_status()
        db = JobsDB(path=self.legacy_path, user_id=1)
        snapshot = (count_events(db), {r["job_id"]: (r["stage"], r["status"], r["stage_entered_at"])
                                      for r in db.get_all()})
        for _ in range(3):
            db = JobsDB(path=self.legacy_path, user_id=1)
            JobsDB(path=self.legacy_path)
        again = (count_events(db), {r["job_id"]: (r["stage"], r["status"], r["stage_entered_at"])
                                   for r in db.get_all()})
        self.assertEqual(snapshot, again)

    def test_a_second_opener_migrating_mid_flight_does_not_duplicate_events(self):
        """Deterministic race: after opener A has read the un-migrated rows but
        before it has written anything, opener B migrates the same database.
        A must then add nothing."""
        for _ in range(3):
            self.make("applied", stages=["applied", "recruiter_screen"],
                      expect=("recruiter_screen", ""))
        real = lifecycle.stage_from_legacy
        raced = []

        def stage_from_legacy_then_race(*a, **k):
            if not raced:
                raced.append(1)
                JobsDB(path=self.legacy_path, user_id=1)         # B migrates everything now
            return real(*a, **k)

        with mock.patch.object(lifecycle, "stage_from_legacy", stage_from_legacy_then_race):
            a = JobsDB(path=self.legacy_path, user_id=1)         # A, holding stale rows
        self.assertEqual(raced, [1])
        self.assertEqual(count_events(a), 3 * 2, "events were duplicated by the racing migration")

    def test_migration_never_undoes_a_later_transition(self):
        jid = self.make("found", expect=("found", ""))
        db = JobsDB(path=self.legacy_path, user_id=1)
        db.transition(jid, "queued")
        JobsDB(path=self.legacy_path, user_id=1)
        self.assertEqual(db.get_stage(jid), "queued")
        self.assertEqual(count_events(db, jid), 2)

    def test_migrated_events_belong_to_the_user_who_adopts_the_rows(self):
        jid = self.make("found", expect=("found", ""))
        JobsDB(path=self.legacy_path)                           # unscoped open migrates first
        bob = JobsDB(path=self.legacy_path, user_id=2)
        self.assertEqual((bob.events_since(0), bob.event_head(), bob.list_applications()),
                         ([], 0, []), "user 2 saw the pre-existing history")
        alice = JobsDB(path=self.legacy_path, user_id=1)
        self.assertEqual([e["job_id"] for e in alice.events_since(0)], [jid])
        self.assertEqual(JobsDB(path=self.legacy_path, user_id=2).events_since(0), [])

    def test_a_brand_new_database_needs_no_migration(self):
        j = self.alice.add(job())
        self.assertEqual(count_events(self.alice, j), 1)
        JobsDB(path=self.path, user_id=1)
        self.assertEqual(count_events(self.alice, j), 1)


# ── the old read shapes still work ────────────────────────────────

class OldShapesTests(Base):
    """Run the same script through the ORIGINAL implementation and the new
    one and compare what the old endpoints read."""

    def run_script(self, db):
        ids = []
        for i, (t, co, src) in enumerate([("A", "Ca", "linkedin"), ("B", "Cb", "indeed"),
                                          ("C", "Cc", "linkedin"), ("D", "Cd", "naukri")]):
            ids.append(db.add({"url": f"https://s.com/{i}", "title": t, "company": co,
                               "source": src}))
        a, b, c, d = ids
        db.update_status(b, "skipped", error="Auto-skip: low score")
        db.add_stage(a, "applied", note="sent")
        db.add_stage(c, "applied"); db.add_stage(c, "recruiter_screen", note="call booked")
        db.add_note(a, "n1")
        db.update_score(a, 80, "good")
        return ids

    def setUp(self):
        super().setUp()
        self.old = load_legacy().JobsDB(path=self.dir / "old.db")
        self.new = JobsDB(path=self.dir / "new.db")
        self.old_ids = self.run_script(self.old)
        self.new_ids = self.run_script(self.new)

    def test_get_all_rows_keep_every_old_key_and_status_value(self):
        old = {r["title"]: r for r in self.old.get_all()}
        new = {r["title"]: r for r in self.new.get_all()}
        for t in old:
            self.assertTrue(set(old[t]) <= set(new[t]), f"{t} lost keys: {set(old[t]) - set(new[t])}")
            for k in ("status", "error_message", "ai_score", "company", "source"):
                self.assertEqual(old[t][k], new[t][k], (t, k))
            self.assertEqual(bool(old[t]["applied_at"]), bool(new[t]["applied_at"]), t)
        self.assertEqual([r["title"] for r in self.new.get_all(status="applied")],
                         [r["title"] for r in self.old.get_all(status="applied")])
        self.assertEqual([r["title"] for r in self.new.get_all(status="in_progress")], ["C"])

    def test_get_by_id_keeps_its_shape_including_stages_and_notes(self):
        old = self.old.get_by_id(self.old_ids[2]); new = self.new.get_by_id(self.new_ids[2])
        self.assertTrue(set(old) <= set(new))
        self.assertEqual([(s["stage"], s["note"]) for s in old["stages"]],
                         [(s["stage"], s["note"]) for s in new["stages"]])
        self.assertEqual([set(s) for s in old["stages"]], [set(s) for s in new["stages"]])
        self.assertEqual([n["note"] for n in self.old.get_by_id(self.old_ids[0])["notes"]],
                         [n["note"] for n in self.new.get_by_id(self.new_ids[0])["notes"]])

    def test_stats_pipeline_and_analytics_match_the_old_implementation(self):
        self.assertEqual(self.old.get_stats(), self.new.get_stats())

        def board(db):
            return {stage: [(r["title"], r["stage"], set(r)) for r in rows]
                    for stage, rows in db.get_pipeline().items()}
        self.assertEqual(board(self.old), board(self.new))
        self.assertEqual(list(self.old.get_pipeline()), list(self.new.get_pipeline()))

        oa, na = self.old.get_analytics(), self.new.get_analytics()
        self.assertEqual(set(oa), set(na))
        for k in ("score_dist", "top_companies", "funnel", "sources"):
            self.assertEqual(oa[k], na[k], k)
        self.assertEqual(oa["daily"][-1], na["daily"][-1])

    def test_old_helpers_behave_like_before(self):
        a = self.new_ids[0]
        self.new.add_stage(a, "applied")                         # same stage again: harmless no-op
        self.assertEqual(count_events(self.new, a), 2)           # created + submitted
        self.new.update_status(a, "applied")                     # likewise
        self.assertEqual(count_events(self.new, a), 2)
        with self.assertRaises(ValueError):
            self.new.add_stage(a, "nonsense")
        with self.assertRaises(ValueError):
            self.new.update_status(a, "in_progress")             # not a state you can write
        with self.assertRaises(lifecycle.IllegalTransition):
            self.new.update_status(a, "skipped")                 # submitted -> skipped isn't a move
        self.assertEqual(self.new.get_stage(a), "submitted")
        self.assertEqual(self.new.count_applied_today(), 2)

    def test_status_column_is_always_the_projection_of_stage(self):
        j = self.new_ids[3]
        for to, reason in [("queued", None), ("preparing", None), ("needs_you", "captcha"),
                           ("preparing", None), ("needs_you", "unsupported_form"),
                           ("ready_for_review", None), ("submitted", None),
                           ("technical_round", None), ("offer", None)]:
            self.new.transition(j, to, reason=reason)
            row = self.new.get_by_id(j)
            self.assertEqual(row["status"], lifecycle.legacy_status(row["stage"], row["blocked_reason"]))
        self.assertEqual(lifecycle.legacy_status("failed"), "error")
        self.assertEqual(lifecycle.legacy_status("found"), "found")


# ── the apply Orchestrator drives the lifecycle ───────────────────

class FakeProfile:
    def __init__(self, pending=()):
        self.pending = list(pending)
    def summary(self): return {}
    def flag_missing(self, **kw): pass
    def get_pending_missing(self, job_id): return list(self.pending)
    def set_many(self, *a, **k): pass
    def resolve_missing(self, *a, **k): pass


class FakeCtx:
    pages = []
    def new_page(self): return object()
    def close(self): pass


class FakePlaywright:
    def __enter__(self): return object()
    def __exit__(self, *a): return False


class QuietSettings:
    """Settings with every default: no blacklist, no salary floor."""
    def read(self, key, default=""):
        return default


class OrchestratorWiringTests(Base):
    def setUp(self):
        super().setUp()
        # Pre-flight runs inside the Orchestrator: give it quiet settings and
        # make sure a test can NEVER reach the network through it.
        for p in (mock.patch("applier.session.Orchestrator._settings",
                             staticmethod(lambda: QuietSettings())),
                  mock.patch("applier.preflight.fetch_page", lambda url: (200, "")),
                  mock.patch("urllib.request.urlopen",
                             side_effect=AssertionError("a test touched the network"))):
            p.start()
            self.addCleanup(p.stop)

    def orchestrator(self, jid, db, pending=()):
        from applier.session import ApplicationSession, Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        o.job_id = jid
        o.job = db.get_by_id(jid)
        o.profile_db = FakeProfile(pending)
        o.jobs_db = db
        o.session = ApplicationSession(job_id=jid, job_url="u", job_title="t",
                                       company="c", source="linkedin")
        o._open_browser = lambda pw: FakeCtx()
        return o

    def patched(self, **results):
        stack = [mock.patch("playwright.sync_api.sync_playwright", lambda: FakePlaywright())]
        for name, r in results.items():
            fn = r if callable(r) else (lambda *a, _r=r, **k: _r)
            stack.append(mock.patch(f"applier.universal.{name}", fn))
        return stack

    def run_with(self, stack, fn):
        from contextlib import ExitStack
        with ExitStack() as es:
            for m in stack:
                es.enter_context(m)
            return fn()

    def stages(self, db, jid):
        """The stage changes (progress "step" events are not stage changes)."""
        return [(e["to_stage"], e["actor"]) for e in db.get_application(jid)["events"]
                if e["kind"] != "step"]

    def test_clean_fill_review_submit_walks_the_lifecycle(self):
        jid = self.alice.add(job())
        o = self.orchestrator(jid, self.alice)
        shot = "/data/screenshots/review_1.png"
        self.run_with(self.patched(run_application={"success": True, "screenshots": [shot],
                                                    "missing_fields": [], "error": ""}),
                      o.run_iter1)
        self.assertEqual(self.alice.get_stage(jid), "ready_for_review")
        self.assertEqual(self.alice.get_application(jid)["events"][-1]["screenshot"], "review_1.png")

        with mock.patch("applier.session.Orchestrator._dry_run_enabled", staticmethod(lambda: False)):
            self.run_with(self.patched(run_submit={"success": True, "screenshots": [shot], "error": ""}),
                          o.submit)
        row = self.alice.get_by_id(jid)
        self.assertEqual((row["stage"], row["status"]), ("submitted", "applied"))
        self.assertNotEqual(row["applied_at"], "")
        self.assertEqual(self.stages(self.alice, jid),
                         [("found", "system"), ("preparing", "agent"),
                          ("ready_for_review", "agent"), ("submitted", "agent")])

    def test_missing_fields_park_the_job_in_needs_you_question_then_resume(self):
        jid = self.alice.add(job())
        pending = [{"id": 1, "field_key": "ctc", "field_label": "Expected CTC",
                    "field_type": "text", "options": "", "placeholder_used": ""}]
        o = self.orchestrator(jid, self.alice, pending)
        ff = mock.Mock(label="Expected CTC", field_key="ctc", field_type="text",
                       options="", placeholder="")
        self.run_with(self.patched(run_application={"success": True, "screenshots": [],
                                                    "missing_fields": [ff], "error": ""}),
                      o.run_iter1)
        row = self.alice.get_by_id(jid)
        self.assertEqual((row["stage"], row["blocked_reason"]), ("needs_you", "question"))
        ev = [e for e in self.alice.get_application(jid)["events"] if e["kind"] != "step"][-1]
        self.assertEqual(ev["detail"], {"missing_fields": ["Expected CTC"]},
                         "labels only, never answers")

        o.profile_db.pending = []
        self.run_with(self.patched(run_iteration2={"success": True, "screenshots": ["/s/r2.png"],
                                                   "missing_fields": [], "error": ""}),
                      lambda: o.run_iter2({"ctc": "10 LPA"}))
        row = self.alice.get_by_id(jid)
        self.assertEqual((row["stage"], row["blocked_reason"]), ("ready_for_review", ""))
        self.assertEqual([s for s, _ in self.stages(self.alice, jid)],
                         ["found", "preparing", "needs_you", "preparing", "ready_for_review"])

    def test_failures_end_in_failed_with_the_reason_and_can_be_retried(self):
        jid = self.alice.add(job())
        o = self.orchestrator(jid, self.alice)
        err = "Navigation failed: Page.goto: Timeout 30000ms exceeded."
        self.run_with(self.patched(run_application={"success": False, "screenshots": [],
                                                    "missing_fields": [], "error": err}),
                      o.run_iter1)
        row = self.alice.get_by_id(jid)
        self.assertEqual((row["stage"], row["status"], row["error_message"]),
                         ("failed", "error", err))
        o2 = self.orchestrator(jid, self.alice)                    # retry: failed -> preparing
        self.run_with(self.patched(run_application={"success": True, "screenshots": [],
                                                    "missing_fields": [], "error": ""}),
                      o2.run_iter1)
        self.assertEqual(self.alice.get_stage(jid), "ready_for_review")

    def test_dry_run_does_not_mark_the_job_submitted(self):
        jid = self.alice.add(job())
        force(self.alice, jid, "ready_for_review")
        o = self.orchestrator(jid, self.alice)
        with mock.patch("applier.session.Orchestrator._dry_run_enabled", staticmethod(lambda: True)):
            self.run_with(self.patched(run_submit={"success": True, "screenshots": [], "error": ""}),
                          o.submit)
        self.assertEqual(self.alice.get_stage(jid), "ready_for_review")
        self.assertEqual(self.alice.get_by_id(jid)["applied_at"], "")

    def test_a_job_that_cannot_be_worked_on_never_launches_a_browser(self):
        jid = self.alice.add(job())
        force(self.alice, jid, "rejected")
        o = self.orchestrator(jid, self.alice)
        launched = []
        o._open_browser = lambda pw: launched.append(1) or FakeCtx()
        with self.assertRaises(lifecycle.IllegalTransition):
            self.run_with(self.patched(run_application={"success": True, "screenshots": [],
                                                        "missing_fields": [], "error": ""}),
                          o.run_iter1)
        self.assertEqual((launched, self.alice.get_stage(jid)), ([], "rejected"))

    def test_scorer_style_auto_skip_is_recorded_as_a_system_event(self):
        j = self.alice.add(job())
        self.alice.update_status(j, "skipped", error="Auto-skip: score 12/100")
        e = self.alice.get_application(j)["events"][-1]
        self.assertEqual((e["to_stage"], e["actor"], e["detail"]),
                         ("skipped", "system", {"note": "Auto-skip: score 12/100"}))
        self.assertEqual(self.alice.get_by_id(j)["error_message"], "Auto-skip: score 12/100")


# ── the live stream ───────────────────────────────────────────────

class StreamTests(Base, unittest.IsolatedAsyncioTestCase):
    async def take(self, gen, n, timeout=3.0):
        out = []
        async def pull():
            async for chunk in gen:
                out.append(chunk)
                if len(out) >= n:
                    return
        await asyncio.wait_for(pull(), timeout)
        await gen.aclose()
        return out

    @staticmethod
    def ids(chunks):
        return [int(c.split("\n")[0][4:]) for c in chunks if c.startswith("id: ")]

    def test_frame_format_and_last_event_id_parsing(self):
        ev = {"id": 7, "job_id": "j", "detail": {"note": "line1\nline2"}}
        frame = sse.format_event(ev)
        self.assertTrue(frame.startswith("id: 7\nevent: application_event\ndata: "))
        self.assertTrue(frame.endswith("\n\n"))
        self.assertEqual(frame.count("\n"), 4, "data must stay on ONE line or the frame breaks")
        self.assertEqual(json.loads(frame.split("data: ")[1]), ev)
        for raw, want in [("5", 5), (" 12 ", 12), ("0", 0), (None, None), ("", None),
                          ("abc", None), ("-3", None), ("1.5", None)]:
            self.assertEqual(sse.parse_last_event_id(raw), want, raw)

    async def test_without_last_event_id_it_starts_from_now_and_streams_new_events(self):
        j = self.alice.add(job())
        gen = sse.stream(self.alice, None, poll=0.01)
        first = await gen.__anext__()
        self.assertTrue(first.startswith("retry:"))
        self.alice.transition(j, "queued")
        chunk = await asyncio.wait_for(gen.__anext__(), 3)
        self.assertEqual(json.loads(chunk.split("data: ")[1])["to_stage"], "queued")
        self.assertEqual(self.ids([chunk]), [self.alice.event_head()])
        await gen.aclose()

    async def test_last_event_id_replays_everything_after_it_and_nothing_before(self):
        j = self.alice.add(job())
        e2 = self.alice.transition(j, "queued")
        e3 = self.alice.transition(j, "preparing")
        e4 = self.alice.transition(j, "failed")
        out = await self.take(sse.stream(self.alice, str(e2["id"]), poll=0.01), 3)
        self.assertEqual(self.ids(out), [e3["id"], e4["id"]])
        # reconnecting with the last id seen gives only what is newer
        e5 = self.alice.transition(j, "preparing")
        out = await self.take(sse.stream(self.alice, e4["id"], poll=0.01), 2)
        self.assertEqual(self.ids(out), [e5["id"]])

    async def test_junk_or_future_last_event_id_does_not_replay_history_or_stall(self):
        j = self.alice.add(job())
        self.alice.transition(j, "queued")
        for raw in ("junk", "-1", None):
            gen = sse.stream(self.alice, raw, poll=0.01)
            self.assertTrue((await gen.__anext__()).startswith("retry:"))
            with self.assertRaises(asyncio.TimeoutError, msg=f"history replayed for {raw!r}"):
                await asyncio.wait_for(gen.__anext__(), 0.2)
        # an id beyond anything this user has is clamped, so a new event still arrives
        gen = sse.stream(self.alice, "999999", poll=0.01)
        await gen.__anext__()
        new = self.alice.transition(j, "preparing")
        chunk = await asyncio.wait_for(gen.__anext__(), 3)
        self.assertEqual(self.ids([chunk]), [new["id"]])
        await gen.aclose()

    async def test_a_stream_never_carries_another_users_events_whatever_id_it_asks_for(self):
        a = self.alice.add(job(1)); b = self.bob.add(job(2))
        self.alice.transition(a, "queued")
        bob_events = self.bob.events_since(0)
        out = await self.take(sse.stream(self.alice, "0", poll=0.01), 3)
        payloads = [json.loads(c.split("data: ")[1]) for c in out if c.startswith("id: ")]
        self.assertEqual({p["job_id"] for p in payloads}, {a})
        self.assertFalse(set(self.ids(out)) & {e["id"] for e in bob_events})

        gen = sse.stream(self.bob, None, poll=0.01)                # live only
        await gen.__anext__()
        self.alice.transition(a, "preparing"); bnew = self.bob.transition(b, "queued")
        chunk = await asyncio.wait_for(gen.__anext__(), 3)
        self.assertEqual(self.ids([chunk]), [bnew["id"]], "Bob's stream got Alice's live event")
        await gen.aclose()

    async def test_heartbeat_comments_keep_an_idle_connection_alive(self):
        out = await self.take(sse.stream(self.alice, None, poll=0.01, heartbeat=0.05), 2)
        self.assertEqual(out[1], ": keep-alive\n\n")

    async def test_stream_ends_when_the_client_disconnects(self):
        calls = {"n": 0}
        async def gone():
            calls["n"] += 1
            return calls["n"] > 2
        chunks = [c async for c in sse.stream(self.alice, None, poll=0.01, is_disconnected=gone)]
        self.assertEqual(len(chunks), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
