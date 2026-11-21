"""Shared types and helpers for job sources."""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser

import httpx

USER_AGENT = "AutoApply/0.1 (personal job search CLI)"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class SourceError(RuntimeError):
    pass


class NotFound(SourceError):
    """The company slug doesn't exist on this ATS."""


@dataclass
class RawJob:
    source: str  # greenhouse / lever / ashby
    company_slug: str
    external_id: str
    company: str
    title: str
    url: str
    apply_url: str | None = None
    location: str | None = None
    locations: list[str] = field(default_factory=list)
    remote: bool = False
    workplace: str | None = None  # remote / hybrid / onsite, as the ATS reports it
    department: str | None = None
    employment_type: str | None = None
    salary: str | None = None
    description: str = ""
    posted_at: datetime | None = None

    @property
    def dedupe_key(self) -> str:
        raw = f"{self.source}:{self.company_slug}:{self.external_id}"
        return hashlib.sha1(raw.encode()).hexdigest()

    def all_locations_text(self) -> str:
        return " | ".join(dict.fromkeys([x for x in [self.location, *self.locations] if x]))


def client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, follow_redirects=True)


def get_json(http: httpx.Client, url: str, params: dict | None = None):
    try:
        r = http.get(url, params=params)
    except httpx.HTTPError as e:
        raise SourceError(f"network error: {e}") from e
    if r.status_code == 404:
        raise NotFound(url)
    if r.status_code >= 400:
        raise SourceError(f"HTTP {r.status_code} from {url}")
    try:
        data = r.json()
    except ValueError as e:
        raise SourceError(f"invalid JSON from {url}") from e
    # Lever answers unknown companies with 200 + {"ok": false} on some endpoints.
    if isinstance(data, dict) and data.get("ok") is False:
        raise NotFound(url)
    return data


# ---------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------

_BLOCK = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "tr", "section"}


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str | None, *, escaped: bool = False) -> str:
    if not raw:
        return ""
    if escaped:
        raw = html.unescape(raw)
    p = _TextExtractor()
    p.feed(raw)
    text = "".join(p.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_dt(value) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        if isinstance(value, (int, float)):  # epoch millis (Lever)
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None


def looks_remote(*texts: str | None) -> bool:
    return any(t and re.search(r"\bremote\b|\banywhere\b", t, re.I) for t in texts)


def pretty_slug(slug: str) -> str:
    return re.sub(r"[-_]+", " ", slug).title()
