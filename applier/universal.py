"""
applier/universal.py — Universal application handler.

Works on ANY job URL regardless of platform:
  - LinkedIn Easy Apply (modal)
  - Indeed Easy Apply (smartapply.indeed.com)
  - Greenhouse, Lever, Workday, iCIMS, SmartRecruiters
  - Company career pages with direct forms
  - Any URL with a fillable application form

Flow per job:
  1. Open URL → detect page type
  2. Click "Apply" button if on a listing page
  3. Handle login walls, new tabs, modals
  4. LLM analyzes all form fields
  5. Fill known fields from ProfileDB
  6. Flag missing fields for user input
  7. Screenshot → user reviews → submit
"""

import logging
import random
import re
import time
from pathlib import Path

from applier.answers import summarize

from core.config import cfg
from core.browser import detect_captcha, is_login_wall, human_delay, wait_for_manual_login
from applier.form_analyzer import extract_fields, analyze, FormAnalysis
from applier.form_filler import execute, click_next, click_submit
from applier.site_type import detect_site_type

logger = logging.getLogger(__name__)

# Platforms where clicking "Apply" *is* the final submission when no form
# follows — there's no separate fill-then-submit step like LinkedIn/Indeed
# Easy Apply. Naukri is the known case: a registered user's profile IS the
# application, so most listings are single-click apply with nothing to
# review afterwards. For these platforms:
#   - run_application() (iteration 1) never clicks Apply. It only confirms
#     the button exists and goes straight to REVIEW, so the human-review-
#     before-anything-happens guarantee that LinkedIn/Indeed jobs get is
#     preserved here too.
#   - The actual click only happens in run_submit(), gated by dry_run
#     exactly like a real submit button would be.
# If Naukri instead shows a follow-up questionnaire after the click (its
# chat-style screening widget), that's a different, unsupported flow —
# see _is_chatbot_flow() — and run_submit() fails loudly rather than
# guessing at answers or leaving the session in an unclear state.
CLICK_IS_TERMINAL = {"naukri"}

# Apply button selectors — tried in priority order
APPLY_BUTTON_SELECTORS = [
    # LinkedIn Easy Apply
    "button.jobs-apply-button",
    "button[data-control-name*='apply']",
    "button:has-text('Apply')",
    # Indeed Easy Apply
    "button[data-testid='indeedApplyButton']",
    "button.ia-IndeedApplyButton",
    "span.ia-IndeedApplyButton",
    "button:has-text('Apply with Indeed')",
    # Company career pages
    "a:has-text('Apply Now')",   "button:has-text('Apply Now')",
    "a:has-text('Apply now')",   "button:has-text('Apply now')",
    "a:has-text('Apply online')", "button:has-text('Apply online')",
    "a[href*='/apply']",
    # ATS-specific
    "button[id*='apply']",       "a[id*='apply']",
    "[class*='apply-btn']",      "[class*='applyButton']",
    "[data-qa='btn-apply']",
]


