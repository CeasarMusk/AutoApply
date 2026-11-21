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
