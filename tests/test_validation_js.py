# tests/test_validation_js.py
"""
tests/test_validation_js.py — the JavaScript that spots "the site rejected the
answers" (applier/universal.py: _VALIDATION_JS) and the "Step 2 of 4" reader,
run against REAL HTML in a REAL browser. A fake page can't run JavaScript, so
this is the only way to know the selector does what the comment claims -
above all that it does NOT mistake a banner or toast for a validation error,
which would park a good application waiting for a person.

Run from the project root:   python3 -m unittest discover -s tests -v

Skipped automatically when Playwright or a Chromium for it isn't available.
No network: pages are built with set_content().
"""

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
os.environ.setdefault("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())

from applier import universal


class ValidationJsTests(unittest.TestCase):
    """Starts one Chromium for the class (not at import, so merely discovering
    this file costs nothing) and skips if Playwright or a browser is missing."""

    @classmethod
    def setUpClass(cls):
        cls._pw = cls._browser = None
        try:
            from playwright.sync_api import sync_playwright
            cls._pw = sync_playwright().start()
            cls._browser = cls._pw.chromium.launch()
        except Exception as e:
            cls._stop()
            raise unittest.SkipTest(f"no usable Chromium for Playwright ({type(e).__name__})")

    @classmethod
    def _stop(cls):
        for closer in (getattr(cls._browser, "close", None), getattr(cls._pw, "stop", None)):
            try:
                if closer:
                    closer()
            except Exception:
                pass
        cls._pw = cls._browser = None

    @classmethod
    def tearDownClass(cls):
        cls._stop()

    def setUp(self):
        self.page = self._browser.new_page()
        self.addCleanup(self.page.close)

    def errors(self, html):
        self.page.set_content(f"<html><body>{html}</body></html>")
        return universal._validation_errors(self.page)

    def test_a_field_marked_invalid_is_reported_by_its_label_or_described_message(self):
        self.assertEqual(self.errors('<input aria-invalid="true" aria-label="Email">'), ["Email"])
        self.assertEqual(self.errors(
            '<input aria-invalid="true" aria-describedby="e1"><p id="e1">Enter a valid phone number</p>'),
            ["Enter a valid phone number"])
        self.assertEqual(self.errors('<input aria-invalid="true" name="ctc">'), ["ctc"])

    def test_the_usual_inline_error_messages_are_reported(self):
        for html, text in [
            ('<div class="artdeco-inline-feedback--error">Please enter a valid answer</div>', "Please enter a valid answer"),
            ('<span data-test-form-element-error-message>Required</span>', "Required"),
            ('<div class="invalid-feedback">Too short</div>', "Too short"),
            ('<p class="field-error">Pick one</p>', "Pick one"),
            ('<p class="form-error">Fix the form</p>', "Fix the form"),
            ('<p class="error-message">Nope</p>', "Nope"),
        ]:
            with self.subTest(html=html):
                self.assertEqual(self.errors(html), [text])

    def test_banners_toasts_and_alerts_are_NOT_validation_errors(self):
        for html in ('<div role="alert">Your session will expire soon</div>',
                     '<div role="status">Saved</div>',
                     '<div class="toast">Cookie preferences saved</div>',
                     '<input aria-invalid="false" aria-label="Email">',
                     '<input aria-label="Email" required>',
                     '<p>An error occurred in a previous life</p>'):
            with self.subTest(html=html):
                self.assertEqual(self.errors(html), [])

    def test_hidden_or_empty_error_elements_are_ignored(self):
        for html in ('<div class="invalid-feedback" style="display:none">Hidden message</div>',
                     '<div class="invalid-feedback" style="visibility:hidden">Hidden message</div>',
                     '<div class="invalid-feedback" style="width:0;height:0;overflow:hidden">x</div>',
                     '<input aria-invalid="true" aria-label="Email" style="display:none">',
                     '<input aria-invalid="true" aria-label="Email" style="visibility:hidden">',
                     '<div style="visibility:hidden"><div class="invalid-feedback">Inherited hidden</div></div>',
                     '<div class="invalid-feedback" style="padding:10px"></div>',          # visible but empty
                     '<div class="invalid-feedback">   </div>'):
            with self.subTest(html=html):
                self.assertEqual(self.errors(html), [])

    def test_at_most_five_are_returned_and_long_text_is_cut(self):
        many = "".join(f'<div class="invalid-feedback">problem {i}</div>' for i in range(9))
        self.assertEqual(self.errors(many), [f"problem {i}" for i in range(5)])
        self.assertEqual([len(t) for t in self.errors(f'<div class="invalid-feedback">{"x" * 500}</div>')], [120])

    def test_a_clean_form_reports_nothing(self):
        self.assertEqual(self.errors('<form><input aria-label="Name"><select><option>1</option></select></form>'), [])

    def test_step_x_of_y_is_read_from_a_real_page(self):
        for text, want in [("Step 2 of 4 — Contact info", (2, 4)), ("Page 3/5", (3, 5)),
                           ("Step 9 of 3", (1, None)), ("Welcome. Apply below.", (1, None))]:
            with self.subTest(text=text):
                self.page.set_content(f"<html><body><h2>{text}</h2></body></html>")
                self.assertEqual(universal._form_progress(self.page, 0), want)


if __name__ == "__main__":
    unittest.main(verbosity=2)
