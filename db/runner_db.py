# db/runner_db.py
"""
The RUNNER: a small program on your own computer that does the browser work for
an application in YOUR real browser, so CAPTCHAs and logins happen where you can
deal with them and this server never holds your job-site sessions
(applier/executors.py chooses it, applier/runner_hub.py serves it).

Two things are stored here, both in the jobs database and both per user:

RUNNER TOKENS (runner_tokens). What a runner proves itself with. A token is
`apxr_` plus 256 random bits. It is shown ONCE, when created; only its SHA-256
is stored (a high-entropy random token needs no slow hash, and the digest
cannot be turned back into the token), so neither a database leak nor this API
can show it again. A token belongs to ONE user and can only ever act as them.
It can be revoked, which stops it at once, and it records when it was last
seen - that is how the `auto` executor knows a runner is there.
authenticate() is the single place that looks a token up without knowing the
user (it is how the user is learnt); everything after it is scoped by user_id.

RUNNER TASKS (runner_tasks). One unit of browser work for one application,
left for a runner by the RunnerExecutor:

    kind      apply   the first pass: open the form, fill it, screenshot it
              refill  the second pass, after you answered its questions
              submit  fill the form again and click Submit (the one irreversible step)
    status    queued -> claimed -> done
              queued/claimed -> expired | cancelled

A claimed task has a LEASE: it must be renewed (every event, screenshot or
resume download renews it) or the runner is taken to have disappeared. Then an
apply/refill task goes back to `queued` for another claim (up to MAX_ATTEMPTS
claims in all) and a task nobody picks up expires after QUEUE_TTL. A SUBMIT
task is never re-queued once claimed: the runner may already have clicked
Submit, and a second attempt would apply twice - it is marked expired and a
person is asked to check (applier/runner_hub.py).

Every query is scoped by user_id; there is no unscoped view of tasks.
Times are epoch seconds (REAL), and every method that needs "now" takes it.
"""

from __future__ import annotations      # the classes have a method called `list`; annotations must not see it

import hashlib
import json
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from core.config import cfg

TOKEN_PREFIX         = "apxr_"
TOKEN_MAX_LENGTH     = 200            # longer than any token we issue; refuse to hash megabytes
MAX_ACTIVE_TOKENS    = 10
LABEL_MAX            = 60
SEEN_WINDOW_SECONDS  = 60             # "seen recently": a runner long-polls at least every 25 s

LEASE_SECONDS        = 120
QUEUE_TTL_SECONDS    = 30 * 60
MAX_ATTEMPTS         = 3
MAX_EVENTS_PER_TASK  = 300
MAX_SCREENSHOTS_PER_TASK = 20

KINDS    = ("apply", "refill", "submit")
STATUSES = ("queued", "claimed", "done", "expired", "cancelled")
OPEN     = ("queued", "claimed")

