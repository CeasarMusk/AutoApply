"""`apply`, `tailor` and `answers` commands."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import questionary
import typer
from rich.console import Console
from rich.markup import escape as esc
from rich.table import Table
from rich.text import Text

from . import config, db
from .apply import runner, sites
from .apply.answers import STANDARD, AnswerStore, ResumeFacts, describe
from .models import Job
from .resume import store as resume_store

console = Console()
answers_app = typer.Typer(help="Your saved application answers (asked once, reused everywhere).", no_args_is_help=True)


def _fail(msg: str) -> None:
    console.print(f"[red]{esc(msg)}[/red]")
    raise typer.Exit(1)


def _resume_file(cfg: config.Config) -> Path:
    if cfg.resume_file and Path(cfg.resume_file).exists():
        return Path(cfg.resume_file)
    for suffix in (".pdf", ".docx"):
        p = config.home() / f"resume{suffix}"
        if p.exists():
            return p
    path = questionary.path("Which resume file should applications upload? (PDF recommended)").ask()
    if not path or not Path(path).expanduser().exists():
        _fail("A resume file is needed to apply. Run `resume import <your.pdf>`.")
    cfg.resume_file = str(Path(path).expanduser().resolve())
    config.save_config(cfg)
    return Path(cfg.resume_file)


# ---------------------------------------------------------------------------
# tailoring
# ---------------------------------------------------------------------------


def _tailor_paths(job: Job, master) -> tuple[Path, Path]:
    folder = config.output_dir() / f"{job.id}-{re.sub(r'[^a-z0-9]+', '-', job.company.lower()).strip('-')}"
    name = re.sub(r"[^A-Za-z0-9]+", "_", master.contact.name or "").strip("_")
    return folder / f"{name + '_' if name else ''}Resume.pdf", folder / "tailored.yaml"


def _show_tailored(t, master, fresh: bool) -> None:
    from rich.panel import Panel

    r = t.resume
    kept_b = sum(len(x.bullets) for x in r.experience)
    all_b = sum(len(x.bullets) for x in master.experience)
    lines = [f"Experience: {len(r.experience)}/{len(master.experience)} roles, {kept_b}/{all_b} bullets"]
    if master.projects:
        lines.append(f"Projects: {len(r.projects)}/{len(master.projects)}")
    if r.skills:
        lines.append("Skills first: " + ", ".join(r.skills[0].items[:6]))
    if t.keywords:
        lines.append("Matches: " + ", ".join(t.keywords[:10]))
    if t.notes:
        lines.append(f"[dim]{esc(t.notes)}[/dim]")
    if fresh:
        if t.summary_changed:
            lines.append("[cyan]Summary adapted to this job.[/cyan]")
        if t.reworded:
            lines.append(f"[cyan]{len(t.reworded)} bullet(s) reworded[/cyan] (same facts, job's wording):")
            for old, new in t.reworded[:6]:
                lines.append(f"  [dim]- {esc(old)}[/dim]\n  [green]+ {esc(new)}[/green]")
        if t.rejected:
            lines.append(f"[yellow]{len(t.rejected)} rewrite(s) kept in your original wording[/yellow] "
                         f"(they'd have changed facts: {esc(t.rejected[0][2])}{'…' if len(t.rejected) > 1 else ''})")
        if t.dropped_for_space:
            lines.append(f"[dim]Dropped {t.dropped_for_space} lower-priority line(s) to fit one page.[/dim]")
    console.print(Panel("\n".join(lines), title="Tailored resume", title_align="left", expand=True))


def make_tailored(job: Job, master, *, redo: bool = False) -> Path | None:
    """Tailor (or reuse the earlier tailoring for) this job. Returns the PDF, or None if it failed."""
    from .llm import LLMError
    from .resume import render, tailor

    pdf, meta = _tailor_paths(job, master)
    if pdf.exists() and meta.exists() and not redo:
        try:
            t = tailor.Tailored.from_yaml(meta.read_text())
            when = datetime.fromtimestamp(pdf.stat().st_mtime).strftime("%b %d")
            console.print(f"[dim]Using the resume tailored for this job on {when}.[/dim]")
            _show_tailored(t, master, fresh=False)
            return pdf
        except Exception:
            pass
    try:
        with console.status(f"Tailoring your resume for {esc(job.company)} with Claude…"):
            t = tailor.tailor(master, job)
        with console.status("Laying it out on one page…"):
            render.render_pdf(t, pdf)
    except LLMError as e:
        console.print(f"[yellow]Couldn't tailor the resume ({esc(str(e))}).[/yellow]")
        return None
    except Exception as e:
        msg = str(e).splitlines()[0] if str(e) else type(e).__name__
        if "Executable doesn't exist" in msg:
            msg = "the browser isn't installed; run: uv run playwright install chromium"
        console.print(f"[yellow]Couldn't make the tailored PDF ({esc(msg)}).[/yellow]")
        return None
    meta.write_text(t.to_yaml())
    _show_tailored(t, master, fresh=True)
    return pdf


def _preview(pdf: Path) -> None:
    console.print(f"[dim]Opening {esc(str(pdf))}[/dim]")
    typer.launch(str(pdf))


def choose_resume(job: Job, master, original: Path, *, interactive: bool) -> Path:
    """Tailor for this job and let the user use it, preview it, redo it, or fall back to the original."""
    pdf = make_tailored(job, master)
    if pdf is None:
        console.print(f"[dim]Uploading your original resume instead: {esc(original.name)}[/dim]")
        return original
    while interactive:
        choice = runner.ask(questionary.select(
            "Which resume should this application upload?",
            choices=[
                questionary.Choice("Tailored resume", value="use"),
                questionary.Choice("Preview the tailored resume first", value="preview"),
                questionary.Choice("Tailor it again", value="redo"),
                questionary.Choice(f"My original resume ({original.name})", value="original"),
            ],
        ))
        if choice == "preview":
            _preview(pdf)
        elif choice == "redo":
            pdf = make_tailored(job, master, redo=True) or pdf
        elif choice == "original":
            return original
        else:
            break
    return pdf


def tailor_cmd(
    job_id: int = typer.Argument(..., help="Job id from `jobs` (or `use <id>` first in the console)"),
    preview: bool = typer.Option(False, "--preview", "-p", help="Open the PDF when it's ready."),
    redo: bool = typer.Option(False, "--redo", help="Tailor again even if this job already has one."),
) -> None:
    """Make a one-page resume tailored to a job (without applying). `apply` does this automatically."""
    with db.session() as s:
        job = s.get(Job, job_id)
    if job is None:
        _fail(f"No job with id {job_id}.")
    try:
        master = resume_store.load()
    except FileNotFoundError:
        _fail("No master resume yet. Run `resume import <file>` first.")
    pdf = make_tailored(job, master, redo=redo)
    if pdf is None:
        raise typer.Exit(1)
    console.print(f"[green]Saved[/green] {esc(str(pdf))}")
    if preview:
        _preview(pdf)


def apply(
    job_id: int = typer.Argument(..., help="Job id from `jobs` (or `use <id>` first in the console)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Fill the form invisibly and show what would be entered; ask nothing, submit nothing."),
    no_ai: bool = typer.Option(False, "--no-ai", help="Don't draft open-ended answers with AI."),
    no_tailor: bool = typer.Option(False, "--no-tailor", help="Upload your original resume instead of a tailored one."),
    headless: bool = typer.Option(False, "--headless", hidden=True, help="Run the browser invisibly (testing)."),
) -> None:
    """Open a job's application, fill it from your resume and saved answers, and let you submit it."""
    with db.session() as s:
        job = s.get(Job, job_id)
    if job is None:
        _fail(f"No job with id {job_id}.")
    site = sites.for_job(job)
    if not site.full_support:
        console.print(f"[yellow]{esc(job.company)}'s application is on {esc(site.label)}. AutoApply fills what it "
                      f"recognizes there (best effort); finish the rest in the browser.[/yellow]")
    if job.status == "applied" and not dry_run and not questionary.confirm(
            "You already applied to this one. Open it anyway?", default=False).ask():
        return
    cfg = config.load_config()
    try:
        resume = resume_store.load()
    except FileNotFoundError:
        resume = None
        console.print("[yellow]No master resume, so contact details can't be filled automatically.[/yellow]")
    resume_file = _resume_file(cfg)
    if resume and cfg.tailor_resume and not no_tailor:
        resume_file = choose_resume(job, resume, resume_file, interactive=not dry_run)
    shot = config.output_dir() / f"dry-run-{job.id}.png" if dry_run else None
    if shot:
        shot.parent.mkdir(parents=True, exist_ok=True)
    console.print(f"[bold]Applying:[/bold] {esc(job.title)} @ {esc(job.company)}")
    try:
        result = runner.run(job, resume, resume_file, dry_run=dry_run, headless=headless,
                            use_ai=cfg.ai_essays and not no_ai, screenshot=shot,
                            transcript_file=runner.saved_transcript(cfg), ask_optional=cfg.ask_optional)
    except Exception as e:
        msg = str(e).splitlines()[0]
        if "Executable doesn't exist" in msg or "playwright install" in msg:
            _fail("The browser isn't installed yet. Run: uv run playwright install chromium")
        _fail(f"Couldn't run the application: {msg}")
    if dry_run:
        console.print(f"[dim]Dry run: nothing was submitted. Screenshot: {shot}[/dim]")
        return
    runner.record(job, result, resume_file)
    if result.submitted:
        console.print(f"[green]✓ Marked as applied.[/green] {esc(job.title)} @ {esc(job.company)}")
    else:
        console.print("Not marked as applied.")


