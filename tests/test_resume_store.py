from autoapply.models import Bullet, Experience
from autoapply.resume import store, validate

from .conftest import sample_resume


def test_assign_ids_fills_blanks():
    r = store.assign_ids(sample_resume())
    assert [e.id for e in r.experience] == ["exp1", "exp2"]
    assert [b.id for b in r.experience[0].bullets] == ["exp1.b1", "exp1.b2"]
    assert r.experience[1].bullets[0].id == "exp2.b1"
    assert r.skills[0].id == "skill1"


def test_ids_stay_stable_across_edits():
    r = store.assign_ids(sample_resume())
    # Insert a new job at the top and a new bullet in the middle.
    r.experience.insert(0, Experience(company="NewCo", title="Lead"))
    r.experience[1].bullets.insert(1, Bullet(text="Mentored two juniors"))
    store.assign_ids(r)
    assert r.experience[1].id == "exp1"  # Acme keeps its id
    assert r.experience[0].id == "exp3"  # NewCo gets the next free one
    assert [b.id for b in r.experience[1].bullets] == ["exp1.b1", "exp1.b3", "exp1.b2"]


def test_duplicate_and_malformed_ids_are_replaced():
    r = sample_resume()
    r.experience[0].id = "exp1"
    r.experience[1].id = "exp1"  # duplicate
    r.skills[0].id = "banana"  # malformed
    store.assign_ids(r)
    assert r.experience[0].id == "exp1"
    assert r.experience[1].id == "exp2"
    assert r.skills[0].id == "skill1"


def test_moved_bullet_gets_new_id():
    r = store.assign_ids(sample_resume())
    moved = r.experience[0].bullets.pop()
    r.experience[1].bullets.append(moved)
    store.assign_ids(r)
    assert moved.id.startswith("exp2.b")


def test_save_load_roundtrip(isolated_home):
    r = sample_resume()
    path = store.save(r)
    assert path.read_text().startswith("# AutoApply master resume")
    loaded = store.load()
    assert loaded == r
    assert loaded.experience[0].bullets[1].id == "exp1.b2"


def test_validate_flags_problems():
    r = sample_resume()
    r.contact.email = None
    r.experience[0].start = "March 2022"
    r.experience[1].bullets = []
    issues = validate.validate(r)
    msgs = {(i.level, i.where.split("[")[0]): i.message for i in issues}
    assert ("error", "contact.email") in msgs
    assert any("not YYYY-MM" in i.message for i in issues)
    assert any(i.message == "no bullet points" for i in issues)
    assert validate.has_errors(issues)


def test_validate_clean_resume():
    r = store.assign_ids(sample_resume())
    assert not validate.has_errors(validate.validate(r))


def test_review_does_not_eat_brackets():
    from rich.console import Console

    from autoapply.resume import review

    r = store.assign_ids(sample_resume())
    r.experience[0].bullets[0].text = "Grew revenue [X%] using [bold]tricks[/bold]"
    console = Console(record=True, width=200)
    review.show(console, r, validate.validate(r))
    out = console.export_text()
    assert "[X%]" in out and "[bold]tricks[/bold]" in out
