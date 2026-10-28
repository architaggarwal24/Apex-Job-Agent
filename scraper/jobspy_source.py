# scraper/jobspy_source.py
"""
LinkedIn, Indeed and Naukri searches through python-jobspy — no account, no
browser window, no saved session.

JobSpy talks to each board's public search (LinkedIn's guest endpoint,
Indeed's job API, Naukri's search API) and gives back one row per posting.
Two things matter for the agent:

  * `job_url`        the card on the board - what the job was FOUND on (jobs.url)
  * `job_url_direct` the employer's own apply page when the board links out to
                     one - where the job is APPLIED (jobs.apply_url). LinkedIn
                     and Indeed give it for "apply on company site" postings;
                     Naukri never does (its apply button needs a Naukri login).

Everything the board says is kept: full description, salary, posted date. A
posting with no title, company or URL is dropped.

LinkedIn: JobSpy 1.4 no longer reads the "apply on company site" link, so the
job page is fetched here instead of by JobSpy (one request per posting either
way) and both the description and that link come out of it
(`linkedin_details`). A 429 from LinkedIn stops the page fetches for the rest
of the run; the cards found so far are still kept.

`scrape_jobs` is looked up at call time so tests can swap in a fake; nothing
here reaches the network on import.
"""

import logging
import math
import re
from urllib.parse import unquote, urlparse

logger = logging.getLogger(__name__)

SITES = ("linkedin", "indeed", "naukri")

# JobSpy's Indeed scraper is per country site. The profile's country picks it;
# anything JobSpy doesn't know falls back to India, the default audience here.
_INDEED_COUNTRIES = {
    "india": "india", "usa": "usa", "united states": "usa", "us": "usa", "uk": "uk",
    "united kingdom": "uk", "canada": "canada", "australia": "australia", "germany": "germany",
    "singapore": "singapore", "uae": "united arab emirates", "united arab emirates": "united arab emirates",
    "netherlands": "netherlands", "france": "france", "ireland": "ireland",
}


def _clean(v) -> str:
    """A cell of the DataFrame as text: NaN/None become ""."""
    if v is None:
        return ""
    if isinstance(v, float) and math.isnan(v):
        return ""
    return str(v).strip()


