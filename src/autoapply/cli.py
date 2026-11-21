from __future__ import annotations

import shutil
from pathlib import Path

import questionary
import typer
from rich.console import Console
from rich.markup import escape as esc
from rich.table import Table
from sqlmodel import func, select

from . import config, db
from .llm import LLM, LLMError
from .llm.client import ClaudeCodeBackend
from .models import LLMUsage, MasterResume
from .resume import builder, review, store, validate
from .resume.importer import SUPPORTED, ImportError_, import_resume

app = typer.Typer(
    help="AutoApply: tailor and send job applications from one master resume. "
    "Run with no command to open the interactive console.",
    invoke_without_command=True,
)
resume_app = typer.Typer(help="Create, import, and edit your master resume.", no_args_is_help=True)
app.add_typer(resume_app, name="resume")


@app.callback()
def _root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        from .shell import run_shell

        run_shell(app)

console = Console()


def _fail(msg: str) -> None:
    console.print(f"[red]{esc(msg)}[/red]")
    raise typer.Exit(1)


def _load_master() -> MasterResume:
    try:
        return store.load()
    except FileNotFoundError:
        _fail("No master resume yet. Run `autoapply resume import <file>` or `autoapply resume build`.")
    raise AssertionError  # unreachable


def _save_master(r: MasterResume) -> None:
    path = config.master_resume_path()
    if path.exists():
        shutil.copy2(path, path.with_suffix(".yaml.bak"))
    store.save(r)
    issues = validate.validate(r)
    console.print(f"[green]Saved[/green] {path}")
    errors = [i for i in issues if i.level == "error"]
    if errors:
        console.print(f"[yellow]{len(errors)} problem(s) remain — run `autoapply resume validate`.[/yellow]")


def _edit_then_save(r: MasterResume, llm: LLM | None, section: str | None = None) -> None:
    try:
        should_save = builder.edit(r, llm, section=section)
    except KeyboardInterrupt:
        console.print()
        should_save = questionary.confirm("Interrupted. Save what you have?", default=True).ask()
    if should_save:
        _save_master(r)
    else:
        console.print("Nothing saved.")


def _confirm_overwrite(yes: bool = False) -> None:
    if store.exists() and not yes and not questionary.confirm(
        f"{config.master_resume_path()} already exists. Replace it? (a .bak copy is kept)",
        default=False,
    ).ask():
        raise typer.Exit(0)


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


@app.command()
def init() -> None:
    """Set up ~/.autoapply and get your master resume in."""
    home = config.home()
    home.mkdir(parents=True, exist_ok=True)
    if not config.config_path().exists():
        cfg = config.Config()
        has_cc = ClaudeCodeBackend.available()
        cfg.llm_backend = questionary.select(
            "Which Claude should AutoApply use?",
            choices=[
                questionary.Choice(
                    "My Claude subscription, via Claude Code (no API cost)"
                    + ("" if has_cc else "  [Claude Code not found]"),
                    value="claude-code",
                ),
                questionary.Choice("Anthropic API key (pay per token)", value="api"),
            ],
            default="claude-code" if has_cc else "api",
        ).ask() or cfg.llm_backend
        config.save_config(cfg)
        console.print(f"Created {config.config_path()}")
        if cfg.llm_backend == "claude-code" and not has_cc:
            console.print("[yellow]Install Claude Code and run `claude` once to log in before using AI features.[/yellow]")
    if not config.answers_path().exists():
        config.answers_path().write_text(config.DEFAULT_ANSWERS)
        console.print(f"Created {config.answers_path()} [dim](fill this in before applying)[/dim]")
    db.engine()

    if store.exists():
        console.print(f"Master resume found at {config.master_resume_path()}.")
        return

    choice = questionary.select(
        "How do you want to create your master resume?",
        choices=[
            questionary.Choice("Import my existing resume (PDF, DOCX, TXT)", value="import"),
            questionary.Choice("Build one step by step", value="build"),
            questionary.Choice("Skip for now", value="skip"),
        ],
    ).ask()
    if choice == "import":
        path = questionary.path("Path to your resume file:").ask()
        if path:
            resume_import(Path(path))
    elif choice == "build":
        resume_build()


# ---------------------------------------------------------------------------
# resume commands
# ---------------------------------------------------------------------------


