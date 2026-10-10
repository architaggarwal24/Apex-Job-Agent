# applier/adapters/form.py
"""
FormAdapter: an Adapter for the common case - an ordinary HTML application
form - driven by a few class attributes, so a site's adapter is a small
declarative file (see greenhouse.py and lever.py):

    name, site_types           who it is and which site types it serves
    scope_selectors            CSS for the <form> (tried in order)
    scope_probe                (optional) a selector for a control that only
                               this form has; its <form> is the scope if no
                               scope selector matched
    submit_selectors           CSS/Playwright selectors for the submit button
    name_keys                  {control name: standard key} for names the site
                               always uses ("urls[LinkedIn]" -> linkedin_url);
                               "" means "known, but not a standard question"
    skip_name_rx / skip_containers
                               controls to leave alone (demographic questions)

HOW IT READS A FORM. One page.evaluate() walks the form's controls and returns,
for each, its label (label[for], aria-labelledby, a wrapping <label>, the
question's container), its type, whether it is mandatory, and its options. A
radio or checkbox group is ONE field whose options are its choices. The Python
side then names each field (a standard key, else a key from its label) and
refuses, with UnsupportedForm, anything it could not answer if it were
mandatory: a group of checkboxes, a multi-select, an unknown file upload.

HOW IT FILLS. With Playwright's own fill / select_option / check / set_input_files
(no human-style typing, no LLM), then READS THE VALUE BACK; a field counts as
filled only if the page now holds what was meant. Native selects, radio groups,
single checkboxes, file inputs and ARIA comboboxes (the React-select widget
newer Greenhouse boards use) are handled.

Everything here was written against saved HTML (tests/fixtures), not live
sites.
"""

import logging
import random
import re
import time
from pathlib import Path

from core.config import cfg
from applier.adapters import standard
from applier.adapters.base import Adapter, AdapterField, UnsupportedForm

logger = logging.getLogger(__name__)

PAUSE = (0.15, 0.4)                    # a short, human-ish gap between fields (tests set it to (0, 0))
FILL_TIMEOUT_MS = 5000
YES = ("yes", "true", "1", "on", "checked", "y")

