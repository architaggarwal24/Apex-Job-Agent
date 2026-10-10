# applier/adapters/lever.py
"""
Lever (jobs.lever.co): the posting is at /<company>/<id> and the application
form at /<company>/<id>/apply. Standard fields are one "Full name" box, email,
phone, current company, the links (urls[LinkedIn], urls[GitHub],
urls[Portfolio], ...), a resume upload and an "Additional information" box;
the company's own questions are "cards" (name="cards[<id>][field0]"). The
equal-opportunity block (eeo[...]) is left alone.

Lever protects submit with hCaptcha: the runner stops with the usual captcha
failure if one is showing.

Written against a saved copy of Lever-style markup (tests/fixtures); live
Lever pages were not tested.
"""

from urllib.parse import urlsplit, urlunsplit

from applier.adapters.form import FormAdapter


class LeverAdapter(FormAdapter):
    name = "lever"
    site_types = ("lever",)

    scope_selectors = ("form#application-form", "form.application-form", "form[action$='/apply']")
    scope_probe = "input[name='resume'], input[name='name'][type='text']"
    submit_selectors = ("#btn-submit", "button[type=submit]", "button:has-text('Submit application')")
    name_keys = {
        "name": "full_name", "email": "email", "phone": "phone",
        "urls[LinkedIn]": "linkedin_url", "urls[GitHub]": "github_url", "urls[Portfolio]": "portfolio_url",
        "urls[Twitter]": "", "urls[Other]": "",           # links the profile has no fact for
        "org": "", "comments": "",                       # current company / additional info: not profile facts
    }
    skip_name_rx = r"^eeo\[|^consent\[marketing"
    skip_containers = ("[class*='eeo']", "#eeo-section")

    def start_url(self, url: str) -> str:
        """The posting page has no form; the form is at <posting>/apply."""
        parts = urlsplit(url)
        path = parts.path.rstrip("/")
        if not path.endswith("/apply"):
            path += "/apply"
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))
