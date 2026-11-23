"""Keryx offline list, URL recognition and the new ATS boards (no network)."""

import gzip
import json

import httpx
import pytest

from autoapply import sources
from autoapply.sources import base, keryx, urls
from autoapply.sources import bamboohr, greenhouse, oracle, smartrecruiters, workable, workday
from autoapply.sources.base import RawJob


def client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------------------
# URL recognition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url,expected", [
    ("https://job-boards.greenhouse.io/artefactlinkedin/jobs/8958079002", ("greenhouse", "artefactlinkedin", "8958079002")),
    ("https://boards.greenhouse.io/andurilindustries/jobs/5225618007?gh_jid=5225618007", ("greenhouse", "andurilindustries", "5225618007")),
    ("https://boards.greenhouse.io/embed/job_app?for=stripe&token=123", ("greenhouse", "stripe", "123")),
    ("https://jobs.lever.co/palantir/66b495eb-aaf9", ("lever", "palantir", "66b495eb-aaf9")),
    ("https://jobs.lever.co/palantir", ("lever", "palantir", None)),
    ("https://jobs.ashbyhq.com/first-resonance/56d901c8/application?embed=true", ("ashby", "first-resonance", "56d901c8")),
    ("https://jobs.smartrecruiters.com/BoschGroup/744000163222869-lehrstelle", ("smartrecruiters", "BoschGroup", "744000163222869")),
    ("https://apply.workable.com/huggingface/j/81B46579FE/", ("workable", "huggingface", "81B46579FE")),
    ("https://apply.workable.com/huggingface/jobs/view/81B46579FE.md", ("workable", "huggingface", "81B46579FE")),
    ("https://aceelectric.bamboohr.com/careers/601", ("bamboohr", "aceelectric", "601")),
    ("https://ejwl.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/26153168",
     ("oracle", "ejwl.fa.us2.oraclecloud.com/CX", "26153168")),
    ("https://boeing.wd1.myworkdayjobs.com/en-US/external_subsidiary/job/USA---Richardson-TX/Entry-Level_JR2026523382",
     ("workday", "boeing.wd1.myworkdayjobs.com/boeing/external_subsidiary", "/job/USA---Richardson-TX/Entry-Level_JR2026523382")),
    ("https://acme.wd5.myworkdayjobs.com/External", ("workday", "acme.wd5.myworkdayjobs.com/acme/External", None)),
])
def test_parse_urls(url, expected):
    ref = urls.parse(url)
    assert (ref.ats, ref.slug, ref.external_id) == expected


@pytest.mark.parametrize("url", ["https://lifeattiktok.com/search/767", "not a url", "https://jobs.lever.co/",
                                 "https://boards.greenhouse.io/embed/job_app"])
def test_parse_unknown(url):
    assert urls.parse(url) is None


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------

KERYX_JOBS = {"country": "United States", "schema_version": 2, "jobs": [
    {"id": "job_1", "status": "open", "company": "Stripe", "title": "Software Engineer Intern",
     "location": "Remote in USA", "url": "https://job-boards.greenhouse.io/stripe/jobs/111",
     "program": "internship", "cycle": "summer-2027", "posted_at": "2026-11-20", "sponsorship": None},
    {"id": "job_2", "status": "open", "company": "TikTok", "title": "SRE Intern", "location": "San Jose, CA",
     "url": "https://lifeattiktok.com/search/767", "program": "internship", "cycle": "unscheduled",
     "posted_at": "2026-11-21", "sponsorship": "no-sponsorship"},
    {"id": "job_3", "status": "closed", "company": "Old", "title": "Gone", "url": "https://x.example/1"},
    {"id": "job_4", "status": "open", "company": "Boeing", "title": "Software Engineer New Grad",
     "location": "Richardson, TX", "program": "new-grad", "cycle": "2027", "posted_at": "2026-11-21",
     "url": "https://boeing.wd1.myworkdayjobs.com/en-US/External_Subsidiary/job/USA/SWE_JR1"},
]}
KERYX_BOARDS = {"schema_version": 1, "boards": [
    {"ats": "greenhouse", "company": "Stripe", "key": "greenhouse:stripe", "slug": "stripe"},
    {"ats": "workday", "company": "Boeing", "host": "boeing.wd1.myworkdayjobs.com", "key": "workday:x",
     "site": "external_subsidiary", "slug": "boeing", "wd": "wd1"},
    {"ats": "oracle", "company": "Worthington", "host": "cbdt.fa.us2.oraclecloud.com", "key": "oracle:y", "site": "CX", "slug": "cbdt"},
]}


