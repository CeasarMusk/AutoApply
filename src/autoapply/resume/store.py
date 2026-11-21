"""Load/save the master resume and keep item ids stable.

Ids look like ``exp2``, ``exp2.b3``, ``proj1``, ``edu1``, ``skill1``, ``cert1``. Once an
item has an id it never changes; new items get the next unused number. That lets
tailored resumes (and the hallucination guard) refer to items by id even after edits.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from .. import config
from ..models import MasterResume

HEADER = (
    "# AutoApply master resume. Include everything (more than fits on one page);\n"
    "# tailoring picks from here and never invents content. Ids are managed by the\n"
    "# program: leave them alone, and leave `id: ''` on new items you add by hand.\n"
)


def load(path: Path | None = None) -> MasterResume:
    path = path or config.master_resume_path()
    if not path.exists():
        raise FileNotFoundError(path)
    return MasterResume.model_validate(yaml.safe_load(path.read_text()) or {})


def save(resume: MasterResume, path: Path | None = None) -> Path:
    path = path or config.master_resume_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    assign_ids(resume)
    data = resume.model_dump(mode="json", exclude_none=True)
    path.write_text(HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100))
    return path


def exists(path: Path | None = None) -> bool:
    return (path or config.master_resume_path()).exists()


class _Allocator:
    def __init__(self, used: set[str]):
        self.used = used

    def next(self, prefix: str) -> str:
        n = 1
        while f"{prefix}{n}" in self.used:
            n += 1
        new = f"{prefix}{n}"
        self.used.add(new)
        return new


def all_ids(resume: MasterResume) -> list[str]:
    ids: list[str] = []
    for exp in resume.experience:
        ids.append(exp.id)
        ids.extend(b.id for b in exp.bullets)
    for proj in resume.projects:
        ids.append(proj.id)
        ids.extend(b.id for b in proj.bullets)
    ids.extend(e.id for e in resume.education)
    ids.extend(s.id for s in resume.skills)
    ids.extend(c.id for c in resume.certifications)
    return [i for i in ids if i]


def assign_ids(resume: MasterResume) -> MasterResume:
    """Fill in missing ids, and replace malformed or duplicate ones, without touching valid ids.

    Top-level items keep an id like ``exp3``; bullets keep ``exp3.b2`` only while they sit
    under ``exp3`` (a bullet moved to another job gets a fresh id).
    """
    used: set[str] = set()

    def claim(obj, pattern: str) -> bool:
        if obj.id and re.fullmatch(pattern, obj.id) and obj.id not in used:
            used.add(obj.id)
            return True
        obj.id = ""
        return False

    alloc = _Allocator(used)
    sections = [
        (resume.experience, "exp"),
        (resume.projects, "proj"),
        (resume.education, "edu"),
        (resume.skills, "skill"),
        (resume.certifications, "cert"),
    ]
    for items, prefix in sections:
        for item in items:
            claim(item, rf"{prefix}\d+")
    for items, prefix in sections:
        for item in items:
            if not item.id:
                item.id = alloc.next(prefix)

    for parent in [*resume.experience, *resume.projects]:
        for b in parent.bullets:
            claim(b, rf"{re.escape(parent.id)}\.b\d+")
        for b in parent.bullets:
            if not b.id:
                b.id = alloc.next(f"{parent.id}.b")
    return resume


def _walk(resume: MasterResume):
    for exp in resume.experience:
        yield exp
        yield from exp.bullets
    for proj in resume.projects:
        yield proj
        yield from proj.bullets
    yield from resume.education
    yield from resume.skills
    yield from resume.certifications
