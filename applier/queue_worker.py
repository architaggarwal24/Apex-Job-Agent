# applier/queue_worker.py
"""
The queue worker: the user approves jobs (stage "queued") and this works
through them in the background, one at a time, highest ai_score first, by
running the existing Orchestrator's FIRST pass. Each job ends up
ready_for_review, needs_you or failed. The worker NEVER submits anything -
submitting stays a deliberate user action (/api/apply/submit).

What it respects, read fresh from settings on every decision so a change in
the Settings page applies to the very next job:
  * daily limit   - nothing new starts while applied_today >= the limit
                    (not enforced in dry_run: nothing is being applied, the
                    same rule /api/apply/submit follows);
  * delay         - after each job it waits a random min..max seconds
                    (min/max_delay_between_applies) before starting the next;
  * dry_run       - see daily limit;
  * shutdown      - core.shutdown.shutdown_event stops the loop (a first pass
                    already in the browser finishes; it cannot be interrupted);
  * pause/resume  - persisted in settings, so a paused queue stays paused
                    across a restart. Pausing never interrupts the job in
                    progress; it just stops the next one from starting.

quality_first (default ON) tightens two things: the daily cap becomes
min(daily_apply_limit, 10), and a job can only be queued if its ai_score is
at least auto_skip_score_threshold. Turn it off and today's limits apply.

On start-up anything left in "preparing" (a run a restart cut short) goes
back to "queued".

Failures (applier/failures.py classifies each one):
  * a BLOCKER (captcha, login_required, question, unsupported_form,
    naukri_chat_screening, validation_error, job_closed) parks the job in
    needs_you - the Orchestrator does that - and the queue simply carries on
    with the next job;
  * timeout / unknown are retried ONCE: the job goes failed -> queued with an
    event saying so, and is not picked again until a backoff has passed (the
    queue works on other jobs meanwhile). The retry count comes from the
    audit trail (JobsDB.auto_retry_count), so a restart can't grant extra
    retries, and the user queueing it again gives it a fresh allowance;
  * a pre-flight skip (job closed, already applied, blacklisted, salary below
    floor) opened no browser, so it isn't followed by the pacing delay.

Testability: step() makes ONE scheduling decision synchronously and returns
what it did; the thread is just step() in a loop. Clock, delay randomness,
orchestrator, DB and settings are all injectable.

Plain Python - no FastAPI import - so it can be tested on its own. The
process-wide instance and the HTTP routes live in server/routes/queue.py.
"""

import logging
import random
import threading
import time
from datetime import datetime, timedelta

from applier import failures
from core.config import cfg
from core.shutdown import register_thread, shutdown_event
from db import lifecycle

logger = logging.getLogger(__name__)

QUALITY_DAILY_CAP   = 10      # the daily cap while quality_first is on
DEFAULT_DAILY_LIMIT = 30      # defaults match db/settings_db.DEFAULT_SETTINGS
DEFAULT_MIN_DELAY   = 45
DEFAULT_MAX_DELAY   = 120
DEFAULT_THRESHOLD   = 30
MAX_QUEUE_REQUEST   = 200     # job ids accepted in one queue/unqueue call
IDLE_POLL_SECONDS   = 5.0     # how often an idle worker looks again (queue/resume wake it sooner)
PAUSE_KEY           = "queue_paused"
RETRY_BACKOFF_SECONDS = (60,)  # one automatic retry, after this long; len() is the retry cap


# ── settings-derived rules (shared by the worker and the routes) ──

def _int(settings, key: str, default: int) -> int:
    try:
        return int(str(settings.read(key, str(default))).strip())
    except (TypeError, ValueError):
        return default


def quality_first(settings) -> bool:
    """ON unless explicitly turned off - a garbled value keeps the stricter mode."""
    return str(settings.read("quality_first", "true")).strip().lower() not in (
        "false", "0", "no", "off")


def daily_limit(settings) -> int:
    """The effective daily apply cap; 0 means unlimited (only possible with
    quality_first off). With quality_first on it is min(daily_apply_limit, 10),
    and an 'unlimited' setting still gets the cap of 10."""
    configured = _int(settings, "daily_apply_limit", DEFAULT_DAILY_LIMIT)
    if not quality_first(settings):
        return configured
    return QUALITY_DAILY_CAP if configured <= 0 else min(configured, QUALITY_DAILY_CAP)


