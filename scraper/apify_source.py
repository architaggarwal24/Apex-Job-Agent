# scraper/apify_source.py
"""
LinkedIn, Indeed and Naukri searched through an Apify Store actor - the
fallback engine for a board whose public search is blocked from your network.
Needs an Apify token (`apify_api_token`, free tier included) and runs the actor
named by `apify_actor_<board>`:

    linkedin   bebity~linkedin-jobs-scraper    (default; returns the employer's apply link)
    indeed     misceres~indeed-scraper         (default; returns the employer's apply link)
    naukri     srd836~naukri-jobs-scraper      (default; Naukri never exposes an apply link)

One call per title x location: `run-sync-get-dataset-items`, which runs the
actor and answers with its dataset in one request (up to 5 minutes). Each
actor has its own input and output names; the input is built per actor and the
output is read by trying the names these actors use, so a swapped actor of the
same kind still mostly works. The token travels only in the request to
api.apify.com and is never logged.

`post` is looked up at call time so tests can swap in a fake; nothing here
reaches the network on import.
"""

import logging
import re
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

SITES = ("linkedin", "indeed", "naukri")
DEFAULT_ACTORS = {"linkedin": "bebity~linkedin-jobs-scraper", "indeed": "misceres~indeed-scraper",
                  "naukri": "srd836~naukri-jobs-scraper"}
ACTOR_RX = re.compile(r"^[A-Za-z0-9_.-]+[~/][A-Za-z0-9_.-]+$")
RUN_TIMEOUT = 300
_INDEED_CC = {"india": "IN", "usa": "US", "united states": "US", "uk": "GB", "united kingdom": "GB", "canada": "CA",
              "australia": "AU", "germany": "DE", "singapore": "SG", "france": "FR", "netherlands": "NL"}


class ApifyError(Exception):
    pass


def clean_actor(value) -> str:
    v = str(value or "").strip().replace("/", "~")
    if not ACTOR_RX.match(v):
        raise ValueError("an Apify actor is written owner~name, e.g. bebity~linkedin-jobs-scraper")
    return v


def _first(row: dict, *names):
    for n in names:
        v = row.get(n)
        if v not in (None, "", [], {}):
            return v
    return ""


def _text(v) -> str:
    return str(v).strip() if v is not None else ""


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def _same_board(site: str, url: str) -> bool:
    return {"linkedin": "linkedin.", "indeed": "indeed.", "naukri": "naukri."}[site] in _host(url)


def actor_input(site: str, actor: str, title: str, location: str, results: int, hours_old: int, country: str) -> dict:
    """The run input the named actor expects."""
    if site == "linkedin":
        when = "r86400" if hours_old and hours_old <= 24 else "r604800" if hours_old and hours_old <= 168 else "r2592000"
        return {"title": title, "location": location, "rows": results, "publishedAt": when}
    if site == "indeed":
        return {"position": title, "location": location, "country": _INDEED_CC.get((country or "India").lower(), "IN"),
                "maxItemsPerSearch": results, "parseCompanyDetails": False, "saveOnlyUniqueItems": True}
    freshness = 1 if hours_old and hours_old <= 24 else 3 if hours_old and hours_old <= 72 else 7 if hours_old and hours_old <= 168 else 30
    return {"keywords": title, "keyword": title, "location": location, "freshness": freshness, "maxJobs": results, "maxItems": max(20, results)}


