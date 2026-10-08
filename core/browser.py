"""
core/browser.py — Playwright browser management.

<<<<<<< HEAD
Uses Opera with a persistent profile so all login sessions
(LinkedIn, Indeed) are preserved between runs.

Key: ignore_default_args strips Playwright's session-breaking flags.
=======
Login strategy: no credentials stored anywhere.
The agent uses Opera's existing logged-in sessions.
If a session has expired, it opens the login page and
waits for the user to log in manually (up to 90 seconds).
Cookies are saved after each successful login for future runs.
>>>>>>> 4226f10 (.)
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
<<<<<<< HEAD
    Launch Opera with the persistent user profile.
    ignore_default_args prevents Playwright from injecting flags that
    disable extensions/sync and wipe saved login sessions.
=======
    Launch Opera with persistent profile.
    ignore_default_args keeps your saved sessions alive.
>>>>>>> 4226f10 (.)
    """
    exe  = cfg.opera_exe()
    data = cfg.opera_data()

    if not exe:
        raise EnvironmentError("OPERA_EXECUTABLE_PATH not set in .env")
    if not os.path.exists(exe):
        raise FileNotFoundError(f"Opera not found at: {exe}")

<<<<<<< HEAD
    # Warn if Opera is already running
=======
    # Warn if Opera is already open
>>>>>>> 4226f10 (.)
    try:
        import subprocess
        out = subprocess.check_output("tasklist", shell=True, text=True,
                                      stderr=subprocess.DEVNULL)
        if "opera.exe" in out.lower():
            logger.warning("[Browser] Opera is running — close it before the agent starts")
    except Exception:
        pass

<<<<<<< HEAD
    logger.info(f"[Browser] Launching Opera: {exe}")
=======
    logger.info(f"[Browser] Launching Opera")

    # Read headless + slow_mo from SettingsDB if available, else .env
    headless = cfg.BROWSER_HEADLESS
    slow_mo  = cfg.BROWSER_SLOW_MO
    try:
        from db.settings_db import SettingsDB
        s        = SettingsDB.get()
        headless = s.read("browser_headless", "false").lower() == "true"
        slow_mo  = int(s.read("browser_slow_mo", "60"))
    except Exception:
        pass
