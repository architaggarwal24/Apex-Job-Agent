# db/notifications_db.py
"""
db/notifications_db.py — what the notifier still has to tell you, and when it last did.

Two tables in the jobs database, every row and every query scoped by user_id:

    notification_outbox(id, user_id, kind, item_key, text, status, created_at, sent_at)
      one line of news ("Engineer at Acme needs you: a question"), waiting to go out in the
      next message of its kind
      kind    needs_you | ready_for_review | follow_up_due | interview_soon | autonomy_submitted
      status  pending | sent | failed | dropped
      UNIQUE (user_id, kind, item_key) - the same news twice (a periodic check that sees the
      same due follow-up every minute, a repeated event) is ONE line, told once

    notification_sends(user_id, kind, last_sent_at)
      when a message of that kind last went out - the limit of one per kind per 10 minutes
      is counted from here

Sending, batching, quiet hours and the toggles live in core/notifier.py. Nothing here sends.
"""

import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

from core.config import cfg

KINDS = ("needs_you", "ready_for_review", "follow_up_due", "interview_soon", "autonomy_submitted")
MAX_TEXT = 300

NOTIFY_DDL = (
    """CREATE TABLE IF NOT EXISTS notification_outbox (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,
        kind       TEXT NOT NULL,
        item_key   TEXT NOT NULL,
        text       TEXT NOT NULL,
        status     TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','sent','failed','dropped')),
        created_at TEXT NOT NULL,
        sent_at    TEXT NOT NULL DEFAULT '',
        UNIQUE (user_id, kind, item_key)
    )""",
    """CREATE TABLE IF NOT EXISTS notification_sends (
        user_id      INTEGER NOT NULL,
        kind         TEXT NOT NULL,
        last_sent_at TEXT NOT NULL,
        PRIMARY KEY (user_id, kind)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_outbox_pending ON notification_outbox(user_id, status, kind, id)",
)


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


class NotificationStore:
    """One user's outbox. NotificationStore(user_id) can only ever see that user's rows."""

    def __init__(self, user_id: int, path: Path | None = None):
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            raise ValueError("A notification store needs a user_id; there is no unscoped view")
        self.user_id = user_id
        self.path = Path(path or cfg.JOBS_DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            for ddl in NOTIFY_DDL:
                c.execute(ddl)

    @contextmanager
    def _conn(self):
        with closing(sqlite3.connect(str(self.path))) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def enqueue(self, kind: str, item_key: str, text: str, now: datetime) -> bool:
        """Add one line of news. False if that exact news (kind + item_key) was already queued."""
        if kind not in KINDS:
            raise ValueError(f"Unknown notification kind {kind!r}")
        text = " ".join(str(text or "").split())[:MAX_TEXT]
        with self._conn() as c:
            return c.execute("INSERT OR IGNORE INTO notification_outbox (user_id, kind, item_key, text, created_at) "
                             "VALUES (?, ?, ?, ?, ?)", (self.user_id, kind, item_key, text, _iso(now))).rowcount == 1

    def pending(self, kind: str | None = None) -> list[dict]:
        sql = "SELECT * FROM notification_outbox WHERE user_id=? AND status='pending'"
        params: tuple = (self.user_id,)
        if kind is not None:
            sql += " AND kind=?"
            params += (kind,)
        with self._conn() as c:
            return [dict(r) for r in c.execute(sql + " ORDER BY id", params).fetchall()]

    def mark(self, ids: list[int], status: str, now: datetime) -> None:
        if not ids:
            return
        with self._conn() as c:
            c.execute(f"UPDATE notification_outbox SET status=?, sent_at=? WHERE user_id=? "
                      f"AND id IN ({','.join('?' * len(ids))})", (status, _iso(now), self.user_id, *ids))

    def last_sent(self, kind: str) -> str:
        with self._conn() as c:
            r = c.execute("SELECT last_sent_at FROM notification_sends WHERE user_id=? AND kind=?",
                          (self.user_id, kind)).fetchone()
        return r["last_sent_at"] if r else ""

    def set_last_sent(self, kind: str, now: datetime) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO notification_sends (user_id, kind, last_sent_at) VALUES (?, ?, ?) "
                      "ON CONFLICT(user_id, kind) DO UPDATE SET last_sent_at=excluded.last_sent_at",
                      (self.user_id, kind, _iso(now)))