# One round trip: the form's controls, labelled. Returns {found, controls}.
_SCAN_JS = r"""(opts) => {
  const norm = s => (s || '').replace(/\s+/g, ' ').trim();
  const q = sel => { try { return document.querySelector(sel); } catch (e) { return null; } };
  let scope = null;
  for (const sel of opts.scopes) { scope = q(sel); if (scope) break; }
  if (!scope && opts.probe) { const p = q(opts.probe); scope = p && p.closest('form'); }
  if (!scope) return {found: false, controls: [], frames: document.querySelectorAll('iframe').length};

  const esc = v => String(v).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  const visible = el => {
    if (!el.getClientRects().length) return false;
    const st = getComputedStyle(el);
    return st.visibility !== 'hidden' && st.display !== 'none';
  };
  const textOf = node => {
    const c = node.cloneNode(true);
    c.querySelectorAll('input,select,textarea,option,button,script,style,svg').forEach(x => x.remove());
    return norm(c.textContent);
  };
  const marked = node => !!node && (/[*\u2731]/.test(node.textContent) ||
                         !!node.querySelector('.required, .asterisk, [class*="required"]'));
  const blockOf = el => el.closest('.application-question, .custom-question, .input-wrapper, .field, ' +
                                   '.select, .file-upload, fieldset, .form-group');
  const titleIn = (box, el) => {
    if (!box) return null;
    for (const n of box.querySelectorAll('.application-label, .label, legend, [class*="question-label"], label')) {
      if (n.contains(el) && n.tagName === 'LABEL' && !n.querySelector('.application-label')) continue;
      if (n.tagName === 'LABEL' && n.querySelector('input,select,textarea')) continue;
      if (textOf(n)) return n;
    }
    return null;
  };
  // The node that names a control: label[for], aria-labelledby, a wrapping <label>, or the
  // container's own title. Returns {node, text}.
  const labelOf = el => {
    const lby = el.getAttribute('aria-labelledby');
    if (lby) {
      const nodes = lby.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean);
      const t = norm(nodes.map(textOf).join(' '));
      if (t) return {node: nodes[0], text: t};
    }
    if (el.id) {
      const le = document.querySelector('label[for="' + esc(el.id) + '"]');
      if (le && textOf(le)) return {node: le, text: textOf(le)};
    }
    const wrap = el.closest('label');
    if (wrap) {
      const inner = wrap.querySelector('.application-label, .label');
      const n = inner || wrap;
      if (textOf(n)) return {node: n, text: textOf(n)};
    }
    const box = blockOf(el), t = titleIn(box, el);
    if (t) return {node: t, text: textOf(t)};
    const al = el.getAttribute('aria-label');
    if (al) return {node: null, text: norm(al)};
    return {node: null, text: norm(el.getAttribute('placeholder') || el.getAttribute('name') || '')};
  };
  const groupLabel = els => {
    const first = els[0];
    const fs = first.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg && textOf(lg)) return {node: lg, text: textOf(lg)}; }
    const grp = first.closest('[role="radiogroup"],[role="group"]');
    if (grp) {
      const lby = grp.getAttribute('aria-labelledby');
      if (lby) {
        const nodes = lby.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean);
        const t = norm(nodes.map(textOf).join(' '));
        if (t) return {node: nodes[0], text: t};
      }
      if (grp.getAttribute('aria-label')) return {node: null, text: norm(grp.getAttribute('aria-label'))};
    }
    const box = blockOf(first), t = titleIn(box, first);
    if (t) return {node: t, text: textOf(t)};
    return labelOf(first);
  };
  const optionLabel = el => {
    const wrap = el.closest('label');
    if (wrap && textOf(wrap)) return textOf(wrap);
    if (el.id) { const le = document.querySelector('label[for="' + esc(el.id) + '"]'); if (le && textOf(le)) return textOf(le); }
    const sib = el.nextSibling && el.nextSibling.textContent;
    return norm(sib || el.value || '');
  };
  let idx = 0;
  const selOf = el => {
    if (el.id) return '[id="' + esc(el.id) + '"]';
    const n = el.getAttribute('name');
    if (n) {
      const v = (el.type === 'radio' || el.type === 'checkbox') ? '[value="' + esc(el.value) + '"]' : '';
      return el.tagName.toLowerCase() + '[name="' + esc(n) + '"]' + v;
    }
    el.setAttribute('data-apex-idx', String(++idx));
    return '[data-apex-idx="' + idx + '"]';
  };
  const skipContainer = el => (opts.skip_containers || []).some(s => { try { return !!el.closest(s); } catch (e) { return false; } });

  const controls = [], groups = new Map();
  const position = new Map();
  Array.from(scope.querySelectorAll('input, select, textarea')).forEach((e, i) => position.set(e, i));
  for (const el of scope.querySelectorAll('input, select, textarea')) {
    const tag = el.tagName.toLowerCase();
    const type = (tag === 'input' ? (el.getAttribute('type') || 'text') : tag).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset'].includes(type) || el.disabled) continue;
    if (type !== 'file' && el.closest('[aria-hidden="true"]')) continue;
    const isChoice = type === 'radio' || type === 'checkbox';
    if (type !== 'file' && !isChoice && !visible(el)) continue;
    const name = el.getAttribute('name') || '';
    const base = {tag, type, id: el.id || '', name, skip: skipContainer(el)};

    if (isChoice && name) {
      let g = groups.get(type + ':' + name);
      if (!g) { g = []; groups.set(type + ':' + name, g); }
      g.push(el);
      continue;
    }
    const lab = labelOf(el);
    const combobox = el.getAttribute('role') === 'combobox' || el.getAttribute('aria-haspopup') === 'listbox';
    const options = tag === 'select'
      ? Array.from(el.options).filter(o => o.value !== '' && !/^(select|choose|please)\b/i.test(norm(o.textContent)))
             .map(o => norm(o.textContent))
      : [];
    controls.push({...base, kind: 'single', selector: selOf(el), label: lab.text,
                   required: el.required || el.getAttribute('aria-required') === 'true' || marked(lab.node),
                   options, combobox, multiple: !!el.multiple, order: position.get(el)});
  }
  // radio / checkbox groups become one control each; a lone checkbox is a single yes/no
  for (const [key, els] of groups) {
    const type = key.split(':')[0];
    const lab = groupLabel(els);
    const single = type === 'checkbox' && els.length === 1;
    const own = single ? labelOf(els[0]) : null;
    const optSel = {};
    const options = els.map(e => { const l = optionLabel(e); optSel[l] = selOf(e); return l; });
    controls.push({tag: 'input', type, id: els[0].id || '', name: els[0].getAttribute('name'),
                   skip: els.every(skipContainer), kind: single ? 'single' : 'group',
                   selector: single ? selOf(els[0]) : 'input[name="' + esc(els[0].getAttribute('name')) + '"]',
                   label: single ? (own.text || lab.text) : lab.text,
                   required: els.some(e => e.required || e.getAttribute('aria-required') === 'true') || marked(lab.node),
                   options: single ? [] : options, option_selectors: single ? {} : optSel,
                   combobox: false, multiple: type === 'checkbox' && els.length > 1,
                   order: position.get(els[0])});
  }
  controls.sort((a, b) => a.order - b.order);
  return {found: true, controls};
}"""


