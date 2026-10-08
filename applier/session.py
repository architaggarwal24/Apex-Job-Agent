"""
applier/session.py — ApplicationSession state machine.

One session per job. Persisted to JSON so the web UI
can resume across requests and server restarts.

States:
  idle → analyzing → filling → awaiting_input →
  refilling → screenshot_review → submitting → done | failed | skipped
<<<<<<< HEAD
=======

These are the fine-grained phases of ONE browser run (what the progress UI
shows). Where the job stands is its lifecycle stage (db/lifecycle.py): the
Orchestrator below moves the job's stage as the run progresses, and the
routes decide what is allowed from the stage, not from this state.
>>>>>>> 4226f10 (.)
"""

import json
import logging
import random
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path

<<<<<<< HEAD
=======
from applier import failures
from applier.answers import normalize_question, remember_answer, remember_modes
>>>>>>> 4226f10 (.)
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
<<<<<<< HEAD
=======
    # The answer bank's per-application state (applier/answers.py). This file
    # belongs to ONE job, so what is kept here can never reach another application.
    application_answers: dict   = field(default_factory=dict)   # question_norm -> answer, definitive for THIS application
    bank_used:       list       = field(default_factory=list)   # bank entries already counted as used by it
    bank_confirm:    dict       = field(default_factory=dict)   # field_key -> a saved answer awaiting your confirmation
>>>>>>> 4226f10 (.)
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
<<<<<<< HEAD
=======
        self._stage("preparing", strict=True)
        if not self._preflight():
            return s
>>>>>>> 4226f10 (.)
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
<<<<<<< HEAD
                return s.fail(f"Browser launch failed: {e}")
=======
                return self._fail(f"Browser launch failed: {e}", e)
>>>>>>> 4226f10 (.)

            try:
                s.transition(State.FILLING, "Analyzing and filling form")
                self._emit(emit, "state", State.FILLING.value)

<<<<<<< HEAD
                result = run_application(page, self.job, profile, emit=self._emit_fn(emit))
=======
                result = run_application(page, self.job, profile, emit=self._emit_fn(emit),
                                         **self._answers_kw(profile))
>>>>>>> 4226f10 (.)
                s.screenshots.extend(result.get("screenshots", []))

                if not result["success"] and result.get("error"):
                    ctx.close()
<<<<<<< HEAD
                    return s.fail(result["error"])
=======
                    return self._fail(result["error"])
>>>>>>> 4226f10 (.)

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
<<<<<<< HEAD
                return s.fail(str(e))
=======
                return self._fail(str(e), e)
>>>>>>> 4226f10 (.)

        # Build missing prompts for UI
        pending = self.profile_db.get_pending_missing(self.job_id)
        s.missing_prompts = [
            {"id": r["id"], "field_key": r["field_key"],
             "label": r["field_label"], "field_type": r["field_type"],
<<<<<<< HEAD
             "options": r["options"], "placeholder": r["placeholder_used"]}
=======
             "options": r["options"], "placeholder": r["placeholder_used"],
             **self._confirmation(r["field_key"])}
>>>>>>> 4226f10 (.)
            for r in pending
        ]

        if s.missing_prompts:
<<<<<<< HEAD
=======
            self._stage("needs_you", reason="question",
                        detail={"missing_fields": [p["label"] for p in s.missing_prompts]})
            self._step("waiting_for_you", failures.WAITING_MESSAGES["question"],
                       {"reason": "question", "count": len(s.missing_prompts)})
>>>>>>> 4226f10 (.)
            s.transition(State.AWAITING,
                f"Waiting for input — {len(s.missing_prompts)} missing fields")
            self._emit(emit, "awaiting_input", {"missing_fields": s.missing_prompts})
        else:
<<<<<<< HEAD
=======
            self._stage("ready_for_review", screenshot=self._last_screenshot())
>>>>>>> 4226f10 (.)
            s.transition(State.REVIEW, "Form filled — review screenshot")
            self._emit(emit, "screenshot_review", {"screenshots": s.screenshots})

        s.save()
        return s

<<<<<<< HEAD
    def run_iter2(self, answers: dict[str, str], emit=None):
        s = self.session
        self.profile_db.set_many(answers, source="user_prompt")
        for p in s.missing_prompts:
            if answers.get(p["field_key"]):
                self.profile_db.resolve_missing(p["id"], answers[p["field_key"]])

=======
    def run_iter2(self, answers: dict[str, str], emit=None, remember=None):
        """`remember` says what to do with the answers (applier/answers.py:
        auto | global | company | once, or {field_key: flag}); default auto."""
        s = self.session
        self._save_answers(answers, remember_modes(remember, answers))

        self._stage("preparing", strict=True)
        if not self._preflight():
            return s
