"""Pretty-print a master resume plus any problems found, for review in the terminal."""

from __future__ import annotations

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.markup import escape as esc
from rich.text import Text
from rich.tree import Tree

from ..models import MasterResume
from .validate import Issue


def _dates(start: str | None, end: str | None) -> str:
    if not start and not end:
        return "[red]no dates[/red]"
    return esc(f"{start or '?'} – {end or '?'}")


def render(resume: MasterResume) -> Tree:
    c = resume.contact
    contact_bits = [x for x in [c.email, c.phone, c.location] if x]
    contact_bits += [f"{l.label}: {l.url}" for l in c.links]
    root = Tree(Text.assemble((c.name or "(no name)", "bold"), "  ", (" · ".join(contact_bits), "dim")))

    if resume.summary:
        root.add(Text.assemble(("Summary  ", "bold cyan"), resume.summary))

    if resume.experience:
        exp_node = root.add("[bold cyan]Experience[/bold cyan]")
        for e in resume.experience:
            loc = f", {esc(e.location)}" if e.location else ""
            node = exp_node.add(
                f"[dim]{e.id}[/dim] [bold]{esc(e.title)}[/bold] @ {esc(e.company)}{loc}  "
                f"[dim]{_dates(e.start, e.end)}[/dim]"
            )
            for b in e.bullets:
                node.add(f"[dim]{b.id}[/dim] {esc(b.text)}")

    if resume.projects:
        proj_node = root.add("[bold cyan]Projects[/bold cyan]")
        for p in resume.projects:
            url = f"  [dim]{esc(p.url)}[/dim]" if p.url else ""
            node = proj_node.add(f"[dim]{p.id}[/dim] [bold]{esc(p.name)}[/bold]{url}")
            if p.description:
                node.add(Text(p.description))
            for b in p.bullets:
                node.add(f"[dim]{b.id}[/dim] {esc(b.text)}")

    if resume.education:
        edu_node = root.add("[bold cyan]Education[/bold cyan]")
        for ed in resume.education:
            degree = esc(", ".join(x for x in [ed.degree, ed.field] if x))
            node = edu_node.add(
                f"[dim]{ed.id}[/dim] [bold]{esc(ed.school)}[/bold]"
                f"{' — ' + degree if degree else ''}  [dim]{_dates(ed.start, ed.end)}[/dim]"
            )
            if ed.gpa:
                node.add(Text(f"GPA {ed.gpa}"))
            for d in ed.details:
                node.add(Text(d))

    if resume.skills:
        sk_node = root.add("[bold cyan]Skills[/bold cyan]")
        for s in resume.skills:
            sk_node.add(f"[dim]{s.id}[/dim] [bold]{esc(s.category)}:[/bold] {esc(', '.join(s.items))}")

    if resume.certifications:
        cert_node = root.add("[bold cyan]Certifications[/bold cyan]")
        for ct in resume.certifications:
            extra = esc(" · ".join(x for x in [ct.issuer, ct.date] if x))
            cert_node.add(f"[dim]{ct.id}[/dim] {esc(ct.name)}{'  [dim]' + extra + '[/dim]' if extra else ''}")

    return root


def issues_table(issues: list[Issue], warnings: list[str] | None = None) -> Table | None:
    warnings = warnings or []
    if not issues and not warnings:
        return None
    t = Table(title="Things to check", show_lines=False, expand=True)
    t.add_column("", width=3)
    t.add_column("Where", style="dim", no_wrap=True)
    t.add_column("Issue")
    for i in issues:
        icon = "[red]✗[/red]" if i.level == "error" else "[yellow]![/yellow]"
        t.add_row(icon, Text(i.where), Text(i.message))
    for w in warnings:
        t.add_row("[yellow]?[/yellow]", "import", Text(w))
    return t


def show(
    console: Console,
    resume: MasterResume,
    issues: list[Issue] | None = None,
    warnings: list[str] | None = None,
    source_text: str | None = None,
) -> None:
    if source_text:
        console.print(Panel(Text(source_text), title="Source text", border_style="dim"))
    console.print(Panel(render(resume), title="Master resume", border_style="cyan"))
    table = issues_table(issues or [], warnings)
    if table is not None:
        console.print(table)
    else:
        console.print(Group("[green]No problems found.[/green]"))
