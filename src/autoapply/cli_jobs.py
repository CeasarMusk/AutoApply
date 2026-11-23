"""Job commands: search (menu-driven), jobs, job show/open/save/ignore, companies, profile."""

from __future__ import annotations

import sys
import webbrowser
from datetime import datetime, timezone

import questionary
import typer
from rich.console import Console
from rich.markup import escape as esc
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from sqlmodel import col, func, select

from . import config, db, sources
from .jobs import presets
from .jobs import profile as profile_mod
from .jobs.rank import score
from .jobs.search import Filters, matches, store
from .models import Job
from .resume import store as resume_store

console = Console()

companies_app = typer.Typer(help="Extra companies to search (on top of the built-in list).", no_args_is_help=True)
job_app = typer.Typer(help="Look at or act on one job.", no_args_is_help=True)

STATUSES = ["new", "seen", "saved", "ignored", "applied", "closed"]
STATUS_STYLE = {"new": "bold green", "saved": "yellow", "applied": "cyan", "ignored": "dim", "closed": "dim strike"}


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
    if days < 730:
        return f"{days // 30}mo"
    return f"{days // 365}y"


def _get_job(job_id: int) -> Job:
    with db.session() as s:
        job = s.get(Job, job_id)
    if job is None:
        _fail(f"No job with id {job_id}. Run `jobs` to list them.")
    return job


def _match_text(score_: int | None) -> Text:
    if score_ is None:
        return Text("-", style="dim")
    style = "bold green" if score_ >= 60 else "yellow" if score_ >= 35 else "dim"
    return Text(f"{score_}%", style=style)


# ---------------------------------------------------------------------------
# search menu
# ---------------------------------------------------------------------------

BACK = "__back__"


def _ask(q):
    """Run a questionary prompt; Ctrl-C / Esc returns None."""
    return q.ask()


def _pick_level(default: str) -> str | None:
    return _ask(questionary.select(
        "Experience level?",
        choices=[questionary.Choice(l.label, value=l.id) for l in presets.LEVELS],
        default=default,
    ))


def _pick_location(cfg: config.Config) -> str | None:
    choices = [questionary.Choice(label, value=label) for label, _, _ in presets.LOCATION_PRESETS]
    choices.append(questionary.Choice("A city or region…", value="__city__"))
    pick = _ask(questionary.select("Where?", choices=choices))
    if pick == "__city__":
        city = _ask(questionary.text("City or region (e.g. Austin, New York, Canada):"))
        return city.strip() if city and city.strip() else None
    return pick


def _menu() -> dict | None:
    """Ask what to search for. Returns a saved-search dict, or None if cancelled."""
    cfg = config.load_config()
    has_resume = resume_store.exists()
    cached = profile_mod.load_cached()

    while True:
        choices = []
        if cfg.last_search:
            choices.append(questionary.Choice(f"↻ Repeat last: {cfg.last_search['label']}", value="last"))
        if has_resume:
            hint = f" — {cached.summary}" if cached else ""
            choices.append(questionary.Choice(f"★ Jobs like my resume{hint}", value="resume"))
        choices += [questionary.Choice(f"{f.label}  ▸", value=f"field:{f.id}") for f in presets.FIELDS]
        choices.append(questionary.Choice("✎ Type my own keywords", value="custom"))
        pick = _ask(questionary.select("What do you want to search for?", choices=choices))
        if pick is None:
            return None
        if pick == "last":
            return cfg.last_search

        extra: list[str] = []
        level_default = "entry"
        if pick == "resume":
            try:
                with console.status("Reading your resume to pick matching jobs (one-time, cached)…"):
                    prof = profile_mod.get(on_ai_error=lambda e: console.print(f"[yellow]AI unavailable ({esc(str(e))}); using a simpler profile.[/yellow]"))
            except FileNotFoundError:
                console.print("[red]No master resume. Run `resume import <your.pdf>` first.[/red]")
                continue
            console.print(Panel(
                Text.assemble(
                    (prof.summary + "\n\n", ""),
                    ("Titles: ", "bold"), ", ".join(prof.titles) + "\n",
                    ("Specialties: ", "bold"), ", ".join(presets.specialty_by_id(s).label for s in prof.specialties) + "\n",
                    ("Skills: ", "bold"), ", ".join(prof.skills[:15]),
                ),
                title="Your search profile", border_style="cyan",
            ))
            spec_ids = prof.specialties or ["swe"]
            extra = prof.titles
            level_default = prof.level
            label_prefix = "Like my resume"
        elif pick == "custom":
            text = _ask(questionary.text("Title keywords, comma-separated (e.g. python developer, data engineer):"))
            if not text:
                continue
            extra = [k.strip() for k in text.split(",") if k.strip()]
            spec_ids = []
            label_prefix = ", ".join(extra)
        else:
            fld = presets.field_by_id(pick.split(":", 1)[1])
            spec_ids = _ask(questionary.checkbox(
                f"{fld.label}: which specialties? (space to select, enter to confirm)",
                choices=[questionary.Choice(s.label, value=s.id) for s in fld.specialties],
                validate=lambda xs: bool(xs) or "Pick at least one (space bar).",
            ))
            if not spec_ids:
                continue
            label_prefix = None

        level = _pick_level(level_default)
        if level is None:
            continue
        where = _pick_location(cfg)
        if where is None:
            continue
        preset = presets.build(spec_ids, level, where, extra)
        label = f"{label_prefix} · {presets.level_by_id(level).label} · {where}" if label_prefix else preset.label
        saved = {"label": label, "specialties": spec_ids, "level": level, "location": where, "extra": extra}
        cfg.last_search = saved
        config.save_config(cfg)
        return saved


