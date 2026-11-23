"""Job sources. Each ATS module exposes NAME and fetch(http, slug) -> list[RawJob]."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from . import ashby, github_lists, greenhouse, lever, remoteok, starter
from .base import NotFound, RawJob, SourceError, client

ATS = {m.NAME: m for m in (greenhouse, lever, ashby)}  # per-company boards
FEEDS = {remoteok.NAME: remoteok, github_lists.NAME: github_lists}  # multi-company feeds (slug is ignored)
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


def fetch_all(companies: list[tuple[str, str]], workers: int = 24,
              progress: Callable[[int, int], None] | None = None) -> FetchReport:
    """Fetch every (ats, slug) in parallel. One company failing doesn't stop the rest.

    progress(done, total) is called as each board finishes.
    """
    report = FetchReport()
    feed_jobs: list[RawJob] = []
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
            report.per_company[key] = len(jobs)
            if ats in FEEDS:
                feed_jobs.extend(jobs)
            else:
                report.jobs.extend(jobs)
                report.fetched_keys.setdefault((ats, slug), set()).update(j.dedupe_key for j in jobs)
    # A feed listing on a board we fetched is a duplicate without the description; keep the board's copy.
    # Listings on boards that failed stand in for them, so those failures aren't worth reporting.
    boards_ok = set(report.fetched_keys)
    for j in feed_jobs:
        if (j.source, j.company_slug) in boards_ok:
            continue
        report.jobs.append(j)
        if report.errors.pop(f"{j.source}:{j.company_slug}", None) is None:
            report.fetched_keys.setdefault((j.source, j.company_slug), set()).add(j.dedupe_key)
        # else: the board failed (maybe briefly), so don't close its saved jobs based on the feed alone
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
    """Everything a search should hit: your companies, the starter list, GitHub-list boards, and feeds."""
    targets = list(cfg.companies.pairs())
    if cfg.use_starter_companies:
        targets += starter.pairs()
    if cfg.use_github_lists:
        try:
            with client() as http:
                targets += github_lists.boards(github_lists.listings(http))
        except SourceError:
            pass  # the feed itself will report the error
        targets.append((github_lists.NAME, "all"))
    if cfg.use_remoteok:
        targets.append((remoteok.NAME, "all"))
    return list(dict.fromkeys(targets))


__all__ = ["ALL", "ATS", "FEEDS", "FetchReport", "search_targets", "NotFound", "RawJob", "SourceError", "detect", "fetch_all", "fetch_company"]
