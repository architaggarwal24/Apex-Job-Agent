# applier/autonomy.py
"""
Autonomy: let the agent approve an application ITSELF, but only where the
ledger (db/autonomy_db.py) shows it has earned it, and never by default.

When an application reaches ready_for_review, consider_auto_approval() looks at
this user's rules. It does nothing unless ALL of these hold:

  * the kill switch (autonomy_enabled) is ON - it is OFF until you turn it on,
    and with no rules everything is reviewed anyway;
  * a rule is enabled, applies to the application's site type (or is '*'), and
    every one of its conditions is met:
        min_score                 the job's ai_score is at least this (an unscored job fails)
        min_attempts              at least this many attempts on the site type were
                                  decided by a PERSON (the ledger's `reviewed`)
        min_success_rate          approved-unchanged over the last 20 reviewed is at least this
        require_all_answers_known the review packet exists and EVERY field in it came
                                  from the answer bank or the profile with confidence
                                  >= 0.9 (a field you typed in, or that the AI guessed,
                                  fails it; so does a missing packet)
        max_per_day               this rule has not already auto-approved this many today
  * the daily apply limit has room (not enforced in dry run, nothing is being
    applied - the same rule /api/apply/submit follows).

The first rule that matches wins. A match is logged on the job's timeline as an
event with actor "autonomy" and the rule id, then the application is submitted
through the ordinary Orchestrator.submit, so dry_run behaves exactly as it
does for a person's approval.

A failure (applier/failures.py class) on a site type switches off every rule
that applies to it - until a person re-enables them - and says so on the
timeline. Failures are recorded where the Orchestrator classifies them.

Everything is per user: the user is the job's owner (a row with no owner
belongs to the first account, as everywhere), and every store is scoped to it.
"""

import logging
from datetime import datetime

from applier.site_type import detect_site_type
from db.answer_bank_db import PROFILE_OWNER_ID
from db.autonomy_db import ANY_SITE, AutonomyStore
from db.jobs_db import JobsDB
from db.review_db import ReviewStore

logger = logging.getLogger(__name__)

KNOWN_SOURCES  = ("answer_bank", "profile")
KNOWN_CONFIDENCE = 0.9


def owner_of(job: dict) -> int:
    return job.get("user_id") or PROFILE_OWNER_ID


# ── the decision (pure) ───────────────────────────────────────────

def answers_known(packet: dict | None) -> bool:
    """Every field of the packet came from the answer bank or the profile with
    confidence >= 0.9. No packet is NOT known: with nothing to check, nothing
    is vouched for. A packet with no fields (a one-click apply, where there is
    nothing to answer) is trivially known."""
    if not isinstance(packet, dict) or not isinstance(packet.get("fields"), list):
        return False
    return all(f.get("source") in KNOWN_SOURCES
               and isinstance(f.get("confidence"), (int, float))
               and f["confidence"] >= KNOWN_CONFIDENCE
               for f in packet["fields"])


def rule_failures(rule: dict, *, site_type: str, ai_score, packet: dict | None,
                  stats: dict, approved_today: int) -> list[str]:
    """Why `rule` does NOT allow auto-approval right now; [] means it does.
    Every unmet condition is listed, so a decision can be explained."""
    why = []
    if not rule["enabled"]:
        why.append("rule is disabled")
    if rule["site_type"] not in (site_type, ANY_SITE):
        why.append(f"rule is for {rule['site_type']}, not {site_type}")
    if ai_score is None or ai_score < rule["min_score"]:
        why.append(f"score {ai_score} is below {rule['min_score']}")
    if stats["reviewed"] < rule["min_attempts"]:
        why.append(f"only {stats['reviewed']} reviewed attempts, needs {rule['min_attempts']}")
    rate = stats["success_rate"]
    if rate is None or rate < rule["min_success_rate"]:
        why.append(f"success rate {rate} is below {rule['min_success_rate']}")
    if rule["require_all_answers_known"] and not answers_known(packet):
        why.append("not every answer is from the answer bank or profile at confidence 0.9+")
    if approved_today >= rule["max_per_day"]:
        why.append(f"already auto-approved {approved_today} today (max {rule['max_per_day']})")
    return why