def normalize(site: str, row: dict) -> dict | None:
    """One dataset item -> the job dict tracking/pipeline inserts (None to drop)."""
    if not isinstance(row, dict):
        return None
    url = _text(_first(row, "jobUrl", "link", "url", "job_url", "portalUrl", "jdURL", "jdUrl"))
    title = _text(_first(row, "title", "positionName", "jobTitle", "position", "name"))
    company = _text(_first(row, "companyName", "company", "companyname", "employer"))
    if isinstance(_first(row, "company"), dict):
        company = _text(_first(row, "company").get("name"))
    if not (url.startswith("http") and title and company):
        return None
    direct = _text(_first(row, "applyUrl", "externalApplyLink", "apply_url", "applyLink"))
    apply_url = direct if direct.startswith("http") and direct != url and not _same_board(site, direct) else ""
    desc = _text(_first(row, "descriptionText", "description", "descriptionHtml", "description_full", "descriptionPlain",
                        "jobDescription", "jdSnippet"))
    sal = _first(row, "salary", "salaryText")
    salary = _text(sal.get("salaryText") if isinstance(sal, dict) else sal)
    loc = _first(row, "location", "jobLocation", "locations")
    if isinstance(loc, list):
        loc = ", ".join(_text(x.get("name") if isinstance(x, dict) else x) for x in loc)
    elif isinstance(loc, dict):
        loc = _text(loc.get("formattedAddressLong") or loc.get("formattedAddressShort") or loc.get("name"))
    return {"title": title[:300], "company": company[:200], "location": _text(loc)[:200], "url": url.split("?")[0] if site == "linkedin" else url,
            "apply_url": apply_url, "source": site, "description": desc[:20000], "salary": salary[:100],
            "seniority": _text(_first(row, "experienceLevel", "seniorityLevel", "experience"))[:60], "applicants": "",
            "posted_at": _text(_first(row, "postedAt", "postedTime", "postedDate", "publishedAt", "created_at"))[:10]}


def run_actor(token: str, actor: str, run_input: dict, post=None) -> list[dict]:
    """POST the input to run-sync-get-dataset-items; the dataset items back."""
    if post is None:
        import requests

        def post(url, params, body):
            try:
                r = requests.post(url, params=params, json=body, timeout=RUN_TIMEOUT + 30)
            except requests.RequestException as e:
                raise ApifyError(f"{type(e).__name__}: {e}") from None
            if r.status_code == 401:
                raise ApifyError("Apify refused the token (401): check apify_api_token in Settings")
            if r.status_code == 404:
                raise ApifyError(f"Apify actor {actor} not found (404): check apify_actor_* in Settings")
            if r.status_code >= 400:
                raise ApifyError(f"Apify HTTP {r.status_code}: {r.text[:200]}")
            try:
                return r.json()
            except ValueError:
                raise ApifyError("Apify answered with something that is not JSON") from None
    data = post(f"https://api.apify.com/v2/acts/{actor}/run-sync-get-dataset-items",
                {"token": token, "timeout": RUN_TIMEOUT, "format": "json", "clean": "true"}, run_input)
    if isinstance(data, dict) and "error" in data:
        raise ApifyError(f"Apify: {(data['error'] or {}).get('message') or data['error']}")
    return data if isinstance(data, list) else []


def search(site: str, profile: dict, job_titles: list[str], locations: list[str], *, token: str, actor: str,
           results_per_query: int = 25, hours_old: int = 168, post=None, on_query=None) -> list[dict]:
    if site not in SITES:
        raise ValueError(f"unknown site {site!r}; one of {', '.join(SITES)}")
    if not token:
        raise ApifyError("no Apify token: set apify_api_token in Settings → API keys, or pick another engine")
    actor = clean_actor(actor or DEFAULT_ACTORS[site])
    country = ((profile.get("_flat") or {}).get("location_country") or "India").strip() or "India"
    from scraper.jobspy_source import allowed_countries, is_foreign, query_location
    allowed = allowed_countries(country, locations)    # a bare "Delhi" names the country; foreign non-remote results go
    results, seen = [], set()
    for title in job_titles:
        for location in locations:
            where = location if location.strip().lower() == "remote" else query_location(location, country)[0]
            logger.info(f"  [{site}/apify] '{title}' in '{where}'")
            try:
                rows = run_actor(token, actor, actor_input(site, actor, title, where, results_per_query, hours_old, country), post)
            except ApifyError as e:
                logger.warning(f"  [{site}/apify] '{title}' in '{location}' failed: {e}")
                if on_query:
                    on_query(title, location, 0)
                continue
            before = len(results)
            for row in rows:
                j = normalize(site, row)
                if j and is_foreign(j["location"], allowed):
                    continue
                if j and j["url"] not in seen:
                    seen.add(j["url"]); results.append(j)
            if on_query:
                on_query(title, location, len(results) - before)
    logger.info(f"  [{site}/apify] Total: {len(results)} jobs")
    return results
