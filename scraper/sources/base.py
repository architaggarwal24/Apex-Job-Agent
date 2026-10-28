# scraper/sources/base.py
"""
What every login-free source shares: a small HTTP client, HTML-to-text, the
job dict the pipeline inserts, and the title / location matching used by the
sources that cannot search for themselves (a company board lists every
opening; the agent keeps the ones that look like what you asked for).

Nothing here touches the network on import. `Fetcher` is the one place that
does, and every source takes it as an argument so tests pass a fake.
"""

import html
import logging
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

USER_AGENT = "ApexJobAgent/1.0 (+personal job search; contact via the job form)"
TIMEOUT = 20


class SourceError(Exception):
    """The source could not be read (HTTP error, bad payload): the run notes it and moves on."""


class Fetcher:
    """GET/POST through `requests`, JSON or text. Raises SourceError, never a bare
    requests exception, so a source's caller has one thing to catch."""

    def __init__(self, timeout: float = TIMEOUT):
        self.timeout = timeout

    def _request(self, method: str, url: str, **kw):
        import requests                      # imported here: the test fake never needs it
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json, text/xml, application/xml, text/html;q=0.8"}
        headers.update(kw.pop("headers", None) or {})
        try:
            r = requests.request(method, url, headers=headers, timeout=self.timeout, **kw)
        except requests.RequestException as e:
            raise SourceError(f"{type(e).__name__}: {e}") from None
        if r.status_code >= 400:
            raise SourceError(f"HTTP {r.status_code} from {url.split('?')[0]}")
        return r

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None):
        r = self._request("GET", url, params=params, headers=headers)
        try:
            return r.json()
        except ValueError:
            raise SourceError(f"not JSON from {url.split('?')[0]}") from None

    def get_text(self, url: str, params: dict | None = None, headers: dict | None = None) -> str:
        return self._request("GET", url, params=params, headers=headers).text

    def post_json(self, url: str, body: dict, headers: dict | None = None):
        r = self._request("POST", url, json=body, headers=headers)
        try:
            return r.json()
        except ValueError:
            raise SourceError(f"not JSON from {url.split('?')[0]}") from None


# ── text helpers ─────────────────────────────────────────────────

_TAG_RX = re.compile(r"<[^>]+>")
_BLOCK_RX = re.compile(r"</?(p|br|div|li|ul|ol|h[1-6]|tr|section)[^>]*>", re.I)
_WS_RX = re.compile(r"[ \t\r\f\v]+")
_NL_RX = re.compile(r"\n{3,}")


def html_to_text(raw: str) -> str:
    """Readable text from an HTML fragment (entities decoded, blocks on their own lines)."""
    if not raw:
        return ""
    s = html.unescape(str(raw))
    if "<" in s:                     # Greenhouse ships entity-escaped HTML: unescape may reveal tags
        s = _BLOCK_RX.sub("\n", s)
        s = _TAG_RX.sub(" ", s)
        s = html.unescape(s)
    s = _WS_RX.sub(" ", s)
    s = "\n".join(line.strip() for line in s.split("\n"))
    return _NL_RX.sub("\n\n", s).strip()


def clean(v) -> str:
    """A payload value as stripped text ("" for None / non-text containers)."""
    if v is None or isinstance(v, (list, dict)):
        return ""
    return str(v).strip()


def iso_date(v) -> str:
    """YYYY-MM-DD from an ISO string, epoch seconds/milliseconds, or "" when unreadable."""
    if v is None or v == "":
        return ""
    if isinstance(v, (int, float)):
        try:
            secs = v / 1000 if v > 1e11 else v
            return datetime.fromtimestamp(secs, tz=timezone.utc).strftime("%Y-%m-%d")
        except (OverflowError, OSError, ValueError):
            return ""
    s = str(v).strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    if m:
        return m.group(1)
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


