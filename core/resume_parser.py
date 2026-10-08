"""
core/resume_parser.py — Parse resume PDF into structured profile dict.
Uses pdfplumber for text extraction, then the configured LLM to parse.
"""

import logging
import re
from pathlib import Path

from core.config import cfg
from core import llm as LLM

logger = logging.getLogger(__name__)

SYSTEM = """You are a resume parser. Extract ALL information from the resume text.
Return ONLY valid JSON — no markdown, no explanation.

Schema (use empty string "" or [] if not found):
{
  "full_name": "", "first_name": "", "last_name": "",
  "email": "", "phone": "",
  "location_city": "", "location_state": "", "location_country": "India",
  "linkedin_url": "", "github_url": "", "portfolio_url": "",
  "current_role": "", "experience_years": "0",
  "summary": "",
  "skills": [],
  "education": [{"degree":"","field":"","institution":"","year":"","score":""}],
  "experience": [{"title":"","company":"","duration":"","description":""}],
  "projects": [{"name":"","description":"","technologies":[]}],
  "certifications": [],
  "languages": [],
  "target_roles": []
}

Rules:
- experience_years: total professional experience as string ("0" for freshers/students)
- skills: flat list of all technical skills, tools, languages, frameworks
- target_roles: infer 5-8 likely job titles this person would apply for based on their background
- Split full_name into first_name and last_name
- Return ONLY JSON"""


def parse(pdf_path: Path | None = None) -> dict:
    pdf_path = pdf_path or cfg.RESUME_PATH

    if not pdf_path.exists():
        logger.warning(f"[ResumeParser] PDF not found: {pdf_path}")
        return _empty()

    logger.info(f"[ResumeParser] Extracting text from {pdf_path.name}")
    text = _extract_text(pdf_path)

    if not text.strip():
        logger.warning("[ResumeParser] No text extracted — PDF may be image-based")
        return _empty()

    logger.info(f"[ResumeParser] {len(text)} chars extracted — calling LLM")

    try:
        result = LLM.chat_json(
            messages=[{"role": "user", "content": f"Parse this resume:\n\n{text[:12000]}"}],
            system=SYSTEM,
            max_tokens=2048,
        )
        profile = _normalise(result)
        logger.info(f"[ResumeParser] Parsed: {profile.get('full_name','?')} | "
                    f"{len(profile.get('skills',[]))} skills | "
                    f"{len(profile.get('target_roles',[]))} target roles")
        return profile
    except Exception as e:
        logger.error(f"[ResumeParser] LLM parse failed: {e} — using regex fallback")
        return _regex_fallback(text)


def _extract_text(path: Path) -> str:
    try:
        import pdfplumber
        pages = []
        with pdfplumber.open(str(path)) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    pages.append(t)
        return "\n".join(pages)
    except ImportError:
        raise ImportError("Run: pip install pdfplumber")
    except Exception as e:
        logger.error(f"PDF extraction error: {e}")
        return ""


def _normalise(raw: dict) -> dict:
    base = _empty()
    base.update(raw)

    # Ensure lists
    for k in ("skills", "education", "experience", "projects",
              "certifications", "languages", "target_roles"):
        if not isinstance(base.get(k), list):
            base[k] = []

    # Derive first/last from full_name if missing
    if base.get("full_name") and not base.get("first_name"):
        parts = base["full_name"].strip().split()
        base["first_name"] = parts[0] if parts else ""
        base["last_name"]  = " ".join(parts[1:]) if len(parts) > 1 else ""

    base["experience_years"] = str(base.get("experience_years", "0"))
    return base


def _empty() -> dict:
    return {
        "full_name": "", "first_name": "", "last_name": "",
        "email": "", "phone": "",
        "location_city": "", "location_state": "", "location_country": "India",
        "linkedin_url": "", "github_url": "", "portfolio_url": "",
        "current_role": "", "experience_years": "0", "summary": "",
        "skills": [], "education": [], "experience": [], "projects": [],
        "certifications": [], "languages": [], "target_roles": [],
    }


def _regex_fallback(text: str) -> dict:
    p = _empty()
    m = re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text)
    if m: p["email"] = m.group(0)
    m = re.search(r"(?:\+91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}", text)
    if m: p["phone"] = m.group(0).strip()
    m = re.search(r"linkedin\.com/in/[\w-]+", text, re.I)
    if m: p["linkedin_url"] = "https://" + m.group(0)
    m = re.search(r"github\.com/[\w-]+", text, re.I)
    if m: p["github_url"] = "https://" + m.group(0)
    tech = ["python","javascript","typescript","java","react","node","django","fastapi",
            "flask","sql","postgresql","mongodb","docker","kubernetes","aws","gcp",
            "langchain","openai","llm","machine learning","pytorch","tensorflow"]
    p["skills"] = [t for t in tech if t in text.lower()]
    return p
