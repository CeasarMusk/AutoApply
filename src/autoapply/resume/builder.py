"""Interactive resume builder / editor.

`build_new` walks through every section from scratch; `edit` opens a menu over an existing
resume. Both mutate a MasterResume in memory; the caller decides whether to save.
Ctrl-C raises KeyboardInterrupt so the caller can offer to save or discard.
"""

from __future__ import annotations

from typing import Callable

import questionary
from pydantic import BaseModel, Field
from rich.console import Console
from rich.markup import escape as esc

from ..llm import LLM, LLMError
from ..models import (
    Bullet,
    Certification,
    Contact,
    Education,
    Experience,
    Link,
    MasterResume,
    Project,
    SkillGroup,
)
from . import review, store, validate

console = Console()

BACK = "← Back"
ADD = "+ Add new"


# ---------------------------------------------------------------------------
# small prompt helpers
# ---------------------------------------------------------------------------


def ask(label: str, default: str | None = None, required: bool = False) -> str | None:
    while True:
        value = questionary.text(label, default=default or "").unsafe_ask().strip()
        if value or not required:
            return value or None
        console.print("[red]This one is required.[/red]")


def ask_date(label: str, default: str | None = None, allow_present: bool = False) -> str | None:
    hint = " (YYYY-MM, YYYY" + (", or Present" if allow_present else "") + ")"

    def check(v: str) -> bool | str:
        v = v.strip()
        if not v or validate.DATE_RE.match(v):
            return True
        if allow_present and v.lower() in validate.PRESENT:
            return True
        return "Use YYYY-MM or YYYY" + (" or Present" if allow_present else "")

    value = questionary.text(label + hint, default=default or "", validate=check).unsafe_ask().strip()
    if allow_present and value.lower() in validate.PRESENT:
        return "Present"
    return value or None


def confirm(label: str, default: bool = True) -> bool:
    return questionary.confirm(label, default=default).unsafe_ask()


def choose(label: str, choices: list[str]) -> str:
    return questionary.select(label, choices=choices).unsafe_ask()


def _short(text: str, n: int = 70) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def _move_up(items: list, item) -> None:
    i = items.index(item)
    if i > 0:
        items[i - 1], items[i] = items[i], items[i - 1]


# ---------------------------------------------------------------------------
# AI bullet improvement (optional)
# ---------------------------------------------------------------------------


class BulletSuggestions(BaseModel):
    suggestions: list[str] = Field(description="Up to 3 rewrites of the bullet.")
    tip: str | None = Field(
        default=None,
        description="Optional advice, e.g. which metric the person could add if they have one.",
    )


IMPROVE_SYSTEM = """\
You help someone polish a single resume bullet point. Offer up to three rewrites that are
clearer and stronger: lead with an action verb, show impact, cut filler, keep it to one line
where possible. Never add facts, numbers, tools, or outcomes that are not in the original.
If a metric would help but none is given, say so in `tip` instead of inventing one.
"""


def improve_bullet(text: str, context: str, llm: LLM | None) -> str:
    """Return the chosen text (original if the user rejects all suggestions)."""
    llm = llm or LLM()
    try:
        with console.status("Asking Claude for suggestions…"):
            result = llm.parse(
                task="improve_bullet",
                system=IMPROVE_SYSTEM,
                content=f"Role: {context}\nBullet: {text}",
                output_format=BulletSuggestions,
                effort="low",
                max_tokens=2000,
            )
    except LLMError as e:
        console.print(f"[red]{esc(str(e))}[/red]")
        return text
    if result.tip:
        console.print(f"[cyan]Tip:[/cyan] {esc(result.tip)}")
    keep = "Keep original"
    pick = choose("Pick a version", [*result.suggestions[:3], keep])
    if pick == keep:
        return text
    return ask("Edit if needed", default=pick, required=True) or pick


# ---------------------------------------------------------------------------
# sections
# ---------------------------------------------------------------------------


def edit_contact(c: Contact) -> None:
    c.name = ask("Full name", c.name, required=True) or c.name
    c.email = ask("Email", c.email, required=True)
    c.phone = ask("Phone", c.phone)
    c.location = ask("Location (City, State/Country)", c.location)
    edit_links(c.links)


