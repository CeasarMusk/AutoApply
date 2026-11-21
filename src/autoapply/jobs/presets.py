"""Preset search filters: field -> specialty -> title keywords, plus experience levels.

Edit freely: keywords are title phrases (every word of a phrase must appear in the title;
word endings are ignored, so "engineer" also matches "engineering").
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Specialty:
    id: str
    label: str
    keywords: tuple[str, ...]
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class Field:
    id: str
    label: str
    specialties: tuple[Specialty, ...]


FIELDS: tuple[Field, ...] = (
    Field("cs", "Computer Science & Software", (
        Specialty("swe", "Software Engineering (general)", (
            "software engineer", "software developer", "swe", "programmer", "application engineer",
        )),
        Specialty("backend", "Backend / Distributed Systems", (
            "backend", "back end", "distributed systems", "api engineer", "platform engineer",
        )),
        Specialty("frontend", "Frontend / Web", (
            "frontend", "front end", "web engineer", "web developer", "ui engineer", "javascript",
        )),
        Specialty("fullstack", "Full Stack", ("full stack", "fullstack")),
        Specialty("mobile", "Mobile (iOS / Android)", ("ios", "android", "mobile engineer", "mobile developer")),
        Specialty("infra", "Infrastructure / DevOps / SRE / Cloud", (
            "devops", "site reliability", "sre", "infrastructure engineer", "cloud engineer",
            "production engineer", "systems engineer", "build engineer", "release engineer",
        )),
        Specialty("security", "Security", (
            "security engineer", "application security", "product security", "security analyst",
            "detection engineer", "penetration", "offensive security", "appsec",
        )),
        Specialty("embedded", "Embedded / Systems / Hardware-adjacent", (
            "embedded", "firmware", "systems software", "kernel", "robotics software", "autonomy",
        )),
        Specialty("qa", "QA / Test / Automation", ("qa engineer", "test engineer", "sdet", "quality engineer", "automation engineer")),
        Specialty("games", "Game Development", ("gameplay", "game engineer", "game developer", "engine programmer")),
    )),
    Field("data", "Data & AI", (
        Specialty("ml", "Machine Learning / AI Engineering", (
            "machine learning", "ml engineer", "ai engineer", "applied scientist", "research engineer",
            "deep learning", "llm", "computer vision", "nlp",
        )),
        Specialty("datasci", "Data Science", ("data scientist", "data science", "quantitative", "statistician")),
        Specialty("dataeng", "Data Engineering", ("data engineer", "analytics engineer", "data platform", "etl")),
        Specialty("analyst", "Data / Business Analytics", ("data analyst", "business analyst", "analytics", "business intelligence", "bi analyst")),
        Specialty("research", "Research Scientist", ("research scientist", "researcher")),
    )),
    Field("it", "IT & Support", (
        Specialty("itsupport", "IT / Help Desk", ("it support", "help desk", "helpdesk", "it specialist", "desktop support", "it technician")),
        Specialty("sysadmin", "Systems / Network Admin", ("system administrator", "systems administrator", "network engineer", "network administrator", "it engineer")),
        Specialty("solutions", "Solutions / Support Engineering", ("solutions engineer", "support engineer", "technical support", "customer engineer", "forward deployed")),
    )),
    Field("product", "Product & Design", (
        Specialty("pm", "Product Management", ("product manager", "technical program manager", "program manager")),
        Specialty("design", "Product / UX Design", ("product designer", "ux designer", "ui designer", "ux researcher", "interaction designer")),
    )),
)


@dataclass(frozen=True)
class Level:
    id: str
    label: str
    require: tuple[str, ...] = ()  # title must contain one of these
    exclude: tuple[str, ...] = ()


SENIOR_WORDS = ("senior", "sr", "staff", "principal", "lead", "manager", "director", "head", "vp",
                "architect", "distinguished", "fellow", "chief", "iii", "iv")

LEVELS: tuple[Level, ...] = (
    Level("intern", "Internship / Co-op", require=("intern", "internship", "co-op", "coop")),
    # numbered levels: "Engineer II", "Engineer 3"
    Level("entry", "New grad / Entry level", exclude=SENIOR_WORDS + ("intern", "internship", "ii", "2", "3", "4", "5")),
    Level("mid", "Mid level (not senior+)", exclude=SENIOR_WORDS + ("intern", "internship", "new grad", "3", "4", "5")),
    Level("senior", "Senior and above", require=("senior", "sr", "staff", "principal", "lead")),
    Level("any", "Any level"),
)

LOCATION_PRESETS: tuple[tuple[str, list[str], bool], ...] = (
    # label, locations, remote_only
    ("Remote only", [], True),
    ("United States (incl. remote US)", ["@US"], False),
    ("Anywhere", [], False),
)


def field_by_id(fid: str) -> Field:
    return next(f for f in FIELDS if f.id == fid)


def specialty_by_id(sid: str) -> Specialty:
    return next(s for f in FIELDS for s in f.specialties if s.id == sid)


def level_by_id(lid: str) -> Level:
    return next(l for l in LEVELS if l.id == lid)


def all_specialty_ids() -> list[str]:
    return [s.id for f in FIELDS for s in f.specialties]


@dataclass
class Preset:
    """A resolved search choice, saved so `search` can repeat it."""

    label: str
    keywords: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    require: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    remote_only: bool = False


def build(specialty_ids: list[str], level_id: str, location_label: str, extra_keywords: list[str] | None = None) -> Preset:
    specs = [specialty_by_id(s) for s in specialty_ids]
    level = level_by_id(level_id)
    preset_loc = {p[0]: p for p in LOCATION_PRESETS}.get(location_label)
    if preset_loc:
        loc_label, locs, remote = preset_loc
    else:  # a city/region the user typed
        loc_label, locs, remote = location_label, [location_label], False
    kws = list(dict.fromkeys([k for s in specs for k in s.keywords] + (extra_keywords or [])))
    excl = list(dict.fromkeys([x for s in specs for x in s.exclude] + list(level.exclude)))
    names = ", ".join(s.label for s in specs) if len(specs) <= 2 else f"{len(specs)} specialties"
    return Preset(
        label=f"{names} · {level.label} · {loc_label}",
        keywords=kws,
        exclude=excl,
        require=list(level.require),
        locations=locs,
        remote_only=remote,
    )
