"""Tailor the master resume to one job.

Claude only *chooses*: which experience, projects, bullets and skills to show, in what
order, and may lightly reword a bullet to use the job's language. Everything is referenced
by id, and a guard checks every reworded bullet against the original: no new numbers, no
new tools/names that appear nowhere in your master resume. A rewrite that fails falls back
to your original wording, so the tailored resume never claims anything the master doesn't.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml
from pydantic import BaseModel, Field

from ..llm import LLM
from ..models import Bullet, Job, MasterResume, SkillGroup

# ---------------------------------------------------------------------------
# what Claude returns
# ---------------------------------------------------------------------------


class BulletPick(BaseModel):
    id: str = Field(description="Bullet id from the master resume, e.g. exp2.b3")
    text: str | None = Field(default=None, description="Reworded text, or null to keep the original wording")


class ItemPick(BaseModel):
    id: str = Field(description="Experience or project id, e.g. exp2 or proj1")
    bullets: list[BulletPick] = Field(default_factory=list, description="Most relevant first")


class SkillPick(BaseModel):
    id: str = Field(description="Skill group id, e.g. skill1")
    items: list[str] = Field(default_factory=list, description="Items from that group, most relevant first")


class TailorPlan(BaseModel):
    summary: str | None = Field(default=None, description="Summary adapted to this job, or null to keep the original")
    experience: list[ItemPick] = Field(default_factory=list, description="In the order to show them")
    projects: list[ItemPick] = Field(default_factory=list, description="Most relevant first")
    skills: list[SkillPick] = Field(default_factory=list, description="Most relevant group first")
    certifications: list[str] = Field(default_factory=list, description="Relevant certification ids")
    education_first: bool = Field(default=False, description="True for students/new grads with little experience")
    keywords: list[str] = Field(default_factory=list, description="Job keywords the resume now covers")
    notes: str = Field(default="", description="One or two sentences on what was emphasized and why")


SYSTEM = """\
You tailor a resume to one job posting. You may only select, order and lightly reword items from the
master resume given to you, referring to them by id.

Rules:
- Never add experience, employers, projects, skills, tools, numbers or achievements that aren't in the
  master resume. Rewording a bullet may change emphasis and use the job's terminology for things the
  bullet already says, but must keep every fact, number and tool exactly as it is. If in doubt, set
  text to null to keep the original.
- Aim for one page: usually 2-5 bullets per recent role, fewer for old or unrelated roles, and only the
  1-3 most relevant projects. Keep every job unless it is clearly irrelevant and the resume is long.
  Order bullets most relevant first. Keep experience in reverse-chronological order.
- Skills: include groups and items relevant to the job, most relevant first. Only use items that exist
  in that group.
