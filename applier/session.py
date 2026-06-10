"""
applier/session.py — ApplicationSession state machine.

One session per job. Persisted to JSON so the web UI
can resume across requests and server restarts.

States:
  idle → analyzing → filling → awaiting_input →
  refilling → screenshot_review → submitting → done | failed | skipped
"""

import json
import logging
import random
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path

from core.config import cfg

logger = logging.getLogger(__name__)


class State(str, Enum):
    IDLE         = "idle"
    ANALYZING    = "analyzing"
    FILLING      = "filling"
    AWAITING     = "awaiting_input"
    REFILLING    = "refilling"
    REVIEW       = "screenshot_review"
    SUBMITTING   = "submitting"
    DONE         = "done"
    FAILED       = "failed"
    SKIPPED      = "skipped"

    @property
    def is_terminal(self):
        return self in (State.DONE, State.FAILED, State.SKIPPED)

    @property
    def step_num(self) -> int:
        order = [State.IDLE, State.ANALYZING, State.FILLING,
                 State.AWAITING, State.REFILLING, State.REVIEW,
                 State.SUBMITTING, State.DONE]
        try:
            return order.index(self) + 1
        except ValueError:
            return 0


@dataclass
class ApplicationSession:
    job_id:    str
    job_url:   str
    job_title: str
    company:   str
    source:    str

    state:           str        = State.IDLE.value
    error:           str        = ""
    iteration:       int        = 0
    screenshots:     list[str]  = field(default_factory=list)
    missing_prompts: list[dict] = field(default_factory=list)
    corrections:     list[str]  = field(default_factory=list)
    log:             list[str]  = field(default_factory=list)
    created_at:      str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at:      str = field(default_factory=lambda: datetime.now().isoformat())

    def save(self):
        self.updated_at = datetime.now().isoformat()
        p = cfg.SESSIONS_DIR / f"{self.job_id}.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, job_id: str):
        p = cfg.SESSIONS_DIR / f"{job_id}.json"
        if not p.exists():
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                return cls(**json.load(f))
        except Exception as e:
            logger.warning(f"Session load error {job_id}: {e}")
            return None

    @classmethod
    def load_all(cls) -> list:
        sessions = []
        for p in cfg.SESSIONS_DIR.glob("*.json"):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    sessions.append(cls(**json.load(f)))
            except Exception:
                pass
        return sorted(sessions, key=lambda s: s.updated_at, reverse=True)

    def transition(self, new_state: State, msg: str = ""):
        self.state = new_state.value
        if msg:
            self.add_log(msg)
        logger.info(f"  [Session {self.job_id[:8]}] → {new_state.value}")
        self.save()

    def add_log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log.append(f"[{ts}] {msg}")
        if len(self.log) > 300:
            self.log = self.log[-300:]

    def fail(self, reason: str):
        self.error = reason
        self.transition(State.FAILED, f"FAILED: {reason}")
        return self

    def skip(self, reason: str):
        self.error = reason
        self.transition(State.SKIPPED, f"SKIPPED: {reason}")
        return self

    def to_dict(self) -> dict:
        return asdict(self)


