# applier/apply_mode.py
"""
Apply mode: who clicks Submit.

    auto    the agent drives the site's form itself (the universal filler or a
            site adapter), as it always has
    assist  the agent prepares everything - the kit (db/kits_db.py) and an
            answer pack (applier/pack.py) - and moves the job to ready_for_review;
            YOU apply on the site yourself and record it (applier/assist.py).
            In assist mode no browser code runs: nothing opens the site, nothing
            fills a form, autonomy cannot approve it, and the Orchestrator
            refuses to submit it.

Two settings (db/settings_db.py, owner-only to change):

    apply_mode            auto | assist    the default for every site
    apply_mode_overrides  a JSON object {site_type: mode} for particular site
                          types (applier/site_type.py names). Whatever is
                          stored REPLACES the built-in default below, so
                          {} puts every site back on apply_mode.

The built-in default (also what is seeded) keeps LinkedIn and Indeed on assist -
sites that dislike automation - and everything else on auto.

The site type is the one the URL alone gives (applier/site_type.py): this runs
before any page is open, and in assist mode none ever is. A stored value that
cannot be read means assist: when in doubt, do not automate.
"""

import json
import logging

from applier.site_type import SITE_TYPES

logger = logging.getLogger(__name__)

MODES        = ("auto", "assist")
MODE_KEY     = "apply_mode"
OVERRIDES_KEY = "apply_mode_overrides"
DEFAULT_MODE = "auto"
DEFAULT_OVERRIDES = {"linkedin_easy_apply": "assist", "indeed_apply": "assist"}
SAFE_MODE    = "assist"


def clean_mode(value) -> str:
    """"auto" or "assist" (case and spaces ignored); ValueError for anything else."""
    mode = value.strip().lower() if isinstance(value, str) else value
    if mode not in MODES:
        raise ValueError(f"apply_mode must be one of: {', '.join(MODES)}")
    return mode


def parse_overrides(raw) -> dict[str, str]:
    """{site_type: mode} from a JSON object (text) or a dict. ValueError, in
    words, for anything else: not an object, an unknown site type, a bad mode."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raise ValueError("apply_mode_overrides must be a JSON object such as "
                             '{"linkedin_easy_apply": "assist"}')
    if not isinstance(raw, dict):
        raise ValueError("apply_mode_overrides must be a JSON object")
    out = {}
    for site_type, mode in raw.items():
        if site_type not in SITE_TYPES:
            raise ValueError(f"unknown site type {site_type!r}; use one of: {', '.join(SITE_TYPES)}")
        out[site_type] = clean_mode(mode)
    return out


def dump_overrides(overrides: dict[str, str]) -> str:
    """The canonical stored text: sorted keys, compact."""
    return json.dumps(overrides, sort_keys=True, separators=(",", ":"))


def resolve(site_type: str, mode: str, overrides: dict[str, str]) -> str:
    """The mode for `site_type`: its override, else the default `mode`."""
    return overrides.get(site_type, mode)


def mode_for(settings, site_type: str) -> str:
    """The apply mode in force for `site_type`, read from `settings` (anything
    with read(key, default)). Never raises: an unusable apply_mode is assist; an
    unusable apply_mode_overrides is the built-in default."""
    try:
        raw_mode = settings.read(MODE_KEY, DEFAULT_MODE)
        try:
            mode = clean_mode(raw_mode if raw_mode not in (None, "") else DEFAULT_MODE)
        except ValueError:
            logger.error(f"[ApplyMode] apply_mode {raw_mode!r} is not auto or assist; using {SAFE_MODE}")
            mode = SAFE_MODE
        raw_overrides = settings.read(OVERRIDES_KEY, "")
        if raw_overrides in (None, ""):
            overrides = dict(DEFAULT_OVERRIDES)
        else:
            try:
                overrides = parse_overrides(raw_overrides)
            except ValueError as e:
                logger.error(f"[ApplyMode] {e}; using the built-in overrides")
                overrides = dict(DEFAULT_OVERRIDES)
        return resolve(site_type, mode, overrides)
    except Exception:
        logger.exception("[ApplyMode] could not read the settings; using assist")
        return SAFE_MODE
