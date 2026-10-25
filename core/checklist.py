# core/checklist.py
"""
core/checklist.py — the "get started" checklist on the Today page. It replaces the old
first-run /setup page (templates/setup.html), which the Dashboard used to redirect to; the
Today page shows this list until every item is done.

    resume        a resume is on file: resume.pdf (the owner's, from the profile upload) or
                  one of this user's resume variants (db/resumes_db.py)
    profile       the profile's required details are filled in (name, e-mail, phone -
                  ProfileDB.is_complete, what setup.html's step 2 asked for)
    search_prefs  job titles AND locations to search for are saved (setup.html steps 3-4)
    llm           an LLM provider was tested successfully (Settings > Providers, "Test")
    login         a job site has a saved, unexpired login session (Settings > Job Site Sessions)
    search        a job search has run: one finished at least once, or there are jobs already

Two of these leave no trace on their own, so they are RECORDED when they happen, as
settings (written by the server, never through the generic settings save):

    setup_llm_tested   {"provider", "model", "at"} - by POST /api/settings/test-llm on success
    setup_search_run   the time a search pipeline finished - by /api/pipeline/run and main.py

build() is plain Python over what it is given (settings, profile, resume, cookie status...), so
it tests without a server.
"""

import json
from datetime import datetime

LLM_TESTED_KEY = "setup_llm_tested"
SEARCH_RUN_KEY = "setup_search_run"


def _now(now=None) -> str:
    return (now or datetime.now()).isoformat(timespec="seconds")


def mark_llm_tested(settings, provider: str, model: str, now=None) -> None:
    settings.write(LLM_TESTED_KEY, json.dumps({"provider": provider, "model": model, "at": _now(now)}))


def mark_search_run(settings, now=None) -> None:
    settings.write(SEARCH_RUN_KEY, _now(now))


def llm_tested(settings) -> dict | None:
    try:
        data = json.loads(settings.read(LLM_TESTED_KEY, "") or "null")
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("at") else None


def _json_list(value) -> list:
    try:
        data = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return [x for x in data if isinstance(x, str) and x.strip()] if isinstance(data, list) else []


def build(*, settings, profile_flat: dict, profile_missing: list, resume_file: bool, resume_variants: int,
          logged_in: dict, job_count: int) -> dict:
    """{"items": [{key, label, done, detail, href}], "complete": bool, "done": n, "total": n}.
    `logged_in` is {platform: bool}; `profile_missing` the required profile fields still empty."""
    tested = llm_tested(settings)
    titles, places = _json_list(profile_flat.get("search_positions")), _json_list(profile_flat.get("search_locations"))
    sites = sorted(p for p, ok in logged_in.items() if ok)
    searched = settings.read(SEARCH_RUN_KEY, "") or ""
    items = [
        {"key": "resume", "label": "Upload your resume", "href": "/profile",
         "done": bool(resume_file or resume_variants),
         "detail": ("resume on file" if resume_file else "") + (f"{', ' if resume_file else ''}{resume_variants} resume variant(s)" if resume_variants else "")},
        {"key": "profile", "label": "Fill in your details (name, e-mail, phone)", "href": "/profile",
         "done": not profile_missing, "detail": f"missing: {', '.join(profile_missing)}" if profile_missing else "complete"},
        {"key": "search_prefs", "label": "Choose job titles and locations to search", "href": "",
         "done": bool(titles and places),
         "detail": f"{len(titles)} title(s), {len(places)} location(s)", "titles": titles, "locations": places},
        {"key": "llm", "label": "Test an LLM provider", "href": "/settings",
         "done": tested is not None,
         "detail": f"{tested['provider']} / {tested['model']} worked on {tested['at'][:10]}" if tested else "not tested yet"},
        {"key": "login", "label": "Log in to a job site", "href": "/settings",
         "done": bool(sites), "detail": f"logged in: {', '.join(sites)}" if sites else "no saved login session"},
        {"key": "search", "label": "Run your first job search", "href": "",
         "done": bool(searched) or job_count > 0,
         "detail": (f"last finished {searched[:16].replace('T', ' ')}" if searched else "") or
                   (f"{job_count} job(s) found so far" if job_count else "no search has run yet")},
    ]
    done = sum(1 for i in items if i["done"])
    return {"items": items, "done": done, "total": len(items), "complete": done == len(items)}
