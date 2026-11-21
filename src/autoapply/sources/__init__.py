"""Job sources. Each ATS module exposes NAME and fetch(http, slug) -> list[RawJob]."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import ashby, greenhouse, lever
from .base import NotFound, RawJob, SourceError, client

ATS = {m.NAME: m for m in (greenhouse, lever, ashby)}


@dataclass
class FetchReport:
    jobs: list[RawJob] = field(default_factory=list)
    per_company: dict[str, int] = field(default_factory=dict)  # "ats:slug" -> job count
    errors: dict[str, str] = field(default_factory=dict)  # "ats:slug" -> message


def fetch_company(ats: str, slug: str, http=None) -> list[RawJob]:
    if ats not in ATS:
        raise SourceError(f"unknown ATS '{ats}' (use one of: {', '.join(ATS)})")
    own = http is None
    http = http or client()
    try:
        return ATS[ats].fetch(http, slug)
    finally:
        if own:
            http.close()


def fetch_all(companies: list[tuple[str, str]], workers: int = 8) -> FetchReport:
    """Fetch every (ats, slug) in parallel. One company failing doesn't stop the rest."""
    report = FetchReport()
    with client() as http, ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_company, ats, slug, http): (ats, slug) for ats, slug in companies}
        for fut, (ats, slug) in futures.items():
            key = f"{ats}:{slug}"
            try:
                jobs = fut.result()
            except NotFound:
                report.errors[key] = "board not found (check the slug)"
                continue
            except SourceError as e:
                report.errors[key] = str(e)
                continue
            report.jobs.extend(jobs)
            report.per_company[key] = len(jobs)
    return report


def detect(slug: str) -> list[tuple[str, int]]:
    """Which ATSs have a board for this slug? Returns [(ats, job_count)]."""
    found: list[tuple[str, int]] = []
    with client() as http, ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(fetch_company, name, slug, http): name for name in ATS}
        for fut, name in futures.items():
            try:
                found.append((name, len(fut.result())))
            except SourceError:
                pass
    return found


__all__ = ["ATS", "FetchReport", "NotFound", "RawJob", "SourceError", "detect", "fetch_all", "fetch_company"]
