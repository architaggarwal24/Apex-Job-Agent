# scraper/sources/ats.py
"""
Company boards on an ATS with a public, keyless feed. Applying there is a plain
web form - no job-board account. Each vendor needs the company's board slug
(the part of its careers URL that names it); `parse_url` reads one out of any
link to that vendor, which is how the agent learns companies on its own
(scraper/sources/harvest.py), and the owner can add more by hand.

    vendor            feed                                                   slug example
    greenhouse        boards-api.greenhouse.io/v1/boards/{slug}/jobs          boards.greenhouse.io/{slug}
    lever             api.lever.co/v0/postings/{slug}?mode=json               jobs.lever.co/{slug}
    ashby             api.ashbyhq.com/posting-api/job-board/{slug}            jobs.ashbyhq.com/{slug}
    smartrecruiters   api.smartrecruiters.com/v1/companies/{slug}/postings    jobs.smartrecruiters.com/{slug}
    workable          apply.workable.com/api/v1/widget/accounts/{slug}        apply.workable.com/{slug}
    recruitee         {slug}.recruitee.com/api/offers/                        {slug}.recruitee.com
    teamtailor        {slug}.teamtailor.com/jobs.json                         {slug}.teamtailor.com
    personio          {slug}.jobs.personio.de/xml                             {slug}.jobs.personio.de
    breezy            {slug}.breezy.hr/json                                   {slug}.breezy.hr
    pinpoint          {slug}.pinpointhq.com/postings.json                     {slug}.pinpointhq.com
    bamboohr          {slug}.bamboohr.com/careers/list                        {slug}.bamboohr.com/careers
    polymer           api.polymer.co/v1/hire/organizations/{slug}/jobs        jobs.polymer.co/{slug}

A board lists every opening, so the agent keeps the ones whose title and
location match what you search for (scraper/sources/base.keep).
"""

import re
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

from scraper.sources.base import SourceError, clean, html_to_text, job, keep, salary_text

VENDORS = ("greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee", "teamtailor",
           "personio", "breezy", "pinpoint", "bamboohr", "polymer")

_SLUG_RX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


def valid_slug(slug: str) -> bool:
    return bool(_SLUG_RX.match(slug or ""))


