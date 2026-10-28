# db/views_db.py
"""
db/views_db.py — saved filter views for the Applications page ("Remote AI ≥ 70"),
per user, in jobs.db.

saved_views   one row per (user, name): the view's URL query (the same query the
              page writes, lib/filters.js: "?work=remote&min=70&kind=ai"). Saving
              under an existing name replaces it. At most MAX_VIEWS per user.

Only the query string is stored - it is the page's own contract with links, so a
view is exactly a bookmark the server remembers for you, and it is checked like
one: plain text, starts with "?" (or is empty), no longer than MAX_QUERY.
"""

import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from core.config import cfg

DDL = (
    """CREATE TABLE IF NOT EXISTS saved_views (
           id          INTEGER PRIMARY KEY AUTOINCREMENT,
           user_id     INTEGER NOT NULL,
           name        TEXT NOT NULL,
           query       TEXT NOT NULL DEFAULT '',
           created_at  REAL NOT NULL,
           UNIQUE(user_id, name))""",
    "CREATE INDEX IF NOT EXISTS idx_saved_views_user ON saved_views(user_id)",
)

MAX_VIEWS = 30
MAX_NAME = 60
MAX_QUERY = 1000
_QUERY_RX = re.compile(r"^(\?[A-Za-z0-9_%.,=&+\-~*'()!:/@ ]*)?$")


def clean_name(name) -> str:
    n = " ".join(str(name or "").split())
    if not n or len(n) > MAX_NAME:
        raise ValueError(f"A view needs a name of 1-{MAX_NAME} characters")
    return n


def clean_query(query) -> str:
    q = str(query or "").strip()
    if len(q) > MAX_QUERY or not _QUERY_RX.match(q):
        raise ValueError("A view's filters must be the page's query, e.g. ?work=remote&min=70")
    q = re.sub(r"(^|&)job=[^&]*", "", q.lstrip("?")).strip("&")     # an open job is not a filter
    return f"?{q}" if q else ""


class ViewStore:
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
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def list(self) -> list[dict]:
        with self._conn() as c:
            return [{"id": r["id"], "name": r["name"], "query": r["query"]} for r in c.execute(
                "SELECT id, name, query FROM saved_views WHERE user_id=? ORDER BY name COLLATE NOCASE", (self.user_id,)).fetchall()]

    def save(self, name, query) -> dict:
        """Add a view, or replace the one with the same name. ValueError when invalid or full."""
        name, query = clean_name(name), clean_query(query)
        with self._conn() as c:
            row = c.execute("SELECT id FROM saved_views WHERE user_id=? AND name=?", (self.user_id, name)).fetchone()
            if row:
                c.execute("UPDATE saved_views SET query=? WHERE id=? AND user_id=?", (query, row["id"], self.user_id))
                vid = row["id"]
            else:
                n = c.execute("SELECT COUNT(*) FROM saved_views WHERE user_id=?", (self.user_id,)).fetchone()[0]
                if n >= MAX_VIEWS:
                    raise ValueError(f"At most {MAX_VIEWS} saved views: delete one first")
                vid = c.execute("INSERT INTO saved_views (user_id, name, query, created_at) VALUES (?,?,?,?)",
                                (self.user_id, name, query, self.clock())).lastrowid
        return {"id": vid, "name": name, "query": query}

    def delete(self, view_id: int) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM saved_views WHERE id=? AND user_id=?", (int(view_id), self.user_id)).rowcount > 0
