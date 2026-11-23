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
from .llm import LLM, LLMError, backend_label
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


def _keep_resume_file(file: Path) -> None:
    """Keep a copy of the imported file: it's what gets uploaded to applications."""
    dest = config.home() / f"resume{file.suffix.lower()}"
    try:
        if file.expanduser().resolve() != dest.resolve():
            shutil.copy2(file.expanduser(), dest)
    except OSError as e:
        console.print(f"[yellow]Couldn't copy {esc(str(file))} for uploads: {esc(str(e))}[/yellow]")
        return
    cfg = config.load_config()
    cfg.resume_file = str(dest)
    config.save_config(cfg)
    console.print(f"[dim]Applications will upload {esc(str(dest))}[/dim]")


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
        from .cli_ai import pick_provider

        cfg = config.Config()
        pick_provider(cfg)
        config.save_config(cfg)
        console.print(f"Created {config.config_path()} [dim](change the AI provider any time with `ai`)[/dim]")
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
        with console.status(f"Reading {file.name} with {backend_label()}…"):
            result = import_resume(file, llm)
    except (ImportError_, LLMError) as e:
        _fail(str(e))

    r = result.resume
    review.show(console, r, validate.validate(r), result.warnings, result.source_text if show_source else None)

    def save_both() -> None:
        _save_master(r)
        _keep_resume_file(file)

    if yes:
        save_both()
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
        save_both()
    elif action == "edit":
        _edit_then_save(r, llm)
        _keep_resume_file(file)
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


