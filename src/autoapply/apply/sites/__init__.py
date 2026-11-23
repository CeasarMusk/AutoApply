"""Site adapters: what's different about each application site.

The scanner and filler are site-agnostic. An adapter only knows where the form lives, how to get to
it (cookie banners, "Apply" buttons), and any site-specific extras (Greenhouse's question API).
Sites without an adapter use Generic: best effort, you finish what it can't.

Adapters never click Submit, Create Account, or anything that sends the application.
"""

from __future__ import annotations

import re

from ...models import Job
from .. import scan

STEP_JS = r"""
() => {
  const vis = el => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const heads = [...document.querySelectorAll('h1, h2, h3, [role=heading], [data-automation-id=progressBar] [aria-current]')]
    .filter(vis).slice(0, 8).map(h => (h.innerText || '').trim().slice(0, 80));
  return location.href.split('#')[0] + '|' + heads.join('|');
}
"""

COOKIE_RE = re.compile(r"^\s*(accept( all)?( cookies)?|allow( all)?( cookies)?|i accept|i agree|agree( and close)?|got it)\s*$", re.I)
APPLY_RE = re.compile(r"^\s*(apply|start( my| your)? application|i'm interested)"
                      r"(\s+(now|online|manually|here|today|for this (job|position|role|opening)|to this (job|position|role)))*\s*$",
                      re.I)  # not "Apply with LinkedIn/Indeed": those sign you in elsewhere


def _settle(page, idle_ms: int = 8_000) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15_000)
        page.wait_for_load_state("networkidle", timeout=idle_ms)
    except Exception:
        pass
    page.wait_for_timeout(500)


class Site:
    """Default adapter: best effort on any site."""

    name = "generic"
    label = "company site"
    full_support = False  # True: the filler is tuned and tested for this site

    def form_url(self, job: Job) -> str:
        return job.apply_url or job.url

    def enrich(self, job: Job, fields: list[scan.Field]) -> None:
        """Add details the page doesn't show (e.g. Greenhouse's question API)."""

    def step_signature(self, page) -> str:
        """Changes when the form moves to another step/page. Filling fields must not change it."""
        try:
            return page.evaluate(STEP_JS)
        except Exception:
            return page.url

    def prepare(self, ctx, page):
        """Get from the posting to the form. Returns the page (tab) holding it."""
        dismiss_cookies(page)
        for _ in range(3):  # e.g. "Apply" → "Apply Manually"
            if len(scan.scan(page)) >= 2:
                return page
            nxt = click_apply(ctx, page)
            if nxt is None:
                return page
            page = nxt
            dismiss_cookies(page)
        return page


CHALLENGE_RE = re.compile(r"verify(ing)? you are (a )?human|performing security verification|just a moment|"
                          r"checking your browser|are you a robot", re.I)


def is_challenge(page) -> bool:
    """A bot check (Cloudflare etc.) is in the way. You solve these; AutoApply never tries to."""
    try:
        return bool(CHALLENGE_RE.search(page.title() + " " + page.inner_text("body")[:3000]))
    except Exception:
        return False


def dismiss_cookies(page) -> bool:
    try:
        if not re.search(r"cookie|consent|privacy", page.content()[:400_000], re.I):
            return False
        btn = page.get_by_role("button", name=COOKIE_RE)
        for i in range(min(btn.count(), 4)):
            if btn.nth(i).is_visible():
                btn.nth(i).click(timeout=3_000)
                page.wait_for_timeout(400)
                return True
    except Exception:
        pass
    return False


def click_apply(ctx, page):
    """Click a visible "Apply" link/button that leads to the form (never a form's submit button).
    Returns the page with the form (a new tab if one opened), or None if there was nothing to click."""
    for role in ("button", "link"):
        try:
            loc = page.get_by_role(role, name=APPLY_RE)
            n = loc.count()
        except Exception:
            continue
        for i in range(min(n, 6)):
            el = loc.nth(i)
            try:
                if not el.is_visible() or el.evaluate("e => (e.type === 'submit' && !!e.form) || !!e.closest('form')"):
                    continue
                tabs = len(ctx.pages)
                el.click(timeout=5_000)
            except Exception:
                continue
            page.wait_for_timeout(1_500)
            target = ctx.pages[-1] if len(ctx.pages) > tabs else page
            _settle(target)
            return target
    return None


class Greenhouse(Site):
    name, label, full_support = "greenhouse", "Greenhouse", True

    def form_url(self, job: Job) -> str:
        # Regional boards (job-boards.eu.greenhouse.io) and embed links keep their own URL.
        if job.apply_url and re.search(r"\.(eu|anz)\.greenhouse\.io/|/embed/job_app", job.apply_url):
            return job.apply_url
        return f"https://job-boards.greenhouse.io/{job.company_slug}/jobs/{job.external_id}"

    def enrich(self, job: Job, fields: list[scan.Field]) -> None:
        scan.enrich_greenhouse(fields, scan.greenhouse_questions(job.company_slug, job.external_id))

    def prepare(self, ctx, page):
        dismiss_cookies(page)
        return page


class Lever(Site):
    name, label, full_support = "lever", "Lever", True

    def prepare(self, ctx, page):
        return page


class Ashby(Site):
    name, label, full_support = "ashby", "Ashby", True

    def prepare(self, ctx, page):
        return page


ADAPTERS: dict[str, Site] = {s.name: s for s in (Greenhouse(), Lever(), Ashby())}
LABELS = {"workday": "Workday", "icims": "iCIMS", "smartrecruiters": "SmartRecruiters", "workable": "Workable",
          "jobvite": "Jobvite", "remoteok": "Remote OK", "web": "company site"}


def for_job(job: Job) -> Site:
    if job.source in ADAPTERS:
        return ADAPTERS[job.source]
    from ...sources.github_lists import site_for

    site = site_for(job.apply_url or job.url)
    if site in ADAPTERS:
        return ADAPTERS[site]
    generic = Site()
    generic.label = LABELS.get(site if site != "web" else job.source, "company site")
    return generic


def describe(source: str) -> tuple[str, bool]:
    """(site name, fully supported?) for a job source."""
    if source in ADAPTERS:
        return ADAPTERS[source].label, True
    return LABELS.get(source, source), False
