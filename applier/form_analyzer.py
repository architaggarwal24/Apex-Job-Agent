"""
applier/form_analyzer.py — Universal LLM form field extractor.

Works on ANY application form: Indeed Easy Apply, LinkedIn Easy Apply,
Greenhouse, Lever, Workday, company ATS, direct forms — everything.

Instead of raw HTML (bloated, low signal), we extract a clean structured
summary of visible form fields using DOM traversal, then pass that to
the LLM to map each field to the user's profile.
"""

import logging
from dataclasses import dataclass, field, asdict

from core import llm as LLM

logger = logging.getLogger(__name__)

SYSTEM = """You are an expert job application form analyzer.
Given a structured list of form fields and a user profile, map each field.

Return ONLY valid JSON — no markdown, no explanation:
{
  "fields": [
    {
      "label": "Human readable label",
      "field_key": "profile_key",
      "selector": "CSS selector",
      "field_type": "text|email|tel|number|select|checkbox|radio|textarea|file|date|url",
      "options": [],
      "is_required": true,
      "db_value": "value from profile or empty string",
      "needs_manual": false,
      "placeholder": "fallback value if db_value empty"
    }
  ],
  "has_file_upload": false,
  "submit_selector": "button[type=submit]",
  "next_selector": ""
}

Profile field key reference:
  Personal:     full_name, first_name, last_name, email, phone, phone_alternate,
                date_of_birth, gender, nationality,
                location_city, location_state, location_country, location_pincode,
                linkedin_url, github_url, portfolio_url
  Professional: current_role, experience_years, notice_period_days, notice_period_label,
                current_ctc_lpa, expected_ctc_lpa, current_ctc_monthly, expected_ctc_monthly,
                salary_expectation, willing_to_relocate, work_mode_preference,
                availability, cover_letter
  Education:    highest_degree, degree_field, university_name, graduation_year,
                graduation_score, tenth_score, twelfth_score
  Identity:     authorized_to_work, require_sponsorship, veteran_status,
                disability_status, ethnicity
  Files:        resume_file, cover_letter_file

Rules:
- Look up db_value from the profile _flat dict provided
- If found and non-empty: db_value=value, needs_manual=false
- If empty/missing: db_value="", needs_manual=true
- For select fields with no db match: pick most neutral option as placeholder
- For unknown fields: create descriptive snake_case field_key
- selector should be as specific as possible: prefer [name=X], [id=X], #id"""


@dataclass
class FormField:
    label:       str
    field_key:   str
    selector:    str
    field_type:  str
    options:     list[str]
    is_required: bool
    db_value:    str
    needs_manual: bool
    placeholder: str

    def fill_value(self) -> str:
        return self.db_value or self.placeholder


@dataclass
class FormAnalysis:
    page_url:        str
    job_id:          str
    fields:          list[FormField] = field(default_factory=list)
    has_file_upload: bool = False
    submit_selector: str  = ""
    next_selector:   str  = ""

    @property
    def fillable(self): return [f for f in self.fields if f.db_value]
    @property
    def missing(self):  return [f for f in self.fields if f.needs_manual]

    def to_dict(self):
        d = asdict(self)
        return d


