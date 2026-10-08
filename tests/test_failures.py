# tests/test_failures.py
"""
tests/test_failures.py — the failure classification table (applier/failures.py).

Run from the project root:   python3 -m unittest discover -s tests -v

The samples below are the messages the apply flow REALLY produces. The ones
that are fixed strings are also checked against the source of the code that
emits them, so changing a message there without revisiting this table makes a
test fail instead of quietly turning a blocker into "unknown".
"""

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
os.environ.setdefault("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())

from applier import failures
from db import lifecycle

ROOT = Path(__file__).resolve().parent.parent

# (message, expected class, file that emits it - or None for an f-string/external message)
SAMPLES = [
    # captcha
    ("CAPTCHA detected — skipping", "captcha", "applier/universal.py"),
    ("CAPTCHA after clicking apply", "captcha", "applier/universal.py"),
    ("Please verify you are human to continue", "captcha", None),
    ("Our systems have detected unusual traffic from your network", "captcha", None),
    # login_required
    ("Login wall — page requires authentication and no login was detected", "login_required",
     "applier/universal.py"),
    ("Sign in required to view this job", "login_required", None),
    ("Your session has expired, please log in again", "login_required", None),
    # naukri_chat_screening
    ("Naukri opened a follow-up screening chat after Apply — this isn't automated yet. "
     "Please finish answering it manually in the browser window; the job has NOT been "
     "marked as applied.", "naukri_chat_screening", None),
    ("Naukri chatbot questionnaire appeared", "naukri_chat_screening", None),
    # job_closed
    ("No Apply button found — job may already be applied to, expired, or removed", "job_closed",
     "applier/universal.py"),
    ("This job is no longer available", "job_closed", None),
    ("No longer accepting applications", "job_closed", None),
    ("The position has been filled", "job_closed", None),
    # validation_error
    ("Validation error — the site rejected the answers: Enter a valid email address",
     "validation_error", None),
    ("Phone number: please enter a valid phone number", "validation_error", None),
    ("This is a required field", "validation_error", None),
    # unsupported_form
    ("Could not find submit button", "unsupported_form", "applier/universal.py"),
    ("Could not find submit button (dry run)", "unsupported_form", "applier/universal.py"),
    ("No Apply button found", "unsupported_form", None),
    ("This form is not supported yet", "unsupported_form", None),
    # question
    ("Missing required fields: expected CTC", "question", None),
    ("Awaiting your input", "question", None),
    # timeout
    ("Navigation failed: Page.goto: Timeout 30000ms exceeded.", "timeout", None),
    ("Browser launch failed: Timeout 180000ms exceeded", "timeout", None),
    ("net::ERR_CONNECTION_TIMED_OUT at https://example.com/job/1", "timeout", None),
    ("The operation timed out", "timeout", None),
    # unknown
    ("Browser launch failed: Executable doesn't exist at /root/.cache/ms-playwright", "unknown", None),
    ("net::ERR_NAME_NOT_RESOLVED", "unknown", None),
    ("Unexpected token: invalid selector 'a[[x'", "unknown", None),
    ("sometimes output is odd", "unknown", None),
    ("", "unknown", None),
]


class ClassificationTableTests(unittest.TestCase):
    def test_every_sample_lands_in_its_class(self):
        for message, expected, _ in SAMPLES:
            with self.subTest(message=message[:70]):
                self.assertEqual(failures.classify(message), expected)

    def test_fixed_strings_still_exist_in_the_code_that_emits_them(self):
        for message, _, rel in SAMPLES:
            if rel is None:
                continue
            with self.subTest(message=message[:70]):
                source = (ROOT / rel).read_text(encoding="utf-8")
                # compare with string-literal continuations and quoting flattened away
                flat = re.sub(r'"\s*\n\s*"', "", source)
                self.assertIn(message.replace("—", "—"), flat,
                              "the emitting code changed: update the table and this sample")

    def test_every_class_is_covered_by_a_sample(self):
        self.assertEqual({c for _, c, _ in SAMPLES} | {"unknown"}, set(failures.CATEGORIES))

    def test_the_nine_classes_are_exactly_the_specified_ones(self):
        self.assertEqual(set(failures.CATEGORIES), {
            "captcha", "login_required", "question", "unsupported_form", "naukri_chat_screening",
            "validation_error", "job_closed", "timeout", "unknown"})
        self.assertEqual(len(failures.CATEGORIES), 9)

    def test_blocked_and_retryable_partition_the_classes(self):
        self.assertEqual(set(failures.RETRYABLE), {"timeout", "unknown"})
        self.assertEqual(set(failures.BLOCKED) | set(failures.RETRYABLE), set(failures.CATEGORIES))
        self.assertFalse(set(failures.BLOCKED) & set(failures.RETRYABLE))
        for cat in failures.CATEGORIES:
            self.assertEqual(failures.is_blocked(cat), cat in failures.BLOCKED)
            self.assertEqual(failures.is_retryable(cat), cat in failures.RETRYABLE)

    def test_every_blocking_class_is_a_legal_needs_you_reason(self):
        for cat in failures.BLOCKED:
            self.assertIn(cat, lifecycle.BLOCKED_REASONS, cat)
            lifecycle.check("preparing", "needs_you", cat)          # does not raise
        for cat in failures.RETRYABLE:
            self.assertNotIn(cat, lifecycle.BLOCKED_REASONS, cat)

    def test_every_blocking_class_has_a_waiting_message(self):
        self.assertEqual(set(failures.WAITING_MESSAGES), set(failures.BLOCKED))
        self.assertTrue(all(m.startswith("Waiting for you") for m in failures.WAITING_MESSAGES.values()))

    def test_every_rule_in_the_table_can_fire_and_only_known_classes_appear(self):
        fired = {failures.classify(m) for m, _, _ in SAMPLES}
        for category, _ in failures.RULES:
            self.assertIn(category, failures.CATEGORIES)
            self.assertIn(category, fired, f"no sample reaches the {category} rule")
        self.assertNotIn("unknown", [c for c, _ in failures.RULES], "unknown is the fallback, not a rule")

    # ── precedence: the order of the table is the policy ──────────

    def test_precedence_between_overlapping_messages(self):
        c = failures.classify
        # a Naukri chat message also talks about applying and a browser window
        self.assertEqual(c("Naukri screening chat after Apply; job has NOT been marked as applied"),
                         "naukri_chat_screening")
        # "No Apply button" alone is a form problem; with the closed hint it is a closed job
        self.assertEqual(c("No Apply button found"), "unsupported_form")
        self.assertEqual(c("No Apply button found — job may already be applied to, expired, or removed"),
                         "job_closed")
        # a captcha at a login page is a captcha first; a login problem that timed out is still a login problem
        self.assertEqual(c("CAPTCHA shown on the login wall"), "captcha")
        self.assertEqual(c("Login wall — timed out waiting for sign in"), "login_required")
        # "missing required fields" is the agent lacking an answer; "is a required field" is the site complaining
        self.assertEqual(c("Missing required fields: expected CTC"), "question")
        self.assertEqual(c("Expected CTC is a required field"), "validation_error")
        # a timeout is only a timeout when nothing more specific matched
        self.assertEqual(c("CAPTCHA check timed out"), "captcha")

    def test_loose_look_alikes_are_not_misfiled(self):
        for text in ("invalid selector", "Executable doesn't exist", "TypeError: 'NoneType' object is not subscriptable",
                     "expired certificate", "sometimes out of memory", "required positional argument"):
            with self.subTest(text=text):
                self.assertEqual(failures.classify(text), "unknown")

    # ── exceptions ────────────────────────────────────────────────

    def test_exceptions_are_classified_by_type_name_and_message(self):
        class TimeoutError(Exception):               # what playwright raises is named like this
            pass
        self.assertEqual(failures.classify("", TimeoutError("waiting for locator")), "timeout")
        self.assertEqual(failures.classify("", TimeoutError()), "timeout")
        self.assertEqual(failures.classify("", ValueError("boom")), "unknown")
        self.assertEqual(failures.classify("boom", ConnectionError("reset by peer")), "unknown")
        self.assertEqual(failures.classify("", RuntimeError("CAPTCHA detected")), "captcha")
        self.assertEqual(failures.classify("outer text", OSError("Timeout 30000ms exceeded")), "timeout")

    def test_classify_never_raises_and_handles_odd_input(self):
        for odd in (None, 0, 12.5, b"bytes", ["a"], "x" * 200_000):
            self.assertIn(failures.classify(odd), failures.CATEGORIES)

    def test_matching_is_case_insensitive(self):
        self.assertEqual(failures.classify("captcha DETECTED"), "captcha")
        self.assertEqual(failures.classify("TIMEOUT 5000MS EXCEEDED"), "timeout")


if __name__ == "__main__":
    unittest.main(verbosity=2)
