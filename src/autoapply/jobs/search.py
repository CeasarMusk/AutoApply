"""Filter fetched jobs against search settings and store the matches."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from sqlmodel import Session, select

from ..config import SearchDefaults
from ..models import Job
from ..sources import RawJob

MAX_DESCRIPTION_CHARS = 20_000

_SUFFIXES = ("ments", "ment", "ings", "ing", "ers", "er", "s")


@lru_cache(maxsize=20_000)
def stem(word: str) -> str:
    """Crude stemmer so engineer/engineers/engineering and developer/development match."""
    changed = True
    while changed:
        changed = False
        for suf in _SUFFIXES:
            if word.endswith(suf) and len(word) - len(suf) >= 4:
                word = word[: -len(suf)]
                changed = True
                break
    return word


_TOKEN = re.compile(r"[a-z0-9]+(?:\+\+|#)?")  # keeps c++ / c# intact, but "staff+" -> "staff"


def stem_words(text: str) -> set[str]:
    return {stem(w) for w in _TOKEN.findall(text.lower())}


@dataclass
class Filters:
    keywords: list[str] = field(default_factory=list)  # any phrase; all words of a phrase must be in the title
    exclude: list[str] = field(default_factory=list)  # drop titles containing any of these
    require: list[str] = field(default_factory=list)  # title must contain one of these (e.g. intern)
    locations: list[str] = field(default_factory=list)  # whole-word match on location text; "Remote" = remote jobs
    remote_only: bool = False
    max_age_days: int | None = None

    @classmethod
    def from_config(cls, s: SearchDefaults) -> "Filters":
        return cls(list(s.keywords), list(s.exclude), [], list(s.locations), s.remote_only, s.max_age_days)

    def is_empty(self) -> bool:
        return not any([self.keywords, self.exclude, self.require, self.locations, self.remote_only, self.max_age_days])


def _phrase_in(phrase: str, title_words: set[str]) -> bool:
    words = stem_words(phrase)
    return bool(words) and words <= title_words


def _loc_match(needle: str, haystack: str) -> bool:
    if needle == US:
        return is_us(haystack)
    return re.search(rf"(?<![A-Za-z]){re.escape(needle)}(?![A-Za-z])", haystack, re.IGNORECASE) is not None


US = "@US"  # special location token: anywhere in the United States

_US_STATES = (
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND "
    "OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
).split()
_US_NAMES = (
    "United States|USA|U\\.S\\.A?\\.?|America|Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|"
    "Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|"
    "Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|"
    "New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|"
    "South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|"
    "San Francisco|Seattle|Austin|Boston|Chicago|Los Angeles|Denver|Atlanta|Portland|San Jose|Mountain View|"
    "Palo Alto|Sunnyvale|Menlo Park|Redwood City|Pittsburgh|Miami|Dallas|Houston|Phoenix|San Diego|"
    "Salt Lake City|Raleigh|Philadelphia|Minneapolis|Detroit|Bellevue|Irvine|Santa Clara|Brooklyn|Nashville|"
    "Columbus|Boulder|Foster City|San Mateo|Kirkland|Hawthorne|Culver City|Santa Monica|Oakland|Berkeley"
)
_US_RE = re.compile(
    rf"(?<![A-Za-z])(?:{_US_NAMES})(?![A-Za-z])|\bUS\b|,\s*(?:{'|'.join(_US_STATES)})\b",
)
_NOT_US_RE = re.compile(r"\b(Canada|UK|United Kingdom|Europe|EMEA|APAC|LATAM|India|Germany|Australia)\b", re.I)


def is_us(location: str) -> bool:
    """Best-effort: does this location text include a US place? ('Remote (Canada)' -> False)"""
    for part in re.split(r"\s*[|;/]\s*", location):
        if _US_RE.search(part) and not (_NOT_US_RE.search(part) and not re.search(r"\bUS\b|United States", part)):
            return True
    return False


def matches(job: RawJob, f: Filters, now: datetime | None = None) -> bool:
    title_words = stem_words(job.title)
    if f.keywords and not any(_phrase_in(k, title_words) for k in f.keywords):
        return False
    if f.require and not any(_phrase_in(k, title_words) for k in f.require):
        return False
    if f.exclude and any(_phrase_in(x, title_words) for x in f.exclude):
        return False
    if f.remote_only and not job.remote:
        return False
    if f.locations:
        loc = job.all_locations_text()
        wants_remote = any(l.strip().lower() == "remote" for l in f.locations)
        others = [l.strip() for l in f.locations if l.strip() and l.strip().lower() != "remote"]
        if not ((wants_remote and job.remote) or any(_loc_match(l, loc) for l in others)):
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
    closed: int = 0


def store(session: Session, raw_jobs: list[RawJob], scores: dict[str, tuple[int, list[str]]] | None = None,
          fetched_keys: dict[tuple[str, str], set[str]] | None = None) -> StoreResult:
    """Insert new jobs, refresh existing ones (keeping your status), and close vanished ones.

    scores: dedupe_key -> (match score, matched skills)
    fetched_keys: (source, company_slug) -> every dedupe key that board returned this time.
        Saved jobs from those boards that are no longer listed get status "closed".
    """
    scores = scores or {}
    result = StoreResult()
    now = datetime.now(timezone.utc)
    keys = [r.dedupe_key for r in raw_jobs]
    existing = {j.dedupe_key: j for j in session.exec(select(Job).where(Job.dedupe_key.in_(keys)))} if keys else {}
    seen: set[str] = set()
    for r in raw_jobs:
        if r.dedupe_key in seen:
            continue
        seen.add(r.dedupe_key)
        match_score, skills = scores.get(r.dedupe_key, (None, []))
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
            match_score=match_score,
            matched_skills=", ".join(skills) or None,
        )
        job = existing.get(r.dedupe_key)
        if job is not None:
            # Listings without a description (Keryx, Workday searches) mustn't wipe one fetched earlier.
            if not r.description and job.description:
                fields.pop("description")
            if r.posted_at is None:
                fields.pop("posted_at")
        if job is None:
            job = Job(dedupe_key=r.dedupe_key, found_at=now, **fields)
            session.add(job)
            result.new.append(job)
        else:
            if job.status == "closed":
                job.status = "seen"
            for k, v in fields.items():
                setattr(job, k, v)
            session.add(job)
            result.updated += 1

    for (source, slug), live in (fetched_keys or {}).items():
        stale = session.exec(
            select(Job).where(Job.source == source, Job.company_slug == slug, Job.status.not_in(["closed", "applied"]))
        ).all()
        for job in stale:
            if job.dedupe_key not in live:
                job.status = "closed"
                session.add(job)
                result.closed += 1

    session.commit()
    for j in result.new:
        session.refresh(j)
    return result
