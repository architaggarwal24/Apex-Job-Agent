# scraper/sources/aggregators.py
"""
Job boards with a public, keyless feed - no account, no browser, and the
openings link out to the employer's own form.

  himalayas    search API (keyword), remote and country-tagged jobs; `applicationLink`
               is the employer's form
  jobicy       remote jobs, keyword tag + geo
  remotive     remote jobs, keyword search (≤ a few calls a day: throttled by min_interval)
  remoteok     the newest ~100 remote jobs (no search: filtered here)
  wwr          We Work Remotely RSS, programming categories (no search: filtered here)
  hn_hiring    the month's "Ask HN: Who is hiring?" thread through Algolia (free text:
               filtered here; the comment's first link is the apply form)
  arbeitnow    EU-leaning board with visa-sponsorship jobs (no search: filtered here)

With a free key saved in Settings (ctx.keys):
  adzuna       Adzuna's search API for the profile's country (India: "in"); snippets only,
               `redirect_url` leads to the source board. Free tier: 250 calls a day.
  jsearch      Google-for-Jobs through RapidAPI (JSearch): the widest India coverage,
               incl. Naukri/LinkedIn-sourced postings; `apply_options` often lists the
               company's own ATS form. Free tier: 200 calls a month.
  himalayas    works without a key; with one it gets its own rate limit.

Each `search(ctx)` returns job dicts (scraper/sources/base.job). `ctx` has
titles, locations, fetch (a base.Fetcher or a fake), country, keys and hours_old.
"""

import re
import xml.etree.ElementTree as ET

from scraper.sources.ats import parse_url
from scraper.sources.base import SourceError, clean, html_to_text, iso_date, job, keep, salary_text


def _dedup(jobs):
    seen, out = set(), []
    for j in jobs:
        if j and j["url"] not in seen:
            seen.add(j["url"]); out.append(j)
    return out


# ── Himalayas ─────────────────────────────────────────────────────

def himalayas(ctx) -> list[dict]:
    out = []
    key = (ctx.keys or {}).get("himalayas_api_key") or ""
    headers = {"Authorization": f"Bearer {key}"} if key else None
    for title in ctx.titles:
        data = ctx.fetch.get_json("https://himalayas.app/jobs/api/search",
                                  params={"q": title, "worldwide": "true", "limit": 50}, headers=headers)
        for it in (data.get("jobs") or []) if isinstance(data, dict) else []:
            link = clean(it.get("applicationLink"))
            url = clean(it.get("url")) or clean(it.get("guid")) or link
            restrictions = it.get("locationRestrictions") or []
            loc = ", ".join(clean(x) for x in restrictions) if isinstance(restrictions, list) else clean(restrictions)
            j = job(source="himalayas", title=it.get("title"), company=it.get("companyName"), url=url, apply_url=link,
                    location=loc or "Remote", description=it.get("description") or it.get("excerpt"),
                    salary=salary_text(it.get("minSalary"), it.get("maxSalary"), it.get("currency"), it.get("salaryPeriod")),
                    posted_at=it.get("pubDate"))
            if keep(j, ctx.titles, ctx.locations, is_remote=True, check_title=False):
                out.append(j)
    return _dedup(out)


# ── Jobicy ────────────────────────────────────────────────────────

def jobicy(ctx) -> list[dict]:
    out = []
    for title in ctx.titles:
        tag = re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()[:50]
        if len(tag) < 3:
            continue
        data = ctx.fetch.get_json("https://jobicy.com/api/v2/remote-jobs", params={"count": 50, "tag": tag})
        for it in (data.get("jobs") or []) if isinstance(data, dict) else []:
            j = job(source="jobicy", title=it.get("jobTitle"), company=it.get("companyName"), url=it.get("url"),
                    location=clean(it.get("jobGeo")) or "Remote", description=it.get("jobDescription") or it.get("jobExcerpt"),
                    salary=salary_text(it.get("salaryMin"), it.get("salaryMax"), it.get("salaryCurrency"), it.get("salaryPeriod")),
                    posted_at=it.get("pubDate"))
            if keep(j, ctx.titles, ctx.locations, is_remote=True, check_title=False):
                out.append(j)
    return _dedup(out)


# ── Remotive ──────────────────────────────────────────────────────

def remotive(ctx) -> list[dict]:
    out = []
    for title in ctx.titles:
        data = ctx.fetch.get_json("https://remotive.com/api/remote-jobs", params={"search": title, "limit": 100})
        for it in (data.get("jobs") or []) if isinstance(data, dict) else []:
            j = job(source="remotive", title=it.get("title"), company=it.get("company_name"), url=it.get("url"),
                    location=clean(it.get("candidate_required_location")) or "Remote", description=it.get("description"),
                    salary=it.get("salary"), posted_at=it.get("publication_date"))
            if keep(j, ctx.titles, ctx.locations, is_remote=True, check_title=False):
                out.append(j)
    return _dedup(out)


