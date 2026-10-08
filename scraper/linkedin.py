"""
scraper/linkedin.py — LinkedIn job scraper.

<<<<<<< HEAD
Scrapes ALL LinkedIn jobs matching the user's positions + locations.
No Easy Apply filter — any job with a URL is stored.
Apply flow is handled by applier/universal.py.

Login persistence via cookie storage.
=======
Login strategy (no credentials stored):
  1. Load saved cookies from data/cookies/linkedin.json
  2. Navigate to /feed — if logged in, proceed
  3. If NOT logged in → open the LinkedIn login page in the browser
     and wait up to 90 seconds for the user to log in manually
  4. After manual login, save cookies for all future runs

This means you log in once in the browser, and every subsequent
run reuses your session automatically.
>>>>>>> 4226f10 (.)
"""

import json
import logging
import random
import time
from pathlib import Path
from urllib.parse import urlencode

from core.config  import cfg
<<<<<<< HEAD
from core.browser import (detect_captcha, scroll_to_bottom,
                           load_cookies, save_cookies, human_delay)

logger = logging.getLogger(__name__)

=======
from core.browser import detect_captcha, scroll_to_bottom, load_cookies, save_cookies, human_delay

logger   = logging.getLogger(__name__)
>>>>>>> 4226f10 (.)
MAX_PAGES    = 5
COOKIES_PATH = cfg.COOKIES_DIR / "linkedin.json"


def scrape(playwright, profile: dict,
           job_titles: list[str], locations: list[str]) -> list[dict]:
<<<<<<< HEAD
    """Scrape LinkedIn for all matching jobs. Returns list of job dicts."""
=======
>>>>>>> 4226f10 (.)
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
<<<<<<< HEAD
                jobs = _search(page, title, location)
=======
                jobs = _search(page, title, location, ctx=ctx)
>>>>>>> 4226f10 (.)
                results.extend(jobs)
                human_delay(3, 6)

    except Exception as e:
        logger.error(f"[LinkedIn] Scrape error: {e}")
    finally:
        try: ctx.close()
        except Exception: pass

<<<<<<< HEAD
    logger.info(f"  [LinkedIn] Total: {len(results)} jobs found")
=======
    logger.info(f"  [LinkedIn] Total: {len(results)} jobs")
>>>>>>> 4226f10 (.)
    return results


def _ensure_logged_in(page, ctx) -> bool:
<<<<<<< HEAD
=======
    """
    Check login status. If not logged in, open the login page
    and wait for the user to log in manually (no credentials needed).
    Saves cookies after successful login.
    """
>>>>>>> 4226f10 (.)
    try:
        page.goto("https://www.linkedin.com/feed",
                  timeout=20000, wait_until="domcontentloaded")
        time.sleep(2)

<<<<<<< HEAD
        if "feed" in page.url or "mynetwork" in page.url:
            logger.info("  [LinkedIn] Already logged in ✓")
            return True

        if not cfg.LINKEDIN_EMAIL or not cfg.LINKEDIN_PASSWORD:
            logger.warning("  [LinkedIn] No credentials — log in to LinkedIn in Opera first")
            return False

        page.goto("https://www.linkedin.com/login", timeout=20000)
        time.sleep(2)
        page.fill("input#username", cfg.LINKEDIN_EMAIL)
        human_delay(0.4, 0.8)
        page.fill("input#password", cfg.LINKEDIN_PASSWORD)
        human_delay(0.4, 0.8)
        page.click("button[type='submit']")
        time.sleep(5)

        if detect_captcha(page):
            logger.warning("  [LinkedIn] CAPTCHA after login")
            return False

        if "feed" in page.url or "checkpoint" not in page.url:
            save_cookies(ctx, COOKIES_PATH, domain_filter="linkedin.com")
            logger.info("  [LinkedIn] Login successful ✓")
            return True

        logger.warning("  [LinkedIn] Login blocked (2FA/checkpoint)")
        return False

    except Exception as e:
        logger.warning(f"  [LinkedIn] Login error: {e}")
        return False


