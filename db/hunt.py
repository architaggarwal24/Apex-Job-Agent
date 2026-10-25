# db/hunt.py
"""
db/hunt.py — what a card knows about the HUNT, not just the stage.

Five fields a person fills in on a job (db/jobs_db.py stores them on the jobs row):

    next_step_text   "Technical round with Dana"         what happens next
    next_step_at     "2026-11-03" or a date-time         when (a date alone means that whole day)
    contact_name     "Dana Whitfield"                    who you are dealing with
    contact_email    "dana@acme.example"
    last_contact_at  "2026-10-28"                        when you last heard from / wrote to them

and two the server works out on every read (never stored, so they can't go stale themselves):

    days_in_stage    whole days since the job entered its current stage
    stale            it has been there at least as long as that stage's limit

A job at a terminal stage (rejected, withdrawn, skipped, failed) is never stale: nothing is
waiting on anyone. Every other stage - found, queued... offer - is judged by its limit:
submitted 7 days, recruiter_screen 5, any other stage 7. The limits are the settings
stale_days_submitted / stale_days_recruiter_screen / stale_days_other (owner-editable, whole
days 1-365); a missing or invalid value falls back to the default.

Times are the server's local clock, naive ISO text, like every other timestamp in the app.
"""

import re
from datetime import date, datetime, time, timedelta

from db import lifecycle

HUNT_FIELDS = ("next_step_text", "next_step_at", "contact_name", "contact_email", "last_contact_at")

STALE_DEFAULTS = {"submitted": 7, "recruiter_screen": 5, "other": 7}
STALE_KEYS = {"submitted": "stale_days_submitted",
              "recruiter_screen": "stale_days_recruiter_screen",
              "other": "stale_days_other"}
STALE_MIN, STALE_MAX = 1, 365

MAX_STEP, MAX_NAME, MAX_EMAIL = 300, 120, 254
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


def stale_thresholds(read=None) -> dict:
    """{"submitted": n, "recruiter_screen": n, "other": n} in days. `read(key, default)` is
    the settings reader (default: SettingsDB); anything unreadable or out of range is the
    default for that stage."""
    if read is None:
        try:
            from db.settings_db import SettingsDB
            read = SettingsDB.get().read
        except Exception:
            read = None
    out = {}
    for name, key in STALE_KEYS.items():
        try:
            days = int(str(read(key, "")).strip()) if read else 0
        except (TypeError, ValueError):
            days = 0
        out[name] = days if STALE_MIN <= days <= STALE_MAX else STALE_DEFAULTS[name]
    return out


def clean_stale_days(key: str, value) -> str:
    """A stale_days_* setting as it is stored: whole days, STALE_MIN to STALE_MAX. ValueError says why not."""
    try:
        days = int(str(value).strip())
    except ValueError:
        days = 0
    if not STALE_MIN <= days <= STALE_MAX:
        raise ValueError(f"{key} must be a whole number of days, {STALE_MIN} to {STALE_MAX}")
    return str(days)


def parse_when(value) -> datetime | None:
    """A date ("2026-11-03") or ISO date-time as a naive local datetime; None if it isn't one.
    A time zone (the "Z" a browser adds) is converted to local time and dropped."""
    s = str(value or "").strip()
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    if d.tzinfo is not None:
        d = d.astimezone().replace(tzinfo=None)
    return d


def is_date_only(value) -> bool:
    s = str(value or "").strip()
    try:
        return len(s) == 10 and date.fromisoformat(s) is not None
    except ValueError:
        return False


def derive(stage: str | None, entered_at, now: datetime, limits: dict) -> dict:
    """{"days_in_stage": int | None, "stale": bool} for a job at `stage` since `entered_at`."""
    entered = parse_when(entered_at)
    if entered is None:
        return {"days_in_stage": None, "stale": False}
    days = max(0, (now - entered).days)
    if stage not in lifecycle.STAGES or stage in lifecycle.TERMINAL:
        return {"days_in_stage": days, "stale": False}
    limit = limits.get(stage, limits["other"]) if stage in STALE_KEYS else limits["other"]
    return {"days_in_stage": days, "stale": days >= limit}


def clean_hunt(data) -> dict:
    """Validate a partial update of the five fields. Returns {field: clean value} for the
    fields given; "" (or null) clears one. ValueError says what is wrong. Dates are kept as a
    date or normalised to local date-time text so they sort and compare as text."""
    if not isinstance(data, dict):
        raise ValueError("Expected an object of fields to change")
    unknown = sorted(set(data) - set(HUNT_FIELDS))
    if unknown:
        raise ValueError(f"Unknown field(s): {', '.join(map(str, unknown))}; allowed: {', '.join(HUNT_FIELDS)}")
    if not data:
        raise ValueError("Nothing to change")
    out = {}
    for key, value in data.items():
        value = "" if value is None else value
        if not isinstance(value, str):
            raise ValueError(f"{key} must be text")
        value = " ".join(_CONTROL.sub(" ", value).split())
        if key == "next_step_text" and len(value) > MAX_STEP:
            raise ValueError(f"next_step_text is too long ({MAX_STEP} characters at most)")
        if key == "contact_name" and len(value) > MAX_NAME:
            raise ValueError(f"contact_name is too long ({MAX_NAME} characters at most)")
        if key == "contact_email" and value and (len(value) > MAX_EMAIL or not _EMAIL.match(value)):
            raise ValueError("contact_email is not an email address")
        if key in ("next_step_at", "last_contact_at") and value:
            when = parse_when(value)
            if when is None:
                raise ValueError(f"{key} must be a date (2026-11-03) or a date-time (2026-11-03T14:30)")
            value = value if is_date_only(value) else when.isoformat(timespec="seconds")
        out[key] = value
    return out


def next_step_due(next_step_at: str) -> datetime:
    """When a next step has 'passed': the moment itself, or for a date alone, the end of that day."""
    when = parse_when(next_step_at)
    if when is None:
        raise ValueError("not a date")
    if is_date_only(next_step_at):
        return datetime.combine(when.date(), time.min) + timedelta(days=1)
    return when