def extract_fields(page) -> str:
    """
    Extract a clean structured summary of all visible form fields.
    Uses DOM traversal to get label + type + selector + options.
    This is ~80% smaller than raw HTML and gives the LLM much better accuracy.
    """
    try:
        structured = page.evaluate(r"""() => {
            function getLabel(el) {
                if (el.getAttribute('aria-label')) return el.getAttribute('aria-label');
                const lby = el.getAttribute('aria-labelledby');
                if (lby) {
                    const parts = lby.split(' ');
                    const texts = parts.map(id => {
                        const e = document.getElementById(id);
                        return e ? e.innerText.trim() : '';
                    }).filter(Boolean);
                    if (texts.length) return texts.join(' ');
                }
                if (el.id) {
                    const le = document.querySelector('label[for="' + el.id + '"]');
                    if (le) return le.innerText.trim();
                }
                const pl = el.closest('label');
                if (pl) return pl.innerText.replace(el.value || '', '').trim();
                let sib = el.previousElementSibling;
                while (sib) {
                    const t = sib.tagName; const c = (sib.className || '').toString();
                    if (t === 'LABEL' || c.toLowerCase().includes('label')) return sib.innerText.trim();
                    sib = sib.previousElementSibling;
                }
                const par = el.parentElement;
                if (par) {
                    const pt = par.innerText.split('\n')[0].trim();
                    if (pt && pt.length < 120) return pt;
                }
                return el.placeholder || el.name || el.id || '';
            }

            const results = [];
            const seen = new Set();
            const inputs = document.querySelectorAll(
                'input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=reset]),' +
                'select,textarea'
            );

            inputs.forEach(el => {
                const r = el.getBoundingClientRect();
                if (r.width === 0 || r.height === 0) return;

                const tag = el.tagName.toLowerCase();
                const tp  = (el.getAttribute('type') || tag).toLowerCase();
                let sel   = '';
                if (el.id)   sel = '#' + el.id;
                else if (el.name) sel = tag + '[name="' + el.name + '"]';
                else sel = tag;

                const key = sel + tp;
                if (seen.has(key)) return;
                seen.add(key);

                const f = {
                    label:    getLabel(el),
                    type:     tp,
                    selector: sel,
                    name:     el.name || '',
                    required: el.required || el.getAttribute('aria-required') === 'true',
                    value:    el.value || ''
                };

                if (tag === 'select') {
                    f.type = 'select';
                    f.options = Array.from(el.options)
                        .map(o => o.text.trim())
                        .filter(t => t && t !== '--' && t !== '-' && t !== 'Select');
                }

                if (tp === 'radio' || tp === 'checkbox') {
                    f.checked = el.checked;
                    f.radio_value = el.value;
                    const fs = el.closest('fieldset');
                    if (fs) {
                        const lg = fs.querySelector('legend');
                        if (lg) f.group = lg.innerText.trim();
                    }
                }

                results.push(f);
            });

            return JSON.stringify(results);
        }""")

        if not structured:
            return ""

        import json as _j
        fields = _j.loads(structured)
        if not fields:
            return ""

        lines = [f"FORM FIELDS ON: {page.url}", ""]
        for i, f in enumerate(fields, 1):
            lbl = f.get("label") or f.get("name") or f"Field {i}"
            req = " [REQUIRED]" if f.get("required") else ""
            lines.append(f"{i}. {lbl}{req}")
            lines.append(f"   type={f.get('type','text')} | selector={f.get('selector','')}")
            if f.get("options"):
                lines.append(f"   options: {', '.join(f['options'][:15])}")
            if f.get("group"):
                lines.append(f"   group: {f['group']}")
            if f.get("value"):
                lines.append(f"   current_value: {f['value']}")
            lines.append("")

        return "\n".join(lines)

    except Exception as e:
        logger.debug(f"Structured extraction error: {e}")
        try:
            html = page.evaluate("""() => {
                const forms = document.querySelectorAll('form');
                if (forms.length > 0)
                    return Array.from(forms).map(f => f.outerHTML).join('\\n');
                const m = document.querySelector('main,[role=main],.application');
                return m ? m.outerHTML : document.body.innerHTML;
            }""")
            return (html or "")[:20000]
        except Exception:
            return ""


def analyze(page_html: str, page_url: str, job_id: str,
            job_title: str, company: str, profile: dict,
            only_keys: list[str] | None = None) -> FormAnalysis:
    """
    Send structured field summary to LLM and get back a fill plan.
    """
    flat = profile.get("_flat", profile)

    focus = ""
    if only_keys:
        focus = (f"\n\nFOCUS (iteration 2): Only analyze fields with these profile keys: "
                 f"{only_keys}. Skip all others.")

    prompt = f"""Analyze this job application form.

Job: {job_title} at {company}
URL: {page_url}{focus}

USER PROFILE (map db_value from here):
Name: {flat.get('full_name','')} | Email: {flat.get('email','')} | Phone: {flat.get('phone','')}
Role: {flat.get('current_role','')} | Exp: {flat.get('experience_years','0')}yrs
City: {flat.get('location_city','')} | Country: {flat.get('location_country','India')}
LinkedIn: {flat.get('linkedin_url','')} | GitHub: {flat.get('github_url','')}
Degree: {flat.get('highest_degree','')} in {flat.get('degree_field','')} from {flat.get('university_name','')} ({flat.get('graduation_year','')})
Expected CTC: {flat.get('expected_ctc_lpa','')} LPA | Notice: {flat.get('notice_period_label','Immediate')}
Work mode: {flat.get('work_mode_preference','Hybrid')} | Relocate: {flat.get('willing_to_relocate','Yes')}

Full profile (for any other fields):
{str({k:v for k,v in flat.items() if v})[:2000]}

FORM FIELDS:
{page_html}"""

    logger.info(f"  [FormAnalyzer] Calling LLM for {company} "
                f"({len(page_html):,} chars, only_keys={bool(only_keys)})")

    try:
        result = LLM.chat_json(
            messages=[{"role": "user", "content": prompt}],
            system=SYSTEM,
            max_tokens=4096,
        )
    except Exception as e:
        logger.error(f"  [FormAnalyzer] LLM failed: {e}")
        return FormAnalysis(page_url=page_url, job_id=job_id)

    fields = []
    for fd in result.get("fields", []):
        fields.append(FormField(
            label=        fd.get("label",       ""),
            field_key=    fd.get("field_key",   ""),
            selector=     fd.get("selector",    ""),
            field_type=   fd.get("field_type",  "text"),
            options=      fd.get("options",     []),
            is_required=  fd.get("is_required", False),
            db_value=     fd.get("db_value",    ""),
            needs_manual= fd.get("needs_manual",False),
            placeholder=  fd.get("placeholder", ""),
        ))

    analysis = FormAnalysis(
        page_url=        page_url,
        job_id=          job_id,
        fields=          fields,
        has_file_upload= result.get("has_file_upload", False),
        submit_selector= result.get("submit_selector", ""),
        next_selector=   result.get("next_selector",   ""),
    )
    logger.info(f"  [FormAnalyzer] {len(fields)} fields: "
                f"{len(analysis.fillable)} fillable, {len(analysis.missing)} missing")
    return analysis
