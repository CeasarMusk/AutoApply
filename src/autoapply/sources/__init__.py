"""Job sources. Each ATS module exposes NAME and fetch(http, slug) -> list[RawJob]."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from . import ashby, greenhouse, lever, remoteok, starter
from .base import NotFound, RawJob, SourceError, client

ATS = {m.NAME: m for m in (greenhouse, lever, ashby)}  # per-company boards
FEEDS = {remoteok.NAME: remoteok}  # multi-company feeds (slug is ignored)
ALL = {**ATS, **FEEDS}


@dataclass
class FetchReport:
    jobs: list[RawJob] = field(default_factory=list)
    per_company: dict[str, int] = field(default_factory=dict)  # "ats:slug" -> job count
    errors: dict[str, str] = field(default_factory=dict)  # "ats:slug" -> message
    fetched_keys: dict[tuple[str, str], set[str]] = field(default_factory=dict)  # boards fetched OK


def fetch_company(ats: str, slug: str, http=None) -> list[RawJob]:
    if ats not in ALL:
        raise SourceError(f"unknown source '{ats}' (use one of: {', '.join(ALL)})")
    own = http is None
    http = http or client()
    try:
        return ALL[ats].fetch(http, slug)
    finally:
        if own:
            http.close()


def fetch_all(companies: list[tuple[str, str]], workers: int = 12,
              progress: Callable[[int, int], None] | None = None) -> FetchReport:
    """Fetch every (ats, slug) in parallel. One company failing doesn't stop the rest.

    progress(done, total) is called as each board finishes.
    """
    report = FetchReport()
    with client() as http, ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fetch_company, ats, slug, http): (ats, slug) for ats, slug in companies}
        for done, fut in enumerate(as_completed(futures), 1):
            if progress:
                progress(done, len(futures))
            ats, slug = futures[fut]
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
            report.fetched_keys[(ats, slug)] = {j.dedupe_key for j in jobs}
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


def search_targets(cfg) -> list[tuple[str, str]]:
    """Everything a search should hit: your companies, the starter list, and feeds."""
    targets = list(cfg.companies.pairs())
    if cfg.use_starter_companies:
        targets += starter.pairs()
    if cfg.use_remoteok:
        targets.append((remoteok.NAME, "all"))
    return list(dict.fromkeys(targets))


__all__ = ["ALL", "ATS", "FEEDS", "FetchReport", "search_targets", "NotFound", "RawJob", "SourceError", "detect", "fetch_all", "fetch_company"]
