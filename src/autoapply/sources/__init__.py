"""Job sources. Each ATS module exposes NAME, fetch(http, slug, query=()) -> list[RawJob],
board_url(slug) (except feeds) and detail(http, slug, external_id) -> description text."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from .. import config
from . import ashby, bamboohr, greenhouse, keryx, lever, oracle, remoteok, smartrecruiters, starter, urls, workable, workday
from .base import Listing, NotFound, RawJob, SourceError, client, parse_dt

ATS = {m.NAME: m for m in (greenhouse, lever, ashby, smartrecruiters, workable, bamboohr, workday, oracle)}
FEEDS = {remoteok.NAME: remoteok}  # multi-company feeds (slug is ignored)
ALL = {**ATS, **FEEDS}
# Boards whose slug can be guessed from a company name (the rest need a URL: `companies add <url>`).
DETECTABLE = ("greenhouse", "lever", "ashby")
# Shown when there's no nicer company name than the slug.
_SLUG_NAMED = {"workday", "oracle", "bamboohr"}


@dataclass
class FetchReport:
    jobs: list[RawJob] = field(default_factory=list)
    per_company: dict[str, int] = field(default_factory=dict)  # "ats:slug" -> job count
    errors: dict[str, str] = field(default_factory=dict)  # "ats:slug" -> message
    # Boards whose whole listing came back, so saved jobs missing from it can be marked closed.
    fetched_keys: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    cached: int = 0  # boards answered from the local cache


# ---------------------------------------------------------------------------
# per-board cache, so repeated searches don't re-ask the same boards
# ---------------------------------------------------------------------------


def _cache_dir():
    return config.home() / "cache" / "boards"


def _cache_file(ats: str, slug: str, query: tuple[str, ...]):
    key = hashlib.sha1(json.dumps([ats, slug, list(query)]).encode()).hexdigest()
    return _cache_dir() / f"{key}.json"


def _cache_get(ats: str, slug: str, query: tuple[str, ...], ttl_s: float) -> Listing | None:
    if ttl_s <= 0:
        return None
    path = _cache_file(ats, slug, query)
    try:
        data = json.loads(path.read_text())
    except (FileNotFoundError, ValueError):
        return None
    if time.time() - data.get("at", 0) > ttl_s:
        return None
    out = Listing()
    out.complete = bool(data.get("complete"))
    for j in data.get("jobs") or []:
        j["posted_at"] = parse_dt(j.get("posted_at"))
        out.append(RawJob(**j))
    return out


def _cache_put(ats: str, slug: str, query: tuple[str, ...], jobs: list[RawJob]) -> None:
    rows = []
    for j in jobs:
        d = dataclasses.asdict(j)
        d["posted_at"] = j.posted_at.isoformat() if j.posted_at else None
        rows.append(d)
    path = _cache_file(ats, slug, query)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"at": time.time(), "complete": getattr(jobs, "complete", True), "jobs": rows}))
    except OSError:
        pass


def prune_cache(max_age_s: float = 2 * 86400) -> int:
    n = 0
    for p in _cache_dir().glob("*.json") if _cache_dir().exists() else []:
        try:
            if time.time() - p.stat().st_mtime > max_age_s:
                p.unlink()
                n += 1
        except OSError:
            pass
    return n


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------


def _uses_query(ats: str) -> bool:
    return ats in ("workday", "oracle", "smartrecruiters")


def fetch_company(ats: str, slug: str, http=None, query: tuple[str, ...] = ()) -> list[RawJob]:
    if ats not in ALL:
        raise SourceError(f"unknown source '{ats}' (use one of: {', '.join(ALL)})")
    own = http is None
    http = http or client()
    try:
        jobs = ALL[ats].fetch(http, slug, query=query if _uses_query(ats) else ())
    finally:
        if own:
            http.close()
    if ats in _SLUG_NAMED or ats == "smartrecruiters":
        name = keryx.company_name(ats, slug)
        if name:
            for j in jobs:
                if ats in _SLUG_NAMED or j.company == slug:
                    j.company = name
    return jobs


def fetch_all(companies: list[tuple[str, str]], workers: int = 8,
              progress: Callable[[int, int], None] | None = None,
              query: tuple[str, ...] = (), cache_minutes: float = 0) -> FetchReport:
    """Fetch every (ats, slug) in parallel. One company failing doesn't stop the rest.

    Requests are spaced per host (see base.polite), and with cache_minutes > 0 a board fetched
    that recently is answered from the local cache instead. progress(done, total) is called as
    each board finishes.
    """
    report = FetchReport()
    ttl = cache_minutes * 60

    def one(ats: str, slug: str, http) -> tuple[list[RawJob], bool]:
        q = tuple(query) if _uses_query(ats) else ()
        hit = _cache_get(ats, slug, q, ttl)
        if hit is not None:
            return hit, True
        jobs = fetch_company(ats, slug, http, query=q)
        if ttl > 0:
            _cache_put(ats, slug, q, jobs)
        return jobs, False

    with client() as http, ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, ats, slug, http): (ats, slug) for ats, slug in companies}
        for done, fut in enumerate(as_completed(futures), 1):
            if progress:
                progress(done, len(futures))
            ats, slug = futures[fut]
            key = f"{ats}:{slug}"
            try:
                jobs, cached = fut.result()
            except NotFound:
                report.errors[key] = "board not found (check the slug)"
                continue
            except SourceError as e:
                report.errors[key] = str(e)
                continue
            report.cached += cached
            report.jobs.extend(jobs)
            report.per_company[key] = len(jobs)
            if getattr(jobs, "complete", True):
                report.fetched_keys[(ats, slug)] = {j.dedupe_key for j in jobs}
    return report


def fetch_description(source: str, slug: str, external_id: str, http=None) -> str:
    """The full posting text for one job (Keryx listings and Workday searches come without it)."""
    mod = ATS.get(source)
    if mod is None or not hasattr(mod, "detail"):
        raise SourceError(f"can't fetch descriptions from {source}")
    own = http is None
    http = http or client()
    try:
        return mod.detail(http, slug, external_id)
    finally:
        if own:
            http.close()


def detect(slug: str) -> list[tuple[str, int]]:
    """Which ATSs have a board for this slug? Returns [(ats, job_count)]."""
    found: list[tuple[str, int]] = []
    with client() as http, ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(fetch_company, name, slug, http): name for name in DETECTABLE}
        for fut, name in futures.items():
            try:
                found.append((name, len(fut.result())))
            except SourceError:
                pass
    return found


def search_targets(cfg) -> list[tuple[str, str]]:
    """Boards a search asks directly: your companies, the starter list, and feeds.

    Keryx's offline list isn't here: it's read from disk, and the boards behind its matches are
    added per search (see keryx.live_targets).
    """
    targets = list(cfg.companies.pairs())
    if cfg.use_starter_companies:
        targets += starter.pairs()
    if cfg.use_remoteok:
        targets.append((remoteok.NAME, "all"))
    return list(dict.fromkeys(targets))


__all__ = ["ALL", "ATS", "DETECTABLE", "FEEDS", "FetchReport", "Listing", "NotFound", "RawJob", "SourceError",
           "detect", "fetch_all", "fetch_company", "fetch_description", "keryx", "prune_cache", "search_targets", "urls"]