def _host_path(url: str) -> tuple[str, list[str]]:
    try:
        p = urlparse((url or "").strip())
    except ValueError:
        return "", []
    host = (p.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    return host, [s for s in p.path.split("/") if s]


def parse_url(url: str) -> tuple[str, str] | None:
    """(vendor, slug) when `url` points at a company board on a known ATS, else None."""
    host, path = _host_path(url)
    if not host:
        return None
    first = path[0] if path else ""
    if host in ("boards.greenhouse.io", "job-boards.greenhouse.io", "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io"):
        slug = first if first != "embed" else ""
        return ("greenhouse", slug) if valid_slug(slug) else None
    if host in ("jobs.lever.co", "jobs.eu.lever.co"):
        return ("lever", first) if valid_slug(first) else None
    if host == "jobs.ashbyhq.com":
        return ("ashby", first) if valid_slug(first) else None
    if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com"):
        return ("smartrecruiters", first) if valid_slug(first) else None
    if host == "apply.workable.com":
        return ("workable", first) if valid_slug(first) and first not in ("api", "j") else None
    if host == "jobs.polymer.co":
        return ("polymer", first) if valid_slug(first) else None
    for suffix, vendor in ((".recruitee.com", "recruitee"), (".teamtailor.com", "teamtailor"),
                           (".jobs.personio.de", "personio"), (".jobs.personio.com", "personio"),
                           (".breezy.hr", "breezy"), (".pinpointhq.com", "pinpoint"), (".bamboohr.com", "bamboohr")):
        if host.endswith(suffix):
            slug = host[: -len(suffix)]
            if vendor == "bamboohr" and first != "careers":
                return None
            return (vendor, slug) if valid_slug(slug) and "." not in slug else None
    return None


# ── vendors ───────────────────────────────────────────────────────

def greenhouse(slug, ctx):
    data = ctx.fetch.get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true"})
    out = []
    for it in (data.get("jobs") or []) if isinstance(data, dict) else []:
        loc = clean((it.get("location") or {}).get("name")) if isinstance(it.get("location"), dict) else ""
        j = job(source="greenhouse", title=it.get("title"), company=ctx.company_name(slug, data.get("company_name") if isinstance(data, dict) else ""),
                url=it.get("absolute_url"), location=loc, description=it.get("content"), posted_at=it.get("updated_at"))
        if keep(j, ctx.titles, ctx.locations):
            out.append(j)
    return out


def lever(slug, ctx):
    data = ctx.fetch.get_json(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"})
    if not isinstance(data, list):
        raise SourceError("Lever answered with a page instead of JSON (try again later)")
    out = []
    for it in data:
        cats = it.get("categories") or {}
        sr = it.get("salaryRange") or {}
        j = job(source="lever", title=it.get("text"), company=ctx.company_name(slug), url=it.get("hostedUrl"),
                apply_url=it.get("applyUrl"), location=clean(cats.get("location")) or clean(it.get("workplaceType")),
                description=it.get("descriptionPlain") or it.get("description"),
                salary=salary_text(sr.get("min"), sr.get("max"), sr.get("currency"), sr.get("interval")),
                posted_at=it.get("createdAt"))
        if keep(j, ctx.titles, ctx.locations, is_remote=clean(it.get("workplaceType")).lower() == "remote"):
            out.append(j)
    return out


def ashby(slug, ctx):
    data = ctx.fetch.get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", params={"includeCompensation": "true"})
    out = []
    for it in (data.get("jobs") or []) if isinstance(data, dict) else []:
        if it.get("isListed") is False:
            continue
        comp = it.get("compensation") or {}
        j = job(source="ashby", title=it.get("title"), company=ctx.company_name(slug), url=it.get("jobUrl"),
                apply_url=it.get("applyUrl"), location=clean(it.get("location")),
                description=it.get("descriptionPlain") or it.get("descriptionHtml"),
                salary=clean(comp.get("compensationTierSummary")) if isinstance(comp, dict) else "",
                posted_at=it.get("publishedAt"))
        if keep(j, ctx.titles, ctx.locations, is_remote=bool(it.get("isRemote"))):
            out.append(j)
    return out


def smartrecruiters(slug, ctx):
    out = []
    for offset in (0, 100, 200):
        data = ctx.fetch.get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                                  params={"limit": 100, "offset": offset})
        items = (data.get("content") or []) if isinstance(data, dict) else []
        for it in items:
            loc = it.get("location") or {}
            where = ", ".join(x for x in (clean(loc.get("city")), clean(loc.get("region")), clean(loc.get("country"))) if x)
            j = job(source="smartrecruiters", title=it.get("name"), company=ctx.company_name(slug, clean((it.get("company") or {}).get("name"))),
                    url=f"https://jobs.smartrecruiters.com/{slug}/{clean(it.get('id'))}", location=where,
                    posted_at=it.get("releasedDate"))
            if keep(j, ctx.titles, ctx.locations, is_remote=bool(loc.get("remote"))):
                ref = clean(it.get("ref"))
                if ref:
                    try:                                  # the description is one call per matched job
                        ad = (ctx.fetch.get_json(ref).get("jobAd") or {}).get("sections") or {}
                        j["description"] = html_to_text("\n\n".join(clean((s or {}).get("text")) for s in ad.values()))[:20000]
                    except (SourceError, AttributeError):
                        pass
                out.append(j)
        if len(items) < 100:
            break
    return out


def workable(slug, ctx):
    data = ctx.fetch.get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", params={"details": "true"})
    out = []
    for it in (data.get("jobs") or []) if isinstance(data, dict) else []:
        loc = it.get("location") or {}
        j = job(source="workable", title=it.get("title"), company=ctx.company_name(slug, clean(data.get("name"))),
                url=it.get("url") or it.get("shortlink"), apply_url=it.get("application_url"),
                location=clean(loc.get("location_str")) or ", ".join(x for x in (clean(loc.get("city")), clean(loc.get("country"))) if x),
                description=it.get("full_description") or it.get("description"), posted_at=it.get("published_on"))
        if keep(j, ctx.titles, ctx.locations, is_remote=bool(loc.get("telecommuting")) or clean(loc.get("workplace_type")) == "remote"):
            out.append(j)
    return out


def recruitee(slug, ctx):
    data = ctx.fetch.get_json(f"https://{slug}.recruitee.com/api/offers/")
    out = []
    for it in (data.get("offers") or []) if isinstance(data, dict) else []:
        sal = it.get("salary") or {}
        j = job(source="recruitee", title=it.get("title"), company=ctx.company_name(slug, clean(it.get("company_name"))),
                url=it.get("careers_url"), apply_url=it.get("careers_apply_url"),
                location=clean(it.get("location")) or ", ".join(x for x in (clean(it.get("city")), clean(it.get("country_code"))) if x),
                description="\n\n".join(x for x in (clean(it.get("description")), clean(it.get("requirements"))) if x),
                salary=salary_text(sal.get("min"), sal.get("max"), sal.get("currency"), sal.get("period")) if isinstance(sal, dict) else "",
                posted_at=it.get("published_at") or it.get("created_at"))
        if keep(j, ctx.titles, ctx.locations, is_remote=bool(it.get("remote"))):
            out.append(j)
    return out


def teamtailor(slug, ctx):
    data = ctx.fetch.get_json(f"https://{slug}.teamtailor.com/jobs.json")
    out = []
    for it in (data.get("items") or []) if isinstance(data, dict) else []:
        extra = it.get("_teamtailor") or {}
        loc = clean(extra.get("location")) or ", ".join(clean(x) for x in (extra.get("locations") or []) if clean(x))
        j = job(source="teamtailor", title=it.get("title"), company=ctx.company_name(slug, clean(extra.get("company"))),
                url=it.get("url"), location=loc, description=it.get("content_html") or it.get("content_text"),
                posted_at=it.get("date_published"))
        if keep(j, ctx.titles, ctx.locations, is_remote=bool(extra.get("remote_status")) if "remote_status" in extra else None):
            out.append(j)
    return out


def personio(slug, ctx):
    text = ctx.fetch.get_text(f"https://{slug}.jobs.personio.de/xml", params={"language": "en"})
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise SourceError(f"bad XML: {e}") from None
    out = []
    for pos in root.iter("position"):
        get = lambda tag: clean((pos.findtext(tag) or ""))
        descs = [clean(d.findtext("value")) for d in pos.iter("jobDescription")]
        j = job(source="personio", title=get("name"), company=ctx.company_name(slug), url=f"https://{slug}.jobs.personio.de/job/{get('id')}",
                location=get("office"), description="\n\n".join(d for d in descs if d), posted_at=get("createdAt"),
                seniority=get("seniority"))
        if keep(j, ctx.titles, ctx.locations):
            out.append(j)
    return out


def breezy(slug, ctx):
    data = ctx.fetch.get_json(f"https://{slug}.breezy.hr/json")
    out = []
    for it in data if isinstance(data, list) else []:
        loc = it.get("location") or {}
        j = job(source="breezy", title=it.get("name"), company=ctx.company_name(slug, clean((it.get("company") or {}).get("name"))),
                url=it.get("url") or f"https://{slug}.breezy.hr/p/{clean(it.get('friendly_id') or it.get('id'))}",
                location=clean(loc.get("name")) if isinstance(loc, dict) else clean(loc),
                description=it.get("description"), posted_at=it.get("published_date"))
        if keep(j, ctx.titles, ctx.locations, is_remote=bool(loc.get("is_remote")) if isinstance(loc, dict) else None):
            out.append(j)
    return out


def pinpoint(slug, ctx):
    data = ctx.fetch.get_json(f"https://{slug}.pinpointhq.com/postings.json")
    out = []
    for it in (data.get("data") or []) if isinstance(data, dict) else (data if isinstance(data, list) else []):
        locs = it.get("locations") or ([it.get("location")] if it.get("location") else [])
        loc = ", ".join(clean(l.get("name") if isinstance(l, dict) else l) for l in locs if l)
        j = job(source="pinpoint", title=it.get("title"), company=ctx.company_name(slug, clean((it.get("company") or {}).get("name"))),
                url=it.get("url") or it.get("application_url"), apply_url=it.get("application_url"),
                location=loc, description=it.get("description") or it.get("content"),
                posted_at=it.get("published_at") or it.get("created_at"))
        if keep(j, ctx.titles, ctx.locations, is_remote=clean(it.get("workplace_type")).lower() == "remote" or None):
            out.append(j)
    return out


def bamboohr(slug, ctx):
    data = ctx.fetch.get_json(f"https://{slug}.bamboohr.com/careers/list")
    out = []
    for it in (data.get("result") or []) if isinstance(data, dict) else []:
        loc = it.get("location") or {}
        where = ", ".join(x for x in (clean(loc.get("city")), clean(loc.get("state"))) if x) if isinstance(loc, dict) else clean(loc)
        jid = clean(it.get("id"))
        j = job(source="bamboohr", title=it.get("jobOpeningName"), company=ctx.company_name(slug),
                url=f"https://{slug}.bamboohr.com/careers/{jid}", location=where, posted_at=it.get("datePosted"))
        if keep(j, ctx.titles, ctx.locations, is_remote=bool(it.get("isRemote"))):
            try:                                          # the description is one call per matched job
                detail = ctx.fetch.get_json(f"https://{slug}.bamboohr.com/careers/{jid}/detail")
                j["description"] = html_to_text(clean(((detail.get("result") or {}).get("jobOpening") or {}).get("description")))[:20000]
            except (SourceError, AttributeError):
                pass
            out.append(j)
    return out


def polymer(slug, ctx):
    data = ctx.fetch.get_json(f"https://api.polymer.co/v1/hire/organizations/{slug}/jobs")
    items = data if isinstance(data, list) else ((data.get("jobs") or data.get("data") or []) if isinstance(data, dict) else [])
    out = []
    for it in items:
        j = job(source="polymer", title=it.get("title"), company=ctx.company_name(slug), url=it.get("job_post_url"),
                location=clean(it.get("display_location")) or ", ".join(x for x in (clean(it.get("city")), clean(it.get("country"))) if x),
                description=it.get("description"), posted_at=it.get("published_at") or it.get("created_at"))
        if keep(j, ctx.titles, ctx.locations, is_remote="remote" in clean(it.get("remoteness_pretty")).lower() or None):
            out.append(j)
    return out


SEARCH = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters,
          "workable": workable, "recruitee": recruitee, "teamtailor": teamtailor, "personio": personio,
          "breezy": breezy, "pinpoint": pinpoint, "bamboohr": bamboohr, "polymer": polymer}
assert tuple(SEARCH) == VENDORS
