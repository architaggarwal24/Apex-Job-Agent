# applier/adapters/standard.py
"""
The standard questions every career page asks, recognised by what they say and
answered from the profile, with no LLM:

    first_name last_name full_name email phone
    linkedin_url github_url portfolio_url
    authorized_to_work require_sponsorship        (work authorisation)
    resume_file cover_letter_file                 (uploads)

Anything else is a CUSTOM question: it gets a key made from its label, is
answered from the answer bank (applier/answers.py) if the bank knows it, and
otherwise - if the form makes it mandatory - becomes a question for you,
exactly as with the universal filler. An optional question nobody can answer is
left blank, not asked.

Work authorisation is the one place a profile fact can be wrong for the form:
the profile says whether you may work in YOUR country (Authorized to Work in
India? Yes). A question about another country ("...authorized to work in the
United States?") is therefore NOT answered from it - it is left for you.

Plain Python, no browser.
"""

import re

from applier.answers import fit_to_options

STANDARD_KEYS = ("first_name", "last_name", "full_name", "email", "phone",
                 "linkedin_url", "github_url", "portfolio_url",
                 "authorized_to_work", "require_sponsorship")
FILE_KEYS = ("resume_file", "cover_letter_file")

# (key, label regex), tried in order. Labels are matched lowercased.
_LABEL_RULES = (
    ("first_name",    r"^(legal |preferred )?first[ -]?name\b|^given name"),
    ("last_name",     r"^(legal )?(last|family)[ -]?name\b|^surname"),
    ("full_name",     r"^(your |legal |preferred )?(full )?name$|^full (legal )?name"),
    ("email",         r"e-?mail"),
    ("phone",         r"phone|mobile|cell ?(number)?|contact number"),
    ("linkedin_url",  r"linked ?in"),
    ("github_url",    r"git ?hub"),
    ("portfolio_url", r"portfolio|personal (web ?)?site|personal url|^website$|^your (web ?)?site"),
)
_AUTH   = re.compile(r"authori[sz]ed to work|authori[sz]ation to work|work authori[sz]ation|"
                     r"legally (authori[sz]ed|eligible|entitled)|(eligib|right)\w* to work|permitted to work", re.I)
_SPONSOR = re.compile(r"sponsor", re.I)
_RULES = tuple((k, re.compile(rx, re.I)) for k, rx in _LABEL_RULES)

# Optional demographic / self-identification questions: never filled, never asked.
EEO_LABEL = re.compile(r"\b(gender|race|ethnic\w*|veteran|disabilit\w*|hispanic|latino|sexual orientation|"
                       r"transgender|pronouns?|self[- ]identif\w*)\b", re.I)

# Countries (and regions) a work-authorisation question may name.
_COUNTRIES = ("united states", "u.s.", "usa", "united kingdom", "u.k.", "uk", "great britain", "england",
              "canada", "australia", "new zealand", "germany", "france", "netherlands", "ireland", "spain",
              "italy", "poland", "sweden", "switzerland", "portugal", "european union", "europe", "eu",
              "singapore", "japan", "china", "hong kong", "uae", "united arab emirates", "israel",
              "brazil", "mexico", "south africa", "philippines", "indonesia", "malaysia", "india", "us")
_COUNTRY_RX = re.compile(r"(?<![\w.])(" + "|".join(re.escape(c) for c in sorted(_COUNTRIES, key=len, reverse=True))
                         + r")(?![\w])", re.I)


def clean_label(text: str) -> str:
    """A label without its required-marker or surrounding noise."""
    t = re.sub(r"[*✱]", " ", text or "")
    t = re.sub(r"\(?\brequired\b\)?\s*$", "", t.strip(), flags=re.I)
    return re.sub(r"\s+", " ", t).strip(" :")


def key_for(label: str, field_type: str = "") -> str:
    """The standard key a question is asking for, or "" for a custom question."""
    low = clean_label(label).lower()
    if not low:
        return ""
    if field_type == "file":
        if re.search(r"resume|\bcv\b|curriculum", low):
            return "resume_file"
        if "cover" in low:
            return "cover_letter_file"
        return ""
    auth, spons = bool(_AUTH.search(low)), bool(_SPONSOR.search(low))
    if auth and spons:
        return ""                       # one question about both: don't answer half of it
    if auth:
        return "authorized_to_work"
    if spons:
        return "require_sponsorship"
    for key, rx in _RULES:
        if rx.search(low):
            return key
    return ""


def slug(label: str) -> str:
    """The key of a custom question: its label as snake_case (like the LLM's)."""
    return re.sub(r"[^a-z0-9]+", "_", clean_label(label).lower()).strip("_")[:60] or "question"


def names_other_country(label: str, home: str) -> bool:
    """True if a question names a country that is not `home`."""
    home = (home or "India").strip().lower()
    aliases = {"united states": ("us", "usa", "u.s.", "united states"), "united kingdom": ("uk", "u.k.", "united kingdom")}
    ok = {home, *aliases.get(home, ())}
    return any(m.lower() not in ok for m in _COUNTRY_RX.findall(label or ""))


def _split_name(flat: dict) -> tuple[str, str]:
    first, last = (flat.get("first_name") or "").strip(), (flat.get("last_name") or "").strip()
    if not (first and last):
        parts = (flat.get("full_name") or "").split()
        first = first or (parts[0] if parts else "")
        last = last or (" ".join(parts[1:]) if len(parts) > 1 else "")
    return first, last


def profile_value(key: str, flat: dict, field) -> str:
    """What the profile says for a standard `key` ("" if it says nothing, or
    if it cannot answer THIS question)."""
    first, last = _split_name(flat)
    if key == "first_name": return first
    if key == "last_name":  return last
    if key == "full_name":  return (flat.get("full_name") or f"{first} {last}").strip()
    if key in ("authorized_to_work", "require_sponsorship"):
        if names_other_country(field.label, flat.get("location_country") or "India"):
            return ""
        value = str(flat.get(key) or "").strip()
        if value and (field.field_type or "").lower() in ("select", "radio") and field.options:
            return fit_to_options(value, field.options) or ""
        return value
    return str(flat.get(key) or "").strip()


def resolve(fields: list, profile: dict) -> list[dict]:
    """Decide each non-file field's db_value / needs_manual from the profile.
    A standard question gets its profile value; anything else is left empty.
    needs_manual is set for a field that is MANDATORY and still empty (that is
    what turns into a question for you); an optional empty field is just left.
    Returns a profile hit ({label, source: "profile"}) for every field filled,
    which is how the review packet knows a value came from your profile."""
    flat = (profile or {}).get("_flat", profile or {})
    hits = []
    for f in fields:
        if (f.field_type or "").lower() == "file":
            f.needs_manual = False
            continue
        value = profile_value(f.field_key, flat, f) if f.field_key in STANDARD_KEYS else ""
        f.db_value = value
        f.needs_manual = bool(f.is_required and not value)
        if value:
            hits.append({"label": f.label, "field_key": f.field_key, "action": "filled",
                         "source": "profile", "policy": "always", "confidence": 1.0, "entry_id": None})
    return hits
