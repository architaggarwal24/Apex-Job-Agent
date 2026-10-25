# runner/runner.py
"""
runner/runner.py — the RUNNER: the program that does an application's browser work
on YOUR computer, in YOUR browser profile, for the server's runner endpoints
(server/routes/runner.py, served by applier/runner_hub.py).

    python runner.py --server https://apex.example.com --token apxr_... [--profile-dir PATH]

THE LOOP. claim a task (a long poll) -> download the resume -> open your browser
profile in a VISIBLE window -> run the same form code the server would have run
(applier/universal.py, applier/adapters/ - nothing is copied) -> stream the step
events -> upload the screenshots -> post the result -> claim the next. Ctrl+C stops it.

THE BACKEND SEAM. The loop talks to a Backend (claim / events / screenshot / result /
resume) and never to HTTP or a database directly:

    HttpBackend    the server over HTTP - what `python runner.py` uses
    LocalBackend   a RunnerHub on this computer's own database, no network

so the loop is the same whichever it is given. applier/session.py (the Orchestrator)
is untouched: the server still decides what to do with a result (finish_pass); this
file only does the browser part, as the server's own ServerExecutor does.

WHAT NEVER LEAVES THIS COMPUTER. Your site logins live in the browser profile and
are used only by that browser: the runner never reads, forwards or posts a cookie or
a password. Every request carries the bearer token and nothing else - no cookies are
accepted or sent, redirects are not followed (so the token cannot be handed to
another host), and the token is never logged. What is sent is what the server asked
for: step events, screenshots, and the result (what the form held, what it could not
answer).

WHEN THINGS GO WRONG
  * the server is down or erroring: the runner waits (2, 5, 10, 30, 60 s...) and
    tries again; progress events are kept and sent later; a result is retried for a
    few minutes. A token the server refuses (401) stops the runner.
  * a CAPTCHA or a login wall: the form code reports it; the runner brings the
    browser window forward, tells the server it is waiting for you (a timeline
    step), keeps the task alive, and when the page is clear runs the pass again - up
    to MAX_PAUSES times, HUMAN_WAIT_SECONDS each. Past that the original failure is
    reported and the application is parked for you, as ever.
  * the server re-queues or cancels the task while it runs (409/404): the runner
    drops it - the server already has it elsewhere.
  * Ctrl+C: the heartbeat stops, the browser window closes, the temporary resume file
    is deleted and NO result is posted. That is deliberate: a pass that was cut off
    may or may not have clicked Submit, so only the server's own rules (an apply or
    refill task is re-queued when its lease runs out; a Submit is never re-run, the
    application waits for you to check) may decide what happens next.

NEEDS ON THIS COMPUTER. The project's own modules (it is run from a checkout), a
browser Playwright can drive, and - only for a form no site adapter handles - the
LLM the universal filler uses, which is whatever THIS computer's .env / settings
configure: the server never sends an API key. An OPERA_EXECUTABLE_PATH in this
computer's .env is honoured, as everywhere else in the project.

KNOWN LIMITS. The task carries no per-application "confirm this saved answer" state
and the protocol has no way to say a once-only saved answer was used, so those two
behave as the server's runner protocol defines them (a suggestion to confirm comes
back as an ordinary question; a once-only answer is still in the bank afterwards).
"""

import argparse
import hashlib
import logging
import os
import re
import shutil
import sys
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from contextlib import ExitStack, contextmanager
from http.cookiejar import DefaultCookiePolicy
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:                  # run as `python runner.py` from anywhere in a checkout
    sys.path.insert(0, str(ROOT))

import requests                                                              # noqa: E402  (in requirements.txt)
from requests.auth import AuthBase                                           # noqa: E402

from applier import failures                                                 # noqa: E402
from applier.answers import AnswerContext, MemoryState                       # noqa: E402
from applier.executors import BrowserLaunchError                             # noqa: E402
from applier.runner_hub import (MAX_EVENTS_PER_REQUEST, MAX_MISSING, MAX_REVIEW_FIELDS,   # noqa: E402
                                RunnerError as WireError, validate_png)
from applier.site_type import SITE_TYPES                                     # noqa: E402
from core.browser import detect_captcha, is_login_wall                       # noqa: E402
from core.config import cfg                                                  # noqa: E402
from db.answer_bank_db import (FUZZY_THRESHOLD, compatible, normalize_company,   # noqa: E402
                               normalize_question, profile_entries, similarity)
