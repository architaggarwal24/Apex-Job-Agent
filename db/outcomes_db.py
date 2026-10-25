# db/outcomes_db.py
"""
db/outcomes_db.py — how your applications turned out, and what that says about the
auto-skip score threshold. Read-only: nothing here writes anything.

AN OUTCOME is a SUBMITTED application: a job whose stage history (application_events -
which also holds the old job_stages history, copied in by the lifecycle migration as
"migrated" events) or current stage reaches submitted or any later stage. Everything is
judged from the HISTORY, not the current stage, so a card that had a recruiter screen
and was later rejected still counts as a reply.

    reply       reached recruiter_screen, technical_round, hiring_manager or offer
    interview   reached technical_round, hiring_manager or offer
    offer       reached offer
    time to first response
                from the first move to submitted to the first LATER stage change
                (a rejection counts - it is an answer; withdrawn does not - that was you);
                the median, in days

Each rate is given overall and grouped by:

    source          jobs.source, lower-cased
    title           lower-cased, whitespace collapsed
    score_band      0-39, 40-59, 60-79, 80-100 (and "unscored" for a job with no ai_score)
    resume_variant  the label of the application kit (db/kits_db.py) current when it was
                    submitted - the kit version created at or before the submission; a kit
                    with no variant means the single resume.pdf ("default resume file")
    apply_mode      from the submitted event: "assist" (detail.via == "assist", applier/assist.py),
                    "auto" (the agent or an autonomy rule submitted it, applier/session.py),
                    "manual" (you recorded it yourself on the board)

Anything not recorded - an application from before kits existed, a submission known only
from a migrated legacy row - is "unknown", never guessed.

SAMPLE SIZES. A group with fewer than MIN_GROUP (10) outcomes reports "not enough data"
instead of each rate and the median (its counts are still shown). CALIBRATION needs at least
MIN_CALIBRATION (30) outcomes overall; then each score band's reply rate is compared with the
overall rate and a recommended auto_skip_score_threshold is returned with a one-line reason:

    walking up from the lowest band, every band that has enough data and replies at less
    than half (UNDERPERFORM_RATIO) the overall rate is cut - the recommendation is the lower
    bound of the first band that is not; the first band without enough data stops the walk,
    because nothing can be said about it. If no band is cut, the current threshold stands.

Everything is per user: OutcomeStats(user_id) reads only that user's jobs, events and kits.
"""

import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from statistics import median

from core.config import cfg
from db import hunt

MIN_GROUP = 10
MIN_CALIBRATION = 30
UNDERPERFORM_RATIO = 0.5
NOT_ENOUGH = "not enough data"
UNKNOWN = "unknown"

SUBMITTED_OR_LATER = ("submitted", "recruiter_screen", "technical_round", "hiring_manager",
                      "offer", "rejected", "withdrawn")
REPLY = ("recruiter_screen", "technical_round", "hiring_manager", "offer")
INTERVIEW = ("technical_round", "hiring_manager", "offer")
NOT_A_RESPONSE = ("submitted", "withdrawn")

BANDS = (("0-39", 0, 39), ("40-59", 40, 59), ("60-79", 60, 79), ("80-100", 80, 100))
UNSCORED = "unscored"
DIMENSIONS = ("source", "title", "score_band", "resume_variant", "apply_mode")


def score_band(score) -> str:
    if score is None:
        return UNSCORED
    for label, _lo, hi in BANDS:
        if score <= hi:
            return label
    return BANDS[-1][0]


def norm_title(title) -> str:
    t = re.sub(r"\s+", " ", str(title or "")).strip().lower()
    return t or UNKNOWN


def _detail(text) -> dict:
    try:
        d = json.loads(text or "{}")
    except ValueError:
        return {}
    return d if isinstance(d, dict) else {}


# ── one application -> one outcome record (pure) ──────────────────