def score_threshold(settings) -> int:
    return _int(settings, "auto_skip_score_threshold", DEFAULT_THRESHOLD)


def quality_gate(ai_score, settings) -> tuple[str, str] | None:
    """(reason, message) if quality_first forbids queueing a job with this
    score, else None."""
    if not quality_first(settings):
        return None
    if ai_score is None:
        return ("not_scored", "Not scored yet; quality-first only queues scored jobs")
    threshold = score_threshold(settings)
    if ai_score < threshold:
        return ("below_threshold",
                f"Score {ai_score} is below the threshold of {threshold}")
    return None


def is_dry_run(settings) -> bool:
    try:
        return str(settings.read("dry_run", "false")).strip().lower() == "true"
    except Exception:
        return cfg.DRY_RUN


def delay_bounds(settings) -> tuple[int, int]:
    lo = max(0, _int(settings, "min_delay_between_applies", DEFAULT_MIN_DELAY))
    hi = max(0, _int(settings, "max_delay_between_applies", DEFAULT_MAX_DELAY))
    return lo, max(lo, hi)


def _next_midnight(now: float) -> float:
    d = datetime.fromtimestamp(now)
    return (d + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0).timestamp()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds")


# ── queue / unqueue (pure DB logic; the routes call these) ────────

def _rejected(job_id: str, reason: str, message: str) -> dict:
    return {"job_id": job_id, "reason": reason, "message": message}


def queue_jobs(db, settings, job_ids: list[str]) -> dict:
    """Approve jobs for the worker. Each is checked on its own, so one bad id
    doesn't sink the rest: {"queued": [ids], "rejected": [{job_id, reason,
    message}]}. Reasons: not_found, already_queued, illegal_stage,
    not_scored / below_threshold (quality_first), changed (moved while we
    were looking). `db` is a user-scoped JobsDB - other users' jobs are
    simply not_found."""
    ids = list(dict.fromkeys(job_ids))
    info = db.stage_info(ids)
    queued, rejected = [], []
    for jid in ids:
        row = info.get(jid)
        if row is None:
            rejected.append(_rejected(jid, "not_found", "Job not found"))
        elif row["stage"] == "queued":
            rejected.append(_rejected(jid, "already_queued", "Already in the queue"))
        elif not lifecycle.can_transition(row["stage"], "queued"):
            rejected.append(_rejected(
                jid, "illegal_stage", f"A job that is {row['stage']} can't be queued"))
        else:
            gate = quality_gate(row["ai_score"], settings)
            if gate:
                rejected.append(_rejected(jid, *gate))
                continue
            try:
                db.transition(jid, "queued", actor="user", from_stages=(row["stage"],))
                queued.append(jid)
            except (lifecycle.IllegalTransition, LookupError):
                rejected.append(_rejected(jid, "changed", "The job changed; try again"))
    return {"queued": queued, "rejected": rejected}


def unqueue_jobs(db, job_ids: list[str]) -> dict:
    """Take jobs back out of the queue (queued -> found). A job the worker
    has already started is not_queued: it can't be recalled."""
    ids = list(dict.fromkeys(job_ids))
    info = db.stage_info(ids)
    unqueued, rejected = [], []
    for jid in ids:
        row = info.get(jid)
        if row is None:
            rejected.append(_rejected(jid, "not_found", "Job not found"))
        elif row["stage"] != "queued":
            rejected.append(_rejected(jid, "not_queued",
                                      f"Not in the queue (it is {row['stage']})"))
        else:
            try:
                db.transition(jid, "found", actor="user", from_stages=("queued",))
                unqueued.append(jid)
            except (lifecycle.IllegalTransition, LookupError):
                rejected.append(_rejected(jid, "not_queued", "No longer in the queue"))
    return {"unqueued": unqueued, "rejected": rejected}


# ── the worker ────────────────────────────────────────────────────

def _default_orchestrator(job: dict):
    from applier.session import Orchestrator
    return Orchestrator(job)


