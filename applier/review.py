# applier/review.py
"""
The structured review packet: after the agent fills a form it records, per
application, every field it saw - what it put there, WHERE that came from, how
sure it is, and whether you should look twice - so review is a list you can
check and correct, not a screenshot to squint at.

A packet (stored by db/review_db.py) is

    {job_id, url, resume, screenshots, built_at, updated_at,
     fields: [{id, label, type, value, source, confidence, flagged,
               reason, required, options}]}

source is who is responsible for the value:
    user         you answered it for this application (or edited it in review)
    answer_bank  one of your saved answers (db/answer_bank_db.py)
    profile      a fact you put in your profile
    ai           the analyser's own guess - or, for a field with no value at
                 all, the analyser's "I couldn't find one" (confidence 0)

Default confidence: user 1.0; answer_bank the match's own confidence (1.0
exact); profile 0.95; ai 0.6, or 0.4 for long free text; no value 0.0.

A field is FLAGGED when it has a value and confidence < 0.7, or it is required
but empty, or it is AI-written free text. `reason` says which, in words.

Two things downstream depend on this module:
  * the form code hands over raw per-page snapshots (universal.py) and
    build_fields() turns them into packet fields;
  * approving a review submits THESE values, not a fresh re-analysis that might
    differ: reviewed_answers() is what Orchestrator.submit fills from.

Plain Python, no browser, no database.
"""

import hashlib
import re
from datetime import datetime

from applier.answers import LONG_ANSWER_CHARS, fit_to_options
from db.answer_bank_db import MAX_ANSWER_CHARS, normalize_question

SOURCES = ("profile", "answer_bank", "ai", "user")

CONF_USER    = 1.0
CONF_PROFILE = 0.95
CONF_AI      = 0.6
CONF_AI_LONG = 0.4          # an AI-written essay is the least trustworthy thing on a form
FLAG_BELOW   = 0.7

PACKET_VERSION = 1


class NoReviewPacket(LookupError):
    """There is no review packet for this application. (Not a KeyError: a
    missing FIELD is, and KeyError is itself a LookupError, so the two must
    be caught in that order.)"""


# ── confidence and flags ──────────────────────────────────────────

def is_free_text(field_type: str, value: str) -> bool:
    """A textarea, or any answer long enough to be prose (the same bar the
    answer bank uses to scope an answer to one company)."""
    return (field_type or "").lower() == "textarea" or len((value or "").strip()) >= LONG_ANSWER_CHARS


def default_confidence(source: str, field_type: str = "", value: str = "",
                       matched: float | None = None) -> float:
    """The confidence a value gets by default. `matched` is the answer bank's
    own match confidence (1.0 exact, the similarity ratio if fuzzy)."""
    if not (value or "").strip():
        return 0.0
    if source == "user":
        return CONF_USER
    if source == "profile":
        return CONF_PROFILE
    if source == "answer_bank":
        return 1.0 if matched is None else float(matched)
    if source == "ai":
        return CONF_AI_LONG if is_free_text(field_type, value) else CONF_AI
    raise ValueError(f"unknown source {source!r}")


def flag_reasons(source: str, confidence: float, value: str, required: bool, field_type: str = "") -> list[str]:
    """Why a field needs a second look; [] if it doesn't. Confidence only
    counts for a field that has a value (an empty optional field is simply
    empty; an empty required one is flagged for being required)."""
    has_value = bool((value or "").strip())
    reasons = []
    if required and not has_value:
        reasons.append("required but empty")
    if has_value and source == "ai" and is_free_text(field_type, value):
        reasons.append("AI-written free text")
    if has_value and confidence < FLAG_BELOW:
        reasons.append(f"low confidence ({confidence:.2f})")
    return reasons


def _finish(field: dict) -> dict:
    reasons = flag_reasons(field["source"], field["confidence"], field["value"],
                           field["required"], field["type"])
    field["flagged"], field["reason"] = bool(reasons), "; ".join(reasons)
    return field


# ── building ──────────────────────────────────────────────────────

def _slug(label: str) -> str:
    norm = normalize_question(label)
    slug = re.sub(r"[^a-z0-9]+", "-", norm).strip("-")
    return slug[:40] or "field-" + hashlib.sha1((label or "").encode("utf-8")).hexdigest()[:8]


def _unique_id(label: str, taken: set[str]) -> str:
    base, n, fid = _slug(label), 1, _slug(label)
    while fid in taken:
        n += 1
        fid = f"{base}-{n}"
    taken.add(fid)
    return fid


def _provenance(snap: dict, answered: dict) -> tuple[str, float]:
    value = (snap.get("value") or "").strip()
    hit = snap.get("hit")
    if hit:
        if hit["source"] == "application":
            return "user", default_confidence("user", snap["type"], value)
        if hit["source"] == "profile":
            return "profile", default_confidence("profile", snap["type"], value)
        return "answer_bank", default_confidence("answer_bank", snap["type"], value, hit.get("confidence"))
    if value:
        key = snap.get("field_key") or ""
        if key in answered and str(answered[key]).strip() == value:
            return "profile", default_confidence("profile", snap["type"], value)
        return "ai", default_confidence("ai", snap["type"], value)
    return "ai", 0.0