>>>>>>> 4226f10 (.)

    ctx = playwright.chromium.launch_persistent_context(
        user_data_dir=data,
        executable_path=exe,
<<<<<<< HEAD
        headless=cfg.BROWSER_HEADLESS,
        slow_mo=cfg.BROWSER_SLOW_MO,
        # Strip session-breaking flags injected by Playwright by default
=======
        headless=headless,
        slow_mo=slow_mo,
>>>>>>> 4226f10 (.)
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
<<<<<<< HEAD
    """Return first existing tab or open a new one."""
=======
>>>>>>> 4226f10 (.)
    return ctx.pages[0] if ctx.pages else ctx.new_page()


def load_cookies(ctx, path: Path):
<<<<<<< HEAD
    """Load saved cookies into context."""
    if path.exists():
        try:
            cookies = json.loads(path.read_text())
=======
    """
    Cookies are stored encrypted at rest (see save_cookies) — a saved
    LinkedIn/Indeed/Naukri session is a live login, functionally
    equivalent to a password for that account, so it gets the same
    protection as an API key in SettingsDB. decrypt_bytes() transparently
    passes through any pre-existing plaintext cookie file untouched, so
    upgrading doesn't strand sessions saved before this change.
    """
    if path.exists():
        try:
            from core.crypto import decrypt_bytes
            raw = decrypt_bytes(path.read_bytes())
            cookies = json.loads(raw.decode())
>>>>>>> 4226f10 (.)
            ctx.add_cookies(cookies)
            logger.debug(f"[Browser] Loaded {len(cookies)} cookies from {path.name}")
        except Exception as e:
            logger.debug(f"[Browser] Cookie load error: {e}")


def save_cookies(ctx, path: Path, domain_filter: str = ""):
<<<<<<< HEAD
    """Save context cookies to disk, optionally filtering by domain."""
    try:
        cookies = ctx.cookies()
        if domain_filter:
            cookies = [c for c in cookies if domain_filter in c.get("domain", "")]
        path.write_text(json.dumps(cookies, indent=2))
        logger.debug(f"[Browser] Saved {len(cookies)} cookies to {path.name}")
=======
    try:
        from core.crypto import encrypt_bytes
        cookies = ctx.cookies()
        if domain_filter:
            cookies = [c for c in cookies if domain_filter in c.get("domain", "")]
        payload = json.dumps(cookies, indent=2).encode()
        path.write_bytes(encrypt_bytes(payload))
        logger.debug(f"[Browser] Saved {len(cookies)} cookies → {path.name} (encrypted)")
>>>>>>> 4226f10 (.)
    except Exception as e:
        logger.debug(f"[Browser] Cookie save error: {e}")


<<<<<<< HEAD
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
=======
def wait_for_manual_login(page, check_fn, platform: str,
                           timeout: int = 90) -> bool:
    """
    Open the platform's login page and poll until check_fn(page) returns True.
    Used when cookies are missing or expired.

    Args:
        page:      Playwright page
        check_fn:  Callable(page) -> bool — returns True when logged in
        platform:  Display name e.g. "LinkedIn"
        timeout:   Seconds to wait for manual login (default 90)

    Returns:
        True if login detected within timeout
    """
    logger.info(f"  [Browser] Waiting for manual {platform} login "
                f"(up to {timeout}s)...")

    for elapsed in range(timeout):
        time.sleep(1)
        if elapsed > 0 and elapsed % 30 == 0:
            logger.info(f"  [Browser] Still waiting for {platform} "
                        f"({elapsed}/{timeout}s)...")
        if check_fn(page):
            logger.info(f"  [Browser] {platform} login detected ✓")
            return True

    logger.warning(f"  [Browser] {platform} login timeout after {timeout}s")
    return False


def detect_captcha(page) -> bool:
    """
    Detect an ACTIONABLE captcha — i.e. one that's actually visible and
    blocking the page, not just text mentioning "captcha" somewhere
    (LinkedIn/Indeed footers often say "we use captchas to protect..."
    even on completely normal pages, which caused false positives).

    Only returns True if:
      - A captcha iframe/element exists AND is visible, OR
      - The page title/h1 explicitly indicates a challenge page
    """
    # Check for visible captcha iframes/widgets
    for sel in ["iframe[src*='captcha']", "iframe[src*='recaptcha']",
                "iframe[src*='hcaptcha']", "#captcha", "div.g-recaptcha",
                "[class*='captcha' i]", "#px-captcha"]:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                return True
        except Exception:
            pass

    # Check page title and main heading for explicit challenge pages
    # (much more reliable than scanning all body text)
    try:
        title = (page.title() or "").lower()
        if any(k in title for k in ["security check", "verify you are human",
                                     "let's confirm", "checkpoint", "unusual activity"]):
            return True
    except Exception:
        pass

    try:
        h1 = page.query_selector("h1")
        if h1:
            h1_text = (h1.inner_text() or "").lower()
            if any(k in h1_text for k in ["verify you are human", "let's do a quick security check",
                                           "are you a robot", "unusual activity"]):
                return True
    except Exception:
        pass

>>>>>>> 4226f10 (.)
    return False


def is_login_wall(page) -> bool:
<<<<<<< HEAD
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
=======
    url = page.url.lower()
    if any(p in url for p in ["/403", "/404", "access-denied", "checkpoint/challenge"]):
        return True
    try:
        body = page.inner_text("body")[:300].lower()
        if any(k in body for k in ["403 forbidden", "404 not found", "access denied"]):
>>>>>>> 4226f10 (.)
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
