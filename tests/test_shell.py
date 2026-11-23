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
    sh.handle("set starter no")
    sh.handle("set MODEL sonnet")
    cfg = config.load_config()
    assert cfg.use_starter_companies is False and cfg.claude_code_model == "sonnet"
    sh.handle("unset STARTER")
    assert config.load_config().use_starter_companies is True


def test_set_rejects_bad_values(sh, capsys):
    sh.handle("set REMOTEOK maybe")
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
    assert sh.handle('set MODEL "unclosed') is True
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
    assert "STARTER" in complete("set ")
    assert "--remote" in complete("search --")


def test_ollama_and_keryx_options(sh, capsys):
    sh.handle("set BACKEND ollama")
    sh.handle("set OLLAMA_MODEL qwen3:14b")
    sh.handle("set KERYX_SYNC 6.5")
    sh.handle("set KERYX_MAX 10")
    sh.handle("set CACHE -1")
    cfg = config.load_config()
    assert (cfg.llm_backend, cfg.ollama_model, cfg.keryx_sync_hours, cfg.keryx_live_max_boards) == ("ollama", "qwen3:14b", 6.5, 10)
    assert cfg.board_cache_minutes == 15 and "≥ 0" in capsys.readouterr().out