def _filters_from_saved(saved: dict) -> Filters:
    p = presets.build(saved["specialties"], saved["level"], saved["location"], saved.get("extra"))
    return Filters(keywords=p.keywords, exclude=p.exclude, require=p.require,
                   locations=p.locations, remote_only=p.remote_only)


def _ranking_profile() -> profile_mod.SearchProfile | None:
    if not resume_store.exists():
        return None
    try:
        return profile_mod.get(on_ai_error=lambda e: None)
    except Exception:
        return None


def _sort_key(j: Job):
    posted = j.posted_at or datetime.min
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    return (j.match_score if j.match_score is not None else -1, posted)


def run_search(f: Filters, label: str) -> None:
    cfg = config.load_config()
    targets = sources.search_targets(cfg)
    if not targets:
        _fail("Nothing to search: the built-in list is off and you haven't added companies.")

    console.print(f"[bold]Searching:[/bold] {esc(label)}")
    with console.status(f"Fetching {len(targets)} job boards…") as status:
        report = sources.fetch_all(targets, progress=lambda d, n: status.update(f"Fetching job boards… {d}/{n}"))
    hits = [j for j in report.jobs if matches(j, f)]

    prof = _ranking_profile()
    scores = {}
    if prof:
        for j in hits:
            m = score(j.title, j.description, prof)
            scores[j.dedupe_key] = (m.score, m.skills)

    with db.session() as s:
        result = store(s, hits, scores, report.fetched_keys)
        keys = [h.dedupe_key for h in hits]
        rows = s.exec(select(Job).where(Job.dedupe_key.in_(keys), Job.status != "ignored")).all() if keys else []
        rows = sorted(rows, key=_sort_key, reverse=True)
        new_ids = {j.id for j in result.new}
        arranged = arrange([_row(j, new=j.id in new_ids) for j in rows])
        table_rows = arranged[:30]

    errors = list(report.errors.items())
    for key, err in errors[:5]:
        console.print(f"[yellow]![/yellow] {esc(key)}: {esc(err)}", highlight=False)
    if len(errors) > 5:
        console.print(f"[yellow]![/yellow] …and {len(errors) - 5} more boards couldn't be fetched.", highlight=False)
    console.print(
        f"Scanned [bold]{len(report.jobs):,}[/bold] jobs on {len(report.per_company)} boards → "
        f"[bold]{len(hits)}[/bold] match ([green]{len(result.new)} new[/green])"
        + (f", {result.closed} saved jobs no longer posted" if result.closed else "") + "."
    )
    if not table_rows:
        console.print("[dim]Nothing matched. Try a broader level or location.[/dim]")
        return
    console.print(_table(table_rows, "Best matches" if prof else "Matches", show_skills=bool(prof)))
    if len(arranged) > 30:
        console.print(f"[dim]…{len(arranged) - 30} more: `jobs`[/dim]")
    console.print("[dim]`use <id>` then `info` / `open` / `save`. Listings credited to remoteok.com and "
                  "SimplifyJobs (github.com/SimplifyJobs). [yellow]manual[/yellow] = AutoApply can't fill that site yet.[/dim]")
    if not prof:
        console.print("[dim]Import your resume (`resume import <pdf>`) to rank results by how well they match you.[/dim]")


