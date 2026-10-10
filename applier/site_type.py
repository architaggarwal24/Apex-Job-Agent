# applier/site_type.py
"""
Which kind of application flow is this? detect_site_type(url, html) answers with
ONE of SITE_TYPES. The autonomy ledger and rules (db/autonomy_db.py,
applier/autonomy.py) are kept per site type, so reliability is measured for
"Greenhouse forms" or "Naukri one-click" separately instead of for the agent as
a whole.

A PURE function: it looks only at the two strings it is given (no browser, no
network, no database, no clock), so the same input always gives the same answer.
`html` is optional; the URL alone decides when it is empty.

Precedence, most specific first:
  1. the host of the URL names an ATS (greenhouse, lever, workday, ashby,
     smartrecruiters) - that wins whatever the page says;
  2. a job board's host (linkedin, indeed, naukri) - the page then says which
     flow it is:
       linkedin  Easy Apply if the page shows its marker, or if there is no html
                 at all (the scraper only collects Easy Apply jobs); a page
                 without the marker applies on the company's own site: generic
       indeed    Indeed Apply likewise (marker, or no html); otherwise generic
       naukri    a chat-style questionnaire on the page is naukri_chat,
                 otherwise naukri_one_click
  3. a company's own careers page that EMBEDS an ATS is recognised by the ATS's
     own script/iframe markers in the html;
  4. anything else is generic.
"""

import re
from urllib.parse import urlparse

SITE_TYPES = (
    "linkedin_easy_apply", "indeed_apply", "naukri_one_click", "naukri_chat",
    "greenhouse", "lever", "workday", "ashby", "smartrecruiters", "generic",
)
GENERIC = "generic"

# (site_type, host regex): matched against the URL's host, which is lowercased
# and has any port and "www." removed. A suffix match, so "acme.wd5.myworkdayjobs.com" counts.
_ATS_HOSTS = (
    ("greenhouse",      r"(^|\.)greenhouse\.io$"),
    ("lever",           r"(^|\.)lever\.co$"),
    ("workday",         r"(^|\.)(myworkdayjobs|myworkdaysite)\.com$|(^|\.)workday\.com$"),
    ("ashby",           r"(^|\.)ashbyhq\.com$"),
    ("smartrecruiters", r"(^|\.)smartrecruiters\.com$"),
)
_ATS_HOSTS = tuple((t, re.compile(rx)) for t, rx in _ATS_HOSTS)

_LINKEDIN = re.compile(r"(^|\.)linkedin\.com$")
_INDEED   = re.compile(r"(^|\.)indeed\.[a-z.]+$")
_NAUKRI   = re.compile(r"(^|\.)naukri\.com$")

# Markers in a page's html, matched case-insensitively.
_EMBEDDED_ATS = (
    ("greenhouse",      re.compile(r"boards\.greenhouse\.io|job-boards\.greenhouse\.io|grnhse_app|grnhse_iframe", re.I)),
    ("lever",           re.compile(r"jobs\.lever\.co|lever-jobs-embed", re.I)),
    ("workday",         re.compile(r"myworkdayjobs\.com|myworkdaysite\.com", re.I)),
    ("ashby",           re.compile(r"jobs\.ashbyhq\.com|ashby_jid", re.I)),
    ("smartrecruiters", re.compile(r"smartrecruiters\.com", re.I)),
)
_LINKEDIN_EASY   = re.compile(r"easy apply|jobs-apply-button|data-easy-apply", re.I)
_INDEED_APPLY    = re.compile(r"indeedapplybutton|ia-indeedapplybutton|apply with indeed|easily apply|indeed apply", re.I)
_NAUKRI_CHAT     = re.compile(r"chatbot|chat-container|webbotdrawer", re.I)


def _host(url: str) -> str:
    try:
        host = (urlparse((url or "").strip()).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def detect_site_type(url: str, html: str = "") -> str:
    """One of SITE_TYPES for this application page. Never raises; anything
    unrecognised (including None or non-text input) is "generic"."""
    url  = url  if isinstance(url, str) else ""
    html = html if isinstance(html, str) else ""
    host = _host(url)

    for site_type, rx in _ATS_HOSTS:
        if rx.search(host):
            return site_type

    if _LINKEDIN.search(host):
        return "linkedin_easy_apply" if (not html or _LINKEDIN_EASY.search(html)) else GENERIC

    if _INDEED.search(host):
        return "indeed_apply" if (not html or _INDEED_APPLY.search(html)) else GENERIC

    if _NAUKRI.search(host):
        return "naukri_chat" if _NAUKRI_CHAT.search(html) else "naukri_one_click"

    for site_type, rx in _EMBEDDED_ATS:
        if rx.search(html):
            return site_type
    return GENERIC
