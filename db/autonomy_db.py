# db/autonomy_db.py
"""
Autonomy: what the agent may approve BY ITSELF, and the measured record that
decides it. Four small tables in the jobs database, every one of them per user
and every query scoped by user_id (as with the review packets and the answer
bank there is deliberately no unscoped view):

  autonomy_settings   one row per user: the kill switch (autonomy_enabled).
                      No row means OFF - autonomy is opt-in.
  autonomy_rules      what may be auto-approved (see RULE_FIELDS). None by
                      default, so with no rules everything is reviewed.
  autonomy_attempts   the ledger: one row each time an application of this
                      site type reached ready_for_review, and how it ended.
  autonomy_failures   the ledger: every classified failure (applier/failures.py)
                      on a site type.

The ledger is history, not a counter: it is kept when a job is deleted, because
"how often was this site type right" must not be erasable by tidying the list.

Outcomes of an attempt (autonomy_attempts.outcome):
    pending             reached review, nobody has decided yet
    approved_unchanged  a person approved it exactly as the agent filled it
    approved_edited     a person approved it after changing something
    rejected            a person rejected it in review
    auto_approved       an autonomy rule approved it (no person looked)
    superseded          it reached review again later; this earlier attempt
                        was never decided

SUCCESS RATE = approved_unchanged / the last WINDOW (20) attempts that a PERSON
decided (approved_unchanged, approved_edited, rejected). Auto-approvals are
left out on purpose: counting the agent's own unreviewed approvals as evidence
that it is reliable would let autonomy vouch for itself. A rejection counts
against the rate - leaving it out would flatter it.
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from applier.site_type import SITE_TYPES
from core.config import cfg

WINDOW = 20                          # the success rate is over this many reviewed attempts
ANY_SITE = "*"

DEFAULT_MIN_ATTEMPTS     = 20
DEFAULT_MIN_SUCCESS_RATE = 0.9

OUTCOMES = ("pending", "approved_unchanged", "approved_edited", "rejected",
            "auto_approved", "superseded")
HUMAN_OUTCOMES = ("approved_unchanged", "approved_edited", "rejected")   # a person decided

AUTONOMY_DDL = (
    """CREATE TABLE IF NOT EXISTS autonomy_settings (
        user_id          INTEGER PRIMARY KEY,
        autonomy_enabled INTEGER NOT NULL DEFAULT 0,
        updated_at       TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS autonomy_rules (
        id                        INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id                   INTEGER NOT NULL,
        site_type                 TEXT NOT NULL,
        min_score                 INTEGER NOT NULL,
        require_all_answers_known INTEGER NOT NULL DEFAULT 1,
        min_attempts              INTEGER NOT NULL DEFAULT 20,
        min_success_rate          REAL NOT NULL DEFAULT 0.9,
        max_per_day               INTEGER NOT NULL,
        enabled                   INTEGER NOT NULL DEFAULT 1,
        disabled_reason           TEXT NOT NULL DEFAULT '',
        disabled_at               TEXT NOT NULL DEFAULT '',
        created_at                TEXT NOT NULL,
        updated_at                TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_autonomy_rules_user ON autonomy_rules(user_id, site_type)",
    """CREATE TABLE IF NOT EXISTS autonomy_attempts (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id    INTEGER NOT NULL,
        job_id     TEXT NOT NULL,
        site_type  TEXT NOT NULL,
        reached_at TEXT NOT NULL,
        outcome    TEXT NOT NULL DEFAULT 'pending',
        closed_at  TEXT NOT NULL DEFAULT '',
        rule_id    INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_autonomy_attempts_user ON autonomy_attempts(user_id, site_type, id)",
    "CREATE INDEX IF NOT EXISTS idx_autonomy_attempts_job  ON autonomy_attempts(user_id, job_id, id)",
    """CREATE TABLE IF NOT EXISTS autonomy_failures (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id       INTEGER NOT NULL,
        job_id        TEXT NOT NULL,
        site_type     TEXT NOT NULL,
        failure_class TEXT NOT NULL,
        at            TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_autonomy_failures_user ON autonomy_failures(user_id, site_type, id)",
)

# Rule fields a caller may set. REQUIRED ones have no default: a rule that
# approves things for you should say how good a job must score and how many a
# day it may take, rather than inherit a number someone else picked.
RULE_FIELDS = ("site_type", "min_score", "require_all_answers_known", "min_attempts",
               "min_success_rate", "max_per_day")
REQUIRED_RULE_FIELDS = ("site_type", "min_score", "max_per_day")
UPDATABLE_RULE_FIELDS = RULE_FIELDS + ("enabled",)


def _int(value, name: str, lo: int, hi: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be a whole number")
    if not lo <= value <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return value


def _bool(value, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be true or false")
    return value


def clean_rule(data: dict, partial: bool = False) -> dict:
    """Validate rule fields and fill the defaults (min_attempts 20,
    min_success_rate 0.9, require_all_answers_known true). With partial=True
    (an update) only the fields given are checked and returned. ValueError,
    in words, for anything wrong or unknown."""
    if not isinstance(data, dict):
        raise ValueError("a rule must be an object")
    unknown = set(data) - set(UPDATABLE_RULE_FIELDS)
    if unknown:
        raise ValueError("unknown field(s): " + ", ".join(sorted(map(str, unknown))))
    if not partial:
        missing = [f for f in REQUIRED_RULE_FIELDS if f not in data]
        if missing:
            raise ValueError("required: " + ", ".join(missing))
    out = {}
    if "site_type" in data:
        st = data["site_type"]
        if not isinstance(st, str) or st not in SITE_TYPES + (ANY_SITE,):
            raise ValueError("site_type must be one of: " + ", ".join(SITE_TYPES + (ANY_SITE,)))
        out["site_type"] = st
    if "min_score" in data:
        out["min_score"] = _int(data["min_score"], "min_score", 0, 100)
    if "max_per_day" in data:
        out["max_per_day"] = _int(data["max_per_day"], "max_per_day", 1, 1000)
    if "min_attempts" in data:
        out["min_attempts"] = _int(data["min_attempts"], "min_attempts", 1, 100000)
    elif not partial:
        out["min_attempts"] = DEFAULT_MIN_ATTEMPTS
    if "min_success_rate" in data:
        v = data["min_success_rate"]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 < v <= 1:
            raise ValueError("min_success_rate must be a number above 0 and at most 1")
        out["min_success_rate"] = float(v)
    elif not partial:
        out["min_success_rate"] = DEFAULT_MIN_SUCCESS_RATE
    if "require_all_answers_known" in data:
        out["require_all_answers_known"] = _bool(data["require_all_answers_known"],
                                                 "require_all_answers_known")
    elif not partial:
        out["require_all_answers_known"] = True
    if "enabled" in data:
        out["enabled"] = _bool(data["enabled"], "enabled")
    return out


def _rule(r) -> dict:
    """A rule row as the API shape (user_id is internal)."""
    return {"id": r["id"], "site_type": r["site_type"], "min_score": r["min_score"],
            "require_all_answers_known": bool(r["require_all_answers_known"]),
            "min_attempts": r["min_attempts"], "min_success_rate": r["min_success_rate"],
            "max_per_day": r["max_per_day"], "enabled": bool(r["enabled"]),
            "disabled_reason": r["disabled_reason"], "disabled_at": r["disabled_at"],
            "created_at": r["created_at"], "updated_at": r["updated_at"]}


def _day_start(now: datetime) -> str:
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


class AutonomyStore:
    def __init__(self, user_id: int, path: Path | None = None):
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            raise ValueError("AutonomyStore needs a user_id; there is no unscoped view")
        self.user_id = user_id
        self.path = path or cfg.JOBS_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            for ddl in AUTONOMY_DDL:
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

    # ── the kill switch ───────────────────────────────────────────

    def is_enabled(self) -> bool:
        """OFF unless this user turned it on."""
        with self._conn() as c:
            r = c.execute("SELECT autonomy_enabled FROM autonomy_settings WHERE user_id=?",
                          (self.user_id,)).fetchone()
        return bool(r and r["autonomy_enabled"])

    def set_enabled(self, enabled: bool) -> None:
        _bool(enabled, "autonomy_enabled")
        with self._conn() as c:
            c.execute("""INSERT INTO autonomy_settings (user_id, autonomy_enabled, updated_at)
                         VALUES (?,?,?)
                         ON CONFLICT(user_id) DO UPDATE SET autonomy_enabled=excluded.autonomy_enabled,
                                                            updated_at=excluded.updated_at""",
                      (self.user_id, int(enabled), datetime.now().isoformat()))

    # ── rules ─────────────────────────────────────────────────────

    def list_rules(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM autonomy_rules WHERE user_id=? ORDER BY id",
                             (self.user_id,)).fetchall()
        return [_rule(r) for r in rows]

    def get_rule(self, rule_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM autonomy_rules WHERE id=? AND user_id=?",
                          (rule_id, self.user_id)).fetchone()
        return _rule(r) if r else None

    def add_rule(self, data: dict) -> dict:
        """Create a rule (enabled). ValueError if `data` is not a valid rule."""
        f = clean_rule(data)
        now = datetime.now().isoformat()
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO autonomy_rules (user_id, site_type, min_score, require_all_answers_known,
                       min_attempts, min_success_rate, max_per_day, enabled, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,1,?,?)""",
                (self.user_id, f["site_type"], f["min_score"], int(f["require_all_answers_known"]),
                 f["min_attempts"], f["min_success_rate"], f["max_per_day"], now, now))
            rule_id = cur.lastrowid
        return self.get_rule(rule_id)

    def update_rule(self, rule_id: int, data: dict) -> dict | None:
        """Change some fields of one of this user's rules; None if it isn't
        theirs. enabled=True is how an auto-disabled rule is switched back on
        (its disabled_reason is cleared); enabled=False is a manual disable."""
        f = clean_rule(data, partial=True)
        if not f:
            raise ValueError("nothing to change")
        sets, params = [], []
        for name, value in f.items():
            sets.append(f"{name}=?")
            params.append(int(value) if isinstance(value, bool) else value)
        if "enabled" in f:
            sets += ["disabled_reason=''", "disabled_at=''"]
        now = datetime.now().isoformat()
        with self._conn() as c:
            n = c.execute(f"UPDATE autonomy_rules SET {', '.join(sets)}, updated_at=? "
                          "WHERE id=? AND user_id=?",
                          (*params, now, rule_id, self.user_id)).rowcount
        return self.get_rule(rule_id) if n else None

    def delete_rule(self, rule_id: int) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM autonomy_rules WHERE id=? AND user_id=?",
                             (rule_id, self.user_id)).rowcount == 1

    def disable_rules_for(self, site_type: str, reason: str) -> list[int]:
        """Switch off every ENABLED rule that applies to `site_type` - its own
        and the '*' ones, since those approved applications on it too - and
        record why. They stay off until the person re-enables them. Returns
        the ids it disabled."""
        now = datetime.now().isoformat()
        with self._conn() as c:
            ids = [r["id"] for r in c.execute(
                "SELECT id FROM autonomy_rules WHERE user_id=? AND enabled=1 "
                "AND site_type IN (?,?) ORDER BY id", (self.user_id, site_type, ANY_SITE))]
            for rule_id in ids:
                c.execute("UPDATE autonomy_rules SET enabled=0, disabled_reason=?, disabled_at=?, "
                          "updated_at=? WHERE id=? AND user_id=?",
                          (reason[:300], now, now, rule_id, self.user_id))
        return ids

    # ── the ledger: writing ───────────────────────────────────────

    def record_review_reached(self, job_id: str, site_type: str, now: datetime | None = None) -> int:
        """An application of `site_type` reached ready_for_review. Any earlier
        attempt of the same job that was never decided is marked superseded
        (a job sent back and re-run is a new attempt, not two open ones)."""
        if site_type not in SITE_TYPES:
            raise ValueError(f"unknown site_type {site_type!r}")
        at = (now or datetime.now()).isoformat()
        with self._conn() as c:
            c.execute("UPDATE autonomy_attempts SET outcome='superseded', closed_at=? "
                      "WHERE user_id=? AND job_id=? AND outcome='pending'",
                      (at, self.user_id, job_id))
            return c.execute("INSERT INTO autonomy_attempts (user_id, job_id, site_type, reached_at) "
                             "VALUES (?,?,?,?)", (self.user_id, job_id, site_type, at)).lastrowid

    def pending_attempt(self, job_id: str) -> dict | None:
        """The undecided attempt for this job, if there is one."""
        with self._conn() as c:
            r = c.execute("SELECT id, site_type, reached_at FROM autonomy_attempts "
                          "WHERE user_id=? AND job_id=? AND outcome='pending' ORDER BY id DESC LIMIT 1",
                          (self.user_id, job_id)).fetchone()
        return dict(r) if r else None

    def close_attempt(self, job_id: str, outcome: str, rule_id: int | None = None,
                      now: datetime | None = None) -> bool:
        """Decide the job's pending attempt. False if there is none (so a
        decision on something that never reached review is not recorded)."""
        if outcome not in OUTCOMES or outcome in ("pending", "superseded"):
            raise ValueError(f"cannot close an attempt as {outcome!r}")
        at = (now or datetime.now()).isoformat()
        with self._conn() as c:
            return c.execute(
                "UPDATE autonomy_attempts SET outcome=?, closed_at=?, rule_id=? WHERE id=("
                "SELECT id FROM autonomy_attempts WHERE user_id=? AND job_id=? AND outcome='pending' "
                "ORDER BY id DESC LIMIT 1) AND user_id=?",
                (outcome, at, rule_id, self.user_id, job_id, self.user_id)).rowcount == 1

    def record_failure(self, job_id: str, site_type: str, failure_class: str,
                       now: datetime | None = None) -> None:
        if site_type not in SITE_TYPES:
            raise ValueError(f"unknown site_type {site_type!r}")
        with self._conn() as c:
            c.execute("INSERT INTO autonomy_failures (user_id, job_id, site_type, failure_class, at) "
                      "VALUES (?,?,?,?,?)",
                      (self.user_id, job_id, site_type, failure_class,
                       (now or datetime.now()).isoformat()))

    # ── the ledger: reading ───────────────────────────────────────

    def site_stats(self, site_type: str) -> dict:
        """What the evidence says about one site type:
            attempts, pending, approved_unchanged, approved_edited, rejected,
            auto_approved, superseded   counts over ALL time
            reviewed                    approved_unchanged + approved_edited + rejected
            window                      how many of the last WINDOW reviewed attempts there are
            window_unchanged            how many of those were approved unchanged
            success_rate                window_unchanged / window, or None with no reviewed attempt
            failures                    {class: count}, failures_total"""
        marks = ",".join("?" * len(HUMAN_OUTCOMES))
        with self._conn() as c:
            counts = {r["outcome"]: r["n"] for r in c.execute(
                "SELECT outcome, COUNT(*) n FROM autonomy_attempts WHERE user_id=? AND site_type=? "
                "GROUP BY outcome", (self.user_id, site_type))}
            last = [r["outcome"] for r in c.execute(
                f"SELECT outcome FROM autonomy_attempts WHERE user_id=? AND site_type=? "
                f"AND outcome IN ({marks}) ORDER BY closed_at DESC, id DESC LIMIT ?",
                (self.user_id, site_type, *HUMAN_OUTCOMES, WINDOW))]
            failures = {r["failure_class"]: r["n"] for r in c.execute(
                "SELECT failure_class, COUNT(*) n FROM autonomy_failures WHERE user_id=? AND site_type=? "
                "GROUP BY failure_class ORDER BY failure_class", (self.user_id, site_type))}
        unchanged = sum(1 for o in last if o == "approved_unchanged")
        out = {o: counts.get(o, 0) for o in OUTCOMES}
        out["attempts"] = sum(counts.values())
        out["reviewed"] = sum(counts.get(o, 0) for o in HUMAN_OUTCOMES)
        out["window"] = len(last)
        out["window_unchanged"] = unchanged
        out["success_rate"] = round(unchanged / len(last), 4) if last else None
        out["failures"] = failures
        out["failures_total"] = sum(failures.values())
        return out

    def ledger(self) -> dict:
        """site_stats for every site type (zeros for ones never seen), so the
        shape never depends on what has happened yet."""
        return {st: self.site_stats(st) for st in SITE_TYPES}

    def auto_approved_today(self, rule_id: int, now: datetime | None = None) -> int:
        """How many applications this rule has auto-approved since midnight."""
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) n FROM autonomy_attempts WHERE user_id=? AND rule_id=? "
                             "AND outcome='auto_approved' AND closed_at>=?",
                             (self.user_id, rule_id, _day_start(now or datetime.now()))).fetchone()["n"]