from db.runner_db import LEASE_SECONDS, MAX_SCREENSHOTS_PER_TASK             # noqa: E402

logger = logging.getLogger("runner")

KINDS = ("apply", "refill", "submit")

CLAIM_WAIT         = 25                      # seconds the server may hold a claim (it never holds longer)
MIN_POLL_SECONDS   = 1.0                     # an empty claim that came back faster than this is not hammered
RETRY_DELAYS       = (2, 5, 10, 30, 60)      # after a failed call to the server; the last one repeats
SEND_RETRY_SECONDS = 300                     # how long a result / screenshot / resume is retried in all
HEARTBEAT_SECONDS  = LEASE_SECONDS // 4      # the lease is 120 s; renew well inside it
HUMAN_WAIT_SECONDS = 600                     # how long one CAPTCHA / login may take you
HUMAN_POLL_SECONDS = 2
MAX_PAUSES         = 3

EXIT_OK, EXIT_AUTH, EXIT_BROWSER = 0, 2, 3
DEFAULT_PROFILE = Path.home() / ".apex_runner" / "profile"

# What core/browser.py's launch_browser keeps alive for a real profile (sessions, extensions, sync).
_KEEP_PROFILE_ARGS = (
    "--disable-extensions", "--disable-background-networking", "--disable-sync", "--disable-default-apps",
    "--disable-component-extensions-with-background-pages", "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding",
    "--metrics-recording-only", "--no-first-run",
)

HUMAN_NEEDS = {"captcha": "solve the CAPTCHA", "login_required": "log in to the site"}


# ── errors the loop understands ───────────────────────────────────

class BackendError(Exception):
    """A call to the server failed. `status` is the HTTP status, None when it could not be reached."""
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class Unavailable(BackendError):
    """Unreachable, timing out, or a 5xx: try again later."""


class Unauthorized(BackendError):
    """401: the token is wrong or revoked. Trying again will not help."""


class TaskGone(BackendError):
    """404 / 409: the task is not (or no longer) this runner's - re-queued, finished or cancelled."""


class Rejected(BackendError):
    """Any other refusal (400, 413...): the request itself was wrong; repeating it will not help."""


def error_for(status: int, message: str, *, throttled_is_transient: bool = False) -> BackendError:
    """The BackendError for an HTTP status. A 429 on a claim means another poll of this
    token is still open (a runner just restarted): transient. On any other call it is a limit."""
    if status == 401:
        return Unauthorized(message, status)
    if status in (404, 409):
        return TaskGone(message, status)
    if status >= 500 or (status == 429 and throttled_is_transient):
        return Unavailable(message, status)
    return Rejected(message, status)


# ── the backend seam ──────────────────────────────────────────────

class Backend(ABC):
    """What the loop needs from the server. Every method raises a BackendError subclass."""

    @abstractmethod
    def claim(self, wait: float) -> dict | None:
        """The next task payload, or None after `wait` seconds with nothing to do."""

    @abstractmethod
    def events(self, task_id: str, events: list) -> None:
        """Progress events; an empty list only renews the task's lease."""

    @abstractmethod
    def screenshot(self, task_id: str, png: bytes, label: str) -> None:
        """One PNG for the task."""

    @abstractmethod
    def result(self, task_id: str, body: dict) -> dict:
        """The pass is over."""

    @abstractmethod
    def resume(self, task_id: str) -> bytes:
        """The resume to upload."""


class _Bearer(AuthBase):
    """The Authorization header, set here (not in `headers`) so a ~/.netrc entry can never replace it."""
    def __init__(self, token: str):
        self._token = token

    def __call__(self, request):
        request.headers["Authorization"] = f"Bearer {self._token}"
        return request

    def __repr__(self):
        return "<bearer token>"