def edit_links(links: list[Link]) -> None:
    while True:
        choices = [f"{l.label}: {l.url}" for l in links] + [ADD, BACK]
        pick = choose("Links (LinkedIn, GitHub, portfolio…)", choices)
        if pick == BACK:
            return
        if pick == ADD:
            label = ask("Label (e.g. LinkedIn)", required=True)
            url = ask("URL", required=True)
            links.append(Link(label=label, url=url))
            continue
        link = links[choices.index(pick)]
        action = choose(pick, ["Edit", "Delete", BACK])
        if action == "Edit":
            link.label = ask("Label", link.label, required=True) or link.label
            link.url = ask("URL", link.url, required=True) or link.url
        elif action == "Delete":
            links.remove(link)


def edit_bullets(bullets: list[Bullet], context: str, llm: LLM | None) -> None:
    while True:
        choices = [_short(b.text) for b in bullets] + [ADD, BACK]
        pick = questionary.select(
            f"Bullets for {context}", choices=[questionary.Choice(c, value=i) for i, c in enumerate(choices)]
        ).unsafe_ask()
        if choices[pick] == BACK:
            return
        if choices[pick] == ADD:
            add_bullets(bullets, context, llm)
            continue
        b = bullets[pick]
        console.print(f"[bold]{esc(b.text)}[/bold]")
        action = choose("Bullet", ["Edit", "Improve with AI", "Move up", "Delete", BACK])
        if action == "Edit":
            b.text = ask("Text", b.text, required=True) or b.text
        elif action == "Improve with AI":
            b.text = improve_bullet(b.text, context, llm)
        elif action == "Move up":
            _move_up(bullets, b)
        elif action == "Delete":
            bullets.remove(b)


def add_bullets(bullets: list[Bullet], context: str, llm: LLM | None) -> None:
    console.print(
        "[dim]Enter one bullet at a time; leave blank to finish. Add more than fits on one "
        "page — tailoring picks the most relevant ones per job.[/dim]"
    )
    while True:
        text = ask(f"Bullet #{len(bullets) + 1}")
        if not text:
            return
        if llm is not None and confirm("Improve this bullet with AI?", default=False):
            text = improve_bullet(text, context, llm)
        bullets.append(Bullet(text=text))


def edit_experience_item(e: Experience, llm: LLM | None) -> None:
    e.title = ask("Job title", e.title, required=True) or e.title
    e.company = ask("Company", e.company, required=True) or e.company
    e.location = ask("Location", e.location)
    e.start = ask_date("Start date", e.start)
    e.end = ask_date("End date", e.end, allow_present=True)


def new_experience(llm: LLM | None) -> Experience:
    e = Experience(company="", title="")
    edit_experience_item(e, llm)
    add_bullets(e.bullets, f"{e.title} at {e.company}", llm)
    return e


def edit_education_item(ed: Education) -> None:
    ed.school = ask("School", ed.school, required=True) or ed.school
    ed.degree = ask("Degree (e.g. B.S.)", ed.degree)
    ed.field = ask("Field of study", ed.field)
    ed.start = ask_date("Start date", ed.start)
    ed.end = ask_date("End / expected graduation", ed.end, allow_present=True)
    ed.gpa = ask("GPA (optional)", ed.gpa)
    details = ask("Honors / coursework (comma-separated, optional)", ", ".join(ed.details))
    ed.details = [d.strip() for d in (details or "").split(",") if d.strip()]


def new_education() -> Education:
    ed = Education(school="")
    edit_education_item(ed)
    return ed


def edit_project_item(p: Project) -> None:
    p.name = ask("Project name", p.name, required=True) or p.name
    p.url = ask("URL (optional)", p.url)
    p.description = ask("One-line description (optional)", p.description)


def new_project(llm: LLM | None) -> Project:
    p = Project(name="")
    edit_project_item(p)
    add_bullets(p.bullets, f"project {p.name}", llm)
    return p


def edit_skill_item(s: SkillGroup) -> None:
    s.category = ask("Category (e.g. Languages, Tools)", s.category, required=True) or s.category
    items = ask("Skills (comma-separated)", ", ".join(s.items), required=True) or ""
    s.items = [i.strip() for i in items.split(",") if i.strip()]


def new_skill() -> SkillGroup:
    s = SkillGroup(category="")
    edit_skill_item(s)
    return s


def edit_cert_item(c: Certification) -> None:
    c.name = ask("Certification / award", c.name, required=True) or c.name
    c.issuer = ask("Issuer (optional)", c.issuer)
    c.date = ask_date("Date", c.date)


