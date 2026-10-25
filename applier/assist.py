# applier/assist.py
"""
The two things you do after applying - or failing to - by hand in assist mode
(applier/apply_mode.py):

    mark_applied(jobs, job_id, note)       you clicked Submit on the site
                                           -> submitted, actor "user"
    mark_unavailable(jobs, job_id, reason) the posting is gone / not applicable
                                           -> skipped, actor "user"

Both are one lifecycle transition (db/lifecycle.py is the only rule book: a
job can be recorded as applied or skipped from any stage before submission, and
from none after it), so each writes exactly one application_events row with
actor "user", your note or reason in its detail and `via: "assist"`. They
never touch a browser or the site.

`jobs` is the caller's own JobsDB (JobsDB(user_id=...)): a job that is not
theirs is a LookupError, as everywhere. An illegal move (already submitted,
failed...) is lifecycle.IllegalTransition, a ValueError.
"""

import logging

logger = logging.getLogger(__name__)

MAX_TEXT = 500


def _text(value, name: str, required: bool) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{name} is required")
    if len(value) > MAX_TEXT:
        raise ValueError(f"{name} is longer than {MAX_TEXT} characters")
    return value


def mark_applied(jobs, job_id: str, note=None) -> dict:
    """Record that YOU submitted the application. Returns the transition event."""
    note = _text(note, "note", required=False)
    event = jobs.transition(job_id, "submitted", actor="user", note=note, detail={"via": "assist"})
    _close_session(job_id, applied=True)
    return event


def mark_unavailable(jobs, job_id: str, reason) -> dict:
    """Record that the application cannot be made (the posting is closed, or
    not for you). `reason` is required. Returns the transition event."""
    reason = _text(reason, "reason", required=True)
    event = jobs.transition(job_id, "skipped", actor="user", note=reason,
                            detail={"via": "assist", "unavailable": True})
    _close_session(job_id, applied=False, reason=reason)
    return event


def _close_session(job_id: str, applied: bool, reason: str = "") -> None:
    """Keep the apply session (applier/session.py) in step. Bookkeeping only."""
    try:
        from applier.session import ApplicationSession, State
        session = ApplicationSession.load(job_id)
        if session is None:
            return
        if applied:
            session.transition(State.DONE, "Applied by you on the site (assist mode)")
        else:
            session.skip(reason)
    except Exception:
        logger.exception(f"[Assist] could not update the apply session for {job_id[:8]}")
