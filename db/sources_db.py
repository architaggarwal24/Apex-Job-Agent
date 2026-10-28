# db/sources_db.py
"""
db/sources_db.py — the companies whose ATS boards the agent searches, and a
log of each source's last run. Both per user, in jobs.db.

ats_companies   one row per (user, vendor, slug): where it came from
                ("harvest": learnt from a job link; "user": added by the owner),
                enabled, and what its last fetch gave.
source_runs     the last run of every source (board, aggregator or ATS vendor):
                when, how many jobs, the error if any - Settings shows this and
                the throttled sources (Remotive) read it to wait their turn.
"""

import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from core.config import cfg

DDL = (
    """CREATE TABLE IF NOT EXISTS ats_companies (
           id          INTEGER PRIMARY KEY AUTOINCREMENT,
           user_id     INTEGER NOT NULL,
           vendor      TEXT NOT NULL,
           slug        TEXT NOT NULL,
           name        TEXT NOT NULL DEFAULT '',
           origin      TEXT NOT NULL DEFAULT 'harvest',
           enabled     INTEGER NOT NULL DEFAULT 1,
           created_at  REAL NOT NULL,
           last_run_at REAL,
           last_count  INTEGER,
           last_error  TEXT NOT NULL DEFAULT '',
           UNIQUE(user_id, vendor, slug))""",
    "CREATE INDEX IF NOT EXISTS idx_ats_companies_user ON ats_companies(user_id, vendor, enabled)",
    """CREATE TABLE IF NOT EXISTS source_runs (
           user_id     INTEGER NOT NULL,
           source      TEXT NOT NULL,
           ran_at      REAL NOT NULL,
           found       INTEGER NOT NULL DEFAULT 0,
           error       TEXT NOT NULL DEFAULT '',
           PRIMARY KEY (user_id, source))""",
)

MAX_COMPANIES = 500
_SLUG_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


def iso(ts) -> str:
    return datetime.fromtimestamp(ts).isoformat(timespec="seconds") if ts else ""


def pretty_name(slug: str) -> str:
    """"acme-robotics" -> "Acme Robotics" when a board gives no company name."""
    return " ".join(w.capitalize() for w in re.split(r"[-_.]+", slug or "") if w)


def _company(r) -> dict:
    return {"id": r["id"], "vendor": r["vendor"], "slug": r["slug"], "name": r["name"] or pretty_name(r["slug"]),
            "origin": r["origin"], "enabled": bool(r["enabled"]), "created_at": iso(r["created_at"]),
            "last_run_at": iso(r["last_run_at"]), "last_count": r["last_count"], "last_error": r["last_error"]}


class SourceStore:
    def __init__(self, user_id: int, path: Path | None = None, *, clock=time.time):
        self.user_id = int(user_id)
        self.path = path or cfg.JOBS_DB_PATH
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            for ddl in DDL:
                c.execute(ddl)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(str(self.path), timeout=30)
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

    # ── companies ──────────────────────────────────────────────

    def companies(self, vendor: str | None = None, enabled_only: bool = False) -> list[dict]:
        sql, p = "SELECT * FROM ats_companies WHERE user_id=?", [self.user_id]
        if vendor:
            sql += " AND vendor=?"; p.append(vendor)
        if enabled_only:
            sql += " AND enabled=1"
        sql += " ORDER BY vendor, slug"
        with self._conn() as c:
            return [_company(r) for r in c.execute(sql, p).fetchall()]

    def add(self, vendor: str, slug: str, name: str = "", origin: str = "user") -> dict | None:
        """Add or keep a company. Returns its row when it was new, None when it
        was already there (a harvested row gets the name if it had none).
        ValueError for a bad slug/origin or when the list is full."""
        from scraper.sources.ats import VENDORS
        slug = (slug or "").strip().strip("/")
        if vendor not in VENDORS:
            raise ValueError(f"unknown ATS vendor {vendor!r}; one of: {', '.join(VENDORS)}")
        if not _SLUG_RX.match(slug):
            raise ValueError("the board slug may contain letters, digits, dots, dashes and underscores (up to 80)")
        if origin not in ("user", "harvest"):
            raise ValueError("origin must be user or harvest")
        name = (name or "").strip()[:120]
        with self._conn() as c:
            row = c.execute("SELECT * FROM ats_companies WHERE user_id=? AND vendor=? AND slug=?",
                            (self.user_id, vendor, slug)).fetchone()
            if row:
                if name and not row["name"]:
                    c.execute("UPDATE ats_companies SET name=? WHERE id=?", (name, row["id"]))
                return None
            n = c.execute("SELECT COUNT(*) FROM ats_companies WHERE user_id=?", (self.user_id,)).fetchone()[0]
            if n >= MAX_COMPANIES:
                raise ValueError(f"you already follow {MAX_COMPANIES} company boards; remove some first")
            c.execute("INSERT INTO ats_companies (user_id, vendor, slug, name, origin, created_at) VALUES (?,?,?,?,?,?)",
                      (self.user_id, vendor, slug, name, origin, self.clock()))
            return _company(c.execute("SELECT * FROM ats_companies WHERE user_id=? AND vendor=? AND slug=?",
                                      (self.user_id, vendor, slug)).fetchone())

    def set_enabled(self, company_id: int, enabled: bool) -> bool:
        with self._conn() as c:
            return c.execute("UPDATE ats_companies SET enabled=? WHERE id=? AND user_id=?",
                             (1 if enabled else 0, company_id, self.user_id)).rowcount > 0

    def remove(self, company_id: int) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM ats_companies WHERE id=? AND user_id=?", (company_id, self.user_id)).rowcount > 0

    def record_company_run(self, vendor: str, slug: str, count: int | None, error: str = "") -> None:
        with self._conn() as c:
            c.execute("UPDATE ats_companies SET last_run_at=?, last_count=?, last_error=? WHERE user_id=? AND vendor=? AND slug=?",
                      (self.clock(), count, (error or "")[:300], self.user_id, vendor, slug))

    def name_for(self, vendor: str, slug: str) -> str:
        with self._conn() as c:
            r = c.execute("SELECT name FROM ats_companies WHERE user_id=? AND vendor=? AND slug=?",
                          (self.user_id, vendor, slug)).fetchone()
        return (r["name"] if r else "") or ""

    # ── runs ───────────────────────────────────────────────────

    def record_run(self, source: str, found: int, error: str = "") -> None:
        with self._conn() as c:
            c.execute("""INSERT INTO source_runs (user_id, source, ran_at, found, error) VALUES (?,?,?,?,?)
                         ON CONFLICT(user_id, source) DO UPDATE SET ran_at=excluded.ran_at, found=excluded.found, error=excluded.error""",
                      (self.user_id, source, self.clock(), int(found), (error or "")[:300]))

    def last_run(self, source: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM source_runs WHERE user_id=? AND source=?", (self.user_id, source)).fetchone()
        return {"source": r["source"], "ran_at": r["ran_at"], "found": r["found"], "error": r["error"]} if r else None

    def runs(self) -> dict[str, dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM source_runs WHERE user_id=?", (self.user_id,)).fetchall()
        return {r["source"]: {"ran_at": iso(r["ran_at"]), "found": r["found"], "error": r["error"]} for r in rows}
