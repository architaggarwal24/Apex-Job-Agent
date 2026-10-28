# applier/preflight.py
"""
Pre-flight: the checks that run BEFORE any browser work on a job. If one
fails the job goes to "skipped" with an event that says why, and no browser
is ever opened for it.

In order (cheap local checks first, the one network request last, so a job
that is already ruled out never costs a request):

  already_applied       the same title + company, from ANY source, has already
                        gone out for this user (submitted or any later stage)
  blacklisted_company   the company is in the company_blacklist setting
  salary_below_floor    the listed salary tops out below min_salary_lpa
  job_closed            the posting says it is no longer open, or its page is
                        404/410

Each rule fails SAFE: when it can't tell (no salary listed, a salary in a
currency we won't convert, a network error, a site that blocks our request)
the job passes and the browser stage deals with whatever is really there. A
wrongly skipped job is lost silently; a wrongly passed one just costs a run.
A check that crashes is logged and treated as a pass for the same reason.

Plain Python; the network fetch is injectable (`fetch`) so tests never touch
the network.
"""

import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

RULES = ("already_applied", "blacklisted_company", "salary_below_floor", "job_closed")


@dataclass
class PreflightResult:
    ok: bool = True
    rule: str = ""                 # one of RULES when ok is False
    message: str = ""              # the human reason, shown on the timeline
    detail: dict = field(default_factory=dict)


# ── company blacklist ─────────────────────────────────────────────

_LEGAL_SUFFIXES = {"pvt", "ltd", "limited", "private", "inc", "incorporated",
                   "llc", "llp", "corp", "corporation", "co", "company"}


def _name_tokens(name: str) -> list[str]:
    """'Tata Consultancy Services Pvt. Ltd.' -> [tata, consultancy, services]."""
    toks = re.sub(r"[^a-z0-9]+", " ", (name or "").lower().replace("&", " and ")).split()
    while toks and toks[-1] in _LEGAL_SUFFIXES:
        toks.pop()
    return toks


def blacklisted_entry(company: str, blacklist_text: str) -> str | None:
    """The blacklist entry that matches `company`, or None. Entries are
    separated by commas, semicolons or newlines. An entry matches when its
    WORDS appear, in order and side by side, in the company name -
    case, punctuation and legal suffixes (Pvt Ltd, Inc...) don't matter -
    so "Tata" blocks "Tata Consultancy Services" but "Meta" does not block
    "Metals Inc"."""
    have = _name_tokens(company)
    if not have:
        return None
    for raw in re.split(r"[,;\n]+", blacklist_text or ""):
        entry = raw.strip()
        want = _name_tokens(entry)
        if not want:
            continue
        if any(have[i:i + len(want)] == want for i in range(len(have) - len(want) + 1)):
            return entry
    return None


# ── salary ────────────────────────────────────────────────────────

_FOREIGN     = re.compile(r"[$€£]|\b(usd|eur|gbp|aed|sgd|cad|aud)\b", re.I)
_UNDISCLOSED = re.compile(r"not disclosed|undisclosed|negotiable|competitive|unpaid|"
                          r"as per (the )?(company|industry)|best in (the )?industry|"
                          r"market standard|not mentioned|not specified", re.I)
_MONTH       = re.compile(r"per month|a month|/\s*mo(nth)?\b|monthly|per mth", re.I)
_YEAR        = re.compile(r"per annum|per year|a year|/\s*y(ea)?r\b|\bp\.?\s?a\.?\b|"
                          r"annum|annual|yearly|\bctc\b|\blpa\b", re.I)
_NUM         = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(crores?|cr|lakhs?|lacs?|lpa|l|k)?(?![a-z])", re.I)
_LAKH_UNITS  = ("l", "lac", "lacs", "lakh", "lakhs", "lpa")


def parse_salary_lpa(text: str) -> tuple[float, float] | None:
    """(low, high) of a listed salary in lakhs per annum, or None when it
    can't be read with confidence. Understands "12-18 LPA", "₹12L - ₹18L/yr",
    "10-15 Lacs PA", "₹1,20,000 - ₹1,80,000 a year", "₹30K - 40K a month",
    "1.2 Cr". Returns None for another currency, "not disclosed", an
    ambiguous plain amount ("8,00,000" with no period), or a text that says
    both a month and a year."""
    t = (text or "").lower().replace("₹", " inr ")
    t = re.sub(r"\brs\.?(?=\s*\d)", " inr ", t)
    if not t.strip() or _UNDISCLOSED.search(t) or _FOREIGN.search(t):
        return None
    month, year = bool(_MONTH.search(t)), bool(_YEAR.search(t))
    if month and year:
        return None
    mult = 12 if month else 1

    found = [(float(m.group(1).replace(",", "")), (m.group(2) or "").lower())
             for m in _NUM.finditer(t)]
    units = {u for _, u in found if u}
    lakh_ctx  = any(u in _LAKH_UNITS for u in units)
    crore_ctx = any(u.startswith("cr") for u in units)
    k_ctx     = "k" in units

    values = []
    for v, u in found:
        if u.startswith("cr"):
            lpa = v * 100 * mult
        elif u in _LAKH_UNITS:
            lpa = v * mult
        elif u == "k":
            lpa = v * 1000 / 1e5 * mult
        elif v < 1000:                    # a bare number in a range: it shares the range's unit
            if lakh_ctx:
                lpa = v * mult
            elif crore_ctx:
                lpa = v * 100 * mult
            elif k_ctx:
                lpa = v * 1000 / 1e5 * mult
            else:
                continue                  # "12-18" with no unit anywhere: can't know
        elif month or year or v >= 1_000_000:
            lpa = v * mult / 1e5          # an amount in rupees
        else:
            continue                      # "8,00,000" with no period: monthly or yearly?
        values.append(lpa)
    return (min(values), max(values)) if values else None