def _host(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _same_board(site: str, url: str) -> bool:
    host = _host(url)
    return {"linkedin": "linkedin.com", "indeed": "indeed.", "naukri": "naukri.com"}[site] in host


def _salary(row: dict) -> str:
    lo, hi, cur, interval = (_clean(row.get(k)) for k in ("min_amount", "max_amount", "currency", "interval"))
    if not (lo or hi):
        return ""
    def fmt(x):
        try:
            return f"{float(x):,.0f}"
        except ValueError:
            return x
    amount = f"{fmt(lo)}-{fmt(hi)}" if lo and hi else fmt(lo or hi)
    return " ".join(p for p in (cur, amount, f"/{interval}" if interval else "") if p).strip()


def normalize(site: str, row: dict) -> dict | None:
    """One JobSpy row -> the job dict tracking/pipeline inserts (None to drop)."""
    url = _clean(row.get("job_url")).split("?")[0] if site == "linkedin" else _clean(row.get("job_url"))
    title, company = _clean(row.get("title")), _clean(row.get("company"))
    if not (url and title and company):
        return None
    direct = _clean(row.get("job_url_direct"))
    apply_url = direct if direct and direct != url and not _same_board(site, direct) else ""
    desc = _clean(row.get("description"))
    if _clean(row.get("skills")):
        desc = (desc + "\n\nSkills: " + _clean(row.get("skills"))).strip()
    return {
        "title": title, "company": company, "location": _clean(row.get("location")),
        "url": url, "apply_url": apply_url, "source": site,
        "description": desc[:20000], "salary": _salary(row),
        "seniority": _clean(row.get("job_level")), "applicants": "",
        "posted_at": _clean(row.get("date_posted"))[:10],
    }


class RateLimited(Exception):
    pass


_APPLY_URL_RX = re.compile(r'[?&]url=([^"&\s]+)')
_LI_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/124.0 Safari/537.36", "Accept-Language": "en-US,en;q=0.9"}


def _http_get(url: str) -> tuple[int, str]:
    import requests
    r = requests.get(url, headers=_LI_HEADERS, timeout=15)
    return r.status_code, r.text


def parse_linkedin_page(html: str) -> dict:
    """{"description", "apply_url", "seniority"} from a LinkedIn job page. The
    off-site apply link sits in <code id="applyUrl"><!--"…externalApply…?url=<encoded>…"--></code>."""
    from bs4 import BeautifulSoup
    from scraper.sources.base import html_to_text
    soup = BeautifulSoup(html or "", "html.parser")
    out = {"description": "", "apply_url": "", "seniority": ""}
    body = soup.find("div", class_="show-more-less-html__markup")
    if body:
        out["description"] = html_to_text(body.decode_contents())
    code = soup.find("code", id="applyUrl")
    if code:
        m = _APPLY_URL_RX.search(code.decode_contents())
        if m:
            link = unquote(m.group(1)).strip()
            if link.startswith("http") and "linkedin.com" not in _host(link):
                out["apply_url"] = link
    for item in soup.find_all("li", class_="description__job-criteria-item"):
        head = item.find("h3")
        if head and head.get_text(strip=True).lower() == "seniority level":
            val = item.find("span")
            out["seniority"] = val.get_text(strip=True).lower() if val else ""
    return out


def linkedin_details(job_url: str, get=None) -> dict | None:
    """The job page's details, None when it can't be read; RateLimited on 429."""
    get = get or _http_get
    try:
        status, text = get(job_url)
    except Exception as e:
        logger.debug(f"  [linkedin] {job_url}: {e}")
        return None
    if status == 429:
        raise RateLimited(job_url)
    if status >= 400:
        return None
    return parse_linkedin_page(text)


def _indeed_country(profile: dict) -> str:
    country = ((profile.get("_flat") or {}).get("location_country") or "India").strip().lower()
    return _INDEED_COUNTRIES.get(country, "india")


# ── Where to search, and which results are in the wrong country ─────────────
# A bare city goes to LinkedIn as-is and LinkedIn guesses: "Delhi" became
# Delhi, Michigan. So every query names the profile's country, "Remote" means
# remote jobs in that country, and a result that names another country is
# dropped unless it is remote.

_COUNTRIES = {
    "india": "india", "bharat": "india", "usa": "usa", "us": "usa", "u.s.": "usa", "united states": "usa",
    "united states of america": "usa", "america": "usa", "uk": "uk", "united kingdom": "uk", "england": "uk",
    "scotland": "uk", "wales": "uk", "great britain": "uk", "canada": "canada", "australia": "australia",
    "germany": "germany", "deutschland": "germany", "singapore": "singapore", "uae": "uae",
    "united arab emirates": "uae", "netherlands": "netherlands", "the netherlands": "netherlands",
    "france": "france", "ireland": "ireland", "spain": "spain", "italy": "italy", "poland": "poland",
    "portugal": "portugal", "switzerland": "switzerland", "austria": "austria", "belgium": "belgium",
    "sweden": "sweden", "norway": "norway", "denmark": "denmark", "finland": "finland", "czechia": "czechia",
    "czech republic": "czechia", "romania": "romania", "hungary": "hungary", "greece": "greece",
    "ukraine": "ukraine", "turkey": "turkey", "türkiye": "turkey", "israel": "israel", "japan": "japan",
    "china": "china", "hong kong": "hong kong", "hong kong sar": "hong kong", "taiwan": "taiwan",
    "south korea": "south korea", "korea": "south korea", "philippines": "philippines", "vietnam": "vietnam",
    "indonesia": "indonesia", "malaysia": "malaysia", "thailand": "thailand", "pakistan": "pakistan",
    "bangladesh": "bangladesh", "sri lanka": "sri lanka", "nepal": "nepal", "saudi arabia": "saudi arabia",
    "qatar": "qatar", "egypt": "egypt", "nigeria": "nigeria", "kenya": "kenya", "south africa": "south africa",
    "brazil": "brazil", "mexico": "mexico", "argentina": "argentina", "colombia": "colombia", "chile": "chile",
    "new zealand": "new zealand", "estonia": "estonia", "lithuania": "lithuania", "latvia": "latvia",
}
_US_STATES = set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM "
                 "NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split())
_REMOTE_RX = re.compile(r"\b(remote|anywhere|work from home|wfh)\b", re.I)


def _country_key(name: str) -> str:
    n = (name or "").strip().lower()
    return _COUNTRIES.get(n, n)


def location_country(location: str) -> str | None:
    """The country a result's location names ("Ann Arbor, MI, USA" -> "usa",
    "Lansing, MI" -> "usa", "Bengaluru, Karnataka, India" -> "india"), or None
    when it names none ("Bengaluru", "Remote")."""
    parts = [p.strip() for p in re.sub(r"\(.*?\)", "", location or "").split(",") if p.strip()]
    if not parts:
        return None
    last = parts[-1].lower()
    if last in _COUNTRIES:
        return _COUNTRIES[last]
    if len(parts) >= 2 and parts[-1].upper() in _US_STATES and parts[-1].isupper():
        return "usa"
    return None


def query_location(location: str, country: str) -> tuple[str, bool]:
    """(location to send, is_remote) for one wanted location: "Delhi" ->
    ("Delhi, India", False), "Pune, Maharashtra" -> ("Pune, Maharashtra, India",
    False); "Remote" -> ("India", True); a place that already names a country
    (or is one) is sent unchanged."""
    loc = (location or "").strip()
    if loc.lower() == "remote":
        return country, True
    if location_country(loc) or _country_key(loc) in _COUNTRIES.values() or _country_key(loc) == _country_key(country):
        return loc, False
    return f"{loc}, {country}", False


def is_foreign(location: str, allowed: set[str]) -> bool:
    """True for a result in a country outside `allowed` (country keys) that is not remote."""
    where = location_country(location)
    return where is not None and where not in allowed and not _REMOTE_RX.search(location or "")


def allowed_countries(country: str, locations: list[str]) -> set[str]:
    """The profile's country plus any country a wanted location names on purpose
    ("Berlin, Germany" keeps German results)."""
    out = {_country_key(country)}
    for loc in locations or []:
        named = location_country(loc) or (_country_key(loc) if _country_key(loc) in _COUNTRIES.values() else None)
        if named:
            out.add(named)
    return out


def search(site: str, profile: dict, job_titles: list[str], locations: list[str],
           results_per_query: int = 25, hours_old: int = 168, scrape_jobs=None, on_query=None,
           get=None) -> list[dict]:
    """Every (title, location) pair on one board. Errors on one query are
    logged and the others still run; the caller gets whatever came back.
    `on_query(title, location, n_new)` is called after each query (progress)."""
    if site not in SITES:
        raise ValueError(f"unknown site {site!r}; one of {', '.join(SITES)}")
    if scrape_jobs is None:
        from jobspy import scrape_jobs                       # imported here: optional until used
    results: list[dict] = []
    seen: set[str] = set()
    country = ((profile.get("_flat") or {}).get("location_country") or "India").strip() or "India"
    allowed = allowed_countries(country, locations)
    dropped = 0
    pages_ok = [site == "linkedin"]           # LinkedIn job pages are read here (see the module notes)
    for title in job_titles:
        for location in locations:
            where, remote = query_location(location, country)
            logger.info(f"  [{site}] '{title}' in '{where}'{' (remote)' if remote else ''}")
            try:
                df = scrape_jobs(site_name=[site], search_term=title, location=where, is_remote=remote,
                                 results_wanted=results_per_query, hours_old=hours_old or None,
                                 country_indeed=_indeed_country(profile),
                                 description_format="markdown",
                                 fetch_description=False,     # LinkedIn's pages are read below; Indeed/Naukri carry it in the search
                                 verbose=0)
            except Exception as e:                       # one bad query must not sink the run
                logger.warning(f"  [{site}] '{title}' in '{location}' failed: {e}")
                if on_query:
                    on_query(title, location, 0)
                continue
            rows = df.to_dict("records") if hasattr(df, "to_dict") else list(df or [])
            before = len(results)
            for row in rows:
                job = normalize(site, row)
                if job and is_foreign(job["location"], allowed):
                    dropped += 1
                    continue
                if job and job["url"] not in seen:
                    seen.add(job["url"])
                    results.append(job)
            if pages_ok[0]:
                for job in results[before:]:
                    try:
                        d = linkedin_details(job["url"], get)
                    except RateLimited:
                        logger.warning("  [linkedin] LinkedIn is rate-limiting job pages (429): the rest of this "
                                       "run keeps the cards without descriptions or apply links")
                        pages_ok[0] = False
                        break
                    if d:
                        job["description"] = (d["description"] or job["description"])[:20000]
                        job["apply_url"] = job["apply_url"] or d["apply_url"]
                        job["seniority"] = job["seniority"] or d["seniority"]
            if on_query:
                on_query(title, location, len(results) - before)
    logger.info(f"  [{site}] Total: {len(results)} jobs"
                + (f" ({dropped} outside {country} and not remote dropped)" if dropped else ""))
    return results


def scrape(playwright, profile: dict, job_titles: list[str], locations: list[str], site: str = "",
           **kw) -> list[dict]:
    """The scraper interface tracking/pipeline calls (the playwright handle is
    unused: nothing here opens a browser)."""
    return search(site, profile, job_titles, locations, **kw)
