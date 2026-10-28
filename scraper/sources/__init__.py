# scraper/sources/__init__.py
"""
The login-free job sources and how a search run uses them.

  SOURCES          every aggregator and ATS vendor, with its label, kind and the
                   least time between two fetches (Remotive asks for very few calls)
  enabled_sources  which ones the `search_sources` setting (a JSON object
                   {name: bool}) leaves on - all of them by default
  run              one search over the enabled sources for one user: the
                   aggregators first, then every ATS link seen so far is
                   harvested into the company list, then the company boards.
                   Returns (jobs, report) and never raises for one bad source.

A board's own search (LinkedIn, Indeed, Naukri) is scraper/jobspy_source.py;
this is everything else.
"""

import json
import logging
import time
from dataclasses import dataclass, field

from scraper.sources import aggregators, ats, harvest
from scraper.sources.base import Fetcher, SourceError

logger = logging.getLogger(__name__)

AGGREGATORS = {
    "himalayas": ("Himalayas", 0),
    "jobicy":    ("Jobicy", 3600),
    "remotive":  ("Remotive", 6 * 3600),
    "remoteok":  ("Remote OK", 1800),
    "wwr":       ("We Work Remotely", 1800),
    "hn_hiring": ("HN: Who is hiring?", 6 * 3600),
    "arbeitnow": ("Arbeitnow", 1800),
    "adzuna":    ("Adzuna", 0),
    "jsearch":   ("JSearch (Google Jobs)", 0),
}
# the settings a keyed source cannot run without (it is skipped, not failed, until they are set)
NEEDS_KEY = {"adzuna": ("adzuna_app_id", "adzuna_api_key"), "jsearch": ("jsearch_api_key",)}
# settings a source uses when present
KEY_SETTINGS = ("adzuna_app_id", "adzuna_api_key", "jsearch_api_key", "himalayas_api_key")
ATS_LABELS = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "smartrecruiters": "SmartRecruiters",
              "workable": "Workable", "recruitee": "Recruitee", "teamtailor": "Teamtailor", "personio": "Personio",
              "breezy": "Breezy", "pinpoint": "Pinpoint", "bamboohr": "BambooHR", "polymer": "Polymer"}

# name -> {"label", "kind": "aggregator" | "ats", "min_interval"}
SOURCES = {**{n: {"label": l, "kind": "aggregator", "min_interval": gap, "needs_key": list(NEEDS_KEY.get(n, ()))}
              for n, (l, gap) in AGGREGATORS.items()},
           **{v: {"label": ATS_LABELS[v], "kind": "ats", "min_interval": 0, "needs_key": []} for v in ats.VENDORS}}
SETTING_KEY = "search_sources"


def parse_sources_setting(raw) -> dict[str, bool]:
    """{name: enabled} from the setting's JSON text (or a dict). ValueError when
    it isn't an object of known names and booleans."""
    if isinstance(raw, dict):
        data = raw
    else:
        try:
            data = json.loads(raw or "{}")
        except ValueError:
            raise ValueError("search_sources must be a JSON object such as {\"remotive\": false}") from None
        if not isinstance(data, dict):
            raise ValueError("search_sources must be a JSON object such as {\"remotive\": false}")
    out = {}
    for name, flag in data.items():
        if name not in SOURCES:
            raise ValueError(f"unknown source {name!r}; one of: {', '.join(SOURCES)}")
        if not isinstance(flag, bool):
            raise ValueError(f"search_sources.{name} must be true or false")
        out[name] = flag
    return out


def dump_sources_setting(flags: dict[str, bool]) -> str:
    return json.dumps({k: v for k, v in sorted(flags.items())}, separators=(",", ":"))


def enabled_sources(read) -> list[str]:
    """The source names left on by the setting (`read(key, default)` is SettingsDB.read)."""
    try:
        flags = parse_sources_setting(read(SETTING_KEY, "{}"))
    except ValueError:
        flags = {}
    return [n for n in SOURCES if flags.get(n, True)]


@dataclass
class Context:
    titles: list[str]
    locations: list[str]
    fetch: object = field(default_factory=Fetcher)
    country: str = "India"
    names: dict = field(default_factory=dict)     # (vendor, slug) -> company name the owner gave
    keys: dict = field(default_factory=dict)      # setting name -> value, for the keyed sources
    hours_old: int = 168

    def company_name(self, slug: str, fallback: str = "") -> str:
        from db.sources_db import pretty_name
        for key, name in self.names.items():
            if key[1] == slug and name:
                return name
        return (fallback or "").strip() or pretty_name(slug)