- Include certifications only if relevant.
- Write names, tools and terms exactly as the master spells them; don't introduce new abbreviations or
  capitalized terms (the program rejects rewrites containing words that aren't in the master).
- If the master has a summary, you may adapt it to this role (2-3 sentences): emphasize what's relevant,
  drop goals or skills that point away from this job. Same rule: no facts, skills or numbers that aren't
  already in the master resume.
"""


# ---------------------------------------------------------------------------
# result
# ---------------------------------------------------------------------------


@dataclass
class Tailored:
    resume: MasterResume
    education_first: bool = False
    keywords: list[str] = field(default_factory=list)
    notes: str = ""
    reworded: list[tuple[str, str]] = field(default_factory=list)  # (original, new)
    rejected: list[tuple[str, str, str]] = field(default_factory=list)  # (original, attempted, reason)
    summary_changed: bool = False
    dropped_for_space: int = 0

    def to_yaml(self) -> str:
        return yaml.safe_dump({
            "education_first": self.education_first, "keywords": self.keywords, "notes": self.notes,
            "resume": self.resume.model_dump(mode="json", exclude_none=True),
        }, sort_keys=False, allow_unicode=True, width=100)

    @classmethod
    def from_yaml(cls, text: str) -> "Tailored":
        d = yaml.safe_load(text) or {}
        return cls(resume=MasterResume.model_validate(d.get("resume") or {}),
                   education_first=bool(d.get("education_first")), keywords=d.get("keywords") or [],
                   notes=d.get("notes") or "")


# ---------------------------------------------------------------------------
# guard
# ---------------------------------------------------------------------------

_NUM = re.compile(r"\d[\d,.]*")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#./-]*[A-Za-z0-9+#]|[A-Za-z]")


def _numbers(s: str) -> set[str]:
    return {n.rstrip(".,").replace(",", "") for n in _NUM.findall(s)}


def _vocab(resume: MasterResume) -> set[str]:
    text = yaml.safe_dump(resume.model_dump(mode="json", exclude_none=True), allow_unicode=True)
    vocab = {w.lower() for w in _WORD.findall(text)}
    # Initials of multi-word names count too: "University of Central Florida" -> UCF.
    names = [e.school for e in resume.education] + [x.company for x in resume.experience]
    for n in names:
        caps = "".join(w[0] for w in n.split() if w[:1].isupper())
        if len(caps) >= 2:
            vocab.add(caps.lower())
    return vocab


def _special_terms(s: str) -> list[str]:
    """Words that look like names/tools: capitalized mid-sentence, ALLCAPS, or with digits/symbols."""
    out = []
    for i, m in enumerate(_WORD.finditer(s)):
        w = m.group()
        start = m.start()
        sentence_start = i == 0 or re.search(r"[.!?;:]\s*$", s[:start])
        if (w[0].isupper() and not sentence_start) or (len(w) > 1 and w.isupper()) or re.search(r"[\d+#./]", w):
            out.append(w)
    return out


def check_rewrite(original: str, new: str, vocab: set[str]) -> str | None:
    """Return why a reworded bullet is unsafe, or None if it's fine."""
    new_nums = _numbers(new) - _numbers(original)
    if new_nums:
        return f"adds number(s) {', '.join(sorted(new_nums))}"
    unknown = [w for w in _special_terms(new) if w.lower() not in vocab]
    if unknown:
        return f"adds {', '.join(dict.fromkeys(unknown))}, which isn't in your resume"
    if len(new) > len(original) * 1.6 + 40:
        return "much longer than the original"
    return None


# ---------------------------------------------------------------------------
# building
# ---------------------------------------------------------------------------


def apply_plan(master: MasterResume, plan: TailorPlan) -> Tailored:
    """Build the tailored resume from Claude's picks, ignoring anything not in the master."""
    vocab = _vocab(master)
    out = Tailored(resume=MasterResume(contact=master.contact.model_copy(deep=True), summary=master.summary,
                                       education=[e.model_copy(deep=True) for e in master.education]),
                   education_first=plan.education_first, keywords=plan.keywords, notes=plan.notes)
    new_summary = (plan.summary or "").strip()
    if master.summary and new_summary and new_summary != master.summary.strip():
        why = check_rewrite(master.summary, new_summary, vocab)
        if why:
            out.rejected.append((master.summary, new_summary, why))
        else:
            out.resume.summary = new_summary
            out.summary_changed = True

    def pick_bullets(item, picks: list[BulletPick]) -> list[Bullet]:
        by_id = {b.id: b for b in item.bullets}
        chosen, seen = [], set()
        for p in picks:
            b = by_id.get(p.id)
            if b is None or b.id in seen:
                continue
            seen.add(b.id)
            text = b.text
            new = (p.text or "").strip()
            if new and new != b.text:
                why = check_rewrite(b.text, new, vocab)
                if why:
                    out.rejected.append((b.text, new, why))
                else:
                    text = new
                    out.reworded.append((b.text, new))
            chosen.append(Bullet(id=b.id, text=text))
        return chosen

    def pick_items(items, picks: list[ItemPick]):
        by_id = {x.id: x for x in items}
        chosen, seen = [], set()
        for p in picks:
            x = by_id.get(p.id)
            if x is None or x.id in seen:
                continue
            seen.add(x.id)
            copy = x.model_copy(deep=True)
            copy.bullets = pick_bullets(x, p.bullets)
            if not copy.bullets and x.bullets:
                copy.bullets = [b.model_copy() for b in x.bullets[:2]]
            chosen.append(copy)
        return chosen

    exp = pick_items(master.experience, plan.experience)
    if not exp and master.experience:  # never hand back a resume with no work history
        exp = [e.model_copy(deep=True) for e in master.experience]
    order = {e.id: i for i, e in enumerate(master.experience)}
    out.resume.experience = sorted(exp, key=lambda e: order[e.id])
    out.resume.projects = pick_items(master.projects, plan.projects)

    groups = {g.id: g for g in master.skills}
    for sp in plan.skills:
        g = groups.pop(sp.id, None)
        if g is None:
            continue
        lower = {i.lower(): i for i in g.items}
        items = list(dict.fromkeys(lower[i.lower()] for i in sp.items if i.lower() in lower))
        if items:
            out.resume.skills.append(SkillGroup(id=g.id, category=g.category, items=items))
    if not out.resume.skills:
        out.resume.skills = [g.model_copy(deep=True) for g in master.skills]

    certs = {c.id: c for c in master.certifications}
    out.resume.certifications = [certs[c].model_copy() for c in dict.fromkeys(plan.certifications) if c in certs]
    return out


def tailor(master: MasterResume, job: Job, llm: LLM | None = None) -> Tailored:
    llm = llm or LLM()
    master_text = yaml.safe_dump(master.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True)
    content = (
        f"<master_resume>\n{master_text}\n</master_resume>\n\n"
        f"<job company=\"{job.company}\" title=\"{job.title}\" location=\"{job.location or ''}\">\n"
        f"{(job.description or '')[:12000]}\n</job>"
    )
    plan = llm.parse(task="tailor", system=SYSTEM, content=content, output_format=TailorPlan,
                     effort="medium", max_tokens=8000)
    return apply_plan(master, plan)


def trim_once(t: Tailored) -> bool:
    """Drop the least important line to save space. Returns False if nothing sensible is left to drop."""
    r = t.resume
    # 1. a bullet from the role/project with the most bullets (keeping 2 per role, 1 per project)
    cands = [(len(x.bullets), i, x) for i, x in enumerate(r.experience) if len(x.bullets) > 2]
    cands += [(len(p.bullets), 100 + i, p) for i, p in enumerate(r.projects) if len(p.bullets) > 1]
    if cands:
        _, _, x = max(cands, key=lambda c: (c[0], c[1]))
        x.bullets.pop()
        t.dropped_for_space += 1
        return True
    if len(r.projects) > 1:
        r.projects.pop()
        t.dropped_for_space += 1
        return True
    if r.certifications:
        r.certifications.pop()
        t.dropped_for_space += 1
        return True
    if len(r.skills) > 2:
        r.skills.pop()
        t.dropped_for_space += 1
        return True
    if r.projects:
        r.projects.pop()
        t.dropped_for_space += 1
        return True
    return False
