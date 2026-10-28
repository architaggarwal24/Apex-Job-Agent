# scraper/engines.py
"""
Which engine searches each job board, and the knobs they share. Settings:

  search_engine_<board>     jobspy   python-jobspy, no login (the default)
                            apify    an Apify Store actor (scraper/apify_source.py; needs
                                     apify_api_token, the actor is apify_actor_<board>)
                            browser  the Playwright scraper that reuses your saved
                                     login (scraper/linkedin.py, indeed.py, naukri.py)
  search_results_per_query  postings asked for per title x location, per board (1-200)
  search_hours_old          only postings newer than this many hours (0 = any age)

Pure validation here; tracking/pipeline.py reads the values.
"""

BOARDS  = ("linkedin", "indeed", "naukri")
ENGINES = ("jobspy", "apify", "browser")
DEFAULT_ENGINE = "jobspy"
ENGINE_KEYS = {b: f"search_engine_{b}" for b in BOARDS}


def clean_engine(value) -> str:
    v = str(value or "").strip().lower()
    if v not in ENGINES:
        raise ValueError(f"search engine must be one of: {', '.join(ENGINES)}")
    return v


def clean_results_per_query(value) -> str:
    try:
        n = int(str(value).strip())
    except ValueError:
        n = -1
    if not 1 <= n <= 200:
        raise ValueError("search_results_per_query must be a whole number from 1 to 200")
    return str(n)


def clean_hours_old(value) -> str:
    try:
        n = int(str(value).strip())
    except ValueError:
        n = -1
    if not 0 <= n <= 8760:
        raise ValueError("search_hours_old must be a whole number of hours, 0 to 8760 (0 = any age)")
    return str(n)


def engine_for(board: str, read) -> str:
    """`read(key, default)` is SettingsDB.read; an unknown stored value falls
    back to the default rather than stopping a search."""
    try:
        return clean_engine(read(ENGINE_KEYS[board], DEFAULT_ENGINE))
    except (ValueError, KeyError):
        return DEFAULT_ENGINE
