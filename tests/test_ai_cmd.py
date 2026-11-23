from typer.testing import CliRunner

from autoapply import cli_ai, config
from autoapply.cli import app
from autoapply.llm import LLMError


def _offline(monkeypatch, cc=False, key=None, models=None):
    monkeypatch.setattr(cli_ai.ClaudeCodeBackend, "available", staticmethod(lambda: cc))
    if key:
        monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    else:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    def fake_models(host=""):
        if models is None:
            raise LLMError("down")
        return models

    monkeypatch.setattr(cli_ai, "ollama_models", fake_models)


def test_ai_lists_all_three_providers(monkeypatch):
    _offline(monkeypatch, cc=True, models=["qwen3:14b"])
    res = CliRunner().invoke(app, ["ai"])
    assert res.exit_code == 0, res.output
    out = " ".join(res.output.split())
    for needle in ["claude-code", "api", "ollama", "Anthropic API key", "ANTHROPIC_API_KEY isn't set", "(in use)",
                   "ai use"]:
        assert needle in out, needle


def test_ai_use_switches_and_sets_model(monkeypatch):
    _offline(monkeypatch, models=["llama3.1:8b"])
    res = CliRunner().invoke(app, ["ai", "use", "local", "--model", "llama3.1:8b"])
    assert res.exit_code == 0 and "Ollama" in res.output and "Not ready" not in res.output
    cfg = config.load_config()
    assert cfg.llm_backend == "ollama" and cfg.ollama_model == "llama3.1:8b"
    res = CliRunner().invoke(app, ["ai", "use", "api"])
    assert config.load_config().llm_backend == "api" and "ANTHROPIC_API_KEY" in res.output
    res = CliRunner().invoke(app, ["ai", "use", "gpt"])
    assert res.exit_code == 1 and "Unknown provider" in res.output


def test_status_flags_missing_ollama_model(monkeypatch):
    _offline(monkeypatch, models=["qwen3:14b"])
    cfg = config.Config(llm_backend="ollama", ollama_model="mistral")
    st = cli_ai.provider_status("ollama", cfg)
    assert not st.ready and "not installed" in st.detail