def outcome_for(job: dict, events: list[dict], kits: list[dict]) -> dict | None:
    """The outcome record for one job, or None if it was never submitted.
    `events`: its application_events rows (oldest first, any kind); `kits`: its
    application_kits rows."""
    moves = [e for e in events if e["kind"] != "step"]
    reached = {e["to_stage"] for e in moves} | {job.get("stage")}
    if not reached & set(SUBMITTED_OR_LATER):
        return None
    submitted = next((e for e in moves if e["to_stage"] == "submitted"), None)

    days = None
    if submitted is not None:
        later = next((e for e in moves if e["id"] > submitted["id"] and e["to_stage"] not in NOT_A_RESPONSE), None)
        start = hunt.parse_when(submitted["at"])
        end = hunt.parse_when(later["at"]) if later else None
        if start is not None and end is not None and end >= start:
            days = (end - start).total_seconds() / 86400

    return {"source": (job.get("source") or "").strip().lower() or UNKNOWN,
            "title": norm_title(job.get("title")),
            "score_band": score_band(job.get("ai_score")),
            "resume_variant": _variant(submitted, kits),
            "apply_mode": _mode(submitted),
            "reply": bool(reached & set(REPLY)),
            "interview": bool(reached & set(INTERVIEW)),
            "offer": "offer" in reached,
            "days_to_first_response": days}


def _mode(submitted: dict | None) -> str:
    if submitted is None or submitted["kind"] != "transition":
        return UNKNOWN                                   # a migrated legacy row says nothing about how
    if _detail(submitted.get("detail")).get("via") == "assist":
        return "assist"
    if submitted["actor"] in ("agent", "autonomy"):
        return "auto"
    if submitted["actor"] == "user":
        return "manual"
    return UNKNOWN


def _variant(submitted: dict | None, kits: list[dict]) -> str:
    if submitted is None:
        return UNKNOWN
    at_submit = [k for k in kits if (k["created_at"] or "") <= (submitted["at"] or "")]
    if not at_submit:
        return UNKNOWN
    kit = max(at_submit, key=lambda k: k["version"])
    if kit["variant_id"] is None:
        return "default resume file"
    return (kit["variant_label"] or "").strip() or UNKNOWN


# ── the numbers (pure) ────────────────────────────────────────────

def _pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def group_stats(key, records: list[dict]) -> dict:
    n = len(records)
    replies = sum(r["reply"] for r in records)
    interviews = sum(r["interview"] for r in records)
    offers = sum(r["offer"] for r in records)
    times = [r["days_to_first_response"] for r in records if r["days_to_first_response"] is not None]
    out = {"key": key, "outcomes": n, "replies": replies, "interviews": interviews, "offers": offers,
           "responses_timed": len(times)}
    if n < MIN_GROUP:
        out.update(reply_rate=NOT_ENOUGH, interview_rate=NOT_ENOUGH, offer_rate=NOT_ENOUGH,
                   median_days_to_first_response=NOT_ENOUGH)
    else:
        out.update(reply_rate=_pct(replies, n), interview_rate=_pct(interviews, n), offer_rate=_pct(offers, n),
                   median_days_to_first_response=round(median(times), 1) if times else None)
    return out


def _order(dim: str, groups: dict) -> list:
    if dim == "score_band":
        order = [b[0] for b in BANDS] + [UNSCORED]
        return [k for k in order if k in groups]
    return sorted(groups, key=lambda k: (-len(groups[k]), k == UNKNOWN, k))


