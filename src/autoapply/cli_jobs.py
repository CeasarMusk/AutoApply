"""Commands for companies and jobs: companies add/list/remove, search, jobs, job."""

from __future__ import annotations

import webbrowser
from datetime import datetime, timedelta, timezone

import typer
from rich.console import Console
from rich.markup import escape as esc
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from sqlmodel import col, func, select

from . import config, db, sources
from .jobs.search import Filters, matches, store
from .models import Job

console = Console()

companies_app = typer.Typer(help="Manage the companies whose job boards you search.", no_args_is_help=True)
job_app = typer.Typer(help="Look at or act on one job.", no_args_is_help=True)

STATUSES = ["new", "seen", "saved", "ignored", "applied"]


def _fail(msg: str) -> None:
    console.print(f"[red]{esc(msg)}[/red]")
    raise typer.Exit(1)


def _ago(dt: datetime | None) -> str:
    if dt is None:
        return "-"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    days = (datetime.now(timezone.utc) - dt).days
    if days <= 0:
        return "today"
    if days < 60:
        return f"{days}d"
    return f"{days // 30}mo"


def _get_job(job_id: int) -> Job:
    with db.session() as s:
        job = s.get(Job, job_id)
    if job is None:
        _fail(f"No job with id {job_id}. Run `jobs` to list them.")
    return job


# ---------------------------------------------------------------------------
# companies
# ---------------------------------------------------------------------------


@companies_app.command("add")
def companies_add(
    slugs: list[str] = typer.Argument(..., help="Board slugs, e.g. stripe ramp palantir (from the job-board URL)"),
    ats: str = typer.Option(None, "--ats", help="greenhouse, lever or ashby (auto-detected if omitted)"),
) -> None:
    """Add companies to search. Detects whether each uses Greenhouse, Lever or Ashby."""
    cfg = config.load_config()
    for raw in slugs:
        slug = raw.strip().lower()
        if ats:
            if ats not in sources.ATS:
                _fail(f"Unknown ATS '{ats}'. Use: {', '.join(sources.ATS)}")
            try:
                n = len(sources.fetch_company(ats, slug))
            except sources.NotFound:
                console.print(f"[red]✗[/red] {esc(slug)}: no {ats} board with that name")
                continue
            except sources.SourceError as e:
                console.print(f"[red]✗[/red] {esc(slug)}: {esc(str(e))}")
                continue
            found = [(ats, n)]
        else:
            with console.status(f"Looking for {slug} on Greenhouse, Lever, Ashby…"):
                found = sources.detect(slug)
            if not found:
                console.print(
                    f"[red]✗[/red] {esc(slug)}: not found on Greenhouse, Lever or Ashby. "
                    "Check the slug in the company's job-board URL "
                    "(boards.greenhouse.io/<slug>, jobs.lever.co/<slug>, jobs.ashbyhq.com/<slug>)."
                )
                continue
        best_ats, n = max(found, key=lambda x: x[1])
        added = cfg.companies.add(best_ats, slug)
        others = ", ".join(f"{a} ({c})" for a, c in found if a != best_ats)
        note = f"  [dim](also on {others})[/dim]" if others else ""
        state = "added" if added else "already added"
        console.print(f"[green]✓[/green] {esc(slug)}: {best_ats}, {n} open jobs — {state}{note}")
    config.save_config(cfg)


@companies_app.command("list")
def companies_list() -> None:
    """List the companies you're searching."""
    cfg = config.load_config()
    pairs = cfg.companies.pairs()
    if not pairs:
        console.print("No companies yet. Add some with `companies add stripe ramp palantir`.")
        return
    with db.session() as s:
        counts = dict(
            s.exec(select(Job.company_slug, func.count()).where(Job.status != "ignored").group_by(Job.company_slug)).all()
        )
    t = Table(title=f"{len(pairs)} companies")
    t.add_column("Company")
    t.add_column("ATS")
    t.add_column("Matching jobs", justify="right")
    for ats, slug in pairs:
        t.add_row(slug, ats, str(counts.get(slug, 0)))
    console.print(t)


