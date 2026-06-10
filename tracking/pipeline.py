"""
tracking/pipeline.py — Job search + intelligence pipeline.

1. Scrape LinkedIn + Indeed for user's positions + locations
2. Dedup against existing DB
3. LLM score each new job (0-100 with reason)
4. Auto-skip jobs below threshold
5. Generate brief + tailored resume for top jobs
6. Return stats
"""

import json
import logging
import random
import time

from core.config   import cfg
from db.jobs_db    import JobsDB
from db.profile_db import ProfileDB

logger = logging.getLogger(__name__)

SCRAPERS = {
    "linkedin": "scraper.linkedin",
    "indeed":   "scraper.indeed",
}


def run(
    platforms: list[str] | None = None,
    quick_mode: bool = False,
    emit=None,
) -> dict:
    """
    Full search + intelligence pipeline.

    Args:
        platforms:  Which platforms to scrape (default: all)
        quick_mode: 2 titles × 2 locations only (for testing)
        emit:       Optional callback(event, data) for live UI updates

    Returns:
        Stats dict
    """
    profile_db = ProfileDB()
    jobs_db    = JobsDB()

    profile = profile_db.summary()
    flat    = profile.get("_flat", {})

    # ── Get search preferences ────────────────────────────────────
    job_titles = _get_titles(flat)
    locations  = _get_locations(flat)

    if not job_titles:
        _log(emit, "No job positions saved — go to /setup and add positions first")
        return {"total_found": 0, "new_added": 0, "scored": 0, "auto_skipped": 0}

    if quick_mode:
        job_titles = job_titles[:2]
        locations  = locations[:2]

    active_platforms = platforms or list(SCRAPERS.keys())

    _log(emit, f"Searching {len(active_platforms)} platform(s) | "
               f"{len(job_titles)} titles | {len(locations)} locations")
    _log(emit, f"Titles:    {', '.join(job_titles)}")
    _log(emit, f"Locations: {', '.join(locations)}")

    # ── Scrape ────────────────────────────────────────────────────
    all_jobs: list[dict] = []

    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        for platform in active_platforms:
            if platform not in SCRAPERS:
                logger.warning(f"Unknown platform: {platform}")
                continue
            _log(emit, f"Scraping {platform}...")
            try:
                import importlib
                mod  = importlib.import_module(SCRAPERS[platform])
                jobs = mod.scrape(pw, profile, job_titles, locations)
                for j in jobs:
                    j.setdefault("source", platform)
                all_jobs.extend(jobs)
                _log(emit, f"  {platform}: {len(jobs)} listings")
            except Exception as e:
                logger.error(f"[Pipeline] {platform} failed: {e}")
                _log(emit, f"  {platform}: FAILED — {e}")
            time.sleep(random.uniform(2, 5))

    # ── Dedup ─────────────────────────────────────────────────────
    existing   = jobs_db.exists_bulk([j["url"] for j in all_jobs])
    new_jobs   = [j for j in all_jobs if j["url"] not in existing]
    _log(emit, f"Total: {len(all_jobs)} | New: {len(new_jobs)} | "
               f"Duplicates skipped: {len(all_jobs)-len(new_jobs)}")

    # ── Insert ────────────────────────────────────────────────────
    inserted_ids = []
    for job in new_jobs:
        jid = jobs_db.add(job)
        if jid:
            job["job_id"] = jid
            inserted_ids.append(jid)

    _log(emit, f"Inserted {len(inserted_ids)} new jobs")

    # ── Score ─────────────────────────────────────────────────────
    to_score = [j for j in new_jobs if j.get("job_id")]
    if to_score:
        _log(emit, f"Scoring {len(to_score)} jobs with AI...")
        from intelligence.scorer import score_jobs_batch
        scored = score_jobs_batch(to_score, profile, jobs_db=jobs_db)
        auto_skipped = sum(1 for j in scored if j.get("auto_skip"))
        _log(emit, f"Scoring done — {len(scored)-auto_skipped} queued, "
                   f"{auto_skipped} auto-skipped (score < {cfg.AUTO_SKIP_SCORE_THRESHOLD})")
    else:
        scored, auto_skipped = [], 0

    # ── Brief top jobs ────────────────────────────────────────────
    # Generate briefs for top 10 new jobs that weren't auto-skipped
    top_jobs = sorted(
        [j for j in to_score if not j.get("auto_skip")],
        key=lambda j: j.get("ai_score", 0), reverse=True
    )[:10]

    if top_jobs:
        _log(emit, f"Generating AI briefs for top {len(top_jobs)} jobs...")
        from intelligence.briefer import generate_and_save
        for job in top_jobs:
            try:
                generate_and_save(job, profile, jobs_db)
            except Exception as e:
                logger.warning(f"Brief failed for {job.get('title','?')}: {e}")

    # ── Stats ─────────────────────────────────────────────────────
    stats = {
        "total_found":   len(all_jobs),
        "new_added":     len(inserted_ids),
        "deduped":       len(all_jobs) - len(new_jobs),
        "scored":        len(to_score),
        "auto_skipped":  auto_skipped,
        "briefed":       len(top_jobs),
        "by_platform":   {p: sum(1 for j in all_jobs if j.get("source") == p)
                         for p in active_platforms},
    }

    _log(emit, f"Pipeline complete: {stats}")
    return stats


def _get_titles(flat: dict) -> list[str]:
    raw = flat.get("search_positions", "[]")
    try:
        t = json.loads(raw)
        if isinstance(t, list) and t:
            return [x.strip() for x in t if x.strip()]
    except Exception:
        pass
    return []


def _get_locations(flat: dict) -> list[str]:
    raw = flat.get("search_locations", "[]")
    try:
        locs = json.loads(raw)
        if isinstance(locs, list) and locs:
            locs = [x.strip() for x in locs if x.strip()]
            if not any(l.lower() == "remote" for l in locs):
                locs.append("Remote")
            return locs
    except Exception:
        pass
    return ["Remote"]


def _log(emit, msg: str):
    logger.info(f"[Pipeline] {msg}")
    if emit:
        try: emit("log", msg)
        except Exception: pass
