# db/follow_ups_db.py
"""
db/follow_ups_db.py — the follow-up engine's data: what is worth a message, and when.

THIS NEVER SENDS ANYTHING. A follow-up is a reminder with a draft to read; you send it
yourself, from your own mail, and mark it done. Nothing here (or in the code that writes
the drafts, or the routes) can send a message.

The table, in the jobs database, every row and every query scoped by user_id:

    follow_ups(id, user_id, job_id, kind, due_at, draft_text, status, snoozed_until,
               rule_key, created_at, updated_at)
      kind    check_in | thank_you | status_ask
      status  pending | done | snoozed | dismissed          ("open" = pending or snoozed)

THE RULES - applied inside the same transaction as the change that triggers them, by
JobsDB.transition() and JobsDB.set_hunt():

  * entering `submitted`            check_in 7 days later and another 14 days later;
  * entering recruiter_screen, technical_round or hiring_manager
                                    thank_you the day after (one per stage entered);
  * next_step_at set or changed     status_ask when it passes (the moment itself, or for a
                                    date alone the end of that day). Changing the date takes
                                    back the open nudge for the old one; clearing it does too.

Two things stop a follow-up that no longer makes sense: leaving `submitted` closes the open
check-ins (you have your answer), and reaching a terminal stage closes the open check-ins and
nudges. A thank-you stays - it is still polite after a "no".

NO DUPLICATES. Each follow-up carries the rule_key of what caused it ("check_in:7",
"thank_you:technical_round", "next_step:2026-11-03") and (user_id, job_id, rule_key) is
unique, so the same trigger twice - a retried request, a second process, the backfill below -
makes one row.

BACKFILL. The first time the table is created, jobs already at submitted or an interview stage
get their rules applied as if they had just been entered then - but only the ones still in the
future, so a job submitted last month does not bury you in overdue prompts.

DUE. pending and due_at <= now, or snoozed and snoozed_until <= now. Time comes from an
injectable clock so it can be tested without waiting.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

from core.config import cfg
from db import hunt, lifecycle

KINDS = ("check_in", "thank_you", "status_ask")
STATUSES = ("pending", "done", "snoozed", "dismissed")
OPEN = ("pending", "snoozed")

CHECK_IN_DAYS = (7, 14)
THANK_YOU_AFTER = timedelta(days=1)
INTERVIEW_STAGES = ("recruiter_screen", "technical_round", "hiring_manager")

MAX_SNOOZE_DAYS = 365
MAX_DRAFT_CHARS = 4000


class Closed(ValueError):
    """The follow-up is already done or dismissed."""


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


# ── schema and rules (called by JobsDB, on ITS connection) ────────

def ensure_schema(c, now: datetime) -> None:
    existed = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='follow_ups'").fetchone()
    c.execute("""
        CREATE TABLE IF NOT EXISTS follow_ups (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id       INTEGER NOT NULL,
            job_id        TEXT NOT NULL,
            kind          TEXT NOT NULL CHECK (kind IN ('check_in','thank_you','status_ask')),
            due_at        TEXT NOT NULL,
            draft_text    TEXT NOT NULL DEFAULT '',
            status        TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending','done','snoozed','dismissed')),
            snoozed_until TEXT NOT NULL DEFAULT '',
            rule_key      TEXT NOT NULL,
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL,
            UNIQUE (user_id, job_id, rule_key)
        )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_follow_ups_due ON follow_ups(user_id, status, due_at)")
    if not existed:
        rows = c.execute("""SELECT job_id, user_id, stage, stage_entered_at FROM jobs
                            WHERE user_id IS NOT NULL AND stage IN (?, ?, ?, ?)""",
                         ("submitted",) + INTERVIEW_STAGES).fetchall()
        for r in rows:
            entered = hunt.parse_when(r["stage_entered_at"])
            if entered is not None:
                schedule_for_stage(c, r["user_id"], r["job_id"], r["stage"], entered, now, only_future=True)


def _insert(c, user_id: int, job_id: str, kind: str, rule_key: str, due: datetime, now: datetime) -> None:
    c.execute("""INSERT OR IGNORE INTO follow_ups
                     (user_id, job_id, kind, due_at, rule_key, created_at, updated_at)
                 VALUES (?, ?, ?, ?, ?, ?, ?)""",
              (user_id, job_id, kind, _iso(due), rule_key, _iso(now), _iso(now)))


def schedule_for_stage(c, user_id: int, job_id: str, stage: str, entered: datetime, now: datetime,
                       only_future: bool = False) -> None:
    """The follow-ups a job entering `stage` at `entered` earns."""
    wanted = []
    if stage == "submitted":
        wanted += [("check_in", f"check_in:{days}", entered + timedelta(days=days)) for days in CHECK_IN_DAYS]
    if stage in INTERVIEW_STAGES:
        wanted.append(("thank_you", f"thank_you:{stage}", entered + THANK_YOU_AFTER))
    for kind, key, due in wanted:
        if only_future and due <= now:
            continue
        _insert(c, user_id, job_id, kind, key, due, now)


def _close(c, user_id: int, job_id: str, kinds: tuple, now: datetime) -> None:
    c.execute(f"""UPDATE follow_ups SET status='dismissed', updated_at=?
                  WHERE user_id=? AND job_id=? AND status IN ('pending','snoozed')
                    AND kind IN ({','.join('?' * len(kinds))})""",
              (_iso(now), user_id, job_id) + tuple(kinds))