class FormAdapter(Adapter):
    scope_selectors: tuple[str, ...] = ("form",)
    scope_probe: str = ""
    submit_selectors: tuple[str, ...] = ("button[type=submit]", "input[type=submit]")
    name_keys: dict[str, str] = {}
    skip_name_rx: str = r"(?!x)x"                # matches nothing
    skip_containers: tuple[str, ...] = ()

    # ── Adapter ────────────────────────────────────────────────

    def can_handle(self, site_type: str, url: str) -> bool:
        return site_type in self.site_types

    def extract_fields(self, page) -> list[AdapterField]:
        raw = page.evaluate(_SCAN_JS, {"scopes": list(self.scope_selectors), "probe": self.scope_probe,
                                       "skip_containers": list(self.skip_containers)})
        if not raw.get("found"):
            where = " (the form seems to be inside an embedded frame)" if raw.get("frames") else ""
            raise UnsupportedForm(self.name, "no application form found on the page" + where)
        fields = []
        for c in raw["controls"]:
            f = self._field(c)
            if f is not None:
                fields.append(f)
        if not fields:
            raise UnsupportedForm(self.name, "the form has no fields this adapter can read")
        return fields

    def fill(self, page, fields, resume_path: Path | None):
        filled, failed = [], []
        for f in fields:
            ftype = (f.field_type or "").lower()
            if ftype == "file":
                path = self._upload_for(f, resume_path)
                if path is None:
                    continue
                (filled if self._fill_file(page, f, path) else failed).append(f)
                pause()
                continue
            value = f.fill_value()
            if not value:
                if f.needs_manual:
                    failed.append(f)
                continue
            ok = False
            try:
                ok = self._fill_one(page, f, value)
            except Exception as e:
                logger.debug(f"  [Adapter {self.name}] could not fill {f.label!r}: {e}")
            (filled if ok else failed).append(f)
            pause()
        return filled, failed

    def submit(self, page, dry_run: bool) -> bool:
        button = None
        for sel in self.submit_selectors:
            try:
                loc = page.locator(sel)
                if loc.count() and loc.first.is_visible():
                    button = loc.first
                    break
            except Exception:
                continue
        if button is None:
            raise UnsupportedForm(self.name, "no submit button found on the form")
        if not dry_run:
            button.click(timeout=FILL_TIMEOUT_MS)
        return True

    # ── reading ────────────────────────────────────────────────

    def _key(self, c: dict, label: str, ftype: str) -> str:
        name = c.get("name") or ""
        if name in self.name_keys:
            return self.name_keys[name]
        return standard.key_for(label, ftype)

    def _field(self, c: dict) -> AdapterField | None:
        label = standard.clean_label(c.get("label") or "")
        ftype = self._type(c)
        required = bool(c.get("required"))
        key = self._key(c, label, ftype)
        skippable = (c.get("skip") or re.search(self.skip_name_rx, c.get("name") or "", re.I)
                     or standard.EEO_LABEL.search(label))
        if skippable and not required:
            return None                                   # a demographic question: never filled or asked
        if not label:
            if required:
                raise UnsupportedForm(self.name, "a mandatory control has no label to answer it by")
            return None
        self._check_supported(c, label, ftype, key, required)
        return AdapterField(
            label=label, field_key=key or standard.slug(label), selector=c["selector"], field_type=ftype,
            options=list(c.get("options") or []), is_required=required, db_value="", needs_manual=False,
            placeholder="",
            meta={"name": c.get("name") or "", "id": c.get("id") or "", "kind": c.get("kind", "single"),
                  "combobox": bool(c.get("combobox")), "option_selectors": c.get("option_selectors") or {}})

    @staticmethod
    def _type(c: dict) -> str:
        t, tag = c.get("type", "text"), c.get("tag", "input")
        if c.get("combobox"):
            return "select"
        if tag == "textarea":
            return "textarea"
        if tag == "select":
            return "select"
        if t in ("email", "tel", "url", "number", "file", "date", "radio", "checkbox"):
            return t
        return "text"

    def _check_supported(self, c: dict, label: str, ftype: str, key: str, required: bool):
        """Refuse what we could not answer: only a MANDATORY question can make the
        form unsupported (an optional one we cannot handle is simply left blank)."""
        if not required:
            return
        if c.get("multiple"):
            raise UnsupportedForm(self.name, f"a mandatory multiple-choice question ({label!r}) is not supported")
        if ftype == "file":
            if key == "resume_file":
                return
            if key == "cover_letter_file" and (cfg.ROOT_DIR / "cover_letter.pdf").exists():
                return
            raise UnsupportedForm(self.name, f"a mandatory upload ({label!r}) that is not the resume")

    # ── filling ────────────────────────────────────────────────

    @staticmethod
    def _upload_for(f, resume_path: Path | None) -> Path | None:
        if f.field_key == "resume_file":
            path = resume_path or cfg.RESUME_PATH
        elif f.field_key == "cover_letter_file":
            path = cfg.ROOT_DIR / "cover_letter.pdf"
        else:
            return None
        return Path(str(path)) if path and Path(str(path)).exists() else None

    def _fill_file(self, page, f, path: Path) -> bool:
        try:
            loc = page.locator(f.selector).first
            loc.set_input_files(str(path), timeout=FILL_TIMEOUT_MS)
            return bool(loc.evaluate("el => el.files && el.files.length"))
        except Exception as e:
            logger.debug(f"  [Adapter {self.name}] upload failed for {f.label!r}: {e}")
            return False

    def _fill_one(self, page, f, value: str) -> bool:
        ftype, loc = (f.field_type or "").lower(), page.locator(f.selector).first
        if f.meta.get("combobox"):
            return self._fill_combobox(page, loc, value)
        if ftype in ("text", "email", "tel", "url", "number", "date", "textarea"):
            loc.fill(value, timeout=FILL_TIMEOUT_MS)
            return loc.input_value() == value
        if ftype == "select":
            return self._fill_select(loc, f, value)
        if ftype == "checkbox":
            want = value.strip().lower() in YES
            loc.set_checked(want, timeout=FILL_TIMEOUT_MS)
            return loc.is_checked() == want
        if ftype == "radio":
            return self._fill_radio(page, f, value)
        return False

    @staticmethod
    def _fill_select(loc, f, value: str) -> bool:
        for attempt in (lambda: loc.select_option(label=value, timeout=FILL_TIMEOUT_MS),
                        lambda: loc.select_option(value=value, timeout=FILL_TIMEOUT_MS)):
            try:
                attempt()
                break
            except Exception:
                continue
        else:
            return False
        chosen = loc.evaluate("el => el.selectedOptions.length ? el.selectedOptions[0].textContent.trim() : ''")
        return bool(chosen) and (chosen.casefold() == value.casefold() or value.casefold() in chosen.casefold())

    @staticmethod
    def _fill_radio(page, f, value: str) -> bool:
        options = f.meta.get("option_selectors") or {}
        want = next((s for label, s in options.items() if label.casefold() == value.strip().casefold()), None)
        if want is None:
            return False
        radio = page.locator(want).first
        try:
            radio.check(timeout=FILL_TIMEOUT_MS)
        except Exception:
            radio.check(force=True, timeout=FILL_TIMEOUT_MS)
        return radio.is_checked()

    @staticmethod
    def _fill_combobox(page, loc, value: str) -> bool:
        """An ARIA combobox (React-select): focus it, type the answer, pick the
        option that says it. Done only if an option was really chosen."""
        loc.click(timeout=FILL_TIMEOUT_MS)
        loc.fill(value, timeout=FILL_TIMEOUT_MS)
        options = page.locator('[role="option"]')
        try:
            options.first.wait_for(state="visible", timeout=FILL_TIMEOUT_MS)
        except Exception:
            return False
        texts = [t.strip() for t in options.all_inner_texts()]
        want = value.strip().casefold()
        pick = next((i for i, t in enumerate(texts) if t.casefold() == want), None)
        if pick is None:
            pick = next((i for i, t in enumerate(texts) if want in t.casefold()), None)
        if pick is None:
            return False
        options.nth(pick).click(timeout=FILL_TIMEOUT_MS)
        return True


def pause():
    lo, hi = PAUSE
    if hi > 0:
        time.sleep(random.uniform(lo, hi))