@resume_app.command("github")
def resume_github(
    profile: str = typer.Argument(None, help="Your GitHub profile link (https://github.com/you) or username."),
    deep: bool = typer.Option(False, "--deep", help="Also read languages and dependency files, and write bullets "
                              "from each README with AI (fact-checked). Slower, more requests."),
    basic: bool = typer.Option(False, "--basic", help="Quick scan: repo list + README summaries, no AI (the default)."),
    max_repos: int = typer.Option(12, "--max-repos", "-n", help="Look at this many repos (most starred, then newest)."),
    forks: bool = typer.Option(False, "--forks", help="Include repos you forked."),
    no_ai: bool = typer.Option(False, "--no-ai", help="With --deep: skip the AI bullets."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Add the suggested repos without asking."),
) -> None:
    """Add projects (and skills) to your master resume from your public GitHub repos."""
    from .resume import github as gh

    if deep and basic:
        _fail("Pick one of --basic or --deep.")
    if profile is None:
        profile = questionary.text("Paste your GitHub profile link (e.g. https://github.com/yourname):").ask()
        if not profile:
            raise typer.Exit(0)
    try:
        username = gh.parse_username(profile)
    except gh.GitHubError as e:
        _fail(str(e))
    try:
        r = store.load()
    except FileNotFoundError:
        r = MasterResume()
        console.print("[dim]No master resume yet; starting one with your GitHub projects.[/dim]")

    try:
        with console.status(f"{'Deep' if deep else 'Basic'} scan of github.com/{username}…") as status:
            result = gh.scan(username, deep=deep, max_repos=max_repos, include_forks=forks,
                             progress=lambda m: status.update(m))
    except gh.GitHubError as e:
        _fail(str(e))
    if not result.repos:
        _fail(f"github.com/{result.username} has no public repos of its own"
              + (f" ({result.skipped_forks} forks skipped; add --forks to include them)." if result.skipped_forks else "."))

    t = Table(title=f"github.com/{esc(result.username)} · {len(result.repos)} of {result.total_public} public repos"
              + (" · deep scan" if deep else ""), title_justify="left", expand=True)
    t.add_column("#", justify="right", style="cyan")
    t.add_column("Repo", no_wrap=True)
    t.add_column("★", justify="right")
    t.add_column("Tech" if deep else "Language")
    t.add_column("From the README / description", ratio=3)
    for i, repo in enumerate(result.repos, 1):
        tech = ", ".join(dict.fromkeys(repo.languages + repo.tech)) if deep else (repo.language or "")
        note = esc(repo.summary or "") or "[dim](no README summary or description)[/dim]"
        if gh.already_in(r, repo):
            note = "[dim]already in your resume[/dim]"
        t.add_row(str(i), esc(repo.name) + (" [dim](archived)[/dim]" if repo.archived else ""),
                  str(repo.stars), esc(tech), note)
    console.print(t)
    if result.skipped_forks:
        console.print(f"[dim]{result.skipped_forks} forked repo(s) skipped (--forks to include).[/dim]")

    fresh = [repo for repo in result.repos if not gh.already_in(r, repo)]
    if not fresh:
        console.print("Every repo found is already in your resume's projects.")
        return
    suggested = [repo for repo in fresh if repo.summary][:6]
    if yes:
        chosen = suggested
    else:
        chosen = questionary.checkbox(
            "Which repos should become projects on your resume? (space to toggle, enter to confirm)",
            choices=[questionary.Choice(f"{repo.name} — {(repo.summary or 'no summary')[:70]}", value=repo,
                                        checked=repo in suggested) for repo in fresh],
        ).ask()
    if not chosen:
        console.print("Nothing added.")
        return

    bullets: dict[str, list[str]] = {}
    if deep and not no_ai:
        llm = LLM()
        for repo in chosen:
            try:
                with console.status(f"Writing bullets for {esc(repo.name)} with {backend_label()}…"):
                    kept, rejected = gh.ai_bullets(repo, llm)
            except LLMError as e:
                console.print(f"[yellow]AI bullets for {esc(repo.name)} failed ({esc(str(e))}); "
                              "using the README's feature list.[/yellow]")
                continue
            bullets[repo.full_name] = kept or (repo.readme.features if repo.readme else [])
            for b, why in rejected:
                console.print(f"[dim]Dropped a bullet for {esc(repo.name)} ({esc(why)}): {esc(b)}[/dim]")

    projects = [gh.to_project(repo, bullets.get(repo.full_name)) for repo in chosen]
    for p in projects:
        console.print(f"[bold]{esc(p.name)}[/bold]  [dim]{esc(p.url or '')}[/dim]")
        if p.description:
            console.print(f"  {esc(p.description)}")
        for b in p.bullets:
            console.print(f"  • {esc(b.text)}")
        if not p.description and not p.bullets:
            console.print("  [dim](nothing to say from the README; add a line with `resume edit projects`)[/dim]")
    extra = gh.new_skills(r, chosen)
    if extra:
        console.print("Skills these repos use that your resume doesn't list: "
                      + "; ".join(f"{k}: {', '.join(v)}" for k, v in extra.items()))
    if not yes and not questionary.confirm(f"Add {len(projects)} project(s) to your master resume?", default=True).ask():
        console.print("Nothing saved.")
        return
    r.projects += projects
    if extra and (yes or questionary.confirm("Add those skills too?", default=True).ask()):
        gh.add_skills(r, extra)
    if gh.add_github_link(r, result.profile_url):
        console.print(f"[dim]Added your GitHub link ({esc(result.profile_url)}) to your contact details.[/dim]")
    store.assign_ids(r)
    _save_master(r)
    if not deep:
        console.print("[dim]Tip: `resume github <link> --deep` also reads each repo's dependencies and "
                      "drafts bullets from the README.[/dim]")
    console.print("[dim]Review and polish them with `resume edit projects`.[/dim]")


TRANSCRIPT_TYPES = {".pdf", ".docx", ".doc", ".png", ".jpg", ".jpeg"}


@resume_app.command("transcript")
def resume_transcript(
    file: Path = typer.Argument(None, help="Your transcript (PDF recommended). Omit to see the current one."),
    remove: bool = typer.Option(False, "--remove", help="Forget the saved transcript."),
) -> None:
    """Save your transcript so it's uploaded whenever an application asks for one."""
    from .apply.runner import keep_transcript, saved_transcript

    current = saved_transcript()
    if remove:
        cfg = config.load_config()
        cfg.transcript_file = None
        config.save_config(cfg)
        if current and current.parent == config.home():
            current.unlink(missing_ok=True)
        console.print("Transcript removed." if current else "No transcript was saved.")
        return
    if file is None:
        if current:
            console.print(f"Applications upload [bold]{esc(str(current))}[/bold] when they ask for a transcript.")
        else:
            console.print("No transcript saved. Run `resume transcript <file.pdf>`.")
        return
    file = file.expanduser()
    if not file.is_file():
        _fail(f"No such file: {file}")
    if file.suffix.lower() not in TRANSCRIPT_TYPES:
        _fail(f"Use a {', '.join(sorted(TRANSCRIPT_TYPES))} file; application forms won't accept {file.suffix or 'that'}.")
    try:
        dest = keep_transcript(file)
    except OSError as e:
        _fail(f"Couldn't copy {file}: {e}")
    console.print(f"[green]Saved[/green] {esc(str(dest))}. It'll be uploaded whenever a form asks for a transcript.")


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
    """Show AI token usage by task."""
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
    t = Table(title="AI usage (tokens)")
    for col in ["Task", "Calls", "Input", "Cache read", "Output"]:
        t.add_column(col, justify="right" if col != "Task" else "left")
    for task, n, inp, cache, out in rows:
        t.add_row(task, str(n), f"{inp or 0:,}", f"{cache or 0:,}", f"{out or 0:,}")
    console.print(t)


from . import cli_jobs  # noqa: E402

cli_jobs.register(app)

from . import cli_apply  # noqa: E402

cli_apply.register(app)

from . import cli_queue  # noqa: E402

cli_queue.register(app)

from . import cli_ai  # noqa: E402

cli_ai.register(app)
