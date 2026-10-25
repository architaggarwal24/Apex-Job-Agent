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
from contextlib import contextmanager
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
        if self._apply_mode() == "assist":
            return self._assist_ready(emit)
        self._prepare_kit()
        s.transition(State.ANALYZING, "Starting application")
        s.iteration = 1
        s.filler = ""                       # a (re)start chooses its filler afresh
        self._emit(emit, "state", State.ANALYZING.value)

        profile = self.profile_db.summary()
        result, settled = self._execute("apply", "Iter1", profile, emit, {})
        if settled is not None:
            return settled
        return self._finish_first_pass(result, emit)

    def _finish_first_pass(self, result: dict, emit=None):
        """What happens once the first pass has produced `result` - here, or on a
        runner (finish_pass): record the screenshots, fail or flag what is missing,
        then park the application for your answers or for review."""
        s = self.session
        try:
            s.screenshots.extend(result.get("screenshots", []))

            if not result["success"] and result.get("error"):
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
        except Exception as e:
            logger.exception(f"Iter1 error {self.job_id}")
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

        packet = self._store_packet(result, merge=False)

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
            self._review_ready_step(packet)

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
        if self._apply_mode() == "assist":
            return self._assist_ready(emit)
        s.transition(State.REFILLING, "Filling missing fields (pass 2)")
        s.iteration = 2
        self._emit(emit, "state", State.REFILLING.value)

        missing_keys = [p["field_key"] for p in s.missing_prompts]
        profile = self.profile_db.summary()
        result, settled = self._execute("refill", "Iter2", profile, emit, {"missing_keys": missing_keys})
        if settled is not None:
            return settled
        return self._finish_second_pass(result, emit)

    def _finish_second_pass(self, result: dict, emit=None):
        """The second pass produced `result` (here, or on a runner): back to review."""
        s = self.session
        try:
            s.screenshots.extend(result.get("screenshots", []))
        except Exception as e:
            logger.exception(f"Iter2 error {self.job_id}")
            return self._fail(str(e), e)
        if not result.get("success") and result.get("error"):
            return self._fail(result["error"])            # the packet keeps what the first pass found

        s.missing_prompts = []
        packet = self._store_packet(result, merge=True)
        self._stage("ready_for_review", screenshot=self._last_screenshot())
        s.transition(State.REVIEW, "Pass 2 complete — review screenshot")
        self._emit(emit, "screenshot_review", {"screenshots": s.screenshots})
        self._review_ready_step(packet)
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
        if self._apply_mode() == "assist":
            self._step("assist_submit_blocked", "Assist mode: the agent does not submit - apply on the "
                                                "site yourself, then mark it applied")
            return s
        if not self._preflight():          # e.g. it went out another way since the review
            return s
        if actor != "autonomy":
            from applier import autonomy
            autonomy.record_human_approval(self.job, bool(corrections), self.jobs_db.path)
        if corrections:
            s.corrections = corrections
            self._apply_corrections(corrections)
        self._use_reviewed_values(only_user=bool(corrections))

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
        result, settled = self._execute("submit", "Submit", profile, emit,
                                        {"corrections": corrections, "dry_run": dry_run})
        if settled is not None:
            return settled
        return self._finish_submit(result, dry_run, emit)

    def _finish_submit(self, result: dict, dry_run: bool, emit=None):
        """The submit pass produced `result` (here, or on a runner): record it."""
        s = self.session
        try:
            s.screenshots.extend(result.get("screenshots", []))
        except Exception as e:
            logger.exception(f"Submit error {self.job_id}")
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

    # ── the review packet (applier/review.py, db/review_db.py) ────

    def _packet_owner(self) -> int:
        from db.answer_bank_db import PROFILE_OWNER_ID
        return self.job.get("user_id") or PROFILE_OWNER_ID

    def _resume_name(self) -> str:
        """The file name of the resume this application uploads, or "" when there is none."""
        try:
            from applier import kit
            source = kit.resume_for(self.job, self.jobs_db.path)
            if source is not None:
                return source.filename
        except Exception:
            pass
        return cfg.RESUME_PATH.name if Path(cfg.RESUME_PATH).exists() else ""

    def _store_packet(self, result: dict, merge: bool):
        """Record what the form code put on the page as this application's review packet: one
        entry per field, who it came from, how sure, whether to look twice. The first pass
        replaces any earlier packet (a restart starts afresh); a later pass folds into it.
        Whatever goes wrong is logged and the application carries on - without a packet the
        submit simply re-analyses the form. Returns the packet, or None (also when the pass
        reported no fields)."""
        try:
            from applier import review
            from db.review_db import ReviewStore
            uid = self._packet_owner()
            flat = (self.profile_db.summary() or {}).get("_answered") if uid == self._profile_owner() else None
            snapshots = (result.get("review") or {}).get("fields") or []
            if not snapshots:
                return None                    # the form code reported nothing to review: no packet, nothing to announce
            # the runner protocol does not require a field type; the form code always sets one
            fields = review.build_fields([{"type": "text", **s} for s in snapshots if isinstance(s, dict)], flat or {})
            shots = [Path(p).name for p in result.get("screenshots", []) if p]
            url = (result.get("review") or {}).get("url") or self.job.get("url", "")
            store = ReviewStore(uid, self.jobs_db.path)
            old = store.get(self.job_id) if merge else None
            packet = review.build_packet(self.job_id, url, self._resume_name(), shots, fields)
            if old:
                packet["fields"] = review.merge_fields(old["fields"], fields)
                packet["built_at"] = old.get("built_at", packet["built_at"])
            store.save(self.job_id, packet)
            return packet
        except Exception:
            logger.exception(f"[Session {self.job_id[:8]}] could not store the review packet; continuing without it")
            return None

    @staticmethod
    def _profile_owner() -> int:
        from db.answer_bank_db import PROFILE_OWNER_ID
        return PROFILE_OWNER_ID

    def _review_ready_step(self, packet):
        if packet is None:
            return
        n = len(packet["fields"])
        flagged = sum(1 for f in packet["fields"] if f["flagged"])
        self._step("review_ready", f"Review ready: {n} field{'s' if n != 1 else ''}, {flagged} flagged",
                   {"fields": n, "flagged": flagged})

    def _use_reviewed_values(self, only_user: bool):
        """Approving submits what was REVIEWED, not a fresh re-analysis that might read the form
        differently: the packet's values become this application's own answers, which the
        form code puts on the form ahead of anything it works out afresh. With free-text
        corrections (only_user) just the values you set yourself are kept, so the corrections
        can change the rest. No packet (an application reviewed the older way) or one that
        cannot be read: nothing changes and the form is analysed afresh."""
        try:
            from applier import review
            from db.review_db import ReviewStore
            packet = ReviewStore(self._packet_owner(), self.jobs_db.path).get(self.job_id)
            reviewed = review.reviewed_answers(packet, only_user=only_user)
        except Exception:
            logger.exception(f"[Session {self.job_id[:8]}] could not read the review packet; "
                             "the form will be analysed afresh")
            return
        if reviewed:
            self.session.application_answers.update(reviewed)
            self.session.save()

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

    # ── the executor seam ─────────────────────────────────────────

    def _executor(self):
        """Who does the browser work for this application (applier/executors.py):
        this machine, or a runner on the user's own computer. Today's behaviour
        (the server) if anything about the choice cannot be read."""
        from applier import executors
        from db.answer_bank_db import PROFILE_OWNER_ID
        try:
            return executors.choose(self._settings(), self.job.get("user_id") or PROFILE_OWNER_ID,
                                    self.jobs_db.path)
        except Exception:
            logger.exception(f"[Session {self.job_id[:8]}] could not choose an executor; using the server")
            return executors.ServerExecutor()

    def _before_pass(self, kind: str, emit=None):
        """Called by the executor once it is ready to start the pass."""
        if kind == "apply":
            self.session.transition(State.FILLING, "Analyzing and filling form")
            self._emit(emit, "state", State.FILLING.value)

    def _execute(self, kind: str, label: str, profile, emit, params: dict):
        """Run the browser part of a pass through the executor. Returns
        (result, None) when there is a result to finish, or (None, session) when
        the pass is already settled for now: failed, or LEFT FOR A RUNNER (the
        runner's report resumes it through finish_pass)."""
        from applier import executors
        try:
            outcome = self._executor().execute(self, kind, profile, emit, params)
        except executors.BrowserLaunchError as e:
            return None, self._fail(f"Browser launch failed: {e.original}", e.original)
        except Exception as e:
            logger.exception(f"{label} error {self.job_id}")
            return None, self._fail(str(e), e)
        if outcome.deferred:
            return None, self.session
        return outcome.result, None

    def finish_pass(self, kind: str, result: dict, params: dict | None = None, emit=None):
        """Continue an application whose pass ran somewhere else (a runner): the
        steps run_iter1 / run_iter2 / submit take once the form code has returned
        `result`. A failure the runner reported fails the application, whatever the pass."""
        params = params or {}
        if not result.get("success") and result.get("error"):
            self.session.screenshots.extend(result.get("screenshots", []))
            return self._fail(result["error"])
        if kind == "apply":
            return self._finish_first_pass(result, emit)
        if kind == "refill":
            return self._finish_second_pass(result, emit)
        if kind == "submit":
            # what the runner says it did decides: a dry run it reports is not a submission,
            # whatever the task asked for; with no word on it, the task's setting stands
            dry_run = bool(result["dry_run"]) if "dry_run" in result else bool(params.get("dry_run"))
            return self._finish_submit(result, dry_run, emit)
        raise ValueError(f"unknown pass kind {kind!r}")

    def _prepare_kit(self):
        """Prepare this application's kit (applier/kit.py: which resume, a short
        summary, a cover letter) during `preparing`. A kit failure NEVER blocks
        the application, so nothing escapes: without a kit the fillers upload
        the user's default resume. The profile is offered only to its owner."""
        try:
            from applier import kit
            kit.prepare_kit(self.job, path=self.jobs_db.path, profile=self._owner_profile())
        except Exception:
            logger.exception(f"[Session {self.job_id[:8]}] could not prepare the kit; continuing without it")

    def _owner_profile(self) -> dict | None:
        """The profile summary, but only for the profile's owner (None for anyone else)."""
        from db.answer_bank_db import PROFILE_OWNER_ID
        uid = self.job.get("user_id") or PROFILE_OWNER_ID
        return self.profile_db.summary() if uid == PROFILE_OWNER_ID else None

    def _apply_mode(self) -> str:
        """"auto" or "assist" for this application (applier/apply_mode.py), by the
        site type the URL gives - no page is open yet. When in doubt: assist."""
        try:
            from applier import apply_mode, autonomy
            site_type = autonomy.site_type_for(self.job, self.session.site_type)
            return apply_mode.mode_for(self._settings(), site_type)
        except Exception:
            logger.exception(f"[Session {self.job_id[:8]}] could not read the apply mode; using assist")
            return "assist"

    def _assist_ready(self, emit=None):
        """ASSIST MODE: no browser, no site contact. Prepare the kit (LLM only),
        build the answer pack to be sure it can be built, and park the job at
        ready_for_review for YOU to apply by hand and mark it (applier/assist.py).
        Autonomy never sees it: nothing is auto-approved and nothing reaches the
        ledger, because the agent filled nothing."""
        s = self.session
        s.iteration = 1
        self._prepare_kit()
        fields = 0
        try:
            from applier import pack
            fields = len(pack.build_pack(self.job, path=self.jobs_db.path, profile=self._owner_profile())["fields"])
        except Exception:
            logger.exception(f"[Session {self.job_id[:8]}] could not build the answer pack")
        self._step("assist_ready", f"Assist mode: {fields} fields and your kit are ready - apply on the site "
                                   "yourself, then mark it applied", {"fields": fields})
        s.transition(State.REVIEW, "Assist mode — prepared for you to apply by hand")
        self._stage("ready_for_review", note="assist mode", detail={"assist": True})
        self._emit(emit, "state", State.REVIEW.value)
        s.save()
        return s

    @contextmanager
    def _resume_file(self):
        """Yields the path of the resume the form code should upload - the kit's
        variant (else the user's default), decrypted into a temp file - or None
        (the single resume.pdf, as before) when there is none or it cannot be
        read. The temp file lives for the WHOLE pass that fills and submits the
        form, because the browser reads an uploaded file when the form is sent,
        and is deleted when the pass ends. A resume that cannot be decrypted is
        put on the timeline; it never stops the application."""
        from contextlib import ExitStack
        from applier import kit
        path = None
        with ExitStack() as stack:
            try:
                source = kit.resume_for(self.job, self.jobs_db.path)
                if source is not None:
                    path = stack.enter_context(source.materialize())
            except Exception as e:
                logger.error(f"[Session {self.job_id[:8]}] chosen resume unavailable ({type(e).__name__}); using resume.pdf")
                self._step("resume_unavailable", "The chosen resume could not be read - using the single resume file instead")
            yield path

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
        """Run pass `name` with the kit's resume decrypted for the pass (see
        _resume_file), through _dispatch."""
        with self._resume_file() as resume:
            if resume is not None:
                kw = {**kw, "resume_path": resume}
            return self._dispatch(name, page, profile, args, kw)

    def _dispatch(self, name: str, page, profile, args: tuple, kw: dict) -> dict:
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
