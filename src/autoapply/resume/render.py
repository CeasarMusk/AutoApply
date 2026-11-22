"""Render a (tailored) resume to a one-page PDF with the Chromium Playwright already installed.

Plain single-column layout with real text, so applicant-tracking systems can read it. If the
content runs over a page, it first tightens spacing, then drops the least important lines
(see ``tailor.trim_once``) until it fits.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from jinja2 import Environment
from pypdf import PdfReader

from ..models import MasterResume
from .tailor import Tailored, trim_once

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def fmt_date(d: str | None) -> str:
    if not d:
        return ""
    m = re.fullmatch(r"(\d{4})-(\d{1,2})(?:-\d{1,2})?", d.strip())
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{MONTHS[int(m.group(2)) - 1]} {m.group(1)}"
    return d.strip()


def date_range(start: str | None, end: str | None) -> str:
    s, e = fmt_date(start), fmt_date(end)
    return f"{s} – {e}" if s and e else s or e


def _link_text(url: str) -> str:
    return re.sub(r"^(https?://)?(www\.)?", "", url).rstrip("/")


TEMPLATE = """\
<!doctype html>
<html><head><meta charset="utf-8"><title>{{ r.contact.name }} Resume</title>
<style>
  @page { size: Letter; margin: {{ s.margin }}in; }
  * { box-sizing: border-box; }
  body { font-family: "Helvetica Neue", Helvetica, Arial, "Liberation Sans", sans-serif;
         font-size: {{ s.font }}pt; line-height: {{ s.lh }}; color: #111; margin: 0; }
  h1 { font-size: {{ s.font * 2 }}pt; margin: 0 0 2pt; text-align: center; letter-spacing: .5pt; }
  .contact { text-align: center; font-size: {{ s.font - 0.5 }}pt; color: #333; }
  .contact span + span::before { content: "  |  "; color: #999; }
  a { color: inherit; text-decoration: none; }
  h2 { font-size: {{ s.font + 1 }}pt; text-transform: uppercase; letter-spacing: 1pt; margin: {{ s.gap * 1.6 }}pt 0 {{ s.gap * 0.6 }}pt;
       border-bottom: .75pt solid #444; padding-bottom: 1pt; }
  .item { margin-bottom: {{ s.gap }}pt; break-inside: avoid; }
  .row { display: flex; justify-content: space-between; gap: 8pt; }
  .row .r { white-space: nowrap; text-align: right; }
  .sub { font-style: italic; }
  ul { margin: 1pt 0 0; padding-left: 13pt; }
  li { margin: 0 0 {{ s.gap * 0.35 }}pt; }
  p { margin: 0; }
  .skills p { margin-bottom: 1pt; }
</style></head>
<body>
<h1>{{ r.contact.name }}</h1>
<div class="contact">
  {%- for part in contact %}<span>{{ part }}</span>{% endfor -%}
</div>

{% if r.summary %}<h2>Summary</h2><p>{{ r.summary }}</p>{% endif %}

{% macro education() %}{% if r.education %}
<h2>Education</h2>
{% for e in r.education %}<div class="item">
  <div class="row"><b>{{ e.school }}</b><span class="r">{{ dr(e.start, e.end) }}</span></div>
  {% set deg = [e.degree, e.field] | select | join(", ") %}
  {% if deg or e.gpa %}<div class="row"><span class="sub">{{ deg }}</span><span class="r">{% if e.gpa %}GPA: {{ e.gpa }}{% endif %}</span></div>{% endif %}
  {% if e.details %}<ul>{% for d in e.details %}<li>{{ d }}</li>{% endfor %}</ul>{% endif %}
</div>{% endfor %}
{% endif %}{% endmacro %}

{% if edu_first %}{{ education() }}{% endif %}

{% if r.experience %}<h2>Experience</h2>
{% for x in r.experience %}<div class="item">
  <div class="row"><b>{{ x.title }}</b><span class="r">{{ dr(x.start, x.end) }}</span></div>
  <div class="row"><span class="sub">{{ x.company }}</span><span class="r sub">{{ x.location or "" }}</span></div>
  {% if x.bullets %}<ul>{% for b in x.bullets %}<li>{{ b.text }}</li>{% endfor %}</ul>{% endif %}
</div>{% endfor %}{% endif %}

{% if r.projects %}<h2>Projects</h2>
{% for p in r.projects %}<div class="item">
  <div class="row"><span><b>{{ p.name }}</b>{% if p.description %} — {{ p.description }}{% endif %}</span>
    <span class="r">{% if p.url %}{{ link(p.url) }}{% endif %}</span></div>
  {% if p.bullets %}<ul>{% for b in p.bullets %}<li>{{ b.text }}</li>{% endfor %}</ul>{% endif %}
</div>{% endfor %}{% endif %}

{% if not edu_first %}{{ education() }}{% endif %}

{% if r.skills %}<h2>Skills</h2><div class="skills">
{% for g in r.skills %}<p><b>{{ g.category }}:</b> {{ g.items | join(", ") }}</p>{% endfor %}
</div>{% endif %}

{% if r.certifications %}<h2>Certifications</h2>
{% for c in r.certifications %}<p>{{ c.name }}{% if c.issuer %}, {{ c.issuer }}{% endif %}{% if c.date %} ({{ fd(c.date) }}){% endif %}</p>{% endfor %}
{% endif %}
</body></html>
"""

# Spacing presets tried in order before any content is dropped.
STYLES = [
    {"font": 10.5, "lh": 1.25, "gap": 5, "margin": 0.6},
    {"font": 10, "lh": 1.2, "gap": 4, "margin": 0.5},
    {"font": 9.5, "lh": 1.15, "gap": 3, "margin": 0.45},
]

_env = Environment(autoescape=True, trim_blocks=True, lstrip_blocks=True)
_template = _env.from_string(TEMPLATE)


def to_html(r: MasterResume, education_first: bool = False, style: dict | None = None) -> str:
    c = r.contact
    contact = [x for x in (c.location, c.phone, c.email) if x] + [_link_text(l.url) for l in c.links]
    return _template.render(r=r, s=style or STYLES[0], contact=contact, edu_first=education_first,
                            dr=date_range, fd=fmt_date, link=_link_text)


def _pages(pdf: bytes) -> int:
    return len(PdfReader(io.BytesIO(pdf)).pages)


def render_pdf(t: Tailored, dest: Path, *, max_pages: int = 1) -> Path:
    """Write ``t`` as a PDF at ``dest``, trimming ``t.resume`` in place until it fits."""
    from playwright.sync_api import sync_playwright

    dest.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page()

            def render(style) -> bytes:
                page.set_content(to_html(t.resume, t.education_first, style), wait_until="load")
                return page.pdf(format="Letter", print_background=True, prefer_css_page_size=True)

            pdf = b""
            for style in STYLES:
                pdf = render(style)
                if _pages(pdf) <= max_pages:
                    break
            else:
                for _ in range(60):
                    if _pages(pdf) <= max_pages or not trim_once(t):
                        break
                    pdf = render(STYLES[-1])
        finally:
            browser.close()
    dest.write_bytes(pdf)
    return dest
