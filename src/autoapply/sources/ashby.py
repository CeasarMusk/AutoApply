"""Ashby public job board API: https://developers.ashbyhq.com/docs/public-job-posting-api"""

from __future__ import annotations

import httpx

from .base import RawJob, get_json, html_to_text, looks_remote, parse_dt, pretty_slug

NAME = "ashby"
BASE = "https://api.ashbyhq.com/posting-api/job-board"


def fetch(http: httpx.Client, slug: str) -> list[RawJob]:
    data = get_json(http, f"{BASE}/{slug}", params={"includeCompensation": "true"})
    jobs: list[RawJob] = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        loc = j.get("location")
        secondary = [s.get("location") for s in j.get("secondaryLocations") or [] if s.get("location")]
        workplace = (j.get("workplaceType") or "").lower() or None
        comp = j.get("compensation") or {}
        jobs.append(
            RawJob(
                source=NAME,
                company_slug=slug,
                external_id=str(j["id"]),
                company=pretty_slug(slug),
                title=(j.get("title") or "").strip(),
                url=j.get("jobUrl") or f"https://jobs.ashbyhq.com/{slug}/{j['id']}",
                apply_url=j.get("applyUrl"),
                location=loc,
                locations=secondary,
                remote=bool(j.get("isRemote")) or workplace == "remote" or looks_remote(loc, *secondary),
                workplace=workplace,
                department=j.get("department") or j.get("team"),
                employment_type=j.get("employmentType"),
                salary=comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary"),
                description=(j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml"))).strip(),
                posted_at=parse_dt(j.get("publishedAt")),
            )
        )
    return jobs