def new_cert() -> Certification:
    c = Certification(name="")
    edit_cert_item(c)
    return c


def _list_menu(
    title: str,
    items: list,
    label: Callable,
    create: Callable[[], object],
    edit_fields: Callable[[object], None],
    edit_bullets_for: Callable[[object], None] | None = None,
) -> None:
    while True:
        labels = [label(i) for i in items]
        choices = [questionary.Choice(l, value=n) for n, l in enumerate(labels)]
        choices += [questionary.Choice(ADD, value="add"), questionary.Choice(BACK, value="back")]
        pick = questionary.select(title, choices=choices).unsafe_ask()
        if pick == "back":
            return
        if pick == "add":
            items.append(create())
            continue
        item = items[pick]
        actions = ["Edit details"]
        if edit_bullets_for:
            actions.append("Edit bullets")
        actions += ["Move up", "Delete", BACK]
        action = choose(labels[pick], actions)
        if action == "Edit details":
            edit_fields(item)
        elif action == "Edit bullets" and edit_bullets_for:
            edit_bullets_for(item)
        elif action == "Move up":
            _move_up(items, item)
        elif action == "Delete" and confirm(f"Delete '{labels[pick]}'?", default=False):
            items.remove(item)


# ---------------------------------------------------------------------------
# entry points
# ---------------------------------------------------------------------------


def _section_loop(prompt: str, items: list, create: Callable[[], object]) -> None:
    while confirm(prompt, default=not items):
        items.append(create())


def build_new(llm: LLM | None = None) -> MasterResume:
    r = MasterResume()
    console.rule("Contact")
    edit_contact(r.contact)

    console.rule("Summary")
    r.summary = ask("Short professional summary (optional, tailoring rewrites this per job)")

    console.rule("Experience")
    console.print("[dim]Most recent first. Include internships, part-time, and contract work.[/dim]")
    while confirm("Add a job?" if not r.experience else "Add another job?", default=not r.experience):
        r.experience.append(new_experience(llm))

    console.rule("Education")
    _section_loop("Add education?" if not r.education else "Add more education?", r.education, new_education)

    console.rule("Projects")
    _section_loop("Add a project?", r.projects, lambda: new_project(llm))

    console.rule("Skills")
    _section_loop("Add a skill group?", r.skills, new_skill)

    console.rule("Certifications & awards")
    _section_loop("Add a certification or award?", r.certifications, new_cert)

    store.assign_ids(r)
    return r


SECTIONS = [
    "Contact",
    "Summary",
    "Experience",
    "Projects",
    "Education",
    "Skills",
    "Certifications",
    "Review",
]
DONE = "Save & exit"
DISCARD = "Exit without saving"


def edit(r: MasterResume, llm: LLM | None = None, section: str | None = None) -> bool:
    """Menu-driven editor. Returns True to save, False to discard."""
    while True:
        pick = section or choose("What do you want to edit?", [*SECTIONS, DONE, DISCARD])
        section = None
        if pick == DONE:
            return True
        if pick == DISCARD:
            if confirm("Discard all changes from this session?", default=False):
                return False
            continue
        if pick == "Contact":
            edit_contact(r.contact)
        elif pick == "Summary":
            r.summary = ask("Summary", r.summary)
        elif pick == "Experience":
            _list_menu(
                "Experience",
                r.experience,
                lambda e: f"{e.title} @ {e.company} ({e.start or '?'}–{e.end or '?'})",
                lambda: new_experience(llm),
                lambda e: edit_experience_item(e, llm),
                lambda e: edit_bullets(e.bullets, f"{e.title} at {e.company}", llm),
            )
        elif pick == "Projects":
            _list_menu(
                "Projects",
                r.projects,
                lambda p: p.name,
                lambda: new_project(llm),
                edit_project_item,
                lambda p: edit_bullets(p.bullets, f"project {p.name}", llm),
            )
        elif pick == "Education":
            _list_menu("Education", r.education, lambda e: e.school, new_education, edit_education_item)
        elif pick == "Skills":
            _list_menu(
                "Skills",
                r.skills,
                lambda s: f"{s.category}: {_short(', '.join(s.items), 50)}",
                new_skill,
                edit_skill_item,
            )
        elif pick == "Certifications":
            _list_menu("Certifications", r.certifications, lambda c: c.name, new_cert, edit_cert_item)
        elif pick == "Review":
            store.assign_ids(r)
            review.show(console, r, validate.validate(r))
