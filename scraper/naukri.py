"""
scraper/naukri.py — Naukri.com job scraper.

Same login strategy as LinkedIn/Indeed — no credentials in .env.
If not logged in, opens the login page and waits for manual login.
Cookies saved for all future runs.

NAUKRI-SPECIFIC NOTE — read before relying on this:
Naukri redesigns its search-results markup more often than LinkedIn or
Indeed (their own devs joke about it — see any Naukri scraping writeup
from the last few years). The selectors below are current as of the
2026 layout (`div.srp-jobtuple-wrapper` → `div.cust-job-tuple`, with
the pre-2026 `article.jobTuple.bgWhite.br4.mb-8` layout kept as a
fallback). If `_search()` starts returning 0 cards where you'd expect
results, that's the first thing to check — inspect a live search page
and update SELECTORS below, the same way you'd patch the LinkedIn/
Indeed selectors if they broke.

Naukri also fronts its search results with Akamai-style bot detection
(harder-edged than LinkedIn/Indeed's checkpoints). `detect_captcha()`
from core.browser handles the common CAPTCHA widgets; `_is_blocked()`
below adds Naukri's own "access denied / unusual traffic" wording.
"""

import logging
import random
import time
from urllib.parse import urlencode

from core.config  import cfg
from core.browser import detect_captcha, scroll_to_bottom, load_cookies, save_cookies, human_delay

logger       = logging.getLogger(__name__)
MAX_PAGES    = 5
BASE_URL     = "https://www.naukri.com"
COOKIES_PATH = cfg.COOKIES_DIR / "naukri.json"


def scrape(playwright, profile: dict,
           job_titles: list[str], locations: list[str]) -> list[dict]:
    from core.browser import launch_browser, get_page

    results: list[dict] = []
    try:
        ctx  = launch_browser(playwright)
        page = get_page(ctx)
    except Exception as e:
        logger.error(f"[Naukri] Browser launch failed: {e}")
        return results

    try:
        load_cookies(ctx, COOKIES_PATH)

        if not _ensure_logged_in(page, ctx):
            logger.warning("[Naukri] Could not log in — skipping scrape")
            ctx.close()
            return results

        for title in job_titles:
            for location in locations:
                logger.info(f"  [Naukri] '{title}' in '{location}'")
                jobs = _search(page, title, location)
                results.extend(jobs)
                human_delay(2, 5)

    except Exception as e:
        logger.error(f"[Naukri] Scrape error: {e}")
    finally:
        try: ctx.close()
        except Exception: pass

    logger.info(f"  [Naukri] Total: {len(results)} jobs")
    return results


def _ensure_logged_in(page, ctx) -> bool:
    """
    Check Naukri login. If not logged in, open the login page and
    wait up to 90 seconds for the user to log in manually.

    Unlike LinkedIn, we treat "couldn't confirm login" as fatal rather
    than continuing anonymously: Naukri's one-click apply flow (used by
    the applier) depends on being logged in as the profile owner, and
    search results/relevance are also personalised to your profile once
    logged in, so scraping while logged out would silently produce a
    worse (and un-appliable) result set.
    """
    try:
        page.goto(BASE_URL, timeout=20000, wait_until="domcontentloaded")
        time.sleep(2)

        if _is_logged_in(page):
            logger.info("  [Naukri] Already logged in ✓")
            save_cookies(ctx, COOKIES_PATH, domain_filter="naukri.com")
            return True

        logger.info("  [Naukri] Not logged in — opening login page for manual login")
        logger.info("  [Naukri] Please log in to Naukri in the browser window (90 seconds)...")

        page.goto(f"{BASE_URL}/nlogin/login", timeout=20000)

        for _ in range(90):
            time.sleep(1)
            if _is_logged_in(page):
                logger.info("  [Naukri] Login detected ✓ — saving session")
                save_cookies(ctx, COOKIES_PATH, domain_filter="naukri.com")
                return True

        logger.warning("  [Naukri] Login timeout (90s) — skipping Naukri scrape")
        return False

    except Exception as e:
        logger.warning(f"  [Naukri] Login check error: {e}")
        return False


