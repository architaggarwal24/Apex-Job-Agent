# tests/test_runner_client.py
"""
tests/test_runner_client.py — the RUNNER (runner/runner.py): the program on your own
computer that claims tasks from the server and does their browser work.

  * claim -> run -> result: a task is claimed, its resume downloaded (checksum checked)
    and handed to the pass as a file, step events streamed, screenshots uploaded BEFORE
    the result, the result posted; the browser window and the resume file are gone after;
  * the passes: the right form code per task kind, the site adapter chosen as the
    Orchestrator chooses it, a Naukri task never clicks Apply on the first pass;
  * pause-for-human: a CAPTCHA / login wall parks the task visibly, keeps its lease
    alive, and the pass runs again once the page is clear - never when nothing is
    showing (no second Submit), at most three times, and not forever;
  * reconnect: server errors and dropped connections are waited out with a growing
    delay, progress and results are kept and re-sent, a revoked token stops the runner,
    a task the server took back is dropped;
  * Ctrl+C: idle, mid-pass, while waiting for you, and a real SIGINT - clean exit, the
    window closed, the resume file deleted, and NO result posted;
  * nothing secret leaves: no cookie or password, the token only in Authorization, never
    in a log, no redirects followed;
  * the Backend seam: HttpBackend against a fake server, LocalBackend against the real
    RunnerHub and Orchestrator.finish_pass;
  * the real thing: real Chromium (headless here), the real Greenhouse adapter on the saved
    fixture, driven through the real HTTP client.

The fake server checks everything it is sent with the SAME functions the real server
uses (clean_event, clean_result, validate_png), so a body the runner builds that the
real server would refuse fails here. The FastAPI routes themselves are in
tests/test_runner.py (RunnerHttpTests); live job sites were NOT tested.

Run from the project root:   python3 -m unittest discover -s tests -v
Real localhost sockets and throwaway SQLite files; Chromium only for BrowserTests (it
skips without one); no outside network.
"""

