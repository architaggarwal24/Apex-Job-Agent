# intelligence/fit.py
"""
intelligence/fit.py — the instant "quick match" score: how well a job fits the
profile, from words alone. No LLM, no network, so EVERY saved job gets one the
moment it is found, and the Inbox can be sorted by fit even with AI scoring off.

It is kept apart from the AI score (jobs.fit_score vs jobs.ai_score): the
quality gate, auto-skip and autonomy keep using the AI score only, because a
keyword match is a sorting aid, not a judgement good enough to act on.

  title     45  the best of your search titles (and current role): the share of
                its meaningful words found in the job title ("AI Engineer" vs
                "Senior AI/ML Engineer" = all of them)
  skills    35  your resume skills found in the title + description (up to 10
                needed for full marks). Unknown when the job has no description
                or you have no skills saved: the other parts are then rescaled.
  level     10  seniority words in the title against your years of experience
  location  10  remote / one of your search places / elsewhere

quick_fit(job, profile) -> {"score": 0-100, "reason": "..."} or None when the
profile has neither titles nor skills (nothing to compare with).
"""

import re

_WORD_RX = re.compile(r"[a-z0-9+#]+(?:\.[a-z0-9]+)*")
# words that say nothing about the role on their own (seniority is scored separately)
_NOISE = {"senior", "junior", "lead", "staff", "principal", "sr", "jr", "ii", "iii", "iv", "i", "the", "a", "an",
          "of", "and", "or", "for", "to", "in", "at", "with", "remote", "hybrid", "onsite", "contract", "intern",
          "internship", "fresher", "freshers", "new", "grad", "graduate", "entry", "level", "mid", "experienced",
          "m", "f", "d", "w", "x", "full", "time", "part", "permanent", "temporary", "job", "role"}
# one spelling per idea, so "ML Developer" and "Machine Learning Engineer" compare
_SAME = {"developer": "engineer", "dev": "engineer", "engineering": "engineer", "programmer": "engineer",
         "swe": "engineer", "sde": "engineer", "ml": "machinelearning", "ai": "ai", "artificial": "ai",
         "frontend": "frontend", "front": "frontend", "backend": "backend", "back": "backend",
         "fullstack": "fullstack", "full-stack": "fullstack", "analytics": "analyst", "scientist": "science",
         "js": "javascript", "golang": "go", "k8s": "kubernetes", "postgres": "postgresql"}
_PHRASES = (("machine learning", "machinelearning"), ("artificial intelligence", "ai"), ("full stack", "fullstack"),
            ("front end", "frontend"), ("back end", "backend"))

_SENIOR_RX = re.compile(r"\b(senior|sr\.?|staff|principal|lead|manager|director|head of|vp|vice president|architect)\b", re.I)
_JUNIOR_RX = re.compile(r"\b(intern|internship|junior|jr\.?|fresher|entry[ -]level|graduate|trainee|associate|apprentice)\b", re.I)
_REMOTE_RX = re.compile(r"\b(remote|anywhere|work from home|wfh|distributed)\b", re.I)
_ALIASES = (("bangalore", "bengaluru"), ("gurgaon", "gurugram"), ("bombay", "mumbai"), ("calcutta", "kolkata"),
            ("madras", "chennai"), ("new delhi", "delhi"), ("delhi ncr", "delhi"), ("trivandrum", "thiruvananthapuram"),
            ("cochin", "kochi"), ("poona", "pune"))


def _norm_words(text: str) -> list[str]:
    t = " " + (text or "").lower().replace("/", " ").replace("-", " ") + " "
    for a, b in _PHRASES:
        t = t.replace(f" {a} ", f" {b} ")
    return [_SAME.get(w, w) for w in _WORD_RX.findall(t)]


