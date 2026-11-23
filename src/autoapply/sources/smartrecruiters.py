"""SmartRecruiters public postings API: https://developers.smartrecruiters.com/docs/posting-api"""

from __future__ import annotations

import httpx

from .base import Listing, NotFound, RawJob, get_json, html_to_text, looks_remote, parse_dt

NAME = "smartrecruiters"
BASE = "https://api.smartrecruiters.com/v1/companies"
LIMIT = 100
MAX_PAGES = 5  # some boards (big employers) list thousands of postings
MAX_TERMS = 3


def board_url(slug: str) -> str:
    return f"https://jobs.smartrecruiters.com/{slug}"


def _location(loc: dict) -> str | None:
    if loc.get("fullLocation"):
        return loc["fullLocation"]
    return ", ".join(str(loc[k]) for k in ("city", "region", "country") if loc.get(k)) or None


def _job(slug: str, j: dict) -> RawJob:
    loc = j.get("location") or {}
    where = _location(loc)
    workplace = "remote" if loc.get("remote") else "hybrid" if loc.get("hybrid") else None
    return RawJob(
        source=NAME, company_slug=slug, external_id=str(j["id"]),
        company=(j.get("company") or {}).get("name") or slug,
        title=(j.get("name") or "").strip(), url=f"{board_url(slug)}/{j['id']}",
        location=where, remote=bool(loc.get("remote")) or looks_remote(where), workplace=workplace,
        department=(j.get("department") or {}).get("label") or (j.get("function") or {}).get("label"),
        employment_type=(j.get("typeOfEmployment") or {}).get("label"),
        posted_at=parse_dt(j.get("releasedDate")),
    )


def fetch(http: httpx.Client, slug: str, query: tuple[str, ...] = ()) -> list[RawJob]:
    out = Listing()
    seen: set[str] = set()
    terms = [t for t in query if t.strip()][:MAX_TERMS] or [None]
    out.complete = terms == [None]
    for term in terms:
        for page in range(MAX_PAGES):
            params = {"limit": LIMIT, "offset": page * LIMIT}
            if term:
                params["q"] = term
            data = get_json(http, f"{BASE}/{slug}/postings", params=params)
            content = data.get("content") or []
            if page == 0 and term is None and not content and not data.get("totalFound"):
                raise NotFound(f"{BASE}/{slug}/postings")  # unknown companies answer 200 + nothing
            for j in content:
                if str(j.get("id")) not in seen:
                    seen.add(str(j.get("id")))
                    out.append(_job(slug, j))
            total = data.get("totalFound")
            if len(content) < LIMIT or (isinstance(total, int) and (page + 1) * LIMIT >= total):
                break
        else:
            out.complete = False
    return out


def detail(http: httpx.Client, slug: str, external_id: str) -> str:
    d = get_json(http, f"{BASE}/{slug}/postings/{external_id}")
    sections = ((d or {}).get("jobAd") or {}).get("sections") or {}
    parts = []
    for key in ("jobDescription", "qualifications", "additionalInformation", "companyDescription"):
        sec = sections.get(key) or {}
        text = html_to_text(sec.get("text"))
        if text:
            parts.append(f"{sec.get('title') or ''}\n{text}".strip())
    return "\n\n".join(parts)
