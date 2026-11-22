"""Apply to one job: open the form, fill what we know, ask only what's new, you review and submit.

Order of answers for each question:
  1. saved answer (answers.yaml): exact question, same meaning (standard), or a very similar question
  2. your resume (name, email, phone, links, school…)
  3. AI draft for required open-ended questions
  4. ask you in the terminal, then remember it
Optional questions we have no answer for are left blank. Nothing is ever submitted for you.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import questionary
from rich.console import Console
from rich.markup import escape as esc
from rich.table import Table
from rich.text import Text

from .. import config, db
from ..llm import LLMError
from ..models import Application, Job, MasterResume
from . import essay, fill, scan
from .answers import ASK_IF_OPTIONAL, SENSITIVE, AnswerStore, ResumeFacts, describe, match_option, normalize, standard_key, to_iso_date

console = Console()
SKIP = "↷ Skip — I'll answer it in the browser"
_pool = ThreadPoolExecutor(max_workers=1)


def ask(q):
    """questionary can't run inside Playwright's event loop, so prompts run on a worker thread."""
    return _pool.submit(q.ask).result()


SUPPORTED = {"greenhouse", "lever", "ashby"}


def form_url(job: Job) -> str | None:
    if job.source == "greenhouse":
        return f"https://job-boards.greenhouse.io/{job.company_slug}/jobs/{job.external_id}"
    if job.source in ("lever", "ashby"):
        return job.apply_url or job.url
    return None


@dataclass
class Planned:
    field: scan.Field
    value: object = None
    source: str = "skip"  # saved | similar | resume | file | ai | you | page | skip | ask | essay | failed
    key: str | None = None
    note: str = ""
    filled: object = None  # what we actually put in

    @property
    def needs_user(self) -> bool:
        return self.field.required and self.value is None


@dataclass
class Result:
    plans: list[Planned] = field(default_factory=list)
    submitted: bool = False
    url: str = ""


TRANSCRIPT_RE = re.compile(r"transcript|academic record|grade report|record of grades", re.I)


def keep_transcript(src: Path) -> Path:
    """Copy a transcript into the AutoApply home and remember it for future applications."""
    dest = config.keep_document(src, "transcript")
    cfg = config.load_config()
    cfg.transcript_file = str(dest)
    config.save_config(cfg)
    return dest


def saved_transcript(cfg: config.Config | None = None) -> Path | None:
    cfg = cfg or config.load_config()
    if cfg.transcript_file and Path(cfg.transcript_file).is_file():
        return Path(cfg.transcript_file)
    return None


class Applier:
    def __init__(self, job: Job, resume: MasterResume | None, resume_file: Path, *, interactive: bool = True,
                 use_ai: bool = True, store: AnswerStore | None = None, llm=None,
                 transcript_file: Path | None = None, ask_optional: bool = True):
        self.job = job
        self.ask_optional = ask_optional  # also ask about optional questions with no known answer
        self.resume = resume
        self.resume_file = resume_file
        self.transcript_file = transcript_file
        self.interactive = interactive
        self.use_ai = use_ai
        self.store = store or AnswerStore.load()
        self.facts = ResumeFacts.from_resume(resume)
        self.llm = llm
        self.drafts: dict[str, str] = {}  # question -> AI draft, so a re-scan doesn't draft it again
        self.filled_before: dict[str, object] = {}  # question -> what we filled on an earlier scan

    # -- planning ------------------------------------------------------------

    def _fit(self, f: scan.Field, value):
        """Convert a saved/known answer into something this field accepts, or None."""
        if value is None or value == "":
            return None
        if f.kind in ("date", "monthyear"):
            return to_iso_date(value)
        if f.kind in ("select", "combobox", "radio", "yesno"):
            if not f.options:
                return str(value) if f.kind == "combobox" else None
            vals = value if isinstance(value, list) else [value]
            idx = [match_option(str(v), f.options) for v in vals]
            if any(i is None for i in idx):
                return None
            picked = [f.options[i] for i in idx]
            return picked if f.multiple else picked[0]
        if f.kind == "checkboxes":
            vals = value if isinstance(value, list) else [value]
            idx = [match_option(str(v), f.options) for v in vals]
            if any(i is None for i in idx):
                return None
            return [f.options[i] for i in idx]
        if f.kind == "checkbox":
            v = str(value).strip().lower()
            return "Yes" if v in ("yes", "true", "1", "y", "checked") else "No"
        if isinstance(value, list):
            return ", ".join(map(str, value))
        return str(value)

    def plan_field(self, f: scan.Field) -> Planned:
        where = f"{f.label} {f.id or ''} {f.name or ''}".lower()
        if f.kind == "file":
            if TRANSCRIPT_RE.search(where):
                if self.transcript_file:
                    return Planned(f, str(self.transcript_file), "file", key="transcript")
                return Planned(f, None, self._unknown(f), key="transcript",
                               note="no transcript saved yet (`resume transcript <file>`)")
            if re.search(r"resume|\bcv\b", where):
                return Planned(f, str(self.resume_file), "file")
            return Planned(f, None, self._unknown(f), note="file upload")
        note = ""
        hit = self.store.lookup(f.label)
        if hit:
            value, src, key = hit
            fitted = self._fit(f, value)
            if fitted is not None:
                return Planned(f, fitted, src, key=key)
            note = f"saved answer “{value}” isn't an option here"
        key = standard_key(f.label)
        if key and key not in SENSITIVE and key in self.facts.values:
            fitted = self._fit(f, self.facts.values[key])
            if fitted is not None:
                return Planned(f, fitted, "resume", key=key)
        if f.is_essay and not key:
            # AI drafts only required essays; optional ones are yours to answer or skip.
            return Planned(f, None, "essay" if f.required else self._unknown(f), note=note)
        return Planned(f, None, self._unknown(f, key), key=key, note=note)

    def _unknown(self, f: scan.Field, key: str | None = None) -> str:
        """What to do with a question we have no answer for: ask you, or leave it blank."""
        return "ask" if f.required or self.ask_optional or key in ASK_IF_OPTIONAL else "skip"

    def plan(self, fields: list[scan.Field], current: dict | None = None) -> list[Planned]:
        """Plan every field. Fields that already have a value on the page (filled on an earlier
        scan, or by you in the browser) are left alone."""
        plans = []
        for f in fields:
            now = (current or {}).get(f.aa)
            if isinstance(now, list):
                now = [x for x in now if x] or None
            if now and f.kind != "file":
                p = Planned(f, now, "page")
                p.filled = self.filled_before.get(f.label)  # so edits you made in the browser can still be saved
                plans.append(p)
            else:
                plans.append(self.plan_field(f))
        return plans

    # -- asking --------------------------------------------------------------

    def _prompt(self, p: Planned):
        f = p.field
        key = standard_key(f.label)
        hint = f"  [dim](saved as “{describe(key)}” — reused for any wording of this question)[/dim]" if key else ""
        console.print(f"\n[bold]{esc(f.label)}[/bold]{' [red]*[/red]' if f.required else ''}{hint}")
        if p.note:
            console.print(f"[yellow]{esc(p.note)}[/yellow]")
        if f.kind in ("select", "combobox", "radio", "yesno") and f.options:
            if f.multiple:
                ans = ask(questionary.checkbox("Pick all that apply (space to select):", choices=f.options))
                return ans or None
            ans = ask(questionary.select("Answer:", choices=[*f.options, SKIP]))
            return None if ans in (None, SKIP) else ans
        if f.kind == "checkboxes":
            ans = ask(questionary.checkbox("Pick all that apply (space to select):", choices=f.options))
            return ans or None
        if f.kind == "checkbox":
            ans = ask(questionary.select("Check this box?", choices=["Yes", "No", SKIP]))
            return None if ans in (None, SKIP) else ans
        if f.kind == "file":
            ans = ask(questionary.path("File to upload (blank to skip):"))
            return ans.strip() if ans and ans.strip() else None
        default = self.facts.values.get(key or "", "")
        ans = ask(questionary.text("Answer (blank to skip):", default=default, multiline=f.kind == "textarea"))
        return ans.strip() if ans and ans.strip() else None

    def ask_unknowns(self, plans: list[Planned]) -> None:
        todo = [p for p in plans if p.source == "ask"]
        if not todo or not self.interactive:
            return
        console.print(f"\n[bold cyan]{len(todo)} question{'s' if len(todo) != 1 else ''} I haven't seen before.[/bold cyan] "
                      "Answer once and they're saved for every future application.")
        for p in todo:
            value = self._prompt(p)
            if value is None:
                if not p.field.required:
                    p.source = "skip"  # you chose to leave it blank
                continue
            p.value, p.source = value, "you"
            if p.field.kind in ("date", "monthyear"):
                p.value = to_iso_date(value) or value
            if p.field.kind == "file":
                if p.key == "transcript" and Path(value).expanduser().is_file():
                    self.transcript_file = keep_transcript(Path(value))
                    p.value = str(self.transcript_file)
                    console.print(f"[dim]Saved as your transcript; it'll be uploaded automatically from now on.[/dim]")
            else:
                p.key = self.store.remember(p.field.label, value)
        self.store.save()

    def draft_essays(self, plans: list[Planned]) -> None:
        todo = [p for p in plans if p.source == "essay"]
        if not todo:
            return
        if not (self.use_ai and self.resume):
            for p in todo:
                p.source = "ask"
            return
        for p in todo:
            if p.field.label in self.drafts:
                p.value, p.source = self.drafts[p.field.label], "ai"
                continue
            try:
                with console.status(f"Drafting an answer: {p.field.label[:60]}…"):
                    p.value = essay.draft(p.field.label, self.job, self.resume, self.llm)
                p.source = "ai"
                self.drafts[p.field.label] = p.value
            except LLMError as e:
                p.source, p.note = "ask", f"AI draft failed: {e}"

    # -- filling ---------------------------------------------------------------

    def fill_all(self, page, plans: list[Planned]) -> None:
        # Uploads first: some forms parse the resume and overwrite fields after an upload,
        # so wait for that before filling everything else.
        files = [p for p in plans if p.field.kind == "file"]
        rest = [p for p in plans if p.field.kind != "file"]
        for group in (files, rest):
            for p in group:
                if p.value is None:
                    continue
                if p.source == "page":
                    continue
                try:
                    fill.fill(page, p.field, p.value)
                    p.filled = p.value
                    self.filled_before[p.field.label] = p.value
                except fill.FillError as e:
                    p.note = f"couldn't fill: {e}"
                    p.source = "failed"
            if group is files and any(p.filled for p in files):
                fill.wait_until_idle(page)
        self._refill_cleared(page, plans)
        fill.highlight(page, [p.field for p in plans if p.needs_user or p.source == "failed"])

    def _refill_cleared(self, page, plans: list[Planned]) -> None:
        """Some boards finish reading the uploaded resume late and blank out fields we already filled
        (seen on Ashby with School). Read the form back and fill anything that came back empty."""
        page.wait_for_timeout(1000)
        for attempt in range(2):
            try:
                now = scan.read_values(page)
            except Exception:
                return
            def empty(v) -> bool:
                return not ([x for x in v if x] if isinstance(v, list) else v)

            cleared = [p for p in plans if p.filled is not None and p.source != "page"
                       and p.field.kind != "file" and empty(now.get(p.field.aa))]
            if not cleared:
                return
            for p in cleared:
                try:
                    fill.fill(page, p.field, p.value)
                except fill.FillError as e:
                    p.note, p.source = f"couldn't fill: {e}", "failed"
            page.wait_for_timeout(800)

    # -- reporting / learning --------------------------------------------------

    SOURCE_STYLE = {
        "saved": ("saved", "green"), "similar": ("similar question — check", "yellow"),
        "resume": ("resume", "green"), "file": ("resume file", "green"), "ai": ("AI draft — review", "cyan"),
        "you": ("you (saved)", "green"), "page": ("already on the form", "dim"), "skip": ("left blank", "dim"), "ask": ("YOU, in the browser", "bold yellow"),
        "essay": ("YOU, in the browser", "bold yellow"), "failed": ("YOU, in the browser", "bold red"),
    }

    def summary(self, plans: list[Planned]) -> Table:
        t = Table(title=f"{self.job.title} @ {self.job.company}", expand=True, show_lines=False)
        t.add_column("Question", ratio=3)
        t.add_column("Answer", ratio=3)
        t.add_column("From", no_wrap=True)
        for p in plans:
            if p.source == "skip" and not p.field.required:
                continue
            label, style = self.SOURCE_STYLE.get(p.source, (p.source, ""))
            if p.source == "file" and p.key == "transcript":
                label = "saved transcript"
            if p.source in ("ask", "essay") and not p.field.required and not self.ask_optional \
                    and p.key not in ASK_IF_OPTIONAL:
                continue
            val = p.value
            if p.field.kind == "file" and val:
                val = Path(str(val)).name
            if isinstance(val, list):
                val = ", ".join(val)
            text = "" if val is None else str(val)
            text = text if len(text) <= 120 else text[:117] + "…"
            q = Text(p.field.label[:110] + ("…" if len(p.field.label) > 110 else ""))
            if p.field.required:
                q.append(" *", style="red")
            src = Text(label, style=style)
            if p.note and p.source == "failed":
                src.append(f"\n{p.note[:60]}", style="dim")
            t.add_row(q, Text(text), src)
        return t

    def learn_from_browser(self, plans: list[Planned], final: dict) -> None:
        """Offer to save answers the user typed or changed in the browser."""
        changes: list[tuple[Planned, object]] = []
        for p in plans:
            f = p.field
            if f.kind in ("file", "textarea") or p.source == "ai":
                continue
            now = final.get(f.aa)
            if isinstance(now, list):
                now = [x for x in now if x]
                if not now:
                    continue
                now_val = now if f.kind == "checkboxes" or f.multiple else now[0]
            else:
                if not now:
                    continue
                now_val = now
            before = p.filled
            if f.kind in ("date", "monthyear") and to_iso_date(now_val) == before:
                continue  # same date, just shown in the site's format
            b = normalize(", ".join(before)) if isinstance(before, list) else normalize(str(before or ""))
            n = normalize(", ".join(now_val)) if isinstance(now_val, list) else normalize(str(now_val))
            # a site expanding what we typed ("Austin, TX" -> "Austin, TX, USA") isn't the user's edit
            if b and (n == b or n.startswith(b)):
                continue
            if b != n:
                changes.append((p, now_val))
        if not changes or not self.interactive:
            return
        choices = [questionary.Choice(f"{p.field.label[:70]} → {', '.join(v) if isinstance(v, list) else v}"[:110],
                                      value=i, checked=True) for i, (p, v) in enumerate(changes)]
        picked = ask(questionary.checkbox(
            f"You answered or changed {len(changes)} question(s) in the browser. Save them for next time?",
            choices=choices))
        for i in picked or []:
            p, v = changes[i]
            self.store.remember(p.field.label, v)
        if picked:
            self.store.save()
            console.print(f"[green]Saved {len(picked)} answer(s).[/green]")


# ---------------------------------------------------------------------------
# browser session
# ---------------------------------------------------------------------------

CAPTURE_JS = r"""
() => {
  if (window.__aaHooked) return;
  window.__aaHooked = true;
  const send = () => { try { window.__aaSnapshot(window.__aaRead()); } catch (e) {} };
  document.addEventListener('submit', send, true);
  document.addEventListener('click', (e) => {
    const b = e.target.closest('button, input[type=submit]');
    if (b && /submit|apply|send/i.test(b.innerText || b.value || '')) send();
  }, true);
}
"""


def _install_capture(page, captured: dict) -> None:
    page.expose_function("__aaSnapshot", lambda data: captured.update(data or {}))
    page.evaluate(f"() => {{ window.__aaRead = {scan.READ_JS}; }}")
    page.evaluate(CAPTURE_JS)


def run(job: Job, resume: MasterResume | None, resume_file: Path, *, headless: bool = False,
        dry_run: bool = False, use_ai: bool = True, screenshot: Path | None = None,
        transcript_file: Path | None = None, ask_optional: bool = True) -> Result:
    from playwright.sync_api import sync_playwright

    url = form_url(job)
    if url is None:
        raise RuntimeError(f"Auto-fill supports Greenhouse, Lever and Ashby; this job is from {job.source}.")
    app = Applier(job, resume, resume_file, interactive=not dry_run, use_ai=use_ai,
                  transcript_file=transcript_file, ask_optional=ask_optional)
    result = Result(url=url)
    profile_dir = config.home() / "browser"
    profile_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(profile_dir), headless=headless or dry_run, viewport=None if not (headless or dry_run) else {"width": 1280, "height": 900},
            args=["--start-maximized"] if not (headless or dry_run) else [],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            with console.status("Opening the application form…"):
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except Exception:
                    pass
                page.wait_for_timeout(1000)
            captured: dict = {}
            while True:
                with console.status("Reading the form…"):
                    fields = scan.scan(page)
                    if job.source == "greenhouse":
                        scan.enrich_greenhouse(fields, scan.greenhouse_questions(job.company_slug, job.external_id))
                    for f in fields:
                        if f.kind == "combobox" and not f.options:
                            scan.load_combobox_options(page, f)
                if not fields:
                    console.print("[yellow]No form fields found on this page. If there's an Apply button, click it, "
                                  "then choose “Re-scan”.[/yellow]")
                plans = app.plan(fields, scan.read_values(page))
                app.draft_essays(plans)
                app.ask_unknowns(plans)
                with console.status("Filling in the form…"):
                    app.fill_all(page, plans)
                result.plans = plans
                console.print(app.summary(plans))

                if dry_run:
                    if screenshot:
                        page.screenshot(path=str(screenshot), full_page=True)
                    return result

                todo = sum(1 for p in plans if p.needs_user or p.source == "failed")
                console.print(
                    ("[bold yellow]" + f"{todo} field(s) outlined in orange need you. " + "[/bold yellow]" if todo else "")
                    + "Review everything in the browser, solve the CAPTCHA if one appears, and click "
                    "[bold]Submit[/bold] yourself. Then come back here."
                )
                try:
                    _install_capture(page, captured)
                except Exception:
                    pass
                choice = ask(questionary.select(
                    "When you're done:",
                    choices=[
                        questionary.Choice("✓ I submitted the application", value="done"),
                        questionary.Choice("↻ Re-scan the page and fill again (next page / new questions)", value="rescan"),
                        questionary.Choice("✗ I didn't submit — close", value="quit"),
                    ],
                ))
                if choice == "rescan":
                    continue
                final = captured or (scan.read_values(page) if not page.is_closed() else {})
                app.learn_from_browser(plans, final)
                result.submitted = choice == "done"
                return result
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def record(job: Job, result: Result, resume_file: Path) -> None:
    if not result.submitted:
        return
    now = datetime.now(timezone.utc)
    with db.session() as s:
        j = s.get(Job, job.id)
        if j:
            j.status = "applied"
            s.add(j)
        s.add(Application(job_id=job.id, status="applied", resume_path=str(resume_file),
                          notes=f"Applied via AutoApply at {result.url}", created_at=now, updated_at=now))
        s.commit()
