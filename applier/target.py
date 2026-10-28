# applier/target.py
"""
Where an application is made. A job has the page it was FOUND on (`url`, the
board card or the company page) and, when the source gave one, the form it is
APPLIED on (`apply_url`: the company's ATS page a LinkedIn or Naukri card links
out to, or the aggregator's `applicationLink`). The agent always applies on the
form, because an ATS form needs no account while the board's own apply button
usually does.

Pure helpers, no I/O. Everything that opens a page for an application goes
through `with_apply_target`, so the rest of the applier keeps reading
job["url"] and never has to know there were two.
"""


def apply_url(job: dict) -> str:
    """The page to open for this application: its ATS form if there is one,
    else the listing itself."""
    if not isinstance(job, dict):
        return ""
    return (job.get("apply_url") or "").strip() or (job.get("url") or "").strip()


def with_apply_target(job: dict | None) -> dict | None:
    """A copy of `job` whose "url" is the page to apply on, with the page it was
    found on kept as "listing_url". None stays None; a job with no apply_url is
    returned unchanged in substance."""
    if job is None:
        return None
    target = apply_url(job)
    listing = (job.get("url") or "").strip()
    if not target or target == listing:
        return job
    out = dict(job)
    out["listing_url"] = listing
    out["url"] = target
    return out
