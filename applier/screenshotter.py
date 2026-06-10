"""
applier/screenshotter.py — Screenshot capture utility.
"""

import logging
import time
from pathlib import Path
from core.config import cfg

logger = logging.getLogger(__name__)


def take(page, job_id: str, label: str) -> str:
    safe  = label.replace(" ", "_").replace("/", "-")[:40]
    ts    = time.strftime("%H%M%S")
    path  = cfg.SCREENSHOTS_DIR / f"{job_id}_{safe}_{ts}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        logger.info(f"  [Screenshot] {path.name}")
        return str(path)
    except Exception as e:
        logger.warning(f"  [Screenshot] Failed {label}: {e}")
        return ""


def find_review_page(page) -> bool:
    try:
        body = page.inner_text("body")[:2000].lower()
        return any(k in body for k in [
            "review your application", "please review", "check your details",
            "confirm your application", "before you submit",
        ])
    except Exception:
        return False
