"""Workable public job widget API (https://apply.workable.com/api/v1/widget/accounts/<slug>)."""

from __future__ import annotations

import httpx

from .base import RawJob, get_json, html_to_text, parse_dt

NAME = "workable"


def board_url(slug: str) -> str:
    return f"https://apply.workable.com/{slug}/"


def fetch(http: httpx.Client, slug: str, query: tuple[str, ...] = ()) -> list[RawJob]:
    data = get_json(http, f"https://apply.workable.com/api/v1/widget/accounts/{slug}")
    company = (data or {}).get("name") or slug
    jobs: list[RawJob] = []
    for j in (data or {}).get("jobs") or []:
        code = j.get("shortcode")
        if not code:
            continue
        loc = ", ".join(x for x in (j.get("city"), j.get("state"), j.get("country")) if x) or None
        remote = bool(j.get("telecommuting"))
        jobs.append(RawJob(
            source=NAME, company_slug=slug, external_id=code, company=company,
            title=(j.get("title") or "").strip(), url=f"https://apply.workable.com/{slug}/j/{code}/",
            apply_url=j.get("application_url"), location=loc, remote=remote,
            workplace="remote" if remote else None, department=j.get("department") or j.get("function"),
            employment_type=j.get("employment_type"),
            posted_at=parse_dt(j.get("published_on") or j.get("created_at")),
        ))
    return jobs


def detail(http: httpx.Client, slug: str, external_id: str) -> str:
    d = get_json(http, f"https://apply.workable.com/api/v2/accounts/{slug}/jobs/{external_id}") or {}
    parts = [html_to_text(d.get(k)) for k in ("description", "requirements", "benefits")]
    return "\n\n".join(p for p in parts if p)
