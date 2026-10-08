"""
<<<<<<< HEAD
intelligence/scorer.py — LLM-powered job scoring.

Scores each job 0-100 against the user's profile with a plain-English
reason. Inspired by JobOps's scorer but extended with:
  - Explicit scoring criteria breakdown
  - Auto-skip for jobs below threshold
  - Batch scoring to minimise API calls
"""

import logging
=======
intelligence/scorer.py — LLM-powered job scoring with pre-filtering.

Two-stage scoring to handle large batches efficiently:
  Stage 1: Fast heuristic pre-filter (no API calls)
           Eliminates obvious mismatches — senior/lead/manager roles
           for freshers, huge experience gaps, wrong domains.
  Stage 2: LLM scoring for everything that survives Stage 1
           Run with a thread pool for concurrency since each
           call can take 30-90s on slower providers (e.g. NVIDIA NIM).

Supports graceful cancellation via a threading.Event so Ctrl+C
stops the batch cleanly instead of hanging.
"""

import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from core.shutdown import CancelEvent

>>>>>>> 4226f10 (.)
from core import llm as LLM
from core.config import cfg

logger = logging.getLogger(__name__)

<<<<<<< HEAD
=======
# Number of concurrent LLM scoring calls.
# Higher = faster but more load on the provider / rate limits.
SCORING_WORKERS = 5

>>>>>>> 4226f10 (.)
SYSTEM = """You are an expert recruiter evaluating job fit.
Given a job description and a candidate's profile, score the fit 0-100.

Return ONLY valid JSON — no markdown, no explanation:
{
  "score": 75,
  "reason": "One sentence explanation of the score",
  "pros": ["strength 1", "strength 2"],
  "cons": ["gap 1", "gap 2"],
  "auto_skip": false
}

Scoring guide:
  90-100: Exceptional fit — candidate meets every requirement
  70-89:  Strong fit — meets most requirements, minor gaps
  50-69:  Decent fit — meets core requirements, some gaps
  30-49:  Weak fit — significant gaps but not disqualifying
  0-29:   Poor fit — too many disqualifying gaps

<<<<<<< HEAD
Set auto_skip=true if:
  - Required experience far exceeds candidate's (e.g. 10yr req, 0yr candidate)
  - Role is completely outside candidate's domain
  - Senior/lead/manager role for a fresher with no management experience
  - Location is incompatible and candidate cannot relocate

Be realistic. A fresher applying for senior roles should score 20-40."""


def score_job(job: dict, profile: dict) -> dict:
    """
    Score a single job against the user's profile.

    Returns:
        {score, reason, pros, cons, auto_skip}
    """
    flat     = profile.get("_flat", {})
    skills   = profile.get("skills", [])
    exp      = flat.get("experience_years", "0")
    role     = flat.get("current_role", "")
    summary  = flat.get("resume_summary", "")

    jd_text = _build_jd_text(job)

=======
Be realistic. A fresher applying for senior roles should score 20-40."""


# ── Stage 1: Heuristic pre-filter (no API calls) ──────────────────────────────

SENIOR_KEYWORDS = [
    "senior", "sr.", "sr ", "staff ", "principal", "lead ", "lead,",
    "manager", "director", "head of", "vp ", "vice president",
    "architect", "chief", "9+ years", "10+ years", "8+ years",
    "7+ years", "6+ years", "12+ years", "15+ years",
]

JUNIOR_FRIENDLY_KEYWORDS = [
    "intern", "internship", "junior", "fresher", "entry level",
    "entry-level", "graduate", "associate", "trainee", "0-1 year",
    "0-2 year", "1-2 year", "no experience",
]