# ---------------------------------------------------------------------------
# answers
# ---------------------------------------------------------------------------


@answers_app.command("list")
def answers_list() -> None:
    """Everything AutoApply will answer for you."""
    st = AnswerStore.load()
    try:
        facts = ResumeFacts.from_resume(resume_store.load()).values
    except FileNotFoundError:
        facts = {}
    t = Table(title="Standard answers (any wording of these questions)", expand=True)
    t.add_column("Key", style="cyan", no_wrap=True)
    t.add_column("Question")
    t.add_column("Answer")
    for key, desc, _ in STANDARD:
        if key in st.standard:
            v = st.standard[key]
            t.add_row(key, desc, Text(", ".join(v) if isinstance(v, list) else str(v)))
        elif key in facts:
            t.add_row(key, desc, Text(f"{facts[key]}  (from resume)", style="dim"))
    console.print(t)
    if st.learned:
        t2 = Table(title="Learned answers (this exact question)", expand=True)
        t2.add_column("#", style="cyan", justify="right")
        t2.add_column("Question", ratio=3)
        t2.add_column("Answer", ratio=2)
        for i, (k, v) in enumerate(st.learned.items(), 1):
            a = v.get("answer")
            t2.add_row(str(i), Text(v.get("question", k)), Text(", ".join(a) if isinstance(a, list) else str(a)))
        console.print(t2)
    console.print(f"[dim]File: {config.answers_path()} — `answers set <key> <value>`, `answers forget <key or #>`.[/dim]")


