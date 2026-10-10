"""
scraper/indeed.py — Indeed job scraper.

Same login strategy as LinkedIn — no credentials in .env.
If not logged in, opens the login page and waits for manual login.
Cookies saved for all future runs.
"""

import json
import logging
import random
import time
from pathlib import Path
from urllib.parse import urlencode

from core.config  import cfg
from core.browser import detect_captcha, scroll_to_bottom, load_cookies, save_cookies, human_delay

logger       = logging.getLogger(__name__)
MAX_PAGES    = 5
BASE_URL     = "https://in.indeed.com"
COOKIES_PATH = cfg.COOKIES_DIR / "indeed.json"


def scrape(playwright, profile: dict,
           job_titles: list[str], locations: list[str]) -> list[dict]:
    from core.browser import launch_browser, get_page

    results: list[dict] = []
    try:
        ctx  = launch_browser(playwright)
        page = get_page(ctx)
    except Exception as e:
        logger.error(f"[Indeed] Browser launch failed: {e}")
        return results

    try:
        load_cookies(ctx, COOKIES_PATH)
        _ensure_logged_in(page, ctx)

        for title in job_titles:
            for location in locations:
                logger.info(f"  [Indeed] '{title}' in '{location}'")
                jobs = _search(page, title, location)
                results.extend(jobs)
                human_delay(2, 5)

    except Exception as e:
        logger.error(f"[Indeed] Scrape error: {e}")
    finally:
        try: ctx.close()
        except Exception: pass

    logger.info(f"  [Indeed] Total: {len(results)} jobs")
    return results


def _ensure_logged_in(page, ctx) -> bool:
    """
    Check Indeed login. If not logged in, open login page
    and wait up to 90 seconds for the user to log in manually.
    """
    try:
        page.goto(BASE_URL, timeout=20000, wait_until="domcontentloaded")
        time.sleep(2)

        if _is_logged_in(page):
            logger.info("  [Indeed] Already logged in ✓")
            save_cookies(ctx, COOKIES_PATH, domain_filter="indeed.com")
            return True

        logger.info("  [Indeed] Not logged in — opening login page for manual login")
        logger.info("  [Indeed] Please log in to Indeed in the browser window (90 seconds)...")

        page.goto(f"{BASE_URL}/account/login", timeout=20000)

        for _ in range(90):
            time.sleep(1)
            if _is_logged_in(page):
                logger.info("  [Indeed] Login detected ✓ — saving session")
                save_cookies(ctx, COOKIES_PATH, domain_filter="indeed.com")
                return True

        logger.warning("  [Indeed] Login timeout — continuing without login")
        return False

    except Exception as e:
        logger.warning(f"  [Indeed] Login check error: {e}")
        return False


def _is_logged_in(page) -> bool:
    try:
        if page.query_selector("#AccountMenu, [data-testid='AccountMenu'], .gnav-LoggedInDropdown"):
            return True
        # Also check URL — logged-in users don't land on /login
        url = page.url.lower()
        if "indeed.com" in url and "/login" not in url and "/account" not in url:
            # Check for account-related element
            if page.query_selector("[data-gnav-acct]"):
                return True
    except Exception:
        pass
    return False


def _search(page, title: str, location: str) -> list[dict]:
    jobs: list[dict] = []
    try:
        params = urlencode({
            "q":        title,
            "l":        location,
            "fromage":  "1",     # last 24 hours
            "iafilter": "1",     # Easy Apply only (Indeed Apply)
            "sort":     "date",
        })
        page.goto(f"{BASE_URL}/jobs?{params}", timeout=25000, wait_until="domcontentloaded")
        time.sleep(random.uniform(2, 4))

        if detect_captcha(page):
            logger.warning(f"  [Indeed] CAPTCHA on search for '{title}'")
            return jobs

        for page_num in range(1, MAX_PAGES + 1):
            scroll_to_bottom(page)
            time.sleep(1.5)

            cards = page.query_selector_all(
                ".job_seen_beacon, .resultContent, [class*='jobCard']"
            )
            logger.info(f"    Page {page_num}: {len(cards)} cards")

            for card in cards:
                job = _parse_card(card)
                if job:
                    job["source"] = "indeed"
                    jobs.append(job)

            next_btn = page.query_selector(
                "a[aria-label='Next Page'], a[data-testid='pagination-page-next']"
            )
            if not next_btn or page_num >= MAX_PAGES:
                break
            next_btn.click()
            time.sleep(random.uniform(2, 4))

    except Exception as e:
        logger.debug(f"  [Indeed] Search error '{title}': {e}")
    return jobs


def _parse_card(card) -> dict | None:
    try:
        title_el = (
            card.query_selector("h2.jobTitle a") or
            card.query_selector("[class*='jobTitle'] a") or
            card.query_selector("a[data-jk]")
        )
        if not title_el:
            return None
        title = title_el.inner_text().strip()
        if not title:
            return None

        jk   = title_el.get_attribute("data-jk") or ""
        href = title_el.get_attribute("href") or ""
        url  = f"{BASE_URL}/viewjob?jk={jk}" if jk else (
               href if href.startswith("http") else BASE_URL + href)
        if not url:
            return None

        company_el = card.query_selector(
            "[data-testid='company-name'], .companyName, [class*='EmployerName']"
        )
        company = company_el.inner_text().strip() if company_el else "Unknown"

        location_el = card.query_selector(
            "[data-testid='text-location'], .companyLocation"
        )
        location = location_el.inner_text().strip() if location_el else ""

        salary_el = card.query_selector("[class*='salary']")
        salary    = salary_el.inner_text().strip() if salary_el else ""

        desc_el = card.query_selector(".job-snippet, [class*='snippet']")
        desc    = desc_el.inner_text().strip() if desc_el else ""

        easy_apply   = False
        company_site = False
        try:
            ct = card.inner_text().lower()
            easy_apply   = "easily apply" in ct or "indeed apply" in ct
            company_site = "apply on company site" in ct
        except Exception:
            pass

        # Easy Apply only — skip jobs that redirect to company site
        if not easy_apply or company_site:
            return None

        return {
            "title": title, "company": company, "location": location,
            "url": url, "description": desc, "salary": salary,
            "easy_apply": easy_apply, "company_site": company_site,
        }
    except Exception:
        return None
