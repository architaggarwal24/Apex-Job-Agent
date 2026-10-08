"""
intelligence/ghostwriter.py — Per-job AI assistant (from JobOps Ghostwriter).

Context-aware chat: the LLM knows the full job description, the user's
profile, tailored resume, and conversation history.

Features:
  - cover_letter()     — draft a cover letter for this job
  - interview_prep()   — generate likely questions + suggested answers
  - chat()             — free-form chat with full job context
"""

import logging
from core import llm as LLM

logger = logging.getLogger(__name__)

SYSTEM = """You are a personal career assistant helping a candidate apply for a specific job.
You have full context about the job and the candidate.

Your capabilities:
- Write tailored cover letters
- Generate likely interview questions with suggested answers
- Help craft responses to screening questions
- Advise on salary negotiation
- Review and improve resume bullet points

Always be honest — if the candidate is underqualified, acknowledge it and
suggest how to position themselves. Keep responses concise and actionable."""


def build_context(job: dict, profile: dict, jobs_db=None) -> str:
    """Build the full context string passed to every chat message."""
    flat = profile.get("_flat", {})
    skills = profile.get("skills", [])

    job_detail = jobs_db.get_by_id(job["job_id"]) if jobs_db else job
    brief           = job_detail.get("brief", "")            if job_detail else ""
    tailored_summary = job_detail.get("tailored_summary", "") if job_detail else ""
    tailored_skills  = job_detail.get("tailored_skills", "")  if job_detail else ""

    ctx = f"""=== JOB ===
Title:    {job.get('title','')}
Company:  {job.get('company','')}
Location: {job.get('location','')}
URL:      {job.get('url','')}
{f'Brief: {brief}' if brief else ''}

Description:
{job.get('description','No description available')[:3000]}

=== CANDIDATE ===
Name:       {flat.get('full_name','')}
Role:       {flat.get('current_role','')}
Experience: {flat.get('experience_years','0')} years
Location:   {flat.get('location_city','')}, {flat.get('location_country','India')}
Education:  {flat.get('highest_degree','')} in {flat.get('degree_field','')} from {flat.get('university_name','')}

{f'Tailored Summary: {tailored_summary}' if tailored_summary else f'Summary: {flat.get("resume_summary","")}'}
Skills: {tailored_skills if tailored_skills else ', '.join(skills[:20])}

LinkedIn: {flat.get('linkedin_url','')}
GitHub:   {flat.get('github_url','')}
Portfolio: {flat.get('portfolio_url','')}"""

    return ctx.strip()


def cover_letter(job: dict, profile: dict, jobs_db=None,
                 tone: str = "professional") -> str:
    """
    Generate a tailored cover letter for this job.

    Args:
        tone: "professional" | "enthusiastic" | "concise"
    """
    ctx = build_context(job, profile, jobs_db)
    tone_guide = {
        "professional": "formal and professional tone",
        "enthusiastic": "warm, enthusiastic, and personable tone",
        "concise":      "brief and to-the-point, under 200 words",
    }.get(tone, "professional tone")

    try:
        result = LLM.chat(
            messages=[{"role": "user", "content":
                f"Write a cover letter for this job in a {tone_guide}. "
                f"Do not include address blocks or date. Start directly with the opening paragraph."}],
            system=SYSTEM + f"\n\nCONTEXT:\n{ctx}",
            max_tokens=600,
        )
        return result
    except Exception as e:
        logger.error(f"[Ghostwriter] Cover letter failed: {e}")
        return ""


def interview_prep(job: dict, profile: dict, jobs_db=None) -> dict:
    """
    Generate likely interview questions and suggested answers.

    Returns:
        {
            "technical": [{"question": "...", "answer": "..."}],
            "behavioural": [{"question": "...", "answer": "..."}],
            "company": [{"question": "...", "answer": "..."}],
        }
    """
    ctx = build_context(job, profile, jobs_db)

    PREP_SYSTEM = """You are an interview coach.
Given a job description and candidate profile, generate likely interview questions
with concise suggested answers tailored to this candidate's background.

Return ONLY valid JSON:
{
  "technical": [
    {"question": "...", "answer": "..."}
  ],
  "behavioural": [
    {"question": "...", "answer": "..."}
  ],
  "company": [
    {"question": "...", "answer": "..."}
  ],
  "questions_to_ask": ["Smart question to ask interviewer 1", "..."]
}

Generate 3-4 questions per category. Answers should be specific to the candidate."""

    try:
        result = LLM.chat_json(
            messages=[{"role": "user", "content": "Generate interview prep for this job."}],
            system=PREP_SYSTEM + f"\n\nCONTEXT:\n{ctx}",
            max_tokens=2000,
        )
        return result
    except Exception as e:
        logger.error(f"[Ghostwriter] Interview prep failed: {e}")
        return {"technical": [], "behavioural": [], "company": [], "questions_to_ask": []}


def chat(message: str, job: dict, profile: dict,
         history: list[dict], jobs_db=None) -> str:
    """
    Free-form chat with full job + profile context.

    Args:
        message:  User's current message
        history:  List of {"role": "user"/"assistant", "content": "..."} previous turns
        job:      Job dict
        profile:  ProfileDB.summary()
        jobs_db:  JobsDB instance for fetching stored brief/tailoring

    Returns:
        Assistant response string
    """
    ctx = build_context(job, profile, jobs_db)
    system = SYSTEM + f"\n\nCONTEXT:\n{ctx}"

    messages = history + [{"role": "user", "content": message}]

    try:
        return LLM.chat(messages=messages, system=system, max_tokens=800)
    except Exception as e:
        logger.error(f"[Ghostwriter] Chat failed: {e}")
        return "Sorry, I couldn't process that request. Please try again."