class HttpBackend(Backend):
    """The server over HTTP (the routes in server/routes/runner.py)."""

    def __init__(self, server: str, token: str, *, session=None):
        parts = urlsplit((server or "").strip())
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("--server must be an address like https://apex.example.com")
        if not token or not token.strip():
            raise ValueError("--token is empty")
        self.base = f"{parts.scheme}://{parts.netloc}{parts.path.rstrip('/')}"
        self.secure = parts.scheme == "https" or (parts.hostname or "") in ("localhost", "127.0.0.1", "::1")
        self._session = session or requests.Session()
        self._session.auth = _Bearer(token.strip())
        self._session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))     # no cookie, either way
        self._lock = threading.Lock()                # the heartbeat and the pass share one connection pool

    def _call(self, method: str, path: str, *, timeout, claim: bool = False, **kw):
        try:
            with self._lock:
                r = self._session.request(method, self.base + path, timeout=timeout, allow_redirects=False, **kw)
        except requests.RequestException as e:
            raise Unavailable(f"could not reach the server: {type(e).__name__}: {str(e)[:150]}") from None
        if r.status_code >= 300:
            raise error_for(r.status_code, self._detail(r), throttled_is_transient=claim)
        return r

    @staticmethod
    def _detail(r) -> str:
        try:
            detail = r.json().get("detail")
            if isinstance(detail, str) and detail:
                return f"{r.status_code}: {detail[:200]}"
        except Exception:
            pass
        return f"HTTP {r.status_code}"

    @staticmethod
    def _json(r) -> dict:
        try:
            body = r.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise Unavailable("the server's answer was not what the runner protocol says (a proxy page?)", r.status_code)
        return body

    @staticmethod
    def _interruptible(fn):
        """fn() on a helper thread, waited for in short slices. A blocking socket read is not
        interrupted by Ctrl+C on Windows - the key press is only noticed when the read returns,
        up to 25 s later - so the long poll is waited for in quarter-second slices instead and
        Ctrl+C acts at once everywhere. If it does, the helper (a daemon) is left to finish alone."""
        box, done = {}, threading.Event()

        def work():
            try:
                box["value"] = fn()
            except BaseException as e:                      # handed back to the caller's thread below
                box["error"] = e
            finally:
                done.set()
        threading.Thread(target=work, daemon=True, name="runner-claim").start()
        while not done.wait(0.25):
            pass
        if "error" in box:
            raise box["error"]
        return box["value"]

    def claim(self, wait: float):
        r = self._interruptible(lambda: self._call("POST", "/api/runner/claim", json={"wait": wait},
                                                   timeout=(10, wait + 20), claim=True))
        task = self._json(r).get("task")
        if task is not None and not isinstance(task, dict):
            raise Unavailable("the server sent a task in a shape the runner does not understand", r.status_code)
        return task

    def events(self, task_id: str, events: list) -> None:
        self._call("POST", f"/api/runner/{task_id}/events", json={"events": events}, timeout=30)

    def screenshot(self, task_id: str, png: bytes, label: str) -> None:
        self._call("POST", f"/api/runner/{task_id}/screenshot", timeout=60,
                   files={"file": ("screenshot.png", png, "image/png")}, data={"label": label})

    def result(self, task_id: str, body: dict) -> dict:
        return self._json(self._call("POST", f"/api/runner/{task_id}/result", json=body, timeout=60))

    def resume(self, task_id: str) -> bytes:
        return self._call("GET", f"/api/runner/{task_id}/resume", timeout=120).content


class LocalBackend(Backend):
    """The same calls made straight on a RunnerHub (applier/runner_hub.py) over this
    computer's own database: for a runner on the machine that holds it, and so the loop
    can be run against the real server-side code without a network."""

    def __init__(self, hub, token_id: int):
        self.hub, self.token_id = hub, token_id

    def _call(self, fn, *args, claim: bool = False):
        from applier.runner_hub import RunnerError
        try:
            return fn(self.token_id, *args)
        except RunnerError as e:
            raise error_for(e.status, str(e), throttled_is_transient=claim) from None

    def claim(self, wait: float):
        return self._call(self.hub.claim, wait, claim=True)

    def events(self, task_id, events):
        self._call(self.hub.events, task_id, events)

    def screenshot(self, task_id, png, label):
        self._call(self.hub.screenshot, task_id, png, label)

    def result(self, task_id, body):
        return self._call(self.hub.result, task_id, body)

    def resume(self, task_id):
        return self._call(self.hub.resume, task_id)[1]


# ── what goes over the wire (shaped so the server accepts it) ─────

_STEP_CODE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_DATA_KEY  = re.compile(r"^[a-z][a-z0-9_]{0,29}$")


def clip(value, limit: int) -> str:
    return ("" if value is None else str(value)).strip()[:limit]


