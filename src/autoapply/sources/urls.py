"""Recognise ATS job/board URLs → (ats, board slug, job id).

Slug formats match the fetchers: plain slugs for Greenhouse/Lever/Ashby/SmartRecruiters/Workable/
BambooHR, ``host/site`` for Oracle and ``host/tenant/site`` for Workday.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

_LOCALE = re.compile(r"^[a-z]{2}(-[a-z]{2})?$", re.I)
_SAFE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_RESERVED = {"embed", "jobs", "job", "v1", "api", "search", "login", "assets", "j"}


@dataclass(frozen=True)
class Ref:
    ats: str
    slug: str
    external_id: str | None = None  # None: a board URL, not a single posting


def _ok(part: str | None) -> bool:
    return bool(part) and bool(_SAFE.match(part)) and part.lower() not in _RESERVED


def parse(url: str) -> Ref | None:
    try:
        u = urlsplit(url.strip())
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.hostname:
        return None
    host = u.hostname.lower()
    parts = [p for p in u.path.split("/") if p]
    low = [p.lower() for p in parts]

    if host.endswith("greenhouse.io"):
        qs = parse_qs(u.query)
        if "for" in qs and _ok(qs["for"][0]):  # boards.greenhouse.io/embed/job_app?for=x&token=1
            return Ref("greenhouse", qs["for"][0], (qs.get("token") or [None])[0])
        if "jobs" in low:
            i = low.index("jobs")
            if i >= 1 and _ok(parts[i - 1]):
                jid = parts[i + 1] if i + 1 < len(parts) and parts[i + 1].isdigit() else None
                return Ref("greenhouse", parts[i - 1], jid or (qs.get("gh_jid") or [None])[0])
        if parts and _ok(parts[0]):
            return Ref("greenhouse", parts[0], (qs.get("gh_jid") or [None])[0])
        return None
    if host == "jobs.lever.co" and parts and _ok(parts[0]):
        return Ref("lever", parts[0], parts[1] if len(parts) > 1 and parts[1] != "apply" else None)
    if host == "jobs.ashbyhq.com" and parts and _ok(parts[0]):
        jid = parts[1] if len(parts) > 1 and parts[1] not in ("application", "embed") else None
        return Ref("ashby", parts[0], jid)
    if host in ("jobs.smartrecruiters.com", "careers.smartrecruiters.com") and parts and _ok(parts[0]):
        jid = None
        if len(parts) > 1:
            m = re.match(r"^(\d+)", parts[1])
            jid = m.group(1) if m else None
        return Ref("smartrecruiters", parts[0], jid)
    if host == "apply.workable.com" and parts and _ok(parts[0]):
        jid = None
        if len(parts) >= 3 and low[1] in ("j", "jobs") and low[2] != "view":
            jid = parts[2]
        elif len(parts) >= 4 and low[1:3] == ["jobs", "view"]:
            jid = parts[3].removesuffix(".md")
        return Ref("workable", parts[0], jid)
    if host.endswith(".bamboohr.com") and "careers" in low:
        slug = host.split(".")[0]
        i = low.index("careers")
        jid = parts[i + 1] if i + 1 < len(parts) and parts[i + 1].isdigit() else None
        return Ref("bamboohr", slug, jid) if _ok(slug) else None
    if host.endswith(".oraclecloud.com") and "sites" in low:
        i = low.index("sites")
        if i + 1 < len(parts) and _ok(parts[i + 1]):
            jid = None
            if "job" in low[i + 2:]:
                j = low.index("job", i + 2)
                jid = parts[j + 1] if j + 1 < len(parts) else None
            return Ref("oracle", f"{host}/{parts[i + 1]}", jid)
        return None
    if host.endswith(".myworkdayjobs.com"):
        tenant = host.split(".")[0]
        cut = low.index("job") if "job" in low else len(parts)
        site = next((p for p in parts[:cut] if not _LOCALE.match(p)), None)
        if not (_ok(site) and _ok(tenant)):
            return None
        jid = "/" + "/".join(parts[cut:]) if cut < len(parts) else None
        return Ref("workday", f"{host}/{tenant}/{site}", jid)
    if host.endswith(".myworkdaysite.com") and len(parts) >= 3 and low[0] == "recruiting":
        tenant, site = parts[1], parts[2]
        jid = "/" + "/".join(parts[3:]) if len(parts) > 3 and "job" in low[3:] else None
        if _ok(tenant) and _ok(site):
            return Ref("workday", f"{host}/{tenant}/{site}", jid)
    return None