def run_application(
    page,
    job: dict,
    profile: dict,
    emit=None,
    answers=None,
    resume_path=None,
) -> dict:
    """
    Run the full application flow for a job.

    This handles iteration 1 only (fill + screenshot).
    Iteration 2 (missing fields refill) and submit are called
    separately by the session orchestrator.

    Returns:
        {
          "success": bool,
          "screenshots": [path, ...],
          "missing_fields": [FormField, ...],
          "analysis": FormAnalysis | None,
          "error": str,
          "needs_review": bool,
        }
    """
    screenshots   = []
    all_missing   = []
    last_analysis = None
    review_fields = []

    _log(emit, f"Opening application: {job.get('url','')}")

    # ── Step 1: Navigate ──────────────────────────────────────────
    try:
        page.goto(job["url"], timeout=30000, wait_until="networkidle")
        time.sleep(random.uniform(1.5, 3.0))
    except Exception as e:
        return _fail(f"Navigation failed: {e}")
    _step(emit, "opened_page", "Opened the job page")
    if emit:                                   # not a timeline step: the Orchestrator keeps it with the session
        try: emit("site_type", _site_type(page, job))
        except Exception: pass

    if detect_captcha(page):
        return _fail("CAPTCHA detected — skipping")

    # ── Click-is-terminal platforms (Naukri): never click Apply here ──
    if job.get("source") in CLICK_IS_TERMINAL:
        if not _has_apply_button(page):
            return _fail("No Apply button found — job may already be applied "
                         "to, expired, or removed")
        _step(emit, "found_apply_button", "Found the Apply button")
        ss = _screenshot(page, job["job_id"], "pre_apply")
        if ss:
            screenshots.append(ss)
        _log(emit, "One-click apply platform — nothing filled or submitted yet. "
                   "Review the listing, then Submit to actually apply.")
        return {
            "success": True, "screenshots": screenshots, "missing_fields": [],
            "analysis": None, "error": "", "needs_review": True,
        }

    if is_login_wall(page):
        # Try waiting for manual login instead of failing immediately —
        # the user may need to log in to this specific site (e.g. company ATS)
        _log(emit, "Login required — please log in in the browser window (90s)", warn=True)

        def _check(p):
            return not is_login_wall(p) and _page_has_form(p)

        if wait_for_manual_login(page, _check, "this site", timeout=90):
            _log(emit, "Login detected — continuing")
        else:
            return _fail("Login wall — page requires authentication and no login was detected")

    # ── Step 2: Click apply button if on listing page ─────────────
    if not _page_has_form(page):
        _log(emit, "Looking for apply button on listing page")
        clicked, page = _click_apply_button(page, emit)
        if not clicked:
            _log(emit, "No apply button found — treating current page as form", warn=True)

    # ── Step 3: Wait for form to be ready ─────────────────────────
    _wait_for_form(page)

    if detect_captcha(page):
        return _fail("CAPTCHA after clicking apply")

    # ── Step 4: Multi-page form loop ──────────────────────────────
    for page_num in range(8):
        _log(emit, f"Analyzing form page {page_num + 1}")

        form_text = extract_fields(page)
        if not form_text.strip():
            _log(emit, "No form fields found on this page")
            break
        _report_page(page, emit, page_num)

        analysis = analyze(
            page_html=page_text_from_extraction(form_text),
            page_url=  page.url,
            job_id=    job["job_id"],
            job_title= job["title"],
            company=   job["company"],
            profile=   profile,
        )
        hits = _apply_answers(answers, analysis, emit)
        last_analysis = analysis

        _log(emit, f"Found {len(analysis.fields)} fields — "
                   f"{len(analysis.fillable)} fillable, {len(analysis.missing)} missing")

        filled, failed = execute(page, analysis,
                                 resume_path=resume_path or cfg.RESUME_PATH)
        _report_filled(emit, filled, failed)
        review_fields.extend(_snapshot(analysis, hits, page_num))

        all_missing.extend(analysis.missing)

        # Screenshot after filling each page
        ss = _screenshot(page, job["job_id"], f"iter1_p{page_num+1}")
        if ss:
            screenshots.append(ss)

        # Try to advance to next page
        if not click_next(page, analysis):
            break
        errors = _validation_errors(page)
        if errors:
            return _fail(_validation_message(errors))

        time.sleep(random.uniform(1.0, 2.0))

    # ── Step 5: Final screenshot ──────────────────────────────────
    # Check if we're on a review/confirmation page
    is_review = _is_review_page(page)
    final_ss  = _screenshot(page, job["job_id"],
                            "review" if is_review else "pre_submit")
    if final_ss and final_ss not in screenshots:
        screenshots.append(final_ss)

    return {
        "success":       True,
        "screenshots":   screenshots,
        "missing_fields":all_missing,
        "analysis":      last_analysis,
        "error":         "",
        "needs_review":  True,
        "review":        {"fields": review_fields, "url": _page_url(page)},
    }