def _search(page, title: str, location: str) -> list[dict]:
=======
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
>>>>>>> 4226f10 (.)
    jobs: list[dict] = []
    try:
        params = urlencode({
            "keywords": title,
            "location": location,
<<<<<<< HEAD
            "f_TPR":    "r604800",  # last 7 days
            "sortBy":   "DD",       # most recent first
        })
        url = f"https://www.linkedin.com/jobs/search/?{params}"
        page.goto(url, timeout=25000, wait_until="domcontentloaded")
        time.sleep(random.uniform(2, 4))

        if detect_captcha(page):
            logger.warning(f"  [LinkedIn] CAPTCHA on search for '{title}'")
            return jobs
=======
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
>>>>>>> 4226f10 (.)

        if "login" in page.url or "authwall" in page.url:
            logger.warning("  [LinkedIn] Session expired mid-search")
            return jobs

        for page_num in range(1, MAX_PAGES + 1):
            scroll_to_bottom(page)
            time.sleep(2)

            cards = page.query_selector_all(
                ".jobs-search__results-list li, "
<<<<<<< HEAD
                ".job-card-container, "
                "[data-occludable-job-id]"
=======
                ".job-card-container, [data-occludable-job-id]"
>>>>>>> 4226f10 (.)
            )
            logger.info(f"    Page {page_num}: {len(cards)} cards")

            for card in cards:
                job = _parse_card(card)
                if job:
<<<<<<< HEAD
                    job["source"]         = "linkedin"
                    job["search_title"]   = title
                    job["search_location"]= location
=======
                    job["source"] = "linkedin"
>>>>>>> 4226f10 (.)
                    jobs.append(job)

            more = page.query_selector(
                "button[aria-label*='more results'], "
<<<<<<< HEAD
                "button:has-text('Show more results'), "
                "button:has-text('See more jobs')"
=======
                "button:has-text('Show more results')"
>>>>>>> 4226f10 (.)
            )
            if not more or page_num >= MAX_PAGES:
                break
            more.click()
            time.sleep(random.uniform(2, 4))

    except Exception as e:
<<<<<<< HEAD
        logger.debug(f"  [LinkedIn] Search error for '{title}': {e}")

=======
        logger.debug(f"  [LinkedIn] Search error '{title}': {e}")
>>>>>>> 4226f10 (.)
    return jobs


def _parse_card(card) -> dict | None:
    try:
        title_el = (
            card.query_selector(".job-card-list__title") or
<<<<<<< HEAD
            card.query_selector("a.job-card-container__link span[aria-hidden]") or
=======
>>>>>>> 4226f10 (.)
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
<<<<<<< HEAD
            card.query_selector(".artdeco-entity-lockup__subtitle") or
=======
>>>>>>> 4226f10 (.)
            card.query_selector("h4.base-search-card__subtitle") or
            card.query_selector("h4")
        )
        company = company_el.inner_text().strip() if company_el else "Unknown"

        location_el = card.query_selector(
<<<<<<< HEAD
            ".job-card-container__metadata-item, "
            "[class*='location'], .job-search-card__location"
        )
        location = location_el.inner_text().strip() if location_el else ""

        applicants_el = card.query_selector("[class*='applicant']")
        applicants    = applicants_el.inner_text().strip() if applicants_el else ""

        # Detect if Easy Apply (for informational purposes, not filtering)
        easy_apply = False
        try:
            card_text = card.inner_text().lower()
            easy_apply = "easy apply" in card_text or "linkedin apply" in card_text
        except Exception:
            pass

        return {
            "title":      title,
            "company":    company,
            "location":   location,
            "url":        url,
            "description":"",
            "salary":     "",
            "applicants": applicants,
=======
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
>>>>>>> 4226f10 (.)
            "easy_apply": easy_apply,
        }
    except Exception:
        return None
