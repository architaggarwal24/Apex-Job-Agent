# applier/adapters/base.py
"""
The interface a site adapter implements, and the one error it may raise.

An ADAPTER drives one family of career pages (Greenhouse, Lever, ...) with
selectors that are known for that family, instead of sending the page to the
generic LLM form analyser (applier/form_analyzer.py) and filling whatever it
guesses. It is faster, free of LLM calls and does the same thing every time.

Four methods:

    can_handle(site_type, url)       pure: is this application one of mine?
                                     (site_type comes from applier/site_type.py)
    extract_fields(page)             read the form on the page and return its
                                     fields as AdapterField objects
    fill(page, fields, resume_path)  put each field's db_value on the page;
                                     returns (filled, failed)
    submit(page, dry_run)            click the form's submit button, or with
                                     dry_run only check that it is there

plus one optional hook, start_url(url), for a family whose form is not at the
job's own URL (Lever's is at <posting>/apply).

An adapter that meets something it cannot handle - no form on the page, a
mandatory question of a kind it cannot answer - raises UnsupportedForm. That is
NOT a failure of the application: the Orchestrator catches it, says so on the
job's timeline and runs the universal filler instead, once. If that cannot do
it either, the failure is classified unsupported_form (applier/failures.py) and
the job goes to needs_you. So an adapter should raise it rather than guess, and
only BEFORE it has clicked submit - nothing irreversible may have happened
when the universal filler takes over.

Adding an adapter is one new file here plus one line in the REGISTRY of
applier/adapters/__init__.py.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from applier.form_analyzer import FormField


class UnsupportedForm(Exception):
    """This adapter cannot handle this form. The text starts "Unsupported form",
    which is what applier/failures.py classifies as unsupported_form, so the
    error classifies correctly even if it escapes uncaught. Keep `detail`
    free of words the classifier reads as another failure ("required field",
    "invalid email", "timeout", "captcha"): say "mandatory question" instead."""

    def __init__(self, adapter: str, detail: str):
        self.adapter, self.detail = adapter, detail
        super().__init__(f"Unsupported form ({adapter}): {detail}")


@dataclass
class AdapterField(FormField):
    """A FormField (so the answer bank, the review snapshot and the missing-
    answers flow treat it like any other) that also remembers how the adapter
    found it. `meta` keys: name, id, kind ("single" | "group"), combobox
    (bool), option_selectors ({option label: selector}, for radio groups)."""
    meta: dict = field(default_factory=dict)


class Adapter(ABC):
    name: str = ""                      # unique, e.g. "greenhouse"; kept with the application
    site_types: tuple[str, ...] = ()    # the applier/site_type.py types this adapter serves

    @abstractmethod
    def can_handle(self, site_type: str, url: str) -> bool:
        """Pure: True if this adapter should run this application."""

    @abstractmethod
    def extract_fields(self, page) -> list[AdapterField]:
        """The form's fields, db_value empty. Raises UnsupportedForm."""

    @abstractmethod
    def fill(self, page, fields: list[AdapterField], resume_path: Path | None) -> tuple[list, list]:
        """Fill every field that has a value (db_value, or placeholder). Returns
        (filled, failed): fields it filled and fields it tried and could not."""

    @abstractmethod
    def submit(self, page, dry_run: bool) -> bool:
        """Click submit (not when dry_run). True if the button was found - and
        clicked, when not dry_run. Raises UnsupportedForm if there is no button."""

    def start_url(self, url: str) -> str:
        """Where the application form is, given the job's URL."""
        return url
