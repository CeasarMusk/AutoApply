"""Lever public postings API: https://github.com/lever/postings-api"""

from __future__ import annotations

import httpx

from .base import RawJob, get_json, html_to_text, looks_remote, parse_dt, pretty_slug

NAME = "lever"
BASE = "https://api.lever.co/v0/postings"


def _description(j: dict) -> str:
    parts = [j.get("descriptionPlain") or ""]
    for lst in j.get("lists") or []:
        items = lst.get("content") or ""
        # list content is HTML <li> items
        parts.append(f"{lst.get('text', '')}\n{html_to_text(items)}")
    parts.append(j.get("additionalPlain") or "")
    return "\n\n".join(p.strip() for p in parts if p and p.strip())


def fetch(http: httpx.Client, slug: str) -> list[RawJob]:
    data = get_json(http, f"{BASE}/{slug}", params={"mode": "json"})
    jobs: list[RawJob] = []
    for j in data if isinstance(data, list) else []:
        cats = j.get("categories") or {}
        loc = cats.get("location")
        all_locs = cats.get("allLocations") or []
        workplace = (j.get("workplaceType") or "").lower() or None
        salary = None
        rng = j.get("salaryRange") or {}
        if rng.get("min") and rng.get("max"):
            salary = f"{rng.get('currency', '')} {rng['min']:,}–{rng['max']:,} {rng.get('interval', '')}".strip()
        jobs.append(
            RawJob(
                source=NAME,
                company_slug=slug,
                external_id=str(j["id"]),
                company=pretty_slug(slug),
                title=(j.get("text") or "").strip(),
                url=j.get("hostedUrl") or f"https://jobs.lever.co/{slug}/{j['id']}",
                apply_url=j.get("applyUrl"),
                location=loc,
                locations=[l for l in all_locs if l != loc],
                remote=workplace == "remote" or looks_remote(loc, *all_locs),
                workplace=workplace,
                department=cats.get("team"),
                employment_type=cats.get("commitment"),
                salary=salary,
                description=_description(j),
                posted_at=parse_dt(j.get("createdAt")),
            )
        )
    return jobs
