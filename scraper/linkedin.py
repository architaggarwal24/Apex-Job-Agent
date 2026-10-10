"""
scraper/linkedin.py — LinkedIn job scraper.

Login strategy (no credentials stored):
  1. Load saved cookies from data/cookies/linkedin.json
  2. Navigate to /feed — if logged in, proceed
  3. If NOT logged in → open the LinkedIn login page in the browser
     and wait up to 90 seconds for the user to log in manually
  4. After manual login, save cookies for all future runs

This means you log in once in the browser, and every subsequent
run reuses your session automatically.
"""

import json
import logging
import random
import time
from pathlib import Path
from urllib.parse import urlencode

from core.config  import cfg
from core.browser import detect_captcha, scroll_to_bottom, load_cookies, save_cookies, human_delay

logger   = logging.getLogger(__name__)
MAX_PAGES    = 5
COOKIES_PATH = cfg.COOKIES_DIR / "linkedin.json"


def scrape(playwright, profile: dict,
           job_titles: list[str], locations: list[str]) -> list[dict]:
    from core.browser import launch_browser, get_page

    results: list[dict] = []
    try:
        ctx  = launch_browser(playwright)
        page = get_page(ctx)
    except Exception as e:
        logger.error(f"[LinkedIn] Browser launch failed: {e}")
        return results

    try:
        load_cookies(ctx, COOKIES_PATH)

        if not _ensure_logged_in(page, ctx):
            logger.warning("[LinkedIn] Could not log in — skipping scrape")
            ctx.close()
            return results

        for title in job_titles:
            for location in locations:
                logger.info(f"  [LinkedIn] '{title}' in '{location}'")
                jobs = _search(page, title, location, ctx=ctx)
                results.extend(jobs)
                human_delay(3, 6)

    except Exception as e:
        logger.error(f"[LinkedIn] Scrape error: {e}")
    finally:
        try: ctx.close()
        except Exception: pass

    logger.info(f"  [LinkedIn] Total: {len(results)} jobs")
    return results


def _ensure_logged_in(page, ctx) -> bool:
    """
    Check login status. If not logged in, open the login page
    and wait for the user to log in manually (no credentials needed).
    Saves cookies after successful login.
    """
    try:
        page.goto("https://www.linkedin.com/feed",
                  timeout=20000, wait_until="domcontentloaded")
        time.sleep(2)

        if _is_logged_in(page):
            logger.info("  [LinkedIn] Already logged in ✓")
            save_cookies(ctx, COOKIES_PATH, domain_filter="linkedin.com")
            return True

        # Not logged in — open login page and wait for user
        logger.info("  [LinkedIn] Not logged in — opening login page for manual login")
        logger.info("  [LinkedIn] Please log in to LinkedIn in the browser window (90 seconds)...")

        page.goto("https://www.linkedin.com/login", timeout=20000)

        # Poll for up to 90 seconds
        for _ in range(90):
            time.sleep(1)
            if _is_logged_in(page):
                logger.info("  [LinkedIn] Login detected ✓ — saving session")
                save_cookies(ctx, COOKIES_PATH, domain_filter="linkedin.com")
                return True

        logger.warning("  [LinkedIn] Login timeout (90s) — skipping LinkedIn scrape")
        return False

    except Exception as e:
        logger.warning(f"  [LinkedIn] Login check error: {e}")
        return False


def _is_logged_in(page) -> bool:
    try:
        url = page.url.lower()
        if "feed" in url or "mynetwork" in url or "jobs" in url:
            # Confirm no login prompt visible
            if not page.query_selector("input#username"):
                return True
        # Check for profile nav element
        if page.query_selector(".global-nav__me, [data-control-name='identity_welcome_message']"):
            return True
    except Exception:
        pass
    return False


