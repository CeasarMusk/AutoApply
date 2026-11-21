from collections import deque

from autoapply.resume import builder, validate


def test_build_new_scripted(monkeypatch):
    texts = deque([
        # contact
        "Alex Rivera", "alex@example.com", "555-0100", "Austin, TX",
        # summary
        "",
        # job: title, company, location; bullets then blank to finish
        "Backend Engineer", "Acme", "Remote",
        "Built billing API", "Cut p99 latency 40%", "",
        # education: school, degree, field, gpa, details
        "UT Austin", "B.S.", "Computer Science", "", "Dean's List, Algorithms",
        # skills: category, items
        "Languages", "Go, Python , SQL",
    ])
    dates = deque(["2022-03", "Present", "2017", "2021"])
    # confirm() answers in order: add job? add another? add edu? add more edu? project? skill? more skills? cert?
    confirms = deque([True, False, True, False, False, True, False, False])
    choices = deque([builder.BACK])  # links menu: done

    monkeypatch.setattr(builder, "ask", lambda *a, **k: texts.popleft() or None)
    monkeypatch.setattr(builder, "ask_date", lambda *a, **k: dates.popleft())
    monkeypatch.setattr(builder, "confirm", lambda *a, **k: confirms.popleft())
    monkeypatch.setattr(builder, "choose", lambda *a, **k: choices.popleft())

    r = builder.build_new(llm=None)

    assert not texts and not dates and not confirms
    assert r.contact.name == "Alex Rivera"
    assert r.experience[0].title == "Backend Engineer"
    assert [b.id for b in r.experience[0].bullets] == ["exp1.b1", "exp1.b2"]
    assert r.experience[0].end == "Present"
    assert r.education[0].details == ["Dean's List", "Algorithms"]
    assert r.skills[0].items == ["Go", "Python", "SQL"]
    assert not validate.has_errors(validate.validate(r))
