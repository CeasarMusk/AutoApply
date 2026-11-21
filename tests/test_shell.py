import pytest

from autoapply import config, db
from autoapply.cli import app
from autoapply.jobs.search import store
from autoapply.models import Job
from autoapply.shell import Shell

from .test_search import raw


@pytest.fixture
def sh():
    return Shell(app)


@pytest.fixture
def two_jobs():
    with db.session() as s:
        res = store(s, [raw(ext="1"), raw(title="Backend Engineer", ext="2")])
        return [j.id for j in res.new]


def test_set_and_unset_persist(sh):
    sh.handle("set KEYWORDS software engineer, backend")
    sh.handle("set remote yes")
    sh.handle("set MAX_AGE 30")
    cfg = config.load_config()
    assert cfg.search.keywords == ["software engineer", "backend"]
    assert cfg.search.remote_only is True and cfg.search.max_age_days == 30
    sh.handle("unset KEYWORDS")
    assert config.load_config().search.keywords == []


def test_set_rejects_bad_values(sh, capsys):
    sh.handle("set REMOTE maybe")
    sh.handle("set BACKEND gpt")
    sh.handle("set NOPE 1")
    out = capsys.readouterr().out
    assert "true or false" in out and "claude-code, api" in out and "Unknown option" in out
    assert config.load_config().llm_backend == "claude-code"


def test_use_injects_job_id(sh, two_jobs, capsys):
    jid = two_jobs[1]
    sh.handle(f"use {jid}")
    assert sh.current_job.id == jid
    assert f"{jid}:Acme" in str(sh._prompt())
    sh.handle("save")
    with db.session() as s:
        assert s.get(Job, jid).status == "saved"
    assert sh.current_job.status == "saved"  # refreshed after the command
    sh.handle("ignore " + str(two_jobs[0]))  # explicit id wins
    with db.session() as s:
        assert s.get(Job, two_jobs[0]).status == "ignored"
    sh.handle("back")
    assert sh.current_job is None


def test_errors_keep_shell_alive(sh, capsys):
    assert sh.handle("bogus thing") is True
    assert sh.handle("search --nope") is True
    assert sh.handle('set KEYWORDS "unclosed') is True
    assert sh.handle("info") is True  # no job selected: missing argument, not a crash
    out = capsys.readouterr().out
    assert "No such command" in out and "No such option" in out and "Missing argument" in out
    assert sh.handle("exit") is False


def test_runs_regular_commands(sh, two_jobs, capsys):
    sh.handle("jobs")
    out = capsys.readouterr().out
    assert "2 jobs" in out and "Backend Engineer" in out


def test_completer_knows_commands(sh):
    from prompt_toolkit.document import Document

    def complete(text):
        return {c.text for c in sh.session.completer.get_completions(Document(text), None)}

    assert {"search", "jobs", "companies", "resume", "use", "set"} <= complete("")
    assert "add" in complete("companies ")
    assert "KEYWORDS" in complete("set ")
    assert "--remote" in complete("search --")
