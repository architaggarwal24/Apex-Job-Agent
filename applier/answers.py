# applier/answers.py
"""
Putting the answer bank to work while an application is being filled.

AnswerContext.apply(analysis) runs on each analysed form page, after the LLM
has mapped the fields and before anything is filled or anyone is asked. For
each question it looks, in this order, at:

  1. this APPLICATION's own answers - what you answered, confirmed or edited
     for this very application (and once-entries already spent on it);
  2. the bank - your saved answers, then the read-only profile facts.

and acts on what it finds according to the entry's policy:

  always   fill the field with the answer;
  ask      fill it AS A SUGGESTION and mark it "needs confirmation": the field
           becomes a question for you with the answer pre-typed (the job parks
           in needs_you/question until you confirm or edit it);
  once     use it for THIS application only: fill it, move the answer into the
           application's own answers (so the refill and submit passes still
           have it) and delete the entry - it is not kept in the bank.

A profile fact only fills a field the LLM left empty: the LLM already sees the
whole profile, and the bank must not fight it. Everything else the bank knows
beats the LLM's guess and saves asking you.

remember_answer() is the other direction: an answer you give or edit during
review is saved to the bank per the REMEMBER flag.

State that must outlive one pass (this application's answers, which entries
were already counted as used, which suggestions await confirmation) lives on
`state` - the ApplicationSession, whose JSON file belongs to this job alone -
and is saved the moment it changes, so a crash can't lose a spent once-answer.

Plain Python, no browser: tested against fake analyses.
"""

import logging

from db.answer_bank_db import (FUZZY_THRESHOLD, AnswerBank, compatible, names_company,
                               normalize_question, similarity)

logger = logging.getLogger(__name__)

REMEMBER_MODES = ("auto", "global", "company", "once")
LONG_ANSWER_CHARS = 200          # an answer this long (or any textarea) is "long free text"

_TRUE  = {"auto", "default", "true", "yes", "on", "remember"}
_FALSE = {"once", "false", "no", "off", "never", "none"}


# ── the remember flag ─────────────────────────────────────────────

def parse_remember(value) -> str:
    """One remember flag -> 'auto' | 'global' | 'company' | 'once'.
    None/True/'auto'/'yes' -> auto (global, or company-scoped by the rules in
    choose_scope); False/'once'/'no'/'never' -> once (use for this
    application only, store nothing). ValueError for anything else."""
    if value is None or value is True:
        return "auto"
    if value is False:
        return "once"
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _TRUE:
            return "auto"
        if v in _FALSE:
            return "once"
        if v in ("global", "company"):
            return v
    raise ValueError("remember must be one of auto, global, company, once "
                     "(or true/false), or an object mapping field keys to those")


def remember_modes(remember, keys) -> dict[str, str]:
    """The mode for each answered field key. `remember` is one flag for all,
    or an object {field_key: flag, "default": flag}. Validates EVERYTHING in
    it (a typo'd key's value too) so a bad flag is refused before anything
    is saved."""
    keys = list(keys)
    if isinstance(remember, dict):
        default = parse_remember(remember.get("default", remember.get("*")))
        modes = {k: parse_remember(v) for k, v in remember.items() if k not in ("default", "*")}
        return {k: modes.get(k, default) for k in keys}
    mode = parse_remember(remember)
    return {k: mode for k in keys}


def is_long_free_text(field_type: str, answer: str) -> bool:
    return (field_type or "").lower() == "textarea" or len((answer or "").strip()) >= LONG_ANSWER_CHARS


def choose_scope(mode: str, question: str, answer: str, company: str, field_type: str = "") -> str:
    """'global' or 'company' for an answer being remembered. An explicit flag
    wins (company needs a company to scope to). On auto the default is global,
    except when the question NAMES the company ("Why do you want to join
    Acme?") or is long free text - such answers don't travel to other companies."""
    if mode == "global":
        return "global"
    if not (company or "").strip():
        return "global"
    if mode == "company":
        return "company"
    if names_company(question, company) or is_long_free_text(field_type, answer):
        return "company"
    return "global"


def remember_answer(bank: AnswerBank, question: str, answer: str, company: str,
                    mode: str = "auto", field_type: str = "", source: str = "user") -> dict | None:
    """Save an answer you gave or edited. mode 'once' stores nothing (None).
    Saving the same question at the same scope updates the entry in place and
    leaves its policy alone (an answer you set to ask stays ask when you
    correct it); a NEW entry is 'always' - you answered it, so next time it
    just fills. Change the policy on the entry if you want to be asked.
    ValueError if the question or answer can't be stored (blank, no
    meaningful words...)."""
    if mode not in REMEMBER_MODES:
        raise ValueError(f"unknown remember mode {mode!r}")
    if mode == "once":
        return None
    scope = choose_scope(mode, question, answer, company, field_type)
    return bank.add(question, answer, scope=scope, company=company if scope == "company" else "",
                    source=source)


# ── application-level state ───────────────────────────────────────

class MemoryState:
    """Stand-in for the ApplicationSession when there is none (and in tests)."""
    def __init__(self):
        self.application_answers: dict[str, str] = {}
        self.bank_used: list = []
        self.bank_confirm: dict[str, dict] = {}
        self.saves = 0

    def save(self):
        self.saves += 1


