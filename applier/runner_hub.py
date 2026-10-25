# applier/runner_hub.py
"""
The server side of the runner protocol (the routes are server/routes/runner.py,
the storage db/runner_db.py, the chooser applier/executors.py).

    claim       a runner asks for work and waits up to 25 s (a long poll); it gets
                ONE task: {application, profile, answers, kit, resume, ...} or None
    events      progress while it works: timeline steps and the site type
    screenshot  a PNG of the page; stored under a name the SERVER makes
    result      the pass is over: success or an error, what it could not answer,
                the review snapshot. The application then continues exactly as if
                the form code had run here (Orchestrator.finish_pass)
    resume      the resume to upload, decrypted for this one download

Everything is per user: a hub is built for the token's user and every task,
job, bank, kit and resume it touches is looked up through that user, so a
token can only ever see or change that user's work.

WHAT THE RUNNER IS TOLD. Only what the pass needs: the job, a SUBSET of the
profile (applier/pack.py PROFILE_KEYS - no equal-opportunity facts, nothing
that is not a form fact; the profile's owner only), the answer bank for this
company, what you answered for this application, the kit and where to download
the resume. Never an API key, a setting, a cookie or another user's data.

WHAT THE SERVER BELIEVES. Almost nothing: every event, screenshot and result is
validated (types, lengths, counts, PNG signature) and what is kept is what the
server built from it. A screenshot's file name is generated here - the one the
runner sends is never read - and a result can only point at screenshots this
task uploaded, so a runner cannot make the server read or show any other file.

LEASES AND AT-MOST-ONCE. See db/runner_db.py. sweep() runs whenever a runner
polls and from the queue worker; it re-queues work from a runner that went
silent, fails an application whose task could not be done in time (a timeout:
retryable), and for a SUBMIT that went silent does NOT retry - the runner may
have clicked Submit - but parks the application in needs_you to check.
"""

import hashlib
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from applier import pack
from applier.site_type import SITE_TYPES
from core.config import cfg
from db import runner_db
from db.runner_db import (LEASE_SECONDS, MAX_EVENTS_PER_TASK, MAX_SCREENSHOTS_PER_TASK, RunnerTaskStore,
                          RunnerTokenStore)

logger = logging.getLogger(__name__)

MAX_WAIT_SECONDS  = 25
POLL_INTERVAL     = 1.0
TOUCH_EVERY       = 10.0
MAX_PNG_BYTES     = 3 * 1024 * 1024
PNG_SIGNATURE     = b"\x89PNG\r\n\x1a\n"
MAX_DIMENSION     = 20000
MAX_EVENTS_PER_REQUEST = 50
MAX_MISSING       = 50
MAX_REVIEW_FIELDS = 300

# what a pass needs the job to be doing when a task for it is handed out
STAGE_FOR_KIND = {"apply": "preparing", "refill": "preparing", "submit": "ready_for_review"}


class RunnerError(Exception):
    """Something the runner did wrong; `status` is the HTTP status to answer with."""
    status = 400


class NotFound(RunnerError):
    status = 404


class Conflict(RunnerError):
    status = 409


class TooLarge(RunnerError):
    status = 413


class TooMany(RunnerError):
    status = 429


@dataclass
class MissingField:
    """What the Orchestrator reads of a field the form could not answer
    (the same attributes as applier.form_analyzer.FormField)."""
    label: str
    field_key: str
    field_type: str = "text"
    options: list | None = None
    placeholder: str = ""


# ── validation (pure) ─────────────────────────────────────────────