>>>>>>> 4226f10 (.)
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
<<<<<<< HEAD
                return s.fail(f"Browser launch failed: {e}")
            try:
                result = run_iteration2(page, self.job, profile, missing_keys,
                                        emit=self._emit_fn(emit))
=======
                return self._fail(f"Browser launch failed: {e}", e)
            try:
                result = run_iteration2(page, self.job, profile, missing_keys,
                                        emit=self._emit_fn(emit), **self._answers_kw(profile))
>>>>>>> 4226f10 (.)
                s.screenshots.extend(result.get("screenshots", []))
                ctx.close()
            except Exception as e:
                logger.exception(f"Iter2 error {self.job_id}")
                try: ctx.close()
                except Exception: pass
<<<<<<< HEAD
                return s.fail(str(e))

        s.missing_prompts = []
=======
                return self._fail(str(e), e)

        s.missing_prompts = []
        self._stage("ready_for_review", screenshot=self._last_screenshot())
>>>>>>> 4226f10 (.)
        s.transition(State.REVIEW, "Pass 2 complete — review screenshot")
        self._emit(emit, "screenshot_review", {"screenshots": s.screenshots})
        s.save()
        return s

<<<<<<< HEAD
    def submit(self, corrections: list[str] | None = None, emit=None):
        s = self.session
=======
    def _save_answers(self, answers: dict[str, str], modes: dict[str, str]):
        """You answered (or confirmed, or edited) the questions this application
        stopped on. Every answer becomes this application's own, definitive
        answer; what else happens depends on its remember mode: 'once' stores
        nothing anywhere else, anything else also goes to the profile (as
        before) and to the answer bank."""
        s = self.session
        self.profile_db.set_many({k: v for k, v in answers.items() if modes.get(k) != "once"},
                                 source="user_prompt")
        bank = self._bank()
        for p in s.missing_prompts:
            key, value = p["field_key"], (answers.get(p["field_key"]) or "")
            if not value:
                continue
            mode = modes.get(key, "auto")
            confirmed = s.bank_confirm.pop(key, None)
            s.application_answers[normalize_question(p["label"])] = value
            self.profile_db.resolve_missing(p["id"], value, store=mode != "once")
            if bank is None or mode == "once":
                continue
            if mode == "auto" and confirmed and value.strip() == confirmed["suggested"].strip():
                continue                                     # confirmed as it was: nothing new to save
            try:
                remember_answer(bank, p["label"], value, self.job.get("company", ""), mode,
                                p.get("field_type", ""))
            except ValueError as e:
                logger.info(f"[Session {self.job_id[:8]}] not remembered: {e}")
        s.save()

    def _bank(self):
        try:
            from db.answer_bank_db import AnswerBank, PROFILE_OWNER_ID
            return AnswerBank(self.job.get("user_id") or PROFILE_OWNER_ID, self.jobs_db.path)
        except Exception:
            logger.exception(f"[Session {self.job_id[:8]}] answer bank unavailable")
            return None

    def submit(self, corrections: list[str] | None = None, emit=None):
        s = self.session
        if not self._preflight():          # e.g. it went out another way since the review
            return s
>>>>>>> 4226f10 (.)
        if corrections:
            s.corrections = corrections
            self._apply_corrections(corrections)

<<<<<<< HEAD
        s.transition(State.SUBMITTING, "Submitting application")
=======
        # Dry run is read from SettingsDB (UI-managed) with .env as fallback.
        # When enabled, the form is filled and screenshotted exactly as
        # normal, but the submit button is never clicked and the job is
        # never marked "applied" — see applier/universal.run_submit().
        dry_run = self._dry_run_enabled()

        s.transition(State.SUBMITTING,
                     "Submitting application" if not dry_run
                     else "DRY RUN — filling form, will NOT submit")
>>>>>>> 4226f10 (.)
        self._emit(emit, "state", State.SUBMITTING.value)

        profile = self.profile_db.summary()

        from playwright.sync_api import sync_playwright
        from applier.universal import run_submit

        with sync_playwright() as pw:
            try:
                ctx  = self._open_browser(pw)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
            except Exception as e:
<<<<<<< HEAD
                return s.fail(f"Browser launch failed: {e}")
            try:
                result = run_submit(page, self.job, profile,
                                    corrections=corrections,
                                    emit=self._emit_fn(emit))
=======
                return self._fail(f"Browser launch failed: {e}", e)
            try:
                result = run_submit(page, self.job, profile,
                                    corrections=corrections,
                                    emit=self._emit_fn(emit),
                                    dry_run=dry_run,
                                    **self._answers_kw(profile))
