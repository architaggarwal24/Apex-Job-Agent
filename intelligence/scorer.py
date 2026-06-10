"""
intelligence/scorer.py — LLM-powered job scoring.

Scores each job 0-100 against the user's profile with a plain-English
reason. Inspired by JobOps's scorer but extended with:
  - Explicit scoring criteria breakdown
  - Auto-skip for jobs below threshold
  - Batch scoring to minimise API calls
"""

import logging
from core import llm as LLM
from core.config import cfg

logger = logging.getLogger(__name__)

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
        if job.get("ai_score") is not None:
            results.append(job)
            continue

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
        parts.append(f"\nDescription:\n{job['description'][:2000]}")
    return "\n".join(parts)