def run_iteration2(
    page,
    job: dict,
    profile: dict,
    missing_keys: list[str],
    emit=None,
    answers=None,
    resume_path=None,
) -> dict:
    """
    Re-open the form and fill only previously-missing fields.
    Called after user has provided missing values.
    """
    screenshots = []

    _log(emit, "Re-opening form for iteration 2 (filling missing fields)")

    try:
        page.goto(job["url"], timeout=30000, wait_until="networkidle")
        time.sleep(random.uniform(1.5, 3.0))
    except Exception as e:
        return _fail(f"Navigation failed: {e}")
    _step(emit, "opened_page", "Re-opened the job page")

    # Defense in depth: run_application() never reports missing fields for
    # CLICK_IS_TERMINAL platforms, so this shouldn't normally be reachable
    # for Naukri — but guard it anyway rather than falling through to
    # _click_apply_button() below, which would submit for real.
    if job.get("source") in CLICK_IS_TERMINAL:
        _log(emit, "Nothing to re-fill for this platform — proceeding to review", warn=True)
        return {
            "success": True, "screenshots": [], "missing_fields": [], "error": "",
            "needs_review": True,
        }

    if not _page_has_form(page):
        _click_apply_button(page, emit)

    _wait_for_form(page)

    review_fields = []
    for page_num in range(8):
        _log(emit, f"Re-filling page {page_num + 1}")
        form_text = extract_fields(page)
        if not form_text.strip():
            break
        _report_page(page, emit, page_num)

        analysis = analyze(
            page_html=page_text_from_extraction(form_text),
            page_url=  page.url,
            job_id=    job["job_id"],
            job_title= job["title"],
            company=   job["company"],
            profile=   profile,
            only_keys= missing_keys,
        )
        hits = _apply_answers(answers, analysis, emit)

        filled, failed = execute(page, analysis, resume_path=resume_path or cfg.RESUME_PATH)
        _report_filled(emit, filled, failed)
        review_fields.extend(_snapshot(analysis, hits, page_num))

        ss = _screenshot(page, job["job_id"], f"iter2_p{page_num+1}")
        if ss:
            screenshots.append(ss)

        if not click_next(page, analysis):
            break
        errors = _validation_errors(page)
        if errors:
            return _fail(_validation_message(errors))
        time.sleep(random.uniform(1.0, 2.0))

    # Final screenshot
    is_review = _is_review_page(page)
    final_ss  = _screenshot(page, job["job_id"],
                            "review_final" if is_review else "pre_submit_final")
    if final_ss and final_ss not in screenshots:
        screenshots.append(final_ss)

    return {
        "success": True, "screenshots": screenshots,
        "missing_fields": [], "error": "", "needs_review": True,
        "review": {"fields": review_fields, "url": _page_url(page)},
    }


def run_submit(
    page,
    job: dict,
    profile: dict,
    corrections: list[str] | None = None,
    emit=None,
    dry_run: bool = False,
    answers=None,
    resume_path=None,
) -> dict:
    """
    Final submit: re-open form, fill everything, click Submit.

    Args:
        dry_run: If True, fills the form and takes a screenshot exactly
                 as normal, but does NOT click the submit button — and
                 reports success based on whether a submit button was
                 *found*, not whether it was clicked. This lets a user
                 verify the full flow end-to-end without sending a real
                 application.
    """
    screenshots = []
    _log(emit, "Re-opening form for final submission")

    try:
        page.goto(job["url"], timeout=30000, wait_until="networkidle")
        time.sleep(random.uniform(1.5, 3.0))
    except Exception as e:
        return _fail(f"Navigation failed: {e}")
    _step(emit, "opened_page", "Re-opened the job page")

    # ── Click-is-terminal platforms (Naukri): the button click IS the
    # submission. Handle it as its own path rather than falling into the
    # form-fill loop below, which assumes a form appears after clicking.
    if job.get("source") in CLICK_IS_TERMINAL:
        return _run_submit_click_only(page, job, emit, dry_run)

    if not _page_has_form(page):
        _click_apply_button(page, emit)

    _wait_for_form(page)

    submitted = False
    for page_num in range(8):
        form_text = extract_fields(page)
        if not form_text.strip():
            break
        _report_page(page, emit, page_num)

        analysis = analyze(
            page_html=page_text_from_extraction(form_text),
            page_url=  page.url,
            job_id=    job["job_id"],
            job_title= job["title"],
            company=   job["company"],
            profile=   profile,
        )
        _apply_answers(answers, analysis, emit)

        filled, failed = execute(page, analysis, resume_path=resume_path or cfg.RESUME_PATH)
        _report_filled(emit, filled, failed)
        _log(emit, f"Filled form page {page_num + 1}")

        if dry_run:
            ss = _screenshot(page, job["job_id"], "dry_run_final")
            if ss:
                screenshots.append(ss)
            found_submit = _has_submit_button(page, analysis)
            _log(emit, "DRY RUN — submit button "
                       f"{'found but NOT clicked' if found_submit else 'not found'}. "
                       "No application was sent.", warn=True)
            return {
                "success":     found_submit,
                "screenshots": screenshots,
                "error":       "" if found_submit else "Could not find submit button (dry run)",
                "dry_run":     True,
            }

        if click_submit(page, analysis):
            time.sleep(3)
            ss = _screenshot(page, job["job_id"], "submitted")
            if ss:
                screenshots.append(ss)
            submitted = True
            break

        if not click_next(page, analysis):
            break
        errors = _validation_errors(page)
        if errors:
            return _fail(_validation_message(errors))
        time.sleep(random.uniform(1.0, 2.0))

    return {
        "success":     submitted,
        "screenshots": screenshots,
        "error":       "" if submitted else "Could not find submit button",
        "dry_run":     False,
    }