def to_wire(event: str, data) -> dict | None:
    """An event from the form code as the server's protocol has it, or None to keep it
    here. The form code's "log" lines stay on this computer; a step or the site type goes."""
    if event == "site_type":
        return {"event": "site_type", "data": data} if data in SITE_TYPES else None
    if event != "step" or not isinstance(data, dict):
        return None
    code, message = data.get("step"), clip(data.get("message"), 300)
    if not isinstance(code, str) or not _STEP_CODE.match(code) or not message:
        return None
    extras = {k: v for k, v in data.items()
              if k not in ("step", "message") and isinstance(k, str) and _DATA_KEY.match(k)
              and (v is None or isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) <= 200))}
    return {"event": "step", "data": {"step": code, "message": message, **dict(list(extras.items())[:10])}}


def result_body(result: dict) -> dict:
    """A pass's result dict as the body of POST /result: success, error, the questions it
    could not answer, the review snapshot. Lengths and counts are kept inside what the
    server accepts (it refuses the WHOLE report for one over-long value)."""
    body = {"success": bool(result.get("success")), "error": clip(result.get("error"), 1000)}
    missing = []
    for f in list(result.get("missing_fields") or [])[:MAX_MISSING]:
        label = clip(getattr(f, "label", ""), 300)
        if not label:
            continue
        missing.append({
            "label": label,
            "field_key": clip(getattr(f, "field_key", ""), 100) or "_".join(normalize_question(label).split())[:60] or "field",
            "field_type": clip(getattr(f, "field_type", ""), 40) or "text",
            "options": [clip(o, 200) for o in list(getattr(f, "options", None) or [])[:100]],
            "placeholder": clip(getattr(f, "placeholder", ""), 300)})
    body["missing_fields"] = missing
    review = result.get("review") if isinstance(result.get("review"), dict) else {}
    body["review"] = {"fields": list(review.get("fields") or [])[:MAX_REVIEW_FIELDS], "url": clip(review.get("url"), 2000)}
    if "dry_run" in result:
        body["dry_run"] = bool(result["dry_run"])
    return body


# ── the answer bank, as the server sent it ────────────────────────

class PayloadBank:
    """The answer bank as the task carried it (answers.bank): read-only, in memory. It
    stands in for db.answer_bank_db.AnswerBank in applier/answers.py's AnswerContext, with
    the same matching - exact normalised match first, then fuzzy; this company over
    global; your own answer over a profile fact; the entries arrive most recent first.
    touch() and delete() only change what THIS pass sees."""

    def __init__(self, entries):
        self._entries = []
        for rank, e in enumerate(entries or []):
            qn = normalize_question(e.get("question")) if isinstance(e, dict) else ""
            if not qn or not isinstance(e.get("answer"), str) or not e["answer"].strip():
                continue
            self._entries.append({
                "id": e.get("id"), "question_raw": str(e.get("question")), "question_norm": qn,
                "answer": e["answer"], "scope": "company" if e.get("scope") == "company" else "global",
                "company": str(e.get("company") or ""),
                "policy": e.get("policy") if e.get("policy") in ("always", "ask", "once") else "always",
                "source": "user", "rank": rank})

    def resolve(self, question, company: str = "", profile: dict | None = None) -> dict | None:
        qn = normalize_question(question)
        if not qn:
            return None
        cn = normalize_company(company)
        cands = [(e, e["question_norm"]) for e in self._entries
                 if e["scope"] != "company" or (cn and normalize_company(e["company"]) == cn)]
        for e in profile_entries(profile):
            cands.extend((e, p) for p in e["phrasings"])
        scope_rank  = lambda e: 1 if e["scope"] == "company" else 0
        source_rank = lambda e: {"user": 2, "ai": 1, "profile": 0}[e["source"]]
        fresh       = lambda e: -e.get("rank", 0)
        exact = [(e, p) for e, p in cands if p == qn]
        if exact:
            e, _ = max(exact, key=lambda ep: (scope_rank(ep[0]), source_rank(ep[0]), fresh(ep[0])))
            return self._match(e, 1.0)
        scored = [(e, similarity(qn, p)) for e, p in cands if compatible(qn, p)]
        scored = [(e, r) for e, r in scored if r >= FUZZY_THRESHOLD]
        if not scored:
            return None
        e, r = max(scored, key=lambda er: (scope_rank(er[0]), er[1], source_rank(er[0]), fresh(er[0])))
        return self._match(e, round(r, 2))

    @staticmethod
    def _match(e: dict, confidence: float) -> dict:
        return {"answer": e["answer"], "confidence": confidence, "entry_id": e["id"], "source": e["source"],
                "policy": e["policy"], "scope": e["scope"], "question": e["question_raw"]}

    def touch(self, entry_id) -> None:
        pass

    def delete(self, entry_id) -> bool:
        before = len(self._entries)
        self._entries = [e for e in self._entries if e["id"] != entry_id]
        return len(self._entries) < before