def keryx_http(calls: list, etag="v1"):
    def handler(req: httpx.Request):
        calls.append((req.url.path, req.headers.get("If-None-Match")))
        if req.headers.get("If-None-Match") == etag:
            return httpx.Response(304)
        body = KERYX_JOBS if req.url.path.endswith("jobs.json") else KERYX_BOARDS
        return httpx.Response(200, json=body, headers={"ETag": etag})
    return client(handler)


@pytest.fixture
def synced():
    calls = []
    keryx.sync(force=True, http=keryx_http(calls))
    return calls


def test_bundled_snapshot_loads():
    jobs = keryx.load()
    assert len(jobs) > 1000
    assert {"greenhouse", "workday", "keryx"} <= {j.source for j in jobs}
    assert len(keryx.boards()) > 1000


def test_sync_converts_and_identifies(synced):
    jobs = {j.title: j for j in keryx.load()}
    assert set(jobs) == {"Software Engineer Intern", "SRE Intern", "Software Engineer New Grad"}  # closed dropped
    gh = jobs["Software Engineer Intern"]
    assert (gh.source, gh.company_slug, gh.external_id) == ("greenhouse", "stripe", "111")
    assert gh.remote and gh.employment_type == "Internship · Summer 2027"
    assert gh.posted_at.isoformat() == "2026-11-20T00:00:00+00:00"
    # Same identity as the live Greenhouse fetcher, so they dedupe.
    live = RawJob(source="greenhouse", company_slug="stripe", external_id="111", company="Stripe", title="x", url="u")
    assert gh.dedupe_key == live.dedupe_key
    tt = jobs["SRE Intern"]
    assert tt.source == "keryx" and tt.employment_type == "Internship · no sponsorship"
    # Workday site case comes from Keryx's board list so it matches the API's spelling.
    wd = jobs["Software Engineer New Grad"]
    assert wd.company_slug == "boeing.wd1.myworkdayjobs.com/boeing/external_subsidiary"
    assert keryx.company_name("oracle", "CBDT.fa.us2.oraclecloud.com/cx") == "Worthington"


def test_sync_is_conditional_and_spaced(synced):
    calls = []
    assert keryx.sync(http=keryx_http(calls))["skipped"]  # checked < 15 min ago: no request at all
    assert calls == []
    meta = keryx.meta()
    meta["checked_at"] = 0
    keryx._meta_path().write_text(json.dumps(meta))
    r = keryx.sync(http=keryx_http(calls))
    assert r["changed"] == [] and all(etag == "v1" for _, etag in calls)  # sent If-None-Match, got 304s
    assert len(keryx.load()) == 3


def test_sync_due():
    assert keryx.sync_due(12)
    assert not keryx.sync_due(0)


def test_live_targets_orders_and_caps():
    mk = lambda src, slug, i: RawJob(source=src, company_slug=slug, external_id=str(i), company="c", title="t", url="u")  # noqa: E731
    jobs = [mk("workday", "a", 1), mk("workday", "a", 2), mk("greenhouse", "b", 3), mk("keryx", "x", 4), mk("lever", "c", 5)]
    assert keryx.live_targets(jobs, 2) == [("workday", "a"), ("greenhouse", "b")]
    assert keryx.live_targets(jobs, 5, skip=[("greenhouse", "b")]) == [("workday", "a"), ("lever", "c")]


# ---------------------------------------------------------------------------
# new ATS fetchers (trimmed real responses)
# ---------------------------------------------------------------------------

WD_SLUG = "clarios.wd5.myworkdayjobs.com/clarios/clarioscareers"


def test_workday_searches_terms_and_pages():
    bodies = []

    def handler(req):
        body = json.loads(req.content)
        bodies.append(body)
        if req.url.path.endswith("/jobs"):
            n = 20 if body["offset"] == 0 and body["searchText"] == "intern" else 1
            return httpx.Response(200, json={"total": 21, "jobPostings": [
                {"title": f"Engineering Intern {body['offset'] + i}", "externalPath": f"/job/US/Intern_{body['searchText']}{body['offset'] + i}",
                 "locationsText": "Remote, US", "postedOn": "Posted 3 Days Ago"} for i in range(n)]})
        return httpx.Response(404)

    jobs = workday.fetch(client(handler), WD_SLUG, query=("intern", "co-op"))
    assert [b["offset"] for b in bodies] == [0, 20, 0] and bodies[0]["searchText"] == "intern"
    assert len(jobs) == 22 and not jobs.complete
    j = jobs[0]
    assert j.url == "https://clarios.wd5.myworkdayjobs.com/clarioscareers/job/US/Intern_intern0"
    assert j.external_id == "/job/US/Intern_intern0" and j.remote and j.posted_at is not None


