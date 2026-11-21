"""`apply` and `answers` commands."""

from __future__ import annotations

from pathlib import Path

import questionary
import typer
from rich.console import Console
from rich.markup import escape as esc
from rich.table import Table
from rich.text import Text

from . import config, db
from .apply import runner
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


def apply(
    job_id: int = typer.Argument(..., help="Job id from `jobs` (or `use <id>` first in the console)"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Fill the form invisibly and show what would be entered; ask nothing, submit nothing."),
    no_ai: bool = typer.Option(False, "--no-ai", help="Don't draft open-ended answers with AI."),
    headless: bool = typer.Option(False, "--headless", hidden=True, help="Run the browser invisibly (testing)."),
) -> None:
    """Open a job's application, fill it from your resume and saved answers, and let you submit it."""
    with db.session() as s:
        job = s.get(Job, job_id)
    if job is None:
        _fail(f"No job with id {job_id}.")
    if job.source not in runner.SUPPORTED:
        console.print(f"[yellow]{esc(job.company)}'s posting is on an external site AutoApply can't fill yet. "
                      f"Opening it so you can apply there.[/yellow]")
        import webbrowser

        webbrowser.open(job.apply_url or job.url)
        return
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
    shot = config.output_dir() / f"dry-run-{job.id}.png" if dry_run else None
    if shot:
        shot.parent.mkdir(parents=True, exist_ok=True)
    console.print(f"[bold]Applying:[/bold] {esc(job.title)} @ {esc(job.company)}")
    try:
        result = runner.run(job, resume, resume_file, dry_run=dry_run, headless=headless,
                            use_ai=cfg.ai_essays and not no_ai, screenshot=shot)
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
    app.add_typer(answers_app, name="answers")