>>>>>>> 4226f10 (.)
                s.screenshots.extend(result.get("screenshots", []))
                ctx.close()
            except Exception as e:
                logger.exception(f"Submit error {self.job_id}")
                try: ctx.close()
                except Exception: pass
<<<<<<< HEAD
                return s.fail(str(e))

        if result.get("success"):
            s.transition(State.DONE, "Application submitted!")
            self.jobs_db.update_status(self.job_id, "applied")
            self.jobs_db.add_stage(self.job_id, "applied")
            self._emit(emit, "done", {})
        else:
            return s.fail(result.get("error", "Submit failed"))
=======
                return self._fail(str(e), e)

        if result.get("success"):
            if dry_run:
                s.transition(State.DONE,
                             "DRY RUN complete — form was filled but NOT submitted. "
                             "No application was sent.")
                self._emit(emit, "done", {"dry_run": True})
                # Explicitly do NOT move the job to "submitted" — it stays at
                # ready_for_review so the user can run it for real later.
            else:
                s.transition(State.DONE, "Application submitted!")
                self._stage("submitted", screenshot=self._last_screenshot())
                self._emit(emit, "done", {"dry_run": False})
        else:
            return self._fail(result.get("error", "Submit failed"))
>>>>>>> 4226f10 (.)

        s.save()
        return s

<<<<<<< HEAD
    # ── Helpers ───────────────────────────────────────────────────

=======
    @staticmethod
    def _dry_run_enabled() -> bool:
        """
        Read dry_run from SettingsDB (UI toggle) with .env as fallback.
        SettingsDB is the source of truth set via the Settings page.
        """
        try:
            from db.settings_db import SettingsDB
            return SettingsDB.get().read("dry_run", "false").lower() == "true"
        except Exception:
            return cfg.DRY_RUN

    # ── Helpers ───────────────────────────────────────────────────

    def _stage(self, stage: str, *, reason: str | None = None, note: str = "",
               detail: dict | None = None, screenshot: str = "",
               strict: bool = False):
        """Move the job to `stage` as the agent (no-op if it's already
        there). strict=True lets an illegal move raise — used before a run
        starts, so a job that can't be worked on never launches a browser.
        Later moves only log: a finished run must not be lost to a
        bookkeeping error."""
        try:
            if self.jobs_db.get_stage(self.job_id) == stage:
                return
            self.jobs_db.transition(self.job_id, stage, actor="agent", reason=reason,
                                    note=note, detail=detail, screenshot=screenshot)
        except (ValueError, LookupError) as e:
            if strict:
                raise
            logger.error(f"[Session {self.job_id[:8]}] lifecycle → {stage} failed: {e}")

    @staticmethod
    def _settings():
        from db.settings_db import SettingsDB
        return SettingsDB.get()

    def _answers_kw(self, profile: dict | None) -> dict:
        """{"answers": AnswerContext} for the form code, or {} if the bank can't
        be opened: a problem with the bank must never stop an application.
        The bank is the JOB OWNER'S (legacy unowned rows belong to the first
        account, as everywhere), kept in the same database file as the jobs; the
        profile's facts are offered only to the profile's owner."""
        try:
            from applier.answers import AnswerContext
            from db.answer_bank_db import AnswerBank, PROFILE_OWNER_ID
            uid = self.job.get("user_id") or PROFILE_OWNER_ID
            flat = (profile or {}).get("_answered") if uid == PROFILE_OWNER_ID else None
            return {"answers": AnswerContext(AnswerBank(uid, self.jobs_db.path),
                                             self.job.get("company", ""),
                                             profile=flat, state=self.session)}
        except Exception:
            logger.exception(f"[Session {self.job_id[:8]}] answer bank unavailable; continuing without it")
            return {}

    def _step(self, code: str, message: str, data: dict | None = None):
        """Record progress on the job's timeline (application_events, kind
        "step"). Bookkeeping must never break a run, so nothing escapes."""
        try:
            self.jobs_db.record_step(self.job_id, code, message, data)
        except Exception as e:
            logger.warning(f"[Session {self.job_id[:8]}] could not record step {code!r}: {e}")

    def _preflight(self) -> bool:
        """The checks that run before ANY browser work (applier/preflight.py).
        On a failure the job goes to skipped with an event saying why and the
        caller must stop; no browser is opened."""
        from applier import preflight
        try:
            result = preflight.run(self.job, self.jobs_db, self._settings())
        except Exception:
            logger.exception(f"[Session {self.job_id[:8]}] pre-flight crashed; continuing")
            return True
        if result.ok:
            return True
        self._stage("skipped", note=result.message,
                    detail={"preflight": result.rule, **result.detail})
        self.session.skip(result.message)
        return False

    def _confirmation(self, field_key: str) -> dict:
        """Extra prompt keys when this question was pre-filled from a saved
        answer whose policy is 'ask': the form already holds the suggestion and
        you are asked to confirm or edit it."""
        c = self.session.bank_confirm.get(field_key)
        if not c:
            return {}
        return {"needs_confirmation": True, "suggested": c["suggested"], "entry_id": c["entry_id"]}

    def _fail(self, reason: str, exc: BaseException | None = None):
        """The run could not finish. Classify why (applier/failures.py): a
        class a person can fix parks the job in needs_you with that reason;
        timeout/unknown go to failed and are what the queue retries once.
        Either way the class is recorded on the event."""
        category = failures.classify(reason, exc)
        detail = {"classified": category, "error": reason[:300]}
        if failures.is_blocked(category):
            self._stage("needs_you", reason=category, note=reason, detail=detail)
            self._step("waiting_for_you", failures.WAITING_MESSAGES[category],
                       {"reason": category})
        else:
            self._stage("failed", note=reason, detail=detail)
        return self.session.fail(reason)

    def _last_screenshot(self) -> str:
        return self.session.screenshots[-1] if self.session.screenshots else ""

