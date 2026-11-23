"""Data models.

``MasterResume`` is the single source of truth for everything the AI may put on a
tailored resume. Every item carries a stable ``id`` so tailoring can reference items
by id and the guard can reject anything that isn't traceable back to the master.
"""

from __future__ import annotations

from datetime import datetime, timezone

from pydantic import BaseModel, Field
from sqlmodel import Field as SQLField
from sqlmodel import SQLModel

# ---------------------------------------------------------------------------
# Master resume (stored as YAML)
# ---------------------------------------------------------------------------


class Link(BaseModel):
    label: str = Field(description="e.g. LinkedIn, GitHub, Portfolio")
    url: str


class Contact(BaseModel):
    name: str = ""
    email: str | None = None
    phone: str | None = None
    location: str | None = Field(default=None, description="City, State/Country")
    links: list[Link] = Field(default_factory=list)


class Bullet(BaseModel):
    id: str = Field(default="", description="Leave empty; assigned by the program.")
    text: str


class Experience(BaseModel):
    id: str = Field(default="", description="Leave empty; assigned by the program.")
    company: str
    title: str
    location: str | None = None
    start: str | None = Field(default=None, description="YYYY-MM or YYYY")
    end: str | None = Field(default=None, description="YYYY-MM, YYYY, or 'Present'")
    bullets: list[Bullet] = Field(default_factory=list)


class Education(BaseModel):
    id: str = Field(default="", description="Leave empty; assigned by the program.")
    school: str
    degree: str | None = None
    field: str | None = None
    start: str | None = None
    end: str | None = None
    gpa: str | None = None
    details: list[str] = Field(default_factory=list, description="Honors, coursework, etc.")


class Project(BaseModel):
    id: str = Field(default="", description="Leave empty; assigned by the program.")
    name: str
    url: str | None = None
    description: str | None = None
    bullets: list[Bullet] = Field(default_factory=list)


class SkillGroup(BaseModel):
    id: str = Field(default="", description="Leave empty; assigned by the program.")
    category: str = Field(description="e.g. Languages, Frameworks, Tools")
    items: list[str] = Field(default_factory=list)


class Certification(BaseModel):
    id: str = Field(default="", description="Leave empty; assigned by the program.")
    name: str
    issuer: str | None = None
    date: str | None = None


class MasterResume(BaseModel):
    contact: Contact = Field(default_factory=Contact)
    summary: str | None = None
    experience: list[Experience] = Field(default_factory=list)
    education: list[Education] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: list[SkillGroup] = Field(default_factory=list)
    certifications: list[Certification] = Field(default_factory=list)


class ResumeExtraction(BaseModel):
    """What the importer asks Claude to return."""

    resume: MasterResume
    warnings: list[str] = Field(
        default_factory=list,
        description=(
            "Anything uncertain: unreadable text, ambiguous dates, items that might be "
            "split or merged incorrectly, sections that did not fit the schema."
        ),
    )


# ---------------------------------------------------------------------------
# Database tables (sqlite)
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Job(SQLModel, table=True):
    id: int | None = SQLField(default=None, primary_key=True)
    source: str
    company_slug: str = ""
    external_id: str
    company: str
    title: str
    location: str | None = None
    remote: bool = False
    workplace: str | None = None
    department: str | None = None
    employment_type: str | None = None
    salary: str | None = None
    url: str
    apply_url: str | None = None
    description: str = ""
    posted_at: datetime | None = None
    dedupe_key: str = SQLField(index=True, unique=True)
    # new / seen / saved / ignored / applied / closed (no longer posted)
    status: str = "new"
    match_score: int | None = None  # local resume-similarity score, 0-100
    matched_skills: str | None = None
    fit_score: int | None = None  # AI fit score (later)
    fit_reasons: str | None = None
    dealbreakers: str | None = None
    found_at: datetime = SQLField(default_factory=_now)
    last_seen_at: datetime = SQLField(default_factory=_now)


class Application(SQLModel, table=True):
    id: int | None = SQLField(default=None, primary_key=True)
    job_id: int = SQLField(foreign_key="job.id", index=True)
    status: str = "tailored"  # tailored / applied / interview / rejected / offer / withdrawn
    resume_path: str | None = None
    cover_letter_path: str | None = None
    notes: str | None = None
    created_at: datetime = SQLField(default_factory=_now)
    updated_at: datetime = SQLField(default_factory=_now)


class QueueItem(SQLModel, table=True):
    """A job waiting in the apply queue. `queue run` walks these in `position` order."""

    id: int | None = SQLField(default=None, primary_key=True)
    job_id: int = SQLField(foreign_key="job.id", index=True, unique=True)
    position: int = 0
    added_at: datetime = SQLField(default_factory=_now)


class LLMUsage(SQLModel, table=True):
    id: int | None = SQLField(default=None, primary_key=True)
    task: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    created_at: datetime = SQLField(default_factory=_now)