# ── the browser and the passes ────────────────────────────────────

@contextmanager
def launch(profile_dir: Path, headless: bool = False):
    """Your browser profile, in a window you can see (headless only for tests). The
    browser is Playwright's Chromium, or the one OPERA_EXECUTABLE_PATH names in this
    computer's .env when it exists."""
    from playwright.sync_api import sync_playwright
    profile_dir.mkdir(parents=True, exist_ok=True)
    exe = cfg.opera_exe()
    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir), headless=headless, no_viewport=True, accept_downloads=True,
            executable_path=exe if exe and os.path.exists(exe) else None,
            ignore_default_args=list(_KEEP_PROFILE_ARGS),
            args=["--disable-blink-features=AutomationControlled"])
        try:
            yield ctx
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def run_pass(kind: str, page, job: dict, profile: dict, params: dict, emit, answers, resume_path,
             site_type: str) -> dict:
    """One pass through the project's own form code, chosen exactly as the Orchestrator
    chooses (applier/session.py _dispatch): the site adapter for this site type if there
    is one - and if it raises UnsupportedForm, the universal filler once - else the
    universal filler. Returns the pass's result dict."""
    from applier import adapters, universal
    from applier.adapters import runner as adapter_passes
    if kind == "apply":
        name, args, kw = "run_application", (), {}
    elif kind == "refill":
        name, args, kw = "run_iteration2", (list(params.get("missing_keys") or []),), {}
    elif kind == "submit":
        name, args, kw = "run_submit", (), {"corrections": params.get("corrections"),
                                            "dry_run": bool(params.get("dry_run", False))}
    else:
        raise ValueError(f"unknown pass kind {kind!r}")
    kw.update(emit=emit, answers=answers)
    if resume_path is not None:
        kw["resume_path"] = resume_path
    adapter = adapters.adapter_for(site_type, job["url"])
    if adapter is None:
        return getattr(universal, name)(page, job, profile, *args, **kw)
    try:
        return getattr(adapter_passes, name)(adapter, page, job, profile, *args, **kw)
    except adapters.UnsupportedForm as e:
        emit("step", {"step": "adapter_fallback", "adapter": adapter.name, "reason": e.detail,
                      "message": f"The {adapter.name} adapter could not handle this form ({e.detail}) - using the general filler"})
        result = getattr(universal, name)(page, job, profile, *args, **kw)
        if not result.get("success") and result.get("error"):
            result = {**result, "error": f"{result['error']} - after the {adapter.name} adapter reported: {e}"}
        return result


def challenge_open(ctx) -> bool:
    """Is a CAPTCHA or a login wall showing in any window of this browser right now?"""
    for page in list(ctx.pages):
        try:
            if detect_captcha(page) or is_login_wall(page):
                return True
        except Exception:
            continue
    return False


def live_page(ctx):
    return ctx.pages[0] if ctx.pages else ctx.new_page()


# ── reporting while a task runs ───────────────────────────────────

class Reporter:
    """Streams a task's step events to the server. `emit` is what the form code calls. An
    event the server cannot take right now (it is down) is kept and goes out with the next
    one; a task the server no longer lets us have (404/409) sets `gone` and the rest is dropped."""

    MAX_PENDING = 200

    def __init__(self, backend: Backend, task_id: str, gone: threading.Event):
        self.backend, self.task_id, self.gone = backend, task_id, gone
        self._pending: list[dict] = []
        self._lock = threading.Lock()

    def emit(self, event: str, data=None):
        wire = to_wire(event, data)
        if wire is None:
            if event == "log" and isinstance(data, str):
                logger.info("  %s", data)
            return
        if self.gone.is_set():
            return
        with self._lock:
            self._pending = (self._pending + [wire])[-self.MAX_PENDING:]
        self.flush()

    def flush(self):
        with self._lock:
            while self._pending and not self.gone.is_set():
                batch = self._pending[:MAX_EVENTS_PER_REQUEST]
                try:
                    self.backend.events(self.task_id, batch)
                except TaskGone:
                    self.gone.set()
                    self._pending.clear()
                    return
                except Unavailable as e:
                    logger.warning("Could not send progress to the server (%s); it will be sent later", e)
                    return
                except Unauthorized:
                    raise
                except BackendError as e:
                    logger.warning("The server refused some progress events (%s); dropped", e)
                del self._pending[:len(batch)]


