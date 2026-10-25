"""
db/jobs_db.py — SQLite job tracker with full pipeline tracking.

Tables:
  jobs               — Every scraped job with dedup, scoring and its ONE
                       lifecycle stage (see db/lifecycle.py)
  application_events — Audit trail / timeline: one row per stage change
  job_stages         — Legacy (pre-lifecycle) pipeline history; read only by
                       the migration, never written any more
  job_notes          — Per-job notes from the user
  follow_ups         — Reminders (with drafts) to check in / say thanks / ask for
                       news; see db/follow_ups_db.py. Never sends anything.
  job_briefs         — LLM-generated job brief + tailored resume summary
  prep_artifacts     — interview / offer prep per card, and the offers you
                       enter; see db/prep_db.py

Lifecycle: jobs.stage is the single source of truth and JobsDB.transition()
the only writer of it. jobs.status is kept as a DERIVED, read-only
projection (lifecycle.legacy_status) so the old endpoints and the Jobs page
keep working; nothing else may write it.

Dedup: normalised URL hash + title+company fuzzy match.

MULTI-USER: JobsDB(user_id=N) is a scoped view — every read/write is
restricted to that user's rows, and job_id is derived from (user_id,
url) so two users who scrape the same posting each get their own
independent copy (own status, score, notes) instead of the second
user's insert being silently deduped against the first's. JobsDB()
with no user_id is the legacy single-user mode: unscoped, sees every
row. Anything still calling it that way has NOT been migrated to
per-user isolation yet.
"""

import hashlib
import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, date, timedelta
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from core.config import cfg
from db import follow_ups_db, hunt, lifecycle, prep_db
from db.review_db import REVIEW_DDL

logger = logging.getLogger(__name__)

VALID_STATUSES = {
    "found", "applied", "skipped", "error", "in_progress",
    "captcha_blocked", "manual",
}

NOTE_MAX_CHARS = 1000

PIPELINE_STAGES = [
    "applied", "recruiter_screen", "technical_round",
    "hiring_manager", "offer", "rejected", "withdrawn",
]


def _session_states() -> dict[str, str]:
    """job_id -> apply-session state, from the session files. Only the
    migration needs this."""
    try:
        from applier.session import ApplicationSession
        return {s.job_id: s.state for s in ApplicationSession.load_all()}
    except Exception as e:
        logger.warning(f"[JobsDB] Could not read apply sessions for migration: {e}")
        return {}


