"""Find the questions on an application form.

A generic DOM scan (labels, options, required flags) works on Greenhouse, Lever and Ashby;
for Greenhouse it's enriched with the job's question list from the public API, which has
clean labels and the options of the custom dropdowns.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from ..sources.base import USER_AGENT

KINDS = ("text", "textarea", "select", "combobox", "date", "monthyear", "radio", "checkboxes", "checkbox", "yesno", "file")


@dataclass
class Field:
    aa: str  # data-aa attribute we tag the element(s) with
    kind: str
    label: str
    required: bool = False
    options: list[str] = field(default_factory=list)
    multiple: bool = False
    name: str | None = None
    id: str | None = None
    frame: int = 0  # index into page.frames

    @property
    def is_essay(self) -> bool:
        return self.kind == "textarea"


SCAN_JS = r"""
(prefix) => {
  const clean = s => (s || '').replace(/[✱*]/g, ' ').replace(/\s+/g, ' ').trim();
  // Question text is the first meaningful line; later lines are hints, errors, widget text.
  const firstLine = s => {
    const lines = (s || '').split('\n').map(clean).filter(Boolean);
    if (!lines.length) return '';
    return lines[0].length >= 3 || lines.length === 1 ? lines[0] : lines.slice(0, 2).join(' ');
  };
  const isVisible = el => {
    const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none';
  };
  const reqMark = s => /[✱*]\s*$/.test((s || '').trim()) || /\(required\)/i.test(s || '');
  // Visible text of a node, skipping form controls and hidden hint/error elements.
  // Text from different elements goes on separate lines, so firstLine() gets the question.
  const SKIP = 'input, select, textarea, option, button, svg, [role=listbox], [aria-hidden=true]';
  const textWithout = (node) => {
    const parts = [];
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
    let lastParent = null;
    for (let t = walker.nextNode(); t; t = walker.nextNode()) {
      const p = t.parentElement;
      if (!p || p.closest(SKIP)) continue;
      if (p.checkVisibility && !p.checkVisibility()) continue;
      const s = t.textContent.replace(/\s+/g, ' ').trim();
      if (!s) continue;
      parts.push((lastParent && lastParent !== p ? '\n' : ' ') + s);
      lastParent = p;
    }
    return parts.join('').trim();
  };
  const isControl = c => {
    const t = (c.type || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(t)) return false;
    if (/recaptcha|captcha/i.test((c.name || '') + (c.id || '') + (c.className || ''))) return false;
    if (c.getAttribute('aria-hidden') === 'true') return false;
    return true;
  };
  const GENERIC = /^(attach|upload|browse|choose file|select|select\.\.\.|drop files here|enter manually|dropbox|google drive)$/i;

  let lastLabelEl = null;
  const labelRequired = (lab) => !!lab && (
    /required/i.test(lab.className || '') || !!lab.querySelector('[class*=required], [class*=Required]') ||
    /\*/.test(getComputedStyle(lab, '::after').content || '') || /\*/.test(getComputedStyle(lab, '::before').content || ''));

  function containerLabel(el, members) {
    let node = el.parentElement;
    for (let i = 0; i < 7 && node && node !== document.body; i++, node = node.parentElement) {
      const others = Array.from(node.querySelectorAll('input, textarea, select'))
        .filter(c => isControl(c) && !members.includes(c));
      if (others.length) break;
      const cands = node.querySelectorAll('label, legend, [class*=label], [class*=Label], [class*=question], [class*=title], h3, h4');
      for (const lab of cands) {
        if (members.some(m => lab.contains(m))) continue;
        const t = clean(textWithout(lab));
        if (t && !GENERIC.test(t) && !members.some(m => clean(optLabel(m)) === t)) { lastLabelEl = lab; return textWithout(lab); }
      }
    }
    return '';
  }

  function optLabel(el) {
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return textWithout(l); }
    const l = el.closest('label'); if (l) return textWithout(l);
    const sib = el.nextElementSibling; if (sib) return sib.innerText || '';
    return el.value || '';
  }

  function labelFor(el) {
    let raw = '';
    lastLabelEl = null;
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) { raw = textWithout(l); lastLabelEl = l; } }
    if (!clean(raw) && el.getAttribute('aria-labelledby'))
      raw = el.getAttribute('aria-labelledby').split(/\s+/).map(i => (document.getElementById(i) || {}).innerText || '').join(' ');
    if (!clean(raw) && el.getAttribute('aria-label')) raw = el.getAttribute('aria-label');
    if (!clean(raw)) { const l = el.closest('label'); if (l) raw = textWithout(l); }
    if (!clean(raw) || GENERIC.test(clean(raw))) raw = containerLabel(el, [el]) || raw;
    if (!clean(raw) && el.placeholder) raw = el.placeholder;
    return raw;
  }

  const fields = [];
  let n = 0;
  const mark = (el, id, opt) => { el.setAttribute('data-aa', id); if (opt !== undefined) el.setAttribute('data-aa-opt', String(opt)); };
  const all = Array.from(document.querySelectorAll('input, textarea, select')).filter(isControl);
  const groups = new Map();

  // Month + Year dropdown pairs (Ashby education Start/End Date): one question, two selects.
  const optTexts = s => Array.from(s.options).map(o => o.text.trim());
  const isMonthSel = s => s.tagName === 'SELECT' &&
    optTexts(s).filter(t => /^(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)/i.test(t)).length >= 12;
  const isYearSel = s => s.tagName === 'SELECT' && optTexts(s).filter(t => /^\d{4}$/.test(t)).length >= 5;
  const pairs = [], paired = new Set();
  for (const m of all.filter(isMonthSel)) {
    let node = m.parentElement;
    for (let i = 0; i < 3 && node; i++, node = node.parentElement) {
      const sels = Array.from(node.querySelectorAll('select')).filter(isControl);
      if (sels.length > 2) break;
      const y = sels.find(s => s !== m && !paired.has(s) && isYearSel(s));
      if (y) { pairs.push([m, y]); paired.add(m); paired.add(y); break; }
    }
  }

  for (const el of all) {
    if (paired.has(el)) continue;
    const t = (el.type || '').toLowerCase();
    // Ashby names each checkbox of a multi-select after its option; its id says which question it belongs to.
    const groupKey = (el.id && /-labeled-(checkbox|radio)-\d+$/.test(el.id)) ? el.id.replace(/-labeled-(checkbox|radio)-\d+$/, '') : el.name;
    if ((t === 'radio' || t === 'checkbox') && groupKey) {
      if (!groups.has(groupKey)) groups.set(groupKey, []);
      groups.get(groupKey).push(el);
      continue;
    }
    if (t === 'checkbox') { groups.set('__single_' + (n++), [el]); continue; }
    const visible = isVisible(el);
    if (t !== 'file' && !visible) continue;
    const id = prefix + (n++);
    mark(el, id);
    const raw = labelFor(el);
    let kind = el.tagName === 'TEXTAREA' ? 'textarea' : el.tagName === 'SELECT' ? 'select' : t === 'file' ? 'file' : 'text';
    if (el.getAttribute('role') === 'combobox') kind = 'combobox';
    if (t === 'date' || (kind === 'text' && (/date/i.test(el.className || '') || el.closest('.react-datepicker-wrapper') ||
        /^(pick (a )?date|mm\/dd\/yyyy|dd\/mm\/yyyy|yyyy-mm-dd)/i.test(el.placeholder || '')))) kind = 'date';
    const f = { aa: id, kind, label: firstLine(raw), required: !!(el.required || el.getAttribute('aria-required') === 'true' || reqMark(raw) || labelRequired(lastLabelEl)),
                name: el.name || null, id: el.id || null, options: [], multiple: !!el.multiple };
    if (kind === 'select') f.options = Array.from(el.options).filter(o => o.value !== '' && !/^select/i.test(o.text.trim())).map(o => clean(o.text));
    fields.push(f);
  }

  for (const [m, y] of pairs) {
    if (!isVisible(m) && !isVisible(y) && !isVisible(m.parentElement)) continue;
    const id = prefix + (n++);
    mark(m, id, 0); mark(y, id, 1);
    // The label usually points at the wrapper around both selects (label[for=wrapper id]).
    let raw = '', labEl = null;
    for (let node = m.parentElement, i = 0; i < 5 && node && !raw; i++, node = node.parentElement) {
      if (node.id) { const l = document.querySelector(`label[for="${CSS.escape(node.id)}"]`); if (l) { labEl = l; raw = textWithout(l); } }
    }
    if (!clean(raw)) { lastLabelEl = null; raw = containerLabel(m, [m, y]); labEl = lastLabelEl; }
    let label = firstLine(raw);
    // "Start Date" inside "Education History" -> "Education History: Start Date" (not "when can you start").
    const entry = m.closest('[data-field-path], fieldset');
    const head = entry && entry.querySelector('label, legend, [class*=question-title]');
    const section = head ? firstLine(textWithout(head)) : '';
    if (section && label && section !== label && !label.toLowerCase().includes(section.toLowerCase())) label = section + ': ' + label;
    fields.push({ aa: id, kind: 'monthyear', label, required: !!(m.required || reqMark(raw) || labelRequired(labEl)),
                  name: m.name || null, id: (labEl && labEl.htmlFor) || null, options: [], multiple: false });
  }

  for (const [name, members] of groups) {
    const id = prefix + (n++);
    const visibleMembers = members.filter(isVisible);
    // Ashby yes/no: a hidden checkbox next to "Yes" / "No" buttons.
    if (members.length === 1 && !visibleMembers.length) {
      let node = members[0].parentElement, buttons = [];
      for (let i = 0; i < 4 && node; i++, node = node.parentElement) {
        buttons = Array.from(node.querySelectorAll('button')).filter(b => /^(yes|no)$/i.test(clean(b.innerText)));
        if (buttons.length >= 2) break;
      }
      if (buttons.length >= 2) {
        mark(members[0], id);
        buttons.forEach((b, i) => mark(b, id, i));
        lastLabelEl = null;
        const raw = containerLabel(members[0], [members[0], ...buttons]);
        fields.push({ aa: id, kind: 'yesno', label: firstLine(raw), required: reqMark(raw) || labelRequired(lastLabelEl), name: members[0].name || null,
                      id: members[0].id || null, options: buttons.map(b => clean(b.innerText)), multiple: false });
      }
      continue;
    }
    if (!visibleMembers.length) continue;
    members.forEach((m, i) => mark(m, id, i));
    const isRadio = members[0].type === 'radio';
    if (members.length === 1) {
      const raw = optLabel(members[0]) || labelFor(members[0]);
      fields.push({ aa: id, kind: 'checkbox', label: firstLine(raw), required: !!members[0].required || reqMark(raw),
                    name: members[0].name || null, id: members[0].id || null, options: [], multiple: false });
      continue;
    }
    lastLabelEl = null;
    const raw = containerLabel(members[0], members);
    fields.push({ aa: id, kind: isRadio ? 'radio' : 'checkboxes', label: firstLine(raw),
                  required: members.some(m => m.required) || reqMark(raw) || labelRequired(lastLabelEl), name, id: null,
                  options: members.map(m => clean(optLabel(m))), multiple: !isRadio });
  }
  return fields;
}
"""

READ_JS = r"""
() => {
  const out = {}, monthYear = {};
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  for (const el of document.querySelectorAll('[data-aa]')) {
    const id = el.getAttribute('data-aa');
    const opt = el.getAttribute('data-aa-opt');
    const t = (el.type || '').toLowerCase();
    if (opt !== null && el.tagName === 'SELECT') {  // month/year pair -> "March 2023"
      (monthYear[id] = monthYear[id] || ['', ''])[+opt] = el.value !== '' ? clean(el.options[el.selectedIndex].text) : '';
      continue;
    }
    if (opt !== null) {
      const on = el.tagName === 'BUTTON' ? (el.getAttribute('aria-pressed') === 'true' || /active|selected/i.test(el.className))
                                         : el.checked;
      if (!(id in out)) out[id] = [];
      if (on) {
        let lab = '';
        if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) lab = l.innerText; }
        if (!lab) { const l = el.closest('label'); lab = l ? l.innerText : (el.innerText || el.value); }
        out[id].push(clean(lab));
      }
      continue;
    }
    if (t === 'file') { out[id] = el.files && el.files.length ? '[file]' : ''; continue; }
    if (t === 'checkbox' && !(id in out)) { out[id] = el.checked ? 'Yes' : ''; continue; }
    if (el.tagName === 'SELECT') { out[id] = el.selectedIndex > 0 ? clean(el.options[el.selectedIndex].text) : ''; continue; }
    if (el.getAttribute('role') === 'combobox') {
      let node = el.parentElement, val = '';
      for (let i = 0; i < 4 && node && !val; i++, node = node.parentElement) {
        const sv = node.querySelectorAll('[class*=single-value], [class*=singleValue], [class*=multi-value__label], [class*=multiValue]');
        if (sv.length) val = Array.from(sv).map(s => clean(s.innerText)).join(' | ');
      }
      out[id] = val || el.value || '';
      continue;
    }
    if (!(id in out)) out[id] = el.value || '';
  }
  for (const [id, parts] of Object.entries(monthYear)) out[id] = parts.filter(Boolean).join(' ');
  return out;
}
"""


def scan(page) -> list[Field]:
    """Scan the main page and same-origin iframes (some boards embed the form in an iframe)."""
    fields: list[Field] = []
    for i, frame in enumerate(page.frames):
        if i and ("captcha" in frame.url or frame.url in ("about:blank", "")):
            continue
        try:
            raw = frame.evaluate(SCAN_JS, f"aa{i}-")
        except Exception:
            continue
        for r in raw:
            fields.append(Field(frame=i, **r))
    # Some boards have a "upload to autofill" box separate from the real resume field; don't touch it.
    return [f for f in fields if (f.label or f.kind == "file") and "autofill" not in f.label.lower()]


def read_values(page) -> dict[str, object]:
    values: dict[str, object] = {}
    for frame in page.frames:
        try:
            values.update(frame.evaluate(READ_JS))
        except Exception:
            pass
    return values


def load_combobox_options(page, f: Field) -> None:
    """Custom dropdowns only render their options when opened."""
    if f.options:
        return
    frame = page.frames[f.frame]
    loc = frame.locator(f'[data-aa="{f.aa}"]').first
    try:
        loc.click(timeout=3000)
        frame.wait_for_timeout(400)
        f.options = _open_options(frame, loc)[:400]
        loc.press("Escape")
    except Exception:
        pass


def _open_options(frame, loc) -> list[str]:
    """Option texts of the listbox this combobox controls (not some other widget's list)."""
    lb = loc.get_attribute("aria-controls") or loc.get_attribute("aria-owns")
    if lb:
        opts = frame.locator(f'[id="{lb}"] [role="option"]')
        if opts.count():
            return [t.strip() for t in opts.all_inner_texts() if t.strip()]
    # react-select renders the menu next to the input, inside the same control container
    container = loc.locator("xpath=ancestor::*[.//*[@role='option']][1]")
    if container.count():
        return [t.strip() for t in container.first.locator('[role="option"]').all_inner_texts() if t.strip()]
    return []


# ---------------------------------------------------------------------------
# Greenhouse: use the public question list
# ---------------------------------------------------------------------------


def greenhouse_questions(slug: str, job_id: str) -> dict[str, dict]:
    """field name/id -> {label, required, options, multiple} from the job board API."""
    try:
        r = httpx.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{job_id}",
                      params={"questions": "true"}, headers={"User-Agent": USER_AGENT}, timeout=20)
        r.raise_for_status()
        data = r.json()
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for q in data.get("questions") or []:
        for f in q.get("fields") or []:
            out[f["name"]] = {
                "label": q.get("label", ""),
                "required": bool(q.get("required")),
                "options": [v["label"] for v in f.get("values") or []],
                "multiple": f.get("type") == "multi_value_multi_select",
            }
    for q in (data.get("demographic_questions") or {}).get("questions") or []:
        out[str(q.get("id"))] = {
            "label": q.get("label", ""),
            "required": bool(q.get("required")),
            "options": [o.get("label", "") for o in q.get("answer_options") or []],
            "multiple": q.get("type") == "multi_value_multi_select",
        }
    for c in data.get("compliance") or []:
        for q in c.get("questions") or []:
            for f in q.get("fields") or []:
                out.setdefault(f["name"], {
                    "label": q.get("label", ""),
                    "required": bool(q.get("required")),
                    "options": [v["label"] for v in f.get("values") or []],
                    "multiple": False,
                })
    return out


def enrich_greenhouse(fields: list[Field], questions: dict[str, dict]) -> None:
    for f in fields:
        q = questions.get(f.id or "") or questions.get(f.name or "")
        if not q:
            continue
        if q["label"]:
            f.label = q["label"]
        f.required = f.required or q["required"]
        if q["options"] and not f.options:
            f.options = q["options"]
        f.multiple = f.multiple or q["multiple"]