def _search(page, title: str, location: str, ctx=None) -> list[dict]:
    jobs: list[dict] = []
    try:
        params = urlencode({
            "keywords": title,
            "location": location,
            "f_TPR":    "r86400",   # last 24 hours
            "f_AL":     "true",     # Easy Apply only
            "sortBy":   "DD",
        })
        search_url = f"https://www.linkedin.com/jobs/search/?{params}"
        page.goto(search_url, timeout=25000, wait_until="domcontentloaded")
        time.sleep(random.uniform(2, 4))

        if detect_captcha(page):
            logger.warning(
                f"  [LinkedIn] Checkpoint/CAPTCHA on search for '{title}' in '{location}'\n"
                f"    Page title: '{page.title()}'\n"
                f"    Page URL:   {page.url}\n"
                f"    → Please solve it in the browser window. Waiting up to 3 minutes..."
            )

            from core.browser import wait_for_manual_login

            def _check(p):
                still_blocked = detect_captcha(p)
                if not still_blocked:
                    return True
                return False

            solved = wait_for_manual_login(page, _check, "LinkedIn checkpoint", timeout=180)

            if not solved:
                logger.warning(f"  [LinkedIn] Checkpoint not resolved after 3 min — "
                              f"skipping '{title}' in '{location}'")
                return jobs

            logger.info(f"  [LinkedIn] Checkpoint resolved ✓ — saving session and retrying search")

            # Save cookies immediately so this doesn't recur on next search
            if ctx:
                save_cookies(ctx, COOKIES_PATH, domain_filter="linkedin.com")

            # Checkpoint pages often redirect away — go back to the search URL
            page.goto(search_url, timeout=25000, wait_until="domcontentloaded")
            time.sleep(random.uniform(2, 4))

            # If checkpoint re-appears immediately, give up on this query
            if detect_captcha(page):
                logger.warning(f"  [LinkedIn] Checkpoint re-appeared after solving — "
                              f"skipping '{title}' in '{location}'. "
                              f"Try again later or reduce search frequency.")
                return jobs

        # Diagnostic: log if page doesn't look like a search results page
        if "jobs/search" not in page.url and "jobs/collections" not in page.url:
            logger.warning(f"  [LinkedIn] Unexpected URL after search: {page.url} "
                          f"(title: '{page.title()}')")

        if "login" in page.url or "authwall" in page.url:
            logger.warning("  [LinkedIn] Session expired mid-search")
            return jobs

        for page_num in range(1, MAX_PAGES + 1):
            scroll_to_bottom(page)
            time.sleep(2)

            cards = page.query_selector_all(
                ".jobs-search__results-list li, "
                ".job-card-container, [data-occludable-job-id]"
            )
            logger.info(f"    Page {page_num}: {len(cards)} cards")

            for card in cards:
                job = _parse_card(card)
                if job:
                    job["source"] = "linkedin"
                    jobs.append(job)

            more = page.query_selector(
                "button[aria-label*='more results'], "
                "button:has-text('Show more results')"
            )
            if not more or page_num >= MAX_PAGES:
                break
            more.click()
            time.sleep(random.uniform(2, 4))

    except Exception as e:
        logger.debug(f"  [LinkedIn] Search error '{title}': {e}")
    return jobs


def _parse_card(card) -> dict | None:
    try:
        title_el = (
            card.query_selector(".job-card-list__title") or
            card.query_selector("h3.base-search-card__title") or
            card.query_selector("h3")
        )
        if not title_el:
            return None
        title = title_el.inner_text().strip()
        if not title:
            return None

        link_el = (
            card.query_selector("a[href*='/jobs/view/']") or
            card.query_selector("a.job-card-container__link") or
            card.query_selector("a.base-card__full-link")
        )
        url = (link_el.get_attribute("href") or "") if link_el else ""
        if not url:
            return None
        if not url.startswith("http"):
            url = "https://www.linkedin.com" + url
        url = url.split("?")[0].rstrip("/")

        company_el = (
            card.query_selector(".job-card-container__primary-description") or
            card.query_selector("h4.base-search-card__subtitle") or
            card.query_selector("h4")
        )
        company = company_el.inner_text().strip() if company_el else "Unknown"

        location_el = card.query_selector(
            ".job-card-container__metadata-item, [class*='location']"
        )
        location = location_el.inner_text().strip() if location_el else ""

        easy_apply = False
        try:
            easy_apply = "easy apply" in card.inner_text().lower()
        except Exception:
            pass

        # Easy Apply only — skip jobs that redirect to company site
        if not easy_apply:
            return None

        return {
            "title": title, "company": company, "location": location,
            "url": url, "description": "", "salary": "",
            "easy_apply": easy_apply,
        }
    except Exception:
        return None
