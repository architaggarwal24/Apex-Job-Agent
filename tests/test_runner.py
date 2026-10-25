# tests/test_runner.py
"""
tests/test_runner.py — applications run on a RUNNER on your own computer.

  * runner tokens: created once, stored only as a SHA-256, scoped to one user,
    revocable (and dead the moment it is), last_seen tracked;
  * the bearer-token check (core/runner_auth.py) and the executor setting
    (server | runner | auto, applier/executors.py);
  * the Orchestrator leaves a pass for a runner instead of opening a browser, and
    a runner's result continues the application exactly like a local pass;
  * the hub (applier/runner_hub.py): claim as a long poll (<= 25 s, one task,
    the oldest, per user), events, screenshots (PNG, <= 3 MB, signature checked,
    the name never the client's), result, resume download;
  * leases: a runner that goes quiet gets its task re-queued, an apply/refill
    task is failed after too many lost runners or too long in the queue, and a
    SUBMIT that went quiet is never re-run (the runner may have clicked Submit);
  * the queue worker leaves a job waiting for a runner alone;
  * a token cannot reach any other endpoint: the exact public path set, and an
    AST audit that only server/routes/runner.py consults a token, every route
    there depends on require_runner, and the session code never reads the header.

The HTTP class at the end drives the real routes and skips without FastAPI.
No browser, no network, no real LLM.

Run from the project root:   python3 -m unittest discover -s tests -v
"""

import ast
import contextlib
import hashlib
import json
import os
import re
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
os.environ.setdefault("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())

from applier import executors, pack
from applier.executors import RunnerExecutor, ServerExecutor
from applier.runner_hub import (MAX_PNG_BYTES, MAX_WAIT_SECONDS, Conflict, MissingField, NotFound, RunnerError,
                                RunnerHub, TooLarge, TooMany, clean_event, clean_result, validate_png)
from applier.session import ApplicationSession, State
from core.config import cfg
from core.runner_auth import authenticate_header, parse_bearer
from db import runner_db
from db.answer_bank_db import AnswerBank
from db.jobs_db import JobsDB
from db.kits_db import KitStore
from db.resumes_db import ResumeStore
from db.runner_db import (LEASE_SECONDS, MAX_ACTIVE_TOKENS, MAX_ATTEMPTS, QUEUE_TTL_SECONDS, SEEN_WINDOW_SECONDS,
                          RunnerTaskStore, RunnerTokenStore, authenticate, hash_token)
from server import static_shell

try:
    from fastapi.testclient import TestClient
    HAVE_FASTAPI = True
except Exception:
    HAVE_FASTAPI = False

ROOT = Path(__file__).resolve().parent.parent
PDF = b"%PDF-1.4\n% the chosen resume\n"
PROFILE = {"_flat": {
    "full_name": "Priya Sharma", "first_name": "Priya", "last_name": "Sharma", "email": "priya@example.com",
    "phone": "+91 98765 43210", "linkedin_url": "https://linkedin.com/in/priya", "authorized_to_work": "Yes",
    # never sent to a runner:
    "gender": "Female", "date_of_birth": "01/02/1990", "ethnicity": "Asian", "veteran_status": "No",
    "openai_api_key": "sk-SECRET-should-never-leave", "anthropic_api_key": "sk-ant-SECRET"}}


def make_png(width: int = 4, height: int = 3, pad_to: int = 0) -> bytes:
    """A real, valid PNG (optionally padded after IEND to `pad_to` bytes)."""
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return png + b"\x00" * max(0, pad_to - len(png))


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds: float):
        self.t += seconds


class Users:
    """Stands in for UsersDB: get_by_id is None for an account that is not active."""
    def __init__(self, *active):
        self.active = set(active or (1, 2))

    def get_by_id(self, uid):
        return {"id": uid} if uid in self.active else None


class Settings:
    def __init__(self, **values):
        self.values = {k: (v if isinstance(v, str) else str(v)) for k, v in values.items()}

    def read(self, key, default=""):
        return self.values.get(key, default)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "jobs.db"
        self.shots = self.dir / "shots"
        self.clock = Clock()
        self.users = Users()
        self.n = 0
        self._sessions = cfg.SESSIONS_DIR
        cfg.SESSIONS_DIR = self.dir / "sessions"
        cfg.SESSIONS_DIR.mkdir()
        self.addCleanup(lambda: setattr(cfg, "SESSIONS_DIR", self._sessions))
        for p in (mock.patch.object(cfg, "DATA_DIR", self.dir), mock.patch.object(cfg, "JOBS_DB_PATH", self.path),
                  mock.patch.object(cfg, "RESUME_PATH", self.dir / "no-legacy-resume.pdf"),
                  mock.patch.object(tempfile, "tempdir", str(self.dir)),
                  mock.patch("urllib.request.urlopen", side_effect=AssertionError("network"))):
            p.start()
            self.addCleanup(p.stop)

    # stores and hubs
    def db(self, uid=1):
        return JobsDB(path=self.path, user_id=uid)

    def tokens(self, uid=1):
        return RunnerTokenStore(uid, self.path)

    def tasks(self, uid=1):
        return RunnerTaskStore(uid, self.path)

    def make_token(self, uid=1, label="laptop"):
        return self.tokens(uid).create(label, now=self.clock())

    def auth(self, token):
        return authenticate(token["token"], self.path, now=self.clock(), users=self.users)

    def job(self, uid=1, stage="preparing", company="Acme", url=None):
        self.n += 1
        db = self.db(uid)
        jid = db.add({"url": url or f"https://boards.greenhouse.io/acme/jobs/{uid}{self.n}", "title": f"Backend Engineer {self.n}",
                      "company": company, "source": "linkedin", "description": "Build Python services."})
        self.assertIsNotNone(jid)
        steps = {"found": (), "queued": ("queued",), "preparing": ("preparing",),
                 "ready_for_review": ("preparing", "ready_for_review"), "skipped": ("skipped",),
                 "submitted": ("preparing", "ready_for_review", "submitted")}[stage]
        for to in steps:
            db.transition(jid, to)
        return db.get_by_id(jid)

    def events(self, job, uid=1):
        return self.db(uid).get_application(job["job_id"])["events"]

    def step_codes(self, job, uid=1):
        return [e["detail"].get("step") for e in self.events(job, uid) if e["kind"] == "step"]

    def stage(self, job, uid=1):
        return self.db(uid).get_stage(job["job_id"])


# ── runner tokens ─────────────────────────────────────────────────

class TokenTests(Base):
    def test_a_token_is_shown_once_and_never_listed(self):
        t = self.make_token()
        self.assertTrue(t["token"].startswith("apxr_"))
        self.assertGreaterEqual(len(t["token"]), 40)
        for listed in self.tokens().list():
            self.assertNotIn("token", listed)
            self.assertNotIn("token_hash", listed)
            self.assertNotIn(t["token"], json.dumps(listed))
        self.assertEqual(self.tokens().list()[0]["label"], "laptop")

    def test_only_a_sha256_is_stored_never_the_token(self):
        t = self.make_token()
        import sqlite3
        with contextlib.closing(sqlite3.connect(self.path)) as c:        # a bare `with connect()` never closes it (Windows then cannot delete the file)
            rows = c.execute("SELECT * FROM runner_tokens").fetchall()
            cols = [d[0] for d in c.execute("SELECT * FROM runner_tokens").description]
        self.assertEqual(len(rows), 1)
        self.assertEqual(dict(zip(cols, rows[0]))["token_hash"], hashlib.sha256(t["token"].encode()).hexdigest())
        for value in rows[0]:
            self.assertNotEqual(value, t["token"])
            self.assertNotIn(t["token"], str(value))
        for f in self.dir.iterdir():                                    # the db, its WAL, anything else on disk
            if f.is_file():
                self.assertNotIn(t["token"].encode(), f.read_bytes(), f.name)

    def test_hashing_is_deterministic_and_not_reversible_text(self):
        self.assertEqual(hash_token("apxr_x"), hash_token("apxr_x"))
        self.assertNotEqual(hash_token("apxr_x"), hash_token("apxr_y"))
        self.assertRegex(hash_token("apxr_x"), r"^[0-9a-f]{64}$")

    def test_tokens_are_unique_and_unguessable_looking(self):
        seen = {self.make_token(label=f"t{i}")["token"] for i in range(5)}
        self.assertEqual(len(seen), 5)

    def test_a_good_token_authenticates_as_its_owner(self):
        t = self.make_token(1)
        self.assertEqual(self.auth(t), {"token_id": t["id"], "user_id": 1, "label": "laptop"})

    def test_it_is_scoped_to_one_user(self):
        a, b = self.make_token(1, "alice"), self.make_token(2, "bob")
        self.assertEqual((self.auth(a)["user_id"], self.auth(b)["user_id"]), (1, 2))
        self.assertEqual([x["id"] for x in self.tokens(1).list()], [a["id"]])
        self.assertEqual([x["id"] for x in self.tokens(2).list()], [b["id"]])

    def test_anything_that_is_not_a_live_token_is_refused(self):
        t = self.make_token()
        for bad in (None, "", 0, 5, b"apxr_x", [], "apxr_", "apxr_" + "x" * 40, "wrong_" + t["token"][5:],
                    t["token"][:-1], t["token"] + "x", t["token"].upper(), " " + t["token"], "apxr_" + "a" * 500):
            self.assertIsNone(authenticate(bad, self.path, now=self.clock(), users=self.users), repr(bad)[:40])

    def test_input_that_cannot_be_a_token_is_never_even_hashed(self):
        with mock.patch.object(runner_db, "hash_token", side_effect=AssertionError("hashed attacker input")):
            for bad in ("x" * 10_000_000, "no_prefix_at_all", "apxr_" + "a" * 500, "", None, 5):
                self.assertIsNone(authenticate(bad, self.path, now=self.clock(), users=self.users))

    def test_the_hash_is_not_a_token(self):
        t = self.make_token()
        self.assertIsNone(authenticate(hash_token(t["token"]), self.path, now=self.clock(), users=self.users))

    def test_last_seen_is_tracked(self):
        t = self.make_token()
        self.assertEqual(self.tokens().list()[0]["last_seen_at"], "")
        self.clock.advance(500)
        self.auth(t)
        self.assertEqual(self.tokens().list()[0]["last_seen_at"], runner_db.iso(self.clock()))

    def test_revoking_stops_it_at_once_and_records_when(self):
        t = self.make_token()
        self.assertIsNotNone(self.auth(t))
        self.assertTrue(self.tokens().revoke(t["id"], now=self.clock()))
        self.assertIsNone(self.auth(t))
        listed = self.tokens().list()[0]
        self.assertEqual((listed["active"], listed["revoked_at"] != ""), (False, True))

    def test_a_revoked_token_is_not_marked_seen_again(self):
        t = self.make_token()
        self.tokens().revoke(t["id"], now=self.clock())
        seen = self.tokens().list()[0]["last_seen_at"]
        self.clock.advance(100)
        self.auth(t)
        self.assertEqual(self.tokens().list()[0]["last_seen_at"], seen)

    def test_revoking_is_idempotent_and_only_for_your_own(self):
        t = self.make_token(1)
        self.assertTrue(self.tokens(1).revoke(t["id"]))
        self.assertTrue(self.tokens(1).revoke(t["id"]))
        other = self.make_token(1, "second")
        self.assertFalse(self.tokens(2).revoke(other["id"]), "Bob cannot revoke Alice's token")
        self.assertFalse(self.tokens(1).revoke(99999))
        self.assertIsNotNone(self.auth(other), "and it still works")

    def test_a_token_of_a_deactivated_account_is_dead(self):
        t = self.make_token(2)
        self.users.active.discard(2)
        self.assertIsNone(self.auth(t))
        self.users.active.add(2)
        self.assertIsNotNone(self.auth(t))

    def test_label_rules_and_the_limit_of_live_tokens(self):
        for bad in (None, "", "   ", 5, "x" * 61):
            with self.assertRaises(ValueError):
                self.tokens().create(bad)
        self.assertEqual(self.tokens().create("  my   laptop ")["label"], "my laptop")
        for i in range(MAX_ACTIVE_TOKENS - 1):
            self.make_token(label=f"t{i}")
        with self.assertRaises(ValueError):
            self.make_token(label="one too many")
        self.tokens().revoke(self.tokens().list()[0]["id"])
        self.make_token(label="fits again")                               # a revoked one frees a place
        self.make_token(2, "bob is separate")

    def test_seen_recently_for_the_auto_executor(self):
        t = self.make_token()
        self.assertFalse(self.tokens().seen_recently(self.clock()))
        self.auth(t)
        self.assertTrue(self.tokens().seen_recently(self.clock()))
        self.assertTrue(self.tokens().seen_recently(self.clock() + SEEN_WINDOW_SECONDS))
        self.assertFalse(self.tokens().seen_recently(self.clock() + SEEN_WINDOW_SECONDS + 1))
        self.assertFalse(self.tokens(2).seen_recently(self.clock()), "another user's runner is not yours")
        self.tokens().revoke(t["id"])
        self.assertFalse(self.tokens().seen_recently(self.clock()), "a revoked token does not count")

    def test_a_store_needs_a_user_id(self):
        for bad in (None, "1", True):
            with self.assertRaises(ValueError):
                RunnerTokenStore(bad, self.path)
            with self.assertRaises(ValueError):
                RunnerTaskStore(bad, self.path)


