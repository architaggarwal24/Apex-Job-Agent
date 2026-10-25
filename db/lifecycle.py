# db/lifecycle.py
"""
The ONE lifecycle every job moves through. This module is the only place
that knows which stages exist and which moves between them are legal;
db/jobs_db.JobsDB.transition() is the only code that applies one.

    found -> queued -> preparing <-> needs_you -> ready_for_review -> submitted
          -> recruiter_screen -> technical_round -> hiring_manager -> offer
    terminal: rejected, withdrawn, skipped, failed

It replaces three older, separately-stored ideas of "where is this job":
jobs.status, the job_stages table and the apply-session state. The old
vocabulary survives only as read-side translations at the bottom of this
file, so the remaining /api/jobs/{id}, /api/apply/* and /api/tracking/*
routes keep their shapes.
"""

STAGES = (
    "found", "queued", "preparing", "needs_you", "ready_for_review",
    "submitted", "recruiter_screen", "technical_round", "hiring_manager",
    "offer",
    "rejected", "withdrawn", "skipped", "failed",
)

TERMINAL = ("rejected", "withdrawn", "skipped", "failed")

# Why a job is parked in needs_you. Every blocking failure class from
# applier/failures.py is here (that module asserts it), plus "other" for a
# user who parks a job by hand.
BLOCKED_REASONS = ("question", "captcha", "login_required",
                   "unsupported_form", "naukri_chat_screening",
                   "validation_error", "job_closed", "other")

ACTORS = ("agent", "user", "system", "autonomy")

# Why a row exists in application_events. "step" is progress WHILE working
# (opened the page, filled 12 fields...): from_stage == to_stage == the stage
# the job is in, and nothing about the job changes.
KINDS = ("created", "transition", "migrated", "step")

# THE table of legal moves. Anything not listed here raises.
#
# Beyond the happy path it allows:
#   * "submitted" straight from any pre-submit stage - the user applied by
#     hand on the company's site and is recording it;
#   * post-submit stages can be skipped over (recruiter_screen -> offer) but
#     never revisited, and every active stage can end in rejected/withdrawn;
#   * failed and skipped are the two terminal stages a user can re-open
#     (retry a failure, un-skip a job) - the apply flow has always allowed
#     both. rejected and withdrawn are final;
#   * the queue: queued -> found is "unqueue", and preparing -> queued is how
#     the queue worker puts a job back after a restart interrupted it.
TRANSITIONS = {
    "found":            ("queued", "preparing", "submitted", "skipped"),
    "queued":           ("found", "preparing", "submitted", "skipped"),
    "preparing":        ("queued", "needs_you", "ready_for_review",
                         "submitted", "failed", "skipped"),
    "needs_you":        ("preparing", "ready_for_review", "submitted",
                         "failed", "skipped"),
    "ready_for_review": ("preparing", "needs_you", "submitted",
                         "failed", "skipped"),
    "submitted":        ("recruiter_screen", "technical_round",
                         "hiring_manager", "offer", "rejected", "withdrawn"),
    "recruiter_screen": ("technical_round", "hiring_manager", "offer",
                         "rejected", "withdrawn"),
    "technical_round":  ("hiring_manager", "offer", "rejected", "withdrawn"),
    "hiring_manager":   ("offer", "rejected", "withdrawn"),
    "offer":            ("rejected", "withdrawn"),
    "rejected":         (),
    "withdrawn":        (),
    "failed":           ("queued", "preparing"),
    "skipped":          ("found", "queued", "preparing"),
}

# Stages at or after "submitted": the application went out.
SUBMITTED_OR_LATER = ("submitted", "recruiter_screen", "technical_round",
                      "hiring_manager", "offer", "rejected", "withdrawn")


class IllegalTransition(ValueError):
    """A well-formed request for a move the table doesn't allow."""


def allowed_from(stage: str) -> tuple:
    return TRANSITIONS.get(stage, ())


def can_transition(from_stage: str, to_stage: str) -> bool:
    return to_stage in TRANSITIONS.get(from_stage, ())


