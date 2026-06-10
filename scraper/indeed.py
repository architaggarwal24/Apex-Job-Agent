"""
scraper/indeed.py — Indeed job scraper.

Scrapes ALL Indeed jobs matching user's positions + locations.
No iafilter — includes both Easy Apply and company site redirects.
Apply flow handled by applier/universal.py.
"""

import json
import logging
import random
import time
from pathlib import Path
from urllib.parse import urlencode

from core.config  import cfg
from core.browser import (detect_captcha, scroll_to_bottom,
                           load_cookies, save_cookies, human_delay)

logger = logging.getLogger(__name__)

MAX_PAGES    = 5
BASE_URL     = "https://in.indeed.com"
COOKIES_PATH = cfg.COOKIES_DIR / "indeed.json"


def scrape(playwright, profile: dict,
           job_titles: list[str], locations: list[str]) -> list[dict]:
    """Scrape Indeed for all matching jobs. Returns list of job dicts."""
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

    logger.info(f"  [Indeed] Total: {len(results)} jobs found")
    return results


def _ensure_logged_in(page, ctx) -> bool:
    try:
        page.goto(BASE_URL, timeout=20000, wait_until="domcontentloaded")
        time.sleep(2)

        if page.query_selector("#AccountMenu, [data-testid='AccountMenu'], .gnav-LoggedInDropdown"):
            logger.info("  [Indeed] Already logged in ✓")
            return True

        if not cfg.INDEED_EMAIL or not cfg.INDEED_PASSWORD:
            logger.warning("  [Indeed] No credentials — log in to Indeed in Opera first")
            return False

        page.goto(f"{BASE_URL}/account/login", timeout=20000)
        time.sleep(2)

        email_input = page.query_selector("input[type='email'], input[name='__email']")
        if email_input:
            email_input.fill(cfg.INDEED_EMAIL)
            time.sleep(0.5)
            page.keyboard.press("Enter")
            time.sleep(2)

        pw_input = page.query_selector("input[type='password']")
        if pw_input:
            pw_input.fill(cfg.INDEED_PASSWORD)
            time.sleep(0.5)
            page.keyboard.press("Enter")
            time.sleep(3)

        save_cookies(ctx, COOKIES_PATH, domain_filter="indeed.com")
        logger.info("  [Indeed] Login attempted ✓")
        return True

    except Exception as e:
        logger.warning(f"  [Indeed] Login error: {e}")
        return False


def _search(page, title: str, location: str) -> list[dict]:
    jobs: list[dict] = []
    try:
        params = urlencode({
            "q":       title,
            "l":       location,
            "fromage": "7",     # last 7 days
            "sort":    "date",  # newest first
        })
        url = f"{BASE_URL}/jobs?{params}"
        page.goto(url, timeout=25000, wait_until="domcontentloaded")
        time.sleep(random.uniform(2, 4))

        if detect_captcha(page):
            logger.warning(f"  [Indeed] CAPTCHA on search for '{title}'")
            return jobs

        for page_num in range(1, MAX_PAGES + 1):
            scroll_to_bottom(page)
            time.sleep(1.5)

            cards = page.query_selector_all(
                ".job_seen_beacon, .resultContent, "
                "[class*='jobCard'], li.css-5lfssm"
            )
            logger.info(f"    Page {page_num}: {len(cards)} cards")

            for card in cards:
                job = _parse_card(card)
                if job:
                    job["source"]          = "indeed"
                    job["search_title"]    = title
                    job["search_location"] = location
                    jobs.append(job)

            next_btn = page.query_selector(
                "a[aria-label='Next Page'], "
                "a[data-testid='pagination-page-next'], "
                "a.np[aria-label='Next']"
            )
            if not next_btn or page_num >= MAX_PAGES:
                break
            next_btn.click()
            time.sleep(random.uniform(2, 4))

    except Exception as e:
        logger.debug(f"  [Indeed] Search error for '{title}': {e}")

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

        company_el = (
            card.query_selector("[data-testid='company-name'], .companyName") or
            card.query_selector("span[class*='company'], [class*='EmployerName']")
        )
        company = company_el.inner_text().strip() if company_el else "Unknown"

        location_el = card.query_selector(
            "[data-testid='text-location'], .companyLocation, [class*='location']"
        )
        location = location_el.inner_text().strip() if location_el else ""

        salary_el = card.query_selector("[class*='salary'], [data-testid*='salary']")
        salary    = salary_el.inner_text().strip() if salary_el else ""

        desc_el = card.query_selector(".job-snippet, [class*='snippet']")
        desc    = desc_el.inner_text().strip() if desc_el else ""

        # Detect apply type (informational only, not used for filtering)
        easy_apply = False
        company_site = False
        try:
            card_text = card.inner_text().lower()
            easy_apply   = "easily apply" in card_text or "indeed apply" in card_text
            company_site = "apply on company site" in card_text
        except Exception:
            pass

        return {
            "title":        title,
            "company":      company,
            "location":     location,
            "url":          url,
            "description":  desc,
            "salary":       salary,
            "easy_apply":   easy_apply,
            "company_site": company_site,
        }
    except Exception:
        return None
