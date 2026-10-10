# applier/adapters/greenhouse.py
"""
Greenhouse (boards.greenhouse.io, job-boards.greenhouse.io): the application
form sits on the job's own page. Standard fields are first_name, last_name,
email, phone and a resume upload; everything else is the company's own
questions (text, textarea, selects - native or the newer React-select
combobox - and yes/no radios). Demographic ("voluntary self-identification")
questions are left alone.

A form on a company's OWN site that embeds Greenhouse in an iframe is not
reached by this adapter (its site type is not "greenhouse" from the URL, and
the form is not in the page): the universal filler handles those.

Written against a saved copy of Greenhouse-style markup (tests/fixtures);
live Greenhouse boards were not tested.
"""

from applier.adapters.form import FormAdapter


class GreenhouseAdapter(FormAdapter):
    name = "greenhouse"
    site_types = ("greenhouse",)

    scope_selectors = ("#application_form", "form#application-form", "form.application--form",
                       "form[action*='greenhouse.io']")
    scope_probe = "input#first_name, input[name$='[first_name]']"
    submit_selectors = ("#submit_app", "button[type=submit]", "input[type=submit]",
                        "button:has-text('Submit application')")
    skip_name_rx = r"demographic|eeoc|veteran|disabilit|hispanic|\[gender\]|\[race\]"
    skip_containers = ("#eeoc_fields", "#demographic_questions", "[id*='eeoc']", "[class*='eeoc']",
                       "[class*='demographic']", "[data-section='eeoc']")