def search(
    keywords: list[str] = typer.Argument(None, help="Title keywords/phrases. Omit to pick from the menu."),
    location: list[str] = typer.Option(None, "--location", "-l", help="Location (whole word); 'Remote' = remote jobs. Repeatable."),
    remote: bool = typer.Option(False, "--remote", help="Only remote jobs."),
    exclude: list[str] = typer.Option(None, "--exclude", "-x", help="Drop titles with this word. Repeatable."),
    level: str = typer.Option(None, "--level", help="intern, entry, mid, senior, any"),
    last: bool = typer.Option(False, "--last", help="Repeat the last menu search without asking."),
) -> None:
    """Search job boards. With no arguments, pick what to look for from a menu."""
    cfg = config.load_config()
    if last:
        if not cfg.last_search:
            _fail("No previous search yet.")
        run_search(_filters_from_saved(cfg.last_search), cfg.last_search["label"])
        return

    if keywords or location or remote or exclude or level:
        lvl = presets.level_by_id(level) if level else presets.level_by_id("any")
        f = Filters(
            keywords=list(keywords or []),
            exclude=list(exclude or []) + list(lvl.exclude),
            require=list(lvl.require),
            locations=["@US" if l.strip().lower() in ("us", "usa", "united states") else l for l in (location or [])],
            remote_only=remote,
        )
        run_search(f, " ".join(keywords or []) or "custom filters")
        return

    if not sys.stdin.isatty():
        _fail("Pass keywords (e.g. `search \"software engineer\" --level entry`) or run in a terminal for the menu.")
    saved = _menu()
    if saved is None:
        console.print("Cancelled.")
        return
    run_search(_filters_from_saved(saved), saved["label"])


# ---------------------------------------------------------------------------
# jobs list
# ---------------------------------------------------------------------------


AUTOFILL = {"greenhouse", "lever", "ashby"}  # sites the form filler supports
SITE_NAMES = {"greenhouse": "Greenhouse", "lever": "Lever", "ashby": "Ashby", "remoteok": "Remote OK",
              "workday": "Workday", "icims": "iCIMS", "smartrecruiters": "SmartRecruiters",
              "workable": "Workable", "jobvite": "Jobvite", "web": "company site"}


def _row(j: Job, new: bool = False) -> dict:
    return dict(id=j.id, match=j.match_score, company=j.company, title=j.title, location=j.location,
                remote=j.remote, posted=j.posted_at, status=j.status, skills=j.matched_skills, new=new,
                source=j.source, also=[])


def arrange(rows: list[dict], per_company: int = 3) -> list[dict]:
    """Collapse the same role posted in several cities into one row, then spread companies out:
    each company gets at most `per_company` rows before anyone gets more. Input must be sorted best-first."""
    grouped: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r["company"].lower(), " ".join(r["title"].lower().split()))
        if key in grouped:
            g = grouped[key]
            g["also"].append(r["id"])
            if r["location"] and r["location"] not in (g["location"] or ""):
                g["location"] = f"{g['location']} | {r['location']}" if g["location"] else r["location"]
            g["remote"] = g["remote"] or r["remote"]
            g["new"] = g["new"] or r["new"]
        else:
            grouped[key] = dict(r, also=[])
    first, rest, counts = [], [], {}
    for r in grouped.values():
        c = r["company"].lower()
        counts[c] = counts.get(c, 0) + 1
        (first if counts[c] <= per_company else rest).append(r)
    return first + rest


