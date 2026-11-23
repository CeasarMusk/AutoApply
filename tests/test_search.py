from datetime import datetime, timedelta, timezone

from sqlmodel import select

from autoapply import db
from autoapply.jobs.search import Filters, matches, store
from autoapply.models import Job
from autoapply.sources import RawJob


def raw(title="Software Engineer, Backend", location="New York, NY", remote=False, ext="1", days_old=3, **kw):
    return RawJob(source="greenhouse", company_slug="acme", external_id=ext, company="Acme", title=title,
                  url=f"https://x/{ext}", location=location, remote=remote,
                  posted_at=datetime.now(timezone.utc) - timedelta(days=days_old), **kw)


def test_keywords_need_all_words_of_a_phrase():
    f = Filters(keywords=["software engineer"])
    assert matches(raw("Senior Software Engineer"), f)
    assert matches(raw("Engineer, Software Platform"), f)  # word order doesn't matter
    assert not matches(raw("Software Sales"), f)


def test_exclude_and_any_keyword():
    f = Filters(keywords=["backend", "data engineer"], exclude=["senior", "staff"])
    assert matches(raw("Backend Developer"), f)
    assert matches(raw("Data Engineer II"), f)
    assert not matches(raw("Senior Backend Developer"), f)
    assert not matches(raw("Staff Data Engineer"), f)


def test_location_and_remote():
    f = Filters(locations=["Remote", "Austin"])
    assert matches(raw(location="Austin, TX"), f)
    assert matches(raw(location="Anywhere", remote=True), f)
    assert not matches(raw(location="Denver, CO"), f)
    assert matches(raw(location="Denver", locations=["Austin, TX"]), f)  # secondary location counts
    assert not matches(raw(remote=False), Filters(remote_only=True))


def test_max_age():
    f = Filters(max_age_days=14)
    assert matches(raw(days_old=3), f)
    assert not matches(raw(days_old=40), f)
    assert matches(raw(days_old=40, ), Filters())


def test_store_inserts_then_updates_keeping_status():
    with db.session() as s:
        r1 = store(s, [raw(ext="1"), raw(ext="2"), raw(ext="2")])  # duplicate in same batch
        assert len(r1.new) == 2 and r1.updated == 0
        job = s.exec(select(Job).where(Job.external_id == "1")).one()
        job.status = "saved"
        job.fit_score = 80
        s.add(job)
        s.commit()

        r2 = store(s, [raw(title="Software Engineer, Backend (Updated)", ext="1")])
        assert len(r2.new) == 0 and r2.updated == 1
        job = s.exec(select(Job).where(Job.external_id == "1")).one()
        assert job.title.endswith("(Updated)")
        assert (job.status, job.fit_score) == ("saved", 80)


# ---------------------------------------------------------------------------
# stemming, US detection, presets, ranking, closed jobs
# ---------------------------------------------------------------------------

from autoapply.jobs import presets
from autoapply.jobs.profile import SearchProfile
from autoapply.jobs.rank import score
from autoapply.jobs.search import is_us


def test_word_endings_match():
    f = Filters(keywords=["software engineer"])
    assert matches(raw("Software Engineering Intern"), f)
    assert matches(raw("Software Engineers, Platform"), f)


def test_location_is_whole_word():
    f = Filters(locations=["NY"])
    assert matches(raw(location="New York, NY"), f)
    assert not matches(raw(location="Sunnyvale, CA"), f)


def test_us_detection():
    assert is_us("San Francisco, Seattle") and is_us("Austin, TX") and is_us("Remote (US)")
    assert not is_us("Remote (Canada)") and not is_us("London, UK") and not is_us("Remote")
    f = Filters(locations=["@US"])
    assert matches(raw(location="Denver, CO"), f)
    assert not matches(raw(location="Toronto, Canada"), f)


def test_preset_entry_level_excludes_senior_and_interns():
    p = presets.build(["swe", "backend"], "entry", "Anywhere")
    f = Filters(keywords=p.keywords, exclude=p.exclude, require=p.require)
    assert matches(raw("Software Engineer, New Grad"), f)
    assert matches(raw("Backend Engineer"), f)
    assert not matches(raw("Senior Software Engineer"), f)
    assert not matches(raw("Sr. Backend Engineer"), f)
    assert not matches(raw("Software Engineering Intern"), f)
    assert not matches(raw("Product Designer"), f)


def test_preset_intern_requires_intern():
    p = presets.build(["swe"], "intern", "Remote only")
    f = Filters(keywords=p.keywords, require=p.require, remote_only=p.remote_only)
    assert matches(raw("Software Engineer Intern", remote=True), f)
    assert not matches(raw("Software Engineer", remote=True), f)
    assert not matches(raw("Software Engineer Intern", remote=False), f)


def test_every_specialty_resolves():
    for sid in presets.all_specialty_ids():
        assert presets.build([sid], "any", "Anywhere").keywords


def test_rank_prefers_similar_jobs():
    prof = SearchProfile(titles=["Backend Engineer", "Software Engineer"], specialties=["backend"], level="entry",
                         skills=["Python", "Airflow", "AWS", "SQL", "Go", "Docker"], summary="x")
    good = score("Backend Engineer", "We use Python, SQL and AWS. Docker is a plus.", prof)
    meh = score("Data Analyst", "Excel and SQL.", prof)
    assert good.score > meh.score
    assert good.skills == ["Python", "AWS", "SQL", "Docker"]
    # short skill names only match with exact case ("go" the verb isn't Go the language)
    assert "Go" not in score("Backend Engineer", "Ready to go fast", prof).skills
    assert "Go" in score("Backend Engineer", "Written in Go.", prof).skills


def test_vanished_jobs_are_closed():
    with db.session() as s:
        store(s, [raw(ext="1"), raw(ext="2")])
        live = {raw(ext="1").dedupe_key}
        res = store(s, [raw(ext="1")], fetched_keys={("greenhouse", "acme"): live})
        assert res.closed == 1
        assert s.exec(select(Job).where(Job.external_id == "2")).one().status == "closed"
        # reappears -> reopened
        store(s, [raw(ext="2")])
        assert s.exec(select(Job).where(Job.external_id == "2")).one().status == "seen"


def test_entry_level_catches_tricky_titles():
    p = presets.build(["swe"], "entry", "Anywhere")
    f = Filters(keywords=p.keywords, exclude=p.exclude)
    assert not matches(raw("Staff+ Software Engineer, Data"), f)
    assert not matches(raw("Software Engineer 3"), f)
    assert not matches(raw("Software Engineer II"), f)
    assert matches(raw("Software Engineer I"), f)
    from autoapply.jobs.search import stem_words
    assert {"c++", "c#"} <= stem_words("C++ and C# developer")


def test_listing_without_description_keeps_the_fetched_one():
    with db.session() as s:
        store(s, [raw(ext="1", description="Full posting text")])
        store(s, [RawJob(source="greenhouse", company_slug="acme", external_id="1", company="Acme",
                         title="Software Engineer, Backend", url="https://x/1")])
        job = s.exec(select(Job).where(Job.external_id == "1")).one()
        assert job.description == "Full posting text" and job.posted_at is not None