@companies_app.command("remove")
def companies_remove(slugs: list[str] = typer.Argument(...)) -> None:
    """Stop searching these companies (their saved jobs stay)."""
    cfg = config.load_config()
    for slug in slugs:
        removed = cfg.companies.remove(slug.lower())
        console.print(f"Removed {esc(slug)}" if removed else f"[yellow]{esc(slug)} wasn't in the list[/yellow]")
    config.save_config(cfg)


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def search(
    keywords: list[str] = typer.Argument(None, help="Title keywords/phrases (default: your saved KEYWORDS)"),
    location: list[str] = typer.Option(None, "--location", "-l", help="Location filter; repeatable. 'Remote' matches remote jobs."),
    remote: bool = typer.Option(None, "--remote/--any-location", help="Only remote jobs."),
    exclude: list[str] = typer.Option(None, "--exclude", "-x", help="Drop titles with this word; repeatable."),
    max_age: int = typer.Option(None, "--max-age", help="Only jobs posted in the last N days."),
) -> None:
    """Pull open jobs from your companies and save the ones that match."""
    cfg = config.load_config()
    pairs = cfg.companies.pairs()
    if not pairs:
        _fail("No companies to search. Add some first: `companies add stripe ramp palantir`.")

    f = Filters.from_config(cfg.search)
    if keywords:
        f.keywords = list(keywords)
    if location:
        f.locations = list(location)
    if remote is not None:
        f.remote_only = remote
    if exclude:
        f.exclude = list(exclude)
    if max_age is not None:
        f.max_age_days = max_age

    with console.status(f"Fetching jobs from {len(pairs)} companies…"):
        report = sources.fetch_all(pairs)
    hits = [j for j in report.jobs if matches(j, f)]
    with db.session() as s:
        result = store(s, hits)
        new_rows = [(j.id, j.company, j.title, j.location, j.remote, j.posted_at) for j in result.new]

    for key, err in report.errors.items():
        console.print(f"[yellow]![/yellow] {esc(key)}: {esc(err)}")
    summary = (
        f"Scanned [bold]{len(report.jobs)}[/bold] jobs at {len(report.per_company)} companies → "
        f"[bold]{len(hits)}[/bold] match your filters "
        f"([green]{len(result.new)} new[/green], {result.updated} already saved)."
    )
    console.print(summary)
    if not any([f.keywords, f.locations, f.remote_only, f.exclude]):
        console.print("[dim]No filters set, so everything matched. Try `set KEYWORDS \"software engineer\"`.[/dim]")
    if new_rows:
        t = _jobs_table(new_rows[:25], title="New matches")
        console.print(t)
        if len(new_rows) > 25:
            console.print(f"[dim]…and {len(new_rows) - 25} more. Run `jobs` to see all.[/dim]")


# ---------------------------------------------------------------------------
# jobs list / job detail
# ---------------------------------------------------------------------------


def _jobs_table(rows, title: str, statuses: dict[int, str] | None = None, scores: dict[int, int | None] | None = None) -> Table:
    t = Table(title=title, expand=True)
    t.add_column("ID", justify="right", style="cyan", no_wrap=True)
    if scores is not None:
        t.add_column("Fit", justify="right", no_wrap=True)
    t.add_column("Company", no_wrap=True, max_width=18)
    t.add_column("Title", ratio=3)
    t.add_column("Location", ratio=2)
    t.add_column("Posted", justify="right", no_wrap=True)
    if statuses is not None:
        t.add_column("Status", no_wrap=True)
    for jid, company, jtitle, loc, is_remote, posted in rows:
        loc_text = Text(loc or "-")
        if is_remote:
            loc_text = Text.assemble(("● ", "green"), loc_text)
        cells = [str(jid)]
        if scores is not None:
            sc = scores.get(jid)
            cells.append("-" if sc is None else str(sc))
        cells += [Text(company), Text(jtitle), loc_text, _ago(posted)]
        if statuses is not None:
            st = statuses.get(jid, "")
            cells.append(Text(st, style={"new": "bold green", "saved": "yellow", "applied": "cyan", "ignored": "dim"}.get(st, "")))
        t.add_row(*cells)
    return t