def _title_part(job_title: str, wanted: list[str]) -> tuple[float, str]:
    have = set(_norm_words(job_title))
    best, best_title = 0.0, ""
    for w in wanted:
        need = [x for x in dict.fromkeys(_norm_words(w)) if x not in _NOISE]
        if not need:
            continue
        frac = sum(1 for x in need if x in have) / len(need)
        if frac > best:
            best, best_title = frac, w
    return best, best_title


def _skill_rx(skill: str):
    s = skill.strip().lower()
    if len(s) < 2 and s not in ("c", "r"):
        return None
    # word-ish boundaries that still work for c++, c#, .net, node.js
    return re.compile(r"(?<![a-z0-9+#.])" + re.escape(s) + r"(?![a-z0-9+#])")


def _skills_part(text: str, skills: list[str]) -> tuple[float | None, list[str]]:
    skills = [s for s in dict.fromkeys(str(x).strip() for x in skills or []) if s]
    if not skills or not text.strip():
        return None, []
    low = text.lower()
    hits = [s for s in skills if (rx := _skill_rx(s)) and rx.search(low)]
    return min(1.0, len(hits) / min(len(skills), 10)), hits


def _level_part(job_title: str, years: float) -> float:
    senior = bool(_SENIOR_RX.search(job_title or ""))
    junior = bool(_JUNIOR_RX.search(job_title or ""))
    if years <= 1:
        return 1.0 if junior else (0.0 if senior else 0.6)
    if years >= 5:
        return 1.0 if senior else (0.2 if junior else 0.7)
    return 0.5 if senior else (0.6 if junior else 0.9)


def _spellings(place: str) -> set[str]:
    p = place.strip().lower()
    out = {p}
    for a, b in _ALIASES:
        if p == a: out.add(b)
        if p == b: out.add(a)
    return out


def _location_part(location: str, places: list[str]) -> tuple[float, str]:
    loc = (location or "").strip().lower()
    wants_remote = any(p.strip().lower() == "remote" for p in places)
    if _REMOTE_RX.search(loc):
        return (1.0, "remote") if wants_remote else (0.6, "remote")
    if not loc:
        return 0.6, ""
    for p in places:
        if p.strip() and p.strip().lower() != "remote" and any(s in loc for s in _spellings(p)):
            return 1.0, p.strip()
    return 0.3, ""


def quick_fit(job: dict, profile: dict) -> dict | None:
    flat = profile.get("_flat") or {}
    titles = [t for t in (profile.get("search_positions") or []) if str(t).strip()]
    if str(flat.get("current_role") or "").strip():
        titles.append(flat["current_role"])
    skills = profile.get("skills") or []
    if not titles and not skills:
        return None
    try:
        years = float(flat.get("experience_years") or 0)
    except (TypeError, ValueError):
        years = 0.0
    title = job.get("title") or ""
    text = f"{title}\n{job.get('description') or ''}"

    t_frac, t_match = _title_part(title, titles) if titles else (None, "")
    s_frac, hits = _skills_part(text if (job.get("description") or "").strip() else "", skills)
    lvl = _level_part(title, years)
    loc, where = _location_part(job.get("location") or "", profile.get("search_locations") or [])

    parts = [(45, t_frac), (35, s_frac), (10, lvl), (10, loc)]
    weight = sum(w for w, v in parts if v is not None)
    score = round(sum(w * v for w, v in parts if v is not None) * 100 / weight) if weight else 0

    why = []
    if t_frac is not None:
        why.append(f"title matches “{t_match}”" if t_frac >= 0.99 else
                   f"title partly matches “{t_match}”" if t_frac > 0 else "title matches none of your search titles")
    if s_frac is not None:
        shown = ", ".join(hits[:5]) + ("…" if len(hits) > 5 else "")
        why.append(f"{len(hits)} of your skills mentioned ({shown})" if hits else "none of your skills mentioned")
    elif skills:
        why.append("no description to check skills against")
    if where:
        why.append(where if where == "remote" else f"in {where}")
    return {"score": max(0, min(100, score)), "reason": "Quick match (keywords): " + "; ".join(why) + "."}
