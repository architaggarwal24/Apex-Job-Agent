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
import time
from pathlib import Path

from core.config import cfg
from core.browser import detect_captcha, is_login_wall, human_delay
from applier.form_analyzer import extract_fields, analyze, FormAnalysis
from applier.form_filler import execute, click_next, click_submit

logger = logging.getLogger(__name__)

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

    _log(emit, f"Opening application: {job.get('url','')}")

    # ── Step 1: Navigate ──────────────────────────────────────────
    try:
        page.goto(job["url"], timeout=30000, wait_until="networkidle")
        time.sleep(random.uniform(1.5, 3.0))
    except Exception as e:
        return _fail(f"Navigation failed: {e}")

    if detect_captcha(page):
        return _fail("CAPTCHA detected — skipping")

    if is_login_wall(page):
        return _fail("Login wall — page requires authentication")

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

        analysis = analyze(
            page_html=page_text_from_extraction(form_text),
            page_url=  page.url,
            job_id=    job["job_id"],
            job_title= job["title"],
            company=   job["company"],
            profile=   profile,
        )
        last_analysis = analysis

        _log(emit, f"Found {len(analysis.fields)} fields — "
                   f"{len(analysis.fillable)} fillable, {len(analysis.missing)} missing")

        filled, failed = execute(page, analysis,
                                 resume_path=cfg.RESUME_PATH)

        all_missing.extend(analysis.missing)

        # Screenshot after filling each page
        ss = _screenshot(page, job["job_id"], f"iter1_p{page_num+1}")
        if ss:
            screenshots.append(ss)

        # Try to advance to next page
        if not click_next(page, analysis):
            break

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
    }


def run_iteration2(
    page,
    job: dict,
    profile: dict,
    missing_keys: list[str],
    emit=None,
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

    if not _page_has_form(page):
        _click_apply_button(page, emit)

    _wait_for_form(page)

    for page_num in range(8):
        _log(emit, f"Re-filling page {page_num + 1}")
        form_text = extract_fields(page)
        if not form_text.strip():
            break

        analysis = analyze(
            page_html=page_text_from_extraction(form_text),
            page_url=  page.url,
            job_id=    job["job_id"],
            job_title= job["title"],
            company=   job["company"],
            profile=   profile,
            only_keys= missing_keys,
        )

        execute(page, analysis, resume_path=cfg.RESUME_PATH)

        ss = _screenshot(page, job["job_id"], f"iter2_p{page_num+1}")
        if ss:
            screenshots.append(ss)

        if not click_next(page, analysis):
            break
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
    }


def run_submit(
    page,
    job: dict,
    profile: dict,
    corrections: list[str] | None = None,
    emit=None,
) -> dict:
    """
    Final submit: re-open form, fill everything, click Submit.
    """
    screenshots = []
    _log(emit, "Re-opening form for final submission")

    try:
        page.goto(job["url"], timeout=30000, wait_until="networkidle")
        time.sleep(random.uniform(1.5, 3.0))
    except Exception as e:
        return _fail(f"Navigation failed: {e}")

    if not _page_has_form(page):
        _click_apply_button(page, emit)

    _wait_for_form(page)

    submitted = False
    for page_num in range(8):
        form_text = extract_fields(page)
        if not form_text.strip():
            break

        analysis = analyze(
            page_html=page_text_from_extraction(form_text),
            page_url=  page.url,
            job_id=    job["job_id"],
            job_title= job["title"],
            company=   job["company"],
            profile=   profile,
        )

        execute(page, analysis, resume_path=cfg.RESUME_PATH)
        _log(emit, f"Filled form page {page_num + 1}")

        if click_submit(page, analysis):
            time.sleep(3)
            ss = _screenshot(page, job["job_id"], "submitted")
            if ss:
                screenshots.append(ss)
            submitted = True
            break

        if not click_next(page, analysis):
            break
        time.sleep(random.uniform(1.0, 2.0))

    return {
        "success":     submitted,
        "screenshots": screenshots,
        "error":       "" if submitted else "Could not find submit button",
    }


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
        from agent.screenshotter import take
        return take(page, job_id, label)
    except Exception:
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


def _fail(reason: str) -> dict:
    return {
        "success": False, "screenshots": [], "missing_fields": [],
        "analysis": None, "error": reason, "needs_review": False,
    }


def _log(emit, msg: str, warn: bool = False):
    level = logger.warning if warn else logger.info
    level(f"  [Universal] {msg}")
    if emit:
        try: emit("log", msg)
        except Exception: pass