class TaskStoreTests(Base):
    """The store enforces who may act on a task itself, not only the hub above it."""

    def setUp(self):
        super().setUp()
        self.job_ = self.job()
        self.task, _ = self.tasks().enqueue(self.job_["job_id"], "apply", {}, now=self.clock())

    def test_only_the_token_that_claimed_a_task_can_complete_renew_or_read_it_as_its_own(self):
        self.assertIsNotNone(self.tasks().claim(7, self.clock()))
        for other in (8, 0, 99999):
            self.assertFalse(self.tasks().complete(self.task["id"], other, self.clock(), "success"), other)
            self.assertFalse(self.tasks().renew(self.task["id"], other, self.clock()), other)
            self.assertIsNone(self.tasks().get_claimed(self.task["id"], other), other)
        self.assertEqual(self.tasks().get(self.task["id"])["status"], "claimed")
        self.assertTrue(self.tasks().renew(self.task["id"], 7, self.clock()))
        self.assertTrue(self.tasks().complete(self.task["id"], 7, self.clock(), "success"))

    def test_a_task_is_completed_once(self):
        self.tasks().claim(7, self.clock())
        self.assertTrue(self.tasks().complete(self.task["id"], 7, self.clock(), "success"))
        self.assertFalse(self.tasks().complete(self.task["id"], 7, self.clock(), "success"))
        self.assertFalse(self.tasks().cancel(self.task["id"], self.clock(), "too late"))
        self.assertEqual(self.tasks().get(self.task["id"])["status"], "done")

    def test_an_unclaimed_task_cannot_be_completed(self):
        self.assertFalse(self.tasks().complete(self.task["id"], 7, self.clock(), "success"))
        self.assertEqual(self.tasks().get(self.task["id"])["status"], "queued")

    def test_another_users_store_cannot_act_on_it_at_all(self):
        self.tasks().claim(7, self.clock())
        bob = self.tasks(2)
        self.assertIsNone(bob.get(self.task["id"]))
        self.assertFalse(bob.complete(self.task["id"], 7, self.clock(), "success"))
        self.assertFalse(bob.renew(self.task["id"], 7, self.clock()))
        self.assertFalse(bob.cancel(self.task["id"], self.clock(), "x"))
        self.assertIsNone(bob.open_task_for(self.job_["job_id"]))
        self.assertEqual(bob.list(), [])
        self.assertEqual(self.tasks().get(self.task["id"])["status"], "claimed")

    def test_unknown_kinds_are_refused(self):
        with self.assertRaises(ValueError):
            self.tasks().enqueue("j", "format-disk", {})


class BearerTests(Base):
    def test_only_a_well_formed_bearer_header_yields_a_token(self):
        self.assertEqual(parse_bearer("Bearer apxr_abc"), "apxr_abc")
        self.assertEqual(parse_bearer("bearer apxr_abc"), "apxr_abc")
        self.assertEqual(parse_bearer("BEARER apxr_abc"), "apxr_abc")
        for bad in (None, "", "Bearer", "Bearer ", "apxr_abc", "Basic YWxpY2U6cGFzcw==", "Token apxr_abc",
                    "Bearer a b", "Bearer  apxr_abc", " Bearer apxr_abc", "Bearerapxr_abc", 5, b"Bearer x"):
            self.assertIsNone(parse_bearer(bad), repr(bad))

    def test_the_header_authenticates_a_live_token_only(self):
        t = self.make_token()
        kw = dict(now=self.clock(), users=self.users)
        auth = authenticate_header(f"Bearer {t['token']}", self.path, **kw)
        self.assertEqual((auth.token_id, auth.user_id, auth.label), (t["id"], 1, "laptop"))
        self.assertIsNone(authenticate_header(t["token"], self.path, **kw), "the bare token is not a header")
        self.assertIsNone(authenticate_header(f"Basic {t['token']}", self.path, **kw))
        self.assertIsNone(authenticate_header(None, self.path, **kw))
        self.tokens().revoke(t["id"])
        self.assertIsNone(authenticate_header(f"Bearer {t['token']}", self.path, **kw))


# ── the executor setting ──────────────────────────────────────────

class ExecutorChoiceTests(Base):
    def choose(self, setting=None, uid=1, **values):
        s = Settings(**({"executor": setting} if setting is not None else {}), **values)
        return executors.choose(s, uid, self.path, now=self.clock()).name

    def test_it_defaults_to_the_server(self):
        self.assertEqual(self.choose(), "server")
        self.assertEqual(self.choose(""), "server")

    def test_server_and_runner_are_what_they_say(self):
        self.assertEqual(self.choose("server"), "server")
        self.assertEqual(self.choose("runner"), "runner")
        self.assertEqual(self.choose("  RUNNER "), "runner")

    def test_a_server_setting_ignores_a_connected_runner(self):
        t = self.make_token()
        self.auth(t)
        self.assertEqual(self.choose("server"), "server")

    def test_auto_is_the_runner_only_if_one_was_seen_recently(self):
        self.assertEqual(self.choose("auto"), "server")
        t = self.make_token()
        self.assertEqual(self.choose("auto"), "server", "a token that has never connected is not a runner")
        self.auth(t)
        self.assertEqual(self.choose("auto"), "runner")
        self.clock.advance(SEEN_WINDOW_SECONDS + 1)
        self.assertEqual(self.choose("auto"), "server", "it went quiet")

    def test_auto_does_not_count_a_revoked_or_another_users_runner(self):
        t = self.make_token(1)
        self.auth(t)
        self.assertEqual(self.choose("auto", uid=2), "server")
        self.tokens(1).revoke(t["id"])
        self.assertEqual(self.choose("auto", uid=1), "server")

    def test_an_unknown_or_unreadable_setting_is_the_server(self):
        self.assertEqual(self.choose("sometimes"), "server")
        class Broken:
            def read(self, *a): raise RuntimeError("db gone")
        self.assertEqual(executors.choose(Broken(), 1, self.path).name, "server")

    def test_clean_mode(self):
        self.assertEqual(executors.clean_mode(" Auto "), "auto")
        for bad in ("", "manual", None, 1, True):
            with self.assertRaises(ValueError):
                executors.clean_mode(bad)

    def test_the_seeded_default_is_server(self):
        from db.settings_db import SettingsDB
        self.assertEqual(SettingsDB(path=self.dir / "settings.db").read("executor"), "server")

    def test_the_two_executors_are_what_the_interface_says(self):
        self.assertEqual((ServerExecutor().name, RunnerExecutor().name), ("server", "runner"))
        self.assertTrue(issubclass(ServerExecutor, executors.Executor) and issubclass(RunnerExecutor, executors.Executor))


# ── the exact public paths and the audit that a token reaches nothing else ──