def _table(rows: list[dict], title: str, show_status: bool = False, show_skills: bool = True) -> Table:
    t = Table(title=title, expand=True)
    t.add_column("ID", justify="right", style="cyan", no_wrap=True)
    t.add_column("Match", justify="right", no_wrap=True)
    t.add_column("Company", no_wrap=True, max_width=16)
    t.add_column("Title", ratio=3)
    t.add_column("Location", ratio=2)
    t.add_column("Posted", justify="right", no_wrap=True)
    if show_skills:
        t.add_column("Your skills it mentions", ratio=2, style="dim")
    if show_status:
        t.add_column("Status", no_wrap=True)
    for r in rows:
        loc = (r["location"] or "-")
        loc = loc if len(loc) <= 60 else loc[:57] + "…"
        loc_text = Text.assemble(("● ", "green"), loc) if r["remote"] else Text(loc)
        company = r["company"] + (" (via Remote OK)" if r["source"] == "remoteok" else "")
        title = Text(r["title"])
        if r.get("also"):
            n = len(r["also"])
            title.append(f" (+{n} more location{'s' if n > 1 else ''})", style="dim")
        if r.get("new"):
            title.append(" new", style="bold green")
        if r["source"] not in AUTOFILL and r["source"] != "remoteok":
            title.append(f" · {SITE_NAMES.get(r['source'], r['source'])} (manual)", style="yellow")
        cells = [str(r["id"]), _match_text(r["match"]), Text(company), title, loc_text, _ago(r["posted"])]
        if show_skills:
            cells.append(Text(r["skills"] or ""))
        if show_status:
            cells.append(Text(r["status"], style=STATUS_STYLE.get(r["status"], "")))
        t.add_row(*cells)
    return t


def jobs(
    status: str = typer.Option(None, "--status", "-s", help=f"Only this status: {', '.join(STATUSES)}"),
    company: str = typer.Option(None, "--company", "-c", help="Only this company."),
    query: str = typer.Option(None, "--grep", "-g", help="Only titles containing this text."),
    remote: bool = typer.Option(False, "--remote", help="Only remote jobs."),
    show_all: bool = typer.Option(False, "--all", "-a", help="Include ignored and closed jobs."),
    limit: int = typer.Option(40, "--limit", "-n"),
) -> None:
    """List saved jobs, best match first."""
    with db.session() as s:
        q = select(Job)
        if status:
            q = q.where(Job.status == status)
        elif not show_all:
            q = q.where(Job.status.not_in(["ignored", "closed"]))
        if company:
            q = q.where(func.lower(Job.company).like(f"%{company.lower()}%") | (Job.company_slug == company.lower()))
        if query:
            q = q.where(func.lower(Job.title).like(f"%{query.lower()}%"))
        if remote:
            q = q.where(Job.remote == True)  # noqa: E712
        q = q.order_by(col(Job.match_score).desc().nulls_last(), col(Job.posted_at).desc().nulls_last())
        rows = arrange([_row(j) for j in s.exec(q).all()], per_company=3 if not company else 10**6)
        total = len(rows)
        rows = rows[:limit]
    if not rows:
        console.print("No jobs saved yet. Run `search`." if not (status or company or query) else "No jobs match.")
        return
    title = f"{total} job{'s' if total != 1 else ''}" + (f" (top {limit})" if total > limit else "") + \
        ("  ·  same role in several cities shown once" if any(r["also"] for r in rows) else "")
    console.print(_table(rows, title, show_status=True, show_skills=any(r["skills"] for r in rows)))


# ---------------------------------------------------------------------------
# single job
# ---------------------------------------------------------------------------


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
        ("Company", job.company + (" (via Remote OK)" if job.source == "remoteok" else "")),
        ("Applies on", SITE_NAMES.get(job.source, job.source)
         + ("" if job.source in AUTOFILL else " (AutoApply can't fill this site yet)")),
        ("Location", (job.location or "-") + ("  (remote)" if job.remote else "")),
        ("Match", f"{job.match_score}% like your resume" if job.match_score is not None else None),
        ("Your skills", job.matched_skills),
        ("Salary", job.salary),
        ("Type", job.employment_type),
        ("Department", job.department),
        ("Posted", f"{job.posted_at:%Y-%m-%d} ({_ago(job.posted_at)} ago)" if job.posted_at else None),
        ("Status", job.status),
        ("URL", job.url),
        ("Apply", job.apply_url if job.apply_url and job.apply_url != job.url else None),
    ]:
        if value:
            meta.add_row(label, Text(str(value)))
    console.print(Panel(meta, title=Text(f"[{job.id}] {job.title}", style="bold"), border_style="cyan"))
    lines = (job.description or "(no description)").splitlines()
    if not full and len(lines) > 40:
        console.print(Text("\n".join(lines[:40])))
        console.print(f"[dim]… {len(lines) - 40} more lines: `job show {job.id} --full` or `open`.[/dim]")
    else:
        console.print(Text("\n".join(lines)))


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


