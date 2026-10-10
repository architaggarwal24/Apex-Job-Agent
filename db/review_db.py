# db/review_db.py
"""
Review packets: for each application, the structured picture of what the
agent filled in (applier/review.py builds it) so you can check and correct it
before anything is submitted.

One row per (user, job): the packet is a JSON document. Every query is scoped
by user_id and there is deliberately no unscoped view (as with the answer
bank): a packet holds your personal answers.

The table is created by JobsDB (it lives in the jobs database and is cleaned
up with the job) and, for safety, here too.
"""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from core.config import cfg

REVIEW_DDL = """
    CREATE TABLE IF NOT EXISTS review_packets (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,
        job_id     TEXT NOT NULL,
        packet     TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (user_id, job_id)
    )"""


class ReviewStore:
    def __init__(self, user_id: int, path: Path | None = None):
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            raise ValueError("ReviewStore needs a user_id; there is no unscoped view")
        self.user_id = user_id
        self.path = path or cfg.JOBS_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute(REVIEW_DDL)

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

    def save(self, job_id: str, packet: dict) -> None:
        """Store (replace) the packet for one of this user's jobs."""
        now = datetime.now().isoformat()
        body = json.dumps(packet, ensure_ascii=False)
        with self._conn() as c:
            c.execute("""INSERT INTO review_packets (user_id, job_id, packet, created_at, updated_at)
                         VALUES (?,?,?,?,?)
                         ON CONFLICT(user_id, job_id) DO UPDATE SET packet=excluded.packet,
                                                                   updated_at=excluded.updated_at""",
                      (self.user_id, job_id, body, now, now))

    def get(self, job_id: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT packet FROM review_packets WHERE user_id=? AND job_id=?",
                          (self.user_id, job_id)).fetchone()
        return json.loads(r["packet"]) if r else None

    def update(self, job_id: str, mutator):
        """Read-modify-write the packet atomically. `mutator(packet)` changes
        it in place and may return a value; if it raises, nothing is saved.
        Returns (packet, mutator's value), or None if there is no packet."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT packet FROM review_packets WHERE user_id=? AND job_id=?",
                          (self.user_id, job_id)).fetchone()
            if r is None:
                return None
            packet = json.loads(r["packet"])
            result = mutator(packet)
            c.execute("UPDATE review_packets SET packet=?, updated_at=? WHERE user_id=? AND job_id=?",
                      (json.dumps(packet, ensure_ascii=False), datetime.now().isoformat(),
                       self.user_id, job_id))
            return packet, result

    def delete(self, job_id: str) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM review_packets WHERE user_id=? AND job_id=?",
                             (self.user_id, job_id)).rowcount == 1
