"""
db/profile_db.py — SQLite profile store for form auto-filling.

Stores 45+ fields covering personal, professional, education, identity,
and search preferences. Supports dynamic field addition when new form
fields are discovered during applications.
"""

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from core.config import cfg

logger = logging.getLogger(__name__)

STANDARD_FIELDS = [
    # (key, default, category, type, label)
    ("full_name",            "", "personal",      "text",     "Full Name"),
    ("first_name",           "", "personal",      "text",     "First Name"),
    ("last_name",            "", "personal",      "text",     "Last Name"),
    ("email",                "", "personal",      "email",    "Email Address"),
    ("phone",                "", "personal",      "phone",    "Phone Number"),
    ("phone_alternate",      "", "personal",      "phone",    "Alternate Phone"),
    ("date_of_birth",        "", "personal",      "date",     "Date of Birth (DD/MM/YYYY)"),
    ("gender",               "", "personal",      "select",   "Gender"),
    ("nationality",          "Indian", "personal","text",     "Nationality"),
    ("location_city",        "", "personal",      "text",     "City"),
    ("location_state",       "", "personal",      "text",     "State"),
    ("location_country",     "India", "personal", "text",     "Country"),
    ("location_pincode",     "", "personal",      "text",     "PIN / ZIP Code"),
    ("linkedin_url",         "", "personal",      "url",      "LinkedIn Profile URL"),
    ("github_url",           "", "personal",      "url",      "GitHub Profile URL"),
    ("portfolio_url",        "", "personal",      "url",      "Portfolio / Website"),
    ("current_role",         "", "professional",  "text",     "Current Job Title"),
    ("experience_years",     "0","professional",  "number",   "Total Experience (years)"),
    ("notice_period_days",   "0","professional",  "number",   "Notice Period (days)"),
    ("notice_period_label",  "Immediate","professional","text","Notice Period Label"),
    ("current_ctc_lpa",      "", "professional",  "number",   "Current CTC (LPA)"),
    ("expected_ctc_lpa",     "", "professional",  "number",   "Expected CTC (LPA)"),
    ("current_ctc_monthly",  "", "professional",  "number",   "Current CTC Monthly (₹)"),
    ("expected_ctc_monthly", "", "professional",  "number",   "Expected CTC Monthly (₹)"),
    ("salary_expectation",   "", "professional",  "text",     "Salary Expectation (text)"),
    ("willing_to_relocate",  "Yes","professional","select",   "Willing to Relocate?"),
    ("work_mode_preference", "Hybrid","professional","select", "Work Mode Preference"),
    ("availability",         "Immediately","professional","text","Available to Join"),
    ("cover_letter",         "", "professional",  "textarea", "Default Cover Letter"),
    ("highest_degree",       "", "education",     "text",     "Highest Degree"),
    ("degree_field",         "", "education",     "text",     "Field of Study"),
    ("university_name",      "", "education",     "text",     "University / College"),
    ("graduation_year",      "", "education",     "text",     "Graduation Year"),
    ("graduation_score",     "", "education",     "text",     "CGPA / Percentage"),
    ("tenth_score",          "", "education",     "text",     "10th Percentage"),
    ("twelfth_score",        "", "education",     "text",     "12th Percentage"),
    ("authorized_to_work",   "Yes","identity",    "select",   "Authorized to Work in India?"),
    ("require_sponsorship",  "No", "identity",    "select",   "Require Visa Sponsorship?"),
    ("veteran_status",       "No", "identity",    "select",   "Veteran Status"),
    ("disability_status",    "No", "identity",    "select",   "Disability Status"),
    ("ethnicity",            "", "identity",      "text",     "Ethnicity (optional)"),
    ("search_positions",     "[]","search",       "json",     "Job Titles to Search"),
    ("search_locations",     "[]","search",       "json",     "Locations to Search"),
    ("skills_raw",           "[]","meta",         "json",     "Skills list from resume (JSON)"),
    ("resume_summary",       "", "meta",          "textarea", "Resume summary/objective"),
]