def on_transition(c, *, user_id, job_id: str, frm: str | None, to: str, now: datetime) -> None:
    """A job just moved frm -> to. A job nobody owns yet (no user_id) has no one to remind."""
    if user_id is None:
        return
    schedule_for_stage(c, user_id, job_id, to, now, now)
    if frm == "submitted":
        _close(c, user_id, job_id, ("check_in",), now)
    if to in lifecycle.TERMINAL:
        _close(c, user_id, job_id, ("check_in", "status_ask"), now)


def sync_next_step(c, *, user_id, job_id: str, old: str, new: str, now: datetime) -> None:
    """next_step_at went from `old` to `new` ("" = none)."""
    if user_id is None or old == new:
        return
    if old:
        c.execute("""DELETE FROM follow_ups WHERE user_id=? AND job_id=? AND rule_key=?
                     AND status IN ('pending','snoozed')""", (user_id, job_id, f"next_step:{old}"))
    if new:
        row = c.execute("SELECT stage FROM jobs WHERE job_id=? AND user_id=?", (job_id, user_id)).fetchone()
        if row is not None and row["stage"] not in lifecycle.TERMINAL:
            _insert(c, user_id, job_id, "status_ask", f"next_step:{new}", hunt.next_step_due(new), now)


# ── the store ─────────────────────────────────────────────────────

class FollowUpStore:
    """One user's follow-ups. FollowUpStore(user_id) can only ever see that user's rows."""

    _COLS = ("f.id, f.job_id, f.kind, f.due_at, f.draft_text, f.status, f.snoozed_until, "
             "f.created_at, f.updated_at, j.title, j.company, j.stage, j.contact_name, j.contact_email")
    _FROM = " FROM follow_ups f JOIN jobs j ON j.job_id=f.job_id AND j.user_id=f.user_id WHERE f.user_id=?"

    def __init__(self, user_id: int, path: Path | None = None, clock=None):
        if user_id is None:
            raise ValueError("A follow-up store needs a user")
        self.user_id = user_id
        self.path = Path(path or cfg.JOBS_DB_PATH)
        self._clock = clock or datetime.now
        from db.jobs_db import JobsDB                      # creates the tables; imported here, jobs_db imports us
        JobsDB(path=self.path, user_id=user_id, clock=self._clock)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # reads

    def get(self, follow_up_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute(f"SELECT {self._COLS}{self._FROM} AND f.id=?", (self.user_id, follow_up_id)).fetchone()
        return dict(r) if r else None

    def for_job(self, job_id: str) -> list[dict]:
        """Every follow-up on a job of this user's, whatever its status, soonest first."""
        with self._conn() as c:
            rows = c.execute(f"SELECT {self._COLS}{self._FROM} AND f.job_id=? ORDER BY f.due_at, f.id",
                             (self.user_id, job_id)).fetchall()
        return [dict(r) for r in rows]

    def due(self, now: datetime | None = None) -> list[dict]:
        """What needs you now: pending and past due, or snoozed and past its snooze. Oldest first."""
        stamp = _iso(now or self._clock())
        with self._conn() as c:
            rows = c.execute(
                f"SELECT {self._COLS}{self._FROM} AND ((f.status='pending' AND f.due_at<=?) "
                "OR (f.status='snoozed' AND f.snoozed_until<=?)) ORDER BY f.due_at, f.id",
                (self.user_id, stamp, stamp)).fetchall()
        return [dict(r) for r in rows]

    def needing_drafts(self, limit: int = 3, now: datetime | None = None) -> list[dict]:
        return [f for f in self.due(now) if not f["draft_text"]][:max(0, limit)]

    # changes - each returns the follow-up, None if it is not this user's, Closed if it is over

    def _change(self, follow_up_id: int, sets: str, params: tuple) -> dict | None:
        now = _iso(self._clock())
        with self._conn() as c:
            n = c.execute(f"UPDATE follow_ups SET {sets}, updated_at=? WHERE id=? AND user_id=? "
                          "AND status IN ('pending','snoozed')",
                          params + (now, follow_up_id, self.user_id)).rowcount
        found = self.get(follow_up_id)
        if found is not None and n == 0:
            raise Closed(f"This follow-up is already {found['status']}")
        return found

    def snooze(self, follow_up_id: int, *, until: datetime | None = None, days: int | None = None) -> dict | None:
        """Hide it until `until`, or for `days` days from now. ValueError if that is not in
        the future or is more than a year away."""
        now = self._clock()
        if (until is None) == (days is None):
            raise ValueError("Give either until or days")
        until = until if until is not None else now + timedelta(days=days)
        if until <= now:
            raise ValueError("Snooze until a time in the future")
        if until > now + timedelta(days=MAX_SNOOZE_DAYS):
            raise ValueError(f"Snooze for at most {MAX_SNOOZE_DAYS} days")
        return self._change(follow_up_id, "status='snoozed', snoozed_until=?", (_iso(until),))

    def done(self, follow_up_id: int) -> dict | None:
        return self._change(follow_up_id, "status='done'", ())

    def dismiss(self, follow_up_id: int) -> dict | None:
        return self._change(follow_up_id, "status='dismissed'", ())

    def set_draft(self, follow_up_id: int, text: str) -> dict | None:
        """Store a draft on an open follow-up. Same results as the other changes."""
        text = (text or "").strip()[:MAX_DRAFT_CHARS]
        return self._change(follow_up_id, "draft_text=?", (text,))
