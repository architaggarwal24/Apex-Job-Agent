# applier/pack.py
"""
The answer pack: everything the agent has prepared for ONE application, as an
ordered list of copy-ready fields, so you can apply on the site yourself
(assist mode, applier/apply_mode.py) by copying from top to bottom.

    GET /api/applications/{id}/pack  ->  build_pack()

The pack is built when asked, from what is current - the profile, the answer
bank and the application's kit - so an answer you have just saved is in it.
Nothing is stored and no browser is involved.

ORDER. Three groups, always in this order, numbered 1..n across all of them:

  profile      your facts, in the order a form asks for them: name, contact,
               location, links, then professional, education, work
               authorisation. Only facts you have filled in; the sensitive
               equal-opportunity ones (gender, date of birth, veteran,
               disability, ethnicity) are never put in a pack.
  answer_bank  your saved answers to screening questions: ones saved for THIS
               company first, then the global ones, each alphabetical by
               question (so a question is easy to find), at most MAX_ANSWERS.
  kit          the resume to upload (its file name), the 3-sentence tailored
               summary and the cover letter, as the kit stands now.

Every field is {order, group, key, label, value, kind}: `value` is exactly what
to paste, `kind` is "text" (paste it) or "file" (the resume: upload that file).

PER USER. The job, the bank, the kit and the resumes are the job owner's and
looked up through that user; the profile's facts are only ever offered to the
profile's owner (owner_profile()). A group that cannot be read is left out and
logged - a pack is still returned.
"""

import logging
from urllib.parse import urlsplit

from db.answer_bank_db import PROFILE_OWNER_ID, AnswerBank, normalize_company
from db.kits_db import KitStore
from db.profile_db import STANDARD_FIELDS
from db.resumes_db import ResumeStore

logger = logging.getLogger(__name__)

MAX_ANSWERS = 100

# The profile facts a pack carries, in form order.
PROFILE_KEYS = (
    "full_name", "first_name", "last_name", "email", "phone", "phone_alternate",
    "location_city", "location_state", "location_country", "location_pincode",
    "linkedin_url", "github_url", "portfolio_url",
    "current_role", "experience_years", "notice_period_label", "notice_period_days",
    "current_ctc_lpa", "expected_ctc_lpa", "salary_expectation",
    "willing_to_relocate", "work_mode_preference", "availability",
    "highest_degree", "degree_field", "university_name", "graduation_year", "graduation_score",
    "tenth_score", "twelfth_score",
    "authorized_to_work", "require_sponsorship",
)
_PROFILE_LABELS = {key: label for key, _d, _c, _t, label in STANDARD_FIELDS}


def owner_profile(user_id: int) -> dict | None:
    """The profile summary for `user_id`, or None unless they are the profile's
    owner: the profile belongs to the first account only."""
    if user_id != PROFILE_OWNER_ID:
        return None
    from db.profile_db import ProfileDB
    return ProfileDB().summary()


def safe_url(url) -> str:
    """The job's URL if it is a plain http(s) link, else "" (it is meant to be opened)."""
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError:
        return ""
    return str(url).strip() if parts.scheme in ("http", "https") and parts.netloc else ""


def _field(group: str, key: str, label: str, value: str, kind: str = "text") -> dict:
    return {"group": group, "key": key, "label": label, "value": value, "kind": kind}


def _profile_group(profile: dict | None) -> list[dict]:
    flat = (profile or {}).get("_flat") or {}
    out = []
    for key in PROFILE_KEYS:
        value = str(flat.get(key) or "").strip()
        if value:
            out.append(_field("profile", key, _PROFILE_LABELS.get(key, key), value))
    return out


def _answer_group(job: dict, uid: int, path) -> list[dict]:
    company = normalize_company(job.get("company", ""))
    mine = [e for e in AnswerBank(uid, path).entries(limit=2000)
            if (e["answer"] or "").strip()
            and (e["scope"] == "global"
                 or (e["scope"] == "company" and normalize_company(e["company"]) == company))]
    mine.sort(key=lambda e: (e["scope"] != "company", e["question_raw"].casefold(), e["id"]))
    return [_field("answer_bank", f"answer_{e['id']}", e["question_raw"], e["answer"].strip())
            | {"scope": e["scope"], "entry_id": e["id"]} for e in mine[:MAX_ANSWERS]]


def _kit_group(job: dict, uid: int, path, root) -> tuple[list[dict], dict | None]:
    kit = KitStore(uid, path).current(job["job_id"])
    store = ResumeStore(uid, path, root)
    variant = store.get(kit["variant_id"]) if kit and kit["variant_id"] is not None else None
    variant = variant or store.default()          # no kit (or its resume is gone): what the fillers would use
    out = []
    if variant:
        out.append(_field("kit", "resume", f"Resume to upload ({variant['label']})", variant["filename"], "file")
                   | {"variant_id": variant["id"]})
    if kit and kit["summary"]:
        out.append(_field("kit", "summary", "Tailored summary", kit["summary"]))
    if kit and kit["cover_letter"]:
        out.append(_field("kit", "cover_letter", "Cover letter", kit["cover_letter"]))
    meta = None
    if kit:
        meta = {"version": kit["version"], "status": kit["status"], "selection": kit["selection"],
                "reason": kit["reason"], "created_by": kit["created_by"]}
    return out, meta


def build_pack(job: dict, *, path=None, profile: dict | None = None, root=None) -> dict:
    """The pack for `job` (a dict from JobsDB.get_by_id, so it has user_id).
    `profile` is the profile summary to draw on - None leaves the profile group
    empty (see owner_profile). Never raises for a missing group."""
    uid = job.get("user_id") or PROFILE_OWNER_ID
    groups, kit_meta = [], None
    for name, make in (("profile", lambda: _profile_group(profile)),
                       ("answer bank", lambda: _answer_group(job, uid, path)),
                       ("kit", None)):
        try:
            if name == "kit":
                fields, kit_meta = _kit_group(job, uid, path, root)
            else:
                fields = make()
        except Exception:
            logger.exception(f"[Pack] could not read the {name}; leaving it out")
            fields = []
        groups.append(fields)
    fields = [{"order": i, **f} for i, f in enumerate((f for g in groups for f in g), 1)]
    return {"job_id": job["job_id"], "title": job.get("title", ""), "company": job.get("company", ""),
            "url": safe_url(job.get("url")), "stage": job.get("stage", ""), "fields": fields,
            "counts": {g: sum(1 for f in fields if f["group"] == g) for g in ("profile", "answer_bank", "kit")},
            "kit": kit_meta}
