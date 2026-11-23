from typer.testing import CliRunner

from autoapply import config, db
from autoapply.cli import app
from autoapply.jobs import queue as q
from autoapply.jobs.search import store
from autoapply.models import Job

from .test_search import raw


def _jobs():
    """Four jobs: (title, days old, match score). Two share a title (same role, two cities)."""
    specs = [("Backend Engineer", 10, 90, "NYC"), ("Data Engineer", 1, 60, "NYC"),
             ("Platform Engineer", 5, 75, "NYC"), ("Backend Engineer", 2, 80, "Austin")]
    rjs = [raw(title=t, ext=str(i), days_old=d, location=loc) for i, (t, d, _, loc) in enumerate(specs)]
    scores = {r.dedupe_key: (m, []) for r, (_, _, m, _) in zip(rjs, specs)}
    with db.session() as s:
        res = store(s, rjs, scores)
        return {j.title + "@" + j.location: j.id for j in res.new}


def _queued_titles():
    with db.session() as s:
        return [(j.title, j.location) for _, j in q.items(s)]


def test_queue_by_match_keeps_one_copy_per_role():
    ids = _jobs()
    with db.session() as s:
        added, already = q.add(s, q.candidates(s, "all"), "match")
    assert (added, already) == (3, 0)
    # The NYC backend role (90) outranks the Austin copy (80), which is dropped as the same role.
    assert _queued_titles() == [("Backend Engineer", "NYC"), ("Platform Engineer", "NYC"), ("Data Engineer", "NYC")]
    assert ids


def test_queue_by_recent_and_resort():
    _jobs()
    with db.session() as s:
        q.add(s, q.candidates(s, "all"), "recent")
    # Newest first; the Austin backend copy (2 days) wins over the NYC one (10 days).
    assert _queued_titles() == [("Data Engineer", "NYC"), ("Backend Engineer", "Austin"), ("Platform Engineer", "NYC")]
    with db.session() as s:
        q.resort(s, "match")
    assert [t for t, _ in _queued_titles()] == ["Backend Engineer", "Platform Engineer", "Data Engineer"]


def test_queue_uses_last_search_and_skips_inactive():
    ids = _jobs()
    q.remember_results([ids["Data Engineer@NYC"], ids["Platform Engineer@NYC"]], "my search")
    with db.session() as s:
        j = s.get(Job, ids["Platform Engineer@NYC"])
        j.status = "applied"
        s.add(j)
        s.commit()
        added, _ = q.add(s, q.candidates(s, "last"), "match")
    assert added == 1 and _queued_titles() == [("Data Engineer", "NYC")]


def test_min_match_limit_add_again_and_prune():
    ids = _jobs()
    with db.session() as s:
        assert q.add(s, q.candidates(s, "all"), "match", min_match=70, limit=1) == (1, 0)
        assert q.add(s, q.candidates(s, "all"), "match", min_match=70) == (1, 1)
        j = s.get(Job, ids["Backend Engineer@NYC"])
        j.status = "ignored"
        s.add(j)
        s.commit()
        assert q.prune(s) == 1
    assert _queued_titles() == [("Platform Engineer", "NYC")]


def test_queue_cli_build_show_order_remove():
    ids = _jobs()
    q.remember_results(list(ids.values()), "backend")
    runner = CliRunner()
    r = runner.invoke(app, ["queue", "build", "--from", "last", "--order", "recent"])
    assert r.exit_code == 0, r.output
    assert "Queued 3 job(s)" in r.output and "newest first" in r.output
    assert config.load_config().queue_order == "recent"
    r = runner.invoke(app, ["queue", "order", "match"])
    assert r.exit_code == 0 and "best match first" in r.output
    r = runner.invoke(app, ["queue", "remove", str(ids["Data Engineer@NYC"])])
    assert "Removed 1" in r.output
    r = runner.invoke(app, ["queue"])
    assert "2 job(s)" in r.output
    r = runner.invoke(app, ["queue", "build", "--order", "sideways"])
    assert r.exit_code == 1 and "Unknown order" in r.output
    r = runner.invoke(app, ["queue", "clear", "--yes"])
    assert "queue is empty" in r.output


def test_queue_run_applies_and_removes(monkeypatch):
    ids = _jobs()
    with db.session() as s:
        q.add(s, q.candidates(s, "all"), "match")
    from autoapply import cli_apply, cli_queue

    answers = iter(["apply", "skip", "remove"])

    class Sel:
        def __init__(self, *a, **k):
            pass

        def ask(self):
            return next(answers)

    calls = []

    def fake_apply(job, **kw):
        calls.append(job.id)
        with db.session() as s:
            j = s.get(Job, job.id)
            j.status = "applied"
            s.add(j)
            s.commit()
        return "applied"

    monkeypatch.setattr(cli_queue.questionary, "select", Sel)
    monkeypatch.setattr(cli_queue, "_interactive", lambda: True)
    monkeypatch.setattr(cli_apply, "apply_one", fake_apply)
    r = CliRunner().invoke(app, ["queue", "run"])
    assert r.exit_code == 0, r.output
    assert calls == [ids["Backend Engineer@NYC"]]
    assert "1 applied" in r.output and "1 skipped" in r.output and "1 removed" in r.output
    assert _queued_titles() == [("Platform Engineer", "NYC")]