def test_workday_detail_and_bad_slug():
    def handler(req):
        assert req.url.path == "/wday/cxs/clarios/clarioscareers/job/US/X_1"
        return httpx.Response(200, json={"jobPostingInfo": {"jobDescription": "<p>Build <b>things</b>&amp;#xa;&amp;#xa;</p><p>Go</p>"}})

    assert workday.detail(client(handler), WD_SLUG, "/job/US/X_1") == "Build things\n\nGo"
    with pytest.raises(sources.SourceError):
        workday.split_slug("evil.example.com/a/b")


def test_oracle():
    def handler(req):
        assert "siteNumber=CX" in req.url.params["finder"] and "keyword=intern" in req.url.params["finder"]
        return httpx.Response(200, json={"items": [{"TotalJobsCount": 1, "requisitionList": [
            {"Id": "43293", "Title": "Software Intern", "PostedDate": "2026-11-20",
             "PrimaryLocation": "Twinsburg, OH, United States", "WorkplaceTypeCode": "ORA_ON_SITE"}]}]})

    [j] = oracle.fetch(client(handler), "cbdt.fa.us2.oraclecloud.com/CX", query=("intern",))
    assert j.url == "https://cbdt.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/43293"
    assert j.title == "Software Intern" and j.location.startswith("Twinsburg")


def test_smartrecruiters_pages_and_unknown():
    def handler(req):
        if "nobody" in req.url.path:
            return httpx.Response(200, json={"offset": 0, "limit": 100, "totalFound": 0, "content": []})
        off = int(req.url.params["offset"])
        content = [{"id": str(off + i), "name": f"Job {off + i}", "company": {"name": "Bosch Group"},
                    "location": {"fullLocation": "Austin, TX, United States", "remote": False},
                    "typeOfEmployment": {"label": "Full-time"}, "releasedDate": "2026-11-23T13:27:08.018Z"}
                   for i in range(100 if off == 0 else 5)]
        return httpx.Response(200, json={"totalFound": 105, "content": content})

    jobs = smartrecruiters.fetch(client(handler), "BoschGroup")
    assert len(jobs) == 105 and jobs.complete
    assert jobs[0].url == "https://jobs.smartrecruiters.com/BoschGroup/0" and jobs[0].company == "Bosch Group"
    with pytest.raises(sources.NotFound):
        smartrecruiters.fetch(client(handler), "nobody")


def test_workable_and_bamboohr():
    def handler(req):
        if req.url.host == "apply.workable.com":
            return httpx.Response(200, json={"name": "Hugging Face", "jobs": [
                {"title": "ML Engineer", "shortcode": "81B46579FE", "telecommuting": True, "city": "Paris",
                 "country": "France", "published_on": "2026-05-29", "employment_type": "Full-time",
                 "application_url": "https://apply.workable.com/j/81B46579FE/apply"}]})
        return httpx.Response(200, json={"result": [
            {"id": "601", "jobOpeningName": "Electrical Intern", "departmentLabel": "STB",
             "employmentStatusLabel": "Intern", "location": {"city": "Augusta", "state": "Georgia"}, "isRemote": None}]})

    http = client(handler)
    [w] = workable.fetch(http, "huggingface")
    assert (w.company, w.remote, w.url) == ("Hugging Face", True, "https://apply.workable.com/huggingface/j/81B46579FE/")
    [b] = bamboohr.fetch(http, "aceelectric")
    assert (b.url, b.location, b.remote) == ("https://aceelectric.bamboohr.com/careers/601", "Augusta, Georgia", False)
    with pytest.raises(sources.SourceError):
        bamboohr.fetch(http, "evil.com/x")


# ---------------------------------------------------------------------------
# fetch_all: completeness, cache, politeness
# ---------------------------------------------------------------------------


def test_incomplete_listings_dont_close_jobs(monkeypatch):
    def fake(http, slug, query=()):
        out = base.Listing([RawJob(source="workday", company_slug=slug, external_id="/job/1", company="c", title="t", url="u")])
        out.complete = False
        return out

    monkeypatch.setattr(workday, "fetch", fake)
    report = sources.fetch_all([("workday", WD_SLUG)])
    assert report.per_company == {f"workday:{WD_SLUG}": 1} and report.fetched_keys == {}


