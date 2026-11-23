"""Keryx: an offline list of US internship and new-grad openings.

Keryx (https://github.com/GodlyDonuts/keryx, MIT) aggregates ~3,500 company job boards plus the
SimplifyJobs / Jobright / SpeedyApply lists and republishes them every ~15 minutes as
``data/jobs.json`` and ``data/boards.json``.

How AutoApply uses it, from cheapest to freshest:

1. A compact snapshot of the open jobs and the board list ships inside the package
   (``sources/data/``), so search works offline right after install.
2. ``keryx sync`` (and searches, at most every ``keryx_sync_hours``) downloads the current data
   from GitHub with a conditional request (unchanged data costs nothing) into ``~/.autoapply/keryx``.
3. With ``keryx_live`` on, a search also asks the companies' own job boards directly for the boards
   that matched, so listings aren't up to 15 minutes + sync interval stale. Those calls are capped
   (``keryx_live_max_boards``), cached (``board_cache_minutes``) and rate-limited per host.
"""

from __future__ import annotations

import gzip
import json
import time
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timezone
from functools import lru_cache
from importlib import resources
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .. import config
from . import urls
from .base import USER_AGENT, RawJob, SourceError, looks_remote, parse_dt

NAME = "keryx"
REPO = "https://github.com/GodlyDonuts/keryx"
RAW = "https://raw.githubusercontent.com/GodlyDonuts/keryx/main/data"
MIN_SYNC_INTERVAL_S = 15 * 60  # Keryx itself only refreshes every 15 minutes
JOB_KEYS = ("id", "company", "title", "location", "url", "program", "cycle", "posted_at", "sponsorship")
LIVE_ATS = frozenset({"greenhouse", "lever", "ashby", "smartrecruiters", "workable", "bamboohr", "workday", "oracle"})


# ---------------------------------------------------------------------------
# files
# ---------------------------------------------------------------------------


def local_dir() -> Path:
    return config.home() / "keryx"


def _bundled(name: str):
    return resources.files("autoapply.sources").joinpath("data", name)


def _meta_path() -> Path:
    return local_dir() / "meta.json"


def meta() -> dict:
    try:
        return json.loads(_meta_path().read_text())
    except (FileNotFoundError, ValueError):
        return {}


def _read_gz(path) -> dict:
    with path.open("rb") as fh, gzip.open(fh, "rt", encoding="utf-8") as gz:
        return json.load(gz)


