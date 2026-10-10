# tests/test_adapters.py
"""
tests/test_adapters.py — site adapters (applier/adapters/): Greenhouse and Lever
fill their forms deterministically instead of through the LLM filler.

  * the registry: an adapter is one file plus one registry line, a broken entry
    is skipped, the first adapter that can_handle wins;
  * the standard questions (pure): recognised by label, answered from the
    profile, work authorisation never answered for another country;
  * each adapter against a saved HTML fixture of a Greenhouse-style and a
    Lever-style application form, in a real Chromium: which fields are found,
    which values end up in the page, the resume upload, what is left blank,
    what is asked, what is refused as unsupported, dry-run vs real submit;
  * the runner (the three passes, over a fake network that serves the fixture);
  * the Orchestrator: it picks the adapter by site type, else the universal
    filler; an UnsupportedForm falls back to the universal filler once, and if
    that fails too the job goes to needs_you as unsupported_form.

LIVE SITES WERE NOT TESTED. The fixtures in tests/fixtures are hand-written
stand-ins for the markup these ATSes use, not captures of real pages, so a pass
here shows the adapters do what they are meant to on that markup - not that a
real Greenhouse or Lever page matches it.

Run from the project root:   python3 -m unittest discover -s tests -v
Needs a Chromium that Playwright can launch for the browser classes (they skip
without one); no network.
"""

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cryptography.fernet import Fernet
os.environ.setdefault("APP_ENCRYPTION_KEY", Fernet.generate_key().decode())

import applier.adapters as adapters
from applier import universal
from applier.adapters import form, runner, standard
from applier.adapters.base import Adapter, UnsupportedForm
from applier.adapters.greenhouse import GreenhouseAdapter
from applier.adapters.lever import LeverAdapter
from applier.answers import AnswerContext, MemoryState
from applier.failures import classify
from core.config import cfg
from db.answer_bank_db import AnswerBank, normalize_question
from db.jobs_db import JobsDB

FIXTURES = Path(__file__).resolve().parent / "fixtures"
GREENHOUSE_HTML = (FIXTURES / "greenhouse_application.html").read_text(encoding="utf-8")
LEVER_HTML = (FIXTURES / "lever_application.html").read_text(encoding="utf-8")

GH_URL    = "https://boards.greenhouse.io/acme/jobs/4012345"
LEVER_URL = "https://jobs.lever.co/acme/ab12cd34-0000-4aaa-8bbb-1234567890ab"

PROFILE = {"_flat": {
    "full_name": "Priya Sharma", "first_name": "Priya", "last_name": "Sharma",
    "email": "priya@example.com", "phone": "+91 98765 43210",
    "linkedin_url": "https://linkedin.com/in/priya", "github_url": "https://github.com/priya",
    "portfolio_url": "https://priya.dev", "authorized_to_work": "Yes", "require_sponsorship": "No",
    "location_country": "India"}}


# ── the registry ──────────────────────────────────────────────────

