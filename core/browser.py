"""
core/browser.py — Playwright browser management.

Uses Opera with a persistent profile so all login sessions
(LinkedIn, Indeed) are preserved between runs.

Key: ignore_default_args strips Playwright's session-breaking flags.
"""

import json
import logging
import os
import random
import time
from pathlib import Path

from core.config import cfg

logger = logging.getLogger(__name__)


def launch_browser(playwright):
    """
    Launch Opera with the persistent user profile.
    ignore_default_args prevents Playwright from injecting flags that
    disable extensions/sync and wipe saved login sessions.
    """
    exe  = cfg.opera_exe()
    data = cfg.opera_data()

    if not exe:
        raise EnvironmentError("OPERA_EXECUTABLE_PATH not set in .env")
    if not os.path.exists(exe):
        raise FileNotFoundError(f"Opera not found at: {exe}")

    # Warn if Opera is already running
    try:
        import subprocess
        out = subprocess.check_output("tasklist", shell=True, text=True,
                                      stderr=subprocess.DEVNULL)
        if "opera.exe" in out.lower():
            logger.warning("[Browser] Opera is running — close it before the agent starts")
    except Exception:
        pass

    logger.info(f"[Browser] Launching Opera: {exe}")

    ctx = playwright.chromium.launch_persistent_context(
        user_data_dir=data,
        executable_path=exe,
        headless=cfg.BROWSER_HEADLESS,
        slow_mo=cfg.BROWSER_SLOW_MO,
        # Strip session-breaking flags injected by Playwright by default
        ignore_default_args=[
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-default-apps",
            "--disable-component-extensions-with-background-pages",
            "--disable-background-timer-throttling",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--metrics-recording-only",
            "--no-first-run",
        ],
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
            "--disable-infobars",
        ],
        viewport=None,
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36 OPR/106.0.0.0"
        ),
        accept_downloads=True,
    )
    return ctx


def get_page(ctx):
    """Return first existing tab or open a new one."""
    return ctx.pages[0] if ctx.pages else ctx.new_page()


def load_cookies(ctx, path: Path):
    """Load saved cookies into context."""
    if path.exists():
        try:
            cookies = json.loads(path.read_text())
            ctx.add_cookies(cookies)
            logger.debug(f"[Browser] Loaded {len(cookies)} cookies from {path.name}")
        except Exception as e:
            logger.debug(f"[Browser] Cookie load error: {e}")


def save_cookies(ctx, path: Path, domain_filter: str = ""):
    """Save context cookies to disk, optionally filtering by domain."""
    try:
        cookies = ctx.cookies()
        if domain_filter:
            cookies = [c for c in cookies if domain_filter in c.get("domain", "")]
        path.write_text(json.dumps(cookies, indent=2))
        logger.debug(f"[Browser] Saved {len(cookies)} cookies to {path.name}")
    except Exception as e:
        logger.debug(f"[Browser] Cookie save error: {e}")


def detect_captcha(page) -> bool:
    for sel in ["iframe[src*='captcha']", "iframe[src*='recaptcha']",
                "iframe[src*='hcaptcha']", "#captcha", "div.g-recaptcha", "#px-captcha"]:
        try:
            if page.query_selector(sel):
                return True
        except Exception:
            pass
    try:
        body = page.inner_text("body")[:500].lower()
        if any(k in body for k in ["captcha", "verify you are human", "i am not a robot"]):
            return True
    except Exception:
        pass
    return False


def is_login_wall(page) -> bool:
    """True if page is blocking access with a login requirement."""
    url = page.url.lower()
    # Hard blocks only — don't flag normal login redirects that resolve
    if any(p in url for p in ["/403", "/404", "access-denied",
                                "account-suspended", "checkpoint/challenge"]):
        return True
    try:
        body = page.inner_text("body")[:300].lower()
        if any(k in body for k in ["403 forbidden", "404 not found",
                                    "access denied", "page not found"]):
            return True
    except Exception:
        pass
    return False


def human_delay(lo: float = 0.5, hi: float = 1.5):
    time.sleep(random.uniform(lo, hi))


def scroll_to_bottom(page):
    try:
        page.evaluate("""
            () => new Promise(resolve => {
                let pos = 0;
                const step = Math.floor(document.body.scrollHeight / 8);
                const t = setInterval(() => {
                    pos += step;
                    window.scrollTo(0, pos);
                    if (pos >= document.body.scrollHeight) { clearInterval(t); resolve(); }
                }, 200);
            })
        """)
    except Exception:
        pass
