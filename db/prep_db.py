# db/prep_db.py
"""
db/prep_db.py — interview / offer PREP for a card, and the offers you enter.

Two tables in the jobs database, every row and every query scoped by user_id:

    prep_artifacts(id, user_id, job_id, kind, status, content, error, attempt,
                   created_at, updated_at, generated_at)
      kind    technical | hiring_manager | offer
      status  pending     recorded, waiting for the background generator
              generating  the generator has claimed it
              ready       content holds the prep (JSON)
              failed      error holds the exception TYPE only - never provider text
      UNIQUE (user_id, job_id, kind)

    offers(id, user_id, job_id, company, currency, base, variable, joining_bonus,
           equity_per_year, retention_bonus, other, notes, created_at, updated_at)
      the CTC components of an offer you were made, entered by you. job_id
      optionally links it to one of YOUR cards.

THE TRIGGER - inside the same transaction as the stage change (JobsDB.transition):
entering technical_round, hiring_manager or offer records ONE pending row for that
kind (INSERT OR IGNORE on the unique key - that is what makes "once" true: a second
entry, a retry, another process, all find the row already there). Only for the
profile/LLM owner (PROFILE_OWNER_ID): LLM spend is owner-only, so other accounts get
no row and see "not generated". A failure here is swallowed inside a SAVEPOINT, so
prep can never undo the stage change it rides on.

The generating itself happens after the commit, on a background thread
(intelligence/prep.py). `attempt` counts regenerations: a generator only writes its
result if the attempt it claimed is still the current one, so a slow, superseded
run can't overwrite a newer one.

The offer comparison table is computed here, in code, from the components
(compare_offers); only the talking points come from the LLM.
"""

import json
import logging
import re
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

from core.config import cfg
from db.answer_bank_db import PROFILE_OWNER_ID

logger = logging.getLogger(__name__)

KINDS = ("technical", "hiring_manager", "offer")
STAGE_TO_KIND = {"technical_round": "technical", "hiring_manager": "hiring_manager", "offer": "offer"}
STATUSES = ("pending", "generating", "ready", "failed")
NOT_GENERATED = "not_generated"

COMPONENTS = ("base", "variable", "joining_bonus", "equity_per_year", "retention_bonus", "other")
RECURRING = ("base", "variable", "equity_per_year", "other")
ONE_TIME = ("joining_bonus", "retention_bonus")
MAX_AMOUNT = 1e12
MAX_COMPANY, MAX_NOTES = 120, 1000
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")

