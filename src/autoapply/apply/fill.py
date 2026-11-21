"""Put answers into form fields. Never clicks submit."""

from __future__ import annotations

from .answers import match_option
from .scan import Field, _open_options


class FillError(RuntimeError):
    pass


def _loc(page, f: Field, opt: int | None = None):
    frame = page.frames[f.frame]
    sel = f'[data-aa="{f.aa}"]' + (f'[data-aa-opt="{opt}"]' if opt is not None else ":not([data-aa-opt])")
    return frame, frame.locator(sel).first


def _as_list(value) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


def fill(page, f: Field, value) -> None:
    """Fill one field. value: text, option text(s), file path, or 'Yes'/'No' for a checkbox."""
    frame, loc = _loc(page, f)
    try:
        if f.kind == "file":
            loc.set_input_files(str(value), timeout=10_000)
        elif f.kind in ("text", "textarea"):
            if f.kind == "text" and _is_place(f):
                _type_place(frame, loc, str(value))
            else:
                loc.fill(str(value), timeout=5_000)
        elif f.kind == "select":
            idx = match_option(str(value), f.options)
            if idx is None:
                raise FillError(f"'{value}' isn't one of the options")
            loc.select_option(label=f.options[idx], timeout=5_000)
        elif f.kind == "combobox":
            for v in _as_list(value):
                _combobox_pick(frame, loc, v)
        elif f.kind in ("radio", "checkboxes", "yesno"):
            for v in _as_list(value):
                idx = match_option(v, f.options)
                if idx is None:
                    raise FillError(f"'{v}' isn't one of the options")
                _, opt = _loc(page, f, idx)
                if f.kind == "yesno":
                    opt.click(timeout=5_000)
                else:
                    try:
                        opt.check(timeout=3_000)
                    except Exception:
                        opt.check(force=True, timeout=3_000)
        elif f.kind == "checkbox":
            if str(value).strip().lower() in ("yes", "true", "1", "checked", "y"):
                try:
                    loc.check(timeout=3_000)
                except Exception:
                    loc.check(force=True, timeout=3_000)
        else:
            raise FillError(f"don't know how to fill a {f.kind}")
    except FillError:
        raise
    except Exception as e:
        raise FillError(str(e).splitlines()[0][:160]) from e


def _combobox_pick(frame, loc, value: str) -> None:
    """Open a searchable dropdown, type to filter, click the matching option."""
    loc.click(timeout=5_000)
    loc.fill("")
    loc.type(value[:30], delay=15)
    frame.wait_for_timeout(500)
    options = _open_options(frame, loc)
    idx = match_option(value, options)
    if idx is None:
        # Filtering may hide the right option (e.g. typed "Yes" vs "Yes, I am…"); clear and look again.
        loc.fill("")
        frame.wait_for_timeout(300)
        options = _open_options(frame, loc)
        idx = match_option(value, options)
    if idx is None:
        loc.press("Escape")
        raise FillError(f"no option matching '{value}'")
    lb = loc.get_attribute("aria-controls") or loc.get_attribute("aria-owns")
    scope = frame.locator(f'[id="{lb}"]') if lb and frame.locator(f'[id="{lb}"]').count() else \
        loc.locator("xpath=ancestor::*[.//*[@role='option']][1]").first
    scope.locator('[role="option"]').nth(idx).click(timeout=5_000)
    frame.wait_for_timeout(150)


def _is_place(f: Field) -> bool:
    text = f"{f.label} {f.id or ''} {f.name or ''}".lower()
    return any(w in text for w in ("location", "city", "where are you", "based"))


SUGGESTIONS = ['.dropdown-results > *', '[role="listbox"] [role="option"]', ".pac-item", '[class*=suggestion]']


def _type_place(frame, loc, value: str) -> None:
    """Location fields often only accept a place picked from their suggestions (e.g. Lever),
    and only suggest on real keystrokes. Type it, then take the first suggestion if one shows up."""
    loc.click(timeout=5_000)
    loc.fill("")
    loc.press_sequentially(value, delay=35)
    for _ in range(10):
        frame.wait_for_timeout(250)
        for sel in SUGGESTIONS:
            sugg = frame.locator(sel)
            try:
                if sugg.count() and sugg.first.is_visible() and sugg.first.inner_text().strip():
                    sugg.first.click(timeout=2_000)
                    return
            except Exception:
                continue
    # no suggestion list: the typed text stays


BUSY_RE = r"analy[sz]ing|parsing|uploading|processing your resume"


def wait_until_idle(page, timeout_ms: int = 20_000) -> None:
    """After an upload, some boards read the resume and rewrite fields; wait for that to finish."""
    import re as _re

    waited = 0
    page.wait_for_timeout(800)
    while waited < timeout_ms:
        try:
            busy = page.get_by_text(_re.compile(BUSY_RE, _re.I)).first
            if not (busy.count() and busy.is_visible()):
                return
        except Exception:
            return
        page.wait_for_timeout(500)
        waited += 500


HIGHLIGHT_JS = r"""
(ids) => {
  for (const id of ids) {
    for (const el of document.querySelectorAll(`[data-aa="${id}"]`)) {
      let target = el;
      if (el.getAttribute('role') === 'combobox')
        target = el.closest('[class*=control], [class*=Control]') || el.parentElement.parentElement || el;
      else if (['file', 'checkbox', 'radio'].includes(el.type) || el.tagName === 'BUTTON')
        target = el.closest('label, li, fieldset, div') || el;
      target.style.outline = '3px solid #f5a623';
      target.style.outlineOffset = '2px';
    }
  }
}
"""


def highlight(page, fields: list[Field]) -> None:
    """Outline fields the user still needs to handle."""
    by_frame: dict[int, list[str]] = {}
    for f in fields:
        by_frame.setdefault(f.frame, []).append(f.aa)
    for i, ids in by_frame.items():
        try:
            page.frames[i].evaluate(HIGHLIGHT_JS, ids)
        except Exception:
            pass