def _heuristic_prefilter(job: dict, experience_years: float) -> dict | None:
    """
    Fast pre-check using title/description keywords — no LLM call.

    Returns a score dict (auto_skip=True) if the job is an obvious
    mismatch, or None if it should go to the LLM for proper scoring.
    """
    title = (job.get("title") or "").lower()
    desc  = (job.get("description") or "")[:500].lower()
    text  = f"{title} {desc}"

    # Freshers (0-1 yrs): skip senior/lead/manager/architect roles
    if experience_years <= 1:
        # If title explicitly says junior/intern/fresher/associate, let it through
        if any(k in title for k in JUNIOR_FRIENDLY_KEYWORDS):
            return None

        for kw in SENIOR_KEYWORDS:
            if kw in title:
                return {
                    "score": 15,
                    "reason": f"Pre-filtered: title contains '{kw.strip()}' — "
                              f"too senior for {experience_years:.0f} years experience",
                    "pros": [], "cons": [f"Role requires senior-level experience ({kw.strip()})"],
                    "auto_skip": True,
                }

        # Check for explicit large experience requirements in description
        m = re.search(r"(\d{1,2})\+?\s*(?:to\s*\d+\s*)?years?\s*(?:of\s*)?experience", desc)
        if m:
            required_years = int(m.group(1))
            if required_years >= 5:
                return {
                    "score": 18,
                    "reason": f"Pre-filtered: requires {required_years}+ years experience, "
                              f"candidate has {experience_years:.0f}",
                    "pros": [], "cons": [f"Experience gap: needs {required_years}+ years"],
                    "auto_skip": True,
                }

    return None  # Needs LLM scoring


# ── Stage 2: LLM scoring ────────────────────────────────────────────────────

def score_job(job: dict, profile: dict) -> dict:
    """Score a single job against the user's profile via LLM."""
    flat    = profile.get("_flat", {})
    skills  = profile.get("skills", [])
    exp     = flat.get("experience_years", "0")
    role    = flat.get("current_role", "")
    summary = flat.get("resume_summary", "")

    # Stage 1: heuristic pre-filter
    try:
        exp_f = float(exp)
    except (ValueError, TypeError):
        exp_f = 0.0

    pre = _heuristic_prefilter(job, exp_f)
    if pre is not None:
        logger.info(f"  [Scorer] {job.get('title','?')} @ {job.get('company','?')}: "
                    f"{pre['score']}/100 (pre-filtered) — {pre['reason'][:60]}")
        return pre

    # Stage 2: LLM scoring
    jd_text = _build_jd_text(job)
>>>>>>> 4226f10 (.)
    candidate_text = f"""
Current Role: {role}
Experience: {exp} years
Skills: {', '.join(skills[:20]) if skills else 'Not specified'}
Education: {flat.get('highest_degree','')} in {flat.get('degree_field','')}
Location: {flat.get('location_city','')}, {flat.get('location_country','India')}
Willing to relocate: {flat.get('willing_to_relocate','Yes')}
Summary: {summary[:300] if summary else 'Not provided'}
""".strip()

    try:
        result = LLM.chat_json(
            messages=[{"role": "user", "content":
                f"JOB:\n{jd_text}\n\nCANDIDATE:\n{candidate_text}"}],
            system=SYSTEM,
            max_tokens=512,
        )
<<<<<<< HEAD
        score     = int(result.get("score", 50))
        score     = max(0, min(100, score))
        reason    = result.get("reason", "")
        pros      = result.get("pros", [])
        cons      = result.get("cons", [])
        auto_skip = result.get("auto_skip", False)

        # Also auto-skip if below configured threshold
        if score < cfg.AUTO_SKIP_SCORE_THRESHOLD:
            auto_skip = True

        logger.info(f"  [Scorer] {job.get('title','?')} @ {job.get('company','?')}: "
                    f"{score}/100 — {reason[:60]}")
        return {
            "score": score, "reason": reason,
            "pros": pros, "cons": cons, "auto_skip": auto_skip,
        }

    except Exception as e:
        logger.warning(f"  [Scorer] Failed for {job.get('title','?')}: {e}")
        return {"score": 50, "reason": "Scoring failed", "pros": [], "cons": [], "auto_skip": False}