class ProfileDB:
    def __init__(self, path: Path | None = None):
        self.path = path or cfg.PROFILE_DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()
        self._seed()

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

    def _init_schema(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS profile_fields (
                    field_key   TEXT PRIMARY KEY,
                    field_value TEXT NOT NULL DEFAULT '',
                    category    TEXT NOT NULL DEFAULT 'general',
                    data_type   TEXT NOT NULL DEFAULT 'text',
                    label       TEXT NOT NULL DEFAULT '',
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                )""")
            c.execute("""
                CREATE TABLE IF NOT EXISTS field_history (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    field_key  TEXT NOT NULL,
                    old_value  TEXT DEFAULT '',
                    new_value  TEXT NOT NULL,
                    source     TEXT DEFAULT 'user',
                    changed_at TEXT NOT NULL
                )""")
            c.execute("""
                CREATE TABLE IF NOT EXISTS missing_fields (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id           TEXT NOT NULL,
                    job_url          TEXT NOT NULL,
                    job_title        TEXT NOT NULL,
                    company          TEXT NOT NULL,
                    field_label      TEXT NOT NULL,
                    field_key        TEXT NOT NULL,
                    field_type       TEXT DEFAULT 'text',
                    options          TEXT DEFAULT '[]',
                    placeholder_used TEXT DEFAULT '',
                    status           TEXT DEFAULT 'pending',
                    created_at       TEXT NOT NULL,
                    resolved_at      TEXT DEFAULT ''
                )""")

    def _seed(self):
        now = datetime.now().isoformat()
        with self._conn() as c:
            for key, val, cat, dtype, label in STANDARD_FIELDS:
                c.execute("""
                    INSERT OR IGNORE INTO profile_fields
                        (field_key, field_value, category, data_type, label, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (key, val, cat, dtype, label, now, now))

    # ── Read ──────────────────────────────────────────────────────

    def get(self, key: str, default: str = "") -> str:
        with self._conn() as c:
            r = c.execute("SELECT field_value FROM profile_fields WHERE field_key=?",
                          (key,)).fetchone()
            return r["field_value"] if r else default

    def get_all(self) -> dict[str, str]:
        with self._conn() as c:
            rows = c.execute("SELECT field_key, field_value FROM profile_fields").fetchall()
            return {r["field_key"]: r["field_value"] for r in rows}

    def get_by_category(self) -> dict[str, list[dict]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM profile_fields ORDER BY category, field_key"
            ).fetchall()
        out: dict[str, list] = {}
        for r in rows:
            out.setdefault(r["category"], []).append({
                "key": r["field_key"], "value": r["field_value"],
                "data_type": r["data_type"], "label": r["label"],
            })
        return out

    def summary(self) -> dict:
        """Rich summary dict passed to the LLM for form analysis."""
        f = self.get_all()

        def jl(key):  # parse JSON list field
            try: return json.loads(f.get(key, "[]"))
            except Exception: return []

        return {
            "personal": {k: f.get(k, "") for k in [
                "full_name","first_name","last_name","email","phone",
                "phone_alternate","date_of_birth","gender","nationality",
                "location_city","location_state","location_country","location_pincode",
                "linkedin_url","github_url","portfolio_url"]},
            "professional": {k: f.get(k, "") for k in [
                "current_role","experience_years","notice_period_days",
                "notice_period_label","current_ctc_lpa","expected_ctc_lpa",
                "current_ctc_monthly","expected_ctc_monthly","salary_expectation",
                "willing_to_relocate","work_mode_preference","availability","cover_letter"]},
            "education": {k: f.get(k, "") for k in [
                "highest_degree","degree_field","university_name",
                "graduation_year","graduation_score","tenth_score","twelfth_score"]},
            "identity": {k: f.get(k, "") for k in [
                "authorized_to_work","require_sponsorship",
                "veteran_status","disability_status","ethnicity"]},
            "skills": jl("skills_raw"),
            "search_positions": jl("search_positions"),
            "search_locations": jl("search_locations"),
            "_flat": f,
        }

    def is_complete(self) -> tuple[bool, list[str]]:
        required = ["full_name", "email", "phone"]
        f = self.get_all()
        missing = [k for k in required if not f.get(k, "").strip()]
        return len(missing) == 0, missing

    # ── Write ─────────────────────────────────────────────────────

    def set(self, key: str, value: str, source: str = "user"):
        now = datetime.now().isoformat()
        with self._conn() as c:
            old = c.execute("SELECT field_value FROM profile_fields WHERE field_key=?",
                            (key,)).fetchone()
            old_val = old["field_value"] if old else ""
            c.execute("""
                INSERT INTO profile_fields
                    (field_key, field_value, category, data_type, label, created_at, updated_at)
                VALUES (?, ?, 'custom', 'text', ?, ?, ?)
                ON CONFLICT(field_key) DO UPDATE SET
                    field_value = excluded.field_value,
                    updated_at  = excluded.updated_at
            """, (key, value, key.replace("_", " ").title(), now, now))
            if old_val != value:
                c.execute("""
                    INSERT INTO field_history
                        (field_key, old_value, new_value, source, changed_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (key, old_val, value, source, now))

    def set_many(self, updates: dict[str, str], source: str = "user"):
        for k, v in updates.items():
            self.set(k, v, source)

    def import_from_parsed(self, parsed: dict):
        """Map resume parser output into profile fields."""
        mapping: dict[str, str] = {}
        direct = ["full_name","first_name","last_name","email","phone",
                  "location_city","location_state","location_country",
                  "linkedin_url","github_url","portfolio_url","current_role"]
        for k in direct:
            if parsed.get(k):
                mapping[k] = str(parsed[k])
        if parsed.get("experience_years"):
            mapping["experience_years"] = str(parsed["experience_years"])
        if parsed.get("summary"):
            mapping["resume_summary"] = parsed["summary"]
            if not mapping.get("cover_letter"):
                mapping["cover_letter"] = parsed["summary"]
        if parsed.get("skills"):
            mapping["skills_raw"] = json.dumps(parsed["skills"])
        edu = parsed.get("education", [])
        if edu:
            e = edu[0]
            for src, dst in [("degree","highest_degree"),("field","degree_field"),
                              ("institution","university_name"),("year","graduation_year"),
                              ("score","graduation_score")]:
                if e.get(src):
                    mapping[dst] = str(e[src])
        if parsed.get("target_roles"):
            mapping["search_positions"] = json.dumps(parsed["target_roles"])
        self.set_many(mapping, source="resume_import")
        logger.info(f"[ProfileDB] Imported {len(mapping)} fields from resume")

    # ── Missing fields ────────────────────────────────────────────

    def flag_missing(self, job_id: str, job_url: str, job_title: str,
                     company: str, field_label: str, field_key: str,
                     field_type: str = "text", options: list | None = None,
                     placeholder: str = ""):
        now = datetime.now().isoformat()
        with self._conn() as c:
            c.execute("""
                INSERT INTO missing_fields
                    (job_id, job_url, job_title, company, field_label, field_key,
                     field_type, options, placeholder_used, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
            """, (job_id, job_url, job_title, company, field_label, field_key,
                  field_type, json.dumps(options or []), placeholder, now))

    def get_pending_missing(self, job_id: str) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("""
                SELECT * FROM missing_fields
                WHERE job_id=? AND status='pending' ORDER BY id
            """, (job_id,)).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                d["options"] = json.loads(d.get("options", "[]"))
                result.append(d)
            return result

    def resolve_missing(self, field_id: int, value: str):
        now = datetime.now().isoformat()
        with self._conn() as c:
            r = c.execute("SELECT field_key FROM missing_fields WHERE id=?",
                          (field_id,)).fetchone()
            if r:
                self.set(r["field_key"], value, source="user_prompt")
            c.execute("UPDATE missing_fields SET status='resolved', resolved_at=? WHERE id=?",
                      (now, field_id))
