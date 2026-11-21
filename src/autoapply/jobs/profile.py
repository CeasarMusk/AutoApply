"""A search profile derived from the master resume: which jobs are "like my resume".

One AI call turns the resume into target titles, matching preset specialties, a level and a
skills list. It's cached in ~/.autoapply/profile.yaml and only rebuilt when the resume changes.
If the AI isn't available, a simple local version is built from the resume itself.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal

import yaml
from pydantic import BaseModel, Field

from .. import config
from ..llm import LLM, LLMError
from ..models import MasterResume
from ..resume import store
from . import presets


class SearchProfile(BaseModel):
    titles: list[str] = Field(description="4-8 job titles this person is a strong fit for right now")
    specialties: list[str] = Field(description="ids from the specialty list that fit this resume")
    level: Literal["intern", "entry", "mid", "senior", "any"] = Field(
        description="intern if still a student seeking internships; entry if <2 years full-time; "
        "mid if 2-5 years; senior if 5+ years"
    )
    skills: list[str] = Field(description="10-30 concrete skills/tools/technologies taken from the resume")
    summary: str = Field(description="One sentence: what kind of roles this person should look for")
    resume_hash: str = ""
    source: str = "ai"  # ai | local


def profile_path():
    return config.home() / "profile.yaml"


def resume_hash(r: MasterResume) -> str:
    return hashlib.sha256(r.model_dump_json().encode()).hexdigest()[:16]


SYSTEM = """\
You help a job seeker search job boards. Given their resume, describe the jobs they should
search for. Only use what the resume shows; don't inflate seniority. Pick specialty ids only
from the provided list. Skills must appear in the resume (normalize spelling, e.g. "postgres"
-> "PostgreSQL").
"""


def _resume_text(r: MasterResume) -> str:
    return yaml.safe_dump(r.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True)


def _specialty_menu() -> str:
    return "\n".join(f"- {s.id}: {f.label} / {s.label}" for f in presets.FIELDS for s in f.specialties)


def build_with_ai(r: MasterResume, llm: LLM | None = None) -> SearchProfile:
    llm = llm or LLM()
    prof = llm.parse(
        task="search_profile",
        system=SYSTEM,
        content=f"Specialty ids:\n{_specialty_menu()}\n\n<resume>\n{_resume_text(r)}\n</resume>",
        output_format=SearchProfile,
        effort="low",
        max_tokens=4000,
    )
    valid = set(presets.all_specialty_ids())
    prof.specialties = [s for s in prof.specialties if s in valid]
    prof.source = "ai"
    return prof


def build_local(r: MasterResume) -> SearchProfile:
    """No-AI fallback: titles and skills straight from the resume."""
    titles = list(dict.fromkeys(e.title for e in r.experience if e.title))[:6]
    skills = list(dict.fromkeys(i for g in r.skills for i in g.items))[:30]
    text = " ".join(titles).lower()
    specs = [s.id for f in presets.FIELDS for s in f.specialties
             if any(re.search(rf"\b{re.escape(k)}\b", text) for k in s.keywords)]
    return SearchProfile(titles=titles, specialties=specs or ["swe"], level="any", skills=skills,
                         summary="Built from your resume titles and skills (AI unavailable).", source="local")


def load_cached() -> SearchProfile | None:
    path = profile_path()
    if not path.exists():
        return None
    try:
        return SearchProfile.model_validate(yaml.safe_load(path.read_text()))
    except Exception:
        return None


def save(prof: SearchProfile) -> None:
    profile_path().write_text(yaml.safe_dump(prof.model_dump(), sort_keys=False, allow_unicode=True))


def get(refresh: bool = False, llm: LLM | None = None, on_ai_error=None) -> SearchProfile:
    """Return the profile for the current master resume, building it if needed."""
    r = store.load()  # FileNotFoundError if there's no master resume
    h = resume_hash(r)
    cached = load_cached()
    if cached and cached.resume_hash == h and not refresh and cached.source == "ai":
        return cached
    try:
        prof = build_with_ai(r, llm)
    except LLMError as e:
        if on_ai_error:
            on_ai_error(e)
        if cached and cached.resume_hash == h:
            return cached
        prof = build_local(r)
    prof.resume_hash = h
    save(prof)
    return prof