PREP_DDL = (
    """CREATE TABLE IF NOT EXISTS prep_artifacts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      INTEGER NOT NULL,
        job_id       TEXT NOT NULL,
        kind         TEXT NOT NULL CHECK (kind IN ('technical','hiring_manager','offer')),
        status       TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','generating','ready','failed')),
        content      TEXT NOT NULL DEFAULT '',
        error        TEXT NOT NULL DEFAULT '',
        attempt      INTEGER NOT NULL DEFAULT 1,
        created_at   TEXT NOT NULL,
        updated_at   TEXT NOT NULL,
        generated_at TEXT NOT NULL DEFAULT '',
        UNIQUE (user_id, job_id, kind)
    )""",
    """CREATE TABLE IF NOT EXISTS offers (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id         INTEGER NOT NULL,
        job_id          TEXT NOT NULL DEFAULT '',
        company         TEXT NOT NULL,
        currency        TEXT NOT NULL DEFAULT 'INR',
        base            REAL NOT NULL DEFAULT 0,
        variable        REAL NOT NULL DEFAULT 0,
        joining_bonus   REAL NOT NULL DEFAULT 0,
        equity_per_year REAL NOT NULL DEFAULT 0,
        retention_bonus REAL NOT NULL DEFAULT 0,
        other           REAL NOT NULL DEFAULT 0,
        notes           TEXT NOT NULL DEFAULT '',
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_prep_user_job ON prep_artifacts(user_id, job_id)",
    "CREATE INDEX IF NOT EXISTS idx_offers_user ON offers(user_id, id)",
)


def _iso(when: datetime) -> str:
    return when.isoformat(timespec="seconds")


# ── schema and the trigger (called by JobsDB, on ITS connection) ──

def ensure_schema(c) -> None:
    for ddl in PREP_DDL:
        c.execute(ddl)


def on_transition(c, *, user_id, job_id: str, to: str, now: datetime) -> bool:
    """A job just moved to `to`. Records the pending prep row its kind needs (once per
    user + job + kind). True if a new row was recorded. Never raises: whatever goes wrong
    is rolled back to a savepoint and logged, and the stage change commits regardless."""
    kind = STAGE_TO_KIND.get(to)
    if kind is None or user_id != PROFILE_OWNER_ID:
        return False
    try:
        c.execute("SAVEPOINT prep_trigger")
        try:
            n = c.execute("""INSERT OR IGNORE INTO prep_artifacts
                                 (user_id, job_id, kind, status, created_at, updated_at)
                             VALUES (?, ?, ?, 'pending', ?, ?)""",
                          (user_id, job_id, kind, _iso(now), _iso(now))).rowcount
            c.execute("RELEASE SAVEPOINT prep_trigger")
            return n == 1
        except Exception:
            c.execute("ROLLBACK TO SAVEPOINT prep_trigger")
            c.execute("RELEASE SAVEPOINT prep_trigger")
            raise
    except Exception as e:
        logger.warning("[Prep] could not record pending prep for %s: %s", job_id[:8], type(e).__name__)
        return False


# ── offers: validation and the comparison (pure) ──────────────────

def clean_offer(data, *, partial: bool = False) -> dict:
    """Validate an offer (or, with partial=True, some of its fields). ValueError says what
    is wrong. Amounts are annual numbers >= 0 in the offer's currency."""
    if not isinstance(data, dict):
        raise ValueError("Expected an object")
    allowed = {"job_id", "company", "currency", "notes", *COMPONENTS}
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ValueError(f"Unknown field(s): {', '.join(map(str, unknown))}")
    if partial and not data:
        raise ValueError("Nothing to change")
    out = {}
    if "company" in data or not partial:
        company = data.get("company")
        if not isinstance(company, str) or not " ".join(_CONTROL.sub(" ", company).split()):
            raise ValueError("company is required")
        company = " ".join(_CONTROL.sub(" ", company).split())
        if len(company) > MAX_COMPANY:
            raise ValueError(f"company is too long ({MAX_COMPANY} characters at most)")
        out["company"] = company
    if "currency" in data or not partial:
        cur = data.get("currency", "INR")
        cur = cur.strip().upper() if isinstance(cur, str) else cur
        if not isinstance(cur, str) or not _CURRENCY.match(cur):
            raise ValueError("currency must be a three-letter code such as INR or USD")
        out["currency"] = cur
    for key in COMPONENTS:
        if key not in data and partial:
            continue
        value = data.get(key, 0)
        if value is None or value == "":
            value = 0
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number")
        if value != value or not 0 <= value <= MAX_AMOUNT:              # NaN or out of range
            raise ValueError(f"{key} must be 0 or more")
        out[key] = float(value)
    if "notes" in data or not partial:
        notes = data.get("notes") or ""
        if not isinstance(notes, str):
            raise ValueError("notes must be text")
        notes = notes.strip()
        if len(notes) > MAX_NOTES:
            raise ValueError(f"notes are too long ({MAX_NOTES} characters at most)")
        out["notes"] = notes
    if "job_id" in data or not partial:
        job_id = data.get("job_id") or ""
        if not isinstance(job_id, str):
            raise ValueError("job_id must be text")
        out["job_id"] = job_id.strip()
    return out


def _money(x: float):
    return int(x) if float(x).is_integer() else round(x, 2)


def compare_offers(offers: list[dict]) -> dict:
    """The comparison table, in code. Per offer: each component, recurring (base +
    variable + equity per year + other), one_time (joining + retention bonus),
    first_year_total (both) and fixed_share_pct (base / recurring). Rows are sorted by
    first_year_total, best first; the deltas to the best are given only when every offer
    is in the same currency (mixed currencies can't be compared without a rate, and the
    app has none)."""
    rows = []
    for o in offers:
        recurring = sum(o[k] for k in RECURRING)
        one_time = sum(o[k] for k in ONE_TIME)
        rows.append({"offer_id": o["id"], "company": o["company"], "job_id": o["job_id"],
                     "currency": o["currency"], **{k: _money(o[k]) for k in COMPONENTS},
                     "recurring": _money(recurring), "one_time": _money(one_time),
                     "first_year_total": _money(recurring + one_time),
                     "fixed_share_pct": round(100 * o["base"] / recurring, 1) if recurring else None})
    rows.sort(key=lambda r: (-r["first_year_total"], r["offer_id"]))
    currencies = sorted({r["currency"] for r in rows})
    same = len(currencies) <= 1
    if same and rows:
        best_total, best_recurring = rows[0]["first_year_total"], max(r["recurring"] for r in rows)
        for r in rows:
            r["first_year_vs_best"] = _money(r["first_year_total"] - best_total)
            r["recurring_vs_best"] = _money(r["recurring"] - best_recurring)
    return {"rows": rows, "currencies": currencies, "comparable": same,
            "best_offer_id": rows[0]["offer_id"] if rows and same else None,
            "note": "" if same else "These offers are in different currencies, so they are not ranked against each other."}


# ── the stores ────────────────────────────────────────────────────

class _Scoped:
    def __init__(self, user_id: int, path: Path | None = None, clock=None):
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            raise ValueError("A prep store needs a user_id; there is no unscoped view")
        self.user_id = user_id
        self.path = Path(path or cfg.JOBS_DB_PATH)
        self._clock = clock or datetime.now
        from db.jobs_db import JobsDB                      # creates every table; jobs_db imports us
        JobsDB(path=self.path, user_id=user_id, clock=self._clock)

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

    def _owns_job(self, c, job_id: str) -> bool:
        return c.execute("SELECT 1 FROM jobs WHERE job_id=? AND user_id=?",
                         (job_id, self.user_id)).fetchone() is not None


def _artifact(r) -> dict:
    try:
        content = json.loads(r["content"]) if r["content"] else None
    except ValueError:
        content = None
    return {"kind": r["kind"], "status": r["status"], "content": content, "error": r["error"],
            "attempt": r["attempt"], "created_at": r["created_at"], "updated_at": r["updated_at"],
            "generated_at": r["generated_at"]}


def not_generated(kind: str) -> dict:
    return {"kind": kind, "status": NOT_GENERATED, "content": None, "error": "", "attempt": 0,
            "created_at": "", "updated_at": "", "generated_at": ""}


class PrepStore(_Scoped):
    """One user's prep rows. PrepStore(user_id) can only ever see that user's rows."""

    def get(self, job_id: str, kind: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM prep_artifacts WHERE user_id=? AND job_id=? AND kind=?",
                          (self.user_id, job_id, kind)).fetchone()
        return _artifact(r) if r else None

    def for_job(self, job_id: str) -> dict:
        """{kind: artifact} for all three kinds; a kind with no row is "not_generated"."""
        with self._conn() as c:
            rows = c.execute("SELECT * FROM prep_artifacts WHERE user_id=? AND job_id=?",
                             (self.user_id, job_id)).fetchall()
        found = {r["kind"]: _artifact(r) for r in rows}
        return {k: found.get(k) or not_generated(k) for k in KINDS}

    def request(self, job_id: str, kind: str) -> dict:
        """Ask for (re)generation: a missing row is created pending, an existing one goes back
        to pending with the next attempt number (its old content stays until the new one is
        ready). LookupError if the job is not this user's; ValueError for an unknown kind."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {list(KINDS)}")
        now = _iso(self._clock())
        with self._conn() as c:
            if not self._owns_job(c, job_id):
                raise LookupError("Job not found")
            c.execute("""INSERT INTO prep_artifacts (user_id, job_id, kind, status, created_at, updated_at)
                         VALUES (?, ?, ?, 'pending', ?, ?)
                         ON CONFLICT(user_id, job_id, kind) DO UPDATE SET
                             status='pending', error='', attempt=attempt + 1, updated_at=excluded.updated_at""",
                      (self.user_id, job_id, kind, now, now))
        return self.get(job_id, kind)

    def claim(self, job_id: str, kind: str) -> int | None:
        """pending -> generating. Returns the attempt claimed, or None if it was not pending
        (already claimed by someone else, or not there)."""
        now = _iso(self._clock())
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            r = c.execute("SELECT attempt FROM prep_artifacts WHERE user_id=? AND job_id=? AND kind=? "
                          "AND status='pending'", (self.user_id, job_id, kind)).fetchone()
            if r is None:
                return None
            c.execute("UPDATE prep_artifacts SET status='generating', updated_at=? WHERE user_id=? AND job_id=? "
                      "AND kind=? AND attempt=?", (now, self.user_id, job_id, kind, r["attempt"]))
            return r["attempt"]

    def _finish(self, job_id: str, kind: str, attempt: int, sets: str, params: tuple) -> bool:
        now = _iso(self._clock())
        with self._conn() as c:
            n = c.execute(f"UPDATE prep_artifacts SET {sets}, updated_at=? WHERE user_id=? AND job_id=? "
                          "AND kind=? AND attempt=? AND status='generating'",
                          params + (now, self.user_id, job_id, kind, attempt)).rowcount
        return n == 1

    def save_ready(self, job_id: str, kind: str, attempt: int, content: dict) -> bool:
        """Store the result of `attempt`; False (nothing written) if it was superseded."""
        body = json.dumps(content, ensure_ascii=False)
        return self._finish(job_id, kind, attempt, "status='ready', content=?, error='', generated_at=?",
                            (body, _iso(self._clock())))

    def save_failed(self, job_id: str, kind: str, attempt: int, error_type: str) -> bool:
        """Mark `attempt` failed. `error_type` is an exception class name and nothing else."""
        clean = re.sub(r"[^A-Za-z0-9_.]", "", str(error_type or ""))[:80] or "Error"
        return self._finish(job_id, kind, attempt, "status='failed', error=?", (clean,))

    def waiting(self) -> list[tuple[str, str]]:
        """(job_id, kind) of every row left pending or generating - after a restart the
        generating ones were interrupted, so they are put back to pending first."""
        now = _iso(self._clock())
        with self._conn() as c:
            c.execute("UPDATE prep_artifacts SET status='pending', updated_at=? WHERE user_id=? "
                      "AND status='generating'", (now, self.user_id))
            rows = c.execute("SELECT job_id, kind FROM prep_artifacts WHERE user_id=? AND status='pending' "
                             "ORDER BY id", (self.user_id,)).fetchall()
        return [(r["job_id"], r["kind"]) for r in rows]


def _offer(r) -> dict:
    d = {k: r[k] for k in ("id", "job_id", "company", "currency", "notes", "created_at", "updated_at")}
    d.update({k: float(r[k]) for k in COMPONENTS})
    return d


class OfferStore(_Scoped):
    """One user's offers. Every query is scoped by user_id; another user's offer is not found."""

    def list(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM offers WHERE user_id=? ORDER BY id", (self.user_id,)).fetchall()
        return [_offer(r) for r in rows]

    def get(self, offer_id: int) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM offers WHERE id=? AND user_id=?", (offer_id, self.user_id)).fetchone()
        return _offer(r) if r else None

    def _check_job(self, c, job_id: str) -> None:
        if job_id and not self._owns_job(c, job_id):
            raise ValueError("job_id is not one of your applications")

    def create(self, data) -> dict:
        clean = clean_offer(data)
        now = _iso(self._clock())
        with self._conn() as c:
            self._check_job(c, clean["job_id"])
            cols = ("job_id", "company", "currency", *COMPONENTS, "notes")
            cur = c.execute(f"INSERT INTO offers (user_id, {', '.join(cols)}, created_at, updated_at) "
                            f"VALUES (?, {', '.join('?' * len(cols))}, ?, ?)",
                            (self.user_id, *(clean[k] for k in cols), now, now))
            oid = cur.lastrowid
        return self.get(oid)

    def update(self, offer_id: int, data) -> dict | None:
        """Change some fields. None if it is not this user's offer; ValueError if invalid."""
        clean = clean_offer(data, partial=True)
        now = _iso(self._clock())
        with self._conn() as c:
            if c.execute("SELECT 1 FROM offers WHERE id=? AND user_id=?", (offer_id, self.user_id)).fetchone() is None:
                return None
            if "job_id" in clean:
                self._check_job(c, clean["job_id"])
            # column names come from clean_offer's fixed allow-list, never from the caller
            sets = ", ".join(f"{k}=?" for k in clean)
            c.execute(f"UPDATE offers SET {sets}, updated_at=? WHERE id=? AND user_id=?",
                      (*clean.values(), now, offer_id, self.user_id))
        return self.get(offer_id)

    def delete(self, offer_id: int) -> bool:
        with self._conn() as c:
            return c.execute("DELETE FROM offers WHERE id=? AND user_id=?",
                             (offer_id, self.user_id)).rowcount == 1