import contextlib
import hashlib
import io
import json
import logging
import os
import signal
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
os.environ.setdefault("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())

from applier import adapters, failures, universal
from applier.adapters import form
from applier.adapters import runner as adapter_passes
from applier.adapters.base import UnsupportedForm
from applier.answers import AnswerContext
from applier.form_analyzer import FormAnalysis, FormField
from applier.runner_hub import RunnerError, RunnerHub, clean_event, clean_result, validate_png
from applier.session import ApplicationSession
from core.config import cfg
from db.answer_bank_db import AnswerBank
from db.jobs_db import JobsDB
from db.runner_db import RunnerTaskStore, RunnerTokenStore
from runner import runner as R

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GREENHOUSE_HTML = (FIXTURES / "greenhouse_application.html").read_text(encoding="utf-8")
GH_URL = "https://boards.greenhouse.io/acme/jobs/4012345"
PDF = b"%PDF-1.4\n% the chosen resume\n"
PROFILE = {"full_name": "Priya Sharma", "first_name": "Priya", "last_name": "Sharma", "email": "priya@example.com",
           "phone": "+91 98765 43210", "linkedin_url": "https://linkedin.com/in/priya", "authorized_to_work": "Yes",
           "require_sponsorship": "No"}


def make_png(width: int = 4, height: int = 3) -> bytes:
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def wait_for(condition, timeout: float = 5.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if condition():
            return True
        time.sleep(0.01)
    return bool(condition())


# ── a fake server: the runner endpoints over real HTTP ────────────

class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):        # a connection we dropped on purpose is not news
        pass


class FakeServer:
    """The five runner endpoints. Everything it accepts has passed the real server's own
    validators. `script[endpoint]` is a list consumed one request at a time: None = answer
    normally, an int = answer with that status, "drop" = hang up without answering."""
    TOKEN = "apxr_" + "T" * 43

    def __init__(self):
        self.tasks, self.claimed, self.gone = [], {}, set()
        self.requests, self.log = [], []               # every request; the endpoints hit, in order
        self.events, self.shots, self.results = [], [], []
        self.beats = 0                                  # empty event batches: the heartbeat
        self.script = {}
        self.resume_bytes = {}
        self.claim_wait = 0.05
        self.hold_claims = False                        # block a claim (for the SIGINT test)
        self.claim_started = threading.Event()
        self.on_result = None
        self.set_cookie_on_claim = False
        self.redirect_claims_to = None
        self.lock = threading.Lock()
        server = self
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self): server._serve(self, "POST")
            def do_GET(self): server._serve(self, "GET")
            def log_message(self, *a): pass
        self.httpd = _QuietServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # what the tests read
    def endpoint_hits(self, name):
        return [r for r in self.requests if r["endpoint"] == name]

    def step_codes(self):
        return [e["data"]["step"] for e in self.events if e["event"] == "step"]

    # ── serving ───────────────────────────────────────────────

    def _reply(self, h, status, body=None, headers=None, raw=None):
        payload = raw if raw is not None else json.dumps(body if body is not None else {}).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/octet-stream" if raw is not None else "application/json")
        h.send_header("Content-Length", str(len(payload)))
        for k, v in (headers or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(payload)

    def _serve(self, h, method):
        length = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(length) if length else b""
        path = h.path
        endpoint = "claim" if path.endswith("/claim") else path.rsplit("/", 1)[-1]
        task_id = "" if endpoint == "claim" else path.split("/")[3]
        with self.lock:
            self.requests.append({"method": method, "path": path, "endpoint": endpoint, "headers": dict(h.headers),
                                  "body": body, "task": task_id})
            action = (self.script.get(endpoint) or [None]).pop(0) if self.script.get(endpoint) else None
        if h.headers.get("Authorization") != f"Bearer {self.TOKEN}":
            return self._reply(h, 401, {"detail": "Invalid or revoked runner token"})
        if action == "drop":
            h.close_connection = True
            h.connection.shutdown(socket.SHUT_RDWR)
            return
        if isinstance(action, int):
            return self._reply(h, action, {"detail": f"scripted {action}"})
        if endpoint == "claim":
            return self._claim(h, body)
        if task_id not in self.claimed or task_id in self.gone:
            return self._reply(h, 409, {"detail": "this task is not claimed by you"})
        try:
            if endpoint == "events":
                return self._events(h, task_id, body)
            if endpoint == "screenshot":
                return self._screenshot(h, task_id, body)
            if endpoint == "result":
                return self._result(h, task_id, body)
            if endpoint == "resume":
                data = self.resume_bytes.get(task_id)
                self.log.append("resume")
                return self._reply(h, 404, {"detail": "no resume"}) if data is None else self._reply(h, 200, raw=data)
        except RunnerError as e:
            return self._reply(h, e.status, {"detail": str(e)})
        self._reply(h, 404, {"detail": "no such endpoint"})

    def _claim(self, h, body):
        self.log.append("claim")
        if self.redirect_claims_to:
            return self._reply(h, 302, {}, headers={"Location": self.redirect_claims_to})
        headers = {"Set-Cookie": "sid=SERVER-COOKIE; Path=/"} if self.set_cookie_on_claim else {}
        if self.hold_claims:
            self.claim_started.set()
            time.sleep(10)
        end = time.time() + self.claim_wait
        while time.time() < end:
            with self.lock:
                if self.tasks:
                    task = self.tasks.pop(0)
                    self.claimed[task["task_id"]] = task
                    return self._reply(h, 200, {"task": task}, headers=headers)
            time.sleep(0.01)
        self._reply(h, 200, {"task": None}, headers=headers)

    def _events(self, h, task_id, body):
        events = json.loads(body).get("events")
        if not isinstance(events, list) or len(events) > 50:
            raise RunnerError("events must be a list of at most 50")
        clean = [clean_event(e) for e in events]
        with self.lock:
            self.events.extend(clean)
            if not events:
                self.beats += 1
        self.log.append("events")
        self._reply(h, 200, {"accepted": len(clean), "lease_seconds": 120})

    def _screenshot(self, h, task_id, body):
        ctype = h.headers["Content-Type"].encode()
        msg = BytesParser(policy=policy.default).parsebytes(b"Content-Type: " + ctype + b"\r\nMIME-Version: 1.0\r\n\r\n" + body)
        parts = {p.get_param("name", header="content-disposition"): p for p in msg.iter_parts()}
        png = parts["file"].get_payload(decode=True)
        label = parts["label"].get_content() if "label" in parts else ""
        width, height = validate_png(png)
        with self.lock:
            self.shots.append({"label": label, "png": png, "size": (width, height), "task": task_id})
        self.log.append("screenshot")
        self._reply(h, 200, {"screenshot": f"shot{len(self.shots)}.png", "width": width, "height": height, "count": len(self.shots)})

    def _result(self, h, task_id, body):
        data = json.loads(body)
        task = self.claimed[task_id]
        clean_result(data, {"params": task["application"]["params"]}, [f"shot{i}.png" for i, s in enumerate(self.shots)])
        with self.lock:
            self.results.append({"task": task_id, "body": data, "shots_before": len(self.shots)})
        self.log.append("result")
        if self.on_result:                      # BEFORE the answer: once the runner has it, it may loop straight back to claim
            self.on_result(task_id, data)
        self._reply(h, 200, {"status": "accepted", "application": "ready_for_review"})


def make_task(kind="apply", *, task_id="a" * 32, site_type="greenhouse", url=GH_URL, params=None, resume=None,
              bank=None, application_answers=None, profile=None, job_id="job-1"):
    return {"task_id": task_id, "kind": kind, "attempt": 1, "lease_seconds": 120,
            "application": {"job_id": job_id, "title": "Backend Engineer", "company": "Acme", "url": url,
                            "site_type": site_type, "params": params or {}},
            "profile": dict(PROFILE if profile is None else profile),
            "answers": {"bank": bank or [], "application": application_answers or {}},
            "kit": None, "resume": resume}


def resume_info(task_id: str, data: bytes = PDF, filename: str = "Priya Sharma Resume.pdf"):
    return {"filename": filename, "size": len(data), "sha256": hashlib.sha256(data).hexdigest(),
            "url": f"/api/runner/{task_id}/resume"}


# ── a fake browser ────────────────────────────────────────────────

class _Element:
    def is_visible(self):
        return True


class FakePage:
    def __init__(self, ctx):
        self.ctx, self.url, self.fronted = ctx, "https://careers.example.com/job/1", 0

    def query_selector(self, selector):
        return _Element() if self.ctx.captcha and selector == "#captcha" else None

    def title(self):
        return "Backend Engineer"

    def inner_text(self, selector="body"):
        return "Backend Engineer - password field holds hunter2"

    def bring_to_front(self):
        self.fronted += 1


class FakeContext:
    """A context that fails the test if the runner so much as looks at the user's cookies."""
    def __init__(self):
        self.captcha, self.closed = False, False
        self.pages = [FakePage(self)]

    def new_page(self):
        page = FakePage(self)
        self.pages.append(page)
        return page

    def cookies(self, *a, **k):
        raise AssertionError("the runner read the browser's cookies")

    storage_state = add_cookies = cookies


class FakeBrowser:
    """The `browser` the Runner is given: a callable returning a context manager."""
    def __init__(self):
        self.contexts, self.launch_error = [], None

    @contextlib.contextmanager
    def __call__(self):
        if self.launch_error:
            raise self.launch_error
        ctx = FakeContext()
        self.contexts.append(ctx)
        try:
            yield ctx
        finally:
            ctx.closed = True


class FakePass:
    """Stands in for run_pass: records what it was given and returns scripted results."""
    def __init__(self, results=None):
        self.calls, self.results = [], list(results or [])
        self.before = self.during = None

    def __call__(self, kind, page, job, profile, params, emit, answers, resume_path, site_type):
        call = {"kind": kind, "page": page, "job": dict(job), "profile": dict(profile), "params": params,
                "answers": answers, "resume_path": resume_path, "site_type": site_type,
                "resume_bytes": Path(resume_path).read_bytes() if resume_path else None,
                "resume_name": Path(resume_path).name if resume_path else None}
        self.calls.append(call)
        if self.before:
            self.before(len(self.calls), page)
        emit("step", {"step": "opened_page", "message": "Opened the job page"})
        emit("site_type", "greenhouse")
        emit("log", "a log line that stays on this computer")
        if self.during:
            self.during(len(self.calls), page)
        item = self.results[min(len(self.calls), len(self.results)) - 1] if self.results else {"success": True}
        return item(call) if callable(item) else dict(item)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.server = FakeServer()
        self.addCleanup(self.server.close)
        self.browser, self.fake_pass, self.sleeps = FakeBrowser(), FakePass(), []
        self.logs = []
        handler = logging.Handler()
        handler.emit = lambda record: self.logs.append(record.getMessage() + (
            "\n" + logging.Formatter().formatException(record.exc_info) if record.exc_info else ""))
        root = logging.getLogger()
        self._level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(lambda: (root.removeHandler(handler), root.setLevel(self._level)))
        logging.getLogger("runner").propagate = True

    def backend(self):
        return R.HttpBackend(self.server.url, FakeServer.TOKEN)

    def runner(self, backend=None, **kw):
        kw.setdefault("sleep", self.sleeps.append)
        kw.setdefault("heartbeat", 30)
        kw.setdefault("run_pass_fn", self.fake_pass)
        self.r = R.Runner(backend or self.backend(), self.browser, **kw)
        return self.r

    def run_until_result(self, runner=None, results=1, timeout=90):
        """Run the runner (here, on the main thread) until `results` results have been posted."""
        runner = runner or self.r
        previous = self.server.on_result

        def hook(task_id, data):
            if previous:
                previous(task_id, data)
            if len(self.server.results) >= results:
                runner.stop()
        self.server.on_result = hook
        watchdog = self.after(timeout, runner.stop)
        try:
            return runner.run()
        finally:
            watchdog.cancel()

    def after(self, seconds, fn):
        """A timer that cannot outlive its test."""
        timer = threading.Timer(seconds, fn)
        timer.daemon = True
        timer.start()
        self.addCleanup(timer.cancel)
        return timer

    def png(self, name="shot.png"):
        path = self.dir / name
        path.write_bytes(make_png())
        return str(path)

    def queue(self, task):
        self.server.tasks.append(task)
        return task

    def token_in_logs(self):
        return [m for m in self.logs if FakeServer.TOKEN in m]


# ── claim -> run -> result ────────────────────────────────────────

class ClaimRunResultTests(Base):
    def test_a_task_is_claimed_run_and_its_result_posted(self):
        task = self.queue(make_task(resume=resume_info("a" * 32)))
        self.server.resume_bytes["a" * 32] = PDF
        shot1, shot2 = self.png("job-1_pre_submit_120101.png"), self.png("job-1_review_120102.png")
        field = FormField(label="Why Acme?", field_key="why_acme", selector="#q", field_type="textarea",
                          options=[], is_required=True, db_value="", needs_manual=True, placeholder="")
        self.fake_pass.results = [{"success": True, "error": "", "screenshots": [shot1, shot2], "missing_fields": [field],
                                   "review": {"fields": [{"label": "Email", "field_key": "email", "type": "email",
                                                          "value": "priya@example.com", "required": True, "page": 1,
                                                          "hit": {"label": "Email", "source": "profile", "action": "filled"}}],
                                              "url": "https://boards.greenhouse.io/acme/jobs/1/application"}}]
        self.assertEqual(self.run_until_result(self.runner()), R.EXIT_OK)

        # what the server was told
        self.assertEqual(len(self.server.results), 1)
        body = self.server.results[0]["body"]
        self.assertEqual((body["success"], body["error"]), (True, ""))
        self.assertEqual(body["missing_fields"][0]["label"], "Why Acme?")
        self.assertEqual(body["missing_fields"][0]["field_type"], "textarea")
        self.assertEqual(body["review"]["fields"][0]["hit"]["source"], "profile")
        self.assertEqual(body["review"]["url"], "https://boards.greenhouse.io/acme/jobs/1/application")
        self.assertEqual([s["png"] for s in self.server.shots], [make_png(), make_png()])
        self.assertEqual([s["label"] for s in self.server.shots], ["pre_submit_120101", "review_120102"])
        self.assertEqual(self.server.results[0]["shots_before"], 2, "screenshots go up BEFORE the result that names them")
        self.assertLess(max(i for i, x in enumerate(self.server.log) if x == "screenshot"), self.server.log.index("result"))
        codes = self.server.step_codes()
        self.assertEqual(codes[:2], ["runner_started", "opened_page"])
        self.assertIn(("site_type", "greenhouse"), [(e["event"], e["data"]) for e in self.server.events])
        self.assertEqual(sum(1 for e in self.server.events if e["event"] not in ("step", "site_type")), 0)
        self.assertNotIn("a log line", json.dumps(self.server.events), "log lines stay on this computer")

        # what the pass was given
        call = self.fake_pass.calls[0]
        self.assertEqual(call["kind"], "apply")
        self.assertEqual(call["job"], {"job_id": "job-1", "url": GH_URL, "title": "Backend Engineer", "company": "Acme", "source": ""})
        self.assertEqual((call["profile"], call["params"], call["site_type"]), (PROFILE, {}, "greenhouse"))
        self.assertEqual((call["resume_bytes"], call["resume_name"]), (PDF, "Priya Sharma Resume.pdf"))
        self.assertIsInstance(call["answers"], AnswerContext)

        # what was cleaned up
        self.assertFalse(Path(call["resume_path"]).exists(), "the decrypted resume is deleted")
        self.assertFalse(Path(call["resume_path"]).parent.exists())
        self.assertEqual(len(self.browser.contexts), 1)
        self.assertTrue(self.browser.contexts[0].closed, "the browser window is closed after the pass")
        self.assertEqual(self.token_in_logs(), [])

    def test_the_bearer_token_is_on_every_request(self):
        self.queue(make_task())
        self.run_until_result(self.runner())
        self.assertGreaterEqual(len(self.server.requests), 4)
        for req in self.server.requests:
            self.assertEqual(req["headers"].get("Authorization"), f"Bearer {FakeServer.TOKEN}", req["path"])

    def test_the_claim_is_a_long_poll_for_the_longest_the_server_allows(self):
        self.queue(make_task())
        self.run_until_result(self.runner())
        self.assertEqual(json.loads(self.server.endpoint_hits("claim")[0]["body"]), {"wait": R.CLAIM_WAIT})

    def test_tasks_run_one_after_another(self):
        self.queue(make_task(task_id="1" * 32, job_id="j1"))
        self.queue(make_task(task_id="2" * 32, job_id="j2"))
        self.run_until_result(self.runner(), results=2)
        self.assertEqual([c["job"]["job_id"] for c in self.fake_pass.calls], ["j1", "j2"])
        self.assertEqual([r["task"] for r in self.server.results], ["1" * 32, "2" * 32])
        self.assertEqual(len(self.browser.contexts), 2)
        self.assertTrue(all(c.closed for c in self.browser.contexts))

    def test_refill_and_submit_pass_their_parameters(self):
        self.queue(make_task("refill", task_id="1" * 32, params={"missing_keys": ["why_acme"]}))
        self.queue(make_task("submit", task_id="2" * 32, params={"corrections": ["phone is +91 1"], "dry_run": True}))
        self.run_until_result(self.runner(), results=2)
        self.assertEqual([(c["kind"], c["params"]) for c in self.fake_pass.calls],
                         [("refill", {"missing_keys": ["why_acme"]}), ("submit", {"corrections": ["phone is +91 1"], "dry_run": True})])

    def test_a_reported_dry_run_is_passed_on(self):
        self.queue(make_task("submit", params={"dry_run": True}))
        self.fake_pass.results = [{"success": True, "error": "", "screenshots": [], "dry_run": True}]
        self.run_until_result(self.runner())
        self.assertIs(self.server.results[0]["body"]["dry_run"], True)

    def test_a_failure_is_reported_with_its_error(self):
        self.queue(make_task())
        self.fake_pass.results = [{"success": False, "error": "Navigation failed: net::ERR_NAME_NOT_RESOLVED", "screenshots": []}]
        self.run_until_result(self.runner())
        body = self.server.results[0]["body"]
        self.assertEqual((body["success"], body["error"]), (False, "Navigation failed: net::ERR_NAME_NOT_RESOLVED"))

    def test_a_pass_that_raises_is_a_failure_with_its_error_type_so_a_timeout_stays_one(self):
        self.queue(make_task())
        def boom(*a, **k):
            raise TimeoutError("Page.goto: Timeout 30000ms exceeded.")
        self.run_until_result(self.runner(run_pass_fn=boom))
        body = self.server.results[0]["body"]
        self.assertFalse(body["success"])
        self.assertTrue(body["error"].startswith("TimeoutError:"))
        self.assertEqual(failures.classify(body["error"]), "timeout")
        self.assertTrue(self.browser.contexts[0].closed)

    def test_the_resume_must_arrive_intact(self):
        self.queue(make_task(resume=resume_info("a" * 32)))
        self.server.resume_bytes["a" * 32] = PDF + b"corrupted"
        self.run_until_result(self.runner())
        body = self.server.results[0]["body"]
        self.assertFalse(body["success"])
        self.assertIn("did not download intact", body["error"])
        self.assertEqual((self.fake_pass.calls, self.browser.contexts), ([], []), "no browser, no pass, no wrong resume uploaded")

    def test_a_resume_file_name_cannot_escape_its_folder(self):
        info = resume_info("a" * 32, filename="../../etc/passwd\\..\\x.pdf")
        self.queue(make_task(resume=info))
        self.server.resume_bytes["a" * 32] = PDF
        self.run_until_result(self.runner())
        call = self.fake_pass.calls[0]
        self.assertEqual(call["resume_bytes"], PDF)
        self.assertEqual(Path(call["resume_name"]).name, call["resume_name"], "a bare file name, no folders")
        self.assertNotIn("\\", call["resume_name"])

    def test_no_resume_means_no_download_and_no_resume_path(self):
        self.queue(make_task(resume=None))
        self.run_until_result(self.runner())
        self.assertEqual(self.server.endpoint_hits("resume"), [])
        self.assertIsNone(self.fake_pass.calls[0]["resume_path"])

    def test_a_browser_that_will_not_open_is_reported_once_and_stops_the_runner(self):
        self.queue(make_task(task_id="1" * 32))
        self.queue(make_task(task_id="2" * 32))
        self.browser.launch_error = RuntimeError("profile is locked by another browser")
        code = self.run_until_result(self.runner(), results=5)
        self.assertEqual(code, R.EXIT_BROWSER)
        self.assertEqual(len(self.server.results), 1, "one task failed, then the runner stopped instead of draining the queue")
        self.assertIn("Browser launch failed: profile is locked", self.server.results[0]["body"]["error"])
        self.assertEqual(len(self.server.tasks), 1, "the second task was never claimed")
        self.assertTrue(any("profile" in m and "Close any other window" in m for m in self.logs))

    def test_a_task_with_no_usable_link_fails_without_opening_a_browser(self):
        self.queue(make_task(url=""))
        self.run_until_result(self.runner())
        self.assertIn("no usable link", self.server.results[0]["body"]["error"])
        self.assertEqual((self.browser.contexts, self.fake_pass.calls), ([], []))

    def test_a_task_in_a_shape_the_runner_does_not_know_is_ignored_and_the_next_one_runs(self):
        bad = make_task(task_id="1" * 32)
        del bad["kind"]
        self.queue(bad)
        self.queue(make_task(task_id="2" * 32, job_id="j2"))
        self.run_until_result(self.runner())
        self.assertEqual([c["job"]["job_id"] for c in self.fake_pass.calls], ["j2"])
        self.assertEqual([r["task"] for r in self.server.results], ["2" * 32])

    def test_a_naukri_task_tells_the_form_code_the_source_so_the_first_pass_never_clicks_apply(self):
        seen = {}
        def fake_universal(page, job, profile, **kw):
            seen["job"] = dict(job)
            return {"success": True, "screenshots": [], "missing_fields": [], "error": "", "needs_review": True,
                    "review": {"fields": [], "url": ""}}
        job = {"job_id": "j", "url": "https://www.naukri.com/job-listings-x-1", "title": "T", "company": "C", "source": "naukri"}
        with mock.patch.object(universal, "run_application", fake_universal):
            R.run_pass("apply", object(), job, {}, {}, lambda *a: None, None, None, "naukri_one_click")
        self.assertEqual(seen["job"]["source"], "naukri")
        # ...and the runner derives it from the site type, because the task does not carry the job's source
        self.queue(make_task(site_type="naukri_one_click", url="https://www.naukri.com/job-listings-x-1"))
        self.run_until_result(self.runner())
        self.assertEqual(self.fake_pass.calls[0]["job"]["source"], "naukri")
        self.queue(make_task(task_id="2" * 32, site_type="greenhouse"))
        self.server.results.clear()
        self.run_until_result(self.runner())
        self.assertEqual(self.fake_pass.calls[1]["job"]["source"], "")


# ── the passes (the project's own form code, chosen as the Orchestrator chooses) ──

class PassDispatchTests(unittest.TestCase):
    JOB = {"job_id": "j1", "url": GH_URL, "title": "T", "company": "Acme", "source": ""}
    OK = {"success": True, "screenshots": [], "missing_fields": [], "error": "", "needs_review": True,
          "review": {"fields": [], "url": ""}}

    def run_kind(self, kind, site_type="greenhouse", params=None, resume="/tmp/r.pdf"):
        calls = []
        def adapter_pass(name):
            def f(adapter, page, job, profile, *args, **kw):
                calls.append(("adapter", name, adapter.name, args, kw))
                return dict(self.OK)
            return f
        def universal_pass(name):
            def f(page, job, profile, *args, **kw):
                calls.append(("universal", name, None, args, kw))
                return dict(self.OK)
            return f
        names = ("run_application", "run_iteration2", "run_submit")
        with contextlib.ExitStack() as stack:
            for n in names:
                stack.enter_context(mock.patch.object(adapter_passes, n, adapter_pass(n)))
                stack.enter_context(mock.patch.object(universal, n, universal_pass(n)))
            result = R.run_pass(kind, "page", dict(self.JOB), {"email": "e"}, params or {}, "EMIT", "ANSWERS", resume, site_type)
        return result, calls

    def test_the_site_adapter_runs_the_pass_for_its_site(self):
        for kind, name in (("apply", "run_application"), ("refill", "run_iteration2"), ("submit", "run_submit")):
            _, calls = self.run_kind(kind, params={"missing_keys": ["k"], "corrections": ["c"], "dry_run": True})
            self.assertEqual([c[:3] for c in calls], [("adapter", name, "greenhouse")], kind)

    def test_each_kind_gets_its_own_arguments_and_always_the_emit_answers_and_resume(self):
        _, (apply_call,) = self.run_kind("apply")
        self.assertEqual((apply_call[3], apply_call[4]), ((), {"emit": "EMIT", "answers": "ANSWERS", "resume_path": "/tmp/r.pdf"}))
        _, (refill,) = self.run_kind("refill", params={"missing_keys": ["why", "age"]})
        self.assertEqual(refill[3], (["why", "age"],))
        _, (submit,) = self.run_kind("submit", params={"corrections": ["c1"], "dry_run": True})
        self.assertEqual((submit[3], submit[4]["corrections"], submit[4]["dry_run"]), ((), ["c1"], True))
        _, (submit,) = self.run_kind("submit", params={})
        self.assertEqual((submit[4]["corrections"], submit[4]["dry_run"]), (None, False))

    def test_no_resume_leaves_the_form_code_to_its_own_default(self):
        _, (call,) = self.run_kind("apply", resume=None)
        self.assertNotIn("resume_path", call[4])

    def test_a_site_without_an_adapter_gets_the_universal_filler(self):
        _, calls = self.run_kind("apply", site_type="generic")
        self.assertEqual([c[:3] for c in calls], [("universal", "run_application", None)])

    def test_an_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            R.run_pass("delete", "page", dict(self.JOB), {}, {}, print, None, None, "generic")

    def test_an_adapter_that_cannot_handle_the_form_falls_back_to_the_universal_filler_once_and_says_so(self):
        emitted, calls = [], []
        def adapter_fails(adapter, *a, **k):
            calls.append("adapter")
            raise UnsupportedForm("greenhouse", "no form on the page")
        def universal_fails(page, job, profile, **kw):
            calls.append("universal")
            return {"success": False, "error": "No Apply button found", "screenshots": []}
        with mock.patch.object(adapter_passes, "run_application", adapter_fails), \
                mock.patch.object(universal, "run_application", universal_fails):
            result = R.run_pass("apply", "page", dict(self.JOB), {}, {}, lambda e, d: emitted.append((e, d)), None, None, "greenhouse")
        self.assertEqual(calls, ["adapter", "universal"])
        self.assertEqual([d["step"] for e, d in emitted if e == "step"], ["adapter_fallback"])
        self.assertIn("after the greenhouse adapter reported: Unsupported form (greenhouse): no form on the page", result["error"])

    def test_the_fallback_step_is_one_the_server_accepts(self):
        emitted = []
        with mock.patch.object(adapter_passes, "run_application", side_effect=UnsupportedForm("greenhouse", "x" * 500)), \
                mock.patch.object(universal, "run_application", return_value=dict(self.OK)):
            R.run_pass("apply", "page", dict(self.JOB), {}, {}, lambda e, d: emitted.append(R.to_wire(e, d)), None, None, "greenhouse")
        self.assertIsNotNone(emitted[0])
        clean_event(emitted[0])


# ── what goes over the wire ───────────────────────────────────────

class WireTests(unittest.TestCase):
    def test_a_log_line_stays_home_and_a_step_goes(self):
        self.assertIsNone(R.to_wire("log", "Opening application: https://x"))
        self.assertIsNone(R.to_wire("awaiting_input", {"missing_fields": []}))
        self.assertEqual(R.to_wire("step", {"step": "opened_page", "message": "Opened"}),
                         {"event": "step", "data": {"step": "opened_page", "message": "Opened"}})
        self.assertEqual(R.to_wire("site_type", "lever"), {"event": "site_type", "data": "lever"})

    def test_what_the_server_would_refuse_is_never_sent(self):
        self.assertIsNone(R.to_wire("site_type", "not_a_site"))
        self.assertIsNone(R.to_wire("step", {"step": "Bad Code", "message": "x"}))
        self.assertIsNone(R.to_wire("step", {"step": "ok_code", "message": "   "}))
        self.assertIsNone(R.to_wire("step", {"step": "ok_code"}))
        self.assertIsNone(R.to_wire("step", "text"))
        wire = R.to_wire("step", {"step": "form_page", "message": "m" * 1000, "page": 2, "total": None, "BadKey": 1,
                                  "nested": {"a": 1}, "long": "x" * 300, "ok_text": "fine",
                                  **{f"k{i}": i for i in range(30)}})
        data = wire["data"]
        self.assertEqual(len(data["message"]), 300)
        self.assertEqual((data["page"], data["total"], data["ok_text"]), (2, None, "fine"))
        for dropped in ("BadKey", "nested", "long"):
            self.assertNotIn(dropped, data)
        self.assertLessEqual(len(data) - 2, 10)
        clean_event(wire)

    def test_a_result_is_always_one_the_real_server_accepts_however_odd_the_pass_made_it(self):
        weird = FormField(label="L" * 900, field_key="", selector="", field_type="", options=[7, None, "o" * 900] * 60,
                          is_required=True, db_value="", needs_manual=True, placeholder="p" * 900)
        result = {"success": False, "error": "e" * 5000, "missing_fields": [weird] * 80 + [FormField("", "k", "", "", [], False, "", True, "")],
                  "review": {"fields": [{"label": "Q", "value": "v" * 9000, "options": ["a"] * 500, "hit": {"source": "bank"}}] * 400,
                             "url": "u" * 5000}, "dry_run": 1}
        body = R.result_body(result)
        clean = clean_result(body, {"params": {}}, [])
        self.assertEqual(len(body["missing_fields"]), 50)
        self.assertEqual(len(body["review"]["fields"]), 300)
        self.assertTrue(body["missing_fields"][0]["field_key"])
        self.assertIs(body["dry_run"], True)
        self.assertFalse(clean["success"])

    def test_a_field_with_no_label_is_not_reported_and_nothing_is_invented_for_dry_run(self):
        body = R.result_body({"success": True, "missing_fields": [FormField("", "k", "", "text", [], False, "", True, "")]})
        self.assertEqual(body["missing_fields"], [])
        self.assertNotIn("dry_run", body)
        self.assertEqual(body["review"], {"fields": [], "url": ""})

    def test_a_missing_field_key_is_made_from_its_label(self):
        f = FormField("Notice period?", "", "", "radio", ["Yes", "No"], True, "", True, "")
        self.assertEqual(R.result_body({"success": True, "missing_fields": [f]})["missing_fields"][0]["field_key"], "notice_period")


# ── the saved answers the task carries ────────────────────────────

class PayloadBankTests(Base):
    ENTRIES = [
        {"id": 1, "question": "Why do you want to work at Acme?", "answer": "Acme robots", "scope": "company", "company": "Acme Robotics", "policy": "always"},
        {"id": 2, "question": "Why do you want to work at Acme?", "answer": "Generic", "scope": "global", "company": "", "policy": "always"},
        {"id": 3, "question": "Are you willing to relocate?", "answer": "Yes", "scope": "global", "company": "", "policy": "ask"},
        {"id": 4, "question": "Years of experience with Python", "answer": "6", "scope": "global", "company": "", "policy": "once"},
        {"id": 5, "question": "Do you have a driving licence", "answer": "Yes", "scope": "company", "company": "Other Corp", "policy": "always"},
    ]
    QUESTIONS = ["Why do you want to work at Acme?", "Why do you want to work at Acme", "willing to relocate",
                 "Are you willing to relocate??", "Years of experience with Python", "Years of experience with Java",
                 "Do you have a driving licence", "Notice period", "Expected CTC (LPA)", "Something else entirely", ""]
    FLAT = {"notice_period_label": "30 days", "expected_ctc_lpa": "24", "willing_to_relocate": "No"}

    def real_bank(self, company="Acme Robotics"):
        bank = AnswerBank(1, self.dir / "bank.db")
        # same entries, most recent first as the server sends them; ids differ so compare by answer
        for e in reversed(self.ENTRIES):
            row = bank.add(e["question"], e["answer"], scope=e["scope"], company=e["company"])
            if e["policy"] != "always":
                bank.update(row["id"], policy=e["policy"])
        return bank

    def test_it_matches_exactly_as_the_servers_own_bank_does(self):
        real, mine = self.real_bank(), R.PayloadBank(self.ENTRIES)
        for company in ("Acme Robotics", "Acme Robotics Pvt. Ltd.", "Other Corp", ""):
            for q in self.QUESTIONS:
                for profile in (None, self.FLAT):
                    want, got = real.resolve(q, company, profile), mine.resolve(q, company, profile)
                    self.assertEqual(want and (want["answer"], want["confidence"], want["source"], want["policy"], want["scope"]),
                                     got and (got["answer"], got["confidence"], got["source"], got["policy"], got["scope"]),
                                     (q, company, bool(profile)))

    def test_a_company_answer_beats_a_global_one_and_another_companys_is_invisible(self):
        bank = R.PayloadBank(self.ENTRIES)
        self.assertEqual(bank.resolve("Why do you want to work at Acme?", "Acme Robotics")["answer"], "Acme robots")
        self.assertEqual(bank.resolve("Why do you want to work at Acme?", "Zeta")["answer"], "Generic")
        self.assertIsNone(bank.resolve("Do you have a driving licence", "Acme Robotics"))
        self.assertEqual(bank.resolve("Do you have a driving licence", "Other Corp Ltd")["entry_id"], 5)

    def test_what_the_task_carries_fills_the_form_and_this_applications_own_answers_win(self):
        task = make_task(bank=self.ENTRIES, application_answers={"willing relocate": "Maybe"},
                         profile={**PROFILE, "notice_period_label": "30 days"})
        task["application"]["company"] = "Acme Robotics"
        self.queue(task)
        self.run_until_result(self.runner())
        answers = self.fake_pass.calls[0]["answers"]
        fields = [FormField("Why do you want to work at Acme?", "why", "#a", "textarea", [], True, "", True, ""),
                  FormField("Are you willing to relocate?", "reloc", "#b", "text", [], True, "", True, ""),
                  FormField("Years of experience with Python", "py", "#c", "text", [], False, "", False, ""),
                  FormField("Notice period", "notice", "#d", "text", [], False, "", False, "")]
        hits = {h["field_key"]: h for h in answers.apply(FormAnalysis(page_url="u", job_id="j", fields=fields))}
        self.assertEqual((fields[0].db_value, fields[0].needs_manual), ("Acme robots", False))
        self.assertEqual((fields[1].db_value, hits["reloc"]["source"]), ("Maybe", "application"), "this application's answer outranks the bank")
        self.assertEqual((fields[2].db_value, hits["py"]["action"]), ("6", "once"))
        self.assertEqual((fields[3].db_value, hits["notice"]["source"]), ("30 days", "profile"), "profile facts come from the task's profile")

    def test_a_once_only_answer_is_used_up_for_this_pass_without_touching_anything_else(self):
        bank = R.PayloadBank(self.ENTRIES)
        self.assertTrue(bank.delete(4))
        self.assertIsNone(bank.resolve("Years of experience with Python", "Acme"))
        self.assertFalse(bank.delete(4))
        self.assertIsNone(bank.touch(1))
        self.assertEqual(len(R.PayloadBank(self.ENTRIES)._entries), 5, "a new task starts from what the server sent")

    def test_junk_entries_are_skipped_not_fatal(self):
        bank = R.PayloadBank([None, "x", {}, {"question": "", "answer": "a"}, {"question": "Q", "answer": 5},
                              {"question": "Notice period", "answer": "  "}, {"id": 9, "question": "Gender", "answer": "n/a"}])
        self.assertEqual([e["id"] for e in bank._entries], [9])
        self.assertEqual(bank._entries[0]["policy"], "always")


# ── waiting for you: CAPTCHA and login ────────────────────────────

class PauseForHumanTests(Base):
    CAPTCHA = {"success": False, "error": "CAPTCHA detected — skipping", "screenshots": []}
    OK = {"success": True, "error": "", "screenshots": [], "missing_fields": []}

    def setUp(self):
        super().setUp()
        self.queue(make_task())
        self.polls = 0

    def human_clears_after(self, polls, then=None):
        """The `sleep` of the pause loop: you solve the CAPTCHA on the Nth look."""
        def sleep(seconds):
            self.polls += 1
            if self.polls >= polls:
                self.browser.contexts[0].captcha = False
                if then:
                    then()
        return sleep

    def captcha_on_first_run(self, count, page):
        if count == 1:
            page.ctx.captcha = True

    def test_a_captcha_parks_the_task_visibly_and_runs_the_pass_again_when_you_clear_it(self):
        self.fake_pass.before = self.captcha_on_first_run
        self.fake_pass.results = [self.CAPTCHA, self.OK]
        self.assertEqual(self.run_until_result(self.runner(sleep=self.human_clears_after(3), human_poll=2)), R.EXIT_OK)
        self.assertEqual(len(self.fake_pass.calls), 2, "the pass ran again once the page was clear")
        self.assertEqual(len(self.browser.contexts), 1, "in the same window")
        codes = self.server.step_codes()
        self.assertLess(codes.index("runner_waiting_for_you"), codes.index("runner_resumed"))
        waiting = next(e["data"] for e in self.server.events if e["event"] == "step" and e["data"]["step"] == "runner_waiting_for_you")
        self.assertIn("CAPTCHA", waiting["message"])
        self.assertEqual(waiting["reason"], "captcha")
        self.assertEqual([r["body"]["success"] for r in self.server.results], [True], "only the final outcome is reported")
        self.assertGreaterEqual(self.browser.contexts[0].pages[0].fronted, 1, "the window was brought forward")
        self.assertTrue(any("PAUSED" in m and "solve the CAPTCHA" in m for m in self.logs), "and you are told here too")
        self.assertEqual(self.polls, 3)

    def test_the_task_stays_alive_while_you_take_your_time(self):
        self.fake_pass.before = self.captcha_on_first_run
        self.fake_pass.results = [self.CAPTCHA, self.OK]
        def slow_human(seconds):
            self.polls += 1
            if not wait_for(lambda: self.server.beats >= 3, timeout=5):
                self.fail("the heartbeat did not keep the lease alive while paused")
            self.browser.contexts[0].captcha = False
        self.run_until_result(self.runner(sleep=slow_human, heartbeat=0.02))
        self.assertGreaterEqual(self.server.beats, 3)
        self.assertEqual(len(self.server.results), 1)
        self.assertEqual(self.token_in_logs(), [])

    def test_a_login_wall_is_waited_for_the_same_way(self):
        def wall(count, page):
            if count == 1:
                page.url = "https://careers.example.com/403"
        self.fake_pass.before = wall
        self.fake_pass.results = [{"success": False, "error": "Login wall — page requires authentication and no login was detected", "screenshots": []}, self.OK]
        def you_log_in(seconds):
            self.polls += 1
            self.browser.contexts[0].pages[0].url = "https://careers.example.com/job/1"
        self.run_until_result(self.runner(sleep=you_log_in))
        self.assertEqual(len(self.fake_pass.calls), 2)
        waiting = next(e["data"] for e in self.server.events if e["event"] == "step" and e["data"]["step"] == "runner_waiting_for_you")
        self.assertIn("log in", waiting["message"])

    def test_it_gives_up_when_you_do_not_come_back_and_reports_the_original_failure(self):
        self.fake_pass.before = self.captcha_on_first_run
        self.fake_pass.results = [self.CAPTCHA]
        self.run_until_result(self.runner(sleep=self.sleeps.append, human_wait=6, human_poll=2))
        self.assertEqual(self.sleeps.count(2), 3, "it looked every 2 s for 6 s")
        self.assertEqual(len(self.fake_pass.calls), 1)
        body = self.server.results[0]["body"]
        self.assertEqual((body["success"], failures.classify(body["error"])), (False, "captcha"))
        self.assertIn("runner_gave_up_waiting", self.server.step_codes())
        self.assertNotIn("runner_resumed", self.server.step_codes())

    def test_it_pauses_at_most_three_times_for_a_page_that_keeps_asking(self):
        self.fake_pass.before = lambda count, page: setattr(page.ctx, "captcha", True)       # every run meets one
        self.fake_pass.results = [self.CAPTCHA]
        def you_solve_each(seconds):
            self.polls += 1
            self.browser.contexts[0].captcha = False
        self.run_until_result(self.runner(sleep=you_solve_each))
        self.assertEqual(len(self.fake_pass.calls), 1 + R.MAX_PAUSES)
        self.assertFalse(self.server.results[0]["body"]["success"])

    def test_a_failure_that_is_not_a_challenge_is_not_waited_for(self):
        self.fake_pass.results = [{"success": False, "error": "Navigation failed: timeout", "screenshots": []}]
        self.run_until_result(self.runner())
        self.assertEqual((len(self.fake_pass.calls), self.sleeps), (1, []))
        self.assertNotIn("runner_waiting_for_you", self.server.step_codes())

    def test_a_captcha_message_with_nothing_showing_is_not_retried_so_a_submit_is_never_clicked_twice(self):
        """The site's own inline error mentions a captcha AFTER Submit was clicked; no challenge is on
        the page. Running the pass again would click Submit again."""
        self.server.tasks.clear()
        self.queue(make_task("submit"))
        self.fake_pass.results = [{"success": False, "screenshots": [],
                                   "error": "Validation error — the site rejected the answers: Please complete the captcha"}]
        self.run_until_result(self.runner())
        self.assertEqual(len(self.fake_pass.calls), 1)
        self.assertEqual(self.sleeps, [])
        self.assertFalse(self.server.results[0]["body"]["success"])

    def test_a_task_taken_back_while_you_solve_it_is_dropped_and_the_next_one_runs(self):
        self.fake_pass.before = self.captcha_on_first_run
        self.fake_pass.results = [self.CAPTCHA, self.OK]
        self.queue(make_task(task_id="b" * 32, job_id="j2"))
        def server_takes_it_back(seconds):
            self.server.gone.add("a" * 32)                      # the lease lapsed; it went back to the queue
            time.sleep(0.3)                                     # the heartbeat finds out
        self.run_until_result(self.runner(sleep=server_takes_it_back, heartbeat=0.02))
        self.assertEqual([c["job"]["job_id"] for c in self.fake_pass.calls], ["job-1", "j2"], "task a was not run again")
        self.assertEqual([r["task"] for r in self.server.results], ["b" * 32], "no result for a task that is no longer ours")
        self.assertNotIn("runner_resumed", self.server.step_codes())
        self.assertTrue(any("no longer this runner's" in m for m in self.logs))


# ── the server goes away and comes back ───────────────────────────

class ReconnectTests(Base):
    def test_server_errors_and_dropped_connections_are_waited_out_with_a_growing_delay(self):
        self.server.script["claim"] = [500, "drop", 503, None]
        self.queue(make_task())
        self.run_until_result(self.runner())
        self.assertEqual(self.sleeps, [2, 5, 10])
        self.assertEqual(len(self.server.endpoint_hits("claim")), 4)
        self.assertEqual(len(self.server.results), 1, "and the task still ran")
        self.assertTrue(any("trying again in 5 s" in m for m in self.logs))

    def test_the_delay_levels_off_and_starts_over_after_the_server_answers(self):
        self.server.script["claim"] = [500] * 7 + [None, 500, None]
        self.server.claim_wait = 0.01
        runner = self.runner()
        self.after(0.05, lambda: self.queue(make_task()))
        self.run_until_result(runner)
        delays = [d for d in self.sleeps if d != R.MIN_POLL_SECONDS]
        self.assertEqual(delays[:7], [2, 5, 10, 30, 60, 60, 60])
        self.assertEqual(delays[7], 2, "after one good answer the wait starts again from the shortest")

    def test_nothing_is_reachable_at_all(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()                                              # nobody is listening there
        backend = R.HttpBackend(f"http://127.0.0.1:{port}", FakeServer.TOKEN)
        with self.assertRaises(R.Unavailable):
            backend.claim(0)
        with self.assertRaises(R.Unavailable):
            backend.events("t", [])
        stops = []
        def sleep(seconds):
            stops.append(seconds)
            if len(stops) == 3:
                runner.stop()
        runner = R.Runner(backend, self.browser, sleep=sleep)
        self.assertEqual(runner.run(), R.EXIT_OK)
        self.assertEqual(stops, [2, 5, 10])

    def test_a_revoked_token_stops_the_runner_and_never_logs_it(self):
        self.server.script["claim"] = []
        bad = R.HttpBackend(self.server.url, "apxr_" + "X" * 43)
        runner = self.runner(bad)
        self.assertEqual(runner.run(), R.EXIT_AUTH)
        self.assertEqual(len(self.server.endpoint_hits("claim")), 1, "it did not keep knocking")
        self.assertEqual(self.sleeps, [])
        self.assertTrue(any("token" in m and "revoked" in m for m in self.logs))
        self.assertEqual([m for m in self.logs if "X" * 20 in m], [])

    def test_a_token_revoked_in_the_middle_of_a_task_stops_the_runner_too(self):
        self.queue(make_task())
        self.server.script["result"] = [401]
        self.assertEqual(self.run_until_result(self.runner()), R.EXIT_AUTH)

    def test_a_429_on_the_claim_means_another_poll_is_still_open_so_wait_and_retry(self):
        self.server.script["claim"] = [429, None]
        self.queue(make_task())
        self.run_until_result(self.runner())
        self.assertEqual(self.sleeps, [2])
        self.assertEqual(len(self.server.results), 1)

    def test_progress_the_server_could_not_take_is_kept_and_sent_in_order_later(self):
        self.server.script["events"] = [503]                   # the very first event (runner_started) fails
        self.queue(make_task())
        self.run_until_result(self.runner())
        codes = self.server.step_codes()
        self.assertEqual(codes[:2], ["runner_started", "opened_page"], "nothing lost, order kept")
        self.assertEqual(len(self.server.results), 1)

    def test_a_result_the_server_cannot_take_is_retried_until_it_can(self):
        self.server.script["result"] = [503, "drop", None]
        self.queue(make_task())
        self.run_until_result(self.runner())
        self.assertEqual(len(self.server.results), 1)
        self.assertEqual(self.sleeps, [2, 5])
        self.assertEqual(len(self.server.endpoint_hits("result")), 3)

    def test_a_screenshot_upload_is_retried_too(self):
        self.server.script["screenshot"] = [502, None]
        self.queue(make_task())
        self.fake_pass.results = [{"success": True, "screenshots": [self.png()], "missing_fields": []}]
        self.run_until_result(self.runner())
        self.assertEqual(len(self.server.shots), 1)
        self.assertEqual(self.sleeps, [2])

    def test_a_server_that_never_comes_back_is_given_up_on_and_the_runner_goes_on_to_the_next_task(self):
        self.server.script["result"] = [503] * 50
        self.queue(make_task(task_id="1" * 32, job_id="j1"))
        self.queue(make_task(task_id="2" * 32, job_id="j2"))
        runner = self.runner(send_retry=20)
        done = []
        original = runner._deliver
        def deliver(task_id, result):
            try:
                original(task_id, result)
            finally:
                done.append(task_id)
                if len(done) == 2:
                    runner.stop()
        runner._deliver = deliver
        self.after(20, runner.stop)
        runner.run()
        self.assertEqual([c["job"]["job_id"] for c in self.fake_pass.calls], ["j1", "j2"])
        self.assertTrue(any("Giving up on task" in m for m in self.logs))
        self.assertEqual(self.sleeps.count(2), 2)

    def test_a_task_the_server_took_back_is_dropped_without_a_result(self):
        self.queue(make_task(task_id="1" * 32, job_id="j1"))
        self.queue(make_task(task_id="2" * 32, job_id="j2"))
        self.fake_pass.during = lambda count, page: self.server.gone.add("1" * 32) if count == 1 else None
        self.fake_pass.results = [{"success": True, "screenshots": [self.png()], "missing_fields": []}]
        self.run_until_result(self.runner())
        self.assertEqual([r["task"] for r in self.server.results], ["2" * 32], "no result and no screenshot for the task that moved on")
        self.assertEqual([s["task"] for s in self.server.shots], ["2" * 32])
        self.assertTrue(any("no longer this runner's" in m for m in self.logs))

    def test_a_task_taken_back_before_its_first_event_stops_all_further_reporting(self):
        """Told no by the very first event (the heartbeat is far away), the runner stops there:
        no screenshot and no result go to a server that has given the task to someone else."""
        self.queue(make_task())
        self.fake_pass.before = lambda count, page: self.server.gone.add("a" * 32)
        self.fake_pass.results = [{"success": True, "screenshots": [self.png()], "missing_fields": []}]
        runner = self.runner(heartbeat=30)
        self.after(1.0, runner.stop)
        runner.run()
        self.assertEqual(len(self.fake_pass.calls), 1)
        self.assertEqual(self.server.endpoint_hits("screenshot"), [])
        self.assertEqual(self.server.endpoint_hits("result"), [])
        self.assertTrue(any("no longer this runner's" in m for m in self.logs))

    def test_a_result_the_server_refuses_is_not_sent_again(self):
        self.server.script["result"] = [400]
        self.queue(make_task())
        runner = self.runner()
        self.after(1.0, runner.stop)
        runner.run()
        self.assertEqual(len(self.server.endpoint_hits("result")), 1)
        self.assertEqual(self.sleeps.count(2), 0)
        self.assertTrue(any("refused the result" in m for m in self.logs))

    def test_a_screenshot_the_server_would_refuse_is_skipped_with_a_note_not_sent(self):
        bad = self.dir / "bad.png"
        bad.write_bytes(b"not a png at all")
        self.queue(make_task())
        self.fake_pass.results = [{"success": True, "screenshots": [str(bad), str(self.dir / "missing.png"), self.png()], "missing_fields": []}]
        self.run_until_result(self.runner())
        self.assertEqual(len(self.server.shots), 1, "only the good one")
        self.assertEqual(len(self.server.endpoint_hits("screenshot")), 1, "the bad ones were never sent")
        self.assertEqual(self.server.step_codes().count("runner_screenshot_skipped"), 2)
        self.assertTrue(self.server.results[0]["body"]["success"])

    def test_the_heartbeat_keeps_a_busy_pass_alive_and_stops_with_the_task(self):
        self.queue(make_task())
        self.fake_pass.during = lambda count, page: wait_for(lambda: self.server.beats >= 3, timeout=5)
        self.run_until_result(self.runner(heartbeat=0.02))
        self.assertGreaterEqual(self.server.beats, 3)
        beats_at_end = self.server.beats
        time.sleep(0.15)
        self.assertEqual(self.server.beats, beats_at_end, "no heartbeat after the task is over")
        self.assertEqual([t.name for t in threading.enumerate() if t.name == "runner-heartbeat"], [])

    def test_an_idle_claim_that_comes_back_at_once_is_not_hammered(self):
        stops = []
        def sleep(seconds):
            stops.append(seconds)
            runner.stop()
        runner = R.Runner(self.backend(), self.browser, sleep=sleep)
        self.server.claim_wait = 0
        runner.run()
        self.assertEqual(stops, [R.MIN_POLL_SECONDS])


# ── Ctrl+C ────────────────────────────────────────────────────────

class _InterruptedBackend(R.Backend):
    def claim(self, wait): raise KeyboardInterrupt
    def events(self, *a): pass
    def screenshot(self, *a): pass
    def result(self, *a): return {}
    def resume(self, *a): return b""


class CtrlCTests(Base):
    def setUp(self):
        super().setUp()
        self._handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, signal.default_int_handler)               # whatever `unittest -c` installed
        self.addCleanup(lambda: signal.signal(signal.SIGINT, self._handler))

    def test_ctrl_c_while_waiting_for_work_exits_cleanly(self):
        self.assertEqual(R.Runner(_InterruptedBackend(), self.browser).run(), R.EXIT_OK)
        self.assertTrue(any("Stopped" in m for m in self.logs))

    def interrupt_pass(self, count, page):
        raise KeyboardInterrupt

    def test_ctrl_c_is_noticed_within_a_fraction_of_a_second_even_while_a_read_blocks(self):
        """On Windows a blocking socket read ignores Ctrl+C until it returns. The claim is waited for
        in short slices, so a Ctrl+C (simulated the same way on every platform) acts at once."""
        if threading.current_thread() is not threading.main_thread():
            self.skipTest("the interrupt is delivered to the main thread")
        import _thread
        release = threading.Event()
        self.addCleanup(release.set)

        class Blocked:
            auth = None
            cookies = type("C", (), {"set_policy": lambda self, p: None})()

            def request(self, *a, **k):
                release.wait(10)
                raise RuntimeError("never answered")
        backend = R.HttpBackend(self.server.url, FakeServer.TOKEN, session=Blocked())
        self.after(0.3, _thread.interrupt_main)
        self.after(5.0, release.set)                       # only so a broken wait ends the test instead of hanging it
        started = time.time()
        with self.assertRaises(KeyboardInterrupt):
            backend.claim(25)
        self.assertLess(time.time() - started, 2.0, "it waited for the read instead of for Ctrl+C")

    def test_a_claim_hands_back_what_the_helper_thread_got_and_what_it_raised(self):
        self.server.tasks.append(make_task())
        self.assertEqual(self.backend().claim(1)["kind"], "apply")
        self.server.script["claim"] = [503]
        with self.assertRaises(R.Unavailable):
            self.backend().claim(1)
        self.assertEqual([t.name for t in threading.enumerate() if t.name == "runner-claim"], [])

    def test_ctrl_c_in_the_middle_of_a_pass_cleans_up_and_posts_no_result(self):
        self.queue(make_task(resume=resume_info("a" * 32)))
        self.server.resume_bytes["a" * 32] = PDF
        self.fake_pass.results = [{"success": True, "screenshots": [self.png()], "missing_fields": []}]
        self.fake_pass.during = self.interrupt_pass
        runner = self.runner(heartbeat=0.02)
        self.assertEqual(runner.run(), R.EXIT_OK)
        call = self.fake_pass.calls[0]
        self.assertTrue(self.browser.contexts[0].closed, "the browser window is closed")
        self.assertFalse(Path(call["resume_path"]).parent.exists(), "the decrypted resume is deleted")
        self.assertEqual(self.server.results, [], "a cut-off pass may have clicked Submit: only the server's own rules may decide")
        self.assertEqual(self.server.shots, [])
        self.assertEqual([t for t in threading.enumerate() if t.name == "runner-heartbeat"], [])
        beats = self.server.beats
        time.sleep(0.1)
        self.assertEqual(self.server.beats, beats, "the heartbeat stopped")

    def test_ctrl_c_while_waiting_for_you_exits_cleanly_too(self):
        self.queue(make_task())
        self.fake_pass.before = lambda count, page: setattr(page.ctx, "captcha", True)
        self.fake_pass.results = [{"success": False, "error": "CAPTCHA detected — skipping", "screenshots": []}]
        def sleep(seconds):
            raise KeyboardInterrupt
        runner = self.runner(sleep=sleep)
        self.assertEqual(runner.run(), R.EXIT_OK)
        self.assertTrue(self.browser.contexts[0].closed)
        self.assertEqual(self.server.results, [])

    def test_a_real_sigint_during_the_long_poll_ends_the_runner_cleanly(self):
        if sys.platform == "win32" or threading.current_thread() is not threading.main_thread():
            self.skipTest("needs POSIX signals delivered to the main thread")
        self.server.hold_claims = True
        runner = self.runner()
        def interrupt():
            if self.server.claim_started.wait(5):
                time.sleep(0.1)
                os.kill(os.getpid(), signal.SIGINT)
        threading.Thread(target=interrupt, daemon=True).start()
        started = time.time()
        self.assertEqual(runner.run(), R.EXIT_OK)
        self.assertLess(time.time() - started, 5, "it did not wait for the 10 s poll to end")

    @unittest.skipIf(sys.platform == "win32", "os.kill(SIGINT) kills a Windows process outright instead of raising KeyboardInterrupt")
    def test_the_command_line_exits_zero_on_ctrl_c(self):
        self.queue(make_task())
        self.server.on_result = lambda task_id, data: os.kill(os.getpid(), signal.SIGINT)
        with mock.patch.object(R, "launch", lambda profile: self.browser()), \
                mock.patch.object(R, "run_pass", self.fake_pass), \
                mock.patch.object(R.threading.Timer, "start", lambda self_: None):
            code = R.main(["--server", self.server.url, "--token", FakeServer.TOKEN, "--profile-dir", str(self.dir / "p")])
        self.assertEqual(code, 0)
        self.assertEqual(len(self.server.results), 1)


# ── nothing secret leaves ─────────────────────────────────────────

class SecretsTests(Base):
    def test_no_cookie_no_password_and_the_token_only_in_the_authorization_header(self):
        self.queue(make_task(resume=resume_info("a" * 32)))
        self.server.resume_bytes["a" * 32] = PDF
        self.server.set_cookie_on_claim = True
        self.fake_pass.results = [{"success": True, "screenshots": [self.png()], "missing_fields": []}]
        self.run_until_result(self.runner())      # FakeContext raises if the runner touches cookies / storage state
        self.assertGreaterEqual(len(self.server.requests), 5)
        for req in self.server.requests:
            self.assertNotIn("cookie", {k.lower() for k in req["headers"]}, f"{req['path']} carried a cookie")
            self.assertNotIn(FakeServer.TOKEN.encode(), req["body"], "the token is not in a body")
            self.assertNotIn(FakeServer.TOKEN, req["path"], "nor in a URL")
            self.assertNotIn(b"hunter2", req["body"], "nor a password from the page")
            self.assertNotIn(b"SERVER-COOKIE", req["body"])
        for line in self.logs:
            self.assertNotIn(FakeServer.TOKEN, line)
            self.assertNotIn("hunter2", line)

    def test_the_token_is_never_logged_not_even_when_everything_fails(self):
        self.server.script["claim"] = [500, "drop", 429, 404, 400]
        self.queue(make_task())
        runner = self.runner()
        n = []
        def sleep(seconds):
            n.append(seconds)
            if len(n) >= 4:
                runner.stop()
        runner.sleep = sleep
        runner.run()
        self.assertGreaterEqual(len(self.logs), 3)
        self.assertEqual(self.token_in_logs(), [])

    def test_the_token_is_not_shown_when_the_backend_is_printed(self):
        backend = self.backend()
        self.assertNotIn(FakeServer.TOKEN, repr(backend._session.auth))
        self.assertNotIn(FakeServer.TOKEN, repr(vars(backend).get("_session").auth))

    def test_a_redirect_is_not_followed_so_the_token_cannot_be_handed_to_another_host(self):
        other = FakeServer()
        self.addCleanup(other.close)
        self.server.redirect_claims_to = other.url + "/api/runner/claim"
        with self.assertRaises(R.Rejected):
            self.backend().claim(0)
        self.assertEqual(other.requests, [], "nothing was sent to the host it pointed at")

    def test_cookies_the_server_sets_are_not_kept_or_sent_back(self):
        self.server.set_cookie_on_claim = True
        self.server.claimed["t" * 32] = {}
        backend = self.backend()
        backend.claim(0)
        backend.claim(0)
        backend.events("t" * 32, [])
        self.assertFalse(any("cookie" in {k.lower() for k in r["headers"]} for r in self.server.requests))

    def test_a_netrc_entry_cannot_replace_the_token(self):
        netrc = self.dir / "netrc"
        netrc.write_text("machine 127.0.0.1 login someone password SECRET-NETRC\n")
        netrc.chmod(0o600)
        with mock.patch.dict(os.environ, {"NETRC": str(netrc), "HOME": str(self.dir)}):
            self.backend().claim(0)
        self.assertEqual(self.server.requests[0]["headers"]["Authorization"], f"Bearer {FakeServer.TOKEN}")


# ── the backends ──────────────────────────────────────────────────

class HttpBackendTests(Base):
    def test_each_status_means_what_the_loop_needs_it_to_mean(self):
        cases = {401: R.Unauthorized, 404: R.TaskGone, 409: R.TaskGone, 500: R.Unavailable, 502: R.Unavailable,
                 503: R.Unavailable, 400: R.Rejected, 413: R.Rejected, 403: R.Rejected}
        backend = self.backend()
        for status, error in cases.items():
            self.server.script["events"] = [status]
            self.server.claimed["t" * 32] = {}
            with self.assertRaises(error, msg=status) as caught:
                backend.events("t" * 32, [])
            self.assertEqual(caught.exception.status, status)
            self.assertIn(f"scripted {status}", str(caught.exception), "the server's own words reach the log")

    def test_a_429_is_transient_only_for_the_claim(self):
        backend = self.backend()
        self.server.script["claim"], self.server.script["events"] = [429], [429]
        with self.assertRaises(R.Unavailable):
            backend.claim(0)
        self.server.claimed["t" * 32] = {}
        with self.assertRaises(R.Rejected):
            backend.events("t" * 32, [])

    def test_the_screenshot_goes_as_multipart_with_the_png_and_its_label(self):
        self.server.claimed["t" * 32] = make_task()
        self.backend().screenshot("t" * 32, make_png(5, 2), "pre_submit")
        self.assertEqual((self.server.shots[0]["png"], self.server.shots[0]["label"], self.server.shots[0]["size"]),
                         (make_png(5, 2), "pre_submit", (5, 2)))

    def test_the_resume_is_returned_as_bytes(self):
        self.server.claimed["t" * 32] = make_task()
        self.server.resume_bytes["t" * 32] = PDF
        self.assertEqual(self.backend().resume("t" * 32), PDF)
        with self.assertRaises(R.TaskGone):
            self.backend().resume("u" * 32)

    def test_an_answer_that_is_not_the_protocols_is_unavailable_not_a_crash(self):
        class Session:
            auth = None
            cookies = type("C", (), {"set_policy": lambda self, p: None})()
            def request(self, *a, **k):
                r = mock.Mock(status_code=200)
                r.json.side_effect = ValueError("<html>")
                return r
        backend = R.HttpBackend(self.server.url, FakeServer.TOKEN, session=Session())
        with self.assertRaises(R.Unavailable):
            backend.claim(0)

    def test_the_address_is_checked_and_joined_properly(self):
        for bad in ("", "example.com", "ftp://x", "http://", "//x"):
            with self.assertRaises(ValueError, msg=bad):
                R.HttpBackend(bad, "apxr_x")
        with self.assertRaises(ValueError):
            R.HttpBackend("https://x.example", "  ")
        self.assertEqual(R.HttpBackend("https://x.example/apex/", "t").base, "https://x.example/apex")
        self.assertEqual(R.HttpBackend("https://x.example", "t").base, "https://x.example")

    def test_only_https_or_this_computer_counts_as_a_safe_way_to_carry_the_token(self):
        self.assertTrue(R.HttpBackend("https://x.example", "t").secure)
        self.assertTrue(R.HttpBackend("http://localhost:8000", "t").secure)
        self.assertTrue(R.HttpBackend("http://127.0.0.1:8000", "t").secure)
        self.assertFalse(R.HttpBackend("http://192.168.1.5:8000", "t").secure)
        self.assertFalse(R.HttpBackend("http://apex.example.com", "t").secure)

    def test_the_command_line_warns_about_http_but_never_prints_the_token(self):
        with mock.patch.object(R.Runner, "run", return_value=0), mock.patch("builtins.print"):
            R.main(["--server", "http://apex.example.com", "--token", FakeServer.TOKEN])
        self.assertTrue(any("not https" in m for m in self.logs))
        self.assertEqual(self.token_in_logs(), [])

    def test_the_command_line(self):
        with self.assertRaises(SystemExit) as e, contextlib.redirect_stderr(io.StringIO()):
            R.parse_args(["--server", "https://x"])
        self.assertEqual(e.exception.code, 2)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            R.parse_args(["--token", "t"])
        args = R.parse_args(["--server", "https://x", "--token", "t"])
        self.assertEqual((args.server, args.token, args.profile_dir), ("https://x", "t", None))
        self.assertEqual(R.parse_args(["--server", "s", "--token", "t", "--profile-dir", "/p"]).profile_dir, "/p")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(R.main(["--server", "not a url", "--token", "t"]), R.EXIT_AUTH)

    def test_the_profile_the_command_line_opens_is_the_one_it_was_given(self):
        opened = []
        with mock.patch.object(R, "launch", lambda profile: opened.append(profile) or self.browser()), \
                mock.patch.object(R.Runner, "run", lambda self_: (self_.browser().__enter__(), 0)[1]):
            R.main(["--server", "https://x.example", "--token", "t", "--profile-dir", "~/my-profile"])
            R.main(["--server", "https://x.example", "--token", "t"])
        self.assertEqual(opened, [Path("~/my-profile").expanduser(), R.DEFAULT_PROFILE])


# ── LocalBackend: the real hub, the real database, the real Orchestrator.finish_pass ──

class FakeProfileDB:
    def __init__(self):
        self.flagged = []

    def summary(self):
        return {"_flat": PROFILE}

    def flag_missing(self, **kw):
        self.flagged.append(kw)

    def get_pending_missing(self, job_id):
        return [{"id": i, "field_key": f["field_key"], "field_label": f["field_label"], "field_type": f["field_type"],
                 "options": f["options"], "placeholder_used": f["placeholder"]}
                for i, f in enumerate(self.flagged, 1) if f["job_id"] == job_id]

    def __getattr__(self, name):
        return mock.Mock()


class Settings:
    def read(self, key, default=""):
        return {"quality_first": "false", "daily_apply_limit": "0", "min_delay_between_applies": "0"}.get(key, default)


class LocalBackendTests(Base):
    def setUp(self):
        super().setUp()
        self.path = self.dir / "jobs.db"
        self.shots_dir = self.dir / "server-shots"
        self.profile_db = FakeProfileDB()
        self._sessions = cfg.SESSIONS_DIR
        cfg.SESSIONS_DIR = self.dir / "sessions"
        cfg.SESSIONS_DIR.mkdir()
        self.addCleanup(lambda: setattr(cfg, "SESSIONS_DIR", self._sessions))
        for p in (mock.patch.object(cfg, "JOBS_DB_PATH", self.path), mock.patch.object(cfg, "DATA_DIR", self.dir),
                  mock.patch.object(cfg, "RESUME_PATH", self.dir / "none.pdf"),
                  mock.patch("applier.session.Orchestrator._settings", staticmethod(lambda: Settings())),
                  mock.patch("applier.session.Orchestrator._dry_run_enabled", staticmethod(lambda: False)),
                  mock.patch("applier.preflight.fetch_page", lambda url: (200, ""))):
            p.start()
            self.addCleanup(p.stop)
        self.db = JobsDB(path=self.path, user_id=1)
        self.token = RunnerTokenStore(1, self.path).create("laptop")
        self.hub = RunnerHub(1, self.path, orchestrator_factory=self.orchestrator, profile_provider=lambda u: {"_flat": PROFILE},
                             screenshots_dir=self.shots_dir, root=self.dir / "resumes")

    def orchestrator(self, job):
        from applier.session import Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        o.job_id, o.job = job["job_id"], self.db.get_by_id(job["job_id"])
        o.profile_db, o.jobs_db = self.profile_db, JobsDB(path=self.path)
        o.session = ApplicationSession.load(job["job_id"]) or ApplicationSession(
            job_id=job["job_id"], job_url=job["url"], job_title=job["title"], company=job["company"], source="linkedin")
        return o

    def make_job(self, stage="preparing"):
        jid = self.db.add({"url": "https://boards.greenhouse.io/acme/jobs/77", "title": "Backend Engineer", "company": "Acme",
                           "source": "linkedin", "description": "Build services."})
        for to in {"preparing": ("preparing",), "ready_for_review": ("preparing", "ready_for_review")}[stage]:
            self.db.transition(jid, to)
        return self.db.get_by_id(jid)

    def local_runner(self, **kw):
        kw.setdefault("run_pass_fn", self.fake_pass)
        kw.setdefault("sleep", self.sleeps.append)
        self.r = R.Runner(R.LocalBackend(self.hub, self.token["id"]), self.browser, **kw)
        return self.r

    def steps(self, job):
        return [(e["detail"].get("step"), e["detail"].get("via")) for e in self.db.get_application(job["job_id"])["events"]
                if e["kind"] == "step"]

    def test_a_first_pass_through_the_real_hub_parks_the_application_for_review(self):
        job = self.make_job()
        self.hub.enqueue(job, "apply")
        shot = self.png("shot.png")
        self.fake_pass.results = [{"success": True, "error": "", "screenshots": [shot], "missing_fields": [],
                                   "review": {"fields": [], "url": job["url"]}}]
        self.fake_pass.during = lambda count, page: self.r.stop()
        self.assertEqual(self.local_runner().run(), R.EXIT_OK)
        self.assertEqual(self.db.get_stage(job["job_id"]), "ready_for_review")
        stored = list(self.shots_dir.glob("*.png"))
        self.assertEqual(len(stored), 1, "the server stored the screenshot under a name it made itself")
        self.assertEqual(stored[0].read_bytes(), make_png())
        self.assertIn(("runner_started", "runner"), self.steps(job))
        self.assertIn(("opened_page", "runner"), self.steps(job))
        self.assertEqual(RunnerTaskStore(1, self.path).list()[0]["status"], "done")
        self.assertEqual(self.fake_pass.calls[0]["profile"], PROFILE, "the profile subset the hub sent")
        self.assertEqual(self.fake_pass.calls[0]["job"]["url"], job["url"])

    def test_a_question_the_form_could_not_answer_parks_it_in_needs_you(self):
        job = self.make_job()
        self.hub.enqueue(job, "apply")
        field = FormField("Why Acme?", "why_acme", "#q", "textarea", [], True, "", True, "")
        self.fake_pass.results = [{"success": True, "error": "", "screenshots": [], "missing_fields": [field],
                                   "review": {"fields": [], "url": ""}}]
        self.fake_pass.during = lambda count, page: self.r.stop()
        self.local_runner().run()
        self.assertEqual(self.db.get_stage(job["job_id"]), "needs_you")
        self.assertEqual([f["field_label"] for f in self.profile_db.flagged], ["Why Acme?"])

    def test_a_failure_through_the_real_hub_fails_the_application(self):
        job = self.make_job()
        self.hub.enqueue(job, "apply")
        self.fake_pass.results = [{"success": False, "error": "Navigation failed: boom", "screenshots": []}]
        self.fake_pass.during = lambda count, page: self.r.stop()
        self.local_runner().run()
        self.assertIn(self.db.get_stage(job["job_id"]), ("failed", "needs_you"))
        self.assertNotEqual(self.db.get_stage(job["job_id"]), "ready_for_review")

    def test_the_resume_comes_through_the_real_hub_checksummed(self):
        from db.resumes_db import ResumeStore
        job = self.make_job()
        ResumeStore(1, self.path, self.dir / "resumes").add("My CV.pdf", PDF, "Main", make_default=True)
        self.hub.enqueue(job, "apply")
        self.fake_pass.during = lambda count, page: self.r.stop()
        self.local_runner().run()
        self.assertEqual((self.fake_pass.calls[0]["resume_bytes"], self.fake_pass.calls[0]["resume_name"]), (PDF, "My CV.pdf"))

    def test_the_hubs_errors_become_the_loops_errors(self):
        job = self.make_job()
        self.hub.enqueue(job, "apply")
        backend = R.LocalBackend(self.hub, self.token["id"])
        task = backend.claim(0)
        self.assertEqual(task["kind"], "apply")
        with self.assertRaises(R.Rejected):
            backend.screenshot(task["task_id"], b"not a png", "x")
        with self.assertRaises(R.TaskGone):
            backend.events("f" * 32, [])
        other = R.LocalBackend(self.hub, self.token["id"] + 99)
        with self.assertRaises(R.TaskGone):
            other.events(task["task_id"], [])
        backend.events(task["task_id"], [])
        self.assertEqual(backend.claim(0), None)

    def test_a_second_poll_of_the_same_token_is_a_wait_not_a_crash(self):
        key = (1, self.token["id"])
        RunnerHub._polling.add(key)
        self.addCleanup(lambda: RunnerHub._polling.discard(key))
        with self.assertRaises(R.Unavailable):
            R.LocalBackend(self.hub, self.token["id"]).claim(0)


# ── the real thing: real Chromium, the real adapter, the real HTTP client ──

class BrowserTests(Base):
    CAPTCHA_DIV = '<div class="g-recaptcha" style="width:50px;height:50px">x</div>'

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                pw.chromium.launch().close()
        except Exception as e:
            raise unittest.SkipTest(f"no usable Chromium for Playwright ({type(e).__name__})")

    def setUp(self):
        super().setUp()
        self.html, self.requested, self.holder = GREENHOUSE_HTML, [], {}
        resume = self.dir / "local-resume.pdf"
        resume.write_bytes(PDF)
        for p in (mock.patch.object(cfg, "opera_exe", lambda: ""),                    # never your own configured browser
                  mock.patch.object(form, "PAUSE", (0, 0)), mock.patch.object(adapter_passes, "SETTLE", (0, 0)),
                  mock.patch.object(adapter_passes, "AFTER_SUBMIT", (0, 0)),
                  mock.patch.object(cfg, "SCREENSHOTS_DIR", self.dir / "shots"), mock.patch.object(cfg, "RESUME_PATH", resume)):
            p.start()
            self.addCleanup(p.stop)
        (self.dir / "shots").mkdir()

    @contextlib.contextmanager
    def browser_cm(self):
        def handler(route):
            self.requested.append(route.request.url)
            route.fulfill(status=200, content_type="text/html", body=self.html)
        with R.launch(self.dir / "profile", headless=True) as ctx:
            ctx.route("**/*", handler)
            self.holder["ctx"] = ctx
            yield ctx

    def real_runner(self, **kw):
        kw.setdefault("sleep", self.sleeps.append)
        self.r = R.Runner(self.backend(), self.browser_cm, **kw)
        return self.r

    def test_the_real_greenhouse_adapter_fills_the_form_in_a_real_browser_and_the_server_gets_everything(self):
        task_id = "c" * 32
        self.queue(make_task(task_id=task_id, resume=resume_info(task_id, filename="Priya Resume.pdf")))
        self.server.resume_bytes[task_id] = PDF
        seen = {}
        def capture(kind, page, *args):
            result = R.run_pass(kind, page, *args)
            seen["uploaded"] = page.locator("input[type=file]").first.evaluate("e => e.files.length ? e.files[0].name : ''")
            seen["email"] = page.locator("#email").input_value()
            return result
        self.assertEqual(self.run_until_result(self.real_runner(run_pass_fn=capture)), R.EXIT_OK)

        body = self.server.results[0]["body"]
        self.assertEqual((body["success"], body["error"]), (True, ""))
        self.assertEqual([m["field_key"] for m in body["missing_fields"]],
                         ["why_do_you_want_to_work_at_acme_robotics", "are_you_at_least_18_years_of_age"])
        snap = {f["field_key"]: f for f in body["review"]["fields"]}
        self.assertEqual((snap["email"]["value"], snap["email"]["hit"]["source"]), ("priya@example.com", "profile"))
        self.assertEqual(seen, {"uploaded": "Priya Resume.pdf", "email": "priya@example.com"},
                         "the resume the SERVER sent was uploaded, under the name the server gave it")
        self.assertEqual(self.requested, [GH_URL])
        codes = self.server.step_codes()
        self.assertEqual(codes[:2], ["runner_started", "opened_page"])
        self.assertIn("filled_fields", codes)
        self.assertIn(("site_type", "greenhouse"), [(e["event"], e["data"]) for e in self.server.events])
        self.assertGreaterEqual(len(self.server.shots), 1)
        self.assertEqual(self.server.shots[0]["png"][:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(self.server.results[0]["shots_before"], len(self.server.shots))
        self.assertEqual(self.token_in_logs(), [])

    def test_a_real_captcha_in_a_real_browser_pauses_until_you_clear_it_and_then_fills_the_form(self):
        task_id = "d" * 32
        self.queue(make_task(task_id=task_id))
        self.html = GREENHOUSE_HTML.replace('<div class="application--container">',
                                            self.CAPTCHA_DIV + '<div class="application--container">')
        polls = []
        def you_solve_it(seconds):
            polls.append(seconds)
            self.html = GREENHOUSE_HTML                                   # the site no longer asks
            self.holder["ctx"].pages[0].evaluate("document.querySelectorAll('.g-recaptcha').forEach(e => e.remove())")
        self.assertEqual(self.run_until_result(self.real_runner(sleep=you_solve_it)), R.EXIT_OK)
        self.assertGreaterEqual(len(polls), 1)
        body = self.server.results[0]["body"]
        self.assertEqual((body["success"], body["error"]), (True, ""), "the pass ran again and filled the form")
        self.assertEqual({f["field_key"]: f["value"] for f in body["review"]["fields"]}["email"], "priya@example.com")
        codes = self.server.step_codes()
        self.assertLess(codes.index("runner_waiting_for_you"), codes.index("runner_resumed"))
        self.assertEqual(len(self.requested), 2, "the page was opened again after you cleared it")

    def test_a_captcha_nobody_solves_is_reported_as_the_captcha_it_is(self):
        self.queue(make_task())
        self.html = GREENHOUSE_HTML.replace('<div class="application--container">',
                                            self.CAPTCHA_DIV + '<div class="application--container">')
        self.run_until_result(self.real_runner(human_wait=4, human_poll=2))
        body = self.server.results[0]["body"]
        self.assertEqual((body["success"], failures.classify(body["error"])), (False, "captcha"))
        self.assertIn("runner_gave_up_waiting", self.server.step_codes())


if __name__ == "__main__":
    unittest.main()