def _has_apply_button(page) -> bool:
    """Check whether an Apply button exists/is visible without clicking it —
    used for click-is-terminal platforms (see CLICK_IS_TERMINAL)."""
    for sel in APPLY_BUTTON_SELECTORS:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                return True
        except Exception:
            pass
    return False


def _is_chatbot_flow(page) -> bool:
    """
    Best-effort detection of Naukri's chat-style screening questionnaire,
    which can appear after clicking Apply instead of a normal confirmation.
    This isn't a standard <form> — extract_fields()/analyze() don't
    understand a turn-by-turn chat widget, so we detect it and fail loudly
    rather than mis-clicking through an unanswered screening flow.
    """
    try:
        if page.query_selector(
            "[class*='chatbot' i], [class*='chat-container' i], "
            "[class*='webBotDrawer' i], [id*='chatbot' i]"
        ):
            return True
    except Exception:
        pass
    return False


def _run_submit_click_only(page, job: dict, emit, dry_run: bool) -> dict:
    """
    Submit path for CLICK_IS_TERMINAL platforms (Naukri): there is no form
    to fill — the Apply button click itself is the entire application.
    dry_run confirms the button exists without clicking, exactly like
    _has_submit_button() does for form-based platforms.
    """
    if not _has_apply_button(page):
        return _fail("No Apply button found — job may already be applied "
                     "to, expired, or removed")

    if dry_run:
        ss = _screenshot(page, job["job_id"], "dry_run_final")
        _log(emit, "DRY RUN — Apply button found but NOT clicked. "
                   "No application was sent.", warn=True)
        return {
            "success": True, "screenshots": [ss] if ss else [],
            "error": "", "dry_run": True,
        }

    for sel in APPLY_BUTTON_SELECTORS:
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                break
        except Exception:
            continue

    time.sleep(3)
    ss = _screenshot(page, job["job_id"], "submitted")
    screenshots = [ss] if ss else []

    if _is_chatbot_flow(page):
        return {
            "success": False, "screenshots": screenshots,
            "error": "Naukri opened a follow-up screening chat after Apply — "
                     "this isn't automated yet. Please finish answering it "
                     "manually in the browser window; the job has NOT been "
                     "marked as applied.",
            "dry_run": False,
        }

    return {"success": True, "screenshots": screenshots, "error": "", "dry_run": False}


def _has_submit_button(page, analysis: FormAnalysis) -> bool:
    """Check whether a submit button exists/is visible without clicking it."""
    candidates = [
        analysis.submit_selector,
        "button[type='submit']",
        "button:has-text('Submit')",       "button:has-text('Apply')",
        "button:has-text('Submit Application')",
        "button:has-text('Submit your application')",
        "button:has-text('Send Application')",
        "input[type='submit']",
    ]
    for sel in candidates:
        if not sel:
            continue
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                return True
        except Exception:
            pass
    return False


