# scraper/sources/harvest.py
"""
Learning company boards from job links. Every job the agent sees - a LinkedIn
card that applies on Greenhouse, a Himalayas listing whose applicationLink is
a Lever page - names a company board. Recording (vendor, slug) once means the
next search reads that board directly: more openings, no job-board account,
and the company's other roles too.

`harvest(store, jobs)` adds what is new and returns how many it added. The
company name is the job's company. Pure apart from the store writes.
"""

from scraper.sources.ats import parse_url


def boards_in(jobs: list[dict]) -> dict[tuple[str, str], str]:
    """(vendor, slug) -> company name for every ATS link among `jobs`."""
    found: dict[tuple[str, str], str] = {}
    for j in jobs or []:
        for key in ("apply_url", "url"):
            hit = parse_url(j.get(key) or "")
            if hit and hit not in found:
                found[hit] = (j.get("company") or "").strip()
    return found


def harvest(store, jobs: list[dict]) -> int:
    added = 0
    for (vendor, slug), name in boards_in(jobs).items():
        try:
            if store.add(vendor, slug, name, origin="harvest") is not None:
                added += 1
        except ValueError:            # list full or an odd slug: skip, never stop the run
            continue
    return added