def _match_in(qn: str, answers: dict[str, str]) -> tuple[str, float] | None:
    """Best (answer, confidence) for a normalised question among {norm: answer}."""
    if qn in answers:
        return answers[qn], 1.0
    best = None
    for norm, ans in answers.items():
        r = similarity(qn, norm)
        if r >= FUZZY_THRESHOLD and compatible(qn, norm) and (best is None or r > best[1]):
            best = (ans, r)
    return (best[0], round(best[1], 2)) if best else None


def fit_to_options(answer: str, options) -> str | None:
    """An answer for a select/radio must BE one of its options: the option's
    own spelling if it matches (ignoring case/space), else a close match, else
    None - an answer the control can't express is better left to the LLM."""
    ans = (answer or "").strip()
    opts = [str(o).strip() for o in options if str(o).strip()]
    for o in opts:
        if o.casefold() == ans.casefold():
            return o
    an = normalize_question(ans)
    if an:
        close = [(similarity(an, normalize_question(o)), o) for o in opts
                 if normalize_question(o) and compatible(an, normalize_question(o))]
        close = [(r, o) for r, o in close if r >= FUZZY_THRESHOLD]
        if close:
            return max(close)[1]
    return None


class AnswerContext:
    def __init__(self, bank: AnswerBank, company: str, *, profile: dict | None = None, state=None):
        """bank: this user's AnswerBank. company: the job's company (company-
        scoped entries apply only to it). profile: the flat profile dict, only
        for the profile's owner. state: object with application_answers,
        bank_used, bank_confirm and save() (the ApplicationSession)."""
        self.bank, self.company, self.profile = bank, company or "", profile
        self.state = state if state is not None else MemoryState()

    # ── lookup ────────────────────────────────────────────────────

    def _lookup(self, label: str) -> dict | None:
        qn = normalize_question(label)
        if not qn:
            return None
        own = _match_in(qn, self.state.application_answers)
        if own:
            return {"kind": "application", "answer": own[0], "confidence": own[1],
                    "entry_id": None, "source": "user", "policy": "always"}
        m = self.bank.resolve(label, self.company, self.profile)
        return {**m, "kind": "bank"} if m else None

    def _count_use(self, entry_id):
        if isinstance(entry_id, int) and entry_id not in self.state.bank_used:
            self.state.bank_used.append(entry_id)
            self.bank.touch(entry_id)
            return True
        return False

    # ── the policies ──────────────────────────────────────────────

    def apply(self, analysis) -> list[dict]:
        """Apply the bank to every question on an analysed page, mutating the
        fields (db_value / needs_manual). Returns one hit per field it acted
        on: {label, field_key, action, source, policy, confidence, entry_id}
        with action filled | suggested | once. Never raises on one bad field."""
        hits, changed = [], False
        for f in analysis.fields:
            if (f.field_type or "").lower() in ("file", "upload") or not (f.label or "").strip():
                continue
            try:
                hit = self._apply_one(f)
            except Exception:
                logger.exception(f"[AnswerBank] skipped {f.label!r}")
                continue
            if hit:
                hits.append(hit)
                changed = True
        if changed:
            self.state.save()
        return hits

    def _apply_one(self, f) -> dict | None:
        m = self._lookup(f.label)
        if not m:
            return None
        if m["source"] == "profile" and f.db_value:
            return None                                   # the LLM already mapped it from the profile
        answer = m["answer"]
        if (f.field_type or "").lower() in ("select", "radio") and f.options:
            answer = fit_to_options(answer, f.options)
            if answer is None:
                return None
        qn = normalize_question(f.label)
        hit = {"label": f.label, "field_key": f.field_key, "action": "filled",
               "source": "application" if m["kind"] == "application" else m["source"],
               "policy": m["policy"], "confidence": m["confidence"], "entry_id": m["entry_id"]}

        if m["kind"] == "application":
            f.db_value, f.needs_manual = answer, False
            self.state.bank_confirm.pop(f.field_key, None)
            return hit

        policy = m["policy"]
        if policy == "ask":
            f.db_value, f.needs_manual = answer, True
            f.field_key = f.field_key or "_".join(qn.split())[:60]
            self.state.bank_confirm[f.field_key] = {
                "question": f.label, "suggested": answer, "entry_id": m["entry_id"]}
            self._count_use(m["entry_id"])
            return {**hit, "action": "suggested", "field_key": f.field_key}

        f.db_value, f.needs_manual = answer, False
        if policy == "once" and isinstance(m["entry_id"], int):
            # For THIS application only: keep the answer with the application
            # (so the later passes still have it) and drop the entry.
            self.state.application_answers[qn] = answer
            self.state.save()                             # before the delete: a crash must not lose it
            self.bank.delete(m["entry_id"])
            return {**hit, "action": "once"}
        self._count_use(m["entry_id"])
        return hit


def summarize(hits: list[dict]) -> dict:
    """Counts for the timeline: {filled, suggested, once}."""
    out = {"filled": 0, "suggested": 0, "once": 0}
    for h in hits:
        out[h["action"]] += 1
    return out