def _floor(settings) -> float:
    try:
        return max(0.0, float(str(settings.read("min_salary_lpa", "0")).strip() or 0))
    except (TypeError, ValueError):
        return 0.0


# ── is the job still open? ────────────────────────────────────────

_CLOSED_RX = re.compile(
    r"no longer accepting (applications|applicants)"
    r"|(this )?job (is |has )?no longer (available|open|active)"
    r"|(this )?(job|position|posting|listing|vacancy)( has)? (expired|been closed|been filled|been removed)"
    r"|(this )?(job|position|posting|listing) is closed"
    r"|the (job|position) you (are|were) looking for (is no longer|has been)",
    re.I)
_CLOSED_STATUSES = (404, 410)
_NON_CONTENT = re.compile(r"<(script|style|template|noscript)\b.*?</\1\s*>|<!--.*?-->", re.I | re.S)


def _visible_text(html: str) -> str:
    """The page's text without scripts/styles/templates: a closed-job phrase
    sitting in a JS bundle's string table says nothing about THIS job."""
    return re.sub(r"<[^>]+>", " ", _NON_CONTENT.sub(" ", html or ""))


def fetch_page(url: str, timeout: float = 6.0, max_bytes: int = 300_000) -> tuple[int | None, str]:
    """GET `url`: (HTTP status, start of the body), or (None, "") when we
    couldn't tell - a bad or blocked URL, a network error, a timeout. Never
    raises. Only http(s), and never the cloud-metadata addresses."""
    try:
        from core.urlcheck import validate_base_url
        validate_base_url(url)
    except ValueError:
        return None, ""
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (compatible; ApexJobAgent preflight)",
        "Accept-Language": "en"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(max_bytes).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            body = e.read(max_bytes).decode("utf-8", "replace")
        except Exception:
            body = ""
        return e.code, body
    except Exception:
        return None, ""


# ── the rules ─────────────────────────────────────────────────────

def _already_applied(job, jobs_db, settings, fetch) -> PreflightResult:
    dup = jobs_db.find_applied_duplicate(job["job_id"])
    if not dup:
        return PreflightResult()
    return PreflightResult(
        False, "already_applied",
        f"Already applied: the same role at {dup['company']} went out via "
        f"{dup['source'] or 'another source'} ({dup['stage'].replace('_', ' ')})",
        {"duplicate_job_id": dup["job_id"], "duplicate_source": dup["source"],
         "duplicate_stage": dup["stage"]})


def _blacklist(job, jobs_db, settings, fetch) -> PreflightResult:
    entry = blacklisted_entry(job.get("company", ""), settings.read("company_blacklist", ""))
    if entry is None:
        return PreflightResult()
    return PreflightResult(False, "blacklisted_company",
                           f"Company is on your blacklist ({entry})", {"entry": entry})


def _salary(job, jobs_db, settings, fetch) -> PreflightResult:
    floor = _floor(settings)
    listed = (job.get("salary") or "").strip()
    if floor <= 0 or not listed:
        return PreflightResult()
    parsed = parse_salary_lpa(listed)
    if parsed is None or parsed[1] >= floor:
        return PreflightResult()
    return PreflightResult(
        False, "salary_below_floor",
        f"Salary \"{listed}\" tops out at {parsed[1]:g} LPA, below your floor of {floor:g} LPA",
        {"salary": listed, "salary_high_lpa": round(parsed[1], 2), "floor_lpa": floor})


def _open(job, jobs_db, settings, fetch) -> PreflightResult:
    if _CLOSED_RX.search((job.get("description") or "")[:1500]):
        return PreflightResult(False, "job_closed",
                               "Job no longer open: the listing says it is closed",
                               {"how": "listing"})
    from applier.target import apply_url
    url = apply_url(job)                 # the form is what must still be open
    if not url:
        return PreflightResult()
    status, body = fetch(url)
    if status in _CLOSED_STATUSES:
        return PreflightResult(False, "job_closed",
                               f"Job no longer open: the page returns HTTP {status}",
                               {"how": "http_status", "status": status})
    if body and _CLOSED_RX.search(_visible_text(body)):
        return PreflightResult(False, "job_closed",
                               "Job no longer open: the page says it is closed",
                               {"how": "page_text"})
    return PreflightResult()


_CHECKS = (_already_applied, _blacklist, _salary, _open)


def run(job: dict, jobs_db, settings, fetch=None) -> PreflightResult:
    """Run the rules in order; the first failure wins. `job` needs job_id,
    title, company, url (salary and description optional); `jobs_db` any
    JobsDB view; `settings` anything with .read(key, default)."""
    fetch = fetch or fetch_page            # looked up now, so a test can swap it
    for check in _CHECKS:
        try:
            result = check(job, jobs_db, settings, fetch)
        except Exception:
            logger.exception(f"[Preflight] {check.__name__} crashed; treating it as a pass")
            continue
        if not result.ok:
            return result
    return PreflightResult()