class Heartbeat(threading.Thread):
    """Keeps a task's lease alive while a pass is busy (an LLM call, a slow page, you
    solving a CAPTCHA): the server re-queues a task whose runner has been silent for
    LEASE_SECONDS, and an empty batch of events is the quiet way to say "still here"."""

    def __init__(self, backend: Backend, task_id: str, interval: float, gone: threading.Event):
        super().__init__(daemon=True, name="runner-heartbeat")
        self._backend, self._task_id, self._interval, self._gone = backend, task_id, interval, gone
        self._done = threading.Event()

    def run(self):
        while not self._done.wait(self._interval):
            try:
                self._backend.events(self._task_id, [])
            except TaskGone:
                self._gone.set()
                return
            except BackendError:
                continue                  # a server that is down is the main thread's business; try again next beat

    def stop(self):
        self._done.set()
        if self.is_alive() and threading.current_thread() is not self:
            self.join(timeout=5)


class _TaskFailed(Exception):
    """Something stopped the task before or around the pass; the message is the result's error."""


# ── the runner ────────────────────────────────────────────────────

class Runner:
    def __init__(self, backend: Backend, browser, *, run_pass_fn=None, sleep=time.sleep,
                 heartbeat: float = HEARTBEAT_SECONDS, human_wait: float = HUMAN_WAIT_SECONDS,
                 human_poll: float = HUMAN_POLL_SECONDS, retry_delays=RETRY_DELAYS,
                 send_retry: float = SEND_RETRY_SECONDS):
        """backend: a Backend. browser: a zero-argument callable returning a context
        manager that yields an open browser context (see launch). The rest is for tests."""
        self.backend, self.browser = backend, browser
        self.run_pass = run_pass_fn or run_pass
        self.sleep, self.heartbeat = sleep, heartbeat
        self.human_wait, self.human_poll = human_wait, human_poll
        self.retry_delays, self.send_retry = tuple(retry_delays), send_retry
        self._stop = threading.Event()
        self.exit_code = EXIT_OK

    def stop(self, code: int | None = None):
        if code is not None:
            self.exit_code = code
        self._stop.set()

    # ── the loop ──────────────────────────────────────────────

    def run(self) -> int:
        """Claim and run tasks until stopped. Returns the exit code."""
        down = 0
        try:
            while not self._stop.is_set():
                started = time.monotonic()
                try:
                    payload = self.backend.claim(CLAIM_WAIT)
                except Unauthorized as e:
                    logger.error("The server refused this runner's token (%s). It was revoked or is wrong: "
                                 "make a new one and start the runner again.", e)
                    return EXIT_AUTH
                except BackendError as e:
                    delay = self.retry_delays[min(down, len(self.retry_delays) - 1)]
                    down += 1
                    logger.warning("Cannot get work from the server (%s); trying again in %s s", e, delay)
                    self.sleep(delay)
                    continue
                if down:
                    logger.info("The server answers again")
                down = 0
                if payload is None:
                    if time.monotonic() - started < MIN_POLL_SECONDS:
                        self.sleep(MIN_POLL_SECONDS)
                    continue
                try:
                    self._handle(payload)
                except Unauthorized as e:
                    logger.error("The server refused this runner's token (%s); stopping.", e)
                    return EXIT_AUTH
                except Exception:
                    logger.exception("The runner hit a problem with a task and went on")
            return self.exit_code
        except KeyboardInterrupt:
            logger.info("Stopped (Ctrl+C).")
            return EXIT_OK

    # ── one task ──────────────────────────────────────────────

    def _handle(self, payload):
        task_id, kind, app = payload.get("task_id"), payload.get("kind"), payload.get("application")
        if not (isinstance(task_id, str) and task_id and kind in KINDS and isinstance(app, dict)):
            logger.error("The server sent a task in a shape this runner does not understand; ignoring it")
            return
        logger.info("Task %s: %s %s at %s", task_id[:8], kind, app.get("title") or "a job", app.get("company") or "?")
        gone = threading.Event()
        reporter = Reporter(self.backend, task_id, gone)
        beat = Heartbeat(self.backend, task_id, self.heartbeat, gone)
        beat.start()
        try:
            try:
                reporter.emit("step", {"step": "runner_started", "message": f"Your runner started the {kind} pass",
                                       "kind": kind})
                result = self._run_task(payload, kind, app, reporter, gone)
            except _TaskFailed as e:
                result = {"success": False, "error": str(e)}
            except BrowserLaunchError as e:
                logger.error("The browser could not be opened: %s. Close any other window that uses this "
                             "profile, then start the runner again.", e.original)
                result = {"success": False, "error": f"Browser launch failed: {e.original}"}
                self.stop(EXIT_BROWSER)
            if gone.is_set():
                logger.warning("Task %s is no longer this runner's (the server re-queued or cancelled it); dropped",
                               task_id[:8])
                return
            self._upload_screenshots(task_id, app, result, reporter, gone)
            if gone.is_set():
                logger.warning("Task %s is no longer this runner's; its result was not sent", task_id[:8])
                return
            self._deliver(task_id, result)
        except TaskGone:
            logger.warning("Task %s is no longer this runner's; dropped", task_id[:8])
        except Unavailable as e:
            logger.error("Giving up on task %s: the server stayed unreachable (%s). It will re-queue the task "
                         "when its lease runs out.", task_id[:8], e)
        finally:
            beat.stop()

    def _run_task(self, payload, kind, app, reporter: Reporter, gone: threading.Event) -> dict:
        """The pass itself. The browser window and the temporary resume file are closed and deleted
        when this returns - before anything is uploaded - and on Ctrl+C."""
        site_type = app.get("site_type") if isinstance(app.get("site_type"), str) else ""
        job = {"job_id": str(app.get("job_id") or ""), "url": str(app.get("url") or ""),
               "title": str(app.get("title") or ""), "company": str(app.get("company") or ""),
               # the form code stops short of "Apply" on Naukri (a click there IS the submission) by the job's source
               "source": "naukri" if site_type.startswith("naukri") else ""}
        if not job["job_id"] or not job["url"]:
            raise _TaskFailed("The application has no usable link (it must be a plain http or https address)")
        params = app.get("params") if isinstance(app.get("params"), dict) else {}
        profile = payload.get("profile") if isinstance(payload.get("profile"), dict) else {}
        given = payload.get("answers") if isinstance(payload.get("answers"), dict) else {}
        state = MemoryState()
        state.application_answers = {str(k): str(v) for k, v in (given.get("application") or {}).items()}
        answers = AnswerContext(PayloadBank(given.get("bank")), job["company"], profile=profile or None, state=state)
        with ExitStack() as stack:
            resume_path = self._download_resume(payload.get("resume"), payload["task_id"], stack)
            try:
                ctx = stack.enter_context(self.browser())
            except Exception as e:
                raise BrowserLaunchError(e) from e
            pauses = 0
            while True:
                try:
                    result = self.run_pass(kind, live_page(ctx), job, profile, params, reporter.emit, answers,
                                           resume_path, site_type)
                except Exception as e:                          # not KeyboardInterrupt: that is Ctrl+C
                    logger.exception("The pass failed")
                    return {"success": False, "error": f"{type(e).__name__}: {e}"}
                if result.get("success") or gone.is_set() or pauses >= MAX_PAUSES:
                    return result
                category = failures.classify(result.get("error") or "")
                if category not in HUMAN_NEEDS or not challenge_open(ctx):
                    return result                               # not something a person can clear in the window
                pauses += 1
                if not self._wait_for_human(ctx, category, reporter, gone):
                    return result

    def _download_resume(self, info, task_id: str, stack: ExitStack):
        """The task's resume as a file named as the server named it, in a private
        temporary folder that goes when the task does (like Orchestrator._resume_file)."""
        if not isinstance(info, dict):
            return None
        try:
            data = self._retrying("download the resume", lambda: self.backend.resume(task_id))
        except Rejected as e:
            raise _TaskFailed(f"The resume could not be downloaded from the server ({e})") from None
        if len(data) != info.get("size") or hashlib.sha256(data).hexdigest() != info.get("sha256"):
            raise _TaskFailed("The resume did not download intact (its size or checksum is not the server's)")
        folder = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="apex-runner-")))
        name = re.sub(r"[^A-Za-z0-9._ -]", "_", Path(str(info.get("filename") or "")).name)[:100].strip(" .") or "resume.pdf"
        path = folder / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return path

    # ── waiting for you ───────────────────────────────────────

    def _wait_for_human(self, ctx, category: str, reporter: Reporter, gone: threading.Event) -> bool:
        """The page shows a CAPTCHA or a login wall: say so (here and on the application's
        timeline), bring the window forward and wait until it is clear. True when it is."""
        need = HUMAN_NEEDS[category]
        logger.warning("PAUSED: please %s in the browser window. The runner continues by itself once the page is clear.", need)
        reporter.emit("step", {"step": "runner_waiting_for_you", "reason": category,
                               "message": f"Waiting for you: {need} in the browser window on your computer"})
        for page in list(ctx.pages):
            try:
                page.bring_to_front()
            except Exception:
                pass
        waited = 0.0
        while waited < self.human_wait and not gone.is_set():
            if not challenge_open(ctx):
                logger.info("The page is clear; continuing")
                reporter.emit("step", {"step": "runner_resumed", "message": "Thanks - your runner is continuing"})
                return True
            self.sleep(self.human_poll)
            waited += self.human_poll
        if not gone.is_set():
            logger.warning("Gave up waiting for you after %d s", int(waited))
            reporter.emit("step", {"step": "runner_gave_up_waiting",
                                   "message": f"Your runner stopped waiting for you to {need}"})
        return False

    # ── delivering ────────────────────────────────────────────

    def _retrying(self, what: str, call):
        """call() until it works, waiting between tries while the server is unavailable
        (for at most send_retry seconds in all). Other errors are the caller's."""
        waited, tries = 0.0, 0
        while True:
            try:
                return call()
            except Unavailable as e:
                if waited >= self.send_retry:
                    raise
                delay = self.retry_delays[min(tries, len(self.retry_delays) - 1)]
                tries += 1
                waited += delay
                logger.warning("Could not %s (%s); trying again in %s s", what, e, delay)
                self.sleep(delay)

    def _upload_screenshots(self, task_id: str, app: dict, result: dict, reporter: Reporter, gone: threading.Event):
        job_id = str(app.get("job_id") or "")
        for i, raw in enumerate(result.get("screenshots") or []):
            if gone.is_set():
                return
            if i >= MAX_SCREENSHOTS_PER_TASK:
                reporter.emit("step", {"step": "runner_screenshot_skipped",
                                       "message": f"Only the first {MAX_SCREENSHOTS_PER_TASK} screenshots were uploaded"})
                return
            path = Path(str(raw))
            try:
                png = path.read_bytes()
                validate_png(png)
                label = path.stem[len(job_id) + 1:] if path.stem.startswith(job_id + "_") else path.stem
                self._retrying("upload a screenshot", lambda: self.backend.screenshot(task_id, png, label or "shot"))
            except TaskGone:
                gone.set()
                return
            except (OSError, WireError, Rejected) as e:
                logger.warning("A screenshot was not uploaded: %s", e)
                reporter.emit("step", {"step": "runner_screenshot_skipped",
                                       "message": f"A screenshot was not uploaded: {clip(e, 150)}"})

    def _deliver(self, task_id: str, result: dict):
        body = result_body(result)
        try:
            self._retrying("send the result", lambda: self.backend.result(task_id, body))
        except TaskGone:
            logger.warning("The server no longer wanted the result of task %s", task_id[:8])
            return
        except Rejected as e:
            logger.error("The server refused the result of task %s: %s", task_id[:8], e)
            return
        logger.info("Task %s done: %s", task_id[:8], "success" if body["success"] else f"failed ({body['error'][:120]})")


# ── the command line ──────────────────────────────────────────────

def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="runner.py", description="Do your Apex applications' browser work on this computer, "
                                                              "in your own browser profile.")
    p.add_argument("--server", required=True, help="the server's address, e.g. https://apex.example.com")
    p.add_argument("--token", required=True, help="a runner token (apxr_...) made by the account's owner")
    p.add_argument("--profile-dir", default=None, metavar="PATH",
                   help=f"the browser profile to open (default {DEFAULT_PROFILE}); log in to your job sites in it once")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    try:
        backend = HttpBackend(args.server, args.token)
    except ValueError as e:
        print(f"runner.py: {e}", file=sys.stderr)
        return EXIT_AUTH
    if not backend.secure:
        logger.warning("%s is not https: the token travels unencrypted. Use https unless this is a private network.",
                       backend.base)
    profile = Path(args.profile_dir).expanduser() if args.profile_dir else DEFAULT_PROFILE
    logger.info("Runner started for %s - browser profile %s. Ctrl+C stops it.", backend.base, profile)
    return Runner(backend, lambda: launch(profile)).run()


if __name__ == "__main__":
    sys.exit(main())