class RegistryTests(unittest.TestCase):
    def tearDown(self):
        adapters.reload()

    def test_the_two_adapters_are_registered(self):
        self.assertEqual([a.name for a in adapters.all_adapters()], ["greenhouse", "lever"])

    def test_the_adapter_for_a_site_type(self):
        self.assertIsInstance(adapters.adapter_for("greenhouse", GH_URL), GreenhouseAdapter)
        self.assertIsInstance(adapters.adapter_for("lever", LEVER_URL), LeverAdapter)

    def test_every_other_site_type_has_no_adapter(self):
        for st in ("generic", "workday", "ashby", "smartrecruiters", "linkedin_easy_apply", "indeed_apply",
                   "naukri_one_click", "naukri_chat", ""):
            self.assertIsNone(adapters.adapter_for(st, "https://example.com/x"), st)

    def test_get_by_name(self):
        self.assertEqual(adapters.get("lever").name, "lever")
        self.assertIsNone(adapters.get("workday"))

    def _tmp_module(self, source: str, name: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        (Path(tmp.name) / f"{name}.py").write_text(source)
        sys.path.insert(0, tmp.name)
        self.addCleanup(lambda: sys.path.remove(tmp.name))
        self.addCleanup(lambda: sys.modules.pop(name, None))

    ASHBY = '''
from applier.adapters.form import FormAdapter
class AshbyAdapter(FormAdapter):
    name = "ashby"
    site_types = ("ashby",)
'''

    def test_adding_an_adapter_is_one_new_file_and_one_registry_line(self):
        self._tmp_module(self.ASHBY, "apex_test_ashby_adapter")
        with mock.patch.object(adapters, "REGISTRY", adapters.REGISTRY + ("apex_test_ashby_adapter:AshbyAdapter",)):
            adapters.reload()
            self.assertEqual([a.name for a in adapters.all_adapters()], ["greenhouse", "lever", "ashby"])
            self.assertEqual(adapters.adapter_for("ashby", "https://jobs.ashbyhq.com/a/b").name, "ashby")
            self.assertEqual(adapters.adapter_for("greenhouse", GH_URL).name, "greenhouse")

    def test_an_entry_that_cannot_be_imported_is_skipped_and_the_rest_still_work(self):
        with mock.patch.object(adapters, "REGISTRY", ("no.such.module:Nothing",) + adapters.REGISTRY), \
             self.assertLogs("applier.adapters", "ERROR"):
            adapters.reload()
            self.assertEqual([a.name for a in adapters.all_adapters()], ["greenhouse", "lever"])

    def test_bad_adapters_are_skipped(self):
        self._tmp_module(
            "from applier.adapters.form import FormAdapter\n"
            "class Nameless(FormAdapter):\n    site_types = ('ashby',)\n"
            "class Dupe(FormAdapter):\n    name = 'greenhouse'\n    site_types = ('ashby',)\n"
            "class NoSite(FormAdapter):\n    name = 'nosite'\n    site_types = ('monster',)\n"
            "class NotAnAdapter:\n    name = 'x'\n    site_types = ('ashby',)\n", "apex_test_bad_adapters")
        entries = tuple(f"apex_test_bad_adapters:{c}" for c in ("Nameless", "Dupe", "NoSite", "NotAnAdapter"))
        with mock.patch.object(adapters, "REGISTRY", adapters.REGISTRY + entries), self.assertLogs("applier.adapters", "ERROR"):
            adapters.reload()
            self.assertEqual([a.name for a in adapters.all_adapters()], ["greenhouse", "lever"])

    def test_the_first_adapter_that_can_handle_wins_and_one_that_raises_is_ignored(self):
        self._tmp_module(
            "from applier.adapters.form import FormAdapter\n"
            "class Greedy(FormAdapter):\n    name='greedy'\n    site_types=('greenhouse',)\n"
            "class Broken(FormAdapter):\n    name='broken'\n    site_types=('ashby',)\n"
            "    def can_handle(self, site_type, url): raise RuntimeError('boom')\n", "apex_test_order_adapters")
        entries = ("apex_test_order_adapters:Broken", "applier.adapters.greenhouse:GreenhouseAdapter",
                   "apex_test_order_adapters:Greedy")
        with mock.patch.object(adapters, "REGISTRY", entries):
            adapters.reload()
            with self.assertLogs("applier.adapters", "ERROR"):
                self.assertEqual(adapters.adapter_for("greenhouse", GH_URL).name, "greenhouse")

    def test_lever_applies_at_the_apply_url(self):
        lever = LeverAdapter()
        self.assertEqual(lever.start_url(LEVER_URL), LEVER_URL + "/apply")
        self.assertEqual(lever.start_url(LEVER_URL + "/"), LEVER_URL + "/apply")
        self.assertEqual(lever.start_url(LEVER_URL + "/apply"), LEVER_URL + "/apply")
        self.assertEqual(lever.start_url(LEVER_URL + "?lever-source=Twitter#x"), LEVER_URL + "/apply?lever-source=Twitter")

    def test_greenhouse_applies_at_the_jobs_own_url(self):
        self.assertEqual(GreenhouseAdapter().start_url(GH_URL), GH_URL)


# ── the standard questions (pure) ─────────────────────────────────

class _F:
    def __init__(self, label, field_type="text", options=()):
        self.label, self.field_type, self.options = label, field_type, list(options)


class StandardQuestionTests(unittest.TestCase):
    def test_recognised_by_what_they_say(self):
        for label, key in (("First Name *", "first_name"), ("Legal first name", "first_name"),
                           ("Last Name", "last_name"), ("Surname", "last_name"),
                           ("Full name ✱", "full_name"), ("Name", "full_name"),
                           ("Email", "email"), ("Email address", "email"),
                           ("Phone", "phone"), ("Mobile number", "phone"),
                           ("LinkedIn Profile", "linkedin_url"), ("GitHub URL", "github_url"),
                           ("Portfolio URL", "portfolio_url"), ("Website", "portfolio_url"),
                           ("Are you legally authorized to work in India?", "authorized_to_work"),
                           ("Do you have the right to work in the UK?", "authorized_to_work"),
                           ("Will you now or in the future require sponsorship?", "require_sponsorship")):
            self.assertEqual(standard.key_for(label), key, label)

    def test_custom_questions_have_no_standard_key(self):
        for label in ("Why do you want to work here?", "Current employer", "Other website", "Twitter URL",
                      "How did you hear about this role?", ""):
            self.assertEqual(standard.key_for(label), "", label)

    def test_a_question_about_both_work_authorisation_and_sponsorship_is_not_answered_by_halves(self):
        self.assertEqual(standard.key_for("Are you authorized to work here or do you require sponsorship?"), "")

    def test_uploads_are_resume_or_cover_letter(self):
        self.assertEqual(standard.key_for("Resume/CV", "file"), "resume_file")
        self.assertEqual(standard.key_for("Cover Letter", "file"), "cover_letter_file")
        self.assertEqual(standard.key_for("Portfolio samples", "file"), "")

    def test_a_custom_question_gets_a_snake_case_key(self):
        self.assertEqual(standard.slug("Why do you want to work at Acme Robotics? *"), "why_do_you_want_to_work_at_acme_robotics")
        self.assertEqual(standard.slug("???"), "question")

    def test_work_authorisation_is_not_answered_for_another_country(self):
        yes_no = ("Yes", "No")
        india = _F("Are you legally authorized to work in India?", "radio", yes_no)
        us    = _F("Are you legally authorized to work in the United States?", "radio", yes_no)
        none  = _F("Are you legally authorized to work?", "radio", yes_no)
        flat = {"authorized_to_work": "Yes", "location_country": "India"}
        self.assertEqual(standard.profile_value("authorized_to_work", flat, india), "Yes")
        self.assertEqual(standard.profile_value("authorized_to_work", flat, none), "Yes")
        self.assertEqual(standard.profile_value("authorized_to_work", flat, us), "")
        self.assertEqual(standard.profile_value("authorized_to_work", {**flat, "location_country": "United States"}, us), "Yes")
        self.assertEqual(standard.profile_value(
            "require_sponsorship", {"require_sponsorship": "No"},
            _F("Will you require sponsorship to work in the US?", "radio", yes_no)), "")

    def test_an_answer_for_a_choice_must_be_one_of_its_options(self):
        f = _F("Are you authorized to work?", "select", ("Yes, I am", "No"))
        self.assertEqual(standard.profile_value("authorized_to_work", {"authorized_to_work": "Yes"}, f), "Yes, I am")
        f = _F("Are you authorized to work?", "select", ("Maybe", "Unsure"))
        self.assertEqual(standard.profile_value("authorized_to_work", {"authorized_to_work": "Yes"}, f), "")

    def test_names_fall_back_to_splitting_the_full_name(self):
        flat = {"full_name": "Priya Anjali Sharma"}
        self.assertEqual(standard.profile_value("first_name", flat, _F("x")), "Priya")
        self.assertEqual(standard.profile_value("last_name", flat, _F("x")), "Anjali Sharma")
        self.assertEqual(standard.profile_value("full_name", {"first_name": "A", "last_name": "B"}, _F("x")), "A B")

    def test_a_missing_profile_fact_is_empty(self):
        self.assertEqual(standard.profile_value("phone", {}, _F("Phone")), "")


class ClassificationTests(unittest.TestCase):
    def test_an_unsupported_form_classifies_as_unsupported_form(self):
        self.assertEqual(classify(exc=UnsupportedForm("greenhouse", "x")), "unsupported_form")
        self.assertEqual(classify("Unsupported form (lever): no application form found on the page"), "unsupported_form")

    def test_the_type_name_alone_is_enough(self):
        class UnsupportedForm_(Exception):
            pass
        UnsupportedForm_.__name__ = "UnsupportedForm"
        self.assertEqual(classify(exc=UnsupportedForm_("anything")), "unsupported_form")


# ── the adapters against the fixtures, in a real Chromium ─────────

class BrowserCase(unittest.TestCase):
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
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.resume = Path(self.tmp.name) / "resume.pdf"
        self.resume.write_bytes(b"%PDF-1.4 resume")
        self.page = self._browser.new_page()
        self.addCleanup(self.page.close)
        for p in (mock.patch.object(form, "PAUSE", (0, 0)), mock.patch.object(runner, "SETTLE", (0, 0)),
                  mock.patch.object(runner, "AFTER_SUBMIT", (0, 0)),
                  mock.patch.object(universal, "_screenshot", lambda page, job_id, label: f"{label}.png")):
            p.start()
            self.addCleanup(p.stop)

    def load(self, html):
        self.page.set_content(html)

    def value(self, selector):
        return self.page.locator(selector).first.input_value()

    def prepared(self, adapter, answers=None, profile=None):
        """Extract + resolve (+ bank), as the runner does. Returns (fields, hits)."""
        fields = adapter.extract_fields(self.page)
        hits = standard.resolve(fields, profile or PROFILE)
        if answers is not None:
            from applier.form_analyzer import FormAnalysis
            answers.apply(FormAnalysis(page_url="u", job_id="j", fields=fields))
        return fields, hits

    def by_key(self, fields):
        return {f.field_key: f for f in fields}

    def answers(self, **by_label):
        state = MemoryState()
        state.application_answers = {normalize_question(k.replace("_", " ")): v for k, v in by_label.items()}
        bank = AnswerBank(1, Path(self.tmp.name) / "jobs.db")
        return AnswerContext(bank, "Acme", state=state)


class GreenhouseAdapterTests(BrowserCase):
    def setUp(self):
        super().setUp()
        self.adapter = GreenhouseAdapter()
        self.load(GREENHOUSE_HTML)

    def test_the_fields_found(self):
        fields = self.adapter.extract_fields(self.page)
        self.assertEqual([(f.field_key, f.field_type, f.is_required) for f in fields], [
            ("first_name", "text", True), ("last_name", "text", True), ("email", "text", True),
            ("phone", "tel", False), ("resume_file", "file", True), ("cover_letter_file", "file", False),
            ("linkedin_url", "text", False), ("portfolio_url", "text", False), ("github_url", "text", False),
            ("why_do_you_want_to_work_at_acme_robotics", "textarea", True), ("current_employer", "text", False),
            ("authorized_to_work", "select", True), ("require_sponsorship", "select", True),
            ("are_you_at_least_18_years_of_age", "radio", True)])
        labels = {f.field_key: f.label for f in fields}
        self.assertEqual(labels["first_name"], "First Name")
        self.assertEqual(labels["resume_file"], "Resume/CV")
        self.assertEqual(labels["authorized_to_work"], "Are you legally authorized to work in India?")

    def test_choices_and_the_widget_kinds(self):
        f = self.by_key(self.adapter.extract_fields(self.page))
        self.assertEqual(f["require_sponsorship"].options, ["Yes", "No"])          # a native select, "Select..." dropped
        self.assertEqual(f["are_you_at_least_18_years_of_age"].options, ["Yes", "No"])
        self.assertEqual(f["are_you_at_least_18_years_of_age"].meta["kind"], "group")
        self.assertTrue(f["authorized_to_work"].meta["combobox"])
        self.assertFalse(f["require_sponsorship"].meta["combobox"])

    def test_the_demographic_block_is_not_read(self):
        labels = [f.label for f in self.adapter.extract_fields(self.page)]
        self.assertNotIn("Gender", labels)
        self.assertNotIn("Veteran Status", labels)

    def test_the_standard_questions_are_filled_from_the_profile(self):
        fields, hits = self.prepared(self.adapter)
        filled, failed = self.adapter.fill(self.page, fields, self.resume)
        self.assertEqual(self.value("#first_name"), "Priya")
        self.assertEqual(self.value("#last_name"), "Sharma")
        self.assertEqual(self.value("#email"), "priya@example.com")
        self.assertEqual(self.value("#phone"), "+91 98765 43210")
        self.assertEqual(self.value("#question_30001"), "https://linkedin.com/in/priya")
        self.assertEqual(self.value("#question_30002"), "https://priya.dev")
        self.assertEqual(self.value("#question_30003"), "https://github.com/priya")
        self.assertEqual({h["field_key"] for h in hits} >= {"first_name", "email", "authorized_to_work"}, True)

    def test_work_authorisation_in_a_combobox_and_a_native_select(self):
        fields, _ = self.prepared(self.adapter)
        self.adapter.fill(self.page, fields, self.resume)
        self.assertEqual(self.page.locator(".select__single-value").inner_text(), "Yes")        # authorized to work in India
        self.assertEqual(self.page.locator("#question_30006").evaluate("e => e.selectedOptions[0].textContent"), "No")

    def test_the_resume_is_uploaded(self):
        fields, _ = self.prepared(self.adapter)
        filled, failed = self.adapter.fill(self.page, fields, self.resume)
        self.assertEqual(self.page.locator("#resume").evaluate("e => e.files.length ? e.files[0].name : ''"), "resume.pdf")
        self.assertIn("resume_file", [f.field_key for f in filled])
        self.assertEqual(self.page.locator("#cover_letter").evaluate("e => e.files.length"), 0)   # no cover letter file: left alone

    def test_a_mandatory_custom_question_nobody_can_answer_is_a_missing_field_and_left_empty(self):
        fields, _ = self.prepared(self.adapter)
        missing = [f.field_key for f in fields if f.needs_manual]
        self.assertEqual(missing, ["why_do_you_want_to_work_at_acme_robotics", "are_you_at_least_18_years_of_age"])
        self.adapter.fill(self.page, fields, self.resume)
        self.assertEqual(self.value("#question_30004"), "")
        self.assertEqual(self.page.locator("input[type=radio]:checked").count(), 0)

    def test_an_optional_custom_question_is_left_blank_not_asked(self):
        fields, _ = self.prepared(self.adapter)
        employer = self.by_key(fields)["current_employer"]
        self.assertFalse(employer.needs_manual)
        self.adapter.fill(self.page, fields, self.resume)
        self.assertEqual(self.value("#question_30007"), "")

    def test_the_answer_bank_answers_custom_questions(self):
        answers = self.answers(why_do_you_want_to_work_at_acme_robotics="I like robots.",
                               are_you_at_least_18_years_of_age="Yes")
        fields, _ = self.prepared(self.adapter, answers)
        self.assertEqual([f.field_key for f in fields if f.needs_manual], [])
        self.adapter.fill(self.page, fields, self.resume)
        self.assertEqual(self.value("#question_30004"), "I like robots.")
        self.assertEqual(self.page.locator("input[type=radio]:checked").evaluate("e => e.value"), "1")

    def test_nothing_is_reported_filled_that_the_page_does_not_hold(self):
        fields, _ = self.prepared(self.adapter)
        self.page.evaluate("document.getElementById('first_name').addEventListener('input', e => { e.target.value = 'x'; })")
        filled, failed = self.adapter.fill(self.page, fields, self.resume)
        self.assertIn("first_name", [f.field_key for f in failed])
        self.assertNotIn("first_name", [f.field_key for f in filled])

    def test_a_select_the_page_resets_is_not_reported_filled(self):
        fields, _ = self.prepared(self.adapter)
        self.page.evaluate("document.getElementById('question_30006').addEventListener('change', e => { e.target.selectedIndex = 0; })")
        filled, failed = self.adapter.fill(self.page, fields, self.resume)
        self.assertIn("require_sponsorship", [f.field_key for f in failed])
        self.assertNotIn("require_sponsorship", [f.field_key for f in filled])

    def test_an_upload_the_page_discards_is_not_reported_filled(self):
        fields, _ = self.prepared(self.adapter)
        self.page.evaluate("document.getElementById('resume').addEventListener('change', e => { e.target.value = ''; })")
        filled, failed = self.adapter.fill(self.page, fields, self.resume)
        self.assertIn("resume_file", [f.field_key for f in failed])
        self.assertNotIn("resume_file", [f.field_key for f in filled])

    def test_a_value_that_is_not_an_option_is_a_failure_not_a_guess(self):
        fields, _ = self.prepared(self.adapter)
        sponsor = self.by_key(fields)["require_sponsorship"]
        sponsor.db_value = "Perhaps"
        filled, failed = self.adapter.fill(self.page, [sponsor], self.resume)
        self.assertEqual(([f.field_key for f in filled], [f.field_key for f in failed]), ([], ["require_sponsorship"]))

    def test_dry_run_finds_the_button_and_does_not_click_it(self):
        self.assertTrue(self.adapter.submit(self.page, dry_run=True))
        self.assertFalse(self.page.evaluate("window.__submitted"))

    def test_submit_clicks_the_button(self):
        self.assertTrue(self.adapter.submit(self.page, dry_run=False))
        self.assertTrue(self.page.evaluate("window.__submitted"))

    def test_no_submit_button_is_unsupported(self):
        self.page.evaluate("document.getElementById('submit_app').remove()")
        with self.assertRaises(UnsupportedForm):
            self.adapter.submit(self.page, dry_run=False)
        self.assertFalse(self.page.evaluate("window.__submitted"))


class GreenhouseUnsupportedTests(BrowserCase):
    def setUp(self):
        super().setUp()
        self.adapter = GreenhouseAdapter()

    def refused(self, html) -> UnsupportedForm:
        self.load(html)
        with self.assertRaises(UnsupportedForm) as cm:
            self.adapter.extract_fields(self.page)
        self.assertEqual(classify(exc=cm.exception), "unsupported_form", "its wording must classify as unsupported_form")
        self.assertEqual(classify(str(cm.exception)), "unsupported_form")
        return cm.exception

    def test_a_page_without_the_form(self):
        e = self.refused("<html><body><h1>Senior Engineer</h1><p>Apply on our site.</p></body></html>")
        self.assertIn("no application form", e.detail)

    def test_a_form_inside_an_embedded_frame_says_so(self):
        e = self.refused('<html><body><iframe src="https://boards.greenhouse.io/embed/job_app?for=acme"></iframe></body></html>')
        self.assertIn("embedded frame", e.detail)

    def test_a_form_with_nothing_to_read(self):
        self.refused('<form id="application_form"><input type="hidden" name="x" value="1"><button>Go</button></form>')

    def test_a_mandatory_upload_that_is_not_the_resume(self):
        html = GREENHOUSE_HTML.replace(
            '<div class="label" id="upload-label-cover_letter">Cover Letter</div>',
            '<div class="label" id="upload-label-cover_letter">Transcript<span class="required">*</span></div>')
        e = self.refused(html)
        self.assertIn("Transcript", e.detail)

    def test_a_mandatory_cover_letter_when_there_is_no_cover_letter_file(self):
        html = GREENHOUSE_HTML.replace('id="upload-label-cover_letter">Cover Letter</div>',
                                       'id="upload-label-cover_letter">Cover Letter<span class="required">*</span></div>')
        self.refused(html)

    def test_a_mandatory_group_of_checkboxes(self):
        html = GREENHOUSE_HTML.replace("<div id=\"eeoc_fields\">", '''
          <fieldset><legend class="label">Which of these languages do you use?<span class="required">*</span></legend>
            <label><input type="checkbox" name="langs" value="py" required> Python</label>
            <label><input type="checkbox" name="langs" value="go"> Go</label></fieldset>
          <div id="eeoc_fields">''')
        e = self.refused(html)
        self.assertIn("multiple-choice", e.detail)

    def test_a_mandatory_control_with_no_label(self):
        html = GREENHOUSE_HTML.replace("<div id=\"eeoc_fields\">", '<input type="text" required><div id="eeoc_fields">')
        e = self.refused(html)
        self.assertIn("no label", e.detail)

    def test_an_optional_checkbox_group_is_just_left_alone(self):
        html = GREENHOUSE_HTML.replace("<div id=\"eeoc_fields\">", '''
          <fieldset><legend class="label">Which of these languages do you use?</legend>
            <label><input type="checkbox" name="langs" value="py"> Python</label>
            <label><input type="checkbox" name="langs" value="go"> Go</label></fieldset>
          <div id="eeoc_fields">''')
        self.load(html)
        fields = self.adapter.extract_fields(self.page)
        filled, failed = self.adapter.fill(self.page, standard.resolve(fields, PROFILE) and fields, self.resume)
        self.assertEqual(self.page.locator("input[name=langs]:checked").count(), 0)

    def test_a_demographic_question_that_is_mandatory_is_not_skipped(self):
        html = GREENHOUSE_HTML.replace('<label for="gender" class="label">Gender</label>\n              <select id="gender"',
                                       '<label for="gender" class="label">Gender<span class="required">*</span></label>\n              <select id="gender" required')
        self.load(html)
        self.assertIn("gender", [f.field_key for f in self.adapter.extract_fields(self.page)])


class LeverAdapterTests(BrowserCase):
    def setUp(self):
        super().setUp()
        self.adapter = LeverAdapter()
        self.load(LEVER_HTML)

    def test_the_fields_found(self):
        fields = self.adapter.extract_fields(self.page)
        self.assertEqual([(f.field_key, f.field_type, f.is_required) for f in fields], [
            ("resume_file", "file", True), ("full_name", "text", True), ("email", "text", True),
            ("phone", "text", False), ("current_company", "text", False),
            ("linkedin_url", "text", False), ("twitter_url", "text", False), ("github_url", "text", False),
            ("portfolio_url", "text", False), ("other_website", "text", False),
            ("authorized_to_work", "radio", True), ("require_sponsorship", "radio", True),
            ("why_are_you_interested_in_acme_robotics", "textarea", True),
            ("how_did_you_hear_about_this_role", "select", False), ("additional_information", "textarea", False)])
        f = self.by_key(fields)
        self.assertEqual(f["full_name"].label, "Full name")
        self.assertEqual(f["authorized_to_work"].options, ["Yes", "No"])
        self.assertEqual(f["authorized_to_work"].meta["kind"], "group")
        self.assertEqual(f["how_did_you_hear_about_this_role"].options, ["LinkedIn", "Referral", "Other"])

    def test_the_equal_opportunity_block_is_not_read(self):
        labels = [f.label for f in self.adapter.extract_fields(self.page)]
        self.assertNotIn("Gender", labels)
        self.assertNotIn("Veteran status", labels)

    def test_the_standard_questions_are_filled_from_the_profile(self):
        fields, _ = self.prepared(self.adapter)
        self.adapter.fill(self.page, fields, self.resume)
        self.assertEqual(self.value("[name=name]"), "Priya Sharma")          # one full-name box
        self.assertEqual(self.value("[name=email]"), "priya@example.com")
        self.assertEqual(self.value("[name=phone]"), "+91 98765 43210")
        self.assertEqual(self.value("[name='urls[LinkedIn]']"), "https://linkedin.com/in/priya")
        self.assertEqual(self.value("[name='urls[GitHub]']"), "https://github.com/priya")
        self.assertEqual(self.value("[name='urls[Portfolio]']"), "https://priya.dev")

    def test_links_the_profile_has_no_fact_for_are_left_blank(self):
        fields, _ = self.prepared(self.adapter)
        self.adapter.fill(self.page, fields, self.resume)
        for name in ("urls[Twitter]", "urls[Other]", "org", "comments"):
            self.assertEqual(self.value(f"[name='{name}']"), "", name)

    def test_the_resume_is_uploaded(self):
        fields, _ = self.prepared(self.adapter)
        filled, _ = self.adapter.fill(self.page, fields, self.resume)
        self.assertEqual(self.page.locator("[name=resume]").evaluate("e => e.files.length ? e.files[0].name : ''"), "resume.pdf")
        self.assertIn("resume_file", [f.field_key for f in filled])

    def test_sponsorship_radio_is_answered_but_a_question_about_the_us_is_left_for_you(self):
        fields, _ = self.prepared(self.adapter)
        self.adapter.fill(self.page, fields, self.resume)
        spons = self.page.locator("input[name$='000000000002][field0]']:checked")
        self.assertEqual(spons.evaluate("e => e.value"), "No")
        us = self.by_key(fields)["authorized_to_work"]
        self.assertEqual((us.db_value, us.needs_manual), ("", True))
        self.assertEqual(self.page.locator("input[name$='000000000001][field0]']:checked").count(), 0)

    def test_the_custom_questions_a_person_answers(self):
        answers = self.answers(are_you_legally_authorized_to_work_in_the_united_states="No",
                               why_are_you_interested_in_acme_robotics="Robots.",
                               how_did_you_hear_about_this_role="Referral")
        fields, _ = self.prepared(self.adapter, answers)
        self.assertEqual([f.field_key for f in fields if f.needs_manual], [])
        self.adapter.fill(self.page, fields, self.resume)
        self.assertEqual(self.value("textarea[name$='000000000003][field0]']"), "Robots.")
        self.assertEqual(self.page.locator("select[name$='000000000004][field0]']").evaluate("e => e.value"), "Referral")
        self.assertEqual(self.page.locator("input[name$='000000000001][field0]']:checked").evaluate("e => e.value"), "No")

    def test_dry_run_finds_the_button_and_does_not_click_it(self):
        self.assertTrue(self.adapter.submit(self.page, dry_run=True))
        self.assertFalse(self.page.evaluate("window.__submitted"))

    def test_submit_clicks_the_button(self):
        self.page.evaluate("document.getElementById('application-form').noValidate = true")   # the runner fills first
        self.assertTrue(self.adapter.submit(self.page, dry_run=False))
        self.assertTrue(self.page.evaluate("window.__submitted"))

    def test_a_page_that_is_not_the_form_is_unsupported(self):
        self.load('<html><body><form id="newsletter" class="signup"><input name="q" type="text"></form></body></html>')
        with self.assertRaises(UnsupportedForm):
            self.adapter.extract_fields(self.page)

    def test_a_mandatory_upload_that_is_not_the_resume(self):
        html = LEVER_HTML.replace('<li class="application-question">\n            <label>\n              <div class="application-label">Phone</div>',
                                  '<li class="application-question"><label><div class="application-label">Transcript <span class="required">✱</span></div>'
                                  '<div class="application-field"><input type="file" name="transcript" required></div></label></li>'
                                  '<li class="application-question">\n            <label>\n              <div class="application-label">Phone</div>')
        self.load(html)
        with self.assertRaises(UnsupportedForm):
            self.adapter.extract_fields(self.page)


# ── the runner: the three passes, over a fake network ─────────────

class RunnerTests(BrowserCase):
    def setUp(self):
        super().setUp()
        self.requested = []
        self.emitted = []
        cfg_patch = mock.patch.object(cfg, "RESUME_PATH", self.resume)
        cfg_patch.start()
        self.addCleanup(cfg_patch.stop)

        def serve(html):
            def handler(route):
                self.requested.append(route.request.url)
                route.fulfill(status=200, content_type="text/html", body=html)
            self.page.route("**/*", handler)
        self.serve = serve

    def emit(self, event, data=None):
        self.emitted.append((event, data))

    def steps(self):
        return [d["step"] for e, d in self.emitted if e == "step"]

    def gh_job(self):
        return {"job_id": "j1", "url": GH_URL, "title": "Senior Backend Engineer", "company": "Acme", "source": "linkedin"}

    def lever_job(self):
        return {"job_id": "j2", "url": LEVER_URL, "title": "Platform Engineer", "company": "Acme", "source": "linkedin"}

    def test_first_pass_fills_reports_what_is_missing_and_snapshots_for_review(self):
        self.serve(GREENHOUSE_HTML)
        r = runner.run_application(GreenhouseAdapter(), self.page, self.gh_job(), PROFILE, emit=self.emit)
        self.assertEqual((r["success"], r["error"], r["needs_review"]), (True, "", True))
        self.assertEqual(self.requested, [GH_URL])
        self.assertEqual([f.field_key for f in r["missing_fields"]],
                         ["why_do_you_want_to_work_at_acme_robotics", "are_you_at_least_18_years_of_age"])
        self.assertEqual(self.value("#first_name"), "Priya")
        snap = {s["field_key"]: s for s in r["review"]["fields"]}
        self.assertEqual(snap["email"]["value"], "priya@example.com")
        self.assertEqual(snap["email"]["hit"]["source"], "profile")                  # so the packet says "profile"
        self.assertEqual(snap["email"]["required"], True)
        self.assertNotIn("resume_file", snap)                                       # uploads are not reviewed
        self.assertNotIn("current_employer", snap)                                  # an optional blank is not an answer
        self.assertNotIn("phone_alternate", snap)
        self.assertEqual(snap["why_do_you_want_to_work_at_acme_robotics"]["value"], "")   # mandatory, still empty
        self.assertEqual(self.steps()[:1], ["opened_page"])
        self.assertIn("filled_fields", self.steps())
        self.assertIn(("site_type", "greenhouse"), self.emitted)

    def test_the_first_pass_review_snapshot_turns_into_a_packet_the_way_the_universal_one_does(self):
        from applier import review
        self.serve(GREENHOUSE_HTML)
        r = runner.run_application(GreenhouseAdapter(), self.page, self.gh_job(), PROFILE, emit=self.emit)
        fields = {f["label"]: f for f in review.build_fields(r["review"]["fields"])}
        self.assertEqual((fields["Email"]["source"], fields["Email"]["confidence"]), ("profile", 0.95))
        self.assertEqual((fields["Are you legally authorized to work in India?"]["source"],
                          fields["Are you legally authorized to work in India?"]["value"]), ("profile", "Yes"))
        self.assertTrue(fields["Why do you want to work at Acme Robotics?"]["flagged"])      # required but empty

    def test_a_bank_answer_outranks_a_profile_fact_in_the_snapshot(self):
        self.serve(GREENHOUSE_HTML)
        answers = self.answers(why_do_you_want_to_work_at_acme_robotics="Robots.", are_you_at_least_18_years_of_age="Yes")
        r = runner.run_application(GreenhouseAdapter(), self.page, self.gh_job(), PROFILE, emit=self.emit, answers=answers)
        self.assertEqual(r["missing_fields"], [])
        snap = {s["field_key"]: s for s in r["review"]["fields"]}
        self.assertEqual(snap["why_do_you_want_to_work_at_acme_robotics"]["hit"]["source"], "application")
        self.assertEqual(self.value("#question_30004"), "Robots.")

    def test_lever_is_opened_at_its_apply_url(self):
        self.serve(LEVER_HTML)
        r = runner.run_application(LeverAdapter(), self.page, self.lever_job(), PROFILE, emit=self.emit)
        self.assertEqual(self.requested, [LEVER_URL + "/apply"])
        self.assertTrue(r["success"])
        self.assertEqual(self.value("[name=name]"), "Priya Sharma")

    def test_an_unsupported_page_raises_through_the_runner(self):
        self.serve("<html><body><h1>Not a form</h1></body></html>")
        with self.assertRaises(UnsupportedForm):
            runner.run_application(GreenhouseAdapter(), self.page, self.gh_job(), PROFILE, emit=self.emit)

    def test_a_captcha_is_the_usual_failure(self):
        self.serve(GREENHOUSE_HTML.replace("<div class=\"application--container\">",
                                           '<div class="g-recaptcha" style="width:50px;height:50px">x</div><div class="application--container">'))
        r = runner.run_application(GreenhouseAdapter(), self.page, self.gh_job(), PROFILE, emit=self.emit)
        self.assertEqual((r["success"], classify(r["error"])), (False, "captcha"))

    def test_a_navigation_failure_is_a_failure_dict(self):
        self.page.route("**/*", lambda route: route.abort())
        r = runner.run_application(GreenhouseAdapter(), self.page, self.gh_job(), PROFILE, emit=self.emit)
        self.assertFalse(r["success"])
        self.assertIn("Navigation failed", r["error"])

    def test_the_second_pass_refills_everything_but_snapshots_only_the_answered(self):
        self.serve(GREENHOUSE_HTML)
        answers = self.answers(why_do_you_want_to_work_at_acme_robotics="Robots.", are_you_at_least_18_years_of_age="Yes")
        r = runner.run_iteration2(GreenhouseAdapter(), self.page, self.gh_job(), PROFILE,
                                  ["why_do_you_want_to_work_at_acme_robotics", "are_you_at_least_18_years_of_age"],
                                  emit=self.emit, answers=answers)
        self.assertEqual((r["success"], r["missing_fields"]), (True, []))
        self.assertEqual(self.value("#first_name"), "Priya")                        # the whole form is there to screenshot
        self.assertEqual(self.value("#question_30004"), "Robots.")
        self.assertEqual(sorted(s["field_key"] for s in r["review"]["fields"]),
                         ["are_you_at_least_18_years_of_age", "why_do_you_want_to_work_at_acme_robotics"])

    def test_submit_dry_run_fills_and_does_not_click(self):
        self.serve(GREENHOUSE_HTML)
        answers = self.answers(why_do_you_want_to_work_at_acme_robotics="Robots.", are_you_at_least_18_years_of_age="Yes")
        r = runner.run_submit(GreenhouseAdapter(), self.page, self.gh_job(), PROFILE, emit=self.emit,
                              dry_run=True, answers=answers)
        self.assertEqual((r["success"], r["dry_run"], r["error"]), (True, True, ""))
        self.assertEqual(self.value("#email"), "priya@example.com")
        self.assertFalse(self.page.evaluate("window.__submitted"))

    def test_submit_for_real_fills_then_clicks(self):
        self.serve(GREENHOUSE_HTML)
        answers = self.answers(why_do_you_want_to_work_at_acme_robotics="Robots.", are_you_at_least_18_years_of_age="Yes")
        r = runner.run_submit(GreenhouseAdapter(), self.page, self.gh_job(), PROFILE, emit=self.emit, answers=answers)
        self.assertEqual((r["success"], r["dry_run"]), (True, False))
        self.assertTrue(self.page.evaluate("window.__submitted"))
        self.assertEqual(self.value("#question_30004"), "Robots.")                  # it was filled BEFORE the click

    def test_submit_reports_what_the_site_rejected(self):
        self.serve(GREENHOUSE_HTML.replace("e.preventDefault(); window.__submitted = true;",
                   "e.preventDefault(); window.__submitted = true; document.getElementById('email').setAttribute('aria-invalid','true');"))
        r = runner.run_submit(GreenhouseAdapter(), self.page, self.gh_job(), PROFILE, emit=self.emit)
        self.assertFalse(r["success"])
        self.assertEqual(classify(r["error"]), "validation_error")

    def test_a_missing_submit_button_in_dry_run_is_unsupported_before_anything_is_clicked(self):
        self.serve(GREENHOUSE_HTML.replace('id="submit_app"', 'id="not-it" hidden'))
        with self.assertRaises(UnsupportedForm):
            runner.run_submit(GreenhouseAdapter(), self.page, self.gh_job(), PROFILE, emit=self.emit, dry_run=True)
        self.assertFalse(self.page.evaluate("window.__submitted"))

    def answers(self, **by_label):
        state = MemoryState()
        state.application_answers = {normalize_question(k.replace("_", " ")): v for k, v in by_label.items()}
        return AnswerContext(AnswerBank(1, Path(self.tmp.name) / "jobs.db"), "Acme", state=state)


# ── the Orchestrator ──────────────────────────────────────────────

class Playwright:
    def __enter__(self): return object()
    def __exit__(self, *a): return False


class Ctx:
    pages = []
    def new_page(self): return object()
    def close(self): pass


class Settings:
    def read(self, key, default=""):
        return {"quality_first": "false", "daily_apply_limit": "0", "min_delay_between_applies": "0"}.get(key, default)


class OrchestratorTests(unittest.TestCase):
    """Real Orchestrator, real JobsDB; the fillers are fakes that record what ran."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.path = self.dir / "jobs.db"
        self._sessions, self._resume = cfg.SESSIONS_DIR, cfg.RESUME_PATH
        cfg.SESSIONS_DIR = self.dir / "sessions"
        cfg.SESSIONS_DIR.mkdir()
        cfg.RESUME_PATH = self.dir / "resume.pdf"
        cfg.RESUME_PATH.write_bytes(b"%PDF-1.4")
        self.addCleanup(lambda: (setattr(cfg, "SESSIONS_DIR", self._sessions), setattr(cfg, "RESUME_PATH", self._resume)))
        self.db = JobsDB(path=self.path, user_id=1)
        self.calls = []                       # (filler, pass name)
        self.adapter_raises = None            # an UnsupportedForm for the adapter to raise
        self.universal_result = None          # override of what the universal filler returns
        self.n = 0
        ok = {"success": True, "screenshots": [], "missing_fields": [], "error": "", "needs_review": True,
              "review": {"fields": [], "url": "u"}, "dry_run": False}

        def fake(filler, name):
            def run(*args, **kw):
                self.calls.append((filler, name))
                if filler == "adapter" and self.adapter_raises:
                    raise self.adapter_raises
                if filler == "universal" and self.universal_result is not None:
                    return self.universal_result
                return dict(ok)
            return run

        patches = [mock.patch("applier.session.Orchestrator._settings", staticmethod(lambda: Settings())),
                   mock.patch("applier.session.Orchestrator._dry_run_enabled", staticmethod(lambda: False)),
                   mock.patch("applier.preflight.fetch_page", lambda url: (200, "")),
                   mock.patch("playwright.sync_api.sync_playwright", lambda: Playwright()),
                   mock.patch("urllib.request.urlopen", side_effect=AssertionError("network"))]
        for name in ("run_application", "run_iteration2", "run_submit"):
            patches.append(mock.patch(f"applier.universal.{name}", fake("universal", name)))
            patches.append(mock.patch(f"applier.adapters.runner.{name}", fake("adapter", name)))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def job(self, url, stage="preparing"):
        self.n += 1
        jid = self.db.add({"url": url, "title": f"Role {self.n}", "company": "Acme", "source": "linkedin"})
        self.db.update_score(jid, 80, "r")
        if stage in ("preparing", "needs_you", "ready_for_review"):
            self.db.transition(jid, "preparing")
        if stage == "needs_you":
            self.db.transition(jid, "needs_you", reason="question")
        if stage == "ready_for_review":
            self.db.transition(jid, "ready_for_review")
        return jid

    def orchestrator(self, jid):
        from applier.session import ApplicationSession, Orchestrator
        o = Orchestrator.__new__(Orchestrator)
        o.job_id, o.job = jid, self.db.get_by_id(jid)
        o.profile_db = mock.Mock(summary=lambda: {"_flat": {}, "_answered": {}}, get_pending_missing=lambda job_id: [])
        o.jobs_db = JobsDB(path=self.path)
        o.session = ApplicationSession.load(jid) or ApplicationSession(
            job_id=jid, job_url="u", job_title="t", company="Acme", source="linkedin")
        o._open_browser = lambda pw: Ctx()
        return o

    def events(self, jid):
        return self.db.get_application(jid)["events"]

    def stage(self, jid):
        return self.db.get_stage(jid)

    def test_a_greenhouse_application_runs_through_its_adapter(self):
        jid = self.job(GH_URL)
        self.orchestrator(jid).run_iter1()
        self.assertEqual(self.calls, [("adapter", "run_application")])
        self.assertEqual(self.stage(jid), "ready_for_review")

    def test_a_lever_application_runs_through_its_adapter(self):
        jid = self.job(LEVER_URL)
        self.orchestrator(jid).run_iter1()
        self.assertEqual(self.calls, [("adapter", "run_application")])

    def test_any_other_site_runs_through_the_universal_filler(self):
        for url in ("https://example.com/careers/1", "https://acme.wd5.myworkdayjobs.com/en-US/x",
                    "https://jobs.ashbyhq.com/a/b", "https://www.linkedin.com/jobs/view/1"):
            self.calls.clear()
            jid = self.job(url)
            self.orchestrator(jid).run_iter1()
            self.assertEqual(self.calls, [("universal", "run_application")], url)

    def test_the_same_filler_does_the_later_passes(self):
        jid = self.job(GH_URL)
        self.orchestrator(jid).run_iter1()
        self.orchestrator(jid).submit()
        self.assertEqual(self.calls, [("adapter", "run_application"), ("adapter", "run_submit")])

    def test_an_unsupported_form_falls_back_to_the_universal_filler_once(self):
        self.adapter_raises = UnsupportedForm("greenhouse", "a mandatory upload ('Transcript') that is not the resume")
        jid = self.job(GH_URL)
        self.orchestrator(jid).run_iter1()
        self.assertEqual(self.calls, [("adapter", "run_application"), ("universal", "run_application")])
        self.assertEqual(self.stage(jid), "ready_for_review", "the universal filler's success stands")
        (ev,) = [e for e in self.events(jid) if e["kind"] == "step" and e["detail"].get("step") == "adapter_fallback"]
        self.assertIn("Transcript", ev["detail"]["reason"])
        self.assertEqual(ev["detail"]["adapter"], "greenhouse")

    def test_after_a_fallback_every_later_pass_of_that_application_uses_the_universal_filler(self):
        self.adapter_raises = UnsupportedForm("greenhouse", "no application form found on the page")
        jid = self.job(GH_URL)
        self.orchestrator(jid).run_iter1()
        self.calls.clear()
        self.orchestrator(jid).submit()
        self.assertEqual(self.calls, [("universal", "run_submit")], "the adapter is not asked again")

    def test_a_restarted_application_tries_the_adapter_again(self):
        self.adapter_raises = UnsupportedForm("greenhouse", "no application form found on the page")
        jid = self.job(GH_URL)
        self.orchestrator(jid).run_iter1()
        self.db.transition(jid, "preparing")                                   # sent back for another go
        self.adapter_raises, self.calls[:] = None, []
        self.orchestrator(jid).run_iter1()
        self.assertEqual(self.calls, [("adapter", "run_application")])

    def test_when_the_fallback_fails_too_the_job_goes_to_needs_you_as_unsupported_form(self):
        self.adapter_raises = UnsupportedForm("greenhouse", "no application form found on the page")
        self.universal_result = {"success": False, "screenshots": [], "missing_fields": [], "analysis": None,
                                "needs_review": False, "error": "Navigation failed: Timeout 30000ms exceeded"}
        jid = self.job(GH_URL)
        self.orchestrator(jid).run_iter1()
        self.assertEqual(self.calls, [("adapter", "run_application"), ("universal", "run_application")])
        self.assertEqual(self.stage(jid), "needs_you")
        row = self.db.get_by_id(jid)
        self.assertEqual(row["blocked_reason"], "unsupported_form", "a timeout would have been retried; this is not that")

    def test_a_more_specific_blocker_in_the_fallback_keeps_its_own_class(self):
        self.adapter_raises = UnsupportedForm("lever", "no application form found on the page")
        self.universal_result = {"success": False, "screenshots": [], "missing_fields": [], "analysis": None,
                                 "needs_review": False, "error": "CAPTCHA detected — skipping"}
        jid = self.job(LEVER_URL)
        self.orchestrator(jid).run_iter1()
        self.assertEqual(self.db.get_by_id(jid)["blocked_reason"], "captcha")

    def test_an_error_that_is_not_unsupported_form_does_not_fall_back(self):
        self.adapter_raises = RuntimeError("selector exploded")
        jid = self.job(GH_URL)
        self.orchestrator(jid).run_iter1()
        self.assertEqual(self.calls, [("adapter", "run_application")])
        self.assertIn(self.stage(jid), ("failed", "needs_you"))

    def test_an_adapter_failure_result_is_not_a_reason_to_fall_back(self):
        def failing(*a, **k):
            self.calls.append(("adapter", "run_application"))
            return {"success": False, "screenshots": [], "missing_fields": [], "error": "CAPTCHA detected — skipping"}
        with mock.patch("applier.adapters.runner.run_application", failing):
            jid = self.job(GH_URL)
            self.orchestrator(jid).run_iter1()
        self.assertEqual(self.calls, [("adapter", "run_application")])
        self.assertEqual(self.stage(jid), "needs_you")

    def test_the_filler_is_kept_with_the_session(self):
        from applier.session import ApplicationSession
        jid = self.job(GH_URL)
        self.orchestrator(jid).run_iter1()
        self.assertEqual(ApplicationSession.load(jid).filler, "greenhouse")
        jid = self.job("https://example.com/x")
        self.orchestrator(jid).run_iter1()
        self.assertEqual(ApplicationSession.load(jid).filler, "universal")

    def test_a_filler_that_is_no_longer_registered_means_the_universal_one(self):
        jid = self.job(GH_URL)
        self.orchestrator(jid).run_iter1()
        self.calls.clear()
        with mock.patch.object(adapters, "REGISTRY", ()):
            adapters.reload()
            try:
                self.orchestrator(jid).submit()
            finally:
                adapters.reload()
        self.assertEqual(self.calls, [("universal", "run_submit")])


if __name__ == "__main__":
    unittest.main()