def jobs(
    status: str = typer.Option(None, "--status", "-s", help=f"Only this status: {', '.join(STATUSES)}"),
    company: str = typer.Option(None, "--company", "-c", help="Only this company (slug or name)."),
    query: str = typer.Option(None, "--grep", "-g", help="Only titles containing this text."),
    remote: bool = typer.Option(False, "--remote", help="Only remote jobs."),
    show_all: bool = typer.Option(False, "--all", "-a", help="Include ignored jobs and ones no longer posted."),
    limit: int = typer.Option(50, "--limit", "-n"),
) -> None:
    """List saved jobs (best fit first once scored, then newest)."""
    with db.session() as s:
        q = select(Job)
        if status:
            q = q.where(Job.status == status)
        elif not show_all:
            q = q.where(Job.status != "ignored")
        if not show_all:
            q = q.where(Job.last_seen_at >= datetime.now(timezone.utc) - timedelta(days=7))
        if company:
            like = f"%{company.lower()}%"
            q = q.where((func.lower(Job.company).like(like)) | (Job.company_slug == company.lower()))
        if query:
            q = q.where(func.lower(Job.title).like(f"%{query.lower()}%"))
        if remote:
            q = q.where(Job.remote == True)  # noqa: E712
        total = s.exec(select(func.count()).select_from(q.subquery())).one()
        q = q.order_by(col(Job.fit_score).desc().nulls_last(), col(Job.posted_at).desc().nulls_last()).limit(limit)
        rows = s.exec(q).all()
        data = [(j.id, j.company, j.title, j.location, j.remote, j.posted_at) for j in rows]
        statuses = {j.id: j.status for j in rows}
        scores = {j.id: j.fit_score for j in rows}
    if not data:
        console.print("No jobs saved yet. Run `search`." if not (status or company or query) else "No jobs match.")
        return
    title = f"{total} job{'s' if total != 1 else ''}" + (f" (showing {limit})" if total > limit else "")
    console.print(_jobs_table(data, title, statuses, scores))


def _set_status(job_id: int, status: str) -> None:
    with db.session() as s:
        job = s.get(Job, job_id)
        if job is None:
            _fail(f"No job with id {job_id}.")
        job.status = status
        s.add(job)
        s.commit()


@job_app.command("show")
def job_show(
    job_id: int = typer.Argument(..., help="Job id from `jobs`"),
    full: bool = typer.Option(False, "--full", "-f", help="Print the whole description."),
) -> None:
    """Show a job's details and description."""
    job = _get_job(job_id)
    if job.status == "new":
        _set_status(job_id, "seen")
    meta = Table.grid(padding=(0, 2))
    meta.add_column(style="bold")
    meta.add_column()
    for label, value in [
        ("Company", job.company),
        ("Location", (job.location or "-") + ("  (remote)" if job.remote else "")),
        ("Workplace", job.workplace),
        ("Department", job.department),
        ("Type", job.employment_type),
        ("Salary", job.salary),
        ("Posted", f"{job.posted_at:%Y-%m-%d} ({_ago(job.posted_at)} ago)" if job.posted_at else None),
        ("Source", f"{job.source}:{job.company_slug}"),
        ("Status", job.status),
        ("Fit", f"{job.fit_score}/100" if job.fit_score is not None else "not scored yet"),
        ("URL", job.url),
        ("Apply", job.apply_url if job.apply_url and job.apply_url != job.url else None),
    ]:
        if value:
            meta.add_row(label, Text(str(value)))
    console.print(Panel(meta, title=Text(f"[{job.id}] {job.title}", style="bold"), border_style="cyan"))
    desc = job.description or "(no description)"
    lines = desc.splitlines()
    if not full and len(lines) > 40:
        desc = "\n".join(lines[:40])
        console.print(Text(desc))
        console.print(f"[dim]… {len(lines) - 40} more lines. Use `job show {job.id} --full`.[/dim]")
    else:
        console.print(Text(desc))


@job_app.command("open")
def job_open(job_id: int = typer.Argument(...)) -> None:
    """Open the job posting in your browser."""
    job = _get_job(job_id)
    webbrowser.open(job.url)
    console.print(f"Opened {esc(job.url)}")


def _status_cmd(status: str, help_text: str):
    def cmd(job_ids: list[int] = typer.Argument(..., help="One or more job ids")) -> None:
        for jid in job_ids:
            _set_status(jid, status)
        console.print(f"Marked {', '.join(map(str, job_ids))} as {status}.")

    cmd.__doc__ = help_text
    return cmd


job_app.command("save")(_status_cmd("saved", "Shortlist jobs you want to apply to."))
job_app.command("ignore")(_status_cmd("ignored", "Hide jobs you're not interested in."))
job_app.command("reset")(_status_cmd("seen", "Clear a saved/ignored mark."))


def register(app: typer.Typer) -> None:
    app.add_typer(companies_app, name="companies")
    app.add_typer(job_app, name="job")
    app.command("search")(search)
    app.command("jobs")(jobs)