def score_jobs_batch(jobs: list[dict], profile: dict,
                     jobs_db=None) -> list[dict]:
    """
    Score a list of jobs and optionally save scores to JobsDB.
    Returns the same list with score fields added.
    """
    logger.info(f"[Scorer] Scoring {len(jobs)} jobs...")
    results = []

    for job in jobs:
        # Skip already-scored jobs
=======
        score     = max(0, min(100, int(result.get("score", 50))))
        reason    = result.get("reason", "")
        pros      = result.get("pros", [])
        cons      = result.get("cons", [])
        auto_skip = result.get("auto_skip", False) or score < _auto_skip_threshold()

        logger.info(f"  [Scorer] {job.get('title','?')} @ {job.get('company','?')}: "
                    f"{score}/100 — {reason[:60]}")
        return {"score": score, "reason": reason, "pros": pros, "cons": cons,
                "auto_skip": auto_skip}

    except Exception as e:
        logger.warning(f"  [Scorer] Failed for {job.get('title','?')}: {e}")
        return {"score": 50, "reason": "Scoring failed — kept for manual review",
                "pros": [], "cons": [], "auto_skip": False}


# ── Batch scoring with concurrency + cancellation ─────────────────────────────

def _llm_scoring_enabled() -> bool:
    """Check whether LLM-based scoring is enabled (default: off)."""
    try:
        from db.settings_db import SettingsDB
        return SettingsDB.get().read("enable_llm_scoring", "false").lower() == "true"
    except Exception:
        return False


def _auto_skip_threshold() -> int:
    """Read the UI-configurable auto-skip threshold, falling back to
    the .env default only if SettingsDB is unavailable."""
    try:
        from db.settings_db import SettingsDB
        return SettingsDB.get().auto_skip_score_threshold()
    except Exception:
        return cfg.AUTO_SKIP_SCORE_THRESHOLD


def score_jobs_batch(jobs: list[dict], profile: dict,
                     jobs_db=None,
                     cancel_event: threading.Event | None = None,
                     emit=None) -> list[dict]:
    """
    Score a list of jobs. Splits work between:
      - Heuristic pre-filter (instant, no API call) — always runs
      - LLM scoring via thread pool (concurrent, SCORING_WORKERS at a time)
        — only runs if "enable_llm_scoring" is turned on in Settings
          (default: OFF, to save tokens and time)

    Jobs that pass the pre-filter without LLM scoring are left with
    ai_score=None (shown as "—" / not yet scored) rather than a fake
    score, so they remain visible as "found" and ready to apply.

    Args:
        cancel_event: if set, stops processing remaining jobs ASAP
                       (used for graceful Ctrl+C shutdown)
        emit:          optional callback(event, data) for progress updates
    """
    total       = len(jobs)
    llm_enabled = _llm_scoring_enabled()

    logger.info(f"[Scorer] Processing {total} jobs "
                f"(pre-filter always on, LLM scoring {'ON' if llm_enabled else 'OFF'})")

    # Phase 1: pre-filter pass (instant, no API calls)
    needs_llm: list[dict] = []
    results:   list[dict] = []

    flat = profile.get("_flat", {})
    try:
        exp_f = float(flat.get("experience_years", "0"))
    except (ValueError, TypeError):
        exp_f = 0.0

    pre_filtered_count = 0
    for job in jobs:
>>>>>>> 4226f10 (.)
        if job.get("ai_score") is not None:
            results.append(job)
            continue

<<<<<<< HEAD
        scored = score_job(job, profile)
        job["ai_score"]        = scored["score"]
        job["ai_score_reason"] = scored["reason"]
        job["auto_skip"]       = scored["auto_skip"]

        if jobs_db:
            jobs_db.update_score(
                job["job_id"], scored["score"], scored["reason"]
            )
            if scored["auto_skip"]:
                jobs_db.update_status(job["job_id"], "skipped",
                                      error=f"Auto-skip: score {scored['score']}/100")

        results.append(job)
