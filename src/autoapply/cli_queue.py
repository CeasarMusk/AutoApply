"""`queue ...`: line up jobs and apply to them one after another (you still review and submit each)."""

from __future__ import annotations

import sys

import questionary
import typer
from rich.console import Console
from rich.markup import escape as esc

from . import config, db
from .jobs import queue as q
from .models import Job

console = Console()
queue_app = typer.Typer(
    help="Line up jobs to apply to one after another, best match or newest first. "
    "Run `queue` alone to see it.",
    invoke_without_command=True,
)


def _fail(msg: str) -> None:
    console.print(f"[red]{esc(msg)}[/red]")
    raise typer.Exit(1)


def _interactive() -> bool:
    return sys.stdin.isatty()


def _check_order(order: str | None) -> str | None:
    if order is not None and order not in q.ORDERS:
        _fail(f"Unknown order '{order}'. Use one of: {', '.join(q.ORDERS)}")
    return order


def _pick_order(default: str) -> str | None:
    return questionary.select(
        "Apply in which order?",
        choices=[questionary.Choice(desc, value=key) for key, desc in q.ORDERS.items()],
        default=default,
    ).ask()


def _pick_source() -> str | None:
    last = q.last_results()
    with db.session() as s:
        counts = {src: len(q.one_per_role(q.candidates(s, src))) for src in q.SOURCES}
    choices = []
    for key, desc in q.SOURCES.items():
        label = desc + (f" ({esc(last[1])})" if key == "last" and last else "")
        choices.append(questionary.Choice(f"{label} — {counts[key]} job(s)", value=key,
                                          disabled="nothing there" if not counts[key] else None))
    if not any(counts.values()):
        return None
    return questionary.select("Which jobs should go in the queue?", choices=choices,
                              default=next((c for c in choices if not c.disabled), None)).ask()


def _order_label(order: str) -> str:
    return "best match first" if order == "match" else "newest first"


@queue_app.callback()
def _queue_root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        queue_show(limit=50)


@queue_app.command("show")
def queue_show(limit: int = typer.Option(50, "--limit", "-n")) -> None:
    """List the queue in the order `queue run` will go through it."""
    from .cli_jobs import _row, _table

    cfg = config.load_config()
    with db.session() as s:
        pruned = q.prune(s)
        rows = q.items(s)
        table_rows = [_row(j) for _, j in rows[:limit]]
    if pruned:
        console.print(f"[dim]Dropped {pruned} job(s) that were applied to, ignored, or closed.[/dim]")
    if not rows:
        console.print("The apply queue is empty. `queue build` fills it from your last search or shortlist.")
        return
    title = f"Apply queue · {len(rows)} job(s) · {_order_label(cfg.queue_order)}"
    console.print(_table(table_rows, title, show_skills=False))
    if len(rows) > limit:
        console.print(f"[dim]…{len(rows) - limit} more.[/dim]")
    console.print("[dim]`queue run` to start · `queue order recent|match` to re-sort · `queue remove <id>`[/dim]")


@queue_app.command("build")
def queue_build(
    source: str = typer.Option(None, "--from", "-f",
                               help="Which jobs: last (last search's matches), saved (shortlist), all. Asks if omitted."),
    order: str = typer.Option(None, "--order", "-o", help="match (best match first) or recent (newest first). Asks if omitted."),
    min_match: int = typer.Option(None, "--min-match", "-m", help="Only jobs with at least this match score (0-100)."),
    limit: int = typer.Option(None, "--limit", "-n", help="Queue at most this many jobs."),
    replace: bool = typer.Option(False, "--replace", help="Empty the queue first instead of adding to it."),
) -> None:
    """Queue the current list of matching jobs to apply to, in the order you choose."""
    cfg = config.load_config()
    _check_order(order)
    if source is not None and source not in q.SOURCES:
        _fail(f"Unknown source '{source}'. Use one of: {', '.join(q.SOURCES)}")
    if source is None:
        if _interactive():
            source = _pick_source()
            if source is None:
                _fail("No jobs to queue yet. Run `search` first.")
        else:
            source = "last" if q.last_results() else "saved"
    if order is None:
        order = _pick_order(cfg.queue_order) if _interactive() else cfg.queue_order
        if order is None:
            console.print("Cancelled.")
            return
    with db.session() as s:
        jobs = q.candidates(s, source)
        if not jobs:
            hint = {"last": "Run `search` first.", "saved": "Shortlist jobs with `save <id>`.",
                    "all": "Run `search` first."}[source]
            _fail(f"No jobs to queue from {q.SOURCES[source].lower()}. {hint}")
        added, already = q.add(s, jobs, order, replace=replace, min_match=min_match, limit=limit)
        total = len(q.items(s))
    if cfg.queue_order != order:
        cfg.queue_order = order
        config.save_config(cfg)
    msg = f"[green]Queued {added} job(s)[/green]" + (f", {already} already queued" if already else "")
    console.print(f"{msg}. The queue has {total}, {_order_label(order)}.")
    if added or total:
        queue_show(limit=15)


@queue_app.command("add")
def queue_add(job_ids: list[int] = typer.Argument(..., help="Job ids from `jobs` (or `use <id>` first in the console)")) -> None:
    """Add specific jobs to the queue."""
    cfg = config.load_config()
    with db.session() as s:
        jobs = [j for i in job_ids if (j := s.get(Job, i)) is not None]
        missing = sorted(set(job_ids) - {j.id for j in jobs})
        skipped = [j for j in jobs if j.status in q.INACTIVE]
        added, already = q.add(s, jobs, cfg.queue_order)
    if missing:
        console.print(f"[yellow]No job with id {', '.join(map(str, missing))}.[/yellow]")
    for j in skipped:
        console.print(f"[yellow]{j.id} is {j.status}; not queued.[/yellow]")
    console.print(f"Queued {added}" + (f", {already} already queued" if already else "") + ".")