# ── Remote OK ─────────────────────────────────────────────────────

def remoteok(ctx) -> list[dict]:
    data = ctx.fetch.get_json("https://remoteok.com/api")
    out = []
    for it in data if isinstance(data, list) else []:
        if not isinstance(it, dict) or not it.get("position"):
            continue                                  # index 0 is the legal notice
        url = clean(it.get("url"))
        apply_url = clean(it.get("apply_url"))
        if apply_url and "remoteok.com" in apply_url:
            apply_url = ""                            # a bounce through their page, not the form
        j = job(source="remoteok", title=it.get("position"), company=it.get("company"), url=url, apply_url=apply_url,
                location=clean(it.get("location")) or "Remote", description=it.get("description"),
                salary=salary_text(it.get("salary_min"), it.get("salary_max"), "USD", "year"), posted_at=it.get("date"))
        if keep(j, ctx.titles, ctx.locations, is_remote=True):
            out.append(j)
    return _dedup(out)


# ── We Work Remotely (RSS) ────────────────────────────────────────

WWR_FEEDS = ("https://weworkremotely.com/categories/remote-programming-jobs.rss",
             "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
             "https://weworkremotely.com/categories/remote-product-jobs.rss")


def _rss_items(text: str):
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise SourceError(f"bad RSS: {e}") from None
    for item in root.iter("item"):
        yield {c.tag.split("}")[-1]: (c.text or "") for c in item}


def wwr(ctx) -> list[dict]:
    out = []
    for feed in WWR_FEEDS:
        for it in _rss_items(ctx.fetch.get_text(feed)):
            raw = clean(it.get("title"))
            company, _, title = raw.partition(": ")
            if not title:
                company, title = "", raw
            j = job(source="wwr", title=title, company=company or "Unknown", url=it.get("link"),
                    location=clean(it.get("region")) or "Remote", description=it.get("description"), posted_at=it.get("pubDate"))
            if keep(j, ctx.titles, ctx.locations, is_remote=True):
                out.append(j)
    return _dedup(out)


# ── Hacker News "Who is hiring?" ──────────────────────────────────

_LINK_RX = re.compile(r"https?://[^\s<>\"')\]]+")
_HN_REMOTE_RX = re.compile(r"\bremote\b", re.I)


def hn_hiring(ctx) -> list[dict]:
    found = ctx.fetch.get_json("https://hn.algolia.com/api/v1/search",
                               params={"query": "Ask HN: Who is hiring?", "tags": "ask_hn,author_whoishiring", "hitsPerPage": 3})
    hits = [h for h in (found.get("hits") or []) if "who is hiring" in clean(h.get("title")).lower()] if isinstance(found, dict) else []
    if not hits:
        return []
    story = hits[0]
    story_id = clean(story.get("objectID"))
    out = []
    for page in range(0, 4):                        # 4 x 1000 comments is more than a thread has
        data = ctx.fetch.get_json("https://hn.algolia.com/api/v1/search_by_date",
                                  params={"tags": f"comment,story_{story_id}", "hitsPerPage": 1000, "page": page})
        comments = (data.get("hits") or []) if isinstance(data, dict) else []
        for c in comments:
            if clean(c.get("parent_id")) != story_id:
                continue                              # replies, not postings
            text = html_to_text(c.get("comment_text") or "")
            if not text:
                continue
            first = text.split("\n", 1)[0]
            parts = [p.strip() for p in re.split(r"\s\|\s", first) if p.strip()]
            company = parts[0][:120] if parts else "Unknown"
            title = next((p for p in parts[1:] if any(w in p.lower() for w in ("engineer", "developer", "scientist",
                          "manager", "designer", "architect", "lead", "founder"))), "")
            if not title:
                title = next((w for w in ctx.titles if w.lower() in text.lower()), "")
            if not title:
                continue
            links = _LINK_RX.findall(c.get("comment_text") or "")
            links = [html_to_text(l) for l in links]
            j = job(source="hn_hiring", title=title[:120], company=company,
                    url=f"https://news.ycombinator.com/item?id={clean(c.get('objectID'))}",
                    apply_url=links[0] if links else "", location="Remote" if _HN_REMOTE_RX.search(first) else first[:200],
                    description=text, posted_at=c.get("created_at"))
            if j is None or not any(w.lower() in text.lower() or
                                    all(t in text.lower() for t in w.lower().split()) for w in ctx.titles):
                continue
            if keep(j, ctx.titles, ctx.locations, is_remote=bool(_HN_REMOTE_RX.search(first)), check_title=False):
                out.append(j)
        if len(comments) < 1000:
            break
    return _dedup(out)


# ── Arbeitnow ─────────────────────────────────────────────────────

