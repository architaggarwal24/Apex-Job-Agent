"""
applier/session.py — ApplicationSession state machine.

One session per job. Persisted to JSON so the web UI
can resume across requests and server restarts.

States:
  idle → analyzing → filling → awaiting_input →
  refilling → screenshot_review → submitting → done | failed | skipped

These are the fine-grained phases of ONE browser run (what the progress UI
shows). Where the job stands is its lifecycle stage (db/lifecycle.py): the
Orchestrator below moves the job's stage as the run progresses, and the
routes decide what is allowed from the stage, not from this state.
"""

import json
import logging
import random
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from pathlib import Path

from applier import failures
from applier.answers import normalize_question, remember_answer, remember_modes
from applier.site_type import SITE_TYPES
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
    # The answer bank's per-application state (applier/answers.py). This file
    # belongs to ONE job, so what is kept here can never reach another application.
    application_answers: dict   = field(default_factory=dict)   # question_norm -> answer, definitive for THIS application
    bank_used:       list       = field(default_factory=list)   # bank entries already counted as used by it
    bank_confirm:    dict       = field(default_factory=dict)   # field_key -> a saved answer awaiting your confirmation
    site_type:       str        = ""                            # the kind of flow (applier/site_type.py), set when the page is first opened
    filler:          str        = ""                            # who fills the form: an adapter's name, or "universal" (see Orchestrator._filler)
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
        self._stage("preparing", strict=True)
        if not self._preflight():
            return s
        s.transition(State.ANALYZING, "Starting application")
        s.iteration = 1
        s.filler = ""                       # a (re)start chooses its filler afresh
        self._emit(emit, "state", State.ANALYZING.value)

        from playwright.sync_api import sync_playwright

        profile = self.profile_db.summary()

        with sync_playwright() as pw:
            try:
                ctx  = self._open_browser(pw)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
            except Exception as e:
                return self._fail(f"Browser launch failed: {e}", e)

            try:
                s.transition(State.FILLING, "Analyzing and filling form")
                self._emit(emit, "state", State.FILLING.value)

                result = self._run_pass("run_application", page, profile, emit=self._emit_fn(emit),
                                        **self._answers_kw(profile))
                s.screenshots.extend(result.get("screenshots", []))

                if not result["success"] and result.get("error"):
                    ctx.close()
                    return self._fail(result["error"])

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
                return self._fail(str(e), e)

        # Build missing prompts for UI
        pending = self.profile_db.get_pending_missing(self.job_id)
        s.missing_prompts = [
            {"id": r["id"], "field_key": r["field_key"],
             "label": r["field_label"], "field_type": r["field_type"],
             "options": r["options"], "placeholder": r["placeholder_used"],
             **self._confirmation(r["field_key"])}
            for r in pending
        ]

        if s.missing_prompts:
            self._stage("needs_you", reason="question",
                        detail={"missing_fields": [p["label"] for p in s.missing_prompts]})
            self._step("waiting_for_you", failures.WAITING_MESSAGES["question"],
                       {"reason": "question", "count": len(s.missing_prompts)})
            s.transition(State.AWAITING,
                f"Waiting for input — {len(s.missing_prompts)} missing fields")
            self._emit(emit, "awaiting_input", {"missing_fields": s.missing_prompts})
        else:
            self._stage("ready_for_review", screenshot=self._last_screenshot())
            s.transition(State.REVIEW, "Form filled — review screenshot")
            self._emit(emit, "screenshot_review", {"screenshots": s.screenshots})

        s.save()
        if s.state == State.REVIEW.value:
            self._after_review(emit)
        return s

    def run_iter2(self, answers: dict[str, str], emit=None, remember=None):
        """`remember` says what to do with the answers (applier/answers.py:
        auto | global | company | once, or {field_key: flag}); default auto."""
        s = self.session
        self._save_answers(answers, remember_modes(remember, answers))

        self._stage("preparing", strict=True)
        if not self._preflight():
            return s
        s.transition(State.REFILLING, "Filling missing fields (pass 2)")
        s.iteration = 2
        self._emit(emit, "state", State.REFILLING.value)

        missing_keys = [p["field_key"] for p in s.missing_prompts]
        profile = self.profile_db.summary()

        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            try:
                ctx  = self._open_browser(pw)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
            except Exception as e:
                return self._fail(f"Browser launch failed: {e}", e)
            try:
                result = self._run_pass("run_iteration2", page, profile, missing_keys,
                                        emit=self._emit_fn(emit), **self._answers_kw(profile))
                s.screenshots.extend(result.get("screenshots", []))
                ctx.close()
            except Exception as e:
                logger.exception(f"Iter2 error {self.job_id}")
                try: ctx.close()
                except Exception: pass
                return self._fail(str(e), e)

        s.missing_prompts = []
        self._stage("ready_for_review", screenshot=self._last_screenshot())
        s.transition(State.REVIEW, "Pass 2 complete — review screenshot")
        self._emit(emit, "screenshot_review", {"screenshots": s.screenshots})
        s.save()
        self._after_review(emit)
        return s

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

    def submit(self, corrections: list[str] | None = None, emit=None, actor: str = "user"):
        """`actor` is who approved it: "user" (the default), or "autonomy" when
        a rule did (applier/autonomy.py has already recorded that)."""
        s = self.session
        if not self._preflight():          # e.g. it went out another way since the review
            return s
        if actor != "autonomy":
            from applier import autonomy
            autonomy.record_human_approval(self.job, bool(corrections), self.jobs_db.path)
        if corrections:
            s.corrections = corrections
            self._apply_corrections(corrections)

        # Dry run is read from SettingsDB (UI-managed) with .env as fallback.
        # When enabled, the form is filled and screenshotted exactly as
        # normal, but the submit button is never clicked and the job is
        # never marked "applied" — see applier/universal.run_submit().
        dry_run = self._dry_run_enabled()

        s.transition(State.SUBMITTING,
                     "Submitting application" if not dry_run
                     else "DRY RUN — filling form, will NOT submit")
        self._emit(emit, "state", State.SUBMITTING.value)

        profile = self.profile_db.summary()

        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            try:
                ctx  = self._open_browser(pw)
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
            except Exception as e:
                return self._fail(f"Browser launch failed: {e}", e)
            try:
                result = self._run_pass("run_submit", page, profile,
                                        corrections=corrections,
                                        emit=self._emit_fn(emit),
                                        dry_run=dry_run,
                                        **self._answers_kw(profile))
                s.screenshots.extend(result.get("screenshots", []))
                ctx.close()
            except Exception as e:
                logger.exception(f"Submit error {self.job_id}")
                try: ctx.close()
                except Exception: pass
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

        s.save()
        return s

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

    def _adapter(self):
        """The site adapter (applier/adapters) that fills this application, or
        None for the universal filler. Chosen by the detected site type - or what
        the URL alone says, before the page has been opened - on the first pass,
        and kept (session.filler) so every later pass of THIS application uses
        the same filler: a fall back to the universal one is for good, which
        is what makes it happen once."""
        from applier import adapters, autonomy
        s = self.session
        if s.filler == "universal":
            return None
        if s.filler:
            return adapters.get(s.filler)
        return adapters.adapter_for(autonomy.site_type_for(self.job, s.site_type), self.job.get("url", ""))

    def _run_pass(self, name: str, page, profile, *args, **kw) -> dict:
        """Run pass `name` (run_application | run_iteration2 | run_submit) with the
        adapter for this application if there is one, else the universal filler.

        An adapter that meets something it cannot handle raises UnsupportedForm:
        that is put on the timeline and the same pass is run once by the
        universal filler, which then stays this application's filler. If the
        universal filler fails too, its error carries the adapter's too, so it
        classifies as unsupported_form (unless it is a more specific blocker, a
        captcha say) and the job goes to needs_you."""
        from applier import adapters, universal
        from applier.adapters import runner
        s = self.session
        adapter = self._adapter()
        if adapter is not None:
            try:
                result = getattr(runner, name)(adapter, page, self.job, profile, *args, **kw)
                s.filler = adapter.name
                return result
            except adapters.UnsupportedForm as e:
                logger.warning(f"[Session {self.job_id[:8]}] {e}; using the universal filler")
                self._step("adapter_fallback",
                           f"The {adapter.name} adapter could not handle this form ({e.detail}) - using the general filler",
                           {"adapter": adapter.name, "reason": e.detail})
                s.filler = "universal"
                result = getattr(universal, name)(page, self.job, profile, *args, **kw)
                if not result.get("success") and result.get("error"):
                    result = {**result, "error": f"{result['error']} - after the {adapter.name} adapter reported: {e}"}
                return result
        s.filler = "universal"
        return getattr(universal, name)(page, self.job, profile, *args, **kw)

    def _after_review(self, emit=None):
        """The application is at ready_for_review. Put it in the autonomy
        ledger and, if a rule says so (applier/autonomy.py), approve it now
        through the ordinary submit. Bookkeeping or a rule fault must never
        lose the run, so nothing escapes: it just stays for a person."""
        from applier import autonomy
        try:
            site_type = autonomy.site_type_for(self.job, self.session.site_type)
            path = self.jobs_db.path
            autonomy.record_review_reached(self.job, site_type, path)
            if autonomy.consider_auto_approval(self.job, site_type, path=path,
                                               settings=self._settings()) is not None:
                self.submit(None, emit, actor="autonomy")
        except Exception:
            logger.exception(f"[Session {self.job_id[:8]}] autonomy step failed")

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
        from applier import autonomy
        autonomy.record_failure(self.job, autonomy.site_type_for(self.job, self.session.site_type),
                                category, self.jobs_db.path)
        return self.session.fail(reason)

    def _last_screenshot(self) -> str:
        return self.session.screenshots[-1] if self.session.screenshots else ""

    def _open_browser(self, playwright):
        from core.browser import launch_browser
        return launch_browser(playwright)

    def _apply_corrections(self, corrections: list[str]):
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

        try:
            from core import llm as LLM
            result = LLM.chat_json(
                messages=[{"role": "user", "content":
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
        except Exception as e:
            logger.warning(f"Correction LLM failed: {e}")

    def _emit_fn(self, emit):
        def fn(event, data):
            self._emit(emit, event, data)
        return fn

    def _emit(self, emit_fn, event: str, data):
        if event == "log" and isinstance(data, str):
            self.session.add_log(data)
        if event == "site_type":                 # from the form code; bookkeeping only, not forwarded
            if data in SITE_TYPES:
                self.session.site_type = data
            return
        if event == "step" and isinstance(data, dict):
            self._step(str(data.get("step", "")), str(data.get("message", "")),
                       {k: v for k, v in data.items() if k not in ("step", "message")})
        if emit_fn:
            try: emit_fn(event, data)
            except Exception: pass
