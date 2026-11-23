"""SimplifyJobs' community job lists on GitHub (internships + new grad), updated hourly:
https://github.com/SimplifyJobs/Summer2026-Internships and https://github.com/SimplifyJobs/New-Grad-Positions

Each listing links to the company's real application page. Two uses:
- boards(): Greenhouse/Lever/Ashby boards the lists point at. Search fetches those boards directly,
  which gives full descriptions and every other open role at those companies.
- fetch(): every other listing (Workday, iCIMS, company sites, regional ATS hosts...) as a job.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx

from .. import config
from .base import RawJob, SourceError, get_json, looks_remote

NAME = "github"
SLUG = "simplify"  # company_slug for listings that aren't on a board we fetch
LISTS = {
    "internships": "https://raw.githubusercontent.com/SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
    "new grad": "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
}
TTL_SECONDS = 3600  # the lists update about hourly
KEEP = ("id", "company_name", "title", "url", "locations", "date_posted", "category", "terms", "degrees", "sponsorship")

# Hosts whose public board API we fetch (the boards() list). Regional hosts (eu/anz) use other APIs.
BOARD_HOSTS = {
    "job-boards.greenhouse.io": "greenhouse",
    "boards.greenhouse.io": "greenhouse",
    "jobs.lever.co": "lever",
    "jobs.ashbyhq.com": "ashby",
}
SITES = [  # host pattern -> source name for everything else
    (r"(^|\.)greenhouse\.io$", "greenhouse"),
    (r"(^|\.)lever\.co$", "lever"),
    (r"(^|\.)ashbyhq\.com$", "ashby"),
    (r"myworkdayjobs\.com$|(^|\.)myworkdaysite\.com$|(^|\.)workday\.com$", "workday"),
    (r"(^|\.)icims\.com$", "icims"),
    (r"(^|\.)smartrecruiters\.com$", "smartrecruiters"),
    (r"(^|\.)workable\.com$", "workable"),
    (r"(^|\.)jobvite\.com$", "jobvite"),
]


def site_for(url: str) -> str:
    """Which application site a URL is on: greenhouse, lever, ashby, workday, ..., or web."""
    host = (urlparse(url).hostname or "").lower()
    for pat, name in SITES:
        if re.search(pat, host):
            return name
    return "web"


def board_posting(url: str) -> tuple[str, str, str] | None:
    """(ats, slug, job id) for a Greenhouse/Lever/Ashby posting URL, else None."""
    u = urlparse(url)
    host = (u.hostname or "").lower()
    parts = [p for p in u.path.split("/") if p]
    site = site_for(url)
    if site == "greenhouse":
        if len(parts) >= 3 and parts[1] == "jobs" and parts[0] != "embed":
            return "greenhouse", parts[0].lower(), parts[2]
        return None  # embed links carry only a token, not the board slug
    if site in ("lever", "ashby") and host.startswith("jobs.") and len(parts) >= 2:
        return site, parts[0].lower(), parts[1]
    return None


def _cache_path():
    return config.home() / "cache" / "github_lists.json"


def listings(http: httpx.Client, max_age: int = TTL_SECONDS) -> list[dict]:
    """Active listings from every list (cached on disk for an hour)."""
    path = _cache_path()
    try:
        cached = json.loads(path.read_text())
        if time.time() - cached["fetched"] < max_age:
            return cached["listings"]
    except (OSError, ValueError, KeyError, TypeError):
        cached = None
    out: list[dict] = []
    seen: set[str] = set()
    try:
        for name, url in LISTS.items():
            for x in get_json(http, url):
                if not isinstance(x, dict) or not x.get("active") or x.get("is_visible") is False:
                    continue
                if not x.get("url") or x.get("id") in seen:
                    continue
                seen.add(x["id"])
                out.append({k: x.get(k) for k in KEEP} | {"list": name})
    except SourceError:
        if cached:  # stale beats nothing
            return cached["listings"]
        raise
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched": time.time(), "listings": out}))
    return out


def boards(items: list[dict]) -> list[tuple[str, str]]:
    """Greenhouse/Lever/Ashby boards (on hosts whose API we fetch) that have listings."""
    found: dict[tuple[str, str], None] = {}
    for x in items:
        if (urlparse(x["url"]).hostname or "").lower() in BOARD_HOSTS and (p := board_posting(x["url"])):
            found[(p[0], p[1])] = None
    return list(found)


def _description(x: dict) -> str:
    lines = [f"Listed on SimplifyJobs ({x.get('list', 'GitHub')}). The full description is on the posting page."]
    for label, key in (("Category", "category"), ("Terms", "terms"), ("Degrees", "degrees"),
                       ("Sponsorship", "sponsorship")):
        v = x.get(key)
        if isinstance(v, list):
            v = ", ".join(v)
        if v:
            lines.append(f"{label}: {v}")
    return "\n".join(lines)


def to_raw(x: dict) -> RawJob:
    url = x["url"]
    locs = [l for l in x.get("locations") or [] if l]
    posted = None
    if isinstance(x.get("date_posted"), (int, float)):
        posted = datetime.fromtimestamp(x["date_posted"], tz=timezone.utc)
    if (p := board_posting(url)):
        source, slug, ext = p  # same dedupe key as the board fetcher would give
    else:
        source, slug, ext = site_for(url), SLUG, str(x["id"])
    remote = looks_remote(*locs)
    return RawJob(
        source=source,
        company_slug=slug,
        external_id=ext,
        company=(x.get("company_name") or "Unknown").strip(),
        title=(x.get("title") or "").strip(),
        url=url,
        apply_url=url,
        location=locs[0] if locs else None,
        locations=locs[1:],
        remote=remote,
        workplace="remote" if remote else None,
        description=_description(x),
        posted_at=posted,
    )


def fetch(http: httpx.Client, slug: str = "all") -> list[RawJob]:
    """Every listing. fetch_all drops the ones whose board it fetched directly (those have full descriptions)."""
    return [to_raw(x) for x in listings(http)]