@queue_app.command("remove")
def queue_remove(job_ids: list[int] = typer.Argument(..., help="Job ids to take out of the queue")) -> None:
    """Take jobs out of the queue (they stay in `jobs`)."""
    with db.session() as s:
        n = q.remove(s, job_ids)
    console.print(f"Removed {n} job(s) from the queue.")


@queue_app.command("clear")
def queue_clear(yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask.")) -> None:
    """Empty the queue."""
    if not yes and _interactive() and not questionary.confirm("Empty the apply queue?", default=False).ask():
        return
    with db.session() as s:
        n = q.clear(s)
    console.print(f"Removed {n} job(s). The queue is empty.")


@queue_app.command("order")
def queue_order(order: str = typer.Argument(None, help="match (best match first) or recent (newest first)")) -> None:
    """Re-sort the queue: best match first, or most recently posted first."""
    cfg = config.load_config()
    _check_order(order)
    if order is None:
        order = _pick_order(cfg.queue_order) if _interactive() else None
        if order is None:
            console.print(f"The queue is sorted {_order_label(cfg.queue_order)}. Use `queue order match|recent`.")
            return
    cfg.queue_order = order
    config.save_config(cfg)
    with db.session() as s:
        q.resort(s, order)
    console.print(f"Queue sorted {_order_label(order)}.")
    queue_show(limit=15)


def _header(i: int, n: int, job: Job) -> None:
    from .cli_jobs import _ago

    bits = [f"match {job.match_score}" if job.match_score is not None else None,
            f"posted {_ago(job.posted_at)} ago" if job.posted_at else None,
            job.location]
    console.rule(f"[bold][{i}/{n}][/bold] {esc(job.title)} @ {esc(job.company)}", align="left")
    console.print(f"[dim]#{job.id} · {esc(' · '.join(b for b in bits if b))}[/dim]")


@queue_app.command("run")
def queue_run(
    no_ai: bool = typer.Option(False, "--no-ai", help="Don't draft open-ended answers with AI."),
    no_tailor: bool = typer.Option(False, "--no-tailor", help="Upload your original resume instead of tailored ones."),
    max_jobs: int = typer.Option(None, "--max", help="Stop after this many applications."),
) -> None:
    """Go through the queue: each application is filled in a browser and you review and submit it."""
    from .cli_apply import apply_one
    from .cli_jobs import job_show

    if not _interactive():
        _fail("`queue run` needs a terminal: you review and submit each application yourself.")
    with db.session() as s:
        q.prune(s)
        todo = [j for _, j in q.items(s)]
    if not todo:
        console.print("The apply queue is empty. `queue build` fills it from your last search or shortlist.")
        return
    cfg = config.load_config()
    console.print(f"[bold]{len(todo)} job(s) queued, {_order_label(cfg.queue_order)}.[/bold] "
                  "Each form opens in the browser; you review and click Submit. Ctrl-C stops the queue.")

    applied = skipped = removed = attempts = 0
    try:
        for i, job in enumerate(todo, 1):
            if max_jobs is not None and attempts >= max_jobs:
                break
            with db.session() as s:
                job = s.get(Job, job.id)
            if job is None or job.status in q.INACTIVE:
                continue
            _header(i, len(todo), job)
            while True:
                choice = questionary.select(
                    "What now?",
                    choices=[
                        questionary.Choice("Apply now", value="apply"),
                        questionary.Choice("Read the job description first", value="info"),
                        questionary.Choice("Skip for now (keep it queued)", value="skip"),
                        questionary.Choice("Remove it from the queue", value="remove"),
                        questionary.Choice("Ignore this job (hide it everywhere)", value="ignore"),
                        questionary.Choice("Stop here", value="stop"),
                    ],
                ).ask()
                if choice != "info":
                    break
                try:
                    job_show(job.id, full=False)
                except typer.Exit:
                    pass
            if choice in (None, "stop"):
                break
            if choice == "skip":
                skipped += 1
                continue
            if choice in ("remove", "ignore"):
                with db.session() as s:
                    q.remove(s, [job.id])
                    if choice == "ignore":
                        j = s.get(Job, job.id)
                        j.status = "ignored"
                        s.add(j)
                        s.commit()
                removed += 1
                continue

            attempts += 1
            outcome = apply_one(job, no_ai=no_ai, no_tailor=no_tailor, ask_external=True)
            if outcome == "applied":
                applied += 1
                with db.session() as s:
                    q.remove(s, [job.id])
            elif outcome.startswith("error:"):
                console.print(f"[red]{esc(outcome.removeprefix('error:'))}[/red]")
                if "browser isn't installed" in outcome:
                    break
                if not questionary.confirm("Keep going with the next job?", default=True).ask():
                    break
            elif outcome == "not_submitted":
                if not questionary.confirm("Keep it in the queue for later?", default=True).ask():
                    with db.session() as s:
                        q.remove(s, [job.id])
                    removed += 1
    except KeyboardInterrupt:
        console.print("\n[yellow]Queue stopped.[/yellow]")

    with db.session() as s:
        left = len(q.items(s))
    console.print(f"[bold]Done for now:[/bold] [green]{applied} applied[/green], {skipped} skipped, "
                  f"{removed} removed · {left} still queued" + (" (`queue run` to continue)." if left else "."))


def register(app: typer.Typer) -> None:
    app.add_typer(queue_app, name="queue")
