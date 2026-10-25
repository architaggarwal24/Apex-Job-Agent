# db/kits_db.py
"""
Application kits: what the agent prepares for an application BEFORE it is
reviewed - which resume variant to upload, a 3-sentence tailored summary and a
cover letter (applier/kit.py makes them).

A kit is never edited in place: every change (a regeneration, the person
overriding the resume) is a NEW VERSION, numbered from 1 per (user, job), and
the highest version is the current one. So the history of what was prepared,
and why a resume was chosen, is kept. Every query is scoped by user_id, with
no unscoped view: a cover letter is personal text.

    selection   how the resume was chosen:
                  llm       the LLM compared the job with the resumes and picked one
                  default   the user's default (only one resume, no job description,
                            or the LLM could not give a usable answer - `reason` says which)
                  override  the person chose it
                  none      the user has no resume variants (the fillers use the
                            single resume.pdf as before)
    status      ready (resume + summary + cover letter), partial (some part missing), failed
    created_by  agent | user

A kit lives in the jobs database next to the job but is not deleted with it:
like the autonomy ledger it is a record of what was prepared.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from core.config import cfg

SELECTIONS = ("llm", "default", "override", "none")
STATUSES   = ("ready", "partial", "failed")
CREATORS   = ("agent", "user")

KIT_DDL = (
    """CREATE TABLE IF NOT EXISTS application_kits (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id       INTEGER NOT NULL,
        job_id        TEXT NOT NULL,
        version       INTEGER NOT NULL,
        variant_id    INTEGER,
        variant_label TEXT NOT NULL DEFAULT '',
        selection     TEXT NOT NULL DEFAULT 'none',
        reason        TEXT NOT NULL DEFAULT '',
        summary       TEXT NOT NULL DEFAULT '',
        cover_letter  TEXT NOT NULL DEFAULT '',
        status        TEXT NOT NULL DEFAULT 'failed',
        error         TEXT NOT NULL DEFAULT '',
        created_by    TEXT NOT NULL DEFAULT 'agent',
        created_at    TEXT NOT NULL,
        UNIQUE (user_id, job_id, version)
    )""",
)

_FIELDS = ("variant_id", "variant_label", "selection", "reason", "summary", "cover_letter",
           "status", "error", "created_by")


def _kit(r) -> dict:
    return {"job_id": r["job_id"], "version": r["version"], "variant_id": r["variant_id"],
            "variant_label": r["variant_label"], "selection": r["selection"], "reason": r["reason"],
            "summary": r["summary"], "cover_letter": r["cover_letter"], "status": r["status"],
            "error": r["error"], "created_by": r["created_by"], "created_at": r["created_at"]}


class KitStore:
    def __init__(self, user_id: int, path: Path | None = None):
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            raise ValueError("KitStore needs a user_id; there is no unscoped view")
        self.user_id = user_id
        self.path = path or cfg.JOBS_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            for ddl in KIT_DDL:
                c.execute(ddl)

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

    def add_version(self, job_id: str, **fields) -> dict:
        """Store a new version of the job's kit (the next number) and return it.
        `fields` are any of variant_id, variant_label, selection, reason, summary,
        cover_letter, status, error, created_by; ValueError for anything else."""
        unknown = set(fields) - set(_FIELDS)
        if unknown:
            raise ValueError("unknown kit field(s): " + ", ".join(sorted(unknown)))
        sel, status, by = fields.get("selection", "none"), fields.get("status", "failed"), fields.get("created_by", "agent")
        if sel not in SELECTIONS or status not in STATUSES or by not in CREATORS:
            raise ValueError("bad selection, status or created_by")
        row = {"variant_id": None, "variant_label": "", "selection": sel, "reason": "", "summary": "",
               "cover_letter": "", "status": status, "error": "", "created_by": by, **fields}
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            n = c.execute("SELECT COALESCE(MAX(version), 0) + 1 AS n FROM application_kits "
                          "WHERE user_id=? AND job_id=?", (self.user_id, job_id)).fetchone()["n"]
            c.execute("INSERT INTO application_kits (user_id, job_id, version, variant_id, variant_label, "
                      "selection, reason, summary, cover_letter, status, error, created_by, created_at) "
                      "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (self.user_id, job_id, n, row["variant_id"], row["variant_label"], row["selection"],
                       row["reason"], row["summary"], row["cover_letter"], row["status"], row["error"],
                       row["created_by"], datetime.now().isoformat()))
        return self.get(job_id, n)

    def get(self, job_id: str, version: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM application_kits WHERE user_id=? AND job_id=? AND version=?",
                          (self.user_id, job_id, version)).fetchone()
        return _kit(r) if r else None

    def current(self, job_id: str) -> dict | None:
        """The latest version of this job's kit, or None if it has none."""
        with self._conn() as c:
            r = c.execute("SELECT * FROM application_kits WHERE user_id=? AND job_id=? "
                          "ORDER BY version DESC LIMIT 1", (self.user_id, job_id)).fetchone()
        return _kit(r) if r else None

    def versions(self, job_id: str) -> list[dict]:
        """Every version, newest first."""
        with self._conn() as c:
            rows = c.execute("SELECT * FROM application_kits WHERE user_id=? AND job_id=? "
                             "ORDER BY version DESC", (self.user_id, job_id)).fetchall()
        return [_kit(r) for r in rows]
