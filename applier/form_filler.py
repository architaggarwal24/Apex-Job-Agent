"""
applier/form_filler.py — Playwright form filling executor.

Executes a FormAnalysis fill plan on any live page.
Handles: text, email, tel, number, textarea, select, checkbox, radio, file, date.
Uses human-like typing delays and fallback selector chains.
"""

import logging
import random
import time
from pathlib import Path

from core.config import cfg
from applier.form_analyzer import FormAnalysis, FormField

logger = logging.getLogger(__name__)


def _delay(lo: float = 0.3, hi: float = 0.8):
    time.sleep(random.uniform(lo, hi))


def _resolve(page, f: FormField):
    """Try primary selector then fallbacks. Returns (element, selector) or (None, None)."""
    candidates = [f.selector] + _fallbacks(f)
    for sel in candidates:
        if not sel:
            continue
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return el, sel
        except Exception:
            continue
    return None, None


def _fallbacks(f: FormField) -> list[str]:
    key   = f.field_key
    alts  = []
    for attr in ("name", "id", "data-field", "aria-label"):
        alts += [f"[{attr}='{key}']", f"[{attr}*='{key}' i]"]
    if f.label:
        alts += [f"input[placeholder*='{f.label}' i]",
                 f"textarea[placeholder*='{f.label}' i]"]
    type_map = {
        "email":  ["input[type='email']"],
        "tel":    ["input[type='tel']", "input[name*='phone' i]", "input[name*='mobile' i]"],
        "file":   ["input[type='file']"],
        "date":   ["input[type='date']"],
        "number": ["input[type='number']"],
    }
    alts += type_map.get(f.field_type, [])
    return alts


def _fill_one(page, f: FormField, resume_path: Path | None = None) -> bool:
    value = f.fill_value()
    ftype = f.field_type.lower()

    if ftype in ("file", "upload"):
        target = resume_path or cfg.RESUME_PATH
        if "cover" in f.field_key.lower() or "cover" in f.label.lower():
            cl = cfg.ROOT_DIR / "cover_letter.pdf"
            if cl.exists():
                target = cl
        try:
            el, _ = _resolve(page, f)
            if el and Path(str(target)).exists():
                el.set_input_files(str(target))
                _delay(1.5, 2.5)
                return True
        except Exception as e:
            logger.debug(f"File upload error {f.label}: {e}")
        return False

    if not value:
        return False

    el, sel = _resolve(page, f)
    if not el:
        logger.debug(f"Element not found: {f.label} ({f.selector})")
        return False

    try:
        el.scroll_into_view_if_needed()
        _delay(0.1, 0.3)

        if ftype in ("text", "email", "tel", "number", "url", "search"):
            el.click()
            _delay(0.1, 0.2)
            el.fill("")
            _delay(0.05, 0.1)
            for ch in value:
                page.keyboard.type(ch, delay=random.randint(40, 100))
            _delay(0.2, 0.5)

        elif ftype == "textarea":
            el.click()
            _delay(0.2, 0.4)
            el.fill(value)
            _delay(0.3, 0.6)

        elif ftype == "select":
            filled = False
            for attempt in (lambda: el.select_option(value=value),
                            lambda: el.select_option(label=value)):
                try:
                    attempt()
                    filled = True
                    break
                except Exception:
                    pass
            if not filled and f.options:
                v_lower = value.lower()
                best = next((o for o in f.options
                             if v_lower in o.lower() or o.lower() in v_lower),
                            f.options[0])
                try:
                    el.select_option(label=best)
                    filled = True
                except Exception:
                    pass
            if not filled:
                return False
            _delay(0.2, 0.4)

        elif ftype == "checkbox":
            want = value.lower() in ("yes", "true", "1", "on", "checked")
            if el.is_checked() != want:
                el.click()
            _delay(0.1, 0.3)

        elif ftype == "radio":
            radio = page.query_selector(f"{sel}[value='{value}']") or el
            radio.click()
            _delay(0.1, 0.3)

        elif ftype == "date":
            el.click()
            _delay(0.1, 0.2)
            el.fill(value)
            _delay(0.1, 0.2)

        else:
            try:
                el.fill(value)
            except Exception:
                el.click()
                for ch in value:
                    page.keyboard.type(ch, delay=random.randint(40, 100))

        return True

    except Exception as e:
        logger.debug(f"Fill error {f.label} ({sel}): {e}")
        return False


def execute(page, analysis: FormAnalysis,
            resume_path: Path | None = None,
            only_missing: bool = False) -> tuple[list[FormField], list[FormField]]:
    """
    Execute a fill plan on the active page.

    Args:
        only_missing: If True, only fill fields with needs_manual=True (iteration 2)

    Returns:
        (filled, failed) lists of FormField
    """
    fields = analysis.missing if only_missing else analysis.fields
    filled, failed = [], []

    logger.info(f"  [FormFiller] Filling {len(fields)} fields "
                f"(only_missing={only_missing})")

    for f in fields:
        if not f.fill_value() and f.field_type not in ("file", "upload"):
            if f.needs_manual:
                failed.append(f)
            continue

        try:
            el = _resolve(page, f)[0]
            if el:
                el.scroll_into_view_if_needed()
                _delay(0.1, 0.2)
        except Exception:
            pass

        ok = _fill_one(page, f, resume_path)
        if ok:
            filled.append(f)
            logger.debug(f"    ✓ {f.label}: {f.fill_value()[:40]}")
        else:
            failed.append(f)
            logger.debug(f"    ✗ {f.label}")
        _delay(0.4, 1.0)

    logger.info(f"  [FormFiller] ✓ {len(filled)} filled  ✗ {len(failed)} failed")
    return filled, failed


def click_next(page, analysis: FormAnalysis) -> bool:
    candidates = [
        analysis.next_selector,
        "button:has-text('Next')",     "button:has-text('Continue')",
        "button:has-text('Proceed')",  "input[value*='Next' i]",
        "a:has-text('Next')",
    ]
    for sel in candidates:
        if not sel:
            continue
        try:
            btn = page.query_selector(sel)
            if btn and btn.is_visible():
                btn.click()
                _delay(2.0, 3.5)
                return True
        except Exception:
            pass
    return False


def click_submit(page, analysis: FormAnalysis) -> bool:
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
                btn.click()
                _delay(2.5, 4.0)
                return True
        except Exception:
            pass
    return False