def run(store, titles: list[str], locations: list[str], *, read, fetch=None, country: str = "India",
        only: list[str] | None = None, cancelled=lambda: False, log=lambda msg: None, clock=time.time,
        progress=None) -> tuple[list[dict], dict]:
    """One search over the sources for `store`'s user. `read` is SettingsDB.read,
    `only` restricts to some source names (None = every enabled one). The report
    maps source -> {"found", "error", "skipped"}."""
    fetch = fetch or Fetcher()
    wanted = enabled_sources(read)
    if only is not None:
        wanted = [n for n in wanted if n in only]
    keys = {k: (read(k, "") or "").strip() for k in KEY_SETTINGS}
    try:
        hours_old = int(read("search_hours_old", "168") or 0)
    except ValueError:
        hours_old = 168
    ctx = Context(titles=titles, locations=locations, fetch=fetch, country=country, keys=keys, hours_old=hours_old)
    jobs, report = [], {}

    def _run(name, fn, *args):
        missing = [k for k in SOURCES[name]["needs_key"] if not keys.get(k)]
        if missing:
            report[name] = {"found": 0, "error": "", "skipped": f"needs a key in Settings ({', '.join(missing)})"}
            return []
        last = store.last_run(name)
        gap = SOURCES[name]["min_interval"]
        if gap and last and not last["error"] and clock() - last["ran_at"] < gap:
            report[name] = {"found": 0, "error": "", "skipped": f"asked {int((clock() - last['ran_at']) / 60)} min ago; waits {gap // 60} min between calls"}
            return []
        try:
            found = fn(*args)
        except SourceError as e:
            logger.warning(f"[Sources] {name}: {e}")
            store.record_run(name, 0, str(e))
            report[name] = {"found": 0, "error": str(e)[:300], "skipped": ""}
            return []
        except Exception as e:                       # a bug in one source must not end the run
            logger.exception(f"[Sources] {name} crashed")
            store.record_run(name, 0, f"{type(e).__name__}: {e}")
            report[name] = {"found": 0, "error": f"{type(e).__name__}: {e}"[:300], "skipped": ""}
            return []
        store.record_run(name, len(found))
        report[name] = {"found": len(found), "error": "", "skipped": ""}
        return found

    # 1. aggregators
    feeds = [n for n in wanted if SOURCES[n]["kind"] == "aggregator"]
    if progress:
        progress.begin("sources", len(feeds), "Searching job feeds")
    for name in wanted:
        if cancelled():
            break
        if SOURCES[name]["kind"] != "aggregator":
            continue
        log(f"Searching {SOURCES[name]['label']}...")
        if progress:
            progress.note(current=f"Searching {SOURCES[name]['label']}")
        found = _run(name, getattr(aggregators, name), ctx)
        if progress:
            progress.tick("sources", f"{SOURCES[name]['label']}: {len(found)} found", len(found))
        for j in found:
            j.setdefault("source", name)
        jobs.extend(found)
        if report[name].get("skipped"):
            log(f"  {SOURCES[name]['label']}: skipped ({report[name]['skipped']})")
        elif report[name]["error"]:
            log(f"  {SOURCES[name]['label']}: FAILED — {report[name]['error']}")
        else:
            log(f"  {SOURCES[name]['label']}: {len(found)} matching listings")

    # 2. learn company boards from every link seen so far
    learnt = harvest.harvest(store, jobs)
    if learnt:
        log(f"Learnt {learnt} new company board(s) from job links")

    # 3. the company boards
    companies = [c for c in store.companies(enabled_only=True) if c["vendor"] in wanted]
    ctx.names = {(c["vendor"], c["slug"]): c["name"] for c in companies}
    by_vendor: dict[str, list[dict]] = {}
    for c in companies:
        by_vendor.setdefault(c["vendor"], []).append(c)
    if progress:
        progress.begin("companies", len(companies), f"Reading {len(companies)} company board(s)")
    for vendor, rows in by_vendor.items():
        if cancelled():
            break
        log(f"Searching {len(rows)} {ATS_LABELS[vendor]} board(s)...")
        total, errors = 0, 0
        for c in rows:
            if cancelled():
                break
            try:
                found = ats.SEARCH[vendor](c["slug"], ctx)
            except SourceError as e:
                errors += 1
                store.record_company_run(vendor, c["slug"], None, str(e))
                if progress:
                    progress.tick("companies", f"{c['name']} ({ATS_LABELS[vendor]}): failed")
                continue
            except Exception as e:
                errors += 1
                logger.exception(f"[Sources] {vendor}/{c['slug']} crashed")
                store.record_company_run(vendor, c["slug"], None, f"{type(e).__name__}: {e}")
                if progress:
                    progress.tick("companies", f"{c['name']} ({ATS_LABELS[vendor]}): failed")
                continue
            store.record_company_run(vendor, c["slug"], len(found))
            if progress:
                progress.tick("companies", f"{c['name']} ({ATS_LABELS[vendor]}): {len(found)} found", len(found))
            for j in found:
                j.setdefault("source", vendor)
            jobs.extend(found)
            total += len(found)
        store.record_run(vendor, total, f"{errors} board(s) failed" if errors else "")
        report[vendor] = {"found": total, "error": f"{errors} board(s) failed" if errors else "", "skipped": ""}
        log(f"  {ATS_LABELS[vendor]}: {total} matching listings" + (f", {errors} board(s) failed" if errors else ""))

    # the boards may have shown new companies too (a Greenhouse job linking to Lever is rare, but free)
    learnt = harvest.harvest(store, jobs)
    if learnt:
        log(f"Learnt {learnt} more company board(s) for the next run")
    return jobs, report
