# applier/failures.py
"""
Every way an application attempt can go wrong is put in exactly ONE of nine
classes, by classify() below. What happens next depends only on the class:

  blocked  (needs_you, with the class as the reason - a person can unblock it)
      captcha                solve it / the site wants a human
      login_required         log in to the site
      question               the form needs an answer the profile doesn't have
      unsupported_form       the form isn't one the agent can drive
      naukri_chat_screening  Naukri's chat-style screening questionnaire
      validation_error       the site rejected the answers it was given
      job_closed             the posting is gone

  retryable (failed; the queue worker retries ONCE after a backoff)
      timeout                the page/browser was too slow - often transient
      unknown                anything we can't name - worth one more go

The queue never stops on a blocker: the job is parked and the next one runs.

THE TABLE is RULES: ordered, first match wins, so the order IS the precedence
(a Naukri chat message also says "applied"; "No Apply button found" is
unsupported_form on its own but job_closed when the message goes on to say the
job "may already be applied to, expired, or removed"). It is matched against
the error text plus the exception's type name, so a Playwright TimeoutError
classifies as a timeout even when its message is vague.

Patterns are deliberately specific. A loose one ("invalid", "expired") would
misfile an unrelated error - say a bad selector - as something that needs a
person, and the job would sit there waiting for nothing.
"""

import re

CATEGORIES = ("captcha", "login_required", "question", "unsupported_form",
              "naukri_chat_screening", "validation_error", "job_closed",
              "timeout", "unknown")

RETRYABLE = ("timeout", "unknown")
BLOCKED   = tuple(c for c in CATEGORIES if c not in RETRYABLE)

# (category, regex) - first match wins. Case-insensitive.
RULES: tuple[tuple[str, str], ...] = (
    ("naukri_chat_screening",
     r"screening chat|chat-style screening|naukri.{0,40}(chatbot|questionnaire)|chatbot (flow|screening)"),
    ("captcha",
     r"captcha|are you a (human|robot)|verify (that )?you(?:'re| are) (a )?human|unusual traffic"),
    ("login_required",
     r"login wall|log ?in required|sign[ -]?in required|requires? authentication"
     r"|please (log|sign) ?in|session (has )?expired|not (logged|signed) in"),
    ("job_closed",
     r"expired, or removed"
     r"|no longer (open|available|accepting|active)"
     r"|\bjob (is |has )?(expired|closed|been (removed|filled))"
     r"|\b(position|posting|vacancy|listing|role) (is |has )?(been )?(closed|expired|filled|removed)"),
    ("question",
     r"missing (required )?(fields?|answers?)|needs? (your )?(answers?|input)|awaiting (your )?input"),
    ("validation_error",
     r"validation (error|failed)|failed validation|rejected (the|your) (answers|input|details)"
     r"|\bis a required field|required field|please (enter|select|provide|choose) an? valid"
     r"|invalid (email|phone|input|value|format|answer|number|date)"),
    ("unsupported_form",
     r"could ?n[o']t find (a |the )?submit|no (submit|apply) button|unsupported[ _]?(form|flow|site)"
     r"|form (is )?not supported|not supported (yet|by)"),
    ("timeout",
     r"(?<![a-z])time[sd]?[ _]?out|deadline exceeded"),
)

_COMPILED = tuple((cat, re.compile(rx, re.I)) for cat, rx in RULES)


def classify(error: str = "", exc: BaseException | None = None) -> str:
    """The class of a failure: one of CATEGORIES. `error` is the message
    (any text), `exc` the exception if there was one. Never raises."""
    haystack = " ".join(p for p in (
        type(exc).__name__ if exc is not None else "",
        str(exc) if exc is not None else "",
        str(error or ""),
    ) if p)
    for category, rx in _COMPILED:
        if rx.search(haystack):
            return category
    return "unknown"


def is_blocked(category: str) -> bool:
    """A person can unblock it: park the job in needs_you."""
    return category in BLOCKED


def is_retryable(category: str) -> bool:
    return category in RETRYABLE


# What the timeline says when a job is parked, per class.
WAITING_MESSAGES = {
    "captcha":               "Waiting for you: a captcha needs solving",
    "login_required":        "Waiting for you: log in to the site",
    "question":              "Waiting for you: answers needed",
    "unsupported_form":      "Waiting for you: this form can't be automated",
    "naukri_chat_screening": "Waiting for you: finish Naukri's screening chat by hand",
    "validation_error":      "Waiting for you: the site rejected the answers",
    "job_closed":            "Waiting for you: the job looks closed",
}