=======
        pre = _heuristic_prefilter(job, exp_f)
        if pre is not None:
            job["ai_score"]        = pre["score"]
            job["ai_score_reason"] = pre["reason"]
            job["auto_skip"]       = pre["auto_skip"]
            if jobs_db:
                jobs_db.update_score(job["job_id"], pre["score"], pre["reason"])
                if pre["auto_skip"]:
                    jobs_db.update_status(job["job_id"], "skipped",
                                          error=f"Auto-skip: {pre['reason']}")
            results.append(job)
            pre_filtered_count += 1
        elif llm_enabled:
            needs_llm.append(job)
        else:
            # LLM scoring disabled — leave unscored, job stays "found"
            # and is immediately available to apply to.
            results.append(job)

    logger.info(f"[Scorer] Pre-filter: {pre_filtered_count} auto-skipped instantly "
                f"({len(jobs) - pre_filtered_count - len(needs_llm)} left unscored, "
                f"{len(needs_llm)} queued for LLM)")
    if emit:
        if llm_enabled:
            emit("log", f"Pre-filtered {pre_filtered_count} obvious mismatches — "
                        f"{len(needs_llm)} jobs need AI scoring")
        else:
            emit("log", f"Pre-filtered {pre_filtered_count} obvious mismatches — "
                        f"LLM scoring is OFF, remaining jobs are ready to apply")

    if not needs_llm:
        return results

    # Phase 2: concurrent LLM scoring
    def _score_one(job):
        if cancel_event and cancel_event.is_set():
            return job, None
        scored = score_job(job, profile)
        return job, scored

    completed = 0
    # NOTE: deliberately NOT using `with ThreadPoolExecutor(...)` here.
    # The context manager's __exit__ calls shutdown(wait=True), which
    # blocks until every in-flight task finishes — on slow providers
    # (e.g. NVIDIA NIM, 60-90s/call) that meant "Cancel" could still
    # block for up to ~90s waiting for SCORING_WORKERS in-flight calls.
    # We shut down with wait=False + cancel_futures=True instead, so
    # cancellation returns immediately: not-yet-started tasks are
    # dropped, and any already-running calls finish quietly in the
    # background without us waiting for or using their results.
    executor = ThreadPoolExecutor(max_workers=SCORING_WORKERS)
    cancelled = False
    try:
        futures = {executor.submit(_score_one, job): job for job in needs_llm}

        for future in as_completed(futures):
            if cancel_event and cancel_event.is_set():
                logger.info("[Scorer] Cancellation requested — stopping batch "
                            "(in-flight calls will finish in the background)")
                cancelled = True
                break

            job, scored = future.result()
            completed += 1

            if scored is None:
                continue

            job["ai_score"]        = scored["score"]
            job["ai_score_reason"] = scored["reason"]
            job["auto_skip"]       = scored["auto_skip"]

            if jobs_db:
                jobs_db.update_score(job["job_id"], scored["score"], scored["reason"])
                if scored["auto_skip"]:
                    jobs_db.update_status(job["job_id"], "skipped",
                                          error=f"Auto-skip: score {scored['score']}/100")

            results.append(job)

            if emit and completed % 5 == 0:
                emit("log", f"Scored {completed}/{len(needs_llm)} jobs...")
    finally:
        if cancelled:
            executor.shutdown(wait=False, cancel_futures=True)
        else:
            executor.shutdown(wait=True)

>>>>>>> 4226f10 (.)

    skipped = sum(1 for j in results if j.get("auto_skip"))
    logger.info(f"[Scorer] Done. {len(results)-skipped} to apply, {skipped} auto-skipped.")
    return results


def _build_jd_text(job: dict) -> str:
    parts = [
        f"Title: {job.get('title', '')}",
        f"Company: {job.get('company', '')}",
        f"Location: {job.get('location', '')}",
    ]
    if job.get("salary"):
        parts.append(f"Salary: {job['salary']}")
    if job.get("description"):
<<<<<<< HEAD
        parts.append(f"\nDescription:\n{job['description'][:2000]}")
=======
        parts.append(f"\nDescription:\n{job['description'][:1500]}")
>>>>>>> 4226f10 (.)
    return "\n".join(parts)