def salary_text(lo, hi, currency="", period="") -> str:
    lo, hi, currency, period = clean(lo), clean(hi), clean(currency), clean(period)
    if not (lo or hi) or lo in ("0", "0.0") and hi in ("0", "0.0", ""):
        return ""
    def fmt(x):
        try:
            return f"{float(x):,.0f}"
        except ValueError:
            return x
    amount = f"{fmt(lo)}-{fmt(hi)}" if lo and hi else fmt(lo or hi)
    return " ".join(p for p in (currency, amount, f"/{period}" if period else "") if p)


def job(*, source: str, title: str, company: str, url: str, apply_url: str = "", location: str = "",
        description: str = "", salary: str = "", posted_at: str = "", seniority: str = "") -> dict | None:
    """The dict tracking/pipeline inserts; None when the essentials are missing."""
    title, company, url = clean(title), clean(company), clean(url)
    if not (title and company and url.startswith("http")):
        return None
    apply_url = clean(apply_url)
    if not apply_url.startswith("http") or apply_url == url:
        apply_url = ""
    return {"title": title[:300], "company": company[:200], "location": clean(location)[:200],
            "url": url, "apply_url": apply_url, "source": source,
            "description": html_to_text(description)[:20000], "salary": clean(salary)[:100],
            "seniority": clean(seniority)[:60], "applicants": "", "posted_at": iso_date(posted_at)}


# ── matching for sources that only list ───────────────────────────

_WORD_RX = re.compile(r"[a-z0-9+#.]+")
_REMOTE_RX = re.compile(r"\b(remote|anywhere|work from home|wfh|distributed)\b", re.I)
# words that say nothing about the role on their own
_NOISE = {"senior", "junior", "lead", "staff", "principal", "sr", "jr", "ii", "iii", "iv", "i", "the", "a", "an",
          "of", "and", "or", "for", "to", "in", "at", "with", "remote", "hybrid", "onsite", "contract", "intern",
          "internship", "fresher", "freshers", "new", "grad", "graduate", "entry", "level", "mid", "experienced"}


def _words(text: str) -> list[str]:
    return [w for w in _WORD_RX.findall((text or "").lower())]


def title_matches(job_title: str, wanted: list[str]) -> bool:
    """True when every meaningful word of one of the wanted titles is in the
    job's title: "Backend Engineer" matches "Senior Backend Engineer (Python)",
    not "Backend Support Specialist" (engineer missing)."""
    have = set(_words(job_title))
    if not have:
        return False
    for w in wanted or []:
        need = [t for t in _words(w) if t not in _NOISE]
        if need and all(t in have for t in need):
            return True
    return False


# cities with two names in common use: a wanted "Bangalore" must find "Bengaluru, India"
_ALIASES = (("bangalore", "bengaluru"), ("gurgaon", "gurugram"), ("bombay", "mumbai"), ("calcutta", "kolkata"),
            ("madras", "chennai"), ("new delhi", "delhi"), ("delhi ncr", "delhi"), ("trivandrum", "thiruvananthapuram"),
            ("cochin", "kochi"), ("poona", "pune"), ("new york city", "new york"), ("sf", "san francisco"))


def _spellings(place: str) -> set[str]:
    p = place.strip().lower()
    out = {p}
    for a, b in _ALIASES:
        if p == a: out.add(b)
        if p == b: out.add(a)
    return out


def location_matches(location: str, wanted: list[str], is_remote: bool | None = None) -> bool:
    """True for a remote job when "Remote" is among the wanted locations, for
    a job whose location names one of the wanted places, and for a job that
    says nothing about where it is (the score decides those)."""
    loc = (location or "").strip()
    wants_remote = any(w.strip().lower() == "remote" for w in wanted or [])
    if is_remote or _REMOTE_RX.search(loc):
        if wants_remote:
            return True
    if not loc:
        return True
    low = loc.lower()
    return any(sp in low for w in wanted or [] if w.strip() and w.strip().lower() != "remote" for sp in _spellings(w))


def keep(j: dict | None, titles: list[str], locations: list[str], *, is_remote=None, check_title=True) -> bool:
    if j is None:
        return False
    if check_title and not title_matches(j["title"], titles):
        return False
    return location_matches(j["location"], locations, is_remote)