def validate_png(data) -> tuple[int, int]:
    """(width, height) of a PNG, or RunnerError: it must be non-empty, at most
    3 MB, start with the PNG signature and a well-formed IHDR chunk with sane
    dimensions. The client's file name and content type are never consulted."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise RunnerError("the screenshot is empty")
    if len(data) > MAX_PNG_BYTES:
        raise TooLarge(f"the screenshot is larger than {MAX_PNG_BYTES // (1024 * 1024)} MB")
    data = bytes(data)
    if data[:8] != PNG_SIGNATURE:
        raise RunnerError("the screenshot is not a PNG (bad signature)")
    if len(data) < 33 or data[8:12] != b"\x00\x00\x00\x0d" or data[12:16] != b"IHDR":
        raise RunnerError("the screenshot is not a valid PNG (no IHDR header)")
    width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if not (1 <= width <= MAX_DIMENSION and 1 <= height <= MAX_DIMENSION):
        raise RunnerError("the screenshot has impossible dimensions")
    return width, height


def _text(value, name: str, limit: int, required: bool = False, default: str = "") -> str:
    if value is None:
        value = default
    if not isinstance(value, str):
        raise RunnerError(f"{name} must be text")
    value = value.strip()
    if required and not value:
        raise RunnerError(f"{name} is required")
    if len(value) > limit:
        raise RunnerError(f"{name} is longer than {limit} characters")
    return value


_STEP_CODE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_DATA_KEY  = re.compile(r"^[a-z][a-z0-9_]{0,29}$")


def clean_event(raw) -> dict:
    """One progress event, validated: {"event": "step", "data": {"step", "message", ...}}
    or {"event": "site_type", "data": "greenhouse"}. RunnerError otherwise."""
    if not isinstance(raw, dict):
        raise RunnerError("an event must be an object")
    kind, data = raw.get("event"), raw.get("data")
    if kind == "site_type":
        if data not in SITE_TYPES:
            raise RunnerError("unknown site type")
        return {"event": "site_type", "data": data}
    if kind == "step":
        if not isinstance(data, dict):
            raise RunnerError("a step event needs data")
        code = data.get("step")
        if not isinstance(code, str) or not _STEP_CODE.match(code):
            raise RunnerError("a step needs a code like opened_page")
        extras = {}
        for key, value in data.items():
            if key in ("step", "message") or not isinstance(key, str) or not _DATA_KEY.match(key) or len(extras) >= 10:
                continue
            if isinstance(value, str) and len(value) <= 200 or isinstance(value, (int, float, bool)) or value is None:
                extras[key] = value
        return {"event": "step", "data": {"step": code, "message": _text(data.get("message"), "message", 300, required=True),
                                           **extras}}
    raise RunnerError("unknown event type")


def _scalar_map(raw, keys: tuple, limit: int = 2000) -> dict:
    out = {}
    if not isinstance(raw, dict):
        return out
    for key in keys:
        if key not in raw:
            continue
        value = raw.get(key)
        if isinstance(value, str):
            out[key] = value[:limit]
        elif isinstance(value, (int, float, bool)) or value is None:
            out[key] = value
        elif isinstance(value, list) and key == "options":
            out[key] = [str(v)[:200] for v in value[:100] if isinstance(v, (str, int, float))]
    return out


def clean_result(body, task: dict, screenshots: list[str]) -> dict:
    """The runner's report as the dict the Orchestrator's finishers expect (the
    shape applier/universal.py's passes return). `screenshots` are the ones THIS
    task uploaded - never anything the body says."""
    if not isinstance(body, dict):
        raise RunnerError("the result must be an object")
    success = body.get("success")
    if not isinstance(success, bool):
        raise RunnerError("success must be true or false")
    error = _text(body.get("error"), "error", 1000)
    if not success and not error:
        error = "The runner reported a failure without saying why"
    missing = body.get("missing_fields") or []
    if not isinstance(missing, list) or len(missing) > MAX_MISSING:
        raise RunnerError(f"missing_fields must be a list of at most {MAX_MISSING}")
    fields = []
    for m in missing:
        if not isinstance(m, dict):
            raise RunnerError("a missing field must be an object")
        options = m.get("options") or []
        if not isinstance(options, list) or len(options) > 100:
            raise RunnerError("options must be a short list")
        fields.append(MissingField(
            label=_text(m.get("label"), "label", 300, required=True),
            field_key=_text(m.get("field_key"), "field_key", 100, required=True),
            field_type=_text(m.get("field_type"), "field_type", 40, default="text") or "text",
            options=[_text(o, "option", 200) for o in options],
            placeholder=_text(m.get("placeholder"), "placeholder", 300)))
    review = body.get("review") or {}
    review_fields = []
    if isinstance(review, dict):
        raw_fields = review.get("fields") or []
        if not isinstance(raw_fields, list) or len(raw_fields) > MAX_REVIEW_FIELDS:
            raise RunnerError(f"review.fields must be a list of at most {MAX_REVIEW_FIELDS}")
        for f in raw_fields:
            row = _scalar_map(f, ("label", "field_key", "type", "value", "options", "required", "page"))
            if isinstance(f, dict) and isinstance(f.get("hit"), dict):
                row["hit"] = _scalar_map(f["hit"], ("label", "field_key", "action", "source", "policy", "confidence", "entry_id"))
            if row.get("label"):
                review_fields.append(row)
    dry_run = body.get("dry_run")
    if dry_run is None:
        dry_run = bool(task["params"].get("dry_run", False))
    return {"success": success, "error": error, "screenshots": list(screenshots), "missing_fields": fields,
            "needs_review": True, "dry_run": bool(dry_run),
            "review": {"fields": review_fields, "url": _text(review.get("url") if isinstance(review, dict) else "", "url", 2000)}}


# ── the hub ───────────────────────────────────────────────────────

class RunnerHub:
    _wake = threading.Condition()                   # an enqueue wakes every long poll in this process
    _polling: set = set()
    _polling_lock = threading.Lock()

    def __init__(self, user_id: int, path: Path | None = None, *, clock=time.time, waiter=None,
                 orchestrator_factory=None, profile_provider=None, screenshots_dir: Path | None = None, root=None):
        self.user_id = user_id
        self.path = path or cfg.JOBS_DB_PATH
        self.clock = clock
        self._waiter = waiter
        self._orchestrator_factory = orchestrator_factory
        self._profile_provider = profile_provider or pack.owner_profile
        self._shots_dir = screenshots_dir
        self.root = root
        self.tasks = RunnerTaskStore(user_id, self.path)
        self.tokens = RunnerTokenStore(user_id, self.path)

    # ── plumbing ──────────────────────────────────────────────

    def _wait(self, seconds: float):
        if self._waiter is not None:
            self._waiter(seconds)
            return
        with self._wake:
            self._wake.wait(seconds)

    def _notify(self):
        with self._wake:
            self._wake.notify_all()

    def _jobs(self):
        from db.jobs_db import JobsDB
        return JobsDB(path=self.path, user_id=self.user_id)

    def _orchestrator(self, job: dict):
        if self._orchestrator_factory is not None:
            return self._orchestrator_factory(job)
        from applier.session import Orchestrator
        return Orchestrator(job)

    def _step(self, job_id: str, code: str, message: str, data: dict | None = None):
        try:
            self._jobs().record_step(job_id, code, message, {**(data or {}), "via": "runner"})
        except Exception:
            logger.debug("[Runner] could not record a step", exc_info=True)

    # ── queueing (server side) ────────────────────────────────

    def enqueue(self, job: dict, kind: str, params: dict | None = None) -> tuple[dict, bool]:
        task, created = self.tasks.enqueue(job["job_id"], kind, params or {}, now=self.clock())
        if created:
            self._notify()
        return task, created

    # ── claiming (runner side) ────────────────────────────────

    def claim(self, token_id: int, wait: float = MAX_WAIT_SECONDS) -> dict | None:
        """The next task for this user as a payload, or None after `wait`
        seconds (at most 25) with nothing to do. One long poll per token at a
        time: a second one is TooMany."""
        try:
            wait = min(max(float(wait), 0.0), float(MAX_WAIT_SECONDS))
        except (TypeError, ValueError):
            raise RunnerError("wait must be a number of seconds")
        key = (self.user_id, token_id)
        with self._polling_lock:
            if key in self._polling:
                raise TooMany("this token is already waiting for a task")
            self._polling.add(key)
        try:
            deadline, last_touch = self.clock() + wait, self.clock()
            while True:
                self.sweep()
                task = self._claim_valid(token_id)
                if task is not None:
                    return self.payload(task)
                remaining = deadline - self.clock()
                if remaining <= 0:
                    return None
                self._wait(min(remaining, POLL_INTERVAL))
                if self.clock() - last_touch >= TOUCH_EVERY:
                    self.tokens.touch(token_id, self.clock())
                    last_touch = self.clock()
        finally:
            with self._polling_lock:
                self._polling.discard(key)

    def _claim_valid(self, token_id: int) -> dict | None:
        """Claim the oldest task whose application is still waiting for it; tasks
        for applications that moved on (skipped, gone...) are cancelled, not handed out."""
        while True:
            task = self.tasks.claim(token_id, self.clock())
            if task is None:
                return None
            job = self._jobs().get_by_id(task["job_id"])
            want = STAGE_FOR_KIND[task["kind"]]
            if job is not None and job["stage"] == want:
                return task
            self.tasks.cancel(task["id"], self.clock(), "the application is no longer waiting for it")
            if job is not None:
                self._step(job["job_id"], "runner_task_cancelled",
                           f"The {task['kind']} task was dropped: the application is now {job['stage']}")

    # ── what the runner is told ───────────────────────────────

    def _resume(self, job: dict):
        """(filename, bytes) of the resume for this job, or None. The kit's
        variant, else the user's default, else the single resume.pdf."""
        from applier import kit
        source = kit.resume_for(job, self.path, self.root)
        if source is not None:
            try:
                return source.filename, source.store.read(source.variant_id)
            except Exception:
                logger.exception("[Runner] the chosen resume could not be read")
        legacy = Path(str(cfg.RESUME_PATH))
        if legacy.is_file():
            return "resume.pdf", legacy.read_bytes()
        return None

    def payload(self, task: dict) -> dict:
        from db.answer_bank_db import AnswerBank, normalize_company
        from db.kits_db import KitStore
        job = self._jobs().get_by_id(task["job_id"])
        session_answers, site_type = {}, ""
        try:
            from applier.session import ApplicationSession
            session = ApplicationSession.load(job["job_id"])
            if session is not None:
                session_answers, site_type = dict(session.application_answers), session.site_type
        except Exception:
            logger.debug("[Runner] no apply session for the payload", exc_info=True)
        from applier import autonomy
        profile = self._profile_provider(self.user_id) or {}
        flat = profile.get("_flat") or {}
        company = normalize_company(job.get("company", ""))
        bank = [{"id": e["id"], "question": e["question_raw"], "answer": e["answer"], "scope": e["scope"],
                 "company": e["company"], "policy": e["policy"]}
                for e in AnswerBank(self.user_id, self.path).entries(limit=2000)
                if (e["answer"] or "").strip() and (e["scope"] == "global" or (
                    e["scope"] == "company" and normalize_company(e["company"]) == company))][:pack.MAX_ANSWERS]
        kit = KitStore(self.user_id, self.path).current(job["job_id"])
        resume = self._resume(job)
        return {
            "task_id": task["id"], "kind": task["kind"], "attempt": task["attempts"], "lease_seconds": LEASE_SECONDS,
            "application": {"job_id": job["job_id"], "title": job.get("title", ""), "company": job.get("company", ""),
                            "url": pack.safe_url(job.get("url")),
                            "site_type": autonomy.site_type_for(job, site_type), "params": task["params"]},
            "profile": {k: str(flat[k]).strip() for k in pack.PROFILE_KEYS if str(flat.get(k) or "").strip()},
            "answers": {"bank": bank, "application": session_answers},
            "kit": ({"version": kit["version"], "summary": kit["summary"], "cover_letter": kit["cover_letter"],
                     "selection": kit["selection"], "resume_label": kit["variant_label"]} if kit else None),
            "resume": ({"filename": resume[0], "size": len(resume[1]), "sha256": hashlib.sha256(resume[1]).hexdigest(),
                        "url": f"/api/runner/{task['id']}/resume"} if resume else None),
        }

    def resume(self, token_id: int, task_id: str) -> tuple[str, bytes]:
        task = self._claimed(task_id, token_id)
        self.tasks.renew(task_id, token_id, self.clock())
        job = self._jobs().get_by_id(task["job_id"])
        found = self._resume(job) if job else None
        if found is None:
            raise NotFound("this application has no resume")
        return found

    # ── what the runner reports ───────────────────────────────

    def _claimed(self, task_id: str, token_id: int) -> dict:
        task = self.tasks.get_claimed(task_id, token_id)
        if task is not None:
            return task
        if self.tasks.get(task_id) is None:
            raise NotFound("no such task")                  # also what another user's task looks like
        raise Conflict("this task is not claimed by you (it may have been re-queued, finished or cancelled)")

    def events(self, token_id: int, task_id: str, events) -> dict:
        task = self._claimed(task_id, token_id)
        if not isinstance(events, list) or len(events) > MAX_EVENTS_PER_REQUEST:
            raise RunnerError(f"events must be a list of at most {MAX_EVENTS_PER_REQUEST}")
        if task["event_count"] + len(events) > MAX_EVENTS_PER_TASK:
            raise TooMany(f"a task may send at most {MAX_EVENTS_PER_TASK} events")
        clean = [clean_event(e) for e in events]
        for e in clean:
            if e["event"] == "step":
                d = dict(e["data"])
                self._step(task["job_id"], d.pop("step"), d.pop("message"), {**d, "task": task_id[:8]})
            else:
                self._set_site_type(task["job_id"], e["data"])
        self.tasks.add_events(task_id, len(clean))
        self.tasks.renew(task_id, token_id, self.clock())
        return {"accepted": len(clean), "lease_seconds": LEASE_SECONDS}

    def _set_site_type(self, job_id: str, site_type: str):
        try:
            from applier.session import ApplicationSession
            session = ApplicationSession.load(job_id)
            if session is not None:
                session.site_type = site_type
                session.save()
        except Exception:
            logger.debug("[Runner] could not record the site type", exc_info=True)

    def screenshots_dir(self) -> Path:
        return self._shots_dir or cfg.SCREENSHOTS_DIR

    def screenshot(self, token_id: int, task_id: str, data, label: str = "") -> dict:
        """Store one PNG. The name is made here: the job id, a label cleaned to
        [a-z0-9_], and 32 random bits. Nothing the runner sent names the file."""
        task = self._claimed(task_id, token_id)
        if len(task["screenshots"]) >= MAX_SCREENSHOTS_PER_TASK:
            raise TooMany(f"a task may upload at most {MAX_SCREENSHOTS_PER_TASK} screenshots")
        width, height = validate_png(data)
        job_part = re.sub(r"[^A-Za-z0-9]", "", task["job_id"])[:40] or "job"
        label_part = re.sub(r"[^a-z0-9_]", "", label.lower().replace(" ", "_"))[:24] if isinstance(label, str) else ""
        name = f"{job_part}_runner_{label_part or 'shot'}_{secrets.token_hex(4)}.png"
        directory = self.screenshots_dir()
        directory.mkdir(parents=True, exist_ok=True)
        target, tmp = directory / name, directory / (name + ".part")
        tmp.write_bytes(bytes(data))
        tmp.replace(target)
        count = self.tasks.add_screenshot(task_id, str(target))
        self.tasks.renew(task_id, token_id, self.clock())
        return {"screenshot": name, "width": width, "height": height, "count": count}

    def result(self, token_id: int, task_id: str, body) -> dict:
        """The runner is done. The task is closed first (so a repeated report is
        refused), then the application continues as if the form code had run here."""
        task = self._claimed(task_id, token_id)
        clean = clean_result(body, task, task["screenshots"])
        if not self.tasks.complete(task_id, token_id, self.clock(), "success" if clean["success"] else "error"):
            raise Conflict("this task is not claimed by you any more")
        job = self._jobs().get_by_id(task["job_id"])
        if job is None:
            return {"status": "accepted", "application": "gone"}
        orch = self._orchestrator(job)
        try:
            orch.finish_pass(task["kind"], clean, task["params"])
        except Exception as e:
            logger.exception(f"[Runner] could not continue {task['job_id'][:8]} after its result")
            try:
                orch._fail(f"Could not process the runner's result: {type(e).__name__}", e)
            except Exception:
                logger.exception("[Runner] and could not fail it either")
        return {"status": "accepted", "application": self._jobs().get_stage(task["job_id"])}

    # ── disappearing runners ──────────────────────────────────

    def sweep(self) -> list[dict]:
        """Re-queue / expire what needs it (see db/runner_db.py) and act on the
        applications involved. Safe to call from anywhere, any number of times."""
        actions = self.tasks.sweep(self.clock())
        for a in actions:
            try:
                self._apply(a)
            except Exception:
                logger.exception("[Runner] could not act on an expired task")
        if any(a["action"] == "requeued" for a in actions):
            self._notify()
        return actions

    def _apply(self, action: dict):
        task, job_id = action["task"], action["task"]["job_id"]
        job = self._jobs().get_by_id(job_id)
        if job is None:
            return
        if action["action"] == "requeued":
            self._step(job_id, "runner_lost", f"The runner went quiet; the {task['kind']} task is back in the queue "
                                                f"(claim {task['attempts']} of {runner_db.MAX_ATTEMPTS} used)")
        elif action["action"] == "lost_submit":
            self._step(job_id, "runner_lost_submit", "The runner went quiet during Submit")
            try:
                self._jobs().transition(
                    job_id, "needs_you", actor="system", reason="other",
                    note="Your runner went quiet while submitting - check on the site whether it was sent, "
                         "then mark it applied or try again",
                    detail={"via": "runner", "lost_submit": True, "task": task["id"][:8]},
                    from_stages=("ready_for_review",))
            except (ValueError, LookupError) as e:
                logger.warning(f"[Runner] could not park {job_id[:8]} after a lost submit: {e}")
        elif action["action"] == "expired":
            reason = ("no runner picked it up in time" if action.get("reason") == "queue_ttl"
                      else "the runner stopped responding too many times")
            self._step(job_id, "runner_task_expired", f"The {task['kind']} task expired: {reason}")
            if task["kind"] != "submit" and job["stage"] == "preparing":
                self._orchestrator(job)._fail(f"The runner did not finish this application: {reason} (timed out)",
                                              TimeoutError(reason))