def check(from_stage, to_stage: str, reason: str | None = None) -> None:
    """Raise unless from_stage -> to_stage (carrying `reason`) is legal.
    ValueError: malformed input. IllegalTransition: valid names, illegal move."""
    if to_stage not in STAGES:
        raise ValueError(f"Unknown stage: {to_stage!r}")
    if from_stage not in STAGES:
        raise ValueError(f"Job is at an unknown stage: {from_stage!r}")
    if to_stage == "needs_you":
        if reason not in BLOCKED_REASONS:
            raise ValueError(
                f"needs_you requires a reason, one of {list(BLOCKED_REASONS)}")
    elif reason:
        raise ValueError("A reason is only valid when moving to needs_you")
    if to_stage not in TRANSITIONS[from_stage]:
        raise IllegalTransition(
            f"Illegal transition {from_stage} -> {to_stage}. "
            f"Allowed from {from_stage}: {list(TRANSITIONS[from_stage])}")


# ── Translations from/to the pre-lifecycle vocabulary ─────────────
# Used by the migration, by the compatibility wrappers, and to keep the
# read-only jobs.status column that the old endpoints still return.

# Old job_stages / PIPELINE_STAGES names -> stages ("applied" was renamed).
PIPELINE_TO_STAGE = {
    "applied": "submitted", "recruiter_screen": "recruiter_screen",
    "technical_round": "technical_round", "hiring_manager": "hiring_manager",
    "offer": "offer", "rejected": "rejected", "withdrawn": "withdrawn",
}
STAGE_TO_PIPELINE = {v: k for k, v in PIPELINE_TO_STAGE.items()}

# Old apply-session states -> (stage, blocked_reason). idle/done/skipped have
# no entry: "idle" means nothing has run, "done" is covered by the job's
# stage history, and a skipped session is the "skipped" status.
SESSION_STATE_TO_STAGE = {
    "analyzing":         ("preparing", ""),
    "filling":           ("preparing", ""),
    "refilling":         ("preparing", ""),
    "awaiting_input":    ("needs_you", "question"),
    "screenshot_review": ("ready_for_review", ""),
    "submitting":        ("ready_for_review", ""),
    "failed":            ("failed", ""),
}

# Old jobs.status -> new target stage, for update_status() callers.
# in_progress/captcha_blocked/manual have no single honest target stage;
# they are only ever migrated, never written.
STATUS_TO_STAGE = {
    "found": ("found", ""), "applied": ("submitted", ""),
    "skipped": ("skipped", ""), "error": ("failed", ""),
}


def stage_from_legacy(status: str, session_state: str | None,
                      latest_pipeline_stage: str | None) -> tuple[str, str]:
    """Where an old row belongs: (stage, blocked_reason).

    in_progress is decided by the apply session first; if the session says
    nothing useful (idle/done/missing), the latest job_stages row decides
    (that is how a job in a later round was recorded); otherwise it goes
    back to found, where the user can simply start it again.
    applied -> the latest job_stages stage (applied -> submitted)."""
    latest = PIPELINE_TO_STAGE.get(latest_pipeline_stage or "")
    if status == "in_progress":
        if session_state in SESSION_STATE_TO_STAGE:
            return SESSION_STATE_TO_STAGE[session_state]
        return (latest, "") if latest else ("found", "")
    if status == "applied":
        return (latest or "submitted", "")
    if status == "skipped":
        return ("skipped", "")
    if status == "error":
        return ("failed", "")
    if status == "captcha_blocked":
        return ("needs_you", "captcha")
    if status == "manual":
        return ("needs_you", "unsupported_form")
    return ("found", "")                      # found, '' and anything unknown


def legacy_status(stage: str, blocked_reason: str = "") -> str:
    """The old jobs.status value for a stage (what the old endpoints and the
    Jobs page filter on). Derived, never an independent fact."""
    if stage in ("found", "queued"):
        return "found"
    if stage == "submitted":
        return "applied"
    if stage == "skipped":
        return "skipped"
    if stage == "failed":
        return "error"
    if stage == "needs_you" and blocked_reason == "captcha":
        return "captcha_blocked"
    if stage == "needs_you" and blocked_reason == "unsupported_form":
        return "manual"
    return "in_progress"