def _is_logged_in(page) -> bool:
    """
    Best-effort — Naukri doesn't expose as clean a signal as LinkedIn's
    `.global-nav__me`. Checks (in order): a profile/avatar element that
    only renders when logged in, absence of the "Login" nav CTA, and
    the URL not being the login page itself.
    """
    try:
        if page.query_selector(
            "a[title='Profile'], .nI-gNb-drawer__icon, "
            "[class*='gnb-icon-profile'], a[href*='mnjuser/profile']"
        ):
            return True
        url = page.url.lower()
        if "nlogin" in url or "/login" in url:
            return False
        login_cta = page.query_selector("a:has-text('Login'), button:has-text('Login')")
        if not login_cta:
            return True
    except Exception:
        pass
    return False


def _is_blocked(page) -> bool:
    """Naukri's own bot-check wording, on top of the generic detect_captcha()."""
    try:
        body = page.inner_text("body")[:500].lower()
        if any(k in body for k in [
            "access denied", "unusual traffic", "reference #",
            "request could not be satisfied",
        ]):
            return True
    except Exception:
        pass
    return False


def _search(page, title: str, location: str) -> list[dict]:
    jobs: list[dict] = []
    try:
        slug = "-".join(title.lower().split()) or "jobs"
        params = urlencode({"k": title, "l": location, "jobAge": "1", "sort": "r"})
        page.goto(f"{BASE_URL}/{slug}-jobs?{params}",
                  timeout=25000, wait_until="domcontentloaded")
        time.sleep(random.uniform(2, 4))

        if detect_captcha(page) or _is_blocked(page):
            logger.warning(f"  [Naukri] Blocked/CAPTCHA on search for '{title}' in "
                           f"'{location}' — skipping this query. If this happens on "
                           f"every search, slow down the pipeline's request rate.")
            return jobs

        for page_num in range(1, MAX_PAGES + 1):
            scroll_to_bottom(page)
            time.sleep(1.5)

            cards = page.query_selector_all(
                "div.srp-jobtuple-wrapper, div.cust-job-tuple, "
                "article.jobTuple.bgWhite.br4.mb-8"
            )
            logger.info(f"    Page {page_num}: {len(cards)} cards")

            for card in cards:
                job = _parse_card(card)
                if job:
                    job["source"] = "naukri"
                    jobs.append(job)

            next_btn = page.query_selector(
                "a.styles_btn-secondary__2AsIP:has-text('Next'), "
                "a[href*='pageNo=']:has-text('Next')"
            )
            if not next_btn or page_num >= MAX_PAGES:
                break
            next_btn.click()
            time.sleep(random.uniform(2, 4))

    except Exception as e:
        logger.debug(f"  [Naukri] Search error '{title}': {e}")
    return jobs


def _parse_card(card) -> dict | None:
    try:
        title_el = card.query_selector("a.title")
        if not title_el:
            return None
        title = title_el.inner_text().strip()
        if not title:
            return None

        url = (title_el.get_attribute("href") or "").split("?")[0]
        if not url:
            return None
        if not url.startswith("http"):
            url = BASE_URL + url

        company_el = (
            card.query_selector("a.comp-name") or
            card.query_selector(".comp-name") or
            card.query_selector("a.subTitle.ellipsis.fleft")
        )
        company = company_el.inner_text().strip() if company_el else "Unknown"

        location_el = card.query_selector(
            "span.locWdth, [class*='loc'], span.ellipsis.locWdth"
        )
        location = location_el.inner_text().strip() if location_el else ""

        salary_el = card.query_selector("span.sal-wrap, span.sal, [class*='salary']")
        salary    = salary_el.inner_text().strip() if salary_el else ""

        desc_el = card.query_selector("span.job-desc, .job-description, [class*='job-desc']")
        desc    = desc_el.inner_text().strip() if desc_el else ""

        company_site = False
        try:
            company_site = "apply on company site" in card.inner_text().lower()
        except Exception:
            pass

        return {
            "title": title, "company": company, "location": location,
            "url": url, "description": desc, "salary": salary,
            "easy_apply": not company_site, "company_site": company_site,
        }
    except Exception:
        return None
