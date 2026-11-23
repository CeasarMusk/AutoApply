"""Workday public career-site API (the JSON the career site itself calls).

Board slug: ``host/tenant/site``, e.g. ``clarios.wd5.myworkdayjobs.com/clarios/clarioscareers``.
Workday has no "list everything" call that's cheap, so a fetch runs the search's keywords through
the site's own search box (a few pages each) instead of paging through thousands of postings.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone

import httpx

from .base import Listing, RawJob, SourceError, get_json, html_to_text, looks_remote, post_json

NAME = "workday"
PAGE = 20
MAX_PAGES = 3  # per search term
MAX_TERMS = 3


def split_slug(slug: str) -> tuple[str, str, str]:
    parts = slug.split("/")
    if len(parts) != 3 or not all(parts) or not parts[0].endswith((".myworkdayjobs.com", ".myworkdaysite.com")):
        raise SourceError(f"bad Workday board '{slug}' (expected host/tenant/site)")
    return parts[0], parts[1], parts[2]


def board_url(slug: str) -> str:
    host, _, site = split_slug(slug)
    return f"https://{host}/{site}"


def _api(slug: str) -> str:
    host, tenant, site = split_slug(slug)
    return f"https://{host}/wday/cxs/{tenant}/{site}"


def posted_on(text: str | None, now: datetime | None = None) -> datetime | None:
    """'Posted Today' / 'Posted Yesterday' / 'Posted 3 Days Ago' / 'Posted 30+ Days Ago'."""
    if not text:
        return None
    now = now or datetime.now(timezone.utc)
    t = text.lower()
    if "today" in t:
        return now
    if "yesterday" in t:
        return now - timedelta(days=1)
    m = re.search(r"(\d+)\+?\s+days?", t)
    return now - timedelta(days=int(m.group(1))) if m else None


def fetch(http: httpx.Client, slug: str, query: tuple[str, ...] = ()) -> list[RawJob]:
    host, tenant, _ = split_slug(slug)
    api = _api(slug)
    headers = {"Accept": "application/json", "Origin": f"https://{host}", "Referer": board_url(slug)}
    terms = [t for t in query if t.strip()][:MAX_TERMS] or [""]
    out = Listing()
    out.complete = False  # always a search, never the whole board
    seen: set[str] = set()
    for term in terms:
        for page in range(MAX_PAGES):
            data = post_json(http, f"{api}/jobs", {"appliedFacets": {}, "limit": PAGE,
                                                   "offset": page * PAGE, "searchText": term},
                             headers=headers)
            postings = data.get("jobPostings") if isinstance(data, dict) else None
            if not isinstance(postings, list):
                raise SourceError("Workday response had no jobPostings")
            for j in postings:
                path = j.get("externalPath") or ""
                if not path or path in seen:
                    continue
                seen.add(path)
                loc = j.get("locationsText")
                out.append(RawJob(
                    source=NAME, company_slug=slug, external_id=path,
                    company=tenant, title=(j.get("title") or "").strip(),
                    url=board_url(slug) + path, location=loc, remote=looks_remote(loc),
                    posted_at=posted_on(j.get("postedOn")),
                ))
            total = data.get("total")
            if len(postings) < PAGE or (isinstance(total, int) and (page + 1) * PAGE >= total):
                break
    return out


def detail(http: httpx.Client, slug: str, external_id: str) -> str:
    data = get_json(http, f"{_api(slug)}{external_id}")
    info = (data or {}).get("jobPostingInfo") or {}
    text = html_to_text(info.get("jobDescription"))
    # Some tenants double-escape entities (a literal "&#xa;" survives the HTML parse).
    text = html.unescape(text).replace("\r", "")
    return re.sub(r"\n{3,}", "\n\n", text).strip()