@resume_app.command("import")
def resume_import(
    file: Path = typer.Argument(..., help=f"Resume file ({', '.join(sorted(SUPPORTED))})"),
    show_source: bool = typer.Option(False, "--show-source", help="Show extracted source text for comparison."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Save without prompts (replaces an existing master; a .bak is kept)."),
) -> None:
    """Read an existing resume file into your master resume."""
    _confirm_overwrite(yes)
    llm = LLM()
    try:
        with console.status(f"Reading {file.name} with Claude…"):
            result = import_resume(file, llm)
    except (ImportError_, LLMError) as e:
        _fail(str(e))

    r = result.resume
    review.show(console, r, validate.validate(r), result.warnings, result.source_text if show_source else None)

    if yes:
        _save_master(r)
        return
    action = questionary.select(
        "Does this look right?",
        choices=[
            questionary.Choice("Save it", value="save"),
            questionary.Choice("Fix things first (opens the editor)", value="edit"),
            questionary.Choice("Discard", value="discard"),
        ],
    ).ask()
    if action == "save":
        _save_master(r)
    elif action == "edit":
        _edit_then_save(r, llm)
    else:
        console.print("Discarded.")


@resume_app.command("build")
def resume_build(
    no_ai: bool = typer.Option(False, "--no-ai", help="Don't offer AI bullet suggestions."),
) -> None:
    """Create a master resume from scratch with a step-by-step wizard."""
    _confirm_overwrite()
    llm = None if no_ai else LLM()
    try:
        r = builder.build_new(llm)
    except KeyboardInterrupt:
        console.print("\nCancelled; nothing saved.")
        raise typer.Exit(1)
    review.show(console, r, validate.validate(r))
    if questionary.confirm("Save this resume?", default=True).ask():
        _save_master(r)
    elif questionary.confirm("Keep editing instead?", default=True).ask():
        _edit_then_save(r, llm)


@resume_app.command("edit")
def resume_edit(
    section: str = typer.Argument(None, help=f"Jump to a section: {', '.join(builder.SECTIONS[:-1])}"),
    no_ai: bool = typer.Option(False, "--no-ai", help="Don't offer AI bullet suggestions."),
) -> None:
    """Edit your master resume section by section."""
    r = _load_master()
    if section:
        match = [s for s in builder.SECTIONS if s.lower().startswith(section.lower())]
        if not match:
            _fail(f"Unknown section '{section}'.")
        section = match[0]
    _edit_then_save(r, None if no_ai else LLM(), section)


@resume_app.command("show")
def resume_show() -> None:
    """Print your master resume."""
    r = _load_master()
    review.show(console, r, validate.validate(r))


@resume_app.command("validate")
def resume_validate() -> None:
    """Check the master resume for missing or malformed fields (also after hand edits)."""
    try:
        r = store.load()
    except FileNotFoundError:
        _fail("No master resume yet.")
    except Exception as e:  # yaml / pydantic errors from hand edits
        _fail(f"Could not read {config.master_resume_path()}:\n{e}")
    before = store.all_ids(r)
    store.assign_ids(r)
    if store.all_ids(r) != before:
        store.save(r)
        console.print("[dim]Assigned ids to new items.[/dim]")
    issues = validate.validate(r)
    table = review.issues_table(issues)
    if table is None:
        console.print("[green]Master resume looks good.[/green]")
    else:
        console.print(table)
    if validate.has_errors(issues):
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------


@app.command()
def usage() -> None:
    """Show Claude token usage by task."""
    with db.session() as s:
        rows = s.exec(
            select(
                LLMUsage.task,
                func.count(),
                func.sum(LLMUsage.input_tokens),
                func.sum(LLMUsage.cache_read_tokens),
                func.sum(LLMUsage.output_tokens),
            ).group_by(LLMUsage.task)
        ).all()
    if not rows:
        console.print("No API calls logged yet.")
        return
    t = Table(title="Claude usage (tokens)")
    for col in ["Task", "Calls", "Input", "Cache read", "Output"]:
        t.add_column(col, justify="right" if col != "Task" else "left")
    for task, n, inp, cache, out in rows:
        t.add_row(task, str(n), f"{inp or 0:,}", f"{cache or 0:,}", f"{out or 0:,}")
    console.print(t)


from . import cli_jobs  # noqa: E402

cli_jobs.register(app)
