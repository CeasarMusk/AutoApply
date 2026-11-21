"""Filter fetched jobs against your search settings and store the matches."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlmodel import Session, select

from ..config import SearchDefaults
from ..models import Job
from ..sources import RawJob

MAX_DESCRIPTION_CHARS = 20_000


@dataclass
class Filters:
    keywords: list[str] = field(default_factory=list)  # any phrase; all words of a phrase must be in the title
    exclude: list[str] = field(default_factory=list)  # drop titles containing any of these words
    locations: list[str] = field(default_factory=list)  # any substring match on location text
    remote_only: bool = False
    max_age_days: int | None = None

    @classmethod
    def from_config(cls, s: SearchDefaults) -> "Filters":
        return cls(list(s.keywords), list(s.exclude), list(s.locations), s.remote_only, s.max_age_days)


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9+#.]+", text.lower()))


def _phrase_in(phrase: str, title_words: set[str]) -> bool:
    words = _words(phrase)
    return bool(words) and words <= title_words


def matches(job: RawJob, f: Filters, now: datetime | None = None) -> bool:
    title_words = _words(job.title)
    if f.keywords and not any(_phrase_in(k, title_words) for k in f.keywords):
        return False
    if f.exclude and any(_phrase_in(x, title_words) for x in f.exclude):
        return False
    if f.remote_only and not job.remote:
        return False
    if f.locations:
        loc = job.all_locations_text().lower()
        wants_remote = any(l.strip().lower() == "remote" for l in f.locations)
        others = [l.strip().lower() for l in f.locations if l.strip().lower() != "remote"]
        ok = (wants_remote and job.remote) or any(l and l in loc for l in others)
        if not ok:
            return False
    if f.max_age_days and job.posted_at:
        now = now or datetime.now(timezone.utc)
        if job.posted_at < now - timedelta(days=f.max_age_days):
            return False
    return True


@dataclass
class StoreResult:
    new: list[Job] = field(default_factory=list)
    updated: int = 0


def store(session: Session, raw_jobs: list[RawJob]) -> StoreResult:
    """Insert new jobs, refresh existing ones (keeping your status and scores)."""
    result = StoreResult()
    now = datetime.now(timezone.utc)
    keys = [r.dedupe_key for r in raw_jobs]
    existing = {j.dedupe_key: j for j in session.exec(select(Job).where(Job.dedupe_key.in_(keys)))} if keys else {}
    seen: set[str] = set()
    for r in raw_jobs:
        if r.dedupe_key in seen:
            continue
        seen.add(r.dedupe_key)
        fields = dict(
            source=r.source,
            company_slug=r.company_slug,
            external_id=r.external_id,
            company=r.company,
            title=r.title,
            location=r.all_locations_text() or None,
            remote=r.remote,
            workplace=r.workplace,
            department=r.department,
            employment_type=r.employment_type,
            salary=r.salary,
            url=r.url,
            apply_url=r.apply_url,
            description=r.description[:MAX_DESCRIPTION_CHARS],
            posted_at=r.posted_at,
            last_seen_at=now,
        )
        job = existing.get(r.dedupe_key)
        if job is None:
            job = Job(dedupe_key=r.dedupe_key, found_at=now, **fields)
            session.add(job)
            result.new.append(job)
        else:
            for k, v in fields.items():
                setattr(job, k, v)
            session.add(job)
            result.updated += 1
    session.commit()
    for j in result.new:
        session.refresh(j)
    return result