class QueueWorker:
    """Works through ONE user's queue (the owner's: only the owner may drive
    the browser). Everything it reads or writes goes through a JobsDB scoped
    to that user."""

    def __init__(self, user_id: int, *, db_factory=None, settings_factory=None,
                 orchestrator_factory=None, job_lock=None, clock=time.time,
                 rng=random.uniform, stop_event=shutdown_event,
                 idle_poll: float = IDLE_POLL_SECONDS):
        """job_lock: optional (acquire(job_id) -> bool, release(job_id)), so the
        worker and the manual /api/apply/* routes can't run the same job at
        once. clock() -> epoch seconds. rng(lo, hi) -> the delay in seconds."""
        self.user_id       = user_id
        self._db_factory   = db_factory
        self._settings_factory = settings_factory
        self._orchestrator = orchestrator_factory or _default_orchestrator
        self._job_lock     = job_lock
        self._clock        = clock
        self._rng          = rng
        self._stop_event   = stop_event
        self._idle_poll    = idle_poll

        self._state_lock = threading.Lock()
        self._current: dict | None = None        # the job being prepared right now
        self._not_before: float | None = None    # no new job before this (delay)
        self._retry_at: dict[str, float] = {}    # job_id -> not before (retry backoff)
        self._wake   = threading.Event()
        self._stop   = threading.Event()
        self._thread: threading.Thread | None = None

    # ── dependencies, resolved fresh each time (so a swapped DB path or
    #    settings store - tests, a changed config - is always honoured) ──

    def _db(self):
        if self._db_factory:
            return self._db_factory()
        from db.jobs_db import JobsDB
        return JobsDB(user_id=self.user_id)

    def _settings(self):
        if self._settings_factory:
            return self._settings_factory()
        from db.settings_db import SettingsDB
        return SettingsDB.get()

    def _stopped(self) -> bool:
        return self._stop.is_set() or self._stop_event.is_set()

    # ── pause / resume ────────────────────────────────────────────

    def is_paused(self) -> bool:
        return str(self._settings().read(PAUSE_KEY, "false")).strip().lower() == "true"

    def pause(self):
        self._settings().write(PAUSE_KEY, "true")

    def resume(self):
        self._settings().write(PAUSE_KEY, "false")
        self.wake()

    def wake(self):
        """Tell an idle worker to look at the queue now."""
        self._wake.set()

    # ── one scheduling decision ───────────────────────────────────

    def _blocker(self, db, settings, now: float) -> tuple[str | None, float | None]:
        """Why nothing may start right now: ("limit"|"delay", the time that
        clears it) or (None, None). dry_run lifts the daily limit only: the
        delay still paces the browsing, which hits real sites either way."""
        until: dict[str, float] = {}
        if not is_dry_run(settings):
            cap = daily_limit(settings)
            if cap > 0 and db.count_applied_today() >= cap:
                until["limit"] = _next_midnight(now)
        with self._state_lock:
            not_before = self._not_before
        if not_before is not None and now < not_before:
            until["delay"] = not_before
        if not until:
            return None, None
        return ("limit" if "limit" in until else "delay"), max(until.values())

    def step(self) -> str:
        """Make one decision. Returns what happened:
          stopped  shutting down            paused  paused
          idle     nothing startable queued limit   daily limit reached
          delay    waiting out the delay    backoff everything queued is waiting
                                                    out a retry backoff
          processed  ran one job's first pass"""
        if self._stopped():
            return "stopped"
        if self.is_paused():
            return "paused"
        db, settings = self._db(), self._settings()
        self._sweep_runner_tasks(db)
        queued = db.jobs_in_stage("queued")
        if not queued:
            return "idle"
        now = self._clock()
        with self._state_lock:
            self._retry_at = {j: t for j, t in self._retry_at.items()
                              if j in {q["job_id"] for q in queued}}
            retry_at = dict(self._retry_at)
        candidates = [j for j in queued if retry_at.get(j["job_id"], 0) <= now]
        if not candidates:
            return "backoff"                      # everything left is waiting out a retry
        blocker, _ = self._blocker(db, settings, now)
        if blocker:
            return blocker

        for job in candidates:                    # already best-score-first
            job_id = job["job_id"]
            gate = quality_gate(job["ai_score"], settings)
            if gate:
                # Queued before the rules tightened, or put back by a restart.
                self._release_to_found(db, job_id, gate[1])
                continue
            if self._job_lock and not self._job_lock[0](job_id):
                continue                          # being driven by hand right now
            try:
                if not self._claim(db, job_id):
                    continue                      # unqueued/moved since we looked
                self._run_job(db, settings, job)
                return "processed"
            finally:
                if self._job_lock:
                    self._job_lock[1](job_id)
        return "idle"

    def _release_to_found(self, db, job_id: str, message: str):
        try:
            db.transition(job_id, "found", actor="system", note=message,
                          detail={"via": "queue"}, from_stages=("queued",))
        except (lifecycle.IllegalTransition, LookupError, ValueError) as e:
            logger.warning(f"[Queue] could not unqueue {job_id[:8]}: {e}")

    def _claim(self, db, job_id: str) -> bool:
        """queued -> preparing, only if it is still queued (atomic)."""
        try:
            db.transition(job_id, "preparing", actor="agent",
                          detail={"via": "queue"}, from_stages=("queued",))
            with self._state_lock:
                self._retry_at.pop(job_id, None)
            return True
        except (lifecycle.IllegalTransition, LookupError):
            return False

    def _run_job(self, db, settings, job: dict):
        job_id = job["job_id"]
        with self._state_lock:
            self._current = {"user_id": self.user_id, "job_id": job_id,
                             "title": job["title"], "company": job["company"],
                             "started_at": _iso(self._clock())}
        error, crash = "", None
        try:
            full = db.get_by_id(job_id)
            if full is None:
                raise LookupError("Job vanished before it could start")
            self._orchestrator(full).run_iter1()
        except Exception as e:                    # a bad job must not stop the queue
            logger.exception(f"[Queue] first pass crashed for {job_id[:8]}")
            error, crash = f"{type(e).__name__}: {e}"[:300], e
        finally:
            with self._state_lock:
                self._current = None

        # A first pass LEFT FOR A RUNNER (applier/executors.py) is still in flight: the
        # job stays in "preparing" until the runner reports back. Nothing has failed,
        # and there is nothing to pace - the browser work happens on the user's computer.
        if not error and db.get_stage(job_id) == "preparing" and self._left_for_runner(db, job_id):
            return

        # The Orchestrator moves the job itself. If it left it in "preparing"
        # (it crashed, or returned without a result) nothing else ever would.
        try:
            if db.get_stage(job_id) == "preparing":
                reason = error or "The first pass ended without a result"
                db.transition(job_id, "failed", actor="agent", note=reason,
                              detail={"via": "queue", "error": reason[:300],
                                      "classified": failures.classify(reason, crash)},
                              from_stages=("preparing",))
            final = self._last_transition(db, job_id)
            if final and final["to_stage"] == "failed":
                self._maybe_retry(db, job_id, final)
        except (lifecycle.IllegalTransition, LookupError, ValueError) as e:
            logger.error(f"[Queue] could not settle {job_id[:8]}: {e}")
            final = None

        if final and final["to_stage"] == "skipped" and "preflight" in final["detail"]:
            return                                 # pre-flight ruled it out: no browser, no pacing delay
        if final and final["to_stage"] == "ready_for_review" and final["detail"].get("assist"):
            return                                 # assist mode: no browser, no site contact, nothing to pace
        lo, hi = delay_bounds(settings)
        with self._state_lock:
            self._not_before = self._clock() + self._rng(lo, hi)

    def _left_for_runner(self, db, job_id: str) -> bool:
        try:
            from db.runner_db import RunnerTaskStore
            return RunnerTaskStore(self.user_id, db.path).has_open_task(job_id)
        except Exception:
            logger.exception("[Queue] could not check for a runner task")
            return False

    def _sweep_runner_tasks(self, db):
        """Re-queue / expire runner tasks whose runner went quiet (applier/runner_hub.py).
        A runner's own polling does this too; this covers the runner being gone."""
        try:
            from applier.runner_hub import RunnerHub
            RunnerHub(self.user_id, db.path, clock=self._clock).sweep()
        except Exception:
            logger.exception("[Queue] runner task sweep failed")

    @staticmethod
    def _last_transition(db, job_id: str) -> dict | None:
        app = db.get_application(job_id)
        for e in reversed(app["events"] if app else []):
            if e["kind"] == "transition":
                return e
        return None

    def _maybe_retry(self, db, job_id: str, failed_event: dict):
        """timeout / unknown get ONE automatic retry, after a backoff."""
        detail = failed_event["detail"]
        category = detail.get("classified") or failures.classify(
            detail.get("error") or detail.get("note") or "")
        if not failures.is_retryable(category):
            return
        done = db.auto_retry_count(job_id)
        if done >= len(RETRY_BACKOFF_SECONDS):
            return
        backoff = RETRY_BACKOFF_SECONDS[done]
        try:
            db.transition(job_id, "queued", actor="system",
                          note=f"Retrying ({done + 1} of {len(RETRY_BACKOFF_SECONDS)}) "
                               f"in {backoff}s after a {category} failure",
                          detail={"via": "queue", "auto_retry": done + 1,
                                  "classified": category, "backoff_seconds": backoff},
                          from_stages=("failed",))
        except (lifecycle.IllegalTransition, LookupError, ValueError) as e:
            logger.warning(f"[Queue] could not schedule a retry for {job_id[:8]}: {e}")
            return
        with self._state_lock:
            self._retry_at[job_id] = self._clock() + backoff

    # ── status ────────────────────────────────────────────────────

    def status(self, db=None) -> dict:
        """The GET /api/queue/status body, for the user `db` is scoped to
        (default: the worker's own). Counts and the current job are that
        user's only; running/paused describe the worker itself.

        running: the worker loop is alive (not "busy" - current_job says that).
        next_at: when the next queued job may start, if it is being held back
                 by the delay, the daily limit or a retry backoff; null when it can start now,
                 when something is running, or when paused / nothing is queued."""
        db = db or self._db()
        settings = self._settings()
        paused = self.is_paused()
        queued = db.jobs_in_stage("queued")
        queue_length = len(queued)
        with self._state_lock:
            current = dict(self._current) if self._current else None
        mine = current and (db.user_id is None or current["user_id"] == db.user_id)

        next_at = None
        if queue_length and not paused and current is None:
            now = self._clock()
            times = []
            _, until = self._blocker(db, settings, now)
            if until is not None:
                times.append(until)
            with self._state_lock:
                retry_at = dict(self._retry_at)
            earliest = min(retry_at.get(j["job_id"], 0) for j in queued)
            if earliest > now:                     # every queued job is waiting out a retry
                times.append(earliest)
            next_at = _iso(max(times)) if times else None
        return {
            "running":       bool(self._thread and self._thread.is_alive()) and not self._stopped(),
            "paused":        paused,
            "queue_length":  queue_length,
            "current_job":   ({k: current[k] for k in ("job_id", "title", "company", "started_at")}
                              if mine else None),
            "applied_today": db.count_applied_today(),
            "daily_limit":   daily_limit(settings),
            "next_at":       next_at,
        }

    # ── start-up recovery, thread ─────────────────────────────────

    def recover(self) -> int:
        """Anything left in "preparing" belongs to a run a restart cut short
        (nothing is running yet when this is called): back to "queued"."""
        db, n = self._db(), 0
        for job in db.jobs_in_stage("preparing"):
            if self._left_for_runner(db, job["job_id"]):
                continue                    # a runner may still deliver; its task's lease decides
            try:
                db.transition(job["job_id"], "queued", actor="system",
                              note="Interrupted by a restart; back in the queue",
                              detail={"via": "queue", "recovered": True},
                              from_stages=("preparing",))
                n += 1
            except (lifecycle.IllegalTransition, LookupError, ValueError) as e:
                logger.warning(f"[Queue] could not recover {job['job_id'][:8]}: {e}")
        if n:
            logger.info(f"[Queue] {n} interrupted job(s) returned to the queue")
        return n

    def start(self):
        """Recover, then run in a background thread. Safe to call twice."""
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        try:
            self.recover()
        except Exception:
            logger.exception("[Queue] start-up recovery failed")
        t = threading.Thread(target=self._run, name="queue-worker", daemon=True)
        self._thread = t
        t.start()
        register_thread(t)                        # so Ctrl+C waits for it (core/shutdown.py)
        logger.info("[Queue] worker started")

    def stop(self, timeout: float = 5.0):
        self._stop.set()
        self._wake.set()
        t = self._thread
        if t and t.is_alive():
            t.join(timeout)

    def _run(self):
        while not self._stopped():
            try:
                outcome = self.step()
            except Exception:
                logger.exception("[Queue] step failed")
                outcome = "error"
            if outcome != "processed":            # after a job, look again straight away
                self._sleep(self._idle_poll)
        logger.info("[Queue] worker stopped")

    def _sleep(self, seconds: float):
        """Wait up to `seconds`, waking early for wake()/stop and noticing
        the process shutdown event within half a second."""
        end = time.monotonic() + seconds
        while not self._stopped():
            left = end - time.monotonic()
            if left <= 0:
                return
            if self._wake.wait(min(left, 0.5)):
                self._wake.clear()
                return
