"""Oracle Recruiting Cloud (HCM) candidate-experience REST API.

Board slug: ``host/site``, e.g. ``cbdt.fa.us2.oraclecloud.com/CX``. Like Workday, a fetch runs
the search's keywords through the site's own search rather than downloading every requisition.
"""

from __future__ import annotations

import httpx

from .base import Listing, RawJob, SourceError, get_json, html_to_text, looks_remote, parse_dt

NAME = "oracle"
LIMIT = 100
MAX_TERMS = 3


def split_slug(slug: str) -> tuple[str, str]:
    host, _, site = slug.partition("/")
    if not host.endswith(".oraclecloud.com") or not site or "/" in site:
        raise SourceError(f"bad Oracle board '{slug}' (expected host/site)")
    return host, site


def board_url(slug: str) -> str:
    host, site = split_slug(slug)
    return f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}"


def _clean(term: str) -> str:
    # the finder syntax uses , ; and = as separators
    return "".join(c for c in term if c not in ",;=\"").strip()


def fetch(http: httpx.Client, slug: str, query: tuple[str, ...] = ()) -> list[RawJob]:
    host, site = split_slug(slug)
    terms = [_clean(t) for t in query if _clean(t)][:MAX_TERMS] or [""]
    out = Listing()
    out.complete = False
    seen: set[str] = set()
    for term in terms:
        finder = f"findReqs;siteNumber={site},limit={LIMIT},sortBy=POSTING_DATES_DESC"
        if term:
            finder += f",keyword={term}"
        data = get_json(http, f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions",
                        params={"onlyData": "true", "expand": "requisitionList", "finder": finder})
        items = (data or {}).get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise SourceError("Oracle response had no items")
        for item in items[:1]:
            for j in item.get("requisitionList") or []:
                rid = str(j.get("Id") or "")
                if not rid or rid in seen:
                    continue
                seen.add(rid)
                loc = j.get("PrimaryLocation")
                workplace = (j.get("WorkplaceTypeCode") or "").replace("ORA_", "").lower() or None
                out.append(RawJob(
                    source=NAME, company_slug=slug, external_id=rid, company=host.split(".")[0],
                    title=(j.get("Title") or "").strip(), url=f"{board_url(slug)}/job/{rid}",
                    location=loc, remote=workplace == "remote" or looks_remote(loc), workplace=workplace,
                    description=html_to_text(j.get("ShortDescriptionStr")),
                    posted_at=parse_dt(j.get("PostedDate")),
                ))
    return out


def detail(http: httpx.Client, slug: str, external_id: str) -> str:
    host, _ = split_slug(slug)
    d = get_json(http, f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails/{external_id}",
                 params={"onlyData": "true"})
    if isinstance(d, dict) and isinstance(d.get("items"), list):
        d = d["items"][0] if d["items"] else {}
    parts = [html_to_text((d or {}).get(k)) for k in
             ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr", "CorporateDescriptionStr")]
    return "\n\n".join(p for p in parts if p)