def calibrate(records: list[dict], current_threshold: int) -> dict:
    """The calibration block (see the module docstring)."""
    n = len(records)
    if n < MIN_CALIBRATION:
        return {"status": NOT_ENOUGH, "outcomes": n, "needed": MIN_CALIBRATION,
                "current_threshold": current_threshold, "recommended_auto_skip_score_threshold": None,
                "reason": f"Calibration needs at least {MIN_CALIBRATION} submitted applications; you have {n}.",
                "bands": []}
    overall = _pct(sum(r["reply"] for r in records), n)
    bands = []
    for label, lo, _ in BANDS:
        members = [r for r in records if r["score_band"] == label]
        s = group_stats(label, members)
        enough = s["outcomes"] >= MIN_GROUP
        bands.append({"band": label, "lower_bound": lo, "outcomes": s["outcomes"], "reply_rate": s["reply_rate"],
                      "vs_overall": round(s["reply_rate"] - overall, 1) if enough else NOT_ENOUGH,
                      "underperforms": enough and s["reply_rate"] < overall * UNDERPERFORM_RATIO})

    cut = []
    for b in bands:
        if b["reply_rate"] == NOT_ENOUGH or not b["underperforms"]:
            break
        cut.append(b)
    if not cut:
        rec = current_threshold
        reason = (f"Keep {rec}: no score band with enough data replies at less than half the overall "
                  f"reply rate of {overall}%.")
    else:
        # the first band not cut starts at the recommendation (all four cut: the top band's floor)
        rec = bands[len(cut)]["lower_bound"] if len(cut) < len(bands) else cut[-1]["lower_bound"]
        cut_records = [r for r in records if r["score_band"] in {b["band"] for b in cut}]
        verb = "Raise to" if rec > current_threshold else ("Lower to" if rec < current_threshold else "Keep")
        reason = (f"{verb} {rec}: jobs scoring {cut[0]['lower_bound']}-{BANDS[len(cut) - 1][2]} replied at "
                  f"{_pct(sum(r['reply'] for r in cut_records), len(cut_records))}% vs {overall}% overall "
                  f"(n={len(cut_records)}).")
    return {"status": "ok", "outcomes": n, "overall_reply_rate": overall, "current_threshold": current_threshold,
            "recommended_auto_skip_score_threshold": rec, "reason": reason, "bands": bands}


def summarize(records: list[dict], current_threshold: int) -> dict:
    """The whole response of GET /api/analytics/outcomes from the outcome records."""
    by = {}
    for dim in DIMENSIONS:
        groups: dict = {}
        for r in records:
            groups.setdefault(r[dim], []).append(r)
        by[dim] = [group_stats(k, groups[k]) for k in _order(dim, groups)]
    return {"outcomes": len(records), "min_group": MIN_GROUP, "overall": group_stats("overall", records),
            "by": by, "calibration": calibrate(records, current_threshold)}


# ── reading one user's data ───────────────────────────────────────

class OutcomeStats:
    def __init__(self, user_id: int, path: Path | None = None):
        if not isinstance(user_id, int) or isinstance(user_id, bool):
            raise ValueError("Outcome analytics need a user_id; there is no unscoped view")
        self.user_id = user_id
        self.path = Path(path or cfg.JOBS_DB_PATH)
        from db.jobs_db import JobsDB                      # creates the tables (and migrates old ones)
        JobsDB(path=self.path, user_id=user_id)

    def records(self) -> list[dict]:
        with closing(sqlite3.connect(str(self.path))) as c:
            c.row_factory = sqlite3.Row
            jobs = c.execute("SELECT job_id, title, source, ai_score, stage FROM jobs WHERE user_id=?",
                             (self.user_id,)).fetchall()
            events: dict[str, list] = {}
            for e in c.execute("SELECT id, job_id, at, kind, to_stage, actor, detail FROM application_events "
                               "WHERE user_id=? ORDER BY id", (self.user_id,)).fetchall():
                events.setdefault(e["job_id"], []).append(dict(e))
            kits: dict[str, list] = {}
            has_kits = c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='application_kits'").fetchone()
            if has_kits:
                for k in c.execute("SELECT job_id, version, variant_id, variant_label, created_at FROM application_kits "
                                   "WHERE user_id=?", (self.user_id,)).fetchall():
                    kits.setdefault(k["job_id"], []).append(dict(k))
        out = []
        for j in jobs:
            rec = outcome_for(dict(j), events.get(j["job_id"], []), kits.get(j["job_id"], []))
            if rec is not None:
                out.append(rec)
        return out

    def report(self, current_threshold: int) -> dict:
        return summarize(self.records(), current_threshold)
