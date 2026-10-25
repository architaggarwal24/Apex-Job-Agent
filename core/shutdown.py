"""
core/shutdown.py — Shutdown and cancellation coordination.

Two distinct signals are provided, deliberately kept separate:

  shutdown_event
      Process-level. Set ONCE by main.py's Ctrl+C/SIGTERM handler and
      NEVER cleared. Means "the whole process is going down" — any
      loop that sees this set should stop permanently.

  pipeline_cancel_event
      Per-run. Set by POST /api/pipeline/cancel to stop the *current*
      pipeline run ASAP. Unlike shutdown_event, this is reset at the
      start of every new pipeline run via reset_pipeline_cancel(), so
      clicking "Cancel" once does not permanently disable future runs.

Previously both concerns shared a single Event, which meant clicking
"Cancel" in the UI set a flag that was never cleared — permanently
short-circuiting every subsequent pipeline run until the server was
restarted. Keeping them separate fixes that while preserving the
"stop everything on Ctrl+C" behaviour.
"""

import contextlib
import logging
import os
import signal
import subprocess
import threading
from typing import Protocol

logger = logging.getLogger(__name__)


class CancelEvent(Protocol):
    """Anything with .is_set() — threading.Event or _CombinedCancelEvent below.
    Shared type for cancel_event params across pipeline.run() and
    score_jobs_batch()."""
    def is_set(self) -> bool: ...

# Process-level shutdown — set by main.py's signal handler, never cleared.
shutdown_event = threading.Event()

# Per-run pipeline cancellation — reset at the start of each pipeline run.
pipeline_cancel_event = threading.Event()


def reset_pipeline_cancel():
    """Call at the start of each pipeline run so a previous Cancel
    click doesn't short-circuit this new run."""
    pipeline_cancel_event.clear()


def request_pipeline_cancel():
    """Called by POST /api/pipeline/cancel."""
    pipeline_cancel_event.set()


def should_stop() -> bool:
    """
    True if either the process is shutting down OR the current
    pipeline run was cancelled. This is the combined check that
    long-running loops (scraping, scoring, briefing) should use.
    """
    return shutdown_event.is_set() or pipeline_cancel_event.is_set()


# ── Background thread registry ──────────────────────────────────────────────
# Used by main.py's Ctrl+C handler to wait briefly for in-flight
# Playwright/LLM operations to notice `shutdown_event` and exit cleanly,
# rather than killing the process mid-operation (which can leave Opera
# holding a locked profile directory).

_active_threads: list[threading.Thread] = []
_lock = threading.Lock()


def register_thread(t: threading.Thread):
    """Register a background thread so wait_for_threads() can join it
    on shutdown. Call this for any threading.Thread used to run
    pipeline/apply work."""
    with _lock:
        _active_threads.append(t)
        _active_threads[:] = [x for x in _active_threads if x.is_alive()]


def active_count() -> int:
    with _lock:
        _active_threads[:] = [x for x in _active_threads if x.is_alive()]
        return len(_active_threads)


def wait_for_threads(timeout: float = 10.0):
    """Wait for registered background threads to finish, up to timeout seconds."""
    with _lock:
        threads = [x for x in _active_threads if x.is_alive()]

    if not threads:
        return

    logger.info(f"[Shutdown] Waiting for {len(threads)} background task(s) "
                f"to finish (max {timeout}s)...")
    for t in threads:
        t.join(timeout=timeout)

    remaining = [x for x in threads if x.is_alive()]
    if remaining:
        logger.warning(f"[Shutdown] {len(remaining)} task(s) still running — "
                       f"forcing exit anyway")
    else:
        logger.info("[Shutdown] All background tasks finished cleanly")


def is_shutting_down() -> bool:
    return shutdown_event.is_set()


class _CombinedCancelEvent:
    """
    Adapter exposing the same .is_set() interface as threading.Event,
    so existing code (pipeline.run(), score_jobs_batch()) that does
    `cancel_event.is_set()` works unchanged, while actually checking
    should_stop() — i.e. either process shutdown OR this pipeline
    run's cancellation.
    """
    def is_set(self) -> bool:
        return should_stop()


# Singleton passed to pipeline.run()/score_jobs_batch() as cancel_event.
combined_cancel_event = _CombinedCancelEvent()


# ── Ctrl+C ownership and child processes (main.py) ─────────────────────────

def make_server(config):
    """
    A uvicorn.Server that leaves Ctrl+C/SIGTERM to the process's own handler (main.py).

    uvicorn >= 0.29 installs its own handler for as long as it runs (capture_signals),
    replacing main.py's. On Ctrl+C it then closes the port and waits for every open
    connection to finish - and the app's live-updates stream (/api/events/stream) never
    finishes while a tab is open - so Ctrl+C looked like it did nothing, the Next dev server
    was never stopped and kept logging ECONNREFUSED. Older uvicorn used
    install_signal_handlers(); both are disabled here.
    """
    import uvicorn

    class _Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):
            yield

        def install_signal_handlers(self):
            pass

    return _Server(config)


def kill_process_tree(proc, timeout: float = 5.0) -> None:
    """
    Stop a child started in its own process group (start_new_session on POSIX,
    CREATE_NEW_PROCESS_GROUP on Windows) together with everything it started.

    On Windows `npm` is npm.cmd: terminate() would end only that cmd.exe and leave node
    (the Next dev server, still holding port 3000) running, so the tree is ended with
    taskkill /T instead.
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=timeout)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=timeout)
    except Exception:
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            proc.kill()
            proc.wait(timeout=timeout)
        except Exception:
            pass