RUNNER_DDL = (
    """CREATE TABLE IF NOT EXISTS runner_tokens (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      INTEGER NOT NULL,
        label        TEXT NOT NULL,
        token_hash   TEXT NOT NULL UNIQUE,
        created_at   REAL NOT NULL,
        last_seen_at REAL NOT NULL DEFAULT 0,
        revoked_at   REAL NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_runner_tokens_user ON runner_tokens(user_id, id)",
    """CREATE TABLE IF NOT EXISTS runner_tasks (
        id            TEXT PRIMARY KEY,
        user_id       INTEGER NOT NULL,
        job_id        TEXT NOT NULL,
        kind          TEXT NOT NULL,
        params        TEXT NOT NULL DEFAULT '{}',
        status        TEXT NOT NULL DEFAULT 'queued',
        attempts      INTEGER NOT NULL DEFAULT 0,
        claimed_by    INTEGER,
        claimed_at    REAL NOT NULL DEFAULT 0,
        lease_until   REAL NOT NULL DEFAULT 0,
        created_at    REAL NOT NULL,
        expires_at    REAL NOT NULL,
        finished_at   REAL NOT NULL DEFAULT 0,
        outcome       TEXT NOT NULL DEFAULT '',
        event_count   INTEGER NOT NULL DEFAULT 0,
        screenshots   TEXT NOT NULL DEFAULT '[]'
    )""",
    "CREATE INDEX IF NOT EXISTS idx_runner_tasks_user ON runner_tasks(user_id, status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_runner_tasks_job  ON runner_tasks(user_id, job_id, status)",
)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def new_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts).isoformat() if ts else ""


def _token_public(r) -> dict:
    """A token as the API shows it: never its hash, never the token."""
    return {"id": r["id"], "label": r["label"], "created_at": iso(r["created_at"]),
            "last_seen_at": iso(r["last_seen_at"]), "revoked_at": iso(r["revoked_at"]),
            "active": not r["revoked_at"]}


def _task(r) -> dict:
    return {"id": r["id"], "user_id": r["user_id"], "job_id": r["job_id"], "kind": r["kind"],
            "params": json.loads(r["params"]), "status": r["status"], "attempts": r["attempts"],
            "claimed_by": r["claimed_by"], "claimed_at": r["claimed_at"], "lease_until": r["lease_until"],
            "created_at": r["created_at"], "expires_at": r["expires_at"], "finished_at": r["finished_at"],
            "outcome": r["outcome"], "event_count": r["event_count"],
            "screenshots": json.loads(r["screenshots"])}


class _Db:
    def __init__(self, path: Path | None = None):
        self.path = path or cfg.JOBS_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            for ddl in RUNNER_DDL:
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


# ── tokens ────────────────────────────────────────────────────────

def authenticate(raw, path: Path | None = None, *, now: float | None = None, users=None) -> dict | None:
    """Who is this runner token? {"token_id", "user_id", "label"} - or None for
    anything that is not a live token: not text, malformed, unknown, REVOKED, or
    belonging to an account that no longer exists / is inactive. Marks the
    token as seen. The one lookup that is not scoped by user (it finds the user)."""
    if not isinstance(raw, str) or not raw.startswith(TOKEN_PREFIX) or not 0 < len(raw) <= TOKEN_MAX_LENGTH:
        return None
    db = _Db(path)
    with db._conn() as c:
        row = c.execute("SELECT * FROM runner_tokens WHERE token_hash=?", (hash_token(raw),)).fetchone()
        if row is None or row["revoked_at"]:
            return None
        if users is None:
            from db.users_db import UsersDB
            users = UsersDB.get()
        if users.get_by_id(row["user_id"]) is None:
            return None
        c.execute("UPDATE runner_tokens SET last_seen_at=? WHERE id=? AND revoked_at=0",
                  (now if now is not None else time.time(), row["id"]))
    return {"token_id": row["id"], "user_id": row["user_id"], "label": row["label"]}


class RunnerTokenStore(_Db):
    def __init__(self, user_id: int, path: Path | None = None):
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            raise ValueError("RunnerTokenStore needs a user_id; there is no unscoped view")
        self.user_id = user_id
        super().__init__(path)

    def create(self, label, now: float | None = None) -> dict:
        """A new token for this user. The returned dict is the ONLY time the
        token is available: {"id", "label", "token", ...}. ValueError for a bad
        label or if the user already has MAX_ACTIVE_TOKENS live ones."""
        label = " ".join(label.split()) if isinstance(label, str) else ""
        if not label:
            raise ValueError("a label is required")
        if len(label) > LABEL_MAX:
            raise ValueError(f"the label is longer than {LABEL_MAX} characters")
        token = new_token()
        with self._conn() as c:
            live = c.execute("SELECT COUNT(*) n FROM runner_tokens WHERE user_id=? AND revoked_at=0",
                             (self.user_id,)).fetchone()["n"]
            if live >= MAX_ACTIVE_TOKENS:
                raise ValueError(f"you already have {MAX_ACTIVE_TOKENS} active runner tokens; revoke one first")
            cur = c.execute("INSERT INTO runner_tokens (user_id, label, token_hash, created_at) VALUES (?,?,?,?)",
                            (self.user_id, label, hash_token(token), now if now is not None else time.time()))
            row = c.execute("SELECT * FROM runner_tokens WHERE id=?", (cur.lastrowid,)).fetchone()
        return {**_token_public(row), "token": token}

    def list(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM runner_tokens WHERE user_id=? ORDER BY id", (self.user_id,)).fetchall()
        return [_token_public(r) for r in rows]

    def revoke(self, token_id: int, now: float | None = None) -> bool:
        """Revoke one of this user's tokens (idempotent). False if it is not theirs."""
        with self._conn() as c:
            row = c.execute("SELECT id FROM runner_tokens WHERE id=? AND user_id=?",
                            (token_id, self.user_id)).fetchone()
            if row is None:
                return False
            c.execute("UPDATE runner_tokens SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at=0",
                      (now if now is not None else time.time(), token_id, self.user_id))
        return True

    def touch(self, token_id: int, now: float | None = None) -> None:
        """Mark one of this user's live tokens as seen (a runner waiting in a long poll)."""
        with self._conn() as c:
            c.execute("UPDATE runner_tokens SET last_seen_at=? WHERE id=? AND user_id=? AND revoked_at=0",
                      (now if now is not None else time.time(), token_id, self.user_id))

    def seen_recently(self, now: float | None = None, window: float = SEEN_WINDOW_SECONDS) -> bool:
        """Has one of this user's live tokens been used in the last `window` seconds?"""
        now = now if now is not None else time.time()
        with self._conn() as c:
            return c.execute("SELECT 1 FROM runner_tokens WHERE user_id=? AND revoked_at=0 AND last_seen_at>=?",
                             (self.user_id, now - window)).fetchone() is not None


# ── tasks ─────────────────────────────────────────────────────────

class RunnerTaskStore(_Db):
    def __init__(self, user_id: int, path: Path | None = None):
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            raise ValueError("RunnerTaskStore needs a user_id; there is no unscoped view")
        self.user_id = user_id
        super().__init__(path)

    def enqueue(self, job_id: str, kind: str, params: dict | None = None, now: float | None = None,
                ttl: float = QUEUE_TTL_SECONDS) -> tuple[dict, bool]:
        """Queue a task for a runner. (task, created): a job has at most ONE open
        task, so asking again while one is queued or claimed returns that one
        (created False) - a double click must not make two runs."""
        if kind not in KINDS:
            raise ValueError(f"unknown task kind {kind!r}")
        now = now if now is not None else time.time()
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            open_row = c.execute(
                "SELECT * FROM runner_tasks WHERE user_id=? AND job_id=? AND status IN ('queued','claimed') "
                "ORDER BY created_at LIMIT 1", (self.user_id, job_id)).fetchone()
            if open_row is not None:
                return _task(open_row), False
            task_id = uuid.uuid4().hex
            c.execute("INSERT INTO runner_tasks (id, user_id, job_id, kind, params, created_at, expires_at) "
                      "VALUES (?,?,?,?,?,?,?)",
                      (task_id, self.user_id, job_id, kind, json.dumps(params or {}), now, now + ttl))
            return _task(c.execute("SELECT * FROM runner_tasks WHERE id=?", (task_id,)).fetchone()), True

    def get(self, task_id: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM runner_tasks WHERE id=? AND user_id=?", (task_id, self.user_id)).fetchone()
        return _task(r) if r else None

    def get_claimed(self, task_id: str, token_id: int) -> dict | None:
        """The task, only if it is claimed BY THIS TOKEN right now."""
        task = self.get(task_id)
        return task if task and task["status"] == "claimed" and task["claimed_by"] == token_id else None

    def open_task_for(self, job_id: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM runner_tasks WHERE user_id=? AND job_id=? AND status IN ('queued','claimed') "
                          "ORDER BY created_at LIMIT 1", (self.user_id, job_id)).fetchone()
        return _task(r) if r else None

    def has_open_task(self, job_id: str) -> bool:
        return self.open_task_for(job_id) is not None

    def list(self, status: str | None = None) -> list[dict]:
        sql, args = "SELECT * FROM runner_tasks WHERE user_id=?", [self.user_id]
        if status:
            sql, args = sql + " AND status=?", args + [status]
        with self._conn() as c:
            rows = c.execute(sql + " ORDER BY created_at, rowid", args).fetchall()
        return [_task(r) for r in rows]

    def claim(self, token_id: int, now: float, lease: float = LEASE_SECONDS) -> dict | None:
        """Hand the OLDEST queued task of this user to `token_id`, atomically
        (two runners can never get the same one). None if nothing is queued."""
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT * FROM runner_tasks WHERE user_id=? AND status='queued' "
                          "ORDER BY created_at, rowid LIMIT 1", (self.user_id,)).fetchone()
            if r is None:
                return None
            c.execute("UPDATE runner_tasks SET status='claimed', claimed_by=?, claimed_at=?, lease_until=?, "
                      "attempts=attempts+1 WHERE id=? AND user_id=? AND status='queued'",
                      (token_id, now, now + lease, r["id"], self.user_id))
            return _task(c.execute("SELECT * FROM runner_tasks WHERE id=?", (r["id"],)).fetchone())

    def renew(self, task_id: str, token_id: int, now: float, lease: float = LEASE_SECONDS) -> bool:
        with self._conn() as c:
            return c.execute("UPDATE runner_tasks SET lease_until=? WHERE id=? AND user_id=? AND status='claimed' "
                             "AND claimed_by=?", (now + lease, task_id, self.user_id, token_id)).rowcount == 1

    def complete(self, task_id: str, token_id: int, now: float, outcome: str) -> bool:
        """The runner reported a result: claimed -> done, once. False if it was not
        claimed by this token any more (re-queued, expired, cancelled, or already done)."""
        with self._conn() as c:
            return c.execute("UPDATE runner_tasks SET status='done', finished_at=?, outcome=? WHERE id=? AND user_id=? "
                             "AND status='claimed' AND claimed_by=?",
                             (now, outcome[:200], task_id, self.user_id, token_id)).rowcount == 1

    def cancel(self, task_id: str, now: float, reason: str) -> bool:
        with self._conn() as c:
            return c.execute("UPDATE runner_tasks SET status='cancelled', finished_at=?, outcome=? WHERE id=? "
                             "AND user_id=? AND status IN ('queued','claimed')",
                             (now, reason[:200], task_id, self.user_id)).rowcount == 1

    def add_events(self, task_id: str, n: int) -> int:
        """Count n more events against the task; the new total."""
        with self._conn() as c:
            c.execute("UPDATE runner_tasks SET event_count=event_count+? WHERE id=? AND user_id=?",
                      (n, task_id, self.user_id))
            r = c.execute("SELECT event_count FROM runner_tasks WHERE id=? AND user_id=?",
                          (task_id, self.user_id)).fetchone()
        return r["event_count"] if r else 0

    def add_screenshot(self, task_id: str, path: str) -> int:
        """Record a stored screenshot against the task; how many it has now."""
        with self._conn() as c:
            r = c.execute("SELECT screenshots FROM runner_tasks WHERE id=? AND user_id=?",
                          (task_id, self.user_id)).fetchone()
            shots = json.loads(r["screenshots"]) + [path]
            c.execute("UPDATE runner_tasks SET screenshots=? WHERE id=? AND user_id=?",
                      (json.dumps(shots), task_id, self.user_id))
        return len(shots)

    def sweep(self, now: float, lease: float = LEASE_SECONDS, ttl: float = QUEUE_TTL_SECONDS,
              max_attempts: int = MAX_ATTEMPTS) -> list[dict]:
        """Deal with tasks whose runner disappeared or that nobody picked up.
        Returns what it did, one {"action", "task", ...} per task, each applied
        exactly once (a conditional UPDATE decides, so two sweeps cannot both act):

          requeued     claimed, lease lapsed, not submit, attempts left: back to queued
          expired      claimed with no attempts left, or queued past its deadline
          lost_submit  a SUBMIT claimed and then silent: expired, never re-run
        """
        actions = []
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            lapsed = c.execute("SELECT * FROM runner_tasks WHERE user_id=? AND status='claimed' AND lease_until<? "
                               "ORDER BY created_at", (self.user_id, now)).fetchall()
            for r in lapsed:
                if r["kind"] == "submit":
                    c.execute("UPDATE runner_tasks SET status='expired', finished_at=?, outcome=? WHERE id=? AND status='claimed'",
                              (now, "runner went silent during submit", r["id"]))
                    action = {"action": "lost_submit"}
                elif r["attempts"] >= max_attempts:
                    c.execute("UPDATE runner_tasks SET status='expired', finished_at=?, outcome=? WHERE id=? AND status='claimed'",
                              (now, "runner went silent; no attempts left", r["id"]))
                    action = {"action": "expired", "reason": "attempts"}
                else:
                    c.execute("UPDATE runner_tasks SET status='queued', claimed_by=NULL, claimed_at=0, lease_until=0, "
                              "expires_at=? WHERE id=? AND status='claimed'", (now + ttl, r["id"]))
                    action = {"action": "requeued"}
                actions.append({**action, "task": _task(c.execute("SELECT * FROM runner_tasks WHERE id=?",
                                                                   (r["id"],)).fetchone())})
            late = c.execute("SELECT * FROM runner_tasks WHERE user_id=? AND status='queued' AND expires_at<? "
                             "ORDER BY created_at", (self.user_id, now)).fetchall()
            for r in late:
                c.execute("UPDATE runner_tasks SET status='expired', finished_at=?, outcome=? WHERE id=? AND status='queued'",
                          (now, "no runner picked it up in time", r["id"]))
                actions.append({"action": "expired", "reason": "queue_ttl",
                                "task": _task(c.execute("SELECT * FROM runner_tasks WHERE id=?", (r["id"],)).fetchone())})
        return actions