def arbeitnow(ctx) -> list[dict]:
    out = []
    for page in (1, 2, 3):
        data = ctx.fetch.get_json("https://www.arbeitnow.com/api/job-board-api", params={"page": page})
        items = (data.get("data") or []) if isinstance(data, dict) else []
        for it in items:
            j = job(source="arbeitnow", title=it.get("title"), company=it.get("company_name"), url=it.get("url"),
                    location=clean(it.get("location")), description=it.get("description"), posted_at=it.get("created_at"))
            if keep(j, ctx.titles, ctx.locations, is_remote=bool(it.get("remote"))):
                out.append(j)
        if not items:
            break
    return _dedup(out)


# ── Adzuna (key) ──────────────────────────────────────────────────

ADZUNA_COUNTRIES = {"india": "in", "united states": "us", "usa": "us", "united kingdom": "gb", "uk": "gb",
                    "australia": "au", "canada": "ca", "germany": "de", "france": "fr", "singapore": "sg",
                    "netherlands": "nl", "new zealand": "nz", "south africa": "za", "brazil": "br", "italy": "it",
                    "poland": "pl", "spain": "es", "mexico": "mx", "austria": "at", "belgium": "be", "switzerland": "ch"}


def adzuna(ctx) -> list[dict]:
    app_id, key = (ctx.keys or {}).get("adzuna_app_id") or "", (ctx.keys or {}).get("adzuna_api_key") or ""
    cc = ADZUNA_COUNTRIES.get((ctx.country or "India").strip().lower(), "in")
    days = max(1, (ctx.hours_old or 168) // 24) if ctx.hours_old else 30
    out = []
    for title in ctx.titles:
        for loc in ctx.locations:
            params = {"app_id": app_id, "app_key": key, "what": title, "results_per_page": 50,
                      "max_days_old": days, "content-type": "application/json"}
            if loc.strip().lower() != "remote":
                params["where"] = loc
            else:
                params["what_and"] = f"{title} remote"
            data = ctx.fetch.get_json(f"https://api.adzuna.com/v1/api/jobs/{cc}/search/1", params=params)
            for it in (data.get("results") or []) if isinstance(data, dict) else []:
                j = job(source="adzuna", title=it.get("title"), company=clean((it.get("company") or {}).get("display_name")),
                        url=it.get("redirect_url"), location=clean((it.get("location") or {}).get("display_name")),
                        description=it.get("description"), posted_at=it.get("created"),
                        salary=salary_text(it.get("salary_min"), it.get("salary_max"), "", "year") if not it.get("salary_is_predicted") == "1" else "")
                if keep(j, ctx.titles, ctx.locations, check_title=False):
                    out.append(j)
    return _dedup(out)


# ── JSearch (RapidAPI key) ────────────────────────────────────────

def jsearch(ctx) -> list[dict]:
    key = (ctx.keys or {}).get("jsearch_api_key") or ""
    headers = {"X-RapidAPI-Key": key, "X-RapidAPI-Host": "jsearch.p.rapidapi.com"}
    posted = "today" if (ctx.hours_old or 0) and ctx.hours_old <= 24 else "3days" if (ctx.hours_old or 0) and ctx.hours_old <= 72 \
        else "week" if (ctx.hours_old or 0) and ctx.hours_old <= 168 else "month"
    out = []
    for title in ctx.titles:
        for loc in ctx.locations:
            remote = loc.strip().lower() == "remote"
            params = {"query": f"{title} in {ctx.country or 'India'}" if remote else f"{title} in {loc}",
                      "num_pages": 1, "date_posted": posted}
            if remote:
                params["remote_jobs_only"] = "true"
            data = ctx.fetch.get_json("https://jsearch.p.rapidapi.com/search", params=params, headers=headers)
            for it in (data.get("data") or []) if isinstance(data, dict) else []:
                options = [o for o in (it.get("apply_options") or []) if isinstance(o, dict)]
                form = next((clean(o.get("apply_link")) for o in options if parse_url(clean(o.get("apply_link")))), "")
                if not form:
                    form = next((clean(o.get("apply_link")) for o in options if o.get("is_direct")), "")
                where = ", ".join(x for x in (clean(it.get("job_city")), clean(it.get("job_state")), clean(it.get("job_country"))) if x)
                j = job(source="jsearch", title=it.get("job_title"), company=it.get("employer_name"),
                        url=it.get("job_apply_link"), apply_url=form, location="Remote" if it.get("job_is_remote") else where,
                        description=it.get("job_description"), posted_at=it.get("job_posted_at_datetime_utc"),
                        salary=salary_text(it.get("job_min_salary"), it.get("job_max_salary"), it.get("job_salary_currency"), it.get("job_salary_period")))
                if keep(j, ctx.titles, ctx.locations, is_remote=bool(it.get("job_is_remote")), check_title=False):
                    out.append(j)
    return _dedup(out)