def _insert_event(c, *, user_id, job_id: str, at: str, kind: str,
                  from_stage, to_stage: str, actor: str, detail: str = "{}",
                  screenshot: str = "") -> int:
    cur = c.execute("""
        INSERT INTO application_events
            (user_id, job_id, at, kind, from_stage, to_stage, actor, detail, screenshot)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (user_id, job_id, at, kind, from_stage, to_stage, actor, detail, screenshot))
    return cur.lastrowid


# Called after a stage change has COMMITTED, as fn(path, user_id, event). Background
# reactions to a stage change (the prep generator, notifications) register here when the
# app starts (main.py); a listener that raises is logged and ignored - it can never undo
# or fail the stage change, which is already stored.
_STAGE_LISTENERS: list = []


def add_stage_listener(fn) -> None:
    if fn not in _STAGE_LISTENERS:
        _STAGE_LISTENERS.append(fn)


def remove_stage_listener(fn) -> None:
    if fn in _STAGE_LISTENERS:
        _STAGE_LISTENERS.remove(fn)


def _notify_listeners(path, user_id, event: dict) -> None:
    for fn in list(_STAGE_LISTENERS):
        try:
            fn(path, user_id, event)
        except Exception as e:
            logger.warning("[JobsDB] stage listener failed: %s", type(e).__name__)


def _like(text: str) -> str:
    """Substring pattern for LIKE ... ESCAPE '\\', with the user's % and _ literal."""
    t = text.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{t}%"


def _parse_detail(text) -> dict:
    try:
        d = json.loads(text or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


def _event_dict(r) -> dict:
    """An application_events row as the API shape. user_id is internal."""
    return {"id": r["id"], "job_id": r["job_id"], "at": r["at"], "kind": r["kind"],
            "from_stage": r["from_stage"], "to_stage": r["to_stage"],
            "actor": r["actor"], "detail": _parse_detail(r["detail"]),
            "screenshot": r["screenshot"]}


class JobsDB:
    def __init__(self, path: Path | None = None, user_id: int | None = None, *,
                 clock=None, stale_days=None):
        """`clock()` -> now (default datetime.now); `stale_days()` -> the limits
        db/hunt.stale_thresholds returns (default: read from the settings). Both exist
        so the hunt's day-counting can be tested without waiting."""
        self.path    = path or cfg.JOBS_DB_PATH
        self.user_id = user_id
        self._clock  = clock or datetime.now
        self._stale_days = stale_days or hunt.stale_thresholds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ── Per-user scoping helpers ──────────────────────────────────
    # These return fixed SQL fragments (never user-supplied text) plus
    # bound parameters, so scoping can't become an injection vector.

    def _where(self) -> tuple[str, tuple]:
        """For queries with no other WHERE clause."""
        if self.user_id is None:
            return "", ()
        return " WHERE user_id=?", (self.user_id,)

    def _and(self, col: str = "user_id") -> tuple[str, tuple]:
        """For queries that already have a WHERE clause."""
        if self.user_id is None:
            return "", ()
        return f" AND {col}=?", (self.user_id,)

    def _owns(self, c, job_id: str) -> bool:
        """True if this scoped view owns job_id (always True unscoped).
        Guards the tables keyed only by job_id (briefs/stages/notes) so
        one user can't write into another's job by supplying its ID."""
        if self.user_id is None:
            return True
        return c.execute("SELECT 1 FROM jobs WHERE job_id=? AND user_id=?",
                         (job_id, self.user_id)).fetchone() is not None

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_schema(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id          TEXT UNIQUE NOT NULL,
                    url_hash        TEXT NOT NULL,
                    title_co_key    TEXT NOT NULL DEFAULT '',
                    title           TEXT NOT NULL,
                    company         TEXT NOT NULL,
                    location        TEXT DEFAULT '',
                    url             TEXT NOT NULL,
                    source          TEXT NOT NULL,
                    description     TEXT DEFAULT '',
                    salary          TEXT DEFAULT '',
                    seniority       TEXT DEFAULT '',
                    applicants      TEXT DEFAULT '',
                    ai_score        INTEGER DEFAULT NULL,
                    ai_score_reason TEXT DEFAULT '',
                    status          TEXT DEFAULT 'found',
                    error_message   TEXT DEFAULT '',
                    found_at        TEXT NOT NULL,
                    applied_at      TEXT DEFAULT '',
                    updated_at      TEXT NOT NULL
                )""")
            c.execute("""
                CREATE TABLE IF NOT EXISTS job_briefs (
                    job_id          TEXT PRIMARY KEY,
                    brief           TEXT DEFAULT '',
                    tailored_summary TEXT DEFAULT '',
                    tailored_skills TEXT DEFAULT '',
                    generated_at    TEXT NOT NULL
                )""")
            c.execute("""
                CREATE TABLE IF NOT EXISTS job_stages (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id     TEXT NOT NULL,
                    stage      TEXT NOT NULL,
                    note       TEXT DEFAULT '',
                    changed_at TEXT NOT NULL
                )""")
            c.execute("""
                CREATE TABLE IF NOT EXISTS application_events (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id    INTEGER,
                    job_id     TEXT NOT NULL,
                    at         TEXT NOT NULL,
                    kind       TEXT NOT NULL,
                    from_stage TEXT,
                    to_stage   TEXT NOT NULL,
                    actor      TEXT NOT NULL,
                    detail     TEXT NOT NULL DEFAULT '{}',
                    screenshot TEXT NOT NULL DEFAULT ''
                )""")
            c.execute("""
                CREATE TABLE IF NOT EXISTS job_notes (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id     TEXT NOT NULL,
                    note       TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_url_hash  ON jobs(url_hash)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_title_co  ON jobs(title_co_key, source)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_status    ON jobs(status)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_ai_score  ON jobs(ai_score)")
            # Add new columns to old schemas
            for col in [
                ("ai_score",        "INTEGER DEFAULT NULL"),
                ("ai_score_reason", "TEXT DEFAULT ''"),
                ("title_co_key",    "TEXT NOT NULL DEFAULT ''"),
                ("user_id",         "INTEGER DEFAULT NULL"),
                ("stage",           "TEXT DEFAULT NULL"),
                ("stage_entered_at", "TEXT DEFAULT ''"),
                ("blocked_reason",  "TEXT DEFAULT ''"),
                ("next_step_text",  "TEXT DEFAULT ''"),
                ("next_step_at",    "TEXT DEFAULT ''"),
                ("contact_name",    "TEXT DEFAULT ''"),
                ("contact_email",   "TEXT DEFAULT ''"),
                ("last_contact_at", "TEXT DEFAULT ''"),
            ]:
                try:
                    c.execute(f"ALTER TABLE jobs ADD COLUMN {col[0]} {col[1]}")
                except Exception:
                    pass
            c.execute("CREATE INDEX IF NOT EXISTS idx_user ON jobs(user_id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_stage ON jobs(user_id, stage)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_events_user "
                      "ON application_events(user_id, id)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_events_job "
                      "ON application_events(job_id, id)")

            self._migrate_lifecycle(c)

            # Rows from before multi-user support have no owner. They
            # belong to the first account (the person who was running
            # this as a single-user tool), so user 1 adopts them the
            # first time it opens its scoped view — and so do the events
            # the migration (or an unscoped writer) recorded for them.
            if self.user_id == 1:
                c.execute("UPDATE jobs SET user_id=1 WHERE user_id IS NULL")
                c.execute("UPDATE application_events SET user_id=1 "
                          "WHERE user_id IS NULL")

            c.execute(REVIEW_DDL)                  # review packets live here and go with their job
            follow_ups_db.ensure_schema(c, self._clock())
            prep_db.ensure_schema(c)

    def _migrate_lifecycle(self, c):
        """Give every pre-lifecycle row a stage. Idempotent: only rows whose
        stage is still unset are touched, so re-running (every JobsDB() does)
        is a single cheap SELECT once everything is migrated. Each migrated
        job gets one 'migrated' event per old job_stages row (plus one for
        its derived stage if that differs), so its timeline is not empty and
        the old stage history survives. Mapping rules: lifecycle.stage_from_legacy."""
        rows = c.execute("""SELECT job_id, status, user_id, found_at, updated_at,
                                   applied_at
                            FROM jobs WHERE stage IS NULL OR stage=''""").fetchall()
        if not rows:
            return
        sessions = None
        for r in rows:
            old = c.execute("SELECT stage, note, changed_at FROM job_stages "
                            "WHERE job_id=? ORDER BY id", (r["job_id"],)).fetchall()
            session_state = None
            if r["status"] == "in_progress":
                if sessions is None:
                    sessions = _session_states()
                session_state = sessions.get(r["job_id"])
            stage, reason = lifecycle.stage_from_legacy(
                r["status"], session_state, old[-1]["stage"] if old else None)

            # (to_stage, at, note) in order; the last one is the current stage.
            trail = [(lifecycle.PIPELINE_TO_STAGE[o["stage"]], o["changed_at"],
                      o["note"] or "")
                     for o in old if o["stage"] in lifecycle.PIPELINE_TO_STAGE]
            if not trail or trail[-1][0] != stage:
                at = (r["found_at"] if stage == "found"
                      else (r["applied_at"] if stage == "submitted" and r["applied_at"]
                            else r["updated_at"]))
                trail.append((stage, at, ""))

            # Claim the row FIRST. The UPDATE takes SQLite's write lock, so if
            # another process/thread is migrating the same database at the
            # same moment, it waits here, then matches nothing and writes no
            # duplicate events.
            claimed = c.execute("""UPDATE jobs SET stage=?, stage_entered_at=?,
                                          blocked_reason=?, status=?
                                   WHERE job_id=? AND (stage IS NULL OR stage='')""",
                                (stage, trail[-1][1], reason,
                                 lifecycle.legacy_status(stage, reason),
                                 r["job_id"])).rowcount
            if claimed != 1:
                continue
            prev = None
            for to_stage, at, note in trail:
                detail = {"legacy_status": r["status"]}
                if session_state:
                    detail["session_state"] = session_state
                if note:
                    detail["note"] = note
                _insert_event(c, user_id=r["user_id"], job_id=r["job_id"], at=at,
                              kind="migrated", from_stage=prev, to_stage=to_stage,
                              actor="system", detail=json.dumps(detail))
                prev = to_stage

    # ── URL helpers ───────────────────────────────────────────────

    @staticmethod
    def _norm_url(url: str) -> str:
        url = url.strip()
        try:
            p = urlparse(url)
            scheme = p.scheme.lower()
            host   = p.netloc.lower()
            path   = p.path.rstrip("/")
            qs     = parse_qs(p.query, keep_blank_values=False)

            if "linkedin.com" in host:
                return urlunparse((scheme, host, path, "", "", ""))
            if "indeed.com" in host:
                keep = {"jk": qs["jk"]} if "jk" in qs else {}
                return urlunparse((scheme, host, path, "", urlencode(keep, doseq=True), ""))

            tracking = {"utm_source","utm_medium","utm_campaign","utm_content",
                        "utm_term","ref","refId","trk","trkInfo","src","sid"}
            clean = {k: v for k, v in qs.items() if k.lower() not in tracking}
            return urlunparse((scheme, host, path, "", urlencode(clean, doseq=True), ""))
        except Exception:
            return url.rstrip("/").lower()

    @staticmethod
    def _url_hash(url: str) -> str:
        return hashlib.sha256(JobsDB._norm_url(url).encode()).hexdigest()

    def _job_id(self, url: str) -> str:
        """Unscoped: the legacy URL-only hash (so existing IDs stay
        valid). Scoped: hash of (user_id, url), so the same posting
        gets a distinct job_id per user."""
        norm = JobsDB._norm_url(url)
        if self.user_id is not None:
            norm = f"{self.user_id}:{norm}"
        return hashlib.sha256(norm.encode()).hexdigest()[:16]

    @staticmethod
    def _title_co_key(title: str, company: str) -> str:
        def clean(s):
            s = s.lower().strip()
            s = re.sub(r"[^a-z0-9 ]", "", s)
            return re.sub(r"\s+", " ", s).strip()
        return f"{clean(title)}|{clean(company)}"

    # ── Write ─────────────────────────────────────────────────────

    def add(self, job: dict) -> str | None:
        url       = job["url"]
        job_id    = self._job_id(url)
        url_hash  = self._url_hash(url)
        tc_key    = self._title_co_key(job["title"], job["company"])
        now       = datetime.now().isoformat()

        and_sql, and_p = self._and()
        with self._conn() as c:
            if c.execute("SELECT 1 FROM jobs WHERE url_hash=?" + and_sql,
                         (url_hash,) + and_p).fetchone():
                return None
            if c.execute("SELECT 1 FROM jobs WHERE title_co_key=? AND source=?" + and_sql,
                         (tc_key, job.get("source","")) + and_p).fetchone():
                return None
            try:
                c.execute("""
                    INSERT INTO jobs
                        (job_id, url_hash, title_co_key, title, company, location,
                         url, source, description, salary, seniority, applicants,
                         status, stage, stage_entered_at, blocked_reason,
                         found_at, updated_at, user_id)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'found','found',?,'',?,?,?)
                """, (job_id, url_hash, tc_key,
                      job["title"], job["company"], job.get("location",""),
                      url, job.get("source",""), job.get("description",""),
                      job.get("salary",""), job.get("seniority",""),
                      job.get("applicants",""), now, now, now, self.user_id))
                _insert_event(c, user_id=self.user_id, job_id=job_id, at=now,
                              kind="created", from_stage=None, to_stage="found",
                              actor="system")
                return job_id
            except sqlite3.IntegrityError:
                return None

    # ── Lifecycle ─────────────────────────────────────────────────

    def get_stage(self, job_id: str) -> str | None:
        """Current stage of a job this view owns, else None."""
        and_sql, and_p = self._and()
        with self._conn() as c:
            r = c.execute("SELECT stage FROM jobs WHERE job_id=?" + and_sql,
                          (job_id,) + and_p).fetchone()
        return r["stage"] if r else None

    def transition(self, job_id: str, to: str, *, actor: str = "agent",
                   reason: str | None = None, note: str = "",
                   detail: dict | None = None, screenshot: str = "",
                   from_stages: tuple | None = None) -> dict:
        """THE way a job changes stage. Validates the move against
        lifecycle.TRANSITIONS, updates the job and writes exactly one
        application_events row, atomically. Returns that event.

        LookupError      job doesn't exist or isn't this view's
        ValueError       unknown stage/actor, bad reason, unserialisable detail
        IllegalTransition  (a ValueError) the move isn't in the table, or the
                         job moved under us between the check and the write

        `from_stages`, if given, makes the move conditional: it is refused
        (IllegalTransition) unless the job is at one of those stages, checked
        in the same transaction as the write. That is how a claimer takes a
        job only if it is still where it looked a moment ago.

        `reason` is required for needs_you and forbidden otherwise. `note`
        is free text kept in the event's detail; it also becomes
        jobs.error_message when entering failed/skipped. `screenshot` is a
        file NAME (any directory part is dropped)."""
        if actor not in lifecycle.ACTORS:
            raise ValueError(f"Unknown actor: {actor!r}")
        if detail is not None and not isinstance(detail, dict):
            raise ValueError("detail must be a dict")
        note = (note or "").strip()[:NOTE_MAX_CHARS]
        body = dict(detail or {})
        if note:
            body["note"] = note
        body_json = json.dumps(body, ensure_ascii=False)      # fail before touching the DB
        shot = Path(screenshot).name if screenshot else ""
        new_reason = reason or ""
        now_dt = self._clock()
        now = now_dt.isoformat()

        and_sql, and_p = self._and()
        with self._conn() as c:
            row = c.execute("SELECT stage, user_id FROM jobs WHERE job_id=?" + and_sql,
                            (job_id,) + and_p).fetchone()
            if row is None:
                raise LookupError("Job not found")
            frm = row["stage"]
            if from_stages is not None and frm not in from_stages:
                raise lifecycle.IllegalTransition(
                    f"Job {job_id} is at {frm}, not {list(from_stages)}")
            lifecycle.check(frm, to, reason)
            cur = c.execute("""
                UPDATE jobs SET stage=?, stage_entered_at=?, blocked_reason=?,
                    status=?, error_message=?, updated_at=?,
                    applied_at=CASE WHEN ?='submitted' THEN ? ELSE applied_at END
                WHERE job_id=? AND stage=?""" + and_sql,
                (to, now, new_reason, lifecycle.legacy_status(to, new_reason),
                 note if to in ("failed", "skipped") else "", now,
                 to, now, job_id, frm) + and_p)
            if cur.rowcount != 1:
                raise lifecycle.IllegalTransition(
                    f"Job {job_id} changed stage while moving {frm} -> {to}; retry")
            eid = _insert_event(c, user_id=row["user_id"], job_id=job_id, at=now,
                                kind="transition", from_stage=frm, to_stage=to,
                                actor=actor, detail=body_json, screenshot=shot)
            follow_ups_db.on_transition(c, user_id=row["user_id"], job_id=job_id,
                                        frm=frm, to=to, now=now_dt)
            prep_db.on_transition(c, user_id=row["user_id"], job_id=job_id, to=to, now=now_dt)
            event = _event_dict(c.execute(
                "SELECT * FROM application_events WHERE id=?", (eid,)).fetchone())
        _notify_listeners(self.path, row["user_id"], event)
        return event

    def update_status(self, job_id: str, status: str, error: str = "",
                      actor: str = "system"):
        """Compatibility wrapper over transition() for the pre-lifecycle
        vocabulary (found/applied/skipped/error). A job this view doesn't
        own is silently ignored and moving to the stage a job is already at
        is a no-op, as before; an illegal move raises."""
        if status not in lifecycle.STATUS_TO_STAGE:
            raise ValueError(f"Cannot set status {status!r}; use transition()")
        stage, _ = lifecycle.STATUS_TO_STAGE[status]
        current = self.get_stage(job_id)
        if current is None or current == stage:
            return
        self.transition(job_id, stage, actor=actor, note=error)

    def update_score(self, job_id: str, score: int, reason: str):
        and_sql, and_p = self._and()
        with self._conn() as c:
            c.execute("""
                UPDATE jobs SET ai_score=?, ai_score_reason=?, updated_at=?
                WHERE job_id=?""" + and_sql,
                (score, reason, datetime.now().isoformat(), job_id) + and_p)

    def save_brief(self, job_id: str, brief: str,
                   tailored_summary: str = "", tailored_skills: str = ""):
        now = datetime.now().isoformat()
        with self._conn() as c:
            if not self._owns(c, job_id):
                raise LookupError("Job not found")
            c.execute("""
                INSERT INTO job_briefs
                    (job_id, brief, tailored_summary, tailored_skills, generated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(job_id) DO UPDATE SET
                    brief=excluded.brief,
                    tailored_summary=excluded.tailored_summary,
                    tailored_skills=excluded.tailored_skills,
                    generated_at=excluded.generated_at
            """, (job_id, brief, tailored_summary, tailored_skills, now))

    def add_stage(self, job_id: str, stage: str, note: str = "",
                  actor: str = "user"):
        """Compatibility wrapper over transition() for the old pipeline stage
        names ("applied" is now "submitted"). Recording the stage a job is
        already at is a no-op; an illegal move raises IllegalTransition."""
        if stage not in PIPELINE_STAGES:
            raise ValueError(f"Invalid stage: {stage}")
        target = lifecycle.PIPELINE_TO_STAGE[stage]
        current = self.get_stage(job_id)
        if current is None:
            raise LookupError("Job not found")
        if current == target:
            return
        self.transition(job_id, target, actor=actor, note=note)

    def add_note(self, job_id: str, note: str):
        with self._conn() as c:
            if not self._owns(c, job_id):
                raise LookupError("Job not found")
            c.execute("""
                INSERT INTO job_notes (job_id, note, created_at) VALUES (?, ?, ?)
            """, (job_id, note, datetime.now().isoformat()))

    def set_hunt(self, job_id: str, changes: dict) -> dict:
        """Change some of the five hunt fields (db/hunt.py) of a job this view owns, and
        keep the follow-ups in step (a new next_step_at schedules its nudge, a changed or
        cleared one takes back the old). Returns the application as get_application does.
        LookupError: not this view's job. ValueError: a field is invalid; nothing is written."""
        clean = hunt.clean_hunt(changes)
        now = self._clock()
        and_sql, and_p = self._and()
        with self._conn() as c:
            row = c.execute("SELECT user_id, next_step_at FROM jobs WHERE job_id=?" + and_sql,
                            (job_id,) + and_p).fetchone()
            if row is None:
                raise LookupError("Job not found")
            # the column names come from hunt.HUNT_FIELDS (clean_hunt refuses any other), never from the caller
            sets = ", ".join(f"{k}=?" for k in clean)
            c.execute(f"UPDATE jobs SET {sets}, updated_at=? WHERE job_id=?" + and_sql,
                      tuple(clean.values()) + (now.isoformat(), job_id) + and_p)
            if "next_step_at" in clean:
                follow_ups_db.sync_next_step(c, user_id=row["user_id"], job_id=job_id,
                                             old=row["next_step_at"] or "", new=clean["next_step_at"], now=now)
        return self.get_application(job_id)

    def purge_duplicates(self) -> int:
        # GROUP BY includes user_id so "duplicate" means duplicate
        # *within one user's jobs* — two users each having the same
        # posting is correct, not a duplicate to purge.
        and_sql, and_p = self._and()
        dupes = ("SELECT job_id FROM jobs WHERE id NOT IN ("
                 "SELECT MAX(id) FROM jobs GROUP BY user_id, title_co_key, source)"
                 + and_sql)
        with self._conn() as c:
            c.execute(f"DELETE FROM application_events WHERE job_id IN ({dupes})",
                      and_p)
            c.execute(f"DELETE FROM follow_ups WHERE job_id IN ({dupes})", and_p)
            c.execute(f"DELETE FROM review_packets WHERE job_id IN ({dupes})", and_p)
            c.execute(f"DELETE FROM prep_artifacts WHERE job_id IN ({dupes})", and_p)
            r = c.execute("""
                DELETE FROM jobs WHERE id NOT IN (
                    SELECT MAX(id) FROM jobs GROUP BY user_id, title_co_key, source
                )""" + and_sql, and_p)
            return r.rowcount

    def clear_all(self, include_applied: bool = False) -> int:
        """
        Wipe scraped jobs to start the next pipeline run clean.

        By default this only removes 'found' / 'skipped' / 'error' jobs —
        anything 'applied' or 'in_progress' is kept, because the Pipeline
        (tracking) board renders those by joining job_stages against this
        table; deleting an in-flight job here would silently orphan its
        stage history and make it vanish from the Pipeline view too.
        Pass include_applied=True for a true full wipe (also clears
        job_stages/job_notes/job_briefs for consistency in that case).
        """
        and_sql, and_p = self._and()
        where_sql, where_p = self._where()
        with self._conn() as c:
            if include_applied:
                # Children first (they're located via the jobs table),
                # then the jobs themselves.
                mine = "SELECT job_id FROM jobs" + where_sql
                for child in ("job_stages", "job_notes", "job_briefs",
                              "application_events", "follow_ups", "review_packets",
                              "prep_artifacts"):
                    c.execute(f"DELETE FROM {child} WHERE job_id IN ({mine})", where_p)
                r = c.execute("DELETE FROM jobs" + where_sql, where_p)
            else:
                doomed = ("SELECT job_id FROM jobs "
                          "WHERE status NOT IN ('applied','in_progress')" + and_sql)
                c.execute(f"DELETE FROM application_events WHERE job_id IN ({doomed})",
                          and_p)
                c.execute(f"DELETE FROM follow_ups WHERE job_id IN ({doomed})", and_p)
                c.execute(f"DELETE FROM review_packets WHERE job_id IN ({doomed})", and_p)
                c.execute(f"DELETE FROM prep_artifacts WHERE job_id IN ({doomed})", and_p)
                r = c.execute(
                    "DELETE FROM jobs WHERE status NOT IN ('applied','in_progress')"
                    + and_sql, and_p)
            return r.rowcount

    def count_applied_today(self) -> int:
        """Number of jobs marked 'applied' today (local server date).
        Used to enforce DAILY_APPLY_LIMIT / daily_apply_limit setting."""
        today = date.today().isoformat()
        and_sql, and_p = self._and()
        with self._conn() as c:
            r = c.execute(
                "SELECT COUNT(*) n FROM jobs WHERE applied_at LIKE ?" + and_sql,
                (f"{today}%",) + and_p
            ).fetchone()
            return r["n"]

    def get_last_applied_at(self) -> str:
        """ISO timestamp of the most recent successful application,
        or '' if none yet. Used to enforce min_delay_between_applies."""
        and_sql, and_p = self._and()
        with self._conn() as c:
            r = c.execute(
                "SELECT MAX(applied_at) v FROM jobs WHERE applied_at != ''" + and_sql,
                and_p
            ).fetchone()
            return r["v"] or ""

    # ── Read ──────────────────────────────────────────────────────

    def exists_bulk(self, urls: list[str]) -> set[str]:
        if not urls:
            return set()
        hashes = [self._url_hash(u) for u in urls]
        ph = ",".join("?" * len(hashes))
        and_sql, and_p = self._and()
        with self._conn() as c:
            rows = c.execute(
                f"SELECT url FROM jobs WHERE url_hash IN ({ph})" + and_sql,
                tuple(hashes) + and_p
            ).fetchall()
        return {r["url"] for r in rows}

    def job_ids(self) -> set[str]:
        """Every job_id this view owns — used to filter things keyed by
        job_id that live outside this DB (e.g. apply-session files)."""
        where_sql, where_p = self._where()
        with self._conn() as c:
            rows = c.execute("SELECT job_id FROM jobs" + where_sql, where_p).fetchall()
        return {r["job_id"] for r in rows}

    def get_by_id(self, job_id: str) -> dict | None:
        and_sql, and_p = self._and()
        with self._conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE job_id=?" + and_sql,
                          (job_id,) + and_p).fetchone()
            if not r:
                return None
            d = dict(r)
            brief = c.execute("SELECT * FROM job_briefs WHERE job_id=?",
                               (job_id,)).fetchone()
            d["brief"]           = brief["brief"]           if brief else ""
            d["tailored_summary"] = brief["tailored_summary"] if brief else ""
            d["tailored_skills"]  = brief["tailored_skills"]  if brief else ""
            # Old job_stages shape, derived from the audit trail.
            ph = ",".join("?" * len(lifecycle.STAGE_TO_PIPELINE))
            ev_sql, ev_p = self._and()
            d["stages"] = []
            for e in c.execute(
                    f"SELECT id, job_id, to_stage, detail, at FROM application_events "
                    f"WHERE job_id=? AND kind!='step' AND to_stage IN ({ph})"
                    + ev_sql + " ORDER BY id",
                    (job_id,) + tuple(lifecycle.STAGE_TO_PIPELINE) + ev_p).fetchall():
                d["stages"].append({
                    "id": e["id"], "job_id": e["job_id"],
                    "stage": lifecycle.STAGE_TO_PIPELINE[e["to_stage"]],
                    "note": _parse_detail(e["detail"]).get("note", ""),
                    "changed_at": e["at"]})
            d["notes"] = [dict(n) for n in c.execute(
                "SELECT * FROM job_notes WHERE job_id=? ORDER BY created_at DESC",
                (job_id,)).fetchall()]
            d.update(hunt.derive(d["stage"], d["stage_entered_at"], self._clock(), self._stale_days()))
            return d

    def get_all(self, limit: int = 1000, status: str = "") -> list[dict]:
        and_sql, and_p     = self._and()
        where_sql, where_p = self._where()
        with self._conn() as c:
            if status:
                rows = c.execute("""
                    SELECT * FROM jobs WHERE status=?""" + and_sql + """
                    ORDER BY ai_score DESC NULLS LAST, found_at DESC LIMIT ?
                """, (status,) + and_p + (limit,)).fetchall()
            else:
                rows = c.execute("""
                    SELECT * FROM jobs""" + where_sql + """
                    ORDER BY ai_score DESC NULLS LAST, found_at DESC LIMIT ?
                """, where_p + (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_pending(self, limit: int = 50) -> list[dict]:
        """Jobs ready to apply — scored above threshold, not yet applied."""
        try:
            from db.settings_db import SettingsDB
            threshold = SettingsDB.get().auto_skip_score_threshold()
        except Exception:
            threshold = cfg.AUTO_SKIP_SCORE_THRESHOLD
        and_sql, and_p = self._and()
        with self._conn() as c:
            rows = c.execute("""
                SELECT * FROM jobs
                WHERE status='found'
                AND (ai_score IS NULL OR ai_score >= ?)""" + and_sql + """
                ORDER BY ai_score DESC NULLS LAST, found_at DESC
                LIMIT ?
            """, (threshold,) + and_p + (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_pipeline(self) -> dict[str, list[dict]]:
        """Return jobs grouped by pipeline stage (old stage names: a
        submitted job is listed under "applied")."""
        and_sql, and_p = self._and()
        ph = ",".join("?" * len(lifecycle.STAGE_TO_PIPELINE))
        with self._conn() as c:
            stage_rows = c.execute(f"""
                SELECT job_id, stage, title, company, url, ai_score,
                       stage_entered_at AS changed_at
                FROM jobs WHERE stage IN ({ph})""" + and_sql + """
                ORDER BY stage_entered_at DESC
            """, tuple(lifecycle.STAGE_TO_PIPELINE) + and_p).fetchall()

        result: dict[str, list] = {s: [] for s in PIPELINE_STAGES}
        for r in stage_rows:
            d = dict(r)
            d["stage"] = lifecycle.STAGE_TO_PIPELINE[d["stage"]]
            result[d["stage"]].append(d)
        return result

    def get_stats(self) -> dict:
        today = date.today().isoformat()
        and_sql, and_p     = self._and()
        where_sql, where_p = self._where()
        ph = ",".join("?" * len(lifecycle.STAGE_TO_PIPELINE))
        with self._conn() as c:
            status_rows = c.execute(
                "SELECT status, COUNT(*) n FROM jobs" + where_sql + " GROUP BY status",
                where_p
            ).fetchall()
            today_count = c.execute(
                "SELECT COUNT(*) n FROM jobs WHERE found_at LIKE ?" + and_sql,
                (f"{today}%",) + and_p
            ).fetchone()["n"]
            applied_today = c.execute(
                "SELECT COUNT(*) n FROM jobs WHERE applied_at LIKE ?" + and_sql,
                (f"{today}%",) + and_p
            ).fetchone()["n"]
            total = c.execute("SELECT COUNT(*) n FROM jobs" + where_sql,
                              where_p).fetchone()["n"]
            scored = c.execute(
                "SELECT COUNT(*) n FROM jobs WHERE ai_score IS NOT NULL" + and_sql,
                and_p
            ).fetchone()["n"]
            avg_score = c.execute(
                "SELECT AVG(ai_score) v FROM jobs WHERE ai_score IS NOT NULL" + and_sql,
                and_p
            ).fetchone()["v"]
            pipeline_counts = c.execute(
                f"SELECT stage, COUNT(*) n FROM jobs WHERE stage IN ({ph})"
                + and_sql + " GROUP BY stage",
                tuple(lifecycle.STAGE_TO_PIPELINE) + and_p).fetchall()

        stats = {s: 0 for s in VALID_STATUSES}
        for r in status_rows:
            stats[r["status"]] = r["n"]
        stats["total"]         = total
        stats["found_today"]   = today_count
        stats["applied_today"] = applied_today
        stats["scored"]        = scored
        stats["avg_score"]     = round(avg_score or 0, 1)
        stats["pipeline"]      = {lifecycle.STAGE_TO_PIPELINE[r["stage"]]: r["n"]
                                  for r in pipeline_counts}
        return stats

    def get_analytics(self, days: int = 30) -> dict:
        """
        Analytics for the Pipeline page: applications/day, score
        distribution, top companies, conversion funnel, source split.

        Previously this ran a Python loop over each of the last 30
        days issuing 2 LIKE queries per day (60 queries) plus a few
        more — 72 total round-trips for one page load. This version
        uses GROUP BY with SQLite date functions: 2 queries for the
        daily series (found + applied), 1 for score buckets, 1 for
        top companies, 1 for the funnel, 1 for sources = 6 total.
        """
        start_date = (date.today() - timedelta(days=days - 1)).isoformat()
        and_sql, and_p     = self._and()
        where_sql, where_p = self._where()
        ph = ",".join("?" * len(lifecycle.STAGE_TO_PIPELINE))

        with self._conn() as c:
            found_rows = c.execute("""
                SELECT substr(found_at, 1, 10) d, COUNT(*) n
                FROM jobs
                WHERE found_at >= ?""" + and_sql + """
                GROUP BY d
            """, (start_date,) + and_p).fetchall()

            applied_rows = c.execute("""
                SELECT substr(applied_at, 1, 10) d, COUNT(*) n
                FROM jobs
                WHERE applied_at >= ?""" + and_sql + """
                GROUP BY d
            """, (start_date,) + and_p).fetchall()

            score_rows = c.execute("""
                SELECT
                    CASE
                        WHEN ai_score IS NULL THEN 'unscored'
                        WHEN ai_score < 30  THEN '0-29'
                        WHEN ai_score < 50  THEN '30-49'
                        WHEN ai_score < 70  THEN '50-69'
                        WHEN ai_score < 90  THEN '70-89'
                        ELSE '90-100'
                    END AS bucket,
                    COUNT(*) n
                FROM jobs""" + where_sql + """
                GROUP BY bucket
            """, where_p).fetchall()

            top_cos = c.execute("""
                SELECT company, COUNT(*) n FROM jobs
                WHERE status='applied'""" + and_sql + """ GROUP BY company
                ORDER BY n DESC LIMIT 10
            """, and_p).fetchall()

            funnel_rows = c.execute(
                f"SELECT stage, COUNT(*) n FROM jobs WHERE stage IN ({ph})"
                + and_sql + " GROUP BY stage",
                tuple(lifecycle.STAGE_TO_PIPELINE) + and_p).fetchall()

            source_rows = c.execute(
                "SELECT source, COUNT(*) n FROM jobs" + where_sql + " GROUP BY source",
                where_p
            ).fetchall()

        found_by_day   = {r["d"]: r["n"] for r in found_rows   if r["d"]}
        applied_by_day = {r["d"]: r["n"] for r in applied_rows if r["d"]}

        daily = []
        for i in range(days - 1, -1, -1):
            day = (date.today() - timedelta(days=i)).isoformat()
            daily.append({
                "date":    day,
                "found":   found_by_day.get(day, 0),
                "applied": applied_by_day.get(day, 0),
            })

        score_by_bucket = {r["bucket"]: r["n"] for r in score_rows}
        score_dist = [
            {"range": label, "count": score_by_bucket.get(label, 0)}
            for label in ["0-29", "30-49", "50-69", "70-89", "90-100"]
        ]

        funnel_order = ["applied","recruiter_screen","technical_round",
                        "hiring_manager","offer"]
        funnel_counts = {lifecycle.STAGE_TO_PIPELINE[r["stage"]]: r["n"]
                         for r in funnel_rows}
        funnel = [
            {"stage": s.replace("_"," ").title(), "count": funnel_counts.get(s, 0)}
            for s in funnel_order
        ]

        return {
            "daily":          daily,
            "score_dist":     score_dist,
            "top_companies":  [dict(r) for r in top_cos],
            "funnel":         funnel,
            "sources":        [dict(r) for r in source_rows],
        }

    # ── Applications (the lifecycle view of jobs) + audit trail ───

    _APP_COLS = ("job_id, title, company, location, url, source, salary, ai_score, "
                 "stage, stage_entered_at, blocked_reason, error_message, "
                 "found_at, applied_at, updated_at, "
                 "next_step_text, next_step_at, contact_name, contact_email, last_contact_at")

    def list_applications(self, stage: str = "", source: str = "", title: str = "",
                          q: str = "", limit: int = 500) -> list[dict]:
        """This user's jobs with their lifecycle state, most recently moved
        first. Filters (all optional, ANDed): stage (one or comma-separated),
        source (exact, any case), title (substring), q (substring of title,
        company or location). Unknown stage names raise ValueError."""
        where, params = [], []
        if self.user_id is not None:
            where.append("user_id=?"); params.append(self.user_id)
        stages = [x.strip() for x in stage.split(",") if x.strip()]
        for st in stages:
            if st not in lifecycle.STAGES:
                raise ValueError(f"Unknown stage: {st!r}")
        if stages:
            where.append(f"stage IN ({','.join('?' * len(stages))})")
            params += stages
        if source.strip():
            where.append("source = ? COLLATE NOCASE"); params.append(source.strip())
        if title.strip():
            where.append("title LIKE ? ESCAPE '\\'"); params.append(_like(title))
        if q.strip():
            where.append("(title LIKE ? ESCAPE '\\' OR company LIKE ? ESCAPE '\\' "
                         "OR location LIKE ? ESCAPE '\\')")
            params += [_like(q)] * 3
        limit = max(1, min(int(limit), 2000))
        sql = (f"SELECT {self._APP_COLS} FROM jobs"
               + (" WHERE " + " AND ".join(where) if where else "")
               + " ORDER BY stage_entered_at DESC, id DESC LIMIT ?")
        with self._conn() as c:
            rows = [dict(r) for r in c.execute(sql, tuple(params) + (limit,)).fetchall()]
        now, limits = self._clock(), self._stale_days()
        for d in rows:
            d.update(hunt.derive(d["stage"], d["stage_entered_at"], now, limits))
        return rows

    def get_application(self, job_id: str) -> dict | None:
        """One application with its full event timeline (oldest first), or
        None if this view doesn't own it."""
        and_sql, and_p = self._and()
        with self._conn() as c:
            r = c.execute(f"SELECT {self._APP_COLS}, description, ai_score_reason "
                          "FROM jobs WHERE job_id=?" + and_sql,
                          (job_id,) + and_p).fetchone()
            if r is None:
                return None
            d = dict(r)
            d.update(hunt.derive(d["stage"], d["stage_entered_at"], self._clock(), self._stale_days()))
            d["events"] = [_event_dict(e) for e in c.execute(
                "SELECT * FROM application_events WHERE job_id=?" + and_sql
                + " ORDER BY id", (job_id,) + and_p).fetchall()]
            return d

    def record_step(self, job_id: str, step: str, message: str,
                    data: dict | None = None, actor: str = "agent") -> dict:
        """Progress while working: one application_events row of kind "step",
        from_stage == to_stage == where the job is now. It changes nothing
        about the job (no stage, no timestamps), so it can never disturb the
        lifecycle; it exists so the timeline and the live stream can show
        "opened the page", "filled 12 fields", "waiting for you".

        `step` is a short code, `message` the human line, `data` extra
        fields (counts, a reason). Raises LookupError for a job this view
        doesn't own, ValueError for a bad actor/step."""
        if actor not in lifecycle.ACTORS:
            raise ValueError(f"Unknown actor: {actor!r}")
        if not isinstance(step, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", step):
            raise ValueError(f"Bad step code: {step!r}")
        body = {**(data or {}), "step": step,
                "message": (message or "").strip()[:300]}
        body_json = json.dumps(body, ensure_ascii=False)
        and_sql, and_p = self._and()
        with self._conn() as c:
            row = c.execute("SELECT stage, user_id FROM jobs WHERE job_id=?" + and_sql,
                            (job_id,) + and_p).fetchone()
            if row is None:
                raise LookupError("Job not found")
            eid = _insert_event(c, user_id=row["user_id"], job_id=job_id,
                                at=datetime.now().isoformat(), kind="step",
                                from_stage=row["stage"], to_stage=row["stage"],
                                actor=actor, detail=body_json)
            return _event_dict(c.execute(
                "SELECT * FROM application_events WHERE id=?", (eid,)).fetchone())

    def auto_retry_count(self, job_id: str) -> int:
        """How many automatic retries the job has had since a person last did
        something to it (queued it, restarted it...). The user's own action
        resets the allowance; this is derived from the audit trail, so it
        survives restarts."""
        and_sql, and_p = self._and()
        with self._conn() as c:
            rows = c.execute(
                "SELECT actor, detail FROM application_events WHERE job_id=?"
                + and_sql + " AND kind='transition' ORDER BY id DESC",
                (job_id,) + and_p).fetchall()
        n = 0
        for r in rows:
            if r["actor"] == "user":
                break
            if _parse_detail(r["detail"]).get("auto_retry"):
                n += 1
        return n

    def find_applied_duplicate(self, job_id: str) -> dict | None:
        """Another job of the SAME user with the same title and company
        (normalised, any source) that has already gone out - submitted or any
        later stage. None if there isn't one, or if the title or company is
        blank (a blank key would match everything)."""
        and_sql, and_p = self._and()
        marks = ",".join("?" * len(lifecycle.SUBMITTED_OR_LATER))
        with self._conn() as c:
            me = c.execute("SELECT title_co_key, user_id FROM jobs WHERE job_id=?"
                           + and_sql, (job_id,) + and_p).fetchone()
            if me is None:
                return None
            title, _, company = me["title_co_key"].partition("|")
            if not title or not company:
                return None
            r = c.execute(
                "SELECT job_id, title, company, source, stage FROM jobs "
                f"WHERE title_co_key=? AND user_id IS ? AND job_id!=? AND stage IN ({marks}) "
                "ORDER BY stage_entered_at DESC LIMIT 1",
                (me["title_co_key"], me["user_id"], job_id) + lifecycle.SUBMITTED_OR_LATER
            ).fetchone()
        return dict(r) if r else None

    def jobs_in_stage(self, stage: str) -> list[dict]:
        """This user's jobs currently at `stage`, in queue order: highest
        ai_score first (unscored last), then whoever got there first."""
        if stage not in lifecycle.STAGES:
            raise ValueError(f"Unknown stage: {stage!r}")
        and_sql, and_p = self._and()
        with self._conn() as c:
            rows = c.execute(
                "SELECT job_id, title, company, ai_score, stage_entered_at FROM jobs "
                "WHERE stage=?" + and_sql
                + " ORDER BY ai_score DESC NULLS LAST, stage_entered_at ASC, id ASC",
                (stage,) + and_p).fetchall()
        return [dict(r) for r in rows]

    def stage_info(self, job_ids: list[str]) -> dict[str, dict]:
        """{job_id: {"stage", "ai_score"}} for those of `job_ids` this user
        owns (others are simply absent)."""
        ids = list(dict.fromkeys(job_ids))
        out: dict[str, dict] = {}
        and_sql, and_p = self._and()
        with self._conn() as c:
            for i in range(0, len(ids), 500):                 # stay under SQLite's variable limit
                chunk = ids[i:i + 500]
                for r in c.execute(
                        f"SELECT job_id, stage, ai_score FROM jobs "
                        f"WHERE job_id IN ({','.join('?' * len(chunk))})" + and_sql,
                        tuple(chunk) + and_p).fetchall():
                    out[r["job_id"]] = {"stage": r["stage"], "ai_score": r["ai_score"]}
        return out

    def events_since(self, after_id: int = 0, limit: int = 200) -> list[dict]:
        """This user's events with id > after_id, oldest first. Ids only
        grow, so the last id seen is a complete resume point."""
        and_sql, and_p = self._and()
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM application_events WHERE id>?" + and_sql
                + " ORDER BY id LIMIT ?",
                (int(after_id),) + and_p + (max(1, min(int(limit), 1000)),)).fetchall()
        return [_event_dict(r) for r in rows]

    def event_head(self) -> int:
        """Id of this user's newest event (0 if none)."""
        where_sql, where_p = self._where()
        with self._conn() as c:
            return c.execute("SELECT COALESCE(MAX(id), 0) v FROM application_events"
                             + where_sql, where_p).fetchone()["v"]
