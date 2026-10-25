# applier/adapters/runner.py
"""
Runs an application through a site adapter. The three entry points are the
adapter counterparts of applier/universal.py's run_application,
run_iteration2 and run_submit, take the same arguments (plus the adapter
first) and return the same dicts, so the Orchestrator treats their results
alike. They reuse universal.py's helpers (steps, screenshots, the answer-bank
hook, the review snapshot, validation errors), so the timeline and the review
packet read the same whichever filler ran.

What differs is HOW the form is read and filled: adapter.extract_fields() reads
it, applier/adapters/standard.py answers the standard questions from the
profile (no LLM), the answer bank answers what it knows, and adapter.fill()
fills it. A mandatory custom question nobody can answer comes back as a
missing field (a question for you), as with the universal filler.

An adapter that cannot handle the form raises UnsupportedForm, which these
functions let through UNTOUCHED - the Orchestrator turns it into a fall back
to the universal filler. It can only be raised before submit is clicked.

The review snapshot lists what was filled, plus mandatory questions. An
optional question that stayed blank is not listed: nothing was put there, so
there is no answer to review.
"""

import logging
import random
import time

from applier import universal as U
from applier.adapters import standard
from applier.adapters.base import Adapter
from applier.form_analyzer import FormAnalysis
from core.browser import detect_captcha, is_login_wall
from core.config import cfg

logger = logging.getLogger(__name__)

SETTLE        = (1.0, 2.0)        # after a page load (tests set these to (0, 0))
AFTER_SUBMIT  = (2.0, 3.0)


def _wait(span: tuple[float, float]):
    if span[1] > 0:
        time.sleep(random.uniform(*span))


def _open(adapter: Adapter, page, job: dict, emit, again: bool = False) -> dict | None:
    """Go to the form. Returns a failure dict, or None when the form is open."""
    U._log(emit, f"Opening application ({adapter.name} adapter): {job.get('url', '')}")
    try:
        page.goto(adapter.start_url(job["url"]), timeout=30000, wait_until="networkidle")
        _wait(SETTLE)
    except Exception as e:
        return U._fail(f"Navigation failed: {e}")
    U._step(emit, "opened_page", "Re-opened the job page" if again else "Opened the job page")
    if emit:
        try: emit("site_type", U._site_type(page, job))
        except Exception: pass
    if detect_captcha(page):
        return U._fail("CAPTCHA detected — skipping")
    if is_login_wall(page):
        return U._fail("Login wall — page requires authentication and no login was detected")
    return None


def _read(adapter: Adapter, page, job: dict, profile: dict, answers, emit):
    """Extract the form, answer the standard questions, consult the answer bank.
    Returns (analysis, hits_for_the_snapshot). May raise UnsupportedForm."""
    fields = adapter.extract_fields(page)
    profile_hits = standard.resolve(fields, profile)
    analysis = FormAnalysis(page_url=U._page_url(page), job_id=job["job_id"], fields=fields,
                            has_file_upload=any((f.field_type or "").lower() == "file" for f in fields))
    bank_hits = U._apply_answers(answers, analysis, emit)
    U._log(emit, f"Found {len(fields)} fields — {len(analysis.fillable)} fillable, {len(analysis.missing)} missing")
    return analysis, profile_hits + bank_hits          # a bank answer outranks a profile fact for the same field


def _snapshot(analysis: FormAnalysis, hits: list[dict], only_keys=None) -> list[dict]:
    """The review snapshot (universal._snapshot) of what this pass put on the page."""
    keep = [f for f in analysis.fields
            if (f.db_value or f.is_required or f.needs_manual) and (only_keys is None or f.field_key in only_keys)]
    return U._snapshot(FormAnalysis(page_url=analysis.page_url, job_id=analysis.job_id, fields=keep), hits, 0)


def _fill_and_report(adapter: Adapter, page, analysis: FormAnalysis, emit, resume_path=None):
    filled, failed = adapter.fill(page, analysis.fields, resume_path or cfg.RESUME_PATH)
    U._report_filled(emit, filled, failed)
    return filled, failed


def run_application(adapter: Adapter, page, job: dict, profile: dict, emit=None, answers=None,
                    resume_path=None) -> dict:
    """First pass: open the form, fill it, screenshot it. See universal.run_application."""
    failure = _open(adapter, page, job, emit)
    if failure:
        return failure
    U._report_page(page, emit, 0)
    analysis, hits = _read(adapter, page, job, profile, answers, emit)
    _fill_and_report(adapter, page, analysis, emit, resume_path)

    screenshots = []
    shot = U._screenshot(page, job["job_id"], "pre_submit")
    if shot:
        screenshots.append(shot)
    return {"success": True, "screenshots": screenshots, "missing_fields": list(analysis.missing),
            "analysis": analysis, "error": "", "needs_review": True,
            "review": {"fields": _snapshot(analysis, hits), "url": U._page_url(page)}}


def run_iteration2(adapter: Adapter, page, job: dict, profile: dict, missing_keys: list[str],
                   emit=None, answers=None, resume_path=None) -> dict:
    """You have answered the questions the first pass stopped on. The form is
    opened afresh, so everything is filled again (a complete form to screenshot);
    only the answered questions go into the review snapshot, which is folded
    into the packet the first pass made. See universal.run_iteration2."""
    failure = _open(adapter, page, job, emit, again=True)
    if failure:
        return failure
    U._report_page(page, emit, 0)
    analysis, hits = _read(adapter, page, job, profile, answers, emit)
    _fill_and_report(adapter, page, analysis, emit, resume_path)

    screenshots = []
    shot = U._screenshot(page, job["job_id"], "pre_submit_final")
    if shot:
        screenshots.append(shot)
    return {"success": True, "screenshots": screenshots, "missing_fields": [], "error": "",
            "needs_review": True,
            "review": {"fields": _snapshot(analysis, hits, only_keys=set(missing_keys)), "url": U._page_url(page)}}


def run_submit(adapter: Adapter, page, job: dict, profile: dict, corrections=None, emit=None,
               dry_run: bool = False, answers=None, resume_path=None) -> dict:
    """Open, fill and submit. With dry_run the form is filled and the submit
    button only looked for, never clicked. See universal.run_submit."""
    failure = _open(adapter, page, job, emit, again=True)
    if failure:
        return failure
    U._report_page(page, emit, 0)
    analysis, _ = _read(adapter, page, job, profile, answers, emit)
    _fill_and_report(adapter, page, analysis, emit, resume_path)

    screenshots = []
    if dry_run:
        shot = U._screenshot(page, job["job_id"], "dry_run_final")
        if shot:
            screenshots.append(shot)
        found = adapter.submit(page, dry_run=True)          # UnsupportedForm if there is no button
        U._log(emit, "DRY RUN — submit button found but NOT clicked. No application was sent.", warn=True)
        return {"success": found, "screenshots": screenshots, "error": "", "dry_run": True}

    adapter.submit(page, dry_run=False)                      # the one irreversible step
    _wait(AFTER_SUBMIT)
    shot = U._screenshot(page, job["job_id"], "submitted")
    if shot:
        screenshots.append(shot)
    errors = U._validation_errors(page)
    if errors:
        return {"success": False, "screenshots": screenshots, "error": U._validation_message(errors),
                "dry_run": False}
    return {"success": True, "screenshots": screenshots, "error": "", "dry_run": False}
