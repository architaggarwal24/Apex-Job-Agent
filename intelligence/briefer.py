"""
intelligence/briefer.py — LLM job brief + resume tailoring.

For each job generates:
  1. brief       — 3-sentence plain-English summary of what the role is
  2. tailored_summary — rewrites the user's resume summary to match the JD
  3. tailored_skills  — reorders/emphasises skills relevant to this job
"""

import logging
from core import llm as LLM

logger = logging.getLogger(__name__)

BRIEF_SYSTEM = """You are a career advisor summarising job listings.
Given a job description, return ONLY valid JSON:
{
  "brief": "3-sentence plain English summary. What is the company? What will you do? What's required?",
  "key_requirements": ["req1", "req2", "req3", "req4", "req5"],
  "tech_stack": ["tech1", "tech2"],
  "seniority": "fresher|junior|mid|senior|lead|manager",
  "remote_friendly": true
}
No markdown. No explanation."""

TAILOR_SYSTEM = """You are a professional resume writer.
Given a job description and a candidate's current resume summary + skills,
rewrite them to better match the job. Keep it honest — don't add skills they don't have.

Return ONLY valid JSON:
{
  "tailored_summary": "2-3 sentence professional summary tailored to this specific role",
  "tailored_skills": ["most relevant skill 1", "most relevant skill 2", "...up to 12 skills"]
}
No markdown. No explanation."""


def generate_brief(job: dict) -> dict:
    """Generate a job brief for a single job."""
    jd = _jd_text(job)
    try:
        result = LLM.chat_json(
            messages=[{"role": "user", "content": f"Summarise this job:\n\n{jd}"}],
            system=BRIEF_SYSTEM,
            max_tokens=512,
        )
        return {
            "brief":            result.get("brief", ""),
            "key_requirements": result.get("key_requirements", []),
            "tech_stack":       result.get("tech_stack", []),
            "seniority":        result.get("seniority", ""),
            "remote_friendly":  result.get("remote_friendly", False),
        }
    except Exception as e:
        logger.warning(f"[Briefer] Brief failed for {job.get('title','?')}: {e}")
        return {"brief": "", "key_requirements": [], "tech_stack": [],
                "seniority": "", "remote_friendly": False}


def tailor_resume(job: dict, profile: dict) -> dict:
    """
    Rewrite resume summary + skills list to match this specific job.
    Returns {tailored_summary, tailored_skills}.
    """
    flat      = profile.get("_flat", {})
    skills    = profile.get("skills", [])
    summary   = flat.get("resume_summary", "") or flat.get("cover_letter", "")
    jd        = _jd_text(job)

    candidate = f"""
Current summary: {summary[:500] if summary else 'Not provided'}
Skills: {', '.join(skills[:30]) if skills else 'Not provided'}
Role: {flat.get('current_role', '')}
Experience: {flat.get('experience_years', '0')} years
""".strip()

    try:
        result = LLM.chat_json(
            messages=[{"role": "user", "content":
                f"JOB:\n{jd}\n\nCANDIDATE:\n{candidate}"}],
            system=TAILOR_SYSTEM,
            max_tokens=512,
        )
        return {
            "tailored_summary": result.get("tailored_summary", summary),
            "tailored_skills":  result.get("tailored_skills", skills[:12]),
        }
    except Exception as e:
        logger.warning(f"[Briefer] Tailor failed for {job.get('title','?')}: {e}")
        return {"tailored_summary": summary, "tailored_skills": skills[:12]}


def generate_and_save(job: dict, profile: dict, jobs_db) -> dict:
    """Generate brief + tailoring for a job and save to DB."""
    logger.info(f"  [Briefer] Generating brief for {job.get('title','?')} @ {job.get('company','?')}")

    brief_data   = generate_brief(job)
    tailor_data  = tailor_resume(job, profile)

    jobs_db.save_brief(
        job_id=          job["job_id"],
        brief=           brief_data["brief"],
        tailored_summary=tailor_data["tailored_summary"],
        tailored_skills= ", ".join(tailor_data["tailored_skills"]),
    )

    return {**brief_data, **tailor_data}


def _jd_text(job: dict) -> str:
    parts = [
        f"Title: {job.get('title','')}",
        f"Company: {job.get('company','')}",
        f"Location: {job.get('location','')}",
    ]
    if job.get("salary"):
        parts.append(f"Salary: {job['salary']}")
    if job.get("description"):
        parts.append(f"\nDescription:\n{job['description'][:3000]}")
    return "\n".join(parts)
