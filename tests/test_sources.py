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


# --- SimplifyJobs GitHub lists ---------------------------------------------------------------

from autoapply import config as _config  # noqa: E402
from autoapply.sources import github_lists  # noqa: E402

LISTING = [
    {"id": "a1", "active": True, "is_visible": True, "company_name": "ScoutAI", "title": "SWE Intern",
     "url": "https://job-boards.greenhouse.io/scoutai/jobs/4643333008", "locations": ["SF"],
     "date_posted": 1795392000, "category": "Software", "terms": ["Summer 2027"]},
    {"id": "a2", "active": True, "is_visible": True, "company_name": "Benchling", "title": "SWE Intern",
     "url": "https://jobs.ashbyhq.com/Benchling/96370d1b-d35e/application?embed=true", "locations": ["SF"]},
    {"id": "a3", "active": True, "is_visible": True, "company_name": "Acme", "title": "Data Intern",
     "url": "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/Austin/Data-Intern_R123",
     "locations": ["Remote in USA", "Austin, TX"], "sponsorship": "Does Not Offer Sponsorship"},
    {"id": "a4", "active": True, "is_visible": True, "company_name": "Agency", "title": "Intern",
     "url": "https://job-boards.eu.greenhouse.io/agency/jobs/4835696101", "locations": ["London, UK"]},
    {"id": "a5", "active": False, "is_visible": True, "company_name": "Old", "title": "Closed",
     "url": "https://jobs.lever.co/old/123/apply", "locations": []},
    {"id": "a6", "active": True, "is_visible": False, "company_name": "Hidden", "title": "Hidden",
     "url": "https://example.com/job", "locations": []},
]


def _github_http(calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json=LISTING if "Internships" in str(request.url) else [])

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_site_for_and_board_posting():
    assert github_lists.site_for("https://acme.wd5.myworkdayjobs.com/x") == "workday"
    assert github_lists.site_for("https://careers-acme.icims.com/jobs/1/job") == "icims"
    assert github_lists.site_for("https://acme.com/careers/1") == "web"
    assert github_lists.board_posting("https://jobs.lever.co/Cirrus/12cd/apply") == ("lever", "cirrus", "12cd")
    assert github_lists.board_posting("https://boards.greenhouse.io/embed/job_app?token=7918610") is None


def test_github_lists_cache_boards_and_fetch():
    calls: list = []
    http = _github_http(calls)
    items = github_lists.listings(http)
    assert [x["id"] for x in items] == ["a1", "a2", "a3", "a4"]  # inactive and hidden dropped
    github_lists.listings(http)
    assert len(calls) == 2  # second call served from the disk cache

    assert github_lists.boards(items) == [("greenhouse", "scoutai"), ("ashby", "benchling")]

    jobs = {j.title + j.company: j for j in github_lists.fetch(http)}
    assert set(jobs) == {"SWE InternScoutAI", "SWE InternBenchling", "Data InternAcme", "InternAgency"}
    wd = jobs["Data InternAcme"]
    assert (wd.source, wd.company_slug, wd.external_id) == ("workday", "simplify", "a3")
    assert wd.remote and wd.location == "Remote in USA" and wd.locations == ["Austin, TX"]
    assert "Does Not Offer Sponsorship" in wd.description
    eu = jobs["InternAgency"]
    assert (eu.source, eu.company_slug, eu.external_id) == ("greenhouse", "agency", "4835696101")


def test_github_board_posting_matches_board_dedupe_key():
    gh = github_lists.to_raw(LISTING[0])
    board = github_lists.RawJob(source="greenhouse", company_slug="scoutai", external_id="4643333008",
                                company="ScoutAI", title="x", url="u")
    assert gh.dedupe_key == board.dedupe_key
    assert gh.posted_at.year == 2026


def test_search_targets_include_github_boards(monkeypatch):
    calls: list = []
    monkeypatch.setattr(sources, "client", lambda: _github_http(calls))
    cfg = _config.Config(use_starter_companies=False, use_remoteok=False)
    assert sources.search_targets(cfg) == [("greenhouse", "scoutai"), ("ashby", "benchling"), ("github", "all")]
    cfg.use_github_lists = False
    assert sources.search_targets(cfg) == []


def test_fetch_all_prefers_boards_and_falls_back_to_feed(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "raw.githubusercontent" in url:
            return httpx.Response(200, json=LISTING if "Internships" in url else [])
        if "boards-api.greenhouse.io/v1/boards/scoutai" in url:
            return httpx.Response(200, json={"jobs": [dict(GREENHOUSE["jobs"][0], id=4643333008)]})
        return httpx.Response(404, json={})  # the ashby benchling board fails

    monkeypatch.setattr(sources, "client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    report = sources.fetch_all([("greenhouse", "scoutai"), ("ashby", "benchling"), ("github", "all")])
    by_board = {(j.source, j.company_slug): j for j in report.jobs}
    assert by_board[("greenhouse", "scoutai")].description.startswith("About")  # the board's copy, not the feed's
    assert sum(1 for j in report.jobs if j.company_slug == "scoutai") == 1
    assert ("ashby", "benchling") in by_board  # feed listing stands in for the failed board
    assert report.errors == {}
    assert set(report.fetched_keys) == {("greenhouse", "scoutai"), ("workday", "simplify"), ("greenhouse", "agency")}
