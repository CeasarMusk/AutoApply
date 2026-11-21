"""Sanity checks on a master resume (used after import, after building, and by `resume validate`)."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from ..models import MasterResume
from . import store

DATE_RE = re.compile(r"^(\d{4})(-(0[1-9]|1[0-2]))?$")
PRESENT = {"present", "current", "now"}


@dataclass
class Issue:
    level: str  # "error" | "warning"
    where: str
    message: str


def _check_date(value: str | None, where: str, issues: list[Issue], allow_present: bool) -> None:
    if value is None or value == "":
        return
    if allow_present and value.strip().lower() in PRESENT:
        return
    if not DATE_RE.match(value.strip()):
        issues.append(Issue("warning", where, f"date '{value}' is not YYYY-MM or YYYY"))


def validate(resume: MasterResume) -> list[Issue]:
    issues: list[Issue] = []
    c = resume.contact
    if not c.name.strip():
        issues.append(Issue("error", "contact.name", "name is missing"))
    if not c.email:
        issues.append(Issue("error", "contact.email", "email is missing"))
    elif "@" not in c.email:
        issues.append(Issue("error", "contact.email", f"'{c.email}' doesn't look like an email"))
    if not c.phone:
        issues.append(Issue("warning", "contact.phone", "phone is missing"))
    if not c.location:
        issues.append(Issue("warning", "contact.location", "location is missing"))

    if not resume.experience and not resume.projects:
        issues.append(Issue("error", "experience", "no experience or projects"))

    for exp in resume.experience:
        where = f"experience[{exp.id or exp.company}]"
        if not exp.title.strip():
            issues.append(Issue("error", where, "title is missing"))
        if not exp.company.strip():
            issues.append(Issue("error", where, "company is missing"))
        if not exp.start:
            issues.append(Issue("warning", where, "start date is missing"))
        _check_date(exp.start, where + ".start", issues, allow_present=False)
        _check_date(exp.end, where + ".end", issues, allow_present=True)
        if not exp.bullets:
            issues.append(Issue("warning", where, "no bullet points"))
        for b in exp.bullets:
            if not b.text.strip():
                issues.append(Issue("error", f"{where}.{b.id}", "empty bullet"))

    for proj in resume.projects:
        where = f"projects[{proj.id or proj.name}]"
        if not proj.bullets and not proj.description:
            issues.append(Issue("warning", where, "no description or bullets"))

    for edu in resume.education:
        where = f"education[{edu.id or edu.school}]"
        _check_date(edu.start, where + ".start", issues, allow_present=False)
        _check_date(edu.end, where + ".end", issues, allow_present=True)

    if not resume.skills:
        issues.append(Issue("warning", "skills", "no skills listed"))

    dupes = [i for i, n in Counter(store.all_ids(resume)).items() if n > 1]
    for d in dupes:
        issues.append(Issue("error", d, "duplicate id"))

    return issues


def has_errors(issues: list[Issue]) -> bool:
    return any(i.level == "error" for i in issues)