# ── Helpers ───────────────────────────────────────────────────────

def _click_apply_button(page, emit=None):
    """
    Find and click the apply button on a listing page.
    Returns (clicked: bool, page: page).
    The page reference may change if a new tab was opened.
    """
    # Wait up to 10 seconds for button to appear (JS rendering)
    for attempt in range(10):
        for sel in APPLY_BUTTON_SELECTORS:
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    _log(emit, f"Found apply button: {sel}")
                    _step(emit, "found_apply_button", "Found the Apply button")
                    # Watch for new tab
                    try:
                        with page.context.expect_page(timeout=4000) as new_pg_info:
                            btn.click()
                        new_pg = new_pg_info.value
                        new_pg.wait_for_load_state("networkidle", timeout=20000)
                        time.sleep(2)
                        return True, new_pg
                    except Exception:
                        btn.click()
                        time.sleep(random.uniform(2, 3.5))
                        return True, page
            except Exception:
                continue
        time.sleep(1)

    return False, page


def _page_has_form(page) -> bool:
    try:
        return bool(page.evaluate("""() => {
            const inputs = document.querySelectorAll(
                'input:not([type=hidden]):not([type=submit]):not([type=button]),'
                + 'select,textarea'
            );
            return Array.from(inputs).some(el => {
                const r = el.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            });
        }"""))
    except Exception:
        return False


def _wait_for_form(page, timeout: int = 10):
    """Poll until form inputs appear or timeout."""
    for _ in range(timeout):
        if _page_has_form(page):
            return
        time.sleep(1)


def _is_review_page(page) -> bool:
    try:
        body = page.inner_text("body")[:2000].lower()
        return any(kw in body for kw in [
            "review your application", "please review", "check your details",
            "confirm your application", "application summary", "before you submit",
            "100%",
        ])
    except Exception:
        return False


def _screenshot(page, job_id: str, label: str) -> str:
    try:
        from applier.screenshotter import take
        return take(page, job_id, label)
    except Exception as e:
        logger.debug(f"  [Screenshot] screenshotter.take failed: {e}")
        try:
            import time as _t
            ts   = _t.strftime("%H%M%S")
            path = cfg.SCREENSHOTS_DIR / f"{job_id}_{label}_{ts}.png"
            page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception:
            return ""


def page_text_from_extraction(form_text: str) -> str:
    """Pass-through — form_text from extract_fields is already the page_html arg."""
    return form_text


_PROGRESS_RX = re.compile(r"\b(?:step|page)\s+(\d{1,2})\s*(?:of|/)\s*(\d{1,2})\b", re.I)


def _form_progress(page, page_num: int) -> tuple[int, int | None]:
    """(n, total) for "Form page n of total": the page's own "Step 2 of 4"
    text when it has one, otherwise our own count and no total."""
    try:
        m = _PROGRESS_RX.search(page.inner_text("body")[:4000])
        if m:
            n, total = int(m.group(1)), int(m.group(2))
            if 1 <= n <= total <= 20:
                return n, total
    except Exception:
        pass
    return page_num + 1, None


def _report_page(page, emit, page_num: int):
    n, total = _form_progress(page, page_num)
    _step(emit, "form_page", f"Form page {n} of {total}" if total else f"Form page {n}",
          page=n, total=total)


def _page_url(page) -> str:
    try:
        return str(page.url or "")
    except Exception:
        return ""


def _snapshot(analysis, hits: list[dict], page_num: int) -> list[dict]:
    """What the analysed page now holds, field by field, for the review packet
    (applier/review.py turns these into packet fields): the value that was
    just filled, the form's own `required` flag, and - if a saved answer or
    one of this application's own answers filled it - that hit, so the packet
    can say where the value came from. File uploads are not fields to review
    (the chosen resume is recorded separately)."""
    by_label = {h["label"]: h for h in hits}
    out = []
    for f in analysis.fields:
        if (f.field_type or "").lower() in ("file", "upload") or not (f.label or "").strip():
            continue
        value = f.db_value or getattr(f, "placeholder", "") or ""
        out.append({"label": f.label, "field_key": f.field_key, "type": f.field_type or "text",
                    "value": str(value), "options": list(f.options or []),
                    "required": bool(getattr(f, "is_required", False)),
                    "page": page_num + 1, "hit": by_label.get(f.label)})
    return out


