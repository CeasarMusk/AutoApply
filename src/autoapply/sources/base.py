"""Shared types and helpers for job sources."""

from __future__ import annotations

import hashlib
import html
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit

import httpx

USER_AGENT = "AutoApply/0.1 (personal job search CLI)"
TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class SourceError(RuntimeError):
    pass


class NotFound(SourceError):
    """The company slug doesn't exist on this ATS."""


@dataclass
class RawJob:
    source: str  # greenhouse / lever / ashby / workday / … (see sources.ALL)
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


class Listing(list):
    """A fetch result. ``complete`` is False when it's a search or capped, not the whole board,
    so jobs missing from it mustn't be marked closed."""

    complete: bool = True


def client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT, follow_redirects=True)


# ---------------------------------------------------------------------------
# politeness: per-host spacing + concurrency, and backing off when asked to
# ---------------------------------------------------------------------------

MIN_INTERVAL_S = 0.35  # minimum gap between two requests to the same host group
PER_HOST_CONCURRENCY = 2  # parallel requests per host group
MAX_RETRY_AFTER_S = 30.0  # longest Retry-After we'll honour before giving up on a board


class _HostGate:
    def __init__(self):
        self.sem = threading.BoundedSemaphore(PER_HOST_CONCURRENCY)
        self.lock = threading.Lock()
        self.next_at = 0.0


_gates: dict[str, _HostGate] = {}
_gates_lock = threading.Lock()


def host_group(url: str) -> str:
    """Requests to hosts that share infrastructure are paced together.

    Workday tenants (acme.wd5.myworkdayjobs.com) share the wd5 cluster, so they're grouped by
    cluster; Oracle by region pod; everything else by hostname.
    """
    host = (urlsplit(url).hostname or "").lower()
    labels = host.split(".")
    if host.endswith((".myworkdayjobs.com", ".myworkdaysite.com")) and len(labels) >= 3:
        return ".".join(labels[-3:])
    if host.endswith(".oraclecloud.com") and len(labels) >= 4:
        return ".".join(labels[-4:])
    if host.endswith(".bamboohr.com"):
        return "bamboohr.com"
    return host


@contextmanager
def polite(url: str):
    """Hold a per-host-group slot and wait out the minimum spacing before a request."""
    with _gates_lock:
        gate = _gates.setdefault(host_group(url), _HostGate())
    with gate.sem:
        with gate.lock:
            wait = gate.next_at - time.monotonic()
            gate.next_at = max(gate.next_at, time.monotonic()) + MIN_INTERVAL_S
        if wait > 0:
            time.sleep(wait)
        yield


def _retry_after(r: httpx.Response) -> float | None:
    value = r.headers.get("Retry-After")
    if value is None:
        return 5.0
    try:
        return float(value)
    except ValueError:
        return None


def request(http: httpx.Client, method: str, url: str, **kwargs) -> httpx.Response:
    """One polite request. A 429/503 is retried once after Retry-After (if it's short)."""
    for attempt in range(2):
        try:
            with polite(url):
                r = http.request(method, url, **kwargs)
        except httpx.HTTPError as e:
            raise SourceError(f"network error: {e}") from e
        if r.status_code in (429, 503) and attempt == 0:
            delay = _retry_after(r)
            if delay is not None and delay <= MAX_RETRY_AFTER_S:
                time.sleep(delay)
                continue
            raise SourceError(f"HTTP {r.status_code} from {url} (rate limited; try again later)")
        break
    if r.status_code == 404:
        raise NotFound(url)
    if r.status_code >= 400:
        raise SourceError(f"HTTP {r.status_code} from {url}")
    return r


def _json(r: httpx.Response, url: str):
    try:
        return r.json()
    except ValueError as e:
        raise SourceError(f"invalid JSON from {url}") from e


def get_text(http: httpx.Client, url: str, params: dict | None = None) -> str:
    return request(http, "GET", url, params=params).text


def post_json(http: httpx.Client, url: str, body: dict, headers: dict | None = None):
    return _json(request(http, "POST", url, json=body, headers=headers), url)


def get_json(http: httpx.Client, url: str, params: dict | None = None):
    data = _json(request(http, "GET", url, params=params), url)
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
    from .starter import NAMES

    return NAMES.get(slug) or re.sub(r"[-_]+", " ", slug).title()