def test_board_cache(monkeypatch):
    calls = []

    def fake(http, slug, query=()):
        calls.append(slug)
        return [RawJob(source="greenhouse", company_slug=slug, external_id="1", company="Stripe", title="SWE",
                       url="u", posted_at=base.parse_dt("2026-11-01T00:00:00Z"))]

    monkeypatch.setattr(greenhouse, "fetch", fake)
    first = sources.fetch_all([("greenhouse", "stripe")], cache_minutes=15)
    second = sources.fetch_all([("greenhouse", "stripe")], cache_minutes=15)
    assert calls == ["stripe"] and second.cached == 1
    assert second.jobs[0].dedupe_key == first.jobs[0].dedupe_key and second.jobs[0].posted_at.year == 2026
    assert ("greenhouse", "stripe") in second.fetched_keys
    sources.fetch_all([("greenhouse", "stripe")], cache_minutes=0)
    assert calls == ["stripe", "stripe"]


def test_query_only_reaches_search_boards(monkeypatch):
    seen = {}
    monkeypatch.setattr(greenhouse, "fetch", lambda http, slug, query=(): seen.setdefault("gh", query) and [])
    monkeypatch.setattr(workday, "fetch", lambda http, slug, query=(): seen.setdefault("wd", query) and [])
    sources.fetch_all([("greenhouse", "x"), ("workday", WD_SLUG)], query=("intern",))
    assert seen == {"gh": (), "wd": ("intern",)}


def test_retry_after_429(monkeypatch):
    sleeps = []
    monkeypatch.setattr(base.time, "sleep", lambda s: sleeps.append(s))
    n = {"calls": 0}

    def handler(req):
        n["calls"] += 1
        if n["calls"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"ok": True})

    assert base.get_json(client(handler), "https://api.lever.co/v0/postings/x") == {"ok": True}
    assert sleeps == [2.0]


def test_long_retry_after_gives_up(monkeypatch):
    monkeypatch.setattr(base.time, "sleep", lambda s: pytest.fail("shouldn't wait"))
    http = client(lambda req: httpx.Response(429, headers={"Retry-After": "3600"}))
    with pytest.raises(sources.SourceError, match="rate limited"):
        base.get_json(http, "https://api.lever.co/v0/postings/x")


def test_host_groups():
    assert base.host_group("https://boeing.wd1.myworkdayjobs.com/x") == base.host_group("https://acme.wd1.myworkdayjobs.com/y")
    assert base.host_group("https://boeing.wd1.myworkdayjobs.com/x") != base.host_group("https://acme.wd5.myworkdayjobs.com/y")
    assert base.host_group("https://a.bamboohr.com/careers") == "bamboohr.com"


# ---------------------------------------------------------------------------
# search: offline list + live check of the matching boards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("still_listed", [True, False])
def test_search_merges_offline_and_live(synced, monkeypatch, still_listed):
    from sqlmodel import select

    from autoapply import cli_jobs, config, db
    from autoapply.jobs.search import Filters
    from autoapply.models import Job

    config.save_config(config.Config(use_starter_companies=False, use_remoteok=False, use_github_lists=False,
                                     keryx_sync_hours=0))
    asked = {}

    def gh(http, slug, query=()):
        asked["greenhouse"] = slug
        if not still_listed:
            return [RawJob(source="greenhouse", company_slug=slug, external_id="222", company="Stripe",
                           title="Data Analyst", url="https://job-boards.greenhouse.io/stripe/jobs/222")]
        return [RawJob(source="greenhouse", company_slug=slug, external_id="111", company="Stripe",
                       title="Software Engineer Intern", url="https://job-boards.greenhouse.io/stripe/jobs/111",
                       description="Live description")]

    def wd(http, slug, query=()):
        asked["workday"] = (slug, query)
        out = base.Listing()
        out.complete = False
        return out

    monkeypatch.setattr(greenhouse, "fetch", gh)
    monkeypatch.setattr(workday, "fetch", wd)
    cli_jobs.run_search(Filters(keywords=["software engineer"]), "test")

    assert asked["greenhouse"] == "stripe"
    assert asked["workday"] == ("boeing.wd1.myworkdayjobs.com/boeing/external_subsidiary", ("software engineer",))
    with db.session() as s:
        rows = {j.title: j for j in s.exec(select(Job))}
    # Workday's live answer was a search, so the Keryx row stands.
    assert rows["Software Engineer New Grad"].source == "workday"
    if still_listed:
        assert rows["Software Engineer Intern"].description == "Live description"  # live wins
    else:
        assert "Software Engineer Intern" not in rows  # Stripe's full board no longer has it
    assert "SRE Intern" not in rows  # didn't match the keywords