# ---------------------------------------------------------------------------
# companies & profile
# ---------------------------------------------------------------------------


@companies_app.command("add")
def companies_add(slugs: list[str] = typer.Argument(..., help="Board slugs from the company's job-board URL")) -> None:
    """Add companies to search. Detects whether each uses Greenhouse, Lever or Ashby."""
    cfg = config.load_config()
    for raw in slugs:
        slug = raw.strip().lower()
        with console.status(f"Looking for {slug}…"):
            found = sources.detect(slug)
        if not found:
            console.print(f"[red]✗[/red] {esc(slug)}: not found on Greenhouse, Lever or Ashby "
                          "(use the name from boards.greenhouse.io/<slug>, jobs.lever.co/<slug>, jobs.ashbyhq.com/<slug>)")
            continue
        best_ats, n = max(found, key=lambda x: x[1])
        added = cfg.companies.add(best_ats, slug)
        console.print(f"[green]✓[/green] {esc(slug)}: {best_ats}, {n} open jobs — {'added' if added else 'already added'}")
    config.save_config(cfg)


@companies_app.command("list")
def companies_list() -> None:
    """Show which job boards a search covers."""
    cfg = config.load_config()
    mine = cfg.companies.pairs()
    console.print(f"Built-in list: {'[green]on[/green]' if cfg.use_starter_companies else '[red]off[/red]'} "
                  f"({len(sources.starter.pairs())} companies)   Remote OK: "
                  f"{'[green]on[/green]' if cfg.use_remoteok else '[red]off[/red]'}   GitHub lists: "
                  f"{'[green]on[/green]' if cfg.use_github_lists else '[red]off[/red]'}")
    if cfg.use_starter_companies:
        console.print(Text(", ".join(sorted(s for _, s in sources.starter.pairs())), style="dim"))
    console.print(f"Your companies: {', '.join(f'{s} ({a})' for a, s in mine) if mine else 'none'}")


@companies_app.command("remove")
def companies_remove(slugs: list[str] = typer.Argument(...)) -> None:
    """Remove companies you added."""
    cfg = config.load_config()
    for slug in slugs:
        removed = cfg.companies.remove(slug.lower())
        console.print(f"Removed {esc(slug)}" if removed else f"[yellow]{esc(slug)} isn't one of your added companies[/yellow]")
    config.save_config(cfg)


def profile(refresh: bool = typer.Option(False, "--refresh", help="Rebuild it from the resume with AI.")) -> None:
    """Show the search profile built from your resume (used for `Jobs like my resume` and ranking)."""
    try:
        with console.status("Building your search profile…"):
            prof = profile_mod.get(refresh=refresh, on_ai_error=lambda e: console.print(f"[yellow]{esc(str(e))}[/yellow]"))
    except FileNotFoundError:
        _fail("No master resume. Run `resume import <your.pdf>` first.")
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="bold")
    grid.add_column()
    grid.add_row("Summary", Text(prof.summary))
    grid.add_row("Titles", Text(", ".join(prof.titles)))
    grid.add_row("Specialties", Text(", ".join(presets.specialty_by_id(s).label for s in prof.specialties)))
    grid.add_row("Level", presets.level_by_id(prof.level).label)
    grid.add_row("Skills", Text(", ".join(prof.skills)))
    grid.add_row("Built by", "AI" if prof.source == "ai" else "local fallback (no AI)")
    console.print(Panel(grid, title="Search profile", border_style="cyan"))


def register(app: typer.Typer) -> None:
    app.add_typer(companies_app, name="companies")
    app.add_typer(job_app, name="job")
    app.command("search")(search)
    app.command("jobs")(jobs)
    app.command("profile")(profile)