def _write_gz(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as gz:
        json.dump(data, gz, separators=(",", ":"))
    tmp.replace(path)


def _snapshot(name: str) -> dict:
    """The synced copy if there is one, else the copy that ships with AutoApply."""
    local = local_dir() / name
    if local.exists():
        try:
            return _read_gz(local)
        except (OSError, ValueError, EOFError):
            pass  # corrupt download: fall back to the bundled one
    return _read_gz(_bundled(name))


# ---------------------------------------------------------------------------
# converting Keryx's data → compact snapshot
# ---------------------------------------------------------------------------


def board_slug(b: dict) -> tuple[str, str] | None:
    """A Keryx boards.json entry → (ats, slug in AutoApply's format)."""
    ats, slug = b.get("ats"), b.get("slug")
    if not ats or not slug:
        return None
    if ats == "workday":
        return (ats, f"{b['host']}/{slug}/{b['site']}") if b.get("host") and b.get("site") else None
    if ats == "oracle":
        return (ats, f"{b['host']}/{b['site']}") if b.get("host") and b.get("site") else None
    return ats, slug


def compact_boards(raw: dict) -> dict:
    rows = []
    for b in raw.get("boards") or []:
        pair = board_slug(b)
        if pair:
            rows.append([pair[0], pair[1], b.get("company") or ""])
    return {"keys": ["ats", "slug", "company"], "rows": rows}


def compact_jobs(raw: dict) -> dict:
    rows = [[j.get(k) for k in JOB_KEYS] for j in raw.get("jobs") or []
            if j.get("status") == "open" and j.get("url") and j.get("title")]
    return {"keys": list(JOB_KEYS), "rows": rows, "country": raw.get("country")}


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


def _download(http: httpx.Client, name: str, etag: str | None) -> tuple[dict | None, str | None]:
    """Conditional GET of data/<name>. Returns (parsed JSON or None if unchanged, etag)."""
    headers = {"If-None-Match": etag} if etag else {}
    try:
        r = http.get(f"{RAW}/{name}", headers=headers)
    except httpx.HTTPError as e:
        raise SourceError(f"couldn't reach GitHub: {e}") from e
    if r.status_code == 304:
        return None, etag
    if r.status_code != 200:
        raise SourceError(f"HTTP {r.status_code} downloading Keryx {name}")
    try:
        return r.json(), r.headers.get("ETag")
    except ValueError as e:
        raise SourceError(f"Keryx {name} wasn't valid JSON") from e


def sync(force: bool = False, http: httpx.Client | None = None) -> dict:
    """Bring the local Keryx copy up to date. Returns a summary dict.

    Skips the network entirely if the last check was under 15 minutes ago (Keryx can't have
    changed much), unless ``force``.
    """
    m = meta()
    if not force and time.time() - m.get("checked_at", 0) < MIN_SYNC_INTERVAL_S:
        return {"skipped": True, **status()}
    own = http is None
    http = http or httpx.Client(headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
                                timeout=httpx.Timeout(120.0, connect=10.0), follow_redirects=True)
    changed = []
    try:
        for name, out, convert in (("boards.json", "boards.json.gz", compact_boards),
                                   ("jobs.json", "jobs.json.gz", compact_jobs)):
            etag_key = f"etag_{name}"
            have = (local_dir() / out).exists()
            data, etag = _download(http, name, m.get(etag_key) if have and not force else None)
            if data is not None:
                compact = convert(data)
                compact["fetched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
                _write_gz(local_dir() / out, compact)
                changed.append(name)
            m[etag_key] = etag
    finally:
        if own:
            http.close()
    m["checked_at"] = time.time()
    if changed:
        m["synced_at"] = m["checked_at"]
    local_dir().mkdir(parents=True, exist_ok=True)
    _meta_path().write_text(json.dumps(m, indent=1))
    clear_cache()
    return {"skipped": False, "changed": changed, **status()}


def clear_cache() -> None:
    for f in (_load, _boards, _snapshot_time):
        f.cache_clear()


def sync_due(hours: float) -> bool:
    return hours > 0 and time.time() - meta().get("checked_at", 0) > hours * 3600


def status() -> dict:
    local = (local_dir() / "jobs.json.gz").exists()
    data = _load()
    m = meta()
    return {
        "source": "synced" if local else "bundled",
        "jobs": len(data),
        "fetched_at": _snapshot_time(),
        "checked_at": datetime.fromtimestamp(m["checked_at"], timezone.utc) if m.get("checked_at") else None,
        "boards": len(_boards()),
    }


@lru_cache(maxsize=1)
def _snapshot_time() -> str | None:
    return _snapshot("jobs.json.gz").get("fetched_at")


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _boards() -> dict[tuple[str, str], tuple[str, str]]:
    """(ats, lowercased slug) → (slug as Keryx spells it, company name)."""
    data = _snapshot("boards.json.gz")
    return {(ats, slug.lower()): (slug, company) for ats, slug, company in data.get("rows") or []}


def boards() -> list[tuple[str, str]]:
    """Every board Keryx knows, as (ats, slug)."""
    return [(ats, slug) for (ats, _), (slug, _c) in _boards().items()]


def company_name(ats: str, slug: str) -> str | None:
    hit = _boards().get((ats, slug.lower()))
    return hit[1] if hit else None


def _employment(program: str | None, cycle: str | None, sponsorship: str | None) -> str | None:
    kind = {"internship": "Internship", "new-grad": "New grad"}.get(program or "", program)
    when = None if cycle in (None, "", "unscheduled") else cycle.replace("-", " ").title()
    parts = [p for p in (kind, when) if p]
    if sponsorship:
        parts.append(sponsorship.replace("-", " "))
    return " · ".join(parts) or None


def to_raw(row: dict) -> RawJob:
    url = row["url"]
    ref = urls.parse(url)
    if ref and ref.external_id:
        # Same identity as a live fetch of that board, so the two dedupe.
        known = _boards().get((ref.ats, ref.slug.lower()))
        source, slug, ext = ref.ats, known[0] if known else ref.slug, ref.external_id
    else:
        source, slug, ext = NAME, (urlsplit(url).hostname or ""), str(row["id"])
    loc = row.get("location") or None
    return RawJob(
        source=source, company_slug=slug, external_id=ext,
        company=(row.get("company") or "").strip() or slug, title=(row.get("title") or "").strip(),
        url=url, location=loc, remote=looks_remote(loc),
        employment_type=_employment(row.get("program"), row.get("cycle"), row.get("sponsorship")),
        posted_at=_date(row.get("posted_at")),
    )


def _date(value: str | None) -> datetime | None:
    if isinstance(value, str) and len(value) == 10:  # a bare date: midnight UTC, not local time
        value += "T00:00:00+00:00"
    return parse_dt(value)


@lru_cache(maxsize=1)
def _load() -> tuple[RawJob, ...]:
    data = _snapshot("jobs.json.gz")
    keys = data.get("keys") or list(JOB_KEYS)
    out = []
    for values in data.get("rows") or []:
        try:
            out.append(to_raw(dict(zip(keys, values))))
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(out)


def load() -> list[RawJob]:
    return list(_load())


def live_targets(jobs: Iterable[RawJob], limit: int, skip: Iterable[tuple[str, str]] = ()) -> list[tuple[str, str]]:
    """The boards behind these jobs worth asking directly, most matches first, at most ``limit``."""
    skip = set(skip)
    counts = Counter((j.source, j.company_slug) for j in jobs if j.source in LIVE_ATS)
    return [pair for pair, _ in counts.most_common() if pair not in skip][:max(0, limit)]


__all__ = ["LIVE_ATS", "NAME", "REPO", "boards", "company_name", "live_targets", "load", "status", "sync", "sync_due"]
