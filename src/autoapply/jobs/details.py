"""Fill in a job's description on demand.

Keryx listings and Workday/Oracle searches arrive with a title and link but no posting text.
Tailoring, essays and `job show` need it, so it's fetched once (one request) and saved.
"""

from __future__ import annotations

from .. import db, sources
from ..models import Job
from .search import MAX_DESCRIPTION_CHARS

MIN_CHARS = 200  # shorter than this is a stub (e.g. Oracle's short description), worth replacing


def needs_description(job: Job) -> bool:
    return len(job.description or "") < MIN_CHARS and job.source in sources.ATS


def ensure_description(job: Job) -> tuple[Job, str | None]:
    """Fetch and save the description if it's missing. Returns (job, error message or None)."""
    if not needs_description(job):
        return job, None
    try:
        text = sources.fetch_description(job.source, job.company_slug, job.external_id)
    except sources.SourceError as e:
        return job, str(e)
    if len(text) <= len(job.description or ""):
        return job, None
    with db.session() as s:
        row = s.get(Job, job.id)
        if row is None:
            return job, None
        row.description = text[:MAX_DESCRIPTION_CHARS]
        s.add(row)
        s.commit()
        s.refresh(row)
        return row, None