def route_table(module_path: Path) -> list[tuple[str, str, ast.AST]]:
    """(method, path, function node) for every @router.<verb>("...") in a route module."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and isinstance(dec.func.value, ast.Name) and dec.func.value.id == "router"
                        and dec.func.attr in ("get", "post", "put", "delete", "patch") and dec.args
                        and isinstance(dec.args[0], ast.Constant)):
                    out.append((dec.func.attr.upper(), dec.args[0].value, node))
    return out


class TokenCannotReachOtherEndpointsTests(unittest.TestCase):
    TASK = "a" * 32
    RUNNER_ROUTES = {("POST", "/api/runner/claim"), ("POST", "/api/runner/{task_id}/events"),
                     ("POST", "/api/runner/{task_id}/screenshot"), ("POST", "/api/runner/{task_id}/result"),
                     ("GET", "/api/runner/{task_id}/resume")}

    def test_exactly_the_five_runner_paths_are_let_past_the_session_gate(self):
        public = [f"/api/runner/claim", *(f"/api/runner/{self.TASK}/{leaf}" for leaf in ("events", "screenshot", "result", "resume"))]
        for p in public:
            self.assertTrue(static_shell.is_public_path(p, None), p)

    def test_nothing_else_near_them_is_public(self):
        for p in ("/api/runner", "/api/runner/", "/api/runner/claim/", "/api/runner/claim/x", "/api/runner//claim",
                  "/api/runner/../settings", "/api/runner/%2e%2e/settings", f"/api/runner/{self.TASK}",
                  f"/api/runner/{self.TASK}/", f"/api/runner/{self.TASK}/other", f"/api/runner/{self.TASK}/events/x",
                  f"/api/runner/{self.TASK[:-1]}/events", f"/api/runner/{self.TASK}0/events",
                  f"/api/runner/{self.TASK.upper()}/events", "/api/runner/not-a-task-id/events", "/api/runners/claim",
                  "/api/runner/claim?x=1", "/api/runnerclaim", "/api/runner/tokens", "/api/runner/tokens/1", "/api/runner/revoke",
                  "/api/settings/runner-tokens", "/api/settings/runner-tokens/1",
                  "/api/settings", "/api/applications/x/pack", "/api/queue/status", "/api/apply/start", "/api/jobs"):
            self.assertFalse(static_shell.is_public_path(p, None), p)

    def test_the_token_management_routes_are_behind_the_session_gate(self):
        for p in ("/api/settings/runner-tokens", "/api/settings/runner-tokens/5"):
            self.assertFalse(static_shell.is_runner_path(p))
            self.assertFalse(static_shell.is_public_path(p, None))

    def test_the_runner_module_has_exactly_the_documented_routes(self):
        table = {(m, p) for m, p, _ in route_table(ROOT / "server" / "routes" / "runner.py")}
        self.assertEqual(table, self.RUNNER_ROUTES)

    def test_every_runner_route_depends_on_require_runner_and_nothing_else_authenticates_it(self):
        for method, path, fn in route_table(ROOT / "server" / "routes" / "runner.py"):
            defaults = fn.args.defaults + fn.args.kw_defaults
            deps = [d for d in defaults if isinstance(d, ast.Call) and getattr(d.func, "id", "") == "Depends"]
            self.assertTrue(any(isinstance(d.args[0], ast.Name) and d.args[0].id == "require_runner" for d in deps),
                            f"{method} {path} must Depends(require_runner)")
            for d in deps:
                self.assertEqual(d.args[0].id, "require_runner", f"{method} {path} must use no other dependency")

    def test_every_runner_route_path_matches_the_public_path_pattern_and_vice_versa(self):
        for method, path, _ in route_table(ROOT / "server" / "routes" / "runner.py"):
            self.assertTrue(static_shell.is_public_path(path.replace("{task_id}", self.TASK), None), path)

    def test_no_other_route_module_touches_a_runner_token(self):
        for f in sorted((ROOT / "server" / "routes").glob("*.py")):
            if f.name == "runner.py":
                continue
            src = f.read_text(encoding="utf-8")
            if f.name == "runner_tokens.py":
                self.assertNotIn("require_runner", src)
                self.assertNotIn("authenticate_header", src)
                continue
            for needle in ("require_runner", "authenticate_header", "runner_auth", "runner_db.authenticate"):
                self.assertNotIn(needle, src, f"{f.name} must not authenticate runner tokens")

    def test_the_session_code_never_reads_the_authorization_header(self):
        for rel in ("core/auth.py", "server/static_shell.py", "server/app.py", "server/websocket.py"):
            tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) and len(node.value) < 60:
                    self.assertNotIn("authorization", node.value.lower(), rel)
                    self.assertNotEqual(node.value.lower(), "bearer", rel)

    def test_no_non_runner_route_path_could_match_a_runner_path(self):
        for f in sorted((ROOT / "server" / "routes").glob("*.py")):
            if f.name == "runner.py":
                continue
            for method, path, _ in route_table(f):
                self.assertFalse(path.startswith("/api/runner/"), f"{f.name}: {path}")
                self.assertFalse(static_shell.is_runner_path(re.sub(r"\{[^}]+\}", self.TASK, path)), f"{f.name}: {path}")

    def test_the_runner_routes_are_registered_and_the_owner_ones_are_owner_only(self):
        tree = ast.parse((ROOT / "server" / "app.py").read_text(encoding="utf-8"))
        included = {el.id for node in ast.walk(tree) if isinstance(node, ast.For) and isinstance(node.iter, ast.List)
                    for el in node.iter.elts if isinstance(el, ast.Name)}
        self.assertIn("runner_router", included, "the runner routes must be in the list of routers the app includes")
        self.assertIn("runner_tokens_router", included)
        for method, path, fn in route_table(ROOT / "server" / "routes" / "runner_tokens.py"):
            self.assertTrue(path.startswith("/api/settings/runner-tokens"), path)
            defaults = [d for d in fn.args.defaults if isinstance(d, ast.Call)]
            self.assertTrue(any(d.args and isinstance(d.args[0], ast.Name) and d.args[0].id == "require_owner" for d in defaults),
                            f"{method} {path} must be owner-only")

    def test_the_runner_auth_module_reads_no_cookie_and_no_query_string(self):
        tree = ast.parse((ROOT / "core" / "runner_auth.py").read_text(encoding="utf-8"))
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        self.assertFalse(attrs & {"cookies", "query_params", "session", "get_cookie"}, attrs)


# ── the hub ───────────────────────────────────────────────────────

class FakeProfile:
    """Stands in for ProfileDB: records the questions a pass could not answer."""
    def __init__(self):
        self.flagged = []

    def summary(self):
        return PROFILE

    def flag_missing(self, **kw):
        self.flagged.append(kw)

    def get_pending_missing(self, job_id):
        return [{"id": i, "field_key": f["field_key"], "field_label": f["field_label"], "field_type": f["field_type"],
                 "options": f["options"], "placeholder_used": f["placeholder"]}
                for i, f in enumerate(self.flagged, 1) if f["job_id"] == job_id]

    def __getattr__(self, name):                       # set_many, mark_answered... whatever the Orchestrator calls
        return mock.Mock()


class HubBase(Base):
    def setUp(self):
        super().setUp()
        self.waits, self.hooks = [], []
        self.profile = FakeProfile()
        self.settings = Settings(quality_first="false", daily_apply_limit=0, min_delay_between_applies=0)
        self.browser = []
        for p in (mock.patch("applier.session.Orchestrator._settings", staticmethod(lambda: self.settings)),
                  mock.patch("applier.session.Orchestrator._dry_run_enabled", staticmethod(lambda: self.settings.read("dry_run", "false") == "true")),
                  mock.patch("applier.preflight.fetch_page", lambda url: (200, "")),
                  mock.patch("playwright.sync_api.sync_playwright", lambda: self._browser_called())):
            p.start()
            self.addCleanup(p.stop)

    def _browser_called(self):
        self.browser.append("sync_playwright")
        raise AssertionError("browser code ran")

    def waiter(self, seconds):
        self.waits.append(seconds)
        self.clock.advance(seconds)
        for hook in list(self.hooks):
            hook(len(self.waits))

    def orch_factory(self, job):
        from applier.session import Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        o.job_id, o.job = job["job_id"], self.db(job["user_id"]).get_by_id(job["job_id"])
        o.profile_db = self.profile
        o.jobs_db = JobsDB(path=self.path)
        o.session = ApplicationSession.load(job["job_id"]) or ApplicationSession(
            job_id=job["job_id"], job_url=job["url"], job_title=job["title"], company=job["company"], source="linkedin")
        return o

    def hub(self, uid=1, **kw):
        kw.setdefault("clock", self.clock)
        kw.setdefault("waiter", self.waiter)
        kw.setdefault("orchestrator_factory", self.orch_factory)
        kw.setdefault("profile_provider", lambda u: PROFILE if u == 1 else None)
        kw.setdefault("screenshots_dir", self.shots)
        kw.setdefault("root", self.dir / "resumes")
        return RunnerHub(uid, self.path, **kw)

    def queue(self, job, kind="apply", params=None, uid=1):
        task, _ = self.hub(uid).enqueue(job, kind, params or {})
        return task

    def claim(self, token, uid=1, wait=0):
        return self.hub(uid).claim(token["id"], wait)

    def raw_token_row(self, token_id):
        import sqlite3
        with contextlib.closing(sqlite3.connect(self.path)) as c:
            c.row_factory = sqlite3.Row
            return dict(c.execute("SELECT * FROM runner_tokens WHERE id=?", (token_id,)).fetchone())


class PayloadTests(HubBase):
    def setUp(self):
        super().setUp()
        self.token = self.make_token()
        self.job_ = self.job(company="Acme Robotics")
        self.resumes = ResumeStore(1, self.path, self.dir / "resumes")

    def payload(self, **kw):
        self.queue(self.job_, **kw)
        return self.claim(self.token)

    def test_a_task_carries_what_a_pass_needs(self):
        p = self.payload()
        self.assertEqual(set(p), {"task_id", "kind", "attempt", "lease_seconds", "application", "profile", "answers", "kit", "resume"})
        self.assertEqual((p["kind"], p["attempt"], p["lease_seconds"]), ("apply", 1, LEASE_SECONDS))
        app = p["application"]
        self.assertEqual((app["job_id"], app["title"], app["company"], app["url"], app["site_type"], app["params"]),
                         (self.job_["job_id"], self.job_["title"], "Acme Robotics", self.job_["url"], "greenhouse", {}))
        self.assertRegex(p["task_id"], r"^[0-9a-f]{32}$")

    def test_the_profile_is_a_whitelisted_subset(self):
        p = self.payload()
        self.assertEqual(p["profile"], {"full_name": "Priya Sharma", "first_name": "Priya", "last_name": "Sharma",
                                        "email": "priya@example.com", "phone": "+91 98765 43210",
                                        "linkedin_url": "https://linkedin.com/in/priya", "authorized_to_work": "Yes"})
        self.assertTrue(set(p["profile"]) <= set(pack.PROFILE_KEYS))

    def test_no_secret_and_no_sensitive_fact_ever_leaves(self):
        text = json.dumps(self.payload())
        for private in ("sk-SECRET", "sk-ant-SECRET", "openai_api_key", "anthropic_api_key", "Female", "01/02/1990",
                        "Asian", "gender", "date_of_birth", "ethnicity", "veteran"):
            self.assertNotIn(private, text)

    def test_the_profile_is_only_the_profile_owners(self):
        bob = self.make_token(2)
        job2 = self.job(uid=2)
        self.queue(job2, uid=2)
        self.assertEqual(self.claim(bob, uid=2)["profile"], {})

    def test_answers_are_this_users_global_and_this_companys_only(self):
        bank = AnswerBank(1, self.path)
        bank.add("Why this role?", "Because.", scope="global")
        bank.add("Notice period at Acme?", "30 days", scope="company", company="Acme Robotics")
        bank.add("About Globex", "globex secret", scope="company", company="Globex")
        AnswerBank(2, self.path).add("Bobs question here", "bob secret")
        text = self.payload()["answers"]["bank"]
        self.assertEqual(sorted(a["question"] for a in text), ["Notice period at Acme?", "Why this role?"])
        self.assertEqual(set(text[0]), {"id", "question", "answer", "scope", "company", "policy"})
        flat = json.dumps(text)
        self.assertNotIn("globex secret", flat)
        self.assertNotIn("bob secret", flat)

    def test_what_you_answered_for_this_application_is_included(self):
        s = ApplicationSession(job_id=self.job_["job_id"], job_url="u", job_title="t", company="Acme", source="x")
        s.application_answers = {"why us": "Robots."}
        s.site_type = "lever"
        s.save()
        p = self.payload(kind="refill", params={"missing_keys": ["why_us"]})
        self.assertEqual(p["answers"]["application"], {"why us": "Robots."})
        self.assertEqual((p["kind"], p["application"]["params"], p["application"]["site_type"]),
                         ("refill", {"missing_keys": ["why_us"]}, "lever"))

    def test_the_kit_and_the_resume_download(self):
        v = self.resumes.add("Priya_Backend.pdf", PDF, "Backend")
        KitStore(1, self.path).add_version(self.job_["job_id"], variant_id=v["id"], variant_label="Backend",
                                           selection="llm", summary="S1. S2. S3.", cover_letter="Dear Acme.", status="ready")
        p = self.payload()
        self.assertEqual(p["kit"], {"version": 1, "summary": "S1. S2. S3.", "cover_letter": "Dear Acme.",
                                    "selection": "llm", "resume_label": "Backend"})
        self.assertEqual(p["resume"], {"filename": "Priya_Backend.pdf", "size": len(PDF),
                                       "sha256": hashlib.sha256(PDF).hexdigest(), "url": f"/api/runner/{p['task_id']}/resume"})

    def test_without_a_kit_or_a_resume_there_is_nothing_to_download(self):
        p = self.payload()
        self.assertEqual((p["kit"], p["resume"]), (None, None))

    def test_the_old_single_resume_file_is_offered_when_there_are_no_variants(self):
        legacy = self.dir / "resume.pdf"
        legacy.write_bytes(PDF)
        with mock.patch.object(cfg, "RESUME_PATH", legacy):
            self.assertEqual(self.payload()["resume"]["filename"], "resume.pdf")

    def test_the_job_url_is_only_ever_a_plain_http_link(self):
        evil = self.job(url="javascript:alert(1)")
        self.queue(evil)
        self.assertEqual(self.claim(self.token)["application"]["url"], "")


class ClaimTests(HubBase):
    def setUp(self):
        super().setUp()
        self.token = self.make_token()

    def test_the_oldest_task_goes_first_and_each_goes_to_one_claim_only(self):
        jobs = [self.job() for _ in range(3)]
        for j in jobs:
            self.queue(j)
            self.clock.advance(1)
        got = [self.claim(self.token)["application"]["job_id"] for _ in range(3)]
        self.assertEqual(got, [j["job_id"] for j in jobs])
        self.assertIsNone(self.claim(self.token), "nothing left to claim")

    def test_a_claim_records_who_and_counts_the_attempt(self):
        job = self.job()
        task = self.queue(job)
        self.claim(self.token)
        row = self.tasks().get(task["id"])
        self.assertEqual((row["status"], row["claimed_by"], row["attempts"], row["lease_until"]),
                         ("claimed", self.token["id"], 1, self.clock() + LEASE_SECONDS))

    def test_two_runners_never_get_the_same_task(self):
        other = self.make_token(1, "second laptop")
        self.queue(self.job())
        first = self.claim(self.token)
        self.assertIsNotNone(first)
        self.assertIsNone(self.claim(other))

    def test_a_token_only_ever_gets_its_own_users_tasks(self):
        bob = self.make_token(2, "bob")
        task = self.queue(self.job(uid=1))
        self.assertIsNone(self.claim(bob, uid=2))
        self.assertEqual(self.tasks(1).get(task["id"])["status"], "queued", "Alice's task is untouched")
        self.assertIsNone(self.tasks(2).get(task["id"]), "and invisible to Bob's store")
        self.assertIsNotNone(self.claim(self.token))

    def test_a_task_for_an_application_that_moved_on_is_cancelled_not_handed_out(self):
        skipped = self.job(stage="skipped")
        live = self.job()
        stale = self.queue(skipped)
        self.clock.advance(1)
        self.queue(live)
        got = self.claim(self.token)
        self.assertEqual(got["application"]["job_id"], live["job_id"])
        self.assertEqual(self.tasks().get(stale["id"])["status"], "cancelled")
        self.assertIn("runner_task_cancelled", self.step_codes(skipped))

    def test_each_kind_needs_the_application_in_the_right_stage(self):
        review, preparing = self.job(stage="ready_for_review"), self.job(stage="preparing")
        a, b, c, d = (self.queue(review, "apply"), self.queue(preparing, "submit"), self.queue(self.job(), "refill"),
                      self.queue(self.job(stage="ready_for_review"), "submit"))
        got = []
        while True:
            p = self.claim(self.token)
            if p is None:
                break
            got.append(p["kind"])
        self.assertEqual(sorted(got), ["refill", "submit"])
        self.assertEqual([self.tasks().get(t["id"])["status"] for t in (a, b)], ["cancelled", "cancelled"])

    def test_an_empty_long_poll_waits_the_full_25_seconds_then_answers_none(self):
        start = self.clock()
        self.assertIsNone(self.claim(self.token, wait=MAX_WAIT_SECONDS))
        self.assertEqual(self.clock() - start, 25.0)
        self.assertEqual(sum(self.waits), 25.0)
        self.assertTrue(all(w <= 1.0 for w in self.waits), "it polls, it does not sleep the whole time at once")

    def test_it_never_waits_longer_than_25_seconds_whatever_it_is_told(self):
        for ask in (26, 100, 10 ** 9, float("inf")):
            self.waits.clear()
            start = self.clock()
            self.assertIsNone(self.claim(self.token, wait=ask))
            self.assertEqual(self.clock() - start, 25.0, ask)

    def test_wait_zero_and_negative_do_not_wait(self):
        for ask in (0, -5, 0.0):
            self.waits.clear()
            self.assertIsNone(self.claim(self.token, wait=ask))
            self.assertEqual(self.waits, [])

    def test_a_non_numeric_wait_is_an_error(self):
        for bad in ("soon", None, [], {}):
            with self.assertRaises(RunnerError):
                self.claim(self.token, wait=bad)

    def test_a_task_that_arrives_during_the_wait_is_returned_at_once(self):
        job = self.job()
        self.hooks.append(lambda n: self.queue(job) if n == 3 else None)
        start = self.clock()
        got = self.claim(self.token, wait=25)
        self.assertEqual(got["application"]["job_id"], job["job_id"])
        self.assertEqual(self.clock() - start, 3.0, "returned after three polls, not after 25 seconds")

    def test_a_real_long_poll_is_woken_by_a_new_task_in_another_thread(self):
        job = self.job()
        hub = RunnerHub(1, self.path, clock=time.time, orchestrator_factory=self.orch_factory,
                        profile_provider=lambda u: PROFILE, screenshots_dir=self.shots, root=self.dir / "resumes")
        out = {}

        def poll():
            t0 = time.time()
            out["task"] = hub.claim(self.token["id"], 10)
            out["took"] = time.time() - t0
        with mock.patch("applier.runner_hub.POLL_INTERVAL", 30.0):          # only a wake-up can end the wait early
            thread = threading.Thread(target=poll)
            thread.start()
            time.sleep(0.4)
            RunnerHub(1, self.path).enqueue(job, "apply", {})
            thread.join(15)
        self.assertFalse(thread.is_alive())
        self.assertEqual(out["task"]["application"]["job_id"], job["job_id"])
        self.assertLess(out["took"], 3.0, "woken by the enqueue, not by polling or by the end of the 10 seconds")

    def test_only_one_long_poll_per_token_at_a_time(self):
        seen = []

        def again(n):
            if n == 1:
                try:
                    self.hub().claim(self.token["id"], 25)
                except TooMany as e:
                    seen.append(e)
        self.hooks.append(again)
        self.claim(self.token, wait=3)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].status, 429)
        self.assertIsNone(self.claim(self.token, wait=0), "and the token is not left locked out afterwards")

    def test_another_token_may_poll_at_the_same_time(self):
        other = self.make_token(1, "second")
        ok = []
        self.hooks.append(lambda n: ok.append(self.hub().claim(other["id"], 0)) if n == 1 else None)
        self.claim(self.token, wait=2)
        self.assertEqual(ok, [None])

    def test_a_waiting_runner_keeps_its_token_marked_as_seen(self):
        t0 = self.clock()
        self.claim(self.token, wait=25)
        self.assertGreaterEqual(self.raw_token_row(self.token["id"])["last_seen_at"], t0 + 20)

    def test_a_poll_by_a_revoked_token_cannot_be_authenticated_in_the_first_place(self):
        self.tokens().revoke(self.token["id"])
        self.assertIsNone(self.auth(self.token))


class EventTests(HubBase):
    def setUp(self):
        super().setUp()
        self.token = self.make_token()
        self.job_ = self.job()
        self.queue(self.job_)
        self.task = self.claim(self.token)
        self.tid = self.task["task_id"]

    def send(self, events, token=None, tid=None, uid=1):
        return self.hub(uid).events((token or self.token)["id"], tid or self.tid, events)

    def step(self, code="opened_page", message="Opened the job page", **extra):
        return {"event": "step", "data": {"step": code, "message": message, **extra}}

    def test_steps_land_on_the_timeline_marked_as_the_runners(self):
        self.send([self.step("opened_page", "Opened the job page"), self.step("filled_fields", "Filled 5 fields", count=5)])
        self.assertEqual([c for c in self.step_codes(self.job_) if c in ("opened_page", "filled_fields")], ["opened_page", "filled_fields"])
        e = next(e for e in self.events(self.job_) if e["detail"].get("step") == "filled_fields")
        self.assertEqual((e["detail"]["message"], e["detail"]["count"], e["detail"]["via"], e["detail"]["task"]),
                         ("Filled 5 fields", 5, "runner", self.tid[:8]))

    def test_the_site_type_is_kept_with_the_session(self):
        self.send([{"event": "site_type", "data": "lever"}])
        self.assertEqual(ApplicationSession.load(self.job_["job_id"]).site_type, "lever") if ApplicationSession.load(self.job_["job_id"]) else None
        s = ApplicationSession(job_id=self.job_["job_id"], job_url="u", job_title="t", company="c", source="s")
        s.save()
        self.send([{"event": "site_type", "data": "ashby"}])
        self.assertEqual(ApplicationSession.load(self.job_["job_id"]).site_type, "ashby")

    def test_an_event_renews_the_lease(self):
        self.clock.advance(100)
        out = self.send([self.step()])
        self.assertEqual(out, {"accepted": 1, "lease_seconds": LEASE_SECONDS})
        self.assertEqual(self.tasks().get(self.tid)["lease_until"], self.clock() + LEASE_SECONDS)

    def test_an_empty_batch_is_a_heartbeat(self):
        self.clock.advance(100)
        self.assertEqual(self.send([])["accepted"], 0)
        self.assertEqual(self.tasks().get(self.tid)["lease_until"], self.clock() + LEASE_SECONDS)

    def test_bad_events_are_refused_and_nothing_is_recorded(self):
        before = len(self.events(self.job_))
        bad = [5, "x", {}, {"event": "log", "data": {}}, {"event": "step"}, {"event": "step", "data": "x"},
               {"event": "step", "data": {"message": "no code"}}, self.step("Bad Code"), self.step("9starts_with_digit"),
               self.step("x" * 41), self.step("ok", ""), self.step("ok", "m" * 301), self.step("ok", 5),
               {"event": "site_type", "data": "monster"}, {"event": "site_type", "data": None}]
        for event in bad:
            with self.assertRaises(RunnerError, msg=str(event)[:50]):
                self.send([event])
        self.assertEqual(len(self.events(self.job_)), before)

    def test_one_bad_event_rejects_the_whole_batch(self):
        before = len(self.events(self.job_))
        with self.assertRaises(RunnerError):
            self.send([self.step("good_one"), {"event": "nope"}])
        self.assertEqual(len(self.events(self.job_)), before)
        self.assertEqual(self.tasks().get(self.tid)["event_count"], 0)

    def test_extra_data_keeps_only_small_scalars(self):
        self.send([self.step("opened_page", "x", count=3, ok=True, nothing=None, nested={"a": 1}, listy=[1],
                             long="y" * 201, Bad_Key=1)])
        d = next(e for e in self.events(self.job_) if e["detail"].get("step") == "opened_page")["detail"]
        self.assertEqual((d["count"], d["ok"], d["nothing"]), (3, True, None))
        for dropped in ("nested", "listy", "long", "Bad_Key"):
            self.assertNotIn(dropped, d)

    def test_batch_and_task_limits(self):
        with self.assertRaises(RunnerError):
            self.send([self.step()] * 51)
        for _ in range(6):
            self.send([self.step()] * 50)
        with self.assertRaises(TooMany):
            self.send([self.step()])

    def test_a_task_you_do_not_hold_is_a_conflict_and_another_users_is_not_found(self):
        other = self.make_token(1, "other laptop")
        with self.assertRaises(Conflict):
            self.send([self.step()], token=other)
        with self.assertRaises(NotFound):
            self.send([self.step()], tid="0" * 32)
        bob = self.make_token(2, "bob")
        with self.assertRaises(NotFound) as cm:
            self.send([self.step()], token=bob, uid=2)
        self.assertEqual(cm.exception.status, 404, "indistinguishable from a task that does not exist")

    def test_events_after_the_task_was_requeued_are_refused(self):
        self.clock.advance(LEASE_SECONDS + 1)
        self.hub().sweep()
        with self.assertRaises(Conflict):
            self.send([self.step()])

    def test_clean_event_is_pure(self):
        self.assertEqual(clean_event({"event": "site_type", "data": "workday"}), {"event": "site_type", "data": "workday"})
        self.assertEqual(clean_event(self.step("a_b", "  hi  ")), {"event": "step", "data": {"step": "a_b", "message": "hi"}})


class ScreenshotTests(HubBase):
    def setUp(self):
        super().setUp()
        self.token = self.make_token()
        self.job_ = self.job()
        self.queue(self.job_)
        self.tid = self.claim(self.token)["task_id"]

    def shoot(self, data, label="", token=None, tid=None, uid=1):
        return self.hub(uid).screenshot((token or self.token)["id"], tid or self.tid, data, label)

    def test_a_valid_png_is_stored_under_a_name_the_server_makes(self):
        png = make_png(7, 5)
        out = self.shoot(png, "pre_submit")
        self.assertRegex(out["screenshot"], rf"^{re.sub('[^A-Za-z0-9]', '', self.job_['job_id'])}_runner_pre_submit_[0-9a-f]{{8}}\.png$")
        self.assertEqual((out["width"], out["height"], out["count"]), (7, 5, 1))
        self.assertEqual((self.shots / out["screenshot"]).read_bytes(), png)
        self.assertEqual(self.tasks().get(self.tid)["screenshots"], [str(self.shots / out["screenshot"])])

    def test_the_label_can_never_steer_the_path(self):
        for label in ("../../etc/passwd", "..\\..\\x", "/etc/shadow", "a/b", "x" * 100, "Pre Submit!", "\x00evil", "", None, 5):
            out = self.shoot(make_png(), label)
            name = out["screenshot"]
            self.assertRegex(name, r"^[A-Za-z0-9]+_runner_[a-z0-9_]{1,24}_[0-9a-f]{8}\.png$", repr(label))
            self.assertEqual((self.shots / name).resolve().parent, self.shots.resolve())
        self.assertEqual({p.parent for p in self.shots.rglob("*")}, {self.shots})

    def test_two_uploads_never_share_a_name(self):
        names = {self.shoot(make_png(), "same")["screenshot"] for _ in range(5)}
        self.assertEqual(len(names), 5)

    def test_it_must_be_a_png_by_its_bytes(self):
        jpeg = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        gif = b"GIF89a" + b"\x00" * 100
        no_ihdr = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0dIDAT" + b"\x00" * 40
        short = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR"
        for bad in (b"", jpeg, gif, b"<html>", b"%PDF-1.4" + b"0" * 100, no_ihdr, short, b"\x89PNG\r\n\x1a" + b"0" * 100,
                    b"x" + make_png(), None, "a string", 5):
            with self.assertRaises(RunnerError, msg=repr(bad)[:40]):
                self.shoot(bad)
        self.assertEqual(list(self.shots.glob("*")) if self.shots.exists() else [], [], "nothing stored for a rejected upload")

    def test_the_signature_alone_is_enough_to_refuse(self):
        png = bytearray(make_png())
        png[0] = 0x88                                   # a perfectly formed PNG but for its first byte
        with self.assertRaises(RunnerError) as cm:
            self.shoot(bytes(png))
        self.assertIn("signature", str(cm.exception))
        png = bytearray(make_png())
        png[7] = 0x0b
        with self.assertRaises(RunnerError):
            self.shoot(bytes(png))

    def test_a_missing_ihdr_alone_is_enough_to_refuse(self):
        sane = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\x0d" + b"IDAT" + (4).to_bytes(4, "big") + (3).to_bytes(4, "big")
                + b"\x08\x02\x00\x00\x00" + b"\x00" * 8)
        self.assertGreaterEqual(len(sane), 33)
        with self.assertRaises(RunnerError) as cm:
            self.shoot(sane)
        self.assertIn("IHDR", str(cm.exception))
        wrong_length = bytearray(make_png())
        wrong_length[8:12] = (14).to_bytes(4, "big")
        with self.assertRaises(RunnerError):
            self.shoot(bytes(wrong_length))

    def test_the_size_limit_is_3_mb_inclusive(self):
        ok = make_png(pad_to=MAX_PNG_BYTES)
        self.assertEqual(len(ok), MAX_PNG_BYTES)
        self.shoot(ok)
        with self.assertRaises(TooLarge) as cm:
            self.shoot(make_png(pad_to=MAX_PNG_BYTES + 1))
        self.assertEqual(cm.exception.status, 413)

    def test_impossible_dimensions_are_refused(self):
        for w, h in ((0, 5), (5, 0), (20001, 5), (5, 20001)):
            png = bytearray(make_png())
            png[16:20], png[20:24] = w.to_bytes(4, "big"), h.to_bytes(4, "big")
            with self.assertRaises(RunnerError, msg=(w, h)):
                self.shoot(bytes(png))
        self.shoot(bytes(bytearray(make_png())[:16] + (20000).to_bytes(4, "big") + (20000).to_bytes(4, "big") + bytes(make_png())[24:]))

    def test_validate_png_is_pure(self):
        self.assertEqual(validate_png(make_png(9, 4)), (9, 4))
        self.assertEqual(validate_png(bytearray(make_png(2, 2))), (2, 2))

    def test_at_most_20_screenshots_per_task(self):
        for _ in range(20):
            self.shoot(make_png())
        with self.assertRaises(TooMany):
            self.shoot(make_png())

    def test_an_upload_renews_the_lease(self):
        self.clock.advance(100)
        self.shoot(make_png())
        self.assertEqual(self.tasks().get(self.tid)["lease_until"], self.clock() + LEASE_SECONDS)

    def test_you_can_only_upload_to_a_task_you_hold(self):
        other = self.make_token(1, "other")
        with self.assertRaises(Conflict):
            self.shoot(make_png(), token=other)
        with self.assertRaises(NotFound):
            self.shoot(make_png(), tid="f" * 32)
        bob = self.make_token(2, "bob")
        with self.assertRaises(NotFound):
            self.shoot(make_png(), token=bob, uid=2)


class ResultTests(HubBase):
    def setUp(self):
        super().setUp()
        self.token = self.make_token()

    def run_task(self, kind="apply", stage="preparing", params=None):
        job = self.job(stage=stage)
        self.queue(job, kind, params)
        task = self.claim(self.token)
        return job, task["task_id"]

    def report(self, tid, **body):
        body.setdefault("success", True)
        return self.hub().result(self.token["id"], tid, body)

    def test_a_first_pass_with_nothing_missing_goes_to_review(self):
        job, tid = self.run_task()
        out = self.report(tid, review={"fields": [{"label": "Email", "field_key": "email", "value": "a@b.co"}], "url": "https://x/apply"})
        self.assertEqual(out, {"status": "accepted", "application": "ready_for_review"})
        self.assertEqual(self.stage(job), "ready_for_review")
        self.assertEqual(self.tasks().get(tid)["status"], "done")

    def test_a_first_pass_that_could_not_answer_something_asks_you(self):
        job, tid = self.run_task()
        self.report(tid, missing_fields=[{"label": "Why us?", "field_key": "why_us", "field_type": "textarea",
                                          "options": [], "placeholder": "Tell us"}])
        self.assertEqual(self.stage(job), "needs_you")
        self.assertEqual(self.db().get_by_id(job["job_id"])["blocked_reason"], "question")
        (flag,) = self.profile.flagged
        self.assertEqual((flag["field_label"], flag["field_key"], flag["field_type"], flag["placeholder"], flag["job_id"]),
                         ("Why us?", "why_us", "textarea", "Tell us", job["job_id"]))

    def test_a_failure_the_runner_reports_fails_the_application_like_a_local_one(self):
        job, tid = self.run_task()
        self.report(tid, success=False, error="CAPTCHA detected — skipping")
        self.assertEqual((self.stage(job), self.db().get_by_id(job["job_id"])["blocked_reason"]), ("needs_you", "captcha"))

    def test_a_failure_with_no_message_still_fails_it(self):
        job, tid = self.run_task()
        self.report(tid, success=False)
        self.assertIn(self.stage(job), ("failed", "needs_you"))

    def test_a_failure_reported_on_the_second_pass_fails_it_too_instead_of_reaching_review(self):
        job, tid = self.run_task("refill")
        self.report(tid, success=False, error="CAPTCHA detected — skipping")
        self.assertEqual((self.stage(job), self.db().get_by_id(job["job_id"])["blocked_reason"]), ("needs_you", "captcha"))

    def test_the_second_pass_goes_back_to_review(self):
        job, tid = self.run_task("refill")
        self.assertEqual(self.report(tid)["application"], "ready_for_review")

    def test_a_submit_that_worked_is_submitted(self):
        job, tid = self.run_task("submit", "ready_for_review", {"corrections": None, "dry_run": False})
        out = self.report(tid)
        self.assertEqual((out["application"], self.db().get_by_id(job["job_id"])["status"]), ("submitted", "applied"))

    def test_a_dry_run_submit_leaves_the_application_in_review(self):
        job, tid = self.run_task("submit", "ready_for_review", {"corrections": None, "dry_run": True})
        self.report(tid)
        self.assertEqual(self.stage(job), "ready_for_review")
        self.assertEqual(self.db().get_by_id(job["job_id"])["applied_at"] or "", "")

    def test_a_dry_run_flag_in_the_report_wins_over_the_task(self):
        job, tid = self.run_task("submit", "ready_for_review", {"corrections": None, "dry_run": False})
        self.report(tid, dry_run=True)
        self.assertEqual(self.stage(job), "ready_for_review")

    def test_a_submit_that_the_site_rejected_is_not_submitted(self):
        job, tid = self.run_task("submit", "ready_for_review", {"corrections": None, "dry_run": False})
        self.report(tid, success=False, error="Validation error — the site rejected the answers: Phone is required")
        self.assertEqual((self.stage(job), self.db().get_by_id(job["job_id"])["blocked_reason"]), ("needs_you", "validation_error"))

    def test_only_screenshots_this_task_uploaded_are_ever_recorded(self):
        job, tid = self.run_task()
        mine = self.hub().screenshot(self.token["id"], tid, make_png(), "form")["screenshot"]
        self.report(tid, screenshots=["/etc/passwd", "../../secret.png", str(self.dir / "other.png")])
        recorded = ApplicationSession.load(job["job_id"]).screenshots
        self.assertEqual(recorded, [str(self.shots / mine)])

    def test_a_task_is_closed_by_the_first_result(self):
        job, tid = self.run_task()
        self.report(tid)
        with self.assertRaises(Conflict):
            self.report(tid)
        self.assertEqual(self.stage(job), "ready_for_review", "and the second report changed nothing")

    def test_only_the_runner_holding_the_task_may_report(self):
        job, tid = self.run_task()
        other = self.make_token(1, "other")
        with self.assertRaises(Conflict):
            self.hub().result(other["id"], tid, {"success": True})
        bob = self.make_token(2, "bob")
        with self.assertRaises(NotFound):
            self.hub(2).result(bob["id"], tid, {"success": True})
        self.assertEqual((self.stage(job), self.tasks().get(tid)["status"]), ("preparing", "claimed"))

    def test_an_invalid_report_is_refused_and_leaves_the_task_claimed(self):
        job, tid = self.run_task()
        bad = [None, [], "ok", {}, {"success": "yes"}, {"success": 1}, {"success": True, "error": 5},
               {"success": True, "error": "e" * 1001}, {"success": True, "missing_fields": "x"},
               {"success": True, "missing_fields": [{"label": "L"}] * 51},
               {"success": True, "missing_fields": [5]}, {"success": True, "missing_fields": [{"field_key": "k"}]},
               {"success": True, "missing_fields": [{"label": "L", "field_key": "k", "options": "abc"}]},
               {"success": True, "missing_fields": [{"label": "L", "field_key": "k", "options": [1]}]},
               {"success": True, "missing_fields": [{"label": "L" * 301, "field_key": "k"}]},
               {"success": True, "review": {"fields": "x"}}, {"success": True, "review": {"fields": [{}] * 301}}]
        for body in bad:
            with self.assertRaises(RunnerError, msg=str(body)[:60]):
                self.hub().result(self.token["id"], tid, body)
        self.assertEqual((self.tasks().get(tid)["status"], self.stage(job)), ("claimed", "preparing"))
        self.report(tid)                                       # and a good report still works afterwards

    def test_a_duplicate_report_that_races_the_first_is_refused_and_changes_nothing(self):
        job, tid = self.run_task()
        with mock.patch.object(RunnerTaskStore, "complete", return_value=False), \
             mock.patch("applier.session.Orchestrator.finish_pass") as finish:
            with self.assertRaises(Conflict):
                self.report(tid)
        finish.assert_not_called()
        self.assertEqual(self.stage(job), "preparing")

    def test_clean_result_builds_what_the_orchestrator_expects(self):
        task = {"params": {"dry_run": True}}
        r = clean_result({"success": True, "missing_fields": [{"label": " Why? ", "field_key": "why", "options": ["a", "b"]}],
                          "review": {"fields": [{"label": "Email", "value": "x", "hit": {"source": "profile", "junk": 1}, "junk": 2}],
                                     "url": "https://x"}, "screenshots": ["/etc/passwd"]}, task, ["/mine.png"])
        self.assertEqual(r["screenshots"], ["/mine.png"])
        self.assertEqual(r["dry_run"], True)
        self.assertEqual(r["missing_fields"], [MissingField("Why?", "why", "text", ["a", "b"], "")])
        self.assertEqual(r["review"]["fields"], [{"label": "Email", "value": "x", "hit": {"source": "profile"}}])
        self.assertEqual((r["success"], r["error"], r["needs_review"]), (True, "", True))

    def test_a_result_for_an_application_that_is_gone_is_accepted(self):
        job, tid = self.run_task()
        with self.db()._conn() as c:
            c.execute("DELETE FROM jobs WHERE job_id=?", (job["job_id"],))
        self.assertEqual(self.report(tid), {"status": "accepted", "application": "gone"})

    def test_if_continuing_the_application_blows_up_it_is_failed_not_lost(self):
        job, tid = self.run_task()
        with mock.patch("applier.session.Orchestrator.finish_pass", side_effect=RuntimeError("kaboom")), \
             self.assertLogs("applier.runner_hub", "ERROR"):
            out = self.report(tid)
        self.assertEqual(out["status"], "accepted")
        self.assertIn(self.stage(job), ("failed", "needs_you"))
        self.assertEqual(self.tasks().get(tid)["status"], "done")


class ResumeDownloadTests(HubBase):
    def setUp(self):
        super().setUp()
        self.token = self.make_token()
        self.job_ = self.job()
        v = ResumeStore(1, self.path, self.dir / "resumes").add("Priya_Backend.pdf", PDF, "Backend")
        KitStore(1, self.path).add_version(self.job_["job_id"], variant_id=v["id"], variant_label="Backend",
                                           selection="default", summary="S.", cover_letter="C.", status="ready")
        self.queue(self.job_)
        self.tid = self.claim(self.token)["task_id"]

    def test_the_chosen_resume_is_served_decrypted(self):
        name, data = self.hub().resume(self.token["id"], self.tid)
        self.assertEqual((name, data), ("Priya_Backend.pdf", PDF))

    def test_what_is_advertised_is_what_is_served(self):
        advertised = self.hub().payload(self.tasks().get(self.tid))["resume"]
        _, data = self.hub().resume(self.token["id"], self.tid)
        self.assertEqual((advertised["size"], advertised["sha256"]), (len(data), hashlib.sha256(data).hexdigest()))

    def test_it_is_not_stored_in_plaintext_anywhere_it_comes_from(self):
        for f in (self.dir / "resumes").rglob("*"):
            if f.is_file():
                self.assertNotIn(b"%PDF", f.read_bytes())

    def test_a_download_renews_the_lease(self):
        self.clock.advance(100)
        self.hub().resume(self.token["id"], self.tid)
        self.assertEqual(self.tasks().get(self.tid)["lease_until"], self.clock() + LEASE_SECONDS)

    def test_only_the_holder_of_the_task_can_download(self):
        other = self.make_token(1, "other")
        with self.assertRaises(Conflict):
            self.hub().resume(other["id"], self.tid)
        with self.assertRaises(NotFound):
            self.hub().resume(self.token["id"], "e" * 32)
        bob = self.make_token(2, "bob")
        with self.assertRaises(NotFound):
            self.hub(2).resume(bob["id"], self.tid)

    def test_not_after_the_task_is_over(self):
        self.hub().result(self.token["id"], self.tid, {"success": True})
        with self.assertRaises(Conflict):
            self.hub().resume(self.token["id"], self.tid)

    def test_an_application_with_no_resume_is_not_found(self):
        ResumeStore(1, self.path, self.dir / "resumes").delete(ResumeStore(1, self.path, self.dir / "resumes").list()[0]["id"])
        job = self.job()
        self.hub().result(self.token["id"], self.tid, {"success": True})
        self.queue(job)
        tid = self.claim(self.token)["task_id"]
        with self.assertRaises(NotFound):
            self.hub().resume(self.token["id"], tid)


class LeaseTests(HubBase):
    """A runner that disappears: re-queue, expire - and never run a submit twice."""

    def setUp(self):
        super().setUp()
        self.token = self.make_token()
        self.other = self.make_token(1, "second laptop")

    def lapse(self, seconds=LEASE_SECONDS + 1):
        self.clock.advance(seconds)
        return self.hub().sweep()

    def test_a_silent_runner_loses_the_task_which_is_requeued(self):
        job = self.job()
        task = self.queue(job)
        self.claim(self.token)
        (action,) = self.lapse()
        self.assertEqual(action["action"], "requeued")
        row = self.tasks().get(task["id"])
        self.assertEqual((row["status"], row["claimed_by"], row["attempts"], row["lease_until"]), ("queued", None, 1, 0))
        self.assertIn("runner_lost", self.step_codes(job))

    def test_another_runner_can_then_take_it_and_the_attempt_counts(self):
        job = self.job()
        self.queue(job)
        self.claim(self.token)
        self.lapse()
        again = self.claim(self.other)
        self.assertEqual((again["application"]["job_id"], again["attempt"]), (job["job_id"], 2))

    def test_the_first_runner_coming_back_late_is_told_the_task_is_gone(self):
        job = self.job()
        tid = self.queue(job)["id"]
        self.claim(self.token)
        self.lapse()
        self.claim(self.other)
        for call in (lambda: self.hub().events(self.token["id"], tid, []),
                     lambda: self.hub().screenshot(self.token["id"], tid, make_png()),
                     lambda: self.hub().result(self.token["id"], tid, {"success": True}),
                     lambda: self.hub().resume(self.token["id"], tid)):
            with self.assertRaises(Conflict):
                call()

    def test_activity_keeps_the_lease_alive(self):
        job = self.job()
        tid = self.queue(job)["id"]
        self.claim(self.token)
        for _ in range(5):
            self.clock.advance(LEASE_SECONDS - 10)
            self.hub().events(self.token["id"], tid, [])
            self.assertEqual(self.hub().sweep(), [])
        self.assertEqual(self.tasks().get(tid)["status"], "claimed")

    def test_a_lease_that_has_not_lapsed_is_left_alone(self):
        self.queue(self.job())
        self.claim(self.token)
        self.assertEqual(self.lapse(LEASE_SECONDS), [], "exactly at the deadline it is still alive")

    def test_a_revoked_runner_that_goes_quiet_loses_its_task_like_any_other(self):
        job = self.job()
        self.queue(job)
        self.claim(self.token)
        self.tokens().revoke(self.token["id"], now=self.clock())
        (action,) = self.lapse()
        self.assertEqual(action["action"], "requeued")
        self.assertIsNotNone(self.claim(self.other))

    def test_after_three_lost_runners_the_application_fails_as_a_timeout(self):
        job = self.job()
        tid = self.queue(job)["id"]
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.assertEqual(self.claim(self.token)["attempt"], attempt)
            (action,) = self.lapse()
        self.assertEqual((action["action"], action["reason"]), ("expired", "attempts"))
        self.assertEqual(self.tasks().get(tid)["status"], "expired")
        self.assertEqual(self.stage(job), "failed")
        self.assertIn("runner_task_expired", self.step_codes(job))
        failure = [e for e in self.events(job) if e["kind"] == "transition" and e["to_stage"] == "failed"][-1]
        self.assertEqual(failure["detail"].get("classified"), "timeout", "retryable, like any other timeout")
        self.assertIsNone(self.claim(self.token), "and it is not handed out again")

    def test_a_task_nobody_picks_up_expires_and_fails_the_application(self):
        job = self.job()
        tid = self.queue(job)["id"]
        self.assertEqual(self.hub().sweep(), [])
        (action,) = self.lapse(QUEUE_TTL_SECONDS + 1)
        self.assertEqual((action["action"], action["reason"]), ("expired", "queue_ttl"))
        self.assertEqual((self.tasks().get(tid)["status"], self.stage(job)), ("expired", "failed"))

    def test_a_requeued_task_gets_a_fresh_queue_deadline(self):
        job = self.job()
        tid = self.queue(job)["id"]
        self.clock.advance(QUEUE_TTL_SECONDS - 10)
        self.claim(self.token)
        self.lapse()
        self.assertEqual(self.tasks().get(tid)["status"], "queued", "not expired just because the original deadline is near")
        self.assertGreater(self.tasks().get(tid)["expires_at"], self.clock() + QUEUE_TTL_SECONDS - 5)

    def test_a_claimed_submit_that_goes_silent_is_NEVER_rerun(self):
        job = self.job(stage="ready_for_review")
        tid = self.queue(job, "submit", {"corrections": None, "dry_run": False})["id"]
        self.claim(self.token)
        (action,) = self.lapse()
        self.assertEqual(action["action"], "lost_submit")
        self.assertEqual(self.tasks().get(tid)["status"], "expired")
        self.assertIsNone(self.claim(self.other), "nothing is queued: it will not be handed to anyone")
        self.assertIsNone(self.claim(self.token))

    def test_a_lost_submit_asks_you_to_check_the_site(self):
        job = self.job(stage="ready_for_review")
        self.queue(job, "submit", {"corrections": None, "dry_run": False})
        self.claim(self.token)
        self.lapse()
        self.assertEqual(self.stage(job), "needs_you")
        row = self.db().get_by_id(job["job_id"])
        self.assertEqual(row["blocked_reason"], "other")
        e = [e for e in self.events(job) if e["kind"] == "transition" and e["to_stage"] == "needs_you"][-1]
        self.assertEqual((e["actor"], e["detail"]["lost_submit"], e["detail"]["via"]), ("system", True, "runner"))
        self.assertIn("check on the site", e["detail"]["note"])
        self.assertIn("runner_lost_submit", self.step_codes(job))

    def test_a_submit_nobody_claimed_just_expires_and_stays_in_review(self):
        job = self.job(stage="ready_for_review")
        tid = self.queue(job, "submit", {"corrections": None, "dry_run": False})["id"]
        (action,) = self.lapse(QUEUE_TTL_SECONDS + 1)
        self.assertEqual((action["action"], self.tasks().get(tid)["status"]), ("expired", "expired"))
        self.assertEqual(self.stage(job), "ready_for_review", "nothing was sent, so you can approve it again")
        self.assertIn("runner_task_expired", self.step_codes(job))

    def test_sweeping_twice_acts_once(self):
        job = self.job()
        self.queue(job)
        self.claim(self.token)
        self.assertEqual(len(self.lapse()), 1)
        self.assertEqual(self.hub().sweep(), [])
        self.assertEqual(self.step_codes(job).count("runner_lost"), 1)

    def test_a_sweep_only_touches_its_own_users_tasks(self):
        mine, theirs = self.job(uid=1), self.job(uid=2)
        bob = self.make_token(2, "bob")
        self.queue(mine)
        self.queue(theirs, uid=2)
        self.claim(self.token)
        self.claim(bob, uid=2)
        self.clock.advance(LEASE_SECONDS + 1)
        self.assertEqual(len(self.hub(2).sweep()), 1)
        self.assertEqual([t["status"] for t in self.tasks(1).list()], ["claimed"], "Alice's lapsed task is not Bob's to sweep")
        self.assertEqual([t["status"] for t in self.tasks(2).list()], ["queued"])

    def test_a_runner_polling_also_sweeps(self):
        job = self.job()
        self.queue(job)
        self.claim(self.token)
        self.clock.advance(LEASE_SECONDS + 1)
        got = self.claim(self.other)
        self.assertEqual((got["application"]["job_id"], got["attempt"]), (job["job_id"], 2),
                         "one poll noticed the silent runner, re-queued the task and handed it over")

    def test_a_task_that_is_done_is_never_swept(self):
        job = self.job()
        tid = self.queue(job)["id"]
        self.claim(self.token)
        self.hub().result(self.token["id"], tid, {"success": True})
        self.assertEqual(self.lapse(QUEUE_TTL_SECONDS * 2), [])
        self.assertEqual(self.tasks().get(tid)["status"], "done")

    def test_one_open_task_per_application(self):
        job = self.job()
        first, created = self.hub().enqueue(job, "apply", {})
        again, created2 = self.hub().enqueue(job, "apply", {})
        self.assertEqual((created, created2, again["id"]), (True, False, first["id"]))
        self.claim(self.token)
        third, created3 = self.hub().enqueue(job, "apply", {})
        self.assertEqual((created3, third["id"]), (False, first["id"]), "also while it is being worked on")
        self.assertEqual(len(self.tasks().list()), 1)

    def test_a_new_task_is_allowed_once_the_old_one_is_over(self):
        job = self.job()
        tid = self.queue(job)["id"]
        self.claim(self.token)
        self.hub().result(self.token["id"], tid, {"success": True})
        _, created = self.hub().enqueue(job, "refill", {"missing_keys": []})
        self.assertTrue(created)


# ── the Orchestrator leaves the pass for a runner ─────────────────

class Ctx:
    pages = []
    def new_page(self): return object()
    def close(self): pass


class WorkingBrowser:
    def __init__(self, log): self.log = log
    def __enter__(self):
        self.log.append("sync_playwright")
        return object()
    def __exit__(self, *a): return False


class RunnerModeTests(HubBase):
    """The same Orchestrator, the same lifecycle - but the browser work is a task."""

    def setUp(self):
        super().setUp()
        self.settings = Settings(executor="runner", quality_first="false", daily_apply_limit=0, min_delay_between_applies=0)
        for p in (mock.patch("core.llm.chat", return_value="One. Two. Three."),
                  mock.patch("core.llm.chat_json", return_value={"variant_id": 0})):
            p.start()
            self.addCleanup(p.stop)
        self.token = self.make_token()
        self.job_ = self.job(url="https://boards.greenhouse.io/acme/jobs/777")

    def orch(self, job=None):
        o = self.orch_factory(job or self.job_)
        o._open_browser = lambda pw: Ctx()
        return o

    def open_tasks(self):
        return [t for t in self.tasks().list() if t["status"] in ("queued", "claimed")]

    def test_the_first_pass_is_left_as_a_task_and_no_browser_is_opened(self):
        self.orch().run_iter1()
        self.assertEqual(self.browser, [])
        (task,) = self.open_tasks()
        self.assertEqual((task["kind"], task["params"], task["job_id"], task["status"]), ("apply", {}, self.job_["job_id"], "queued"))
        self.assertEqual(self.stage(self.job_), "preparing", "the application waits where it is")
        self.assertIn("runner_queued", self.step_codes(self.job_))

    def test_it_is_not_failed_nor_reviewed_while_it_waits(self):
        self.orch().run_iter1()
        self.assertEqual([e["to_stage"] for e in self.events(self.job_) if e["kind"] == "transition"][-1], "preparing")
        self.assertNotIn("failed", [e["to_stage"] for e in self.events(self.job_) if e["kind"] == "transition"])

    def test_the_kit_is_prepared_before_the_pass_is_left(self):
        self.orch().run_iter1()
        kit = KitStore(1, self.path).current(self.job_["job_id"])
        self.assertEqual((kit["status"], kit["summary"]), ("ready", "One. Two. Three."))
        payload = self.claim(self.token)
        self.assertEqual(payload["kit"]["summary"], "One. Two. Three.", "and the runner gets it")

    def test_a_runners_whole_run_completes_the_application(self):
        self.orch().run_iter1()
        payload = self.claim(self.token)
        tid, hub = payload["task_id"], self.hub()
        hub.events(self.token["id"], tid, [{"event": "site_type", "data": "greenhouse"},
                                           {"event": "step", "data": {"step": "opened_page", "message": "Opened the job page"}},
                                           {"event": "step", "data": {"step": "filled_fields", "message": "Filled 6 fields"}}])
        shot = hub.screenshot(self.token["id"], tid, make_png(), "pre_submit")["screenshot"]
        out = hub.result(self.token["id"], tid, {"success": True, "review": {"fields": [], "url": "https://x/apply"}})
        self.assertEqual(out, {"status": "accepted", "application": "ready_for_review"})
        self.assertEqual(self.stage(self.job_), "ready_for_review")
        self.assertEqual(ApplicationSession.load(self.job_["job_id"]).screenshots, [str(self.shots / shot)])
        codes = self.step_codes(self.job_)
        self.assertEqual([c for c in codes if c in ("runner_queued", "opened_page", "filled_fields")],
                         ["runner_queued", "opened_page", "filled_fields"])
        self.assertEqual(self.browser, [], "not once did this machine open a browser")

    def test_the_second_pass_and_the_submit_go_through_the_runner_too(self):
        o = self.orch()
        o.run_iter1()
        tid = self.claim(self.token)["task_id"]
        self.hub().result(self.token["id"], tid, {"success": True, "missing_fields": [
            {"label": "Why us?", "field_key": "why_us", "field_type": "textarea"}]})
        self.assertEqual(self.stage(self.job_), "needs_you")
        o2 = self.orch()
        o2.run_iter2({"why_us": "Robots."})
        self.assertEqual(self.stage(self.job_), "preparing")
        refill = self.claim(self.token)
        self.assertEqual((refill["kind"], refill["application"]["params"]), ("refill", {"missing_keys": ["why_us"]}))
        self.assertTrue(any("Robots." in v for v in refill["answers"]["application"].values()), "your answer goes to the runner")
        self.hub().result(self.token["id"], refill["task_id"], {"success": True})
        self.assertEqual(self.stage(self.job_), "ready_for_review")
        self.orch().submit()
        submit = self.claim(self.token)
        self.assertEqual((submit["kind"], submit["application"]["params"]), ("submit", {"corrections": None, "dry_run": False}))
        self.assertEqual(self.stage(self.job_), "ready_for_review", "waiting for the runner to click Submit")
        self.hub().result(self.token["id"], submit["task_id"], {"success": True})
        self.assertEqual(self.db().get_by_id(self.job_["job_id"])["status"], "applied")
        self.assertEqual(self.browser, [])

    def test_a_dry_run_is_passed_to_the_runner(self):
        self.job_ = self.job(stage="ready_for_review", url="https://boards.greenhouse.io/acme/jobs/778")
        self.settings = Settings(executor="runner", dry_run="true", quality_first="false")
        self.orch(self.job_).submit()
        self.assertEqual(self.claim(self.token)["application"]["params"]["dry_run"], True)

    def test_asking_twice_does_not_queue_twice(self):
        self.orch().run_iter1()
        self.orch().run_iter1()
        self.assertEqual(len(self.open_tasks()), 1)
        self.assertIn("runner_already_queued", self.step_codes(self.job_))
        self.assertEqual(self.browser, [])

    def test_a_failure_the_runner_reports_is_handled_like_a_local_one(self):
        self.orch().run_iter1()
        tid = self.claim(self.token)["task_id"]
        self.hub().result(self.token["id"], tid, {"success": False, "error": "Login wall — page requires authentication"})
        self.assertEqual((self.stage(self.job_), self.db().get_by_id(self.job_["job_id"])["blocked_reason"]),
                         ("needs_you", "login_required"))

    def test_the_server_executor_is_still_the_default_and_opens_the_browser_here(self):
        self.settings = Settings(quality_first="false", daily_apply_limit=0)          # no executor setting at all
        job = self.job(url="https://boards.greenhouse.io/acme/jobs/900")
        o = self.orch(job)
        with mock.patch("playwright.sync_api.sync_playwright", lambda: WorkingBrowser(self.browser)), \
             mock.patch("applier.adapters.runner.run_application", return_value={
                 "success": True, "screenshots": [], "missing_fields": [], "error": "", "review": {"fields": [], "url": "u"}}):
            o.run_iter1()
        self.assertEqual(self.browser, ["sync_playwright"])
        self.assertEqual(self.stage(job), "ready_for_review")
        self.assertEqual(self.tasks().list(), [], "and no task was left for anyone")

    def test_auto_uses_a_runner_only_while_one_is_connected(self):
        self.settings = Settings(executor="auto", quality_first="false", daily_apply_limit=0)
        lonely = self.job(url="https://boards.greenhouse.io/acme/jobs/901")
        self.orch(lonely).run_iter1()
        self.assertEqual(self.browser, ["sync_playwright"], "no runner seen: the server tried to open its browser")
        self.assertEqual(self.tasks().list(), [])
        self.browser.clear()
        runner_job = self.job(url="https://boards.greenhouse.io/acme/jobs/902")
        authenticate(self.token["token"], self.path, now=time.time(), users=self.users)           # the runner connects
        self.orch(runner_job).run_iter1()
        self.assertEqual(self.browser, [])
        self.assertEqual([t["job_id"] for t in self.tasks().list()], [runner_job["job_id"]])

    def test_assist_mode_wins_over_the_runner(self):
        job = self.job(url="https://www.linkedin.com/jobs/view/3700000001")
        self.orch(job).run_iter1()
        self.assertEqual((self.stage(job), self.tasks().list(), self.browser), ("ready_for_review", [], []))

    def test_a_runner_that_never_finishes_ends_in_a_failed_application(self):
        self.orch().run_iter1()
        self.claim(self.token)
        for _ in range(MAX_ATTEMPTS):
            self.clock.advance(LEASE_SECONDS + 1)
            self.hub().sweep()
            self.claim(self.token)
        self.clock.advance(LEASE_SECONDS + 1)
        self.hub().sweep()
        self.assertEqual(self.stage(self.job_), "failed")


class WorkerTests(HubBase):
    def setUp(self):
        super().setUp()
        self.settings = Settings(executor="runner", quality_first="false", daily_apply_limit=0, min_delay_between_applies=0)
        for p in (mock.patch("core.llm.chat", return_value="One. Two. Three."),
                  mock.patch("core.llm.chat_json", return_value={"variant_id": 0})):
            p.start()
            self.addCleanup(p.stop)
        self.token = self.make_token()

    def worker(self):
        from applier.queue_worker import QueueWorker
        return QueueWorker(1, db_factory=lambda: self.db(), settings_factory=lambda: self.settings,
                           orchestrator_factory=self.orch_factory, clock=self.clock, rng=lambda lo, hi: 77.0)

    def test_a_job_left_for_a_runner_is_not_failed_as_stuck_and_nothing_is_paced(self):
        job = self.job(stage="queued", url="https://boards.greenhouse.io/acme/jobs/1")
        w = self.worker()
        w._run_job(self.db(), self.settings, self.db().get_by_id(job["job_id"]))
        self.assertEqual(self.stage(job), "preparing", "still waiting for the runner")
        self.assertIsNone(w._not_before, "no pacing delay: no browser work happened here")
        self.assertEqual(len(self.tasks().list("queued")), 1)

    def test_the_worker_keeps_going_with_the_next_job_straight_away(self):
        a = self.job(stage="queued", url="https://boards.greenhouse.io/acme/jobs/1")
        b = self.job(stage="queued", url="https://boards.greenhouse.io/acme/jobs/2")
        w = self.worker()
        self.assertEqual(w.step(), "processed")
        self.assertEqual(w.step(), "processed")
        self.assertEqual({self.stage(a), self.stage(b)}, {"preparing"})
        self.assertEqual(len(self.tasks().list("queued")), 2)

    def test_startup_recovery_leaves_jobs_a_runner_may_still_deliver(self):
        waiting = self.job(stage="queued", url="https://boards.greenhouse.io/acme/jobs/1")
        w = self.worker()
        w._run_job(self.db(), self.settings, self.db().get_by_id(waiting["job_id"]))
        crashed = self.job(stage="preparing", url="https://boards.greenhouse.io/acme/jobs/2")        # no task: a real crash
        self.worker().recover()
        self.assertEqual(self.stage(waiting), "preparing")
        self.assertEqual(self.stage(crashed), "queued")

    def test_the_worker_sweeps_so_a_vanished_runner_is_noticed_with_nobody_polling(self):
        job = self.job(stage="queued", url="https://boards.greenhouse.io/acme/jobs/1")
        w = self.worker()
        w.step()
        self.claim(self.token)
        self.clock.advance(LEASE_SECONDS + 1)
        w.step()
        self.assertEqual(self.tasks().list()[0]["status"], "queued", "re-queued by the worker's sweep")
        self.assertIn("runner_lost", self.step_codes(job))
        for _ in range(MAX_ATTEMPTS):
            self.claim(self.token)
            self.clock.advance(LEASE_SECONDS + 1)
            w.step()
        self.assertEqual((self.tasks().list()[0]["status"], self.stage(job)), ("expired", "failed"))


# ── over HTTP ─────────────────────────────────────────────────────

@unittest.skipUnless(HAVE_FASTAPI, "fastapi + httpx not installed")
class RunnerHttpTests(unittest.TestCase):
    def setUp(self):
        import core.auth as auth
        from db.settings_db import SettingsDB
        from db.users_db import UsersDB

        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self._saved_settings = SettingsDB._instance
        SettingsDB._instance = SettingsDB(path=tmp / "settings.db")
        self._saved = (cfg.JOBS_DB_PATH, cfg.PROFILE_DB_PATH, cfg.SESSIONS_DIR, cfg.SCREENSHOTS_DIR, cfg.DATA_DIR,
                       cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE, cfg.ALLOW_SIGNUP, UsersDB._instance, auth._serializer)
        cfg.JOBS_DB_PATH, cfg.PROFILE_DB_PATH, cfg.DATA_DIR = tmp / "jobs.db", tmp / "profile.db", tmp
        cfg.SESSIONS_DIR, cfg.SCREENSHOTS_DIR = tmp / "sessions", tmp / "screenshots"
        cfg.SESSIONS_DIR.mkdir()
        cfg.APP_SECRET_KEY, cfg.COOKIE_SECURE, cfg.ALLOW_SIGNUP = "test-signing-secret-" + "x" * 40, False, False
        UsersDB._instance = UsersDB(path=tmp / "users.db")
        auth._serializer = None
        self._dev = os.environ.pop("APEX_DEV_UI_ORIGIN", None)
        from server.app import create_app
        self.app = create_app(ui_dir=tmp / "no-build")
        self.client = TestClient(self.app, follow_redirects=False)           # alice = owner
        self.client.post("/api/auth/signup", json={"email": "alice@example.com", "password": "correct-horse-battery"})
        cfg.ALLOW_SIGNUP = True
        self.bob = TestClient(self.app, follow_redirects=False)
        self.bob.post("/api/auth/signup", json={"email": "bob@example.com", "password": "correct-horse-battery"})
        self.runner = TestClient(self.app, follow_redirects=False)           # no cookies: only a token
        self.token = self.client.post("/api/settings/runner-tokens", json={"label": "laptop"}).json()["token"]
        self.hdr = {"Authorization": f"Bearer {self.token}"}
        self.jobs = JobsDB(user_id=1)

    def tearDown(self):
        import core.auth as auth
        from db.settings_db import SettingsDB
        from db.users_db import UsersDB
        SettingsDB._instance = self._saved_settings
        (cfg.JOBS_DB_PATH, cfg.PROFILE_DB_PATH, cfg.SESSIONS_DIR, cfg.SCREENSHOTS_DIR, cfg.DATA_DIR, cfg.APP_SECRET_KEY,
         cfg.COOKIE_SECURE, cfg.ALLOW_SIGNUP, UsersDB._instance, auth._serializer) = self._saved
        if self._dev is not None:
            os.environ["APEX_DEV_UI_ORIGIN"] = self._dev
        self.tmp.cleanup()

    def queued_task(self):
        jid = self.jobs.add({"url": "https://boards.greenhouse.io/acme/jobs/1", "title": "Dev", "company": "Acme",
                             "source": "linkedin", "description": "Python."})
        self.jobs.transition(jid, "preparing")
        task, _ = RunnerHub(1).enqueue(self.jobs.get_by_id(jid), "apply", {})
        return jid, task["id"]

    def test_token_management_is_owner_only_and_shows_the_token_once(self):
        listed = self.client.get("/api/settings/runner-tokens").json()["tokens"]
        self.assertEqual([t["label"] for t in listed], ["laptop"])
        self.assertNotIn(self.token, json.dumps(listed))
        for t in listed:
            self.assertNotIn("token", t)
            self.assertNotIn("token_hash", t)
        self.assertEqual(self.bob.get("/api/settings/runner-tokens").status_code, 403)
        self.assertEqual(self.bob.post("/api/settings/runner-tokens", json={"label": "x"}).status_code, 403)
        self.assertEqual(self.runner.get("/api/settings/runner-tokens", headers=self.hdr).status_code, 401, "a runner cannot manage tokens")
        self.assertEqual(self.client.post("/api/settings/runner-tokens", json={"label": ""}).status_code, 400)

    def test_revoking_stops_the_token_immediately(self):
        self.assertEqual(self.runner.post("/api/runner/claim", json={"wait": 0}, headers=self.hdr).status_code, 200)
        tid = self.client.get("/api/settings/runner-tokens").json()["tokens"][0]["id"]
        self.assertEqual(self.client.delete(f"/api/settings/runner-tokens/{tid}").status_code, 200)
        self.assertEqual(self.runner.post("/api/runner/claim", json={"wait": 0}, headers=self.hdr).status_code, 401)
        self.assertEqual(self.client.delete("/api/settings/runner-tokens/9999").status_code, 404)

    def test_every_runner_endpoint_wants_a_valid_token(self):
        tid = "a" * 32
        calls = (("post", "/api/runner/claim", {"json": {}}), ("post", f"/api/runner/{tid}/events", {"json": {"events": []}}),
                 ("post", f"/api/runner/{tid}/screenshot", {"files": {"file": ("x.png", make_png(), "image/png")}}),
                 ("post", f"/api/runner/{tid}/result", {"json": {"success": True}}), ("get", f"/api/runner/{tid}/resume", {}))
        for method, url, kw in calls:
            for headers in ({}, {"Authorization": "Bearer apxr_not-a-real-token"}, {"Authorization": f"Basic {self.token}"},
                            {"Authorization": self.token}):
                r = getattr(self.runner, method)(url, headers=headers, **kw)
                self.assertEqual(r.status_code, 401, f"{method} {url} {headers}")
            self.assertEqual(getattr(self.client, method)(url, **kw).status_code, 401, f"a SESSION is not a runner token: {url}")

    def test_a_runner_token_cannot_reach_any_other_endpoint(self):
        reached = []
        for route in self.app.routes:
            path, methods = getattr(route, "path", ""), getattr(route, "methods", None) or set()
            if not path.startswith("/api/") or path.startswith("/api/runner/") or "{" in path:
                continue
            for method in methods & {"GET", "POST", "PUT", "DELETE", "PATCH"}:
                r = self.runner.request(method, path, headers=self.hdr, json={} if method != "GET" else None)
                if r.status_code not in (401, 404, 405):
                    reached.append((method, path, r.status_code))
        self.assertEqual(reached, [], "a runner token got through to endpoints that want a session")

    def test_nor_the_parameterised_ones(self):
        for method, path in (("get", "/api/applications/x/pack"), ("post", "/api/applications/x/mark-applied"),
                             ("get", "/api/kits/x"), ("put", "/api/resumes/1/default"), ("delete", "/api/resumes/1"),
                             ("delete", "/api/settings/runner-tokens/1"), ("get", "/api/automation/ledger")):
            self.assertEqual(getattr(self.runner, method)(path, headers=self.hdr).status_code, 401, path)

    def test_the_full_cycle(self):
        jid, task_id = self.queued_task()
        r = self.runner.post("/api/runner/claim", json={"wait": 0}, headers=self.hdr)
        self.assertEqual(r.status_code, 200)
        task = r.json()["task"]
        self.assertEqual((task["task_id"], task["application"]["job_id"]), (task_id, jid))
        e = self.runner.post(f"/api/runner/{task_id}/events", headers=self.hdr,
                             json={"events": [{"event": "step", "data": {"step": "opened_page", "message": "Opened"}}]})
        self.assertEqual(e.json()["accepted"], 1)
        s = self.runner.post(f"/api/runner/{task_id}/screenshot", headers=self.hdr, data={"label": "form"},
                             files={"file": ("../../../etc/passwd", make_png(), "text/plain")})
        self.assertEqual(s.status_code, 200)
        self.assertRegex(s.json()["screenshot"], r"_runner_form_[0-9a-f]{8}\.png$")
        self.assertNotIn("passwd", s.json()["screenshot"])
        done = self.runner.post(f"/api/runner/{task_id}/result", headers=self.hdr, json={"success": True})
        self.assertEqual(done.json(), {"status": "accepted", "application": "ready_for_review"})

    def test_bad_screenshots_over_http(self):
        jid, task_id = self.queued_task()
        self.runner.post("/api/runner/claim", json={"wait": 0}, headers=self.hdr)
        url = f"/api/runner/{task_id}/screenshot"
        for name, data in (("a.png", b"not a png"), ("a.png", b""), ("a.jpg", b"\xff\xd8\xff" + b"0" * 50)):
            self.assertEqual(self.runner.post(url, headers=self.hdr, files={"file": (name, data, "image/png")}).status_code, 400)
        big = make_png(pad_to=MAX_PNG_BYTES + 10)
        self.assertEqual(self.runner.post(url, headers=self.hdr, files={"file": ("big.png", big, "image/png")}).status_code, 413)

    def test_a_long_poll_with_nothing_to_do_answers_null(self):
        t0 = time.time()
        r = self.runner.post("/api/runner/claim", json={"wait": 1}, headers=self.hdr)
        self.assertEqual((r.status_code, r.json()), (200, {"task": None}))
        self.assertGreaterEqual(time.time() - t0, 0.9)
        self.assertLess(time.time() - t0, 5)

    def test_the_executor_setting_is_validated(self):
        self.assertEqual(self.client.post("/api/settings", json={"executor": "runner"}).status_code, 200)
        self.assertEqual(self.client.post("/api/settings", json={"executor": "cloud"}).status_code, 400)
        self.assertEqual(self.bob.post("/api/settings", json={"executor": "server"}).status_code, 403)


if __name__ == "__main__":
    unittest.main()
