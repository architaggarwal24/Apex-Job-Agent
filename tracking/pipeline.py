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
import threading
import time

from core.config   import cfg
from core.shutdown import CancelEvent
from db.jobs_db    import JobsDB
from db.profile_db import ProfileDB

logger = logging.getLogger(__name__)

# The "browser" engine: the Playwright scrapers that reuse your saved login.
# The default engine for these three boards is python-jobspy (no login, no
# browser) - scraper/engines.py says which is in force for each board.
SCRAPERS = {
    "linkedin": "scraper.linkedin",
    "indeed":   "scraper.indeed",
    "naukri":   "scraper.naukri",
}


def _search_board(platform: str, pw, profile: dict, job_titles: list[str], locations: list[str],
                  settings=None, on_query=None) -> tuple[list[dict], str]:
    """(jobs, engine) for one board, by the engine its setting names."""
    import importlib
    from scraper import engines
    if settings is None:
        from db.settings_db import SettingsDB
        settings = SettingsDB.get()
    engine = engines.engine_for(platform, settings.read)
    try:
        per_query = int(engines.clean_results_per_query(settings.read("search_results_per_query", "25")))
        hours_old = int(engines.clean_hours_old(settings.read("search_hours_old", "168")))
    except ValueError:
        per_query, hours_old = 25, 168
    if engine == "jobspy":
        from scraper import jobspy_source
        return jobspy_source.search(platform, profile, job_titles, locations,
                                    results_per_query=per_query, hours_old=hours_old,
                                    on_query=on_query), engine
    if engine == "apify":
        from scraper import apify_source
        return apify_source.search(platform, profile, job_titles, locations,
                                   token=(settings.read("apify_api_token", "") or "").strip(),
                                   actor=settings.read(f"apify_actor_{platform}", "") or apify_source.DEFAULT_ACTORS[platform],
                                   results_per_query=per_query, hours_old=hours_old,
                                   on_query=on_query), engine
    mod = importlib.import_module(SCRAPERS[platform])
    return mod.scrape(pw, profile, job_titles, locations), engine


def run(
    platforms: list[str] | None = None,
    quick_mode: bool = False,
    emit=None,
    cancel_event: CancelEvent | None = None,
    user_id: int = 1,
    sources: list[str] | None = None,
    fetch=None,
    progress=None,
    run_id: int | None = None,
) -> dict:
    """
    Full search + intelligence pipeline.

    Args:
        platforms:  Which job boards to scrape (None: all three; []: none)
        quick_mode: 2 titles × 2 locations only (for testing)
        emit:       Optional callback(event, data) for live UI updates
        user_id:    Whose company list and source log (the owner runs searches)
        sources:    Which login-free sources to read (scraper/sources; default:
                    every enabled one; [] = none)
        fetch:      HTTP client for those sources (tests pass a fake)
        progress:   tracking.progress.Progress to report to (default: a private
                    one). With `run_id`, the caller already started the run.

    Returns:
        Stats dict
    """
    from tracking.progress import Progress
    if progress is None:
        progress = Progress()
    if run_id is None:
        progress.start(user_id)

    def _emit(event, data):
        if event == "log":
            progress.log(data)
        elif event == "score_progress":
            progress.set_done("score", data.get("done", 0), data.get("total", 0),
                              f"Scoring with AI · {data.get('done', 0)} of {data.get('total', 0)}")
        if emit and event != "score_progress":
            emit(event, data)

    try:
        stats = _run(platforms, quick_mode, _emit, cancel_event, user_id, sources, fetch, progress)
    except Exception as e:
        progress.finish(error=f"{type(e).__name__}: {e}")
        raise
    progress.finish(stats, error=stats.get("error", ""))
    return stats