@answers_app.command("set")
def answers_set(key: str = typer.Argument(..., help="A standard key, e.g. how_heard, salary, authorized"),
                value: list[str] = typer.Argument(..., help="The answer")) -> None:
    """Set a standard answer, e.g. `answers set how_heard LinkedIn`."""
    keys = [k for k, _, _ in STANDARD]
    if key not in keys:
        _fail(f"Unknown key. Standard keys: {', '.join(keys)}")
    st = AnswerStore.load()
    st.standard[key] = " ".join(value)
    st.save()
    console.print(f"{describe(key)} → {esc(' '.join(value))}")


@answers_app.command("forget")
def answers_forget(target: str = typer.Argument(..., help="Standard key or learned answer number")) -> None:
    """Delete a saved answer so you're asked again next time."""
    st = AnswerStore.load()
    key = target
    if target.isdigit():
        items = list(st.learned)
        i = int(target) - 1
        if not 0 <= i < len(items):
            _fail("No learned answer with that number (see `answers list`).")
        key = items[i]
    if st.forget(key):
        st.save()
        console.print(f"Forgot {esc(target)}.")
    else:
        _fail(f"No saved answer '{target}'.")


@answers_app.command("setup")
def answers_setup() -> None:
    """Answer the common questions now (work authorization, sponsorship, EEO…) so applications go faster."""
    st = AnswerStore.load()
    common = [
        ("authorized", ["Yes", "No"]),
        ("sponsorship", ["Yes", "No"]),
        ("authorized_no_sponsorship", ["Yes", "No"]),
        ("age_18", ["Yes", "No"]),
        ("how_heard", None),
        ("country", None),
        ("start_date", None),
        ("relocate", ["Yes", "No"]),
        ("salary", None),
        ("gender", ["Male", "Female", "Non-binary", "Decline to self-identify"]),
        ("hispanic", ["Yes", "No", "Decline to self-identify"]),
        ("race", ["American Indian or Alaska Native", "Asian", "Black or African American",
                  "Native Hawaiian or Other Pacific Islander", "White", "Two or more races", "Decline to self-identify"]),
        ("veteran", ["I am not a protected veteran", "I identify as one or more of the classifications of protected veteran",
                     "Decline to self-identify"]),
        ("disability", ["No, I don't have a disability", "Yes, I have a disability", "Decline to self-identify"]),
    ]
    console.print("Answer once; leave blank / pick Skip to be asked when a form needs it.")
    for key, options in common:
        current = st.standard.get(key)
        label = describe(key) + (f"  [now: {current}]" if current else "")
        if options:
            ans = questionary.select(label, choices=[*options, "Skip"], default=current if current in options else None).ask()
            if ans is None:
                break
            if ans != "Skip":
                st.standard[key] = ans
        else:
            ans = questionary.text(label + " (blank to skip)", default=str(current or "")).ask()
            if ans is None:
                break
            if ans.strip():
                st.standard[key] = ans.strip()
    st.save()
    console.print(f"[green]Saved.[/green] {config.answers_path()}")


def register(app: typer.Typer) -> None:
    app.command("apply")(apply)
    app.command("tailor")(tailor_cmd)
    app.add_typer(answers_app, name="answers")
