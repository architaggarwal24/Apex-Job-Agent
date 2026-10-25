# applier/executors.py
"""
WHO DOES THE BROWSER WORK of an application pass.

The Orchestrator (applier/session.py) decides WHAT to do - preflight, the kit,
what to do with the result - and hands the browser part of each pass to an
executor:

    ServerExecutor   today's behaviour, unchanged: open the browser here (the
                     Opera profile on this machine), run the form code, close it.
    RunnerExecutor   leave the pass as a TASK for a runner on your own computer
                     (db/runner_db.py, applier/runner_hub.py). Nothing is opened
                     here; the application stays where it is and is resumed
                     when the runner reports back (Orchestrator.finish_pass).

A pass is one of three kinds - "apply" (first pass), "refill" (second pass,
params {"missing_keys"}) and "submit" (params {"corrections", "dry_run"}).
execute() returns an Outcome: either the form code's `result` dict, or
deferred=True when the work was left for a runner.

The `executor` setting chooses, per user:
    server   always this machine (the default - what the app did before)
    runner   always a runner; tasks wait for one (and expire if none comes)
    auto     a runner if one of the user's tokens was seen in the last minute,
             else this machine
An unreadable or unknown value is `server`: the safe fallback is today's behaviour.
Assist mode (applier/apply_mode.py) is decided earlier and needs no executor.
"""

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MODES        = ("server", "runner", "auto")
MODE_KEY     = "executor"
DEFAULT_MODE = "server"


class BrowserLaunchError(Exception):
    """The server could not open its browser. `original` is the underlying error."""
    def __init__(self, original: Exception):
        self.original = original
        super().__init__(str(original))


@dataclass
class Outcome:
    result: dict | None = None      # what the form code returned
    deferred: bool = False          # left for a runner: no result yet
    task_id: str = ""


class Executor(ABC):
    name: str = ""

    @abstractmethod
    def execute(self, orch, kind: str, profile, emit, params: dict) -> Outcome:
        """Run (or leave for someone else) one pass of `kind` for the
        Orchestrator `orch`. Exceptions propagate to the Orchestrator, which
        turns them into the application's failure."""


class ServerExecutor(Executor):
    """Today's behaviour: the browser is opened on this machine."""
    name = "server"

    def execute(self, orch, kind, profile, emit, params):
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            try:
                ctx  = orch._open_browser(pw)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
            except Exception as e:
                raise BrowserLaunchError(e)
            try:
                orch._before_pass(kind, emit)
                answers = orch._answers_kw(profile)
                if kind == "apply":
                    result = orch._run_pass("run_application", page, profile, emit=orch._emit_fn(emit), **answers)
                elif kind == "refill":
                    result = orch._run_pass("run_iteration2", page, profile, params["missing_keys"],
                                            emit=orch._emit_fn(emit), **answers)
                elif kind == "submit":
                    result = orch._run_pass("run_submit", page, profile, corrections=params.get("corrections"),
                                            emit=orch._emit_fn(emit), dry_run=params.get("dry_run", False), **answers)
                else:
                    raise ValueError(f"unknown pass kind {kind!r}")
                return Outcome(result=result)
            finally:
                try:
                    ctx.close()
                except Exception:
                    pass


class RunnerExecutor(Executor):
    """Leave the pass for a runner on the user's own computer."""
    name = "runner"

    def execute(self, orch, kind, profile, emit, params):
        from applier.runner_hub import RunnerHub
        from db.answer_bank_db import PROFILE_OWNER_ID
        orch._before_pass(kind, emit)
        uid = orch.job.get("user_id") or PROFILE_OWNER_ID
        task, created = RunnerHub(uid, orch.jobs_db.path).enqueue(orch.job, kind, params)
        orch._step("runner_queued" if created else "runner_already_queued",
                   "Left for your runner" if created else "Already waiting for your runner",
                   {"task": task["id"][:8], "kind": kind})
        orch.session.save()
        return Outcome(deferred=True, task_id=task["id"])


def clean_mode(value) -> str:
    mode = value.strip().lower() if isinstance(value, str) else value
    if mode not in MODES:
        raise ValueError(f"executor must be one of: {', '.join(MODES)}")
    return mode


def choose(settings, user_id: int, path=None, now: float | None = None) -> Executor:
    """The executor for `user_id` (see the module docstring). Never raises."""
    try:
        raw = settings.read(MODE_KEY, DEFAULT_MODE)
        try:
            mode = clean_mode(raw if raw not in (None, "") else DEFAULT_MODE)
        except ValueError:
            logger.error(f"[Executor] executor {raw!r} is not server, runner or auto; using {DEFAULT_MODE}")
            return ServerExecutor()
        if mode == "runner":
            return RunnerExecutor()
        if mode == "auto":
            from db.runner_db import RunnerTokenStore
            if RunnerTokenStore(user_id, path).seen_recently(now if now is not None else time.time()):
                return RunnerExecutor()
        return ServerExecutor()
    except Exception:
        logger.exception("[Executor] could not choose an executor; using the server")
        return ServerExecutor()
