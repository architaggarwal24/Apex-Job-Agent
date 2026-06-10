"""
scraper/linkedin.py — LinkedIn job scraper.

Scrapes ALL LinkedIn jobs matching the user's positions + locations.
No Easy Apply filter — any job with a URL is stored.
Apply flow is handled by applier/universal.py.

Login persistence via cookie storage.
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
COOKIES_PATH = cfg.COOKIES_DIR / "linkedin.json"


def scrape(playwright, profile: dict,
           job_titles: list[str], locations: list[str]) -> list[dict]:
    """Scrape LinkedIn for all matching jobs. Returns list of job dicts."""
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
                jobs = _search(page, title, location)
                results.extend(jobs)
                human_delay(3, 6)

    except Exception as e:
        logger.error(f"[LinkedIn] Scrape error: {e}")
    finally:
        try: ctx.close()
        except Exception: pass

    logger.info(f"  [LinkedIn] Total: {len(results)} jobs found")
    return results


def _ensure_logged_in(page, ctx) -> bool:
    try:
        page.goto("https://www.linkedin.com/feed",
                  timeout=20000, wait_until="domcontentloaded")
        time.sleep(2)

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
    jobs: list[dict] = []
    try:
        params = urlencode({
            "keywords": title,
            "location": location,
            "f_TPR":    "r604800",  # last 7 days
            "sortBy":   "DD",       # most recent first
        })
        url = f"https://www.linkedin.com/jobs/search/?{params}"
        page.goto(url, timeout=25000, wait_until="domcontentloaded")
        time.sleep(random.uniform(2, 4))

        if detect_captcha(page):
            logger.warning(f"  [LinkedIn] CAPTCHA on search for '{title}'")
            return jobs

        if "login" in page.url or "authwall" in page.url:
            logger.warning("  [LinkedIn] Session expired mid-search")
            return jobs

        for page_num in range(1, MAX_PAGES + 1):
            scroll_to_bottom(page)
            time.sleep(2)

            cards = page.query_selector_all(
                ".jobs-search__results-list li, "
                ".job-card-container, "
                "[data-occludable-job-id]"
            )
            logger.info(f"    Page {page_num}: {len(cards)} cards")

            for card in cards:
                job = _parse_card(card)
                if job:
                    job["source"]         = "linkedin"
                    job["search_title"]   = title
                    job["search_location"]= location
                    jobs.append(job)

            more = page.query_selector(
                "button[aria-label*='more results'], "
                "button:has-text('Show more results'), "
                "button:has-text('See more jobs')"
            )
            if not more or page_num >= MAX_PAGES:
                break
            more.click()
            time.sleep(random.uniform(2, 4))

    except Exception as e:
        logger.debug(f"  [LinkedIn] Search error for '{title}': {e}")

    return jobs


def _parse_card(card) -> dict | None:
    try:
        title_el = (
            card.query_selector(".job-card-list__title") or
            card.query_selector("a.job-card-container__link span[aria-hidden]") or
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
            card.query_selector(".artdeco-entity-lockup__subtitle") or
            card.query_selector("h4.base-search-card__subtitle") or
            card.query_selector("h4")
        )
        company = company_el.inner_text().strip() if company_el else "Unknown"

        location_el = card.query_selector(
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
            "easy_apply": easy_apply,
        }
    except Exception:
        return None
