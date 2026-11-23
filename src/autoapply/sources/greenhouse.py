"""Greenhouse public job board API: https://developers.greenhouse.io/job-board.html"""

from __future__ import annotations

import httpx

from .base import RawJob, get_json, html_to_text, looks_remote, parse_dt, pretty_slug

NAME = "greenhouse"
BASE = "https://boards-api.greenhouse.io/v1/boards"


def fetch(http: httpx.Client, slug: str, query: tuple[str, ...] = ()) -> list[RawJob]:
    data = get_json(http, f"{BASE}/{slug}/jobs", params={"content": "true"})
    jobs: list[RawJob] = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name")
        offices = [o.get("name") for o in j.get("offices") or [] if o.get("name")]
        depts = [d.get("name") for d in j.get("departments") or [] if d.get("name")]
        jobs.append(
            RawJob(
                source=NAME,
                company_slug=slug,
                external_id=str(j["id"]),
                company=j.get("company_name") or pretty_slug(slug),
                title=(j.get("title") or "").strip(),
                url=j.get("absolute_url") or f"https://boards.greenhouse.io/{slug}/jobs/{j['id']}",
                apply_url=j.get("absolute_url"),
                # Office names are often broad regions ("US"); only use them when there's no location.
                location=loc or (", ".join(offices) or None),
                remote=looks_remote(loc, *offices),
                department=depts[0] if depts else None,
                description=html_to_text(j.get("content"), escaped=True),
                posted_at=parse_dt(j.get("first_published") or j.get("updated_at")),
            )
        )
    return jobs


def detail(http: httpx.Client, slug: str, external_id: str) -> str:
    j = get_json(http, f"{BASE}/{slug}/jobs/{external_id}")
    return html_to_text((j or {}).get("content"), escaped=True)
