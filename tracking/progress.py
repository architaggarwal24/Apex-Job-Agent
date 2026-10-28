# tracking/progress.py
"""
Live progress of the job search (tracking/pipeline.py), for the progress bar
on every page and the step list on Today (GET /api/pipeline/status).

One search runs at a time, so this is one in-memory record guarded by a lock:
the pipeline thread writes, the status route reads snapshots. Nothing here is
durable - a restart forgets a run, which is right, because the run died with
the process.

A run is a fixed list of PHASES. Each has done/total units (one board query,
one feed, one company board, one scored job, one brief), so the bar moves as
work finishes, not on a timer. Totals can grow while running (the company
boards are only known after the feeds taught the agent new ones); the
overall percent never goes backwards.
"""

import threading
import time

PHASES = (
    ("boards",    "Job boards"),
    ("sources",   "Job feeds"),
    ("companies", "Company boards"),
    ("save",      "Saving new jobs"),
    ("score",     "Scoring"),
    ("briefs",    "AI briefs"),
)
MAX_LOG = 60


class Progress:
    def __init__(self, clock=time.time):
        self._lock = threading.Lock()
        self._clock = clock
        self._state = self._idle()

    @staticmethod
    def _idle() -> dict:
        return {"run_id": 0, "user_id": None, "running": False, "started_at": None, "finished_at": None,
                "phase": "", "current": "", "found": 0, "new": 0, "percent": 0, "cancelled": False,
                "error": "", "stats": None, "log": [],
                "phases": [{"key": k, "label": l, "status": "pending", "done": 0, "total": 0, "found": 0} for k, l in PHASES]}

    # ── writer side (the pipeline thread) ─────────────────────────

    def start(self, user_id: int) -> int:
        """A new run; ValueError when one is already running."""
        with self._lock:
            if self._state["running"]:
                raise ValueError("A search is already running")
            run_id = self._state["run_id"] + 1
            self._state = self._idle()
            self._state.update(run_id=run_id, user_id=user_id, running=True, started_at=self._clock())
            return run_id

    def is_running(self) -> bool:
        with self._lock:
            return self._state["running"]

    def _phase(self, key):
        return next(p for p in self._state["phases"] if p["key"] == key)

    def begin(self, key: str, total: int, current: str = "") -> None:
        """Phase `key` starts with `total` units (earlier phases are closed)."""
        with self._lock:
            for p in self._state["phases"]:
                if p["key"] == key:
                    break
                if p["status"] == "running":
                    p["status"] = "done"
            p = self._phase(key)
            p.update(status="running", total=max(int(total), p["done"]))
            self._state.update(phase=key, current=current or p["label"])
            self._recompute()

    def grow(self, key: str, more: int) -> None:
        with self._lock:
            self._phase(key)["total"] += max(0, int(more))
            self._recompute()

    def tick(self, key: str, current: str = "", found: int = 0, units: int = 1) -> None:
        """`units` of phase `key` finished, having found `found` jobs."""
        with self._lock:
            p = self._phase(key)
            p["done"] += units
            p["total"] = max(p["total"], p["done"])
            p["found"] += max(0, int(found))
            if key in ("boards", "sources", "companies"):
                self._state["found"] += max(0, int(found))
            if current:
                self._state["current"] = current
            self._recompute()

    def set_done(self, key: str, done: int, total: int, current: str = "") -> None:
        with self._lock:
            p = self._phase(key)
            p.update(done=int(done), total=max(int(total), int(done)))
            if current:
                self._state["current"] = current
            self._recompute()

    def note(self, **fields) -> None:
        """Set top-level fields such as new=... (only known keys)."""
        with self._lock:
            for k, v in fields.items():
                if k in ("new", "current", "cancelled"):
                    self._state[k] = v

    def log(self, msg: str) -> None:
        with self._lock:
            self._state["log"].append({"at": self._clock(), "msg": str(msg)[:300]})
            del self._state["log"][:-MAX_LOG]

    def finish(self, stats: dict | None = None, error: str = "") -> None:
        with self._lock:
            for p in self._state["phases"]:
                if p["status"] == "running":
                    p["status"] = "done" if not error else "failed"
                elif p["status"] == "pending":
                    p["status"] = "skipped"
            cancelled = bool((stats or {}).get("cancelled")) or self._state["cancelled"]
            self._state.update(running=False, finished_at=self._clock(), stats=stats, error=(error or "")[:300],
                               cancelled=cancelled, phase="", current="Cancelled" if cancelled else ("Failed" if error else "Done"))
            if not error and not cancelled:
                self._state["percent"] = 100

    def close(self, run_id: int, error: str = "") -> None:
        """End run `run_id` if it is still open (the caller's safety net when
        the pipeline returned or crashed without closing it)."""
        with self._lock:
            open_ = self._state["running"] and self._state["run_id"] == run_id
        if open_:
            self.finish(error=error)

    def _recompute(self) -> None:
        done = sum(p["done"] for p in self._state["phases"])
        total = sum(p["total"] for p in self._state["phases"])
        pct = int(done * 100 / total) if total else 0
        self._state["percent"] = max(self._state["percent"], min(99, pct))

    # ── reader side ───────────────────────────────────────────────

    def snapshot(self, user_id: int) -> dict:
        """The run as `user_id` may see it: the full record for the user who
        started it, an idle record for anyone else."""
        with self._lock:
            s = self._state
            if s["user_id"] != user_id:
                return {**self._idle(), "run_id": 0}
            out = {k: v for k, v in s.items() if k not in ("phases", "log")}
            out["phases"] = [dict(p) for p in s["phases"]]
            out["log"] = list(s["log"][-20:])
            now = self._clock()
            out["elapsed"] = round(((s["finished_at"] or now) - s["started_at"]), 1) if s["started_at"] else 0
            return out


TRACKER = Progress()