>>>>>>> 4226f10 (.)
    def _open_browser(self, playwright):
        from core.browser import launch_browser
        return launch_browser(playwright)

    def _apply_corrections(self, corrections: list[str]):
<<<<<<< HEAD
        if not corrections:
            return
        flat = self.profile_db.get_all()
        correction_text = "\n".join(f"- {c}" for c in corrections)
=======
        """
        Apply user-typed corrections (free text from the review screen)
        to the profile via the LLM.

        SECURITY: the LLM's output is filtered against
        db.profile_db.CORRECTABLE_FIELDS regardless of what it returns.
        Without this, a correction like "also set authorized_to_work to
        No and clear search_positions" — whether typed deliberately or
        smuggled in via a job posting the user pasted — could let the
        LLM rewrite identity/compliance fields or search-wide settings
        from a single per-job correction. Only fields relevant to a
        single application (contact details, CTC, notice period,
        education, work preferences) can be changed this way.
        """
        if not corrections:
            return

        from db.profile_db import CORRECTABLE_FIELDS

        flat = self.profile_db.get_all()
        # Only show the LLM the fields it's allowed to change, so it
        # doesn't get tempted/confused by seeing the full profile.
        editable_flat = {k: v for k, v in flat.items() if k in CORRECTABLE_FIELDS}
        correction_text = "\n".join(f"- {c}" for c in corrections)

        system = (
            "You update a job applicant's profile based on a correction note. "
            f"You may ONLY return keys from this exact set: {sorted(CORRECTABLE_FIELDS)}. "
            "Return ONLY valid JSON {field_key: new_value} for fields that should "
            "change based on the note. If the note doesn't map to any of these "
            "fields, return {}. Never invent new keys."
        )

>>>>>>> 4226f10 (.)
        try:
            from core import llm as LLM
            result = LLM.chat_json(
                messages=[{"role": "user", "content":
<<<<<<< HEAD
                    f"User wants these corrections:\n{correction_text}\n\n"
                    f"Current profile:\n{json.dumps(flat)[:2000]}\n\n"
                    f"Return ONLY JSON {{field_key: new_value}} for fields to update."}],
                max_tokens=512,
            )
            if isinstance(result, dict):
                self.profile_db.set_many(result, source="correction")
=======
                    f"Correction note:\n{correction_text}\n\n"
                    f"Current values (editable fields only):\n{json.dumps(editable_flat)}"}],
                system=system,
                max_tokens=512,
            )
            if isinstance(result, dict):
                # Defense in depth: filter again even though the system
                # prompt already restricts the key set.
                safe = {k: v for k, v in result.items() if k in CORRECTABLE_FIELDS}
                dropped = set(result.keys()) - safe.keys()
                if dropped:
                    logger.warning(f"[Session] Correction tried to set "
                                   f"non-allowlisted fields, dropped: {dropped}")
                if safe:
                    self.profile_db.set_many(safe, source="correction")
>>>>>>> 4226f10 (.)
        except Exception as e:
            logger.warning(f"Correction LLM failed: {e}")

    def _emit_fn(self, emit):
        def fn(event, data):
            self._emit(emit, event, data)
        return fn

    def _emit(self, emit_fn, event: str, data):
        if event == "log" and isinstance(data, str):
            self.session.add_log(data)
<<<<<<< HEAD
=======
        if event == "step" and isinstance(data, dict):
            self._step(str(data.get("step", "")), str(data.get("message", "")),
                       {k: v for k, v in data.items() if k not in ("step", "message")})
>>>>>>> 4226f10 (.)
        if emit_fn:
            try: emit_fn(event, data)
            except Exception: pass