def build_fields(snapshots: list[dict], answered: dict | None = None,
                 taken: set[str] | None = None) -> list[dict]:
    """Packet fields from the form code's snapshots. A snapshot is
    {label, field_key, type, value, options, required, hit}, where `hit` says
    which answer-bank/application answer filled it (or None). `answered` is
    the profile facts the person really provided ({key: value}): a value that
    came from one of those is 'profile'; any other value the analyser chose is
    'ai'."""
    answered, taken = answered or {}, set() if taken is None else taken
    fields = []
    for snap in snapshots:
        source, confidence = _provenance(snap, answered)
        fields.append(_finish({
            "id": _unique_id(snap["label"], taken), "label": snap["label"], "type": snap["type"],
            "value": (snap.get("value") or "").strip(), "source": source,
            "confidence": round(confidence, 2), "flagged": False, "reason": "",
            "required": bool(snap.get("required")), "options": list(snap.get("options") or [])}))
    return fields


def build_packet(job_id: str, url: str, resume: str, screenshots: list[str], fields: list[dict]) -> dict:
    now = datetime.now().isoformat()
    return {"version": PACKET_VERSION, "job_id": job_id, "url": url, "resume": resume,
            "screenshots": list(screenshots), "fields": fields, "built_at": now, "updated_at": now}


def merge_fields(old: list[dict], new: list[dict]) -> list[dict]:
    """Fold a later pass into the packet. A second pass only re-fills the
    fields you answered, so each new field REPLACES the old one with the same
    label (keeping its id and place); anything not seen before is appended."""
    out = [dict(f) for f in old]
    claimed: set[int] = set()
    taken = {f["id"] for f in out}
    for f in new:
        norm = normalize_question(f["label"])
        match = next((i for i, o in enumerate(out)
                      if i not in claimed and normalize_question(o["label"]) == norm), None)
        if match is None:
            f = dict(f)
            if f["id"] in taken:
                f["id"] = _unique_id(f["label"], taken)
            taken.add(f["id"])
            out.append(f)
            claimed.add(len(out) - 1)
        else:
            out[match] = {**f, "id": out[match]["id"]}
            claimed.add(match)
    return out


# ── editing and submitting ────────────────────────────────────────

def find_field(packet: dict, field_id: str) -> dict:
    for f in packet["fields"]:
        if f["id"] == field_id:
            return f
    raise KeyError(field_id)


def apply_edit(packet: dict, field_id: str, value: str) -> dict:
    """You corrected a value: it is now yours (source user, confidence 1.0, or
    0.0 if you cleared it) and its flags are re-worked out. A select/radio
    value must be one of its options. KeyError if there is no such field;
    ValueError for a value that can't go there. Returns the updated field."""
    field = find_field(packet, field_id)
    value = (value or "").strip()
    if len(value) > MAX_ANSWER_CHARS:
        raise ValueError(f"value is too long ({MAX_ANSWER_CHARS} characters at most)")
    if value and field["type"].lower() in ("select", "radio") and field["options"]:
        fitted = fit_to_options(value, field["options"])
        if fitted is None:
            raise ValueError("value must be one of: " + ", ".join(map(str, field["options"])))
        value = fitted
    field["value"], field["source"] = value, "user"
    field["confidence"] = default_confidence("user", field["type"], value)
    _finish(field)
    packet["updated_at"] = datetime.now().isoformat()
    return field


def reviewed_answers(packet: dict | None, only_user: bool = False) -> dict[str, str]:
    """{normalised question: value} for the packet's fields: what submit must
    fill. only_user=True keeps just the values YOU set, for the older flow in
    which free-text corrections re-derive everything else from the profile."""
    out = {}
    for f in (packet or {}).get("fields", []):
        norm = normalize_question(f["label"])
        if (not norm or not f["value"] or f["type"].lower() in ("file", "upload")
                or (only_user and f["source"] != "user")):
            continue
        out[norm] = f["value"]
    return out


def public(packet: dict) -> dict:
    """The packet as the API returns it (a copy, with the flagged count)."""
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in packet.items()}
    out["fields"] = [dict(f) for f in packet["fields"]]
    out["flagged"] = sum(1 for f in packet["fields"] if f["flagged"])
    return out


# ── the operations behind the API (plain functions, so they test without HTTP) ──

def edit_and_remember(store, bank, job: dict, field_id: str, value: str, mode: str = "auto"):
    """Correct one field of a job's packet and update the answer bank per the
    remember mode (applier/answers.py: auto | global | company | once).
    `store` is the user's ReviewStore, `bank` their AnswerBank, `job` the job
    row (its company scopes a company answer).

    Returns (packet, field, remembered) where remembered is None for 'once',
    for a cleared value, or for a label the bank can't store, else
    {id, scope, company}. NoReviewPacket: no packet. KeyError: no such field.
    ValueError: a value that can't go there (bad option, too long)."""
    from applier.answers import remember_answer
    result = store.update(job["job_id"], lambda packet: dict(apply_edit(packet, field_id, value)))
    if result is None:
        raise NoReviewPacket("No review packet for this application")
    packet, field = result
    remembered = None
    if field["value"] and mode != "once":
        try:
            entry = remember_answer(bank, field["label"], field["value"], job.get("company", ""),
                                    mode, field["type"])
            remembered = {"id": entry["id"], "scope": entry["scope"], "company": entry["company"]}
        except ValueError:
            remembered = None                     # e.g. a label with no meaningful words
    return packet, field, remembered


def reject_application(jobs, job_id: str, reason: str) -> None:
    """Reject in review: ready_for_review -> skipped, the reason on the timeline
    and on the apply session. IllegalTransition if it is not in review."""
    jobs.transition(job_id, "skipped", actor="user", note=reason,
                    detail={"review": "rejected"}, from_stages=("ready_for_review",))
    from applier.session import ApplicationSession
    session = ApplicationSession.load(job_id)
    if session:
        session.skip(reason)
    from applier import autonomy
    autonomy.record_rejection(jobs, job_id)         # a rejection counts against the site type's success rate
