"""BambooHR public careers page JSON (https://<slug>.bamboohr.com/careers/list)."""

from __future__ import annotations

import httpx

from .base import RawJob, SourceError, get_json, html_to_text, looks_remote

NAME = "bamboohr"


def _host(slug: str) -> str:
    if not slug or not all(c.isalnum() or c == "-" for c in slug):
        raise SourceError(f"bad BambooHR board '{slug}'")
    return f"{slug}.bamboohr.com"


def board_url(slug: str) -> str:
    return f"https://{_host(slug)}/careers/"


def fetch(http: httpx.Client, slug: str, query: tuple[str, ...] = ()) -> list[RawJob]:
    data = get_json(http, f"https://{_host(slug)}/careers/list")
    if not isinstance(data, dict) or not isinstance(data.get("result"), list):
        raise SourceError("BambooHR response had no result list")
    jobs: list[RawJob] = []
    for j in data["result"]:
        jid = str(j.get("id") or "")
        if not jid:
            continue
        loc_d = j.get("location") or {}
        loc = ", ".join(str(loc_d[k]) for k in ("city", "state") if loc_d.get(k)) or None
        remote = bool(j.get("isRemote")) or str(j.get("locationType")) == "1" or looks_remote(loc)
        jobs.append(RawJob(
            source=NAME, company_slug=slug, external_id=jid, company=slug,
            title=(j.get("jobOpeningName") or "").strip(), url=f"{board_url(slug)}{jid}",
            location=loc, remote=remote, department=j.get("departmentLabel"),
            employment_type=j.get("employmentStatusLabel"),
        ))
    return jobs


def detail(http: httpx.Client, slug: str, external_id: str) -> str:
    d = get_json(http, f"https://{_host(slug)}/careers/{external_id}/detail")
    opening = ((d or {}).get("result") or {}).get("jobOpening") or {}
    return html_to_text(opening.get("description"))