def first_matching_rule(rules: list[dict], *, site_type: str, ai_score, packet: dict | None,
                        stats: dict, approved_today) -> dict | None:
    """The first rule (lowest id) with no failures. `approved_today` is a
    function rule_id -> int, asked only for rules that get that far."""
    for rule in sorted(rules, key=lambda r: r["id"]):
        if rule_failures(rule, site_type=site_type, ai_score=ai_score, packet=packet,
                         stats=stats, approved_today=approved_today(rule["id"])):
            continue
        return rule
    return None


def auto_submitted(events: list[dict], event_id: int) -> bool:
    """True if the move to submitted with id `event_id` was an autonomy rule's doing: the
    last thing before it, since the job's previous stage change, is an autonomy_approved
    step that was not a dry run (consider_auto_approval logs it, then the Orchestrator
    submits). `events` is the job's timeline, oldest first (JobsDB.get_application)."""
    approved = False
    for e in events:
        if e["id"] == event_id:
            return approved and e["to_stage"] == "submitted"
        if e["kind"] != "step":
            approved = False                         # a stage change since: whatever came before is over
        elif (e.get("detail") or {}).get("step") == "autonomy_approved":
            approved = not (e.get("detail") or {}).get("dry_run")
    return False


# ── what the Orchestrator calls (each is safe to call: bookkeeping must not
#    stop an application, so they log instead of raising) ──────────────────

def _stores(job: dict, path):
    uid = owner_of(job)
    return (AutonomyStore(uid, path), JobsDB(path=path, user_id=uid), ReviewStore(uid, path))


def site_type_for(job: dict, detected: str = "") -> str:
    """The site type the page was detected as, else what the URL alone says."""
    from applier.target import apply_url
    return detected or detect_site_type(apply_url(job))


def record_review_reached(job: dict, site_type: str, path=None) -> None:
    try:
        AutonomyStore(owner_of(job), path).record_review_reached(job["job_id"], site_type)
    except Exception:
        logger.exception("[Autonomy] could not record that the application reached review")


def record_failure(job: dict, site_type: str, failure_class: str, path=None) -> list[int]:
    """A classified failure on `site_type`: write it to the ledger and switch
    off every rule that applies to it. Returns the ids it switched off."""
    try:
        store, jobs, _ = _stores(job, path)
        store.record_failure(job["job_id"], site_type, failure_class)
        reason = f"{failure_class} failure on {site_type} (job {job['job_id'][:8]})"
        disabled = store.disable_rules_for(site_type, reason)
        if disabled:
            jobs.record_step(job["job_id"], "autonomy_disabled",
                             f"Autonomy rules switched off after a {failure_class} failure on {site_type}",
                             {"rule_ids": disabled, "site_type": site_type,
                              "failure_class": failure_class}, actor="autonomy")
        return disabled
    except Exception:
        logger.exception("[Autonomy] could not record a failure")
        return []


def record_human_approval(job: dict, corrections: bool = False, path=None) -> None:
    """A person approved the application: unchanged, or edited (they changed a
    value in review, or sent corrections with the approval)."""
    try:
        store, jobs, _ = _stores(job, path)
        attempt = store.pending_attempt(job["job_id"])
        if attempt is None:
            return
        edited = corrections or any(
            e["kind"] == "step" and e["detail"].get("step") == "review_edit"
            and e["at"] > attempt["reached_at"]
            for e in (jobs.get_application(job["job_id"]) or {}).get("events", []))
        store.close_attempt(job["job_id"], "approved_edited" if edited else "approved_unchanged")
    except Exception:
        logger.exception("[Autonomy] could not record an approval")


def record_rejection(jobs, job_id: str) -> None:
    """A person rejected the application in review. `jobs` is the user's
    scoped JobsDB; an unscoped one can't say whose ledger this belongs to."""
    try:
        if jobs.user_id is None:
            return
        AutonomyStore(jobs.user_id, jobs.path).close_attempt(job_id, "rejected")
    except Exception:
        logger.exception("[Autonomy] could not record a rejection")