def _run(platforms, quick_mode, emit, cancel_event, user_id, sources, fetch, progress) -> dict:
    profile_db = ProfileDB()
    jobs_db    = JobsDB()

    profile = profile_db.summary()
    flat    = profile.get("_flat", {})

    # ── Get search preferences ────────────────────────────────────
    job_titles = _get_titles(flat)
    locations  = _get_locations(flat)

    if not job_titles:
        _log(emit, "No job positions saved — go to /setup and add positions first")
        return {"total_found": 0, "new_added": 0, "scored": 0, "auto_skipped": 0,
                "error": "No job titles saved yet: add them under Get started on Today"}

    if quick_mode:
        job_titles = job_titles[:2]
        locations  = locations[:2]

    active_platforms = list(SCRAPERS.keys()) if platforms is None else platforms      # [] = no job boards

    _log(emit, f"Searching {len(active_platforms)} platform(s) | "
               f"{len(job_titles)} titles | {len(locations)} locations")
    _log(emit, f"Titles:    {', '.join(job_titles)}")
    _log(emit, f"Locations: {', '.join(locations)}")

    # ── Scrape ────────────────────────────────────────────────────
    all_jobs: list[dict] = []

    # Jobs are saved as each board / the feeds return them (not all at the end), so the
    # Inbox fills while the search runs: every save is a "created" event on the live
    # stream. Each saved job gets the pre-filter and the instant quick match at once; the
    # AI scoring (when on) still runs once, after the search.
    from intelligence.scorer import prefilter_and_fit
    seen_urls: set[str] = set()
    new_jobs: list[dict] = []          # every unique url not seen in an earlier run
    inserted_ids: list[str] = []

    def save_batch(jobs: list[dict]) -> None:
        fresh = []
        for j in jobs:
            if j["url"] not in seen_urls:
                seen_urls.add(j["url"])
                fresh.append(j)
        existing = jobs_db.exists_bulk([j["url"] for j in fresh])
        added = []
        for job in fresh:
            if job["url"] in existing:
                continue
            new_jobs.append(job)
            jid = jobs_db.add(job)
            if jid:
                job["job_id"] = jid
                inserted_ids.append(jid)
                added.append(job)
        if added:
            prefilter_and_fit(added, profile, jobs_db)
            progress.note(new=len(inserted_ids))

    from db.settings_db import SettingsDB
    from scraper import engines
    settings = SettingsDB.get()
    needs_browser = any(engines.engine_for(p, settings.read) == "browser"
                        for p in active_platforms if p in SCRAPERS)

    boards = [p for p in active_platforms if p in SCRAPERS]
    progress.begin("boards", len(boards) * len(job_titles) * len(locations), "Searching job boards")

    def _scrape_all(pw):
        for platform in active_platforms:
            if cancel_event and cancel_event.is_set():
                _log(emit, "Pipeline cancelled — stopping before scraping more platforms")
                break
            if platform not in SCRAPERS:
                logger.warning(f"Unknown platform: {platform}")
                continue
            _log(emit, f"Scraping {platform}...")
            units = len(job_titles) * len(locations)
            ticked = [0]
            label = platform.capitalize()

            def on_query(title, location, found, _label=label):
                ticked[0] += 1
                progress.tick("boards", f"{_label} · {title} in {location}", found)

            progress.note(current=f"Searching {label}")
            try:
                jobs, engine = _search_board(platform, pw, profile, job_titles, locations, settings, on_query)
                for j in jobs:
                    j.setdefault("source", platform)
                all_jobs.extend(jobs)
                save_batch(jobs)
                with_form = sum(1 for j in jobs if j.get("apply_url"))
                _log(emit, f"  {platform} ({engine}): {len(jobs)} listings, {with_form} with a company apply form")
                if ticked[0] < units:              # an engine that doesn't report per query (browser)
                    progress.tick("boards", f"{label}: {len(jobs)} listings", len(jobs) if not ticked[0] else 0,
                                  units=units - ticked[0])
            except Exception as e:
                logger.error(f"[Pipeline] {platform} failed: {e}")
                _log(emit, f"  {platform}: FAILED — {e}")
                if ticked[0] < units:
                    progress.tick("boards", f"{label}: failed", 0, units=units - ticked[0])
            time.sleep(random.uniform(2, 5))

    if needs_browser:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            _scrape_all(pw)
    else:
        _scrape_all(None)

    # ── Login-free sources: aggregators, then the company boards ──
    source_report = {}
    if sources != [] and not (cancel_event and cancel_event.is_set()):
        from db.sources_db import SourceStore
        from scraper import sources as job_sources
        try:
            found, source_report = job_sources.run(
                SourceStore(user_id), job_titles, locations, read=settings.read, fetch=fetch,
                country=flat.get("location_country") or "India", only=sources,
                cancelled=lambda: bool(cancel_event and cancel_event.is_set()),
                log=lambda msg: _log(emit, msg), progress=progress)
            all_jobs.extend(found)
            save_batch(found)
        except Exception as e:
            logger.exception("[Pipeline] sources failed")
            _log(emit, f"  sources: FAILED — {e}")

    # ── Saved as they came (save_batch); the totals ───────────────
    _log(emit, f"Total: {len(all_jobs)} | New: {len(new_jobs)} | "
               f"Duplicates skipped: {len(all_jobs)-len(new_jobs)}")
    progress.begin("save", 1, f"Saved {len(inserted_ids)} jobs")
    _log(emit, f"Inserted {len(inserted_ids)} new jobs")
    progress.note(new=len(inserted_ids))
    progress.tick("save", f"{len(inserted_ids)} new jobs saved")

    # ── Score ─────────────────────────────────────────────────────
    to_score = [j for j in new_jobs if j.get("job_id")]
    if to_score and not (cancel_event and cancel_event.is_set()):
        progress.begin("score", 1, f"Scoring {len(to_score)} jobs")
        _log(emit, f"Scoring {len(to_score)} jobs with AI...")
        from intelligence.scorer import score_jobs_batch
        scored = score_jobs_batch(to_score, profile, jobs_db=jobs_db,
                                  cancel_event=cancel_event, emit=emit)
        auto_skipped = sum(1 for j in scored if j.get("auto_skip"))
        from intelligence.scorer import _auto_skip_threshold
        _log(emit, f"Scoring done — {len(scored)-auto_skipped} queued, "
                   f"{auto_skipped} auto-skipped (score < {_auto_skip_threshold()})")
        progress.begin("score", 1)
        progress.set_done("score", 1, 1, f"{auto_skipped} auto-skipped")
    else:
        scored, auto_skipped = [], 0

    # ── Brief top jobs ────────────────────────────────────────────
    # Generate briefs for top 10 new jobs that weren't auto-skipped
    top_jobs = sorted(
        [j for j in to_score if not j.get("auto_skip")],
        key=lambda j: j.get("ai_score", 0), reverse=True
    )[:10]

    if top_jobs and not (cancel_event and cancel_event.is_set()):
        _log(emit, f"Generating AI briefs for top {len(top_jobs)} jobs...")
        progress.begin("briefs", len(top_jobs), "Writing AI briefs")
        from intelligence.briefer import generate_and_save
        for i, job in enumerate(top_jobs, 1):
            if cancel_event and cancel_event.is_set():
                _log(emit, "Pipeline cancelled — stopping brief generation")
                break
            progress.note(current=f"AI brief {i} of {len(top_jobs)} · {job.get('title', '')[:60]}")
            try:
                generate_and_save(job, profile, jobs_db)
            except Exception as e:
                logger.warning(f"Brief failed for {job.get('title','?')}: {e}")
            progress.tick("briefs")

    # ── Stats ─────────────────────────────────────────────────────
    stats = {
        "total_found":   len(all_jobs),
        "new_added":     len(inserted_ids),
        "deduped":       len(all_jobs) - len(new_jobs),
        "scored":        len(to_score),
        "auto_skipped":  auto_skipped,
        "briefed":       len(top_jobs),
        "cancelled":     bool(cancel_event and cancel_event.is_set()),
        "by_platform":   {p: sum(1 for j in all_jobs if j.get("source") == p)
                         for p in active_platforms},
        "by_source":     {name: r["found"] for name, r in source_report.items()},
        "source_errors": {name: r["error"] for name, r in source_report.items() if r["error"]},
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
