"""Remote OK public API (remote jobs from many companies): https://remoteok.com/api

Their API terms ask that Remote OK be credited and linked as the source; job links point
to remoteok.com and listings are labeled "via Remote OK".
"""

from __future__ import annotations

import httpx

from .base import RawJob, get_json, html_to_text, parse_dt

NAME = "remoteok"
URL = "https://remoteok.com/api"


def fetch(http: httpx.Client, slug: str = "all", query: tuple[str, ...] = ()) -> list[RawJob]:
    data = get_json(http, URL)
    jobs: list[RawJob] = []
    for j in data if isinstance(data, list) else []:
        if not isinstance(j, dict) or "position" not in j:
            continue  # first element is the legal notice
        salary = None
        if j.get("salary_min") and j.get("salary_max"):
            salary = f"USD {int(j['salary_min']):,}–{int(j['salary_max']):,}"
        tags = j.get("tags") or []
        desc = html_to_text(j.get("description"))
        if tags:
            desc = f"Tags: {', '.join(tags)}\n\n{desc}"
        jobs.append(
            RawJob(
                source=NAME,
                company_slug="all",
                external_id=str(j.get("id") or j.get("slug")),
                company=(j.get("company") or "Unknown").strip(),
                title=(j.get("position") or "").strip(),
                url=j.get("url") or f"https://remoteok.com/remote-jobs/{j.get('slug')}",
                apply_url=j.get("apply_url"),
                location=(j.get("location") or "Remote").strip() or "Remote",
                remote=True,
                workplace="remote",
                salary=salary,
                description=desc,
                posted_at=parse_dt(j.get("date")),
            )
        )
    return jobs