def consider_auto_approval(job: dict, site_type: str, *, path=None, settings=None,
                           now: datetime | None = None) -> dict | None:
    """Called when `job` has just reached ready_for_review. Returns the rule
    that auto-approved it (the attempt is closed as auto_approved and the event
    logged - the caller now submits), or None: it stays for a person to review.
    Never raises."""
    try:
        store, jobs, packets = _stores(job, path)
        if not store.is_enabled():
            return None
        rules = [r for r in store.list_rules() if r["enabled"]]
        if not rules:
            return None
        job_id = job["job_id"]
        if store.pending_attempt(job_id) is None:        # it did not reach review through us
            return None
        # The job row, not the dict the Orchestrator was built with, says what
        # the score is now.
        current = jobs.get_by_id(job_id) or job
        rule = first_matching_rule(
            rules, site_type=site_type, ai_score=current.get("ai_score"),
            packet=packets.get(job_id), stats=store.site_stats(site_type),
            approved_today=lambda rid: store.auto_approved_today(rid, now))
        if rule is None:
            return None

        from applier.queue_worker import daily_limit, is_dry_run
        settings = settings if settings is not None else _settings()
        dry_run = is_dry_run(settings)
        if not dry_run:
            cap, done = daily_limit(settings), jobs.count_applied_today()
            if cap > 0 and done >= cap:
                jobs.record_step(job_id, "autonomy_blocked",
                                 f"Autonomy rule #{rule['id']} matched but the daily limit is reached ({done}/{cap})",
                                 {"rule_id": rule["id"], "site_type": site_type,
                                  "reason": "daily_limit"}, actor="autonomy")
                return None

        if not store.close_attempt(job_id, "auto_approved", rule_id=rule["id"], now=now):
            return None
        jobs.record_step(job_id, "autonomy_approved",
                         f"Auto-approved by autonomy rule #{rule['id']}" + (" (dry run)" if dry_run else ""),
                         {"rule_id": rule["id"], "site_type": site_type, "dry_run": dry_run},
                         actor="autonomy")
        return rule
    except Exception:
        logger.exception("[Autonomy] could not evaluate the rules; leaving it for review")
        return None


def preview_rule(rule: dict, queued: list[dict], *, store, settings, applied_today: int,
                 now: datetime | None = None) -> dict:
    """What `rule` would do to the jobs waiting in the queue - read-only, nothing is
    approved or recorded. `queued` is this user's queued jobs in queue order (highest
    score first) with their url and ai_score; `store` this user's AutonomyStore.

    Each job gets would_submit and the reasons it would not, from the same checks
    consider_auto_approval makes: the kill switch, assist mode (never auto-submitted),
    rule_failures() with this rule's site type, score, ledger and daily max (counting the
    jobs ahead of it in this preview), and the daily apply limit (not in dry run). A queued
    job has no review packet yet, so "every answer known" can only be judged when it reaches
    review: a rule that requires it marks the job "would_submit" with that condition noted.
    Plus the rule's own ledger stats (its site type's; none for "*") and auto-approvals today."""
    from applier import apply_mode
    from applier.queue_worker import daily_limit, is_dry_run
    enabled = store.is_enabled()
    approved = store.auto_approved_today(rule["id"], now)
    dry_run = is_dry_run(settings)
    cap = daily_limit(settings)
    lenient = {**rule, "require_all_answers_known": False}         # judged at review, not now
    jobs, taken = [], 0
    for job in queued:
        site_type = site_type_for(job)
        why = []
        if not enabled:
            why.append("autonomy is switched off")
        if apply_mode.mode_for(settings, site_type) == "assist":
            why.append(f"{site_type} is in assist mode: you apply by hand")
        why += rule_failures(lenient, site_type=site_type, ai_score=job.get("ai_score"), packet=None,
                             stats=store.site_stats(site_type), approved_today=approved + taken)
        if not dry_run and cap > 0 and applied_today + taken >= cap:
            why.append(f"the daily apply limit is reached ({applied_today + taken}/{cap})")
        ok = not why
        taken += ok
        jobs.append({"job_id": job["job_id"], "title": job.get("title", ""), "company": job.get("company", ""),
                     "ai_score": job.get("ai_score"), "site_type": site_type, "would_submit": ok, "reasons": why,
                     "condition": "only if every answer is from your answer bank or profile (checked at review)"
                                  if ok and rule["require_all_answers_known"] else ""})
    return {"rule_id": rule["id"], "site_type": rule["site_type"], "autonomy_enabled": enabled, "dry_run": dry_run,
            "stats": None if rule["site_type"] == ANY_SITE else store.site_stats(rule["site_type"]),
            "auto_approved_today": approved, "would_submit": taken, "jobs": jobs}


def _settings():
    from db.settings_db import SettingsDB
    return SettingsDB.get()
