"""Parsers for each ATS, using trimmed copies of real API responses (no network)."""

import httpx
import pytest

from autoapply import sources
from autoapply.sources import ashby, greenhouse, lever
from autoapply.sources.base import NotFound, html_to_text

GREENHOUSE = {
    "jobs": [{
        "id": 8301253,
        "title": "Backend / API Engineer, Radar",
        "company_name": "Stripe",
        "absolute_url": "https://stripe.com/jobs/search?gh_jid=8301253",
        "location": {"name": "San Francisco, US-Remote"},
        "offices": [{"name": "US"}],
        "departments": [{"name": "Radar - Eng"}],
        "first_published": "2026-10-19T12:00:00-04:00",
        "content": "&lt;h2&gt;About&lt;/h2&gt;&lt;p&gt;We build &amp;amp; ship.&lt;/p&gt;&lt;ul&gt;&lt;li&gt;Go&lt;/li&gt;&lt;li&gt;SQL&lt;/li&gt;&lt;/ul&gt;",
    }]
}

LEVER = [{
    "id": "6ed76ce8",
    "text": "Backend Software Engineer",
    "hostedUrl": "https://jobs.lever.co/palantir/6ed76ce8",
    "applyUrl": "https://jobs.lever.co/palantir/6ed76ce8/apply",
    "categories": {"commitment": "Full-time", "location": "New York, NY", "team": "Dev",
                   "allLocations": ["New York, NY", "Denver, CO"]},
    "workplaceType": "onsite",
    "createdAt": 1786469891368,
    "descriptionPlain": "Intro text",
    "lists": [{"text": "Requirements", "content": "<li>Java</li><li>Distributed systems</li>"}],
    "additionalPlain": "Benefits",
    "salaryRange": {"currency": "USD", "interval": "per-year-salary", "min": 135000, "max": 200000},
}]

ASHBY = {"jobs": [
    {"id": "34413f8d", "title": " Software Engineer, Security ", "location": "New York, NY (HQ)",
     "isRemote": True, "workplaceType": "Hybrid", "isListed": True,
     "jobUrl": "https://jobs.ashbyhq.com/ramp/34413f8d", "applyUrl": "https://jobs.ashbyhq.com/ramp/34413f8d/application",
     "publishedAt": "2026-04-07T17:12:35.753+00:00", "descriptionPlain": "Do security.",
     "secondaryLocations": [{"location": "Remote (US)"}],
     "compensation": {"scrapeableCompensationSalarySummary": "$211.4K - $290.6K"}},
    {"id": "hidden", "title": "Unlisted", "isListed": False, "jobUrl": "x"},
]}


def mock_http(routes: dict[str, tuple[int, object]]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        for prefix, (status, body) in routes.items():
            if str(request.url).startswith(prefix):
                return httpx.Response(status, json=body)
        return httpx.Response(404, json={"error": "nope"})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_greenhouse():
    http = mock_http({"https://boards-api.greenhouse.io/v1/boards/stripe/jobs": (200, GREENHOUSE)})
    [j] = greenhouse.fetch(http, "stripe")
    assert (j.company, j.title, j.external_id) == ("Stripe", "Backend / API Engineer, Radar", "8301253")
    assert j.location == "San Francisco, US-Remote" and j.remote
    assert j.description == "About\n\nWe build & ship.\n\n- Go\n- SQL"
    assert j.posted_at.year == 2026


def test_lever():
    http = mock_http({"https://api.lever.co/v0/postings/palantir": (200, LEVER)})
    [j] = lever.fetch(http, "palantir")
    assert j.title == "Backend Software Engineer" and not j.remote and j.workplace == "onsite"
    assert j.all_locations_text() == "New York, NY | Denver, CO"
    assert "Requirements\n- Java" in j.description and "Benefits" in j.description
    assert j.salary.startswith("USD 135,000–200,000")
    assert j.apply_url.endswith("/apply")


def test_ashby():
    http = mock_http({"https://api.ashbyhq.com/posting-api/job-board/ramp": (200, ASHBY)})
    jobs = ashby.fetch(http, "ramp")
    assert len(jobs) == 1  # unlisted job dropped
    j = jobs[0]
    assert j.title == "Software Engineer, Security" and j.remote
    assert j.salary == "$211.4K - $290.6K"
    assert "Remote (US)" in j.all_locations_text()


def test_not_found_and_lever_ok_false():
    http = mock_http({"https://api.lever.co/v0/postings/bad": (200, {"ok": False, "error": "Document not found"})})
    with pytest.raises(NotFound):
        lever.fetch(http, "bad")
    with pytest.raises(NotFound):
        greenhouse.fetch(http, "missing")


def test_fetch_all_collects_errors(monkeypatch):
    http = mock_http({"https://boards-api.greenhouse.io/v1/boards/stripe/jobs": (200, GREENHOUSE)})
    monkeypatch.setattr(sources, "client", lambda: http)
    report = sources.fetch_all([("greenhouse", "stripe"), ("lever", "gone")])
    assert report.per_company == {"greenhouse:stripe": 1}
    assert "lever:gone" in report.errors
    assert len(report.jobs) == 1


def test_html_to_text_strips_scripts_and_entities():
    assert html_to_text("<p>a&nbsp;b</p><script>x()</script><br>c") == "a b\n\nc"
