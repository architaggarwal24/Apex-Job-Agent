"""
db/jobs_db.py — SQLite job tracker with full pipeline tracking.

Tables:
  jobs        — Every scraped job with dedup, scoring, status
  job_stages  — Application pipeline stages (Applied → Offer)
  job_notes   — Per-job notes from the user
  job_briefs  — LLM-generated job brief + tailored resume summary

Dedup: normalised URL hash + title+company fuzzy match.
"""

import hashlib
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, date, timedelta
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from core.config import cfg

logger = logging.getLogger(__name__)

VALID_STATUSES = {
    "found", "applied", "skipped", "error", "in_progress",
    "captcha_blocked", "manual",
}

PIPELINE_STAGES = [
    "applied", "recruiter_screen", "technical_round",
    "hiring_manager", "offer", "rejected", "withdrawn",
]


class JobsDB:
    def __init__(self, path: Path | None = None):
        self.path = path or cfg.JOBS_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

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
            ]:
                try:
                    c.execute(f"ALTER TABLE jobs ADD COLUMN {col[0]} {col[1]}")
                except Exception:
                    pass

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

    @staticmethod
    def _job_id(url: str) -> str:
        return hashlib.sha256(JobsDB._norm_url(url).encode()).hexdigest()[:16]

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

        with self._conn() as c:
            if c.execute("SELECT 1 FROM jobs WHERE url_hash=?",
                         (url_hash,)).fetchone():
                return None
            if c.execute("SELECT 1 FROM jobs WHERE title_co_key=? AND source=?",
                         (tc_key, job.get("source",""))).fetchone():
                return None
            try:
                c.execute("""
                    INSERT INTO jobs
                        (job_id, url_hash, title_co_key, title, company, location,
                         url, source, description, salary, seniority, applicants,
                         status, found_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'found',?,?)
                """, (job_id, url_hash, tc_key,
                      job["title"], job["company"], job.get("location",""),
                      url, job.get("source",""), job.get("description",""),
                      job.get("salary",""), job.get("seniority",""),
                      job.get("applicants",""), now, now))
                return job_id
            except sqlite3.IntegrityError:
                return None

    def update_status(self, job_id: str, status: str, error: str = ""):
        now = datetime.now().isoformat()
        applied = now if status == "applied" else ""
        with self._conn() as c:
            c.execute("""
                UPDATE jobs SET status=?, error_message=?, updated_at=?,
                    applied_at=CASE WHEN ?!='' THEN ? ELSE applied_at END
                WHERE job_id=?
            """, (status, error, now, applied, applied, job_id))

    def update_score(self, job_id: str, score: int, reason: str):
        with self._conn() as c:
            c.execute("""
                UPDATE jobs SET ai_score=?, ai_score_reason=?, updated_at=?
                WHERE job_id=?
            """, (score, reason, datetime.now().isoformat(), job_id))

    def save_brief(self, job_id: str, brief: str,
                   tailored_summary: str = "", tailored_skills: str = ""):
        now = datetime.now().isoformat()
        with self._conn() as c:
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

    def add_stage(self, job_id: str, stage: str, note: str = ""):
        if stage not in PIPELINE_STAGES:
            raise ValueError(f"Invalid stage: {stage}")
        now = datetime.now().isoformat()
        with self._conn() as c:
            c.execute("""
                INSERT INTO job_stages (job_id, stage, note, changed_at)
                VALUES (?, ?, ?, ?)
            """, (job_id, stage, note, now))
        self.update_status(job_id, "applied" if stage == "applied" else "in_progress")

    def add_note(self, job_id: str, note: str):
        with self._conn() as c:
            c.execute("""
                INSERT INTO job_notes (job_id, note, created_at) VALUES (?, ?, ?)
            """, (job_id, note, datetime.now().isoformat()))

    def purge_duplicates(self) -> int:
        with self._conn() as c:
            r = c.execute("""
                DELETE FROM jobs WHERE id NOT IN (
                    SELECT MAX(id) FROM jobs GROUP BY title_co_key, source
                )""")
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
        with self._conn() as c:
            if include_applied:
                r = c.execute("DELETE FROM jobs")
                c.execute("DELETE FROM job_stages")
                c.execute("DELETE FROM job_notes")
                c.execute("DELETE FROM job_briefs")
            else:
                r = c.execute(
                    "DELETE FROM jobs WHERE status NOT IN ('applied','in_progress')"
                )
            return r.rowcount

    def count_applied_today(self) -> int:
        """Number of jobs marked 'applied' today (local server date).
        Used to enforce DAILY_APPLY_LIMIT / daily_apply_limit setting."""
        today = date.today().isoformat()
        with self._conn() as c:
            r = c.execute(
                "SELECT COUNT(*) n FROM jobs WHERE applied_at LIKE ?",
                (f"{today}%",)
            ).fetchone()
            return r["n"]

    def get_last_applied_at(self) -> str:
        """ISO timestamp of the most recent successful application,
        or '' if none yet. Used to enforce min_delay_between_applies."""
        with self._conn() as c:
            r = c.execute(
                "SELECT MAX(applied_at) v FROM jobs WHERE applied_at != ''"
            ).fetchone()
            return r["v"] or ""

    # ── Read ──────────────────────────────────────────────────────

    def exists_bulk(self, urls: list[str]) -> set[str]:
        if not urls:
            return set()
        hashes = [self._url_hash(u) for u in urls]
        ph = ",".join("?" * len(hashes))
        with self._conn() as c:
            rows = c.execute(
                f"SELECT url FROM jobs WHERE url_hash IN ({ph})", hashes
            ).fetchall()
        return {r["url"] for r in rows}

    def get_by_id(self, job_id: str) -> dict | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if not r:
                return None
            d = dict(r)
            brief = c.execute("SELECT * FROM job_briefs WHERE job_id=?",
                               (job_id,)).fetchone()
            d["brief"]           = brief["brief"]           if brief else ""
            d["tailored_summary"] = brief["tailored_summary"] if brief else ""
            d["tailored_skills"]  = brief["tailored_skills"]  if brief else ""
            d["stages"] = [dict(s) for s in c.execute(
                "SELECT * FROM job_stages WHERE job_id=? ORDER BY changed_at",
                (job_id,)).fetchall()]
            d["notes"] = [dict(n) for n in c.execute(
                "SELECT * FROM job_notes WHERE job_id=? ORDER BY created_at DESC",
                (job_id,)).fetchall()]
            return d

    def get_all(self, limit: int = 1000, status: str = "") -> list[dict]:
        with self._conn() as c:
            if status:
                rows = c.execute("""
                    SELECT * FROM jobs WHERE status=?
                    ORDER BY ai_score DESC NULLS LAST, found_at DESC LIMIT ?
                """, (status, limit)).fetchall()
            else:
                rows = c.execute("""
                    SELECT * FROM jobs
                    ORDER BY ai_score DESC NULLS LAST, found_at DESC LIMIT ?
                """, (limit,)).fetchall()
            return [dict(r) for r in rows]

    def get_pending(self, limit: int = 50) -> list[dict]:
        """Jobs ready to apply — scored above threshold, not yet applied."""
        try:
            from db.settings_db import SettingsDB
            threshold = SettingsDB.get().auto_skip_score_threshold()
        except Exception:
            threshold = cfg.AUTO_SKIP_SCORE_THRESHOLD
        with self._conn() as c:
            rows = c.execute("""
                SELECT * FROM jobs
                WHERE status='found'
                AND (ai_score IS NULL OR ai_score >= ?)
                ORDER BY ai_score DESC NULLS LAST, found_at DESC
                LIMIT ?
            """, (threshold, limit)).fetchall()
            return [dict(r) for r in rows]

    def get_pipeline(self) -> dict[str, list[dict]]:
        """Return jobs grouped by their latest pipeline stage."""
        with self._conn() as c:
            # Get latest stage per job
            stage_rows = c.execute("""
                SELECT js.job_id, js.stage, j.title, j.company, j.url,
                       j.ai_score, js.changed_at
                FROM job_stages js
                JOIN jobs j ON j.job_id = js.job_id
                WHERE js.id IN (
                    SELECT MAX(id) FROM job_stages GROUP BY job_id
                )
                ORDER BY js.changed_at DESC
            """).fetchall()

        result: dict[str, list] = {s: [] for s in PIPELINE_STAGES}
        for r in stage_rows:
            stage = r["stage"]
            if stage in result:
                result[stage].append(dict(r))
        return result

    def get_stats(self) -> dict:
        today = date.today().isoformat()
        with self._conn() as c:
            status_rows = c.execute(
                "SELECT status, COUNT(*) n FROM jobs GROUP BY status"
            ).fetchall()
            today_count = c.execute(
                "SELECT COUNT(*) n FROM jobs WHERE found_at LIKE ?",
                (f"{today}%",)
            ).fetchone()["n"]
            applied_today = c.execute(
                "SELECT COUNT(*) n FROM jobs WHERE applied_at LIKE ?",
                (f"{today}%",)
            ).fetchone()["n"]
            total = c.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"]
            scored = c.execute(
                "SELECT COUNT(*) n FROM jobs WHERE ai_score IS NOT NULL"
            ).fetchone()["n"]
            avg_score = c.execute(
                "SELECT AVG(ai_score) v FROM jobs WHERE ai_score IS NOT NULL"
            ).fetchone()["v"]
            pipeline_counts = c.execute("""
                SELECT stage, COUNT(*) n FROM job_stages
                WHERE id IN (SELECT MAX(id) FROM job_stages GROUP BY job_id)
                GROUP BY stage
            """).fetchall()

        stats = {s: 0 for s in VALID_STATUSES}
        for r in status_rows:
            stats[r["status"]] = r["n"]
        stats["total"]         = total
        stats["found_today"]   = today_count
        stats["applied_today"] = applied_today
        stats["scored"]        = scored
        stats["avg_score"]     = round(avg_score or 0, 1)
        stats["pipeline"]      = {r["stage"]: r["n"] for r in pipeline_counts}
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

        with self._conn() as c:
            found_rows = c.execute("""
                SELECT substr(found_at, 1, 10) d, COUNT(*) n
                FROM jobs
                WHERE found_at >= ?
                GROUP BY d
            """, (start_date,)).fetchall()

            applied_rows = c.execute("""
                SELECT substr(applied_at, 1, 10) d, COUNT(*) n
                FROM jobs
                WHERE applied_at >= ?
                GROUP BY d
            """, (start_date,)).fetchall()

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
                FROM jobs
                GROUP BY bucket
            """).fetchall()

            top_cos = c.execute("""
                SELECT company, COUNT(*) n FROM jobs
                WHERE status='applied' GROUP BY company
                ORDER BY n DESC LIMIT 10
            """).fetchall()

            funnel_rows = c.execute("""
                SELECT stage, COUNT(DISTINCT job_id) n FROM job_stages
                WHERE id IN (SELECT MAX(id) FROM job_stages GROUP BY job_id)
                GROUP BY stage
            """).fetchall()

            source_rows = c.execute("""
                SELECT source, COUNT(*) n FROM jobs GROUP BY source
            """).fetchall()

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
        funnel_counts = {r["stage"]: r["n"] for r in funnel_rows}
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
