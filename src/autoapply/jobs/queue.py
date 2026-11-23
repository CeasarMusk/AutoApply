"""The apply queue: a to-do list of jobs that `queue run` walks through one application at a time.

Jobs come from the last search's matches (remembered by `search`), the shortlist, or every
active job. The queue is kept sorted by the chosen order: best match or most recently posted.
Nothing here submits anything; `queue run` opens each form the same way `apply` does.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlmodel import Session, col, delete, select

from .. import config
from ..models import Job, QueueItem

ORDERS = {
    "match": "Best match first (resume similarity score)",
    "recent": "Most recently posted first",
}
SOURCES = {
    "last": "The matches from my last search",
    "saved": "My shortlist (saved jobs)",
    "all": "Every job I haven't applied to, ignored, or seen close",
}
INACTIVE = ("applied", "ignored", "closed")

_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _last_path():
    return config.home() / "last_search.json"


def remember_results(job_ids: list[int], label: str) -> None:
    """Called by `search`: what it matched, so `queue build` can queue exactly that list."""
    path = _last_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"label": label, "ids": job_ids,
                                "at": datetime.now(timezone.utc).isoformat()}))


def last_results() -> tuple[list[int], str] | None:
    try:
        data = json.loads(_last_path().read_text())
        return [int(i) for i in data["ids"]], str(data.get("label") or "last search")
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _aware(dt: datetime | None) -> datetime:
    if dt is None:
        return _EPOCH
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def sort_key(order: str):
    """Sort key (use with reverse=True). Jobs with no posting date / score go last."""
    if order == "recent":
        return lambda j: (j.posted_at is not None, _aware(j.posted_at), j.match_score or -1, _aware(j.found_at))
    return lambda j: (j.match_score is not None, j.match_score or -1, _aware(j.posted_at))


def sort_jobs(jobs: list[Job], order: str) -> list[Job]:
    if order not in ORDERS:
        raise ValueError(f"Unknown order '{order}'. Use one of: {', '.join(ORDERS)}")
    return sorted(jobs, key=sort_key(order), reverse=True)


def one_per_role(jobs: list[Job]) -> list[Job]:
    """The same role posted in several cities is one application: keep the first (best-sorted) copy."""
    seen: set[tuple[str, str]] = set()
    out = []
    for j in jobs:
        key = (j.company.lower(), " ".join(j.title.lower().split()))
        if key not in seen:
            seen.add(key)
            out.append(j)
    return out


def candidates(s: Session, source: str) -> list[Job]:
    """Active jobs from the chosen source (never applied / ignored / closed ones)."""
    q = select(Job).where(Job.status.not_in(INACTIVE))
    if source == "last":
        last = last_results()
        if not last or not last[0]:
            return []
        q = q.where(col(Job.id).in_(last[0]))
    elif source == "saved":
        q = q.where(Job.status == "saved")
    elif source != "all":
        raise ValueError(f"Unknown source '{source}'. Use one of: {', '.join(SOURCES)}")
    return list(s.exec(q).all())


def items(s: Session) -> list[tuple[QueueItem, Job]]:
    rows = s.exec(select(QueueItem, Job).where(QueueItem.job_id == Job.id).order_by(QueueItem.position)).all()
    return [(qi, j) for qi, j in rows]


def prune(s: Session) -> int:
    """Drop queued jobs that were applied to, ignored, or closed since they were queued."""
    gone = [qi for qi, j in items(s) if j.status in INACTIVE]
    for qi in gone:
        s.delete(qi)
    if gone:
        s.commit()
    return len(gone)


def resort(s: Session, order: str) -> None:
    pairs = items(s)
    by_job = {j.id: qi for qi, j in pairs}
    for pos, j in enumerate(sort_jobs([j for _, j in pairs], order)):
        by_job[j.id].position = pos
        s.add(by_job[j.id])
    s.commit()


def add(s: Session, jobs: list[Job], order: str, *, replace: bool = False, min_match: int | None = None,
        limit: int | None = None) -> tuple[int, int]:
    """Queue jobs (sorted by `order`, one per role). Returns (added, already queued)."""
    if replace:
        clear(s)
    jobs = [j for j in jobs if j.status not in INACTIVE]
    if min_match is not None:
        jobs = [j for j in jobs if (j.match_score or 0) >= min_match]
    jobs = one_per_role(sort_jobs(jobs, order))
    queued = {qi.job_id for qi, _ in items(s)}
    queued_roles = {(j.company.lower(), " ".join(j.title.lower().split())) for _, j in items(s)}
    added = already = 0
    for j in jobs:
        if limit is not None and added >= limit:
            break
        role = (j.company.lower(), " ".join(j.title.lower().split()))
        if j.id in queued or role in queued_roles:
            already += 1
            continue
        s.add(QueueItem(job_id=j.id, position=10**9 + added))
        queued_roles.add(role)
        added += 1
    s.commit()
    resort(s, order)
    return added, already


def remove(s: Session, job_ids: list[int]) -> int:
    rows = s.exec(select(QueueItem).where(col(QueueItem.job_id).in_(job_ids))).all()
    for qi in rows:
        s.delete(qi)
    s.commit()
    return len(rows)


def clear(s: Session) -> int:
    n = len(items(s))
    s.exec(delete(QueueItem))
    s.commit()
    return n