def _apply_answers(answers, analysis, emit):
    """Consult the answer bank (applier/answers.py) on a freshly analysed page,
    before anything is filled or anyone is asked, and say what it did. A fault
    in the bank is logged and the page is filled as if it were not there."""
    if answers is None:
        return []
    try:
        hits = answers.apply(analysis)
    except Exception:
        logger.exception("  [Universal] answer bank failed; filling without it")
        return []
    n = summarize(hits)
    used = n["filled"] + n["once"]
    if used:
        _step(emit, "used_saved_answers", f"Filled {used} field{'' if used == 1 else 's'} from your saved answers",
              count=used, once=n["once"])
    if n["suggested"]:
        k = n["suggested"]
        _step(emit, "saved_answers_to_confirm", f"{k} saved answer{'' if k == 1 else 's'} to confirm",
              count=k)
    return hits


def _report_filled(emit, filled, failed):
    n = len(filled)
    _step(emit, "filled_fields", f"Filled {n} field{'' if n == 1 else 's'}",
          count=n, unfilled=len(failed))


# Visible fields the site has marked invalid, and the usual inline error
# messages (LinkedIn's, Bootstrap's, the common generic classes). Deliberately
# NOT role="alert": that is also used for banners and toasts, and a false
# positive here would park a good application waiting for a person.
_VALIDATION_JS = """() => {
  const sel = '[aria-invalid="true"], .artdeco-inline-feedback--error, '
            + '[data-test-form-element-error-message], .invalid-feedback, '
            + '.field-error, .form-error, .error-message';
  const out = [];
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    if (getComputedStyle(el).visibility !== 'visible') continue;   // hidden still takes up room
    let t = (el.innerText || '').trim();
    if (!t) {
      if (el.getAttribute('aria-invalid') !== 'true') continue;    // an empty message box is not an error
      const id = (el.getAttribute('aria-describedby') || '').split(' ')[0];
      const d = id && document.getElementById(id);
      t = ((d && d.innerText) || el.getAttribute('aria-label')
           || el.getAttribute('name') || 'a field').trim();
    }
    out.push(t.slice(0, 120));
    if (out.length >= 5) break;
  }
  return out;
}"""


def _validation_errors(page) -> list[str]:
    """Messages for visible fields the site rejected after we moved on, [] if none."""
    try:
        found = page.evaluate(_VALIDATION_JS)
        return [str(x) for x in found][:5] if isinstance(found, list) else []
    except Exception:
        return []


def _site_type(page, job: dict) -> str:
    """Which kind of application flow this is (applier/site_type.py), from the
    page's URL (after any redirect) and html. Never raises."""
    try:
        html = page.content()
    except Exception:
        html = ""
    from applier.target import apply_url
    return detect_site_type(_page_url(page) or apply_url(job), html)


def _validation_message(errors: list[str]) -> str:
    return "Validation error — the site rejected the answers: " + "; ".join(errors[:3])


def _fail(reason: str) -> dict:
    return {
        "success": False, "screenshots": [], "missing_fields": [],
        "analysis": None, "error": reason, "needs_review": False,
    }


def _step(emit, code: str, message: str, **data):
    """Progress for the job's timeline: emit("step", {...}); the Orchestrator
    records it in application_events. Never raises."""
    logger.info(f"  [Universal] step: {message}")
    if emit:
        try: emit("step", {"step": code, "message": message, **data})
        except Exception: pass


def _log(emit, msg: str, warn: bool = False):
    level = logger.warning if warn else logger.info
    level(f"  [Universal] {msg}")
    if emit:
        try: emit("log", msg)
        except Exception: pass