class Orchestrator:
    """
    Drives the full application lifecycle for one job.
    Platform-agnostic — uses universal.py for all sites.
    """

    def __init__(self, job: dict):
        self.job    = job
        self.job_id = job["job_id"]

        from db.profile_db import ProfileDB
        from db.jobs_db    import JobsDB
        self.profile_db = ProfileDB()
        self.jobs_db    = JobsDB()

        self.session = ApplicationSession.load(self.job_id) or ApplicationSession(
            job_id=    self.job_id,
            job_url=   job["url"],
            job_title= job["title"],
            company=   job["company"],
            source=    job.get("source", ""),
        )
        self.session.save()

    # ── Public API ────────────────────────────────────────────────

    def run_iter1(self, emit=None):
        s = self.session
        s.transition(State.ANALYZING, "Starting application")
        s.iteration = 1
        self._emit(emit, "state", State.ANALYZING.value)

        from playwright.sync_api import sync_playwright
        from applier.universal import run_application

        profile = self.profile_db.summary()

        with sync_playwright() as pw:
            try:
                ctx  = self._open_browser(pw)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
            except Exception as e:
                return s.fail(f"Browser launch failed: {e}")

            try:
                s.transition(State.FILLING, "Analyzing and filling form")
                self._emit(emit, "state", State.FILLING.value)

                result = run_application(page, self.job, profile, emit=self._emit_fn(emit))
                s.screenshots.extend(result.get("screenshots", []))

                if not result["success"] and result.get("error"):
                    ctx.close()
                    return s.fail(result["error"])

                # Flag missing fields
                for ff in result.get("missing_fields", []):
                    self.profile_db.flag_missing(
                        job_id=      self.job_id,
                        job_url=     self.job["url"],
                        job_title=   self.job["title"],
                        company=     self.job["company"],
                        field_label= ff.label,
                        field_key=   ff.field_key,
                        field_type=  ff.field_type,
                        options=     ff.options,
                        placeholder= ff.placeholder,
                    )
                ctx.close()

            except Exception as e:
                logger.exception(f"Iter1 error {self.job_id}")
                try: ctx.close()
                except Exception: pass
                return s.fail(str(e))

        # Build missing prompts for UI
        pending = self.profile_db.get_pending_missing(self.job_id)
        s.missing_prompts = [
            {"id": r["id"], "field_key": r["field_key"],
             "label": r["field_label"], "field_type": r["field_type"],
             "options": r["options"], "placeholder": r["placeholder_used"]}
            for r in pending
        ]

        if s.missing_prompts:
            s.transition(State.AWAITING,
                f"Waiting for input — {len(s.missing_prompts)} missing fields")
            self._emit(emit, "awaiting_input", {"missing_fields": s.missing_prompts})
        else:
            s.transition(State.REVIEW, "Form filled — review screenshot")
            self._emit(emit, "screenshot_review", {"screenshots": s.screenshots})

        s.save()
        return s

    def run_iter2(self, answers: dict[str, str], emit=None):
        s = self.session
        self.profile_db.set_many(answers, source="user_prompt")
        for p in s.missing_prompts:
            if answers.get(p["field_key"]):
                self.profile_db.resolve_missing(p["id"], answers[p["field_key"]])

        s.transition(State.REFILLING, "Filling missing fields (pass 2)")
        s.iteration = 2
        self._emit(emit, "state", State.REFILLING.value)

        missing_keys = [p["field_key"] for p in s.missing_prompts]
        profile = self.profile_db.summary()

        from playwright.sync_api import sync_playwright
        from applier.universal import run_iteration2

        with sync_playwright() as pw:
            try:
                ctx  = self._open_browser(pw)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
            except Exception as e:
                return s.fail(f"Browser launch failed: {e}")
            try:
                result = run_iteration2(page, self.job, profile, missing_keys,
                                        emit=self._emit_fn(emit))
                s.screenshots.extend(result.get("screenshots", []))
                ctx.close()
            except Exception as e:
                logger.exception(f"Iter2 error {self.job_id}")
                try: ctx.close()
                except Exception: pass
                return s.fail(str(e))

        s.missing_prompts = []
        s.transition(State.REVIEW, "Pass 2 complete — review screenshot")
        self._emit(emit, "screenshot_review", {"screenshots": s.screenshots})
        s.save()
        return s

    def submit(self, corrections: list[str] | None = None, emit=None):
        s = self.session
        if corrections:
            s.corrections = corrections
            self._apply_corrections(corrections)

        s.transition(State.SUBMITTING, "Submitting application")
        self._emit(emit, "state", State.SUBMITTING.value)

        profile = self.profile_db.summary()

        from playwright.sync_api import sync_playwright
        from applier.universal import run_submit

        with sync_playwright() as pw:
            try:
                ctx  = self._open_browser(pw)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
            except Exception as e:
                return s.fail(f"Browser launch failed: {e}")
            try:
                result = run_submit(page, self.job, profile,
                                    corrections=corrections,
                                    emit=self._emit_fn(emit))
                s.screenshots.extend(result.get("screenshots", []))
                ctx.close()
            except Exception as e:
                logger.exception(f"Submit error {self.job_id}")
                try: ctx.close()
                except Exception: pass
                return s.fail(str(e))

        if result.get("success"):
            s.transition(State.DONE, "Application submitted!")
            self.jobs_db.update_status(self.job_id, "applied")
            self.jobs_db.add_stage(self.job_id, "applied")
            self._emit(emit, "done", {})
        else:
            return s.fail(result.get("error", "Submit failed"))

        s.save()
        return s

    # ── Helpers ───────────────────────────────────────────────────

    def _open_browser(self, playwright):
        from core.browser import launch_browser
        return launch_browser(playwright)

    def _apply_corrections(self, corrections: list[str]):
        if not corrections:
            return
        flat = self.profile_db.get_all()
        correction_text = "\n".join(f"- {c}" for c in corrections)
        try:
            from core import llm as LLM
            result = LLM.chat_json(
                messages=[{"role": "user", "content":
                    f"User wants these corrections:\n{correction_text}\n\n"
                    f"Current profile:\n{json.dumps(flat)[:2000]}\n\n"
                    f"Return ONLY JSON {{field_key: new_value}} for fields to update."}],
                max_tokens=512,
            )
            if isinstance(result, dict):
                self.profile_db.set_many(result, source="correction")
        except Exception as e:
            logger.warning(f"Correction LLM failed: {e}")

    def _emit_fn(self, emit):
        def fn(event, data):
            self._emit(emit, event, data)
        return fn

    def _emit(self, emit_fn, event: str, data):
        if event == "log" and isinstance(data, str):
            self.session.add_log(data)
        if emit_fn:
            try: emit_fn(event, data)
            except Exception: pass
