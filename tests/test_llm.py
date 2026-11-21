from types import SimpleNamespace

import pytest
from pydantic import BaseModel
from sqlmodel import select

from autoapply import db
from autoapply.llm import LLM, LLMError, LLMRefusal
from autoapply.models import LLMUsage


class Out(BaseModel):
    answer: str


class FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class FakeClient:
    def __init__(self, stop_reason="end_turn", parsed=Out(answer="hi")):
        self.kwargs = None
        usage = SimpleNamespace(input_tokens=100, output_tokens=20, cache_read_input_tokens=80,
                                cache_creation_input_tokens=0)
        self.message = SimpleNamespace(stop_reason=stop_reason, parsed_output=parsed, usage=usage,
                                       model="claude-opus-5-5")
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **kwargs):
        self.kwargs = kwargs
        return FakeStream(self.message)


def call(client):
    return LLM(model="claude-opus-5-5", client=client).parse(
        task="t", system="sys", content="hello", output_format=Out, effort="low"
    )


def test_request_shape_and_usage_logged():
    client = FakeClient()
    assert call(client) == Out(answer="hi")
    k = client.kwargs
    assert k["model"] == "claude-opus-5-5"
    assert k["output_format"] is Out
    assert k["output_config"] == {"effort": "low"}
    assert k["fallbacks"] == "default"
    assert k["cache_control"] == {"type": "ephemeral"}
    with db.session() as s:
        row = s.exec(select(LLMUsage)).one()
    assert (row.task, row.input_tokens, row.cache_read_tokens) == ("t", 100, 80)


def test_refusal_raises():
    with pytest.raises(LLMRefusal):
        call(FakeClient(stop_reason="refusal"))


def test_truncation_raises():
    with pytest.raises(LLMError, match="cut off"):
        call(FakeClient(stop_reason="max_tokens"))


def test_unparseable_raises():
    with pytest.raises(LLMError, match="could not be parsed"):
        call(FakeClient(parsed=None))


# ---------------------------------------------------------------------------
# Claude Code backend (subscription)
# ---------------------------------------------------------------------------

import base64 as _b64
import json as _json
import subprocess as _sp

from autoapply.llm.client import ClaudeCodeBackend


class FakeRun:
    def __init__(self, result: dict | None, returncode=0, stderr=""):
        self.result, self.returncode, self.stderr = result, returncode, stderr
        self.cmd = self.kwargs = None
        self.seen_files: dict[str, bytes] = {}

    def __call__(self, cmd, **kwargs):
        self.cmd, self.kwargs = cmd, kwargs
        from pathlib import Path
        for p in Path(kwargs["cwd"]).iterdir():  # capture temp files before cleanup
            self.seen_files[p.name] = p.read_bytes()
        out = _json.dumps(self.result) if self.result is not None else "garbage"
        return _sp.CompletedProcess(cmd, self.returncode, stdout=out, stderr=self.stderr)


def ok_result(data):
    return {"type": "result", "subtype": "success", "is_error": False, "stop_reason": "end_turn",
            "structured_output": data, "usage": {"input_tokens": 5, "output_tokens": 7},
            "modelUsage": {"claude-opus-5-5": {}}}


def cc_call(run, content="hello"):
    backend = ClaudeCodeBackend(model="opus", runner=run)
    return LLM(backend=backend).parse(task="t", system="sys", content=content,
                                      output_format=Out, effort="low")


@pytest.fixture
def have_claude(monkeypatch):
    monkeypatch.setattr(ClaudeCodeBackend, "available", staticmethod(lambda: True))


def test_cc_text_request(have_claude):
    run = FakeRun(ok_result({"answer": "hi"}))
    assert cc_call(run) == Out(answer="hi")
    cmd = run.cmd
    assert cmd[:2] == ["claude", "-p"]
    assert "--bare" not in cmd  # would bypass the subscription login
    assert _json.loads(cmd[cmd.index("--json-schema") + 1]) == Out.model_json_schema()
    assert cmd[cmd.index("--tools") + 1] == ""
    assert cmd[cmd.index("--effort") + 1] == "low"
    assert run.kwargs["input"] == "hello"
    with db.session() as s:
        row = s.exec(select(LLMUsage)).one()
    assert (row.model, row.output_tokens) == ("claude-opus-5-5", 7)


def test_cc_pdf_becomes_file_with_read_tool(have_claude):
    pdf = b"%PDF-1.4 fake"
    content = [
        {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                        "data": _b64.standard_b64encode(pdf).decode()}},
        {"type": "text", "text": "Convert it."},
    ]
    run = FakeRun(ok_result({"answer": "x"}))
    cc_call(run, content)
    assert run.seen_files == {"document_0.pdf": pdf}
    assert run.cmd[run.cmd.index("--tools") + 1] == "Read"
    assert "document_0.pdf" in run.kwargs["input"] and "Convert it." in run.kwargs["input"]


def test_cc_usage_limit(have_claude):
    run = FakeRun({"type": "result", "subtype": "error_during_execution", "is_error": True,
                   "result": "Claude AI usage limit reached", "api_error_status": 429})
    with pytest.raises(LLMError, match="usage limit"):
        cc_call(run)


def test_cc_bad_output(have_claude):
    with pytest.raises(LLMError, match="exit 1"):
        cc_call(FakeRun(None, returncode=1, stderr="boom"))
    with pytest.raises(LLMError, match="expected format"):
        cc_call(FakeRun(ok_result({"wrong": 1})))


def test_cc_missing_binary(monkeypatch):
    monkeypatch.setattr(ClaudeCodeBackend, "available", staticmethod(lambda: False))
    with pytest.raises(LLMError, match="isn't installed"):
        cc_call(FakeRun(ok_result({"answer": "hi"})))
