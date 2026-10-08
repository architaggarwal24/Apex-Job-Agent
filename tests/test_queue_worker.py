# tests/test_queue_worker.py
"""
tests/test_queue_worker.py — the background queue worker: processing order,
daily limit / delay / dry_run / shutdown, pause/resume, restart recovery and
the quality-first score gate.

Run from the project root:   python3 -m unittest discover -s tests -v

Everything is driven through QueueWorker.step() with a FAKE CLOCK and a FAKE
orchestrator, against real throwaway SQLite files - no browser, no sleeping,
no network. A handful of tests start the real background thread (with the
fake orchestrator) to prove the loop itself: they wait on conditions, never
on fixed sleeps. The HTTP routes are in tests/test_queue_http.py.
"""

import contextlib
import os
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
os.environ.setdefault("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())

from applier import failures
from applier import queue_worker as qw
from applier.queue_worker import QueueWorker
from core.config import cfg
from db import lifecycle
from db.jobs_db import JobsDB

T0 = 1_000_000_000.0        # the fake clock's start (epoch seconds)


class FakeSettings:
    """dict-backed stand-in for SettingsDB (read/write only). Defaults make
    the delay zero so tests that don't care about it never wait."""
    def __init__(self, **kw):
        self.values = {"quality_first": "true", "daily_apply_limit": "30",
                       "auto_skip_score_threshold": "30", "dry_run": "false",
                       "min_delay_between_applies": "0", "max_delay_between_applies": "0"}
        self.values.update({k: str(v) for k, v in kw.items()})

    def read(self, key, default=""):
        return self.values.get(key, default)

    def write(self, key, value):
        self.values[key] = str(value)


class FakeClock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class Script:
    """The orchestrator factory handed to the worker. Records what ran, and
    moves the job the way the real Orchestrator does (through an UNSCOPED
    JobsDB, as the real one is). outcomes[job_id] picks the result."""
    def __init__(self, path):
        self.path = path
        self.ran = []
        self.outcomes = {}
        self.active = 0
        self.max_active = 0
        self.on_run = None
        self.forbidden = []          # anything the worker must never call

    def __call__(self, job):
        return _FakeOrchestrator(self, job)


class _FakeOrchestrator:
    def __init__(self, script, job):
        self.script, self.job = script, job

    def run_iter1(self, emit=None):
        s, jid = self.script, self.job["job_id"]
        s.active += 1
        s.max_active = max(s.max_active, s.active)
        try:
            s.ran.append(jid)
            if s.on_run:
                s.on_run(self.job)
            outcome = s.outcomes.get(jid, "ready_for_review")
            kind, _, arg = outcome.partition(":")
            db = JobsDB(path=s.path)
            if kind == "ready_for_review":
                db.transition(jid, "ready_for_review", actor="agent")
            elif kind == "needs_you":
                db.transition(jid, "needs_you", actor="agent", reason="question")
            elif kind == "blocked":              # blocked:<class> - parked for a person, as the real one does
                db.transition(jid, "needs_you", actor="agent", reason=arg,
                              note=f"{arg} blocker", detail={"classified": arg})
            elif kind == "failed":               # failed:<class> - timeout/unknown, what the queue retries
                db.transition(jid, "failed", actor="agent", note=f"{arg} failure",
                              detail={"classified": arg, "error": f"{arg} failure"})
            elif kind == "preflight_skip":       # pre-flight ruled it out: no browser was opened
                db.transition(jid, "skipped", actor="agent", note="Company is on your blacklist (Acme)",
                              detail={"preflight": "blacklisted_company"})
            elif kind == "raise":
                raise RuntimeError("browser exploded")
            elif kind == "timeout_raise":
                raise TimeoutError("Page.goto: Timeout 30000ms exceeded.")
            # "nothing": return without moving the job
        finally:
            s.active -= 1

    def run_iter2(self, *a, **k):
        self.script.forbidden.append("run_iter2")

    def submit(self, *a, **k):
        self.script.forbidden.append("submit")


def force(db, job_id, stage, reason=""):
    with db._conn() as c:
        c.execute("UPDATE jobs SET stage=?, blocked_reason=? WHERE job_id=?",
                  (stage, reason, job_id))


def wait_until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "jobs.db"
        self._sessions_dir = cfg.SESSIONS_DIR
        cfg.SESSIONS_DIR = self.dir / "sessions"
        cfg.SESSIONS_DIR.mkdir()
        self.settings = FakeSettings()
        self.clock = FakeClock()
        self.script = Script(self.path)
        self.stop = threading.Event()              # the "process shutdown" event
        self.rng_calls = []
        self.alice = JobsDB(path=self.path, user_id=1)
        self.bob = JobsDB(path=self.path, user_id=2)
        self.workers = []
        self.worker = self.make_worker()
        self.n = 0

    def tearDown(self):
        for w in self.workers:
            w.stop()
        cfg.SESSIONS_DIR = self._sessions_dir
        self.tmp.cleanup()

    def _rng(self, lo, hi):
        self.rng_calls.append((lo, hi))
        return hi                                   # deterministic: always the longest delay

    def make_worker(self, user_id=1, **kw):
        opts = dict(db_factory=lambda: JobsDB(path=self.path, user_id=user_id),
                    settings_factory=lambda: self.settings,
                    orchestrator_factory=self.script, clock=self.clock,
                    rng=self._rng, stop_event=self.stop)
        opts.update(kw)
        w = QueueWorker(user_id, **opts)
        self.workers.append(w)
        return w

    def job(self, score=80, *, db=None, queue=True, title=None):
        """A new job for `db` (default alice) with that ai_score (None = unscored)."""
        db = db or self.alice
        self.n += 1
        jid = db.add({"url": f"https://x.com/{db.user_id}/{self.n}",
                      "title": title or f"Job {self.n}", "company": f"Co {self.n}",
                      "source": "linkedin"})
        if score is not None:
            db.update_score(jid, score, "test")
        if queue:
            db.transition(jid, "queued", actor="user")
        return jid

    def applied(self, n, db=None):
        """n jobs submitted today (what count_applied_today counts)."""
        for _ in range(n):
            jid = self.job(None, db=db, queue=False)
            (db or self.alice).transition(jid, "submitted", actor="user")

    def drain(self, limit=50):
        """step() until something other than a processed job; returns the outcomes."""
        outs = []
        for _ in range(limit):
            outs.append(self.worker.step())
            if outs[-1] != "processed":
                return outs
        self.fail("worker never stopped processing")

    def stage(self, jid, db=None):
        return (db or self.alice).get_stage(jid)


# ── ordering and the shape of one job's run ───────────────────────

class OrderingTests(Base):
    def test_highest_score_first_and_oldest_first_among_equals(self):
        a = self.job(50); b = self.job(90); c = self.job(70); d = self.job(90)
        self.assertEqual(self.drain(), ["processed"] * 4 + ["idle"])
        self.assertEqual(self.script.ran, [b, d, c, a])      # 90 (queued first), 90, 70, 50

    def test_unscored_jobs_go_last_when_quality_first_allows_them(self):
        self.settings.values["quality_first"] = "false"
        none = self.job(None); low = self.job(5); high = self.job(80)
        self.drain()
        self.assertEqual(self.script.ran, [high, low, none])

    def test_one_job_per_step_and_never_two_at_once(self):
        ids = [self.job(60 + i) for i in range(3)]
        self.assertEqual(self.worker.step(), "processed")
        self.assertEqual(len(self.script.ran), 1)
        self.assertEqual([self.stage(i) for i in ids].count("queued"), 2)
        self.drain()
        self.assertEqual(self.script.max_active, 1)

    def test_a_job_queued_later_with_a_higher_score_jumps_the_line(self):
        low = self.job(40)
        self.assertEqual(self.worker.step(), "processed")
        low2 = self.job(40); high = self.job(99)
        self.drain()
        self.assertEqual(self.script.ran, [low, high, low2])

    def test_nothing_queued_is_idle_and_touches_nothing(self):
        found = self.job(90, queue=False)
        self.assertEqual(self.worker.step(), "idle")
        self.assertEqual((self.script.ran, self.stage(found)), ([], "found"))


class OutcomeTests(Base):
    def test_each_first_pass_result_is_kept_and_the_queue_moves_on(self):
        ok = self.job(90); ask = self.job(80); stuck = self.job(70)
        self.script.outcomes = {ask: "needs_you", stuck: "blocked:captcha"}
        self.drain()
        self.assertEqual(self.stage(ok), "ready_for_review")
        self.assertEqual((self.stage(ask), self.alice.get_by_id(ask)["blocked_reason"]),
                         ("needs_you", "question"))
        self.assertEqual((self.stage(stuck), self.alice.get_by_id(stuck)["blocked_reason"]),
                         ("needs_you", "captcha"))

    def test_a_crashing_orchestrator_fails_that_job_and_does_not_stop_the_queue(self):
        boom = self.job(90); fine = self.job(80)
        self.script.outcomes = {boom: "raise"}
        with self.assertLogs("applier.queue_worker", "ERROR"):          # the crash is logged
            outcomes = self.drain()
        # the crash was classified "unknown", so it is waiting out its one retry
        self.assertEqual(outcomes, ["processed", "processed", "backoff"])
        self.assertEqual(self.stage(boom), "queued")
        failed = [e for e in self.alice.get_application(boom)["events"] if e["to_stage"] == "failed"][0]
        self.assertIn("browser exploded", failed["detail"]["error"])
        self.assertEqual(failed["detail"]["classified"], "unknown")
        self.assertEqual(self.stage(fine), "ready_for_review")

    def test_an_orchestrator_that_returns_without_a_result_cannot_leave_a_job_stuck(self):
        j = self.job(90)
        self.script.outcomes = {j: "nothing"}
        self.worker.step()
        self.assertEqual(self.stage(j), "queued", "it is held for its one retry, not stuck in preparing")
        self.clock.advance(60)
        self.worker.step()                                   # the retry also ends without a result
        self.assertEqual(self.stage(j), "failed")
        self.assertIn("without a result", self.alice.get_by_id(j)["error_message"])

    def test_the_worker_never_submits_and_never_marks_anything_applied(self):
        ids = [self.job(90 - i) for i in range(3)]
        self.drain()
        self.assertEqual(self.script.forbidden, [])
        for i in ids:
            row = self.alice.get_by_id(i)
            self.assertNotIn(row["stage"], lifecycle.SUBMITTED_OR_LATER)
            self.assertEqual(row["applied_at"], "")
        self.assertEqual(self.alice.count_applied_today(), 0)

    def test_the_audit_trail_shows_who_did_what(self):
        j = self.job(90)
        self.worker.step()
        ev = self.alice.get_application(j)["events"]
        self.assertEqual([(e["to_stage"], e["actor"]) for e in ev],
                         [("found", "system"), ("queued", "user"),
                          ("preparing", "agent"), ("ready_for_review", "agent")])
        self.assertEqual(ev[2]["detail"], {"via": "queue"})

    def test_status_while_a_job_runs_names_it_and_only_to_its_owner(self):
        j = self.job(90, title="Platform Engineer")
        seen = {}
        def peek(job):
            seen["alice"] = self.worker.status(self.alice)
            seen["bob"] = self.worker.status(self.bob)
        self.script.on_run = peek
        self.worker.step()
        cur = seen["alice"]["current_job"]
        self.assertEqual((cur["job_id"], cur["title"]), (j, "Platform Engineer"))
        self.assertEqual(set(cur), {"job_id", "title", "company", "started_at"})
        self.assertIsNone(seen["alice"]["next_at"])
        self.assertIsNone(seen["bob"]["current_job"], "Bob was shown Alice's current job")
        self.assertIsNone(self.worker.status(self.alice)["current_job"], "cleared after the run")


# ── blockers and retry-once ───────────────────────────────────────

class BlockerTests(Base):
    def test_every_blocking_class_parks_the_job_and_the_queue_carries_on(self):
        for cat in failures.BLOCKED:
            with self.subTest(blocked=cat):
                stuck = self.job(90); after = self.job(80)
                self.script.outcomes = {stuck: f"blocked:{cat}"}
                self.assertEqual(self.drain(), ["processed", "processed", "idle"])
                row = self.alice.get_by_id(stuck)
                self.assertEqual((row["stage"], row["blocked_reason"]), ("needs_you", cat))
                self.assertEqual(self.stage(after), "ready_for_review")
                self.assertEqual(self.script.ran[-2:], [stuck, after], "the next job must run")
                self.assertEqual(self.script.ran.count(stuck), 1, "a blocker is never re-run")
                self.assertFalse([e for e in self.alice.get_application(stuck)["events"]
                                  if e["detail"].get("auto_retry")])

    def test_a_blocker_in_the_middle_does_not_stop_the_jobs_behind_it(self):
        ids = [self.job(90), self.job(80), self.job(70), self.job(60)]
        self.script.outcomes = {ids[1]: "blocked:captcha", ids[2]: "blocked:login_required"}
        self.assertEqual(self.drain(), ["processed"] * 4 + ["idle"])
        self.assertEqual([self.stage(i) for i in ids],
                         ["ready_for_review", "needs_you", "needs_you", "ready_for_review"])


class RetryTests(Base):
    def events(self, jid):
        return self.alice.get_application(jid)["events"]

    def retries(self, jid):
        return [e for e in self.events(jid) if e["detail"].get("auto_retry")]

    def test_a_timeout_is_retried_once_after_the_backoff_then_fails_for_good(self):
        j = self.job(90)
        self.script.outcomes = {j: "failed:timeout"}
        self.assertEqual(self.worker.step(), "processed")
        self.assertEqual(self.stage(j), "queued")
        (retry,) = self.retries(j)
        self.assertEqual((retry["from_stage"], retry["to_stage"], retry["actor"]),
                         ("failed", "queued", "system"))
        self.assertEqual(retry["detail"], {"via": "queue", "auto_retry": 1,
                                           "classified": "timeout", "backoff_seconds": 60,
                                           "note": retry["detail"]["note"]})
        self.assertIn("Retrying (1 of 1) in 60s after a timeout", retry["detail"]["note"])

        for waited in (0, 30, 59):                           # not before the backoff is over
            self.clock.now = T0 + waited
            self.assertEqual(self.worker.step(), "backoff", waited)
        self.assertEqual(self.script.ran, [j])
        self.clock.now = T0 + 60
        self.assertEqual(self.worker.step(), "processed")    # the retry
        self.assertEqual(self.script.ran, [j, j])
        self.assertEqual(self.stage(j), "failed", "a second failure is final")
        self.assertEqual(len(self.retries(j)), 1)
        self.clock.advance(10_000)
        self.assertEqual(self.worker.step(), "idle")
        self.assertEqual(self.script.ran, [j, j], "there must be no third attempt")

    def test_unknown_is_retried_once_too_and_a_crash_counts_as_unknown(self):
        for outcome in ("failed:unknown", "raise", "nothing"):
            with self.subTest(outcome=outcome):
                j = self.job(90)
                self.script.outcomes = {j: outcome}
                crash_logged = (self.assertLogs("applier.queue_worker", "ERROR")
                                if outcome == "raise" else contextlib.nullcontext())
                with crash_logged:
                    self.worker.step()
                    self.assertEqual(self.stage(j), "queued", outcome)
                    self.clock.advance(60)
                    self.worker.step()
                self.assertEqual((self.stage(j), len(self.retries(j))), ("failed", 1), outcome)

    def test_an_exception_that_is_a_timeout_is_classified_as_one(self):
        j = self.job(90)
        self.script.outcomes = {j: "timeout_raise"}
        with self.assertLogs("applier.queue_worker", "ERROR"):
            self.worker.step()
        failed = [e for e in self.events(j) if e["to_stage"] == "failed"][0]
        self.assertEqual(failed["detail"]["classified"], "timeout")
        self.assertEqual(self.retries(j)[0]["detail"]["classified"], "timeout")

    def test_a_retry_that_works_ends_ready_for_review(self):
        j = self.job(90)
        self.script.outcomes = {j: "failed:timeout"}
        self.worker.step()
        self.script.outcomes = {}                            # the site is fine the second time
        self.clock.advance(60)
        self.worker.step()
        self.assertEqual(self.stage(j), "ready_for_review")

    def test_other_jobs_are_worked_while_one_waits_out_its_backoff(self):
        a = self.job(90); b = self.job(80); c = self.job(70)
        self.script.outcomes = {a: "failed:timeout"}
        self.assertEqual([self.worker.step() for _ in range(3)], ["processed"] * 3)
        self.assertEqual(self.script.ran, [a, b, c], "b and c must not wait for a's backoff")
        self.assertEqual(self.worker.step(), "backoff")
        self.clock.advance(60)
        self.assertEqual(self.worker.step(), "processed")
        self.assertEqual(self.script.ran, [a, b, c, a])

    def test_blocked_classes_are_never_retried_and_retryable_ones_always_are(self):
        for cat in failures.CATEGORIES:
            if cat == "question":
                continue                                     # covered by the needs_you(question) path
            with self.subTest(cat=cat):
                j = self.job(90)
                blocked = failures.is_blocked(cat)
                self.script.outcomes = {j: f"{'blocked' if blocked else 'failed'}:{cat}"}
                self.worker.step()
                self.assertEqual(self.stage(j), "needs_you" if blocked else "queued")
                self.assertEqual(len(self.retries(j)), 0 if blocked else 1)
                self.clock.advance(60); self.worker.step()   # let a pending retry run

    def test_a_blocker_class_is_never_retried_even_if_it_is_reported_as_a_plain_failure(self):
        """Defence in depth: the retry decision looks at the CLASS, not at which stage
        the job happened to land in."""
        for cat in failures.BLOCKED:
            with self.subTest(cat=cat):
                j = self.job(90)
                self.script.outcomes = {j: f"failed:{cat}"}
                self.worker.step()
                self.assertEqual(self.stage(j), "failed")
                self.assertEqual(self.retries(j), [])
                self.clock.advance(1000)

    def test_the_retry_allowance_survives_a_restart(self):
        j = self.job(90)
        self.script.outcomes = {j: "failed:timeout"}
        self.worker.step()
        restarted = self.make_worker()                       # a new process: its memory of the backoff is gone
        self.assertEqual(restarted.step(), "processed")      # so the retry may run at once...
        self.assertEqual(self.stage(j), "failed")            # ...but it can't earn a second retry
        self.assertEqual(len(self.retries(j)), 1)
        self.assertEqual(restarted.step(), "idle")

    def test_queueing_it_again_by_hand_gives_a_fresh_retry(self):
        j = self.job(90)
        self.script.outcomes = {j: "failed:timeout"}
        self.worker.step(); self.clock.advance(60); self.worker.step()
        self.assertEqual((self.stage(j), self.alice.auto_retry_count(j)), ("failed", 1))
        self.assertEqual(qw.queue_jobs(self.alice, self.settings, [j])["queued"], [j])
        self.assertEqual(self.alice.auto_retry_count(j), 0, "a person acted: the allowance resets")
        self.clock.advance(60); self.worker.step()           # fails again, and is retried again
        self.assertEqual(self.stage(j), "queued")
        self.clock.advance(60); self.worker.step()
        self.assertEqual((self.stage(j), len(self.retries(j))), ("failed", 2))

    def test_the_backoff_schedule_is_the_cap_and_the_delays(self):
        j = self.job(90)
        self.script.outcomes = {j: "failed:unknown"}
        with mock.patch.object(qw, "RETRY_BACKOFF_SECONDS", (10, 20)):
            self.worker.step()                               # at T0: retry 1 in 10 s
            self.clock.now = T0 + 9;  self.assertEqual(self.worker.step(), "backoff")
            self.clock.now = T0 + 10; self.assertEqual(self.worker.step(), "processed")
            self.assertEqual(self.stage(j), "queued")        # retry 2, in 20 s
            self.clock.now = T0 + 29; self.assertEqual(self.worker.step(), "backoff")
            self.clock.now = T0 + 30; self.assertEqual(self.worker.step(), "processed")
        self.assertEqual([e["detail"]["backoff_seconds"] for e in self.retries(j)], [10, 20])
        self.assertEqual((self.stage(j), self.script.ran.count(j)), ("failed", 3))

    def test_status_says_when_a_job_waiting_out_its_retry_may_start(self):
        j = self.job(90)
        self.script.outcomes = {j: "failed:timeout"}
        self.worker.step()
        st = self.worker.status(self.alice)
        self.assertEqual((st["queue_length"], st["next_at"]),
                         (1, datetime.fromtimestamp(T0 + 60).isoformat(timespec="seconds")))
        self.job(70)                                         # something else is startable now
        self.assertIsNone(self.worker.status(self.alice)["next_at"])

    def test_a_job_that_ruled_itself_out_in_preflight_is_not_followed_by_the_delay(self):
        self.settings.values.update(min_delay_between_applies="45", max_delay_between_applies="120")
        a = self.job(90); b = self.job(80); c = self.job(70)
        self.script.outcomes = {a: "preflight_skip"}
        self.assertEqual(self.worker.step(), "processed")
        self.assertEqual(self.stage(a), "skipped")
        self.assertEqual(self.rng_calls, [], "no browser was opened, so no pacing delay")
        self.assertEqual(self.worker.step(), "processed")    # b starts straight away
        self.assertEqual(self.worker.step(), "delay")        # a real run is paced as before
        self.assertEqual(self.script.ran, [a, b])
        self.assertEqual(self.stage(c), "queued")


# ── daily limit / delay / dry_run / shutdown ──────────────────────

class DailyLimitTests(Base):
    def test_effective_cap_table(self):
        cases = [("true", "30", 10), ("true", "10", 10), ("true", "5", 5), ("true", "0", 10),
                 ("true", "-3", 10), ("true", "junk", 10), ("garbage", "30", 10),
                 ("false", "30", 30), ("false", "5", 5), ("false", "0", 0), ("false", "junk", 30)]
        for qf, configured, want in cases:
            with self.subTest(quality_first=qf, daily_apply_limit=configured):
                s = FakeSettings(quality_first=qf, daily_apply_limit=configured)
                self.assertEqual(qw.daily_limit(s), want)

    def test_quality_first_stops_the_worker_at_ten_even_with_a_limit_of_thirty(self):
        self.applied(9)
        j = self.job(90)
        self.assertEqual(self.worker.step(), "processed")
        self.applied(1)                                     # now 10 today
        k = self.job(90)
        self.assertEqual(self.worker.step(), "limit")
        self.assertEqual((self.stage(k), self.script.ran), ("queued", [j]))

    def test_with_quality_first_off_todays_limit_applies_again(self):
        self.settings.values["quality_first"] = "false"
        self.applied(10)
        j = self.job(90)
        self.assertEqual(self.worker.step(), "processed")   # 10 of 30 is fine
        self.settings.values["daily_apply_limit"] = "11"
        self.applied(1)
        self.job(90)
        self.assertEqual(self.worker.step(), "limit")
        self.settings.values["daily_apply_limit"] = "12"    # a settings change applies at once
        self.assertEqual(self.worker.step(), "processed")

    def test_a_limit_of_zero_means_unlimited_only_without_quality_first(self):
        self.settings.values.update(quality_first="false", daily_apply_limit="0")
        self.applied(15)
        self.job(90)
        self.assertEqual(self.worker.step(), "processed")

    def test_dry_run_lifts_the_daily_limit(self):
        self.applied(10)
        j = self.job(90)
        self.assertEqual(self.worker.step(), "limit")
        self.settings.values["dry_run"] = "true"
        self.assertEqual(self.worker.step(), "processed")
        self.assertEqual(self.script.ran, [j])

    def test_the_limit_counts_only_this_users_applications(self):
        self.applied(10, db=self.bob)
        j = self.job(90)
        self.assertEqual(self.worker.step(), "processed", "Bob's applications used Alice's quota")

    def test_status_reports_the_limit_and_when_it_lifts(self):
        self.applied(10)
        self.job(90)
        st = self.worker.status(self.alice)
        tomorrow = (datetime.fromtimestamp(T0) + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        self.assertEqual((st["applied_today"], st["daily_limit"], st["queue_length"]), (10, 10, 1))
        self.assertEqual(st["next_at"], tomorrow.isoformat(timespec="seconds"))


class DelayTests(Base):
    def setUp(self):
        super().setUp()
        self.settings.values.update(min_delay_between_applies="45", max_delay_between_applies="120")

    def test_waits_out_the_delay_between_jobs_to_the_second(self):
        a = self.job(90); b = self.job(80)
        self.assertEqual(self.worker.step(), "processed")
        self.assertEqual(self.rng_calls, [(45, 120)])
        for waited in (0, 60, 119):
            self.clock.now = T0 + waited
            self.assertEqual(self.worker.step(), "delay", waited)
        self.assertEqual(self.script.ran, [a])
        self.clock.now = T0 + 120
        self.assertEqual(self.worker.step(), "processed")
        self.assertEqual(self.script.ran, [a, b])

    def test_the_first_job_after_start_up_does_not_wait(self):
        self.job(90)
        self.assertEqual(self.worker.step(), "processed")

    def test_bounds_come_from_settings_each_time_and_are_made_sane(self):
        self.job(90); self.job(80); self.job(70); self.job(60)
        self.worker.step()
        self.settings.values.update(min_delay_between_applies="100", max_delay_between_applies="50")
        self.clock.advance(1000); self.worker.step()
        self.settings.values.update(min_delay_between_applies="-5", max_delay_between_applies="junk")
        self.clock.advance(1000); self.worker.step()
        self.assertEqual(self.rng_calls, [(45, 120), (100, 100), (0, 120)])

    def test_a_blocked_or_crashed_job_is_followed_by_a_delay_too(self):
        stuck = self.job(90); boom = self.job(80); self.job(70)
        self.script.outcomes = {stuck: "blocked:captcha", boom: "raise"}
        self.assertEqual(self.worker.step(), "processed")
        self.assertEqual(self.worker.step(), "delay")
        self.clock.advance(120)
        with self.assertLogs("applier.queue_worker", "ERROR"):
            self.assertEqual(self.worker.step(), "processed")
        self.assertEqual(self.worker.step(), "delay")

    def test_dry_run_lifts_the_limit_but_not_the_delay(self):
        self.settings.values["dry_run"] = "true"
        self.job(90); self.job(80)
        self.assertEqual(self.worker.step(), "processed")
        self.assertEqual(self.worker.step(), "delay")

    def test_status_next_at_is_when_the_delay_ends(self):
        self.job(90); self.job(80)
        self.worker.step()
        self.assertEqual(self.worker.status(self.alice)["next_at"],
                         datetime.fromtimestamp(T0 + 120).isoformat(timespec="seconds"))
        self.clock.advance(500)
        self.assertIsNone(self.worker.status(self.alice)["next_at"], "it can start now")

    def test_when_both_hold_it_reports_the_later_one_and_names_the_limit(self):
        self.applied(10)
        self.job(90)
        self.settings.values["dry_run"] = "false"
        self.not_before = self.worker._not_before = T0 + 120
        self.assertEqual(self.worker.step(), "limit")
        tomorrow = (datetime.fromtimestamp(T0) + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0)
        self.assertEqual(self.worker.status(self.alice)["next_at"],
                         tomorrow.isoformat(timespec="seconds"))


class ShutdownTests(Base):
    def test_a_set_shutdown_event_stops_everything_before_the_next_job(self):
        j = self.job(90)
        self.stop.set()
        self.assertEqual(self.worker.step(), "stopped")
        self.assertEqual((self.script.ran, self.stage(j)), ([], "queued"))

    def test_the_background_thread_exits_when_shutdown_is_set(self):
        w = self.make_worker(idle_poll=30)
        w.start()
        self.assertTrue(wait_until(lambda: w.status(self.alice)["running"]))
        self.stop.set()
        self.assertTrue(wait_until(lambda: not w._thread.is_alive(), 3), "thread ignored shutdown")
        self.assertFalse(w.status(self.alice)["running"])

    def test_shutdown_during_a_job_lets_it_finish_then_takes_no_more(self):
        a = self.job(90); b = self.job(80)
        self.script.on_run = lambda job: self.stop.set()
        self.assertEqual(self.worker.step(), "processed")
        self.assertEqual(self.worker.step(), "stopped")
        self.assertEqual((self.stage(a), self.stage(b)), ("ready_for_review", "queued"))


# ── pause / resume ────────────────────────────────────────────────

class PauseTests(Base):
    def test_paused_starts_nothing_and_resume_carries_on(self):
        j = self.job(90)
        self.worker.pause()
        self.assertEqual(self.worker.step(), "paused")
        self.assertEqual((self.script.ran, self.stage(j)), ([], "queued"))
        st = self.worker.status(self.alice)
        self.assertEqual((st["paused"], st["queue_length"], st["next_at"]), (True, 1, None))
        self.worker.resume()
        self.assertFalse(self.worker.status(self.alice)["paused"])
        self.assertEqual(self.worker.step(), "processed")
        self.assertEqual(self.stage(j), "ready_for_review")

    def test_pause_does_not_interrupt_the_job_in_progress(self):
        a = self.job(90); b = self.job(80)
        self.script.on_run = lambda job: self.worker.pause()
        self.assertEqual(self.worker.step(), "processed")
        self.assertEqual(self.stage(a), "ready_for_review")
        self.assertEqual(self.worker.step(), "paused")
        self.assertEqual(self.stage(b), "queued")

    def test_pause_survives_a_restart(self):
        self.worker.pause()
        restarted = self.make_worker()                       # a new process, same settings
        self.assertTrue(restarted.is_paused())
        self.job(90)
        self.assertEqual(restarted.step(), "paused")

    def test_while_paused_there_is_no_next_start_time_even_with_a_delay_pending(self):
        self.settings.values.update(min_delay_between_applies="45", max_delay_between_applies="120")
        self.job(90); self.job(80)
        self.worker.step()                                   # leaves a 120 s delay running
        self.assertIsNotNone(self.worker.status(self.alice)["next_at"])
        self.worker.pause()
        self.assertIsNone(self.worker.status(self.alice)["next_at"])
        self.worker.resume()
        self.assertIsNotNone(self.worker.status(self.alice)["next_at"])

    def test_queueing_while_paused_is_fine_and_jobs_wait(self):
        self.worker.pause()
        ids = [self.job(90), self.job(80)]
        self.assertEqual(self.worker.status(self.alice)["queue_length"], 2)
        self.assertEqual([self.worker.step() for _ in range(2)], ["paused", "paused"])
        self.assertEqual([self.stage(i) for i in ids], ["queued", "queued"])

    def test_resume_wakes_a_sleeping_worker(self):
        w = self.make_worker(idle_poll=30)
        w.pause()                                            # paused before it starts, so no race
        w.start()
        j = self.job(90)
        time.sleep(0.3)
        self.assertEqual(self.stage(j), "queued")
        w.resume()
        self.assertTrue(wait_until(lambda: self.stage(j) == "ready_for_review", 3),
                        "resume did not wake the worker")


# ── restart recovery ──────────────────────────────────────────────

class RecoveryTests(Base):
    def setUp(self):
        super().setUp()
        self.p1 = self.job(60, queue=False); self.alice.transition(self.p1, "preparing")
        self.p2 = self.job(80, queue=False); self.alice.transition(self.p2, "preparing")
        self.q = self.job(70)
        self.rev = self.job(90, queue=False)
        self.alice.transition(self.rev, "preparing"); self.alice.transition(self.rev, "ready_for_review")
        self.ask = self.job(90, queue=False)
        self.alice.transition(self.ask, "preparing")
        self.alice.transition(self.ask, "needs_you", reason="question")
        self.bobs = self.job(95, db=self.bob, queue=False)
        self.bob.transition(self.bobs, "preparing")

    def test_only_jobs_left_in_preparing_go_back_to_queued(self):
        self.assertEqual(self.worker.recover(), 2)
        self.assertEqual([self.stage(i) for i in (self.p1, self.p2, self.q)], ["queued"] * 3)
        self.assertEqual(self.stage(self.rev), "ready_for_review")
        self.assertEqual(self.stage(self.ask), "needs_you")
        self.assertEqual(self.stage(self.bobs, self.bob), "preparing", "touched another user's job")

    def test_recovery_is_recorded_as_a_system_event_and_is_idempotent(self):
        self.worker.recover()
        ev = self.alice.get_application(self.p1)["events"][-1]
        self.assertEqual((ev["from_stage"], ev["to_stage"], ev["actor"]),
                         ("preparing", "queued", "system"))
        self.assertTrue(ev["detail"]["recovered"])
        self.assertEqual(self.worker.recover(), 0)
        self.assertEqual(len(self.alice.get_application(self.p1)["events"]), 3,
                         "found, preparing, queued - and a second recover() added nothing")

    def test_recovered_jobs_are_worked_in_score_order_with_the_rest(self):
        self.worker.recover()
        self.drain()
        self.assertEqual(self.script.ran, [self.p2, self.q, self.p1])      # 80, 70, 60

    def test_starting_the_worker_recovers_before_it_works(self):
        w = self.make_worker(idle_poll=0.05)
        w.start()
        self.assertTrue(wait_until(lambda: all(
            self.stage(i) == "ready_for_review" for i in (self.p1, self.p2, self.q))))
        first = self.alice.get_application(self.p2)["events"]
        self.assertEqual([e["to_stage"] for e in first][-4:],
                         ["preparing", "queued", "preparing", "ready_for_review"])

    def test_a_recovered_job_that_no_longer_passes_the_gate_goes_back_to_found(self):
        low = self.job(10, queue=False)
        self.alice.transition(low, "preparing")
        self.worker.recover()
        self.drain()
        self.assertEqual(self.stage(low), "found")
        self.assertNotIn(low, self.script.ran)


# ── quality-first: the score gate ─────────────────────────────────

class ScoreGateTests(Base):
    def test_threshold_is_inclusive_and_follows_the_setting(self):
        self.settings.values["auto_skip_score_threshold"] = "60"
        a = self.job(59, queue=False); b = self.job(60, queue=False); c = self.job(95, queue=False)
        res = qw.queue_jobs(self.alice, self.settings, [a, b, c])
        self.assertEqual(res["queued"], [b, c])
        self.assertEqual([(r["job_id"], r["reason"]) for r in res["rejected"]],
                         [(a, "below_threshold")])
        self.assertIn("59", res["rejected"][0]["message"])
        self.assertIn("60", res["rejected"][0]["message"])
        self.assertEqual((self.stage(a), self.stage(b)), ("found", "queued"))
        self.settings.values["auto_skip_score_threshold"] = "59"
        self.assertEqual(qw.queue_jobs(self.alice, self.settings, [a])["queued"], [a])

    def test_unscored_jobs_cannot_be_queued(self):
        j = self.job(None, queue=False)
        res = qw.queue_jobs(self.alice, self.settings, [j])
        self.assertEqual((res["queued"], res["rejected"][0]["reason"]), ([], "not_scored"))
        self.assertEqual(self.stage(j), "found")

    def test_turning_quality_first_off_removes_the_gate(self):
        self.settings.values["quality_first"] = "false"
        low = self.job(2, queue=False); none = self.job(None, queue=False)
        res = qw.queue_jobs(self.alice, self.settings, [low, none])
        self.assertEqual((res["queued"], res["rejected"]), ([low, none], []))

    def test_a_garbled_quality_first_value_keeps_the_gate_on(self):
        self.settings.values["quality_first"] = "banana"
        j = self.job(1, queue=False)
        self.assertEqual(qw.queue_jobs(self.alice, self.settings, [j])["queued"], [])

    def test_the_gate_is_rechecked_when_a_job_is_picked(self):
        keep = self.job(50); stale = self.job(40)            # both pass the default threshold of 30
        self.settings.values["auto_skip_score_threshold"] = "45"
        self.assertEqual(self.worker.step(), "processed")    # 50 runs
        self.assertEqual(self.worker.step(), "idle")         # 40 is let go, nothing else to run
        self.assertEqual(self.script.ran, [keep])
        self.assertEqual(self.stage(stale), "found")
        ev = self.alice.get_application(stale)["events"][-1]
        self.assertEqual((ev["from_stage"], ev["to_stage"], ev["actor"]),
                         ("queued", "found", "system"))
        self.assertIn("45", ev["detail"]["note"])

    def test_every_gated_job_is_let_go_in_a_single_step(self):
        ids = [self.job(40), self.job(38), self.job(36)]
        self.settings.values["auto_skip_score_threshold"] = "50"
        self.assertEqual(self.worker.step(), "idle")
        self.assertEqual([self.stage(i) for i in ids], ["found"] * 3)
        self.assertEqual(self.script.ran, [])

    def test_stages_that_cannot_be_queued_are_refused_with_a_reason(self):
        self.settings.values["quality_first"] = "false"
        for stage in lifecycle.STAGES:
            with self.subTest(stage=stage):
                j = self.job(90, queue=False)
                force(self.alice, j, stage, "question" if stage == "needs_you" else "")
                res = qw.queue_jobs(self.alice, self.settings, [j])
                if stage == "queued":
                    self.assertEqual(res["rejected"][0]["reason"], "already_queued")
                elif lifecycle.can_transition(stage, "queued"):
                    self.assertEqual(res["queued"], [j])
                else:
                    self.assertEqual(res["rejected"][0]["reason"], "illegal_stage")
                    self.assertEqual(self.stage(j), stage)

    def test_a_failed_or_skipped_job_can_be_queued_again(self):
        f = self.job(90, queue=False); self.alice.transition(f, "preparing")
        self.alice.transition(f, "failed")
        s = self.job(90, queue=False); self.alice.transition(s, "skipped")
        self.assertEqual(qw.queue_jobs(self.alice, self.settings, [f, s])["queued"], [f, s])

    def test_unknown_duplicate_and_foreign_ids(self):
        mine = self.job(90, queue=False)
        theirs = self.job(90, db=self.bob, queue=False)
        res = qw.queue_jobs(self.alice, self.settings, [mine, "nope", theirs, mine])
        self.assertEqual(res["queued"], [mine], "a duplicate id must not be queued twice")
        self.assertEqual([(r["job_id"], r["reason"]) for r in res["rejected"]],
                         [("nope", "not_found"), (theirs, "not_found")])
        self.assertEqual(self.stage(theirs, self.bob), "found")

    def test_queueing_is_recorded_as_a_user_action(self):
        j = self.job(90, queue=False)
        qw.queue_jobs(self.alice, self.settings, [j])
        ev = self.alice.get_application(j)["events"][-1]
        self.assertEqual((ev["from_stage"], ev["to_stage"], ev["actor"]), ("found", "queued", "user"))


class UnqueueTests(Base):
    def test_unqueue_returns_jobs_to_found_and_the_worker_ignores_them(self):
        a = self.job(90); b = self.job(80)
        res = qw.unqueue_jobs(self.alice, [a])
        self.assertEqual(res, {"unqueued": [a], "rejected": []})
        self.assertEqual(self.stage(a), "found")
        ev = self.alice.get_application(a)["events"][-1]
        self.assertEqual((ev["from_stage"], ev["to_stage"], ev["actor"]), ("queued", "found", "user"))
        self.drain()
        self.assertEqual(self.script.ran, [b])

    def test_only_queued_jobs_can_be_unqueued(self):
        found = self.job(90, queue=False)
        running = self.job(90, queue=False); self.alice.transition(running, "preparing")
        theirs = self.job(90, db=self.bob)
        res = qw.unqueue_jobs(self.alice, [found, running, "nope", theirs])
        self.assertEqual(res["unqueued"], [])
        self.assertEqual([r["reason"] for r in res["rejected"]],
                         ["not_queued", "not_queued", "not_found", "not_found"])
        self.assertIn("preparing", res["rejected"][1]["message"],
                      "a running job should be named as running, not just 'no longer queued'")
        self.assertEqual(self.stage(running), "preparing")
        self.assertEqual(self.stage(theirs, self.bob), "queued", "Alice unqueued Bob's job")

    def test_a_job_unqueued_after_the_worker_looked_is_not_run(self):
        """The claim is conditional on the job still being queued."""
        a = self.job(90); b = self.job(80)
        real = JobsDB(path=self.path, user_id=1)

        class Racy:
            """Alice's DB, except the user unqueues the top job right after the
            worker has listed the queue."""
            def __getattr__(inner, name):
                return getattr(real, name)
            def jobs_in_stage(inner, stage):
                rows = real.jobs_in_stage(stage)
                if stage == "queued" and not getattr(inner, "done", False):
                    inner.done = True
                    real.transition(a, "found", actor="user")
                return rows

        w = self.make_worker(db_factory=lambda: Racy())
        self.assertEqual(w.step(), "processed")
        self.assertEqual(self.script.ran, [b])
        self.assertEqual(self.stage(a), "found", "an unqueued job was run anyway")


# ── isolation, locking, status shape ──────────────────────────────

class IsolationAndLockTests(Base):
    def test_the_worker_only_touches_its_own_users_jobs(self):
        mine = self.job(10); theirs = self.job(99, db=self.bob)
        self.settings.values["quality_first"] = "false"
        self.drain()
        self.assertEqual(self.script.ran, [mine])
        self.assertEqual(self.stage(theirs, self.bob), "queued")

    def test_status_counts_are_the_callers_own(self):
        self.job(90); self.job(80); self.job(70, db=self.bob)
        self.applied(2); self.applied(1, db=self.bob)
        a, b = self.worker.status(self.alice), self.worker.status(self.bob)
        self.assertEqual((a["queue_length"], a["applied_today"]), (2, 2))
        self.assertEqual((b["queue_length"], b["applied_today"]), (1, 1))

    def test_status_has_exactly_the_documented_keys(self):
        st = self.worker.status(self.alice)
        self.assertEqual(set(st), {"running", "paused", "queue_length", "current_job",
                                   "applied_today", "daily_limit", "next_at"})
        self.assertEqual(st, {"running": False, "paused": False, "queue_length": 0,
                              "current_job": None, "applied_today": 0, "daily_limit": 10,
                              "next_at": None})

    def test_a_job_someone_is_running_by_hand_is_skipped_not_doubled_up(self):
        busy = self.job(90); other = self.job(80)
        held, released = {busy}, []                          # `busy` is being run by hand
        def acquire(jid):
            if jid in held:
                return False
            held.add(jid)
            return True
        def release(jid):
            released.append(jid)
            held.discard(jid)
        w = self.make_worker(job_lock=(acquire, release))
        self.assertEqual(w.step(), "processed")
        self.assertEqual(self.script.ran, [other])
        self.assertEqual(self.stage(busy), "queued")
        self.assertEqual(released, [other], "must release only what it acquired")

    def test_the_job_lock_is_released_even_when_the_orchestrator_crashes(self):
        j = self.job(90)
        self.script.outcomes = {j: "raise"}
        held, released = set(), []
        def acquire(jid): held.add(jid); return True
        def release(jid): held.discard(jid); released.append(jid)
        w = self.make_worker(job_lock=(acquire, release))
        with self.assertLogs("applier.queue_worker", "ERROR"):
            w.step()
        self.assertEqual((held, released), (set(), [j]))


# ── the real thread, and the real orchestrator's first pass ───────

class ThreadTests(Base):
    def test_the_background_loop_works_the_whole_queue_one_at_a_time(self):
        ids = [self.job(50 + i) for i in range(5)]
        w = self.make_worker(idle_poll=0.05)
        w.start()
        self.assertTrue(wait_until(lambda: all(self.stage(i) == "ready_for_review" for i in ids)))
        self.assertEqual(self.script.ran, list(reversed(ids)))
        self.assertEqual(self.script.max_active, 1)
        self.assertTrue(w.status(self.alice)["running"])
        w.stop()
        self.assertFalse(w.status(self.alice)["running"])

    def test_wake_makes_an_idle_worker_look_now(self):
        w = self.make_worker(idle_poll=30)                   # would otherwise sleep for 30 s
        w.start()
        time.sleep(0.2)
        j = self.job(90)
        w.wake()
        self.assertTrue(wait_until(lambda: self.stage(j) == "ready_for_review", 3),
                        "wake() did not rouse the worker")

    def test_a_second_start_does_not_spawn_a_second_worker(self):
        w = self.make_worker(idle_poll=0.05)
        w.start(); first = w._thread
        w.start()
        self.assertIs(w._thread, first)


class FakeProfile:
    def summary(self): return {}
    def flag_missing(self, **kw): pass
    def get_pending_missing(self, job_id): return []


class FakeCtx:
    pages = []
    def new_page(self): return object()
    def close(self): pass


class FakePlaywright:
    def __enter__(self): return object()
    def __exit__(self, *a): return False


class RealOrchestratorTests(Base):
    """The worker driving the actual Orchestrator.run_iter1 (browser and form
    code faked out), to prove the first pass is what it calls."""

    def setUp(self):
        super().setUp()
        quiet = type("Quiet", (), {"read": lambda self, k, d="": d})
        for p in (mock.patch("applier.session.Orchestrator._settings", staticmethod(lambda: quiet())),
                  mock.patch("applier.preflight.fetch_page", lambda url: (200, "")),
                  mock.patch("urllib.request.urlopen",
                             side_effect=AssertionError("a test touched the network"))):
            p.start()
            self.addCleanup(p.stop)

    def factory(self, job):
        from applier.session import ApplicationSession, Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        o.job_id, o.job = job["job_id"], job
        o.profile_db, o.jobs_db = FakeProfile(), JobsDB(path=self.path)
        o.session = ApplicationSession(job_id=job["job_id"], job_url="u", job_title="t",
                                       company="c", source="linkedin")
        o._open_browser = lambda pw: FakeCtx()
        return o

    def run_with(self, result, worker):
        with mock.patch("playwright.sync_api.sync_playwright", lambda: FakePlaywright()), \
             mock.patch("applier.universal.run_application", lambda *a, **k: result), \
             mock.patch("applier.universal.run_submit",
                        side_effect=AssertionError("worker submitted")):
            return worker.step()

    def test_a_clean_first_pass_ends_ready_for_review_and_is_not_submitted(self):
        j = self.job(90)
        w = self.make_worker(orchestrator_factory=self.factory)
        self.assertEqual(self.run_with({"success": True, "screenshots": ["/s/review.png"],
                                        "missing_fields": [], "error": ""}, w), "processed")
        row = self.alice.get_by_id(j)
        self.assertEqual((row["stage"], row["applied_at"]), ("ready_for_review", ""))
        ev = self.alice.get_application(j)["events"]
        self.assertEqual([e["to_stage"] for e in ev][-2:], ["preparing", "ready_for_review"])
        self.assertEqual(ev[-1]["screenshot"], "review.png")

    def test_a_form_error_is_classified_and_parks_the_job_for_you(self):
        j = self.job(90)
        w = self.make_worker(orchestrator_factory=self.factory)
        self.run_with({"success": False, "screenshots": [], "missing_fields": [],
                       "error": "CAPTCHA detected — skipping"}, w)
        row = self.alice.get_by_id(j)
        self.assertEqual((row["stage"], row["blocked_reason"]), ("needs_you", "captcha"))

    def test_the_real_queue_carries_on_after_a_blocker_and_does_not_retry_it(self):
        blocked = self.job(90); next_one = self.job(80)
        w = self.make_worker(orchestrator_factory=self.factory)
        results = iter([{"success": False, "screenshots": [], "missing_fields": [],
                         "error": "Login wall — page requires authentication and no login was detected"},
                        {"success": True, "screenshots": [], "missing_fields": [], "error": ""}])
        with mock.patch("playwright.sync_api.sync_playwright", lambda: FakePlaywright()), \
             mock.patch("applier.universal.run_application", lambda *a, **k: next(results)):
            self.assertEqual([w.step(), w.step(), w.step()], ["processed", "processed", "idle"])
        self.assertEqual((self.stage(blocked), self.alice.get_by_id(blocked)["blocked_reason"]),
                         ("needs_you", "login_required"))
        self.assertEqual(self.stage(next_one), "ready_for_review")
        self.assertFalse([e for e in self.alice.get_application(blocked)["events"]
                          if e["detail"].get("auto_retry")], "a blocker must not be auto-retried")

    def test_a_real_timeout_is_retried_once_then_fails_for_good(self):
        j = self.job(90)
        w = self.make_worker(orchestrator_factory=self.factory)
        slow = {"success": False, "screenshots": [], "missing_fields": [],
                "error": "Navigation failed: Page.goto: Timeout 30000ms exceeded."}
        with mock.patch("playwright.sync_api.sync_playwright", lambda: FakePlaywright()), \
             mock.patch("applier.universal.run_application", lambda *a, **k: slow):
            self.assertEqual(w.step(), "processed")
            self.assertEqual(self.stage(j), "queued")                 # retry scheduled
            self.assertEqual(w.step(), "backoff")
            self.clock.advance(60)
            self.assertEqual(w.step(), "processed")
        self.assertEqual(self.stage(j), "failed")
        self.assertEqual(w.step(), "idle")


# ── the DB pieces the queue relies on ─────────────────────────────

class DBSupportTests(Base):
    def test_conditional_transition_refuses_and_writes_nothing_when_the_stage_differs(self):
        j = self.job(90, queue=False)
        before = len(self.alice.get_application(j)["events"])
        with self.assertRaises(lifecycle.IllegalTransition):
            self.alice.transition(j, "preparing", from_stages=("queued",))
        self.assertEqual((self.stage(j), len(self.alice.get_application(j)["events"])),
                         ("found", before))
        self.alice.transition(j, "queued", from_stages=("found", "failed"))
        self.assertEqual(self.stage(j), "queued")

    def test_jobs_in_stage_orders_by_score_then_age_and_is_scoped(self):
        a = self.job(50); b = self.job(None); c = self.job(90); d = self.job(50)
        self.job(99, db=self.bob)
        self.assertEqual([r["job_id"] for r in self.alice.jobs_in_stage("queued")], [c, a, d, b])
        self.assertEqual(len(self.bob.jobs_in_stage("queued")), 1)
        with self.assertRaises(ValueError):
            self.alice.jobs_in_stage("applied")

    def test_stage_info_returns_only_the_callers_jobs(self):
        mine = self.job(70); theirs = self.job(80, db=self.bob)
        self.assertEqual(self.alice.stage_info([mine, theirs, "nope"]),
                         {mine: {"stage": "queued", "ai_score": 70}})

    def test_the_new_moves_are_in_the_one_table(self):
        self.assertTrue(lifecycle.can_transition("queued", "found"))
        self.assertTrue(lifecycle.can_transition("preparing", "queued"))
        self.assertFalse(lifecycle.can_transition("needs_you", "queued"))
        self.assertFalse(lifecycle.can_transition("submitted", "queued"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
