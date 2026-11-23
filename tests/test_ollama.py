"""Ollama backend against a mock Ollama server (no model runs)."""

import base64
import json

import httpx
import pytest
from pydantic import BaseModel
from sqlmodel import select

from autoapply import config, db
from autoapply.llm import LLM, LLMError, OllamaBackend
from autoapply.llm.client import ollama_host
from autoapply.models import LLMUsage

from .conftest import make_pdf


class Out(BaseModel):
    answer: str
    items: list[str] = []


class FakeOllama:
    def __init__(self, replies, caps=("completion",), models=("qwen3:14b",)):
        self.replies = list(replies)
        self.caps = list(caps)
        self.models = list(models)
        self.chats: list[dict] = []

    def handler(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in self.models]})
        if req.url.path == "/api/show":
            return httpx.Response(200, json={"capabilities": self.caps})
        if req.url.path == "/api/chat":
            body = json.loads(req.content)
            self.chats.append(body)
            reply = self.replies.pop(0)
            if isinstance(reply, httpx.Response):
                return reply
            return httpx.Response(200, json={"message": {"role": "assistant", "content": reply},
                                             "done_reason": "stop", "prompt_eval_count": 120, "eval_count": 30})
        return httpx.Response(404)

    def backend(self, **kw) -> OllamaBackend:
        return OllamaBackend(http=httpx.Client(transport=httpx.MockTransport(self.handler)), **kw)


def call(backend, content="hello", effort="high"):
    return LLM(backend=backend).parse(task="t", system="sys", content=content, output_format=Out, effort=effort)


def test_request_shape_and_usage():
    fake = FakeOllama(['{"answer": "hi", "items": ["a"]}'])
    assert call(fake.backend(model="qwen3:14b", num_ctx=8192)) == Out(answer="hi", items=["a"])
    body = fake.chats[0]
    assert body["model"] == "qwen3:14b" and body["stream"] is False
    assert body["format"] == Out.model_json_schema()
    assert body["options"]["num_ctx"] == 8192
    assert "think" not in body  # model has no thinking capability
    assert body["messages"][0]["role"] == "system" and '"answer"' in body["messages"][0]["content"]
    assert body["messages"][1] == {"role": "user", "content": "hello"}
    with db.session() as s:
        row = s.exec(select(LLMUsage)).one()
    assert (row.model, row.input_tokens, row.output_tokens) == ("ollama:qwen3:14b", 120, 30)


def test_picks_first_installed_model():
    fake = FakeOllama(['{"answer": "x"}'], models=["llama3.1:8b", "qwen3:14b"])
    call(fake.backend())
    assert fake.chats[0]["model"] == "llama3.1:8b"


def test_no_models_installed():
    fake = FakeOllama([], models=[])
    with pytest.raises(LLMError, match="ollama pull"):
        call(fake.backend())


@pytest.mark.parametrize("model,think,effort,expected", [
    ("qwen3:14b", False, "high", False),
    ("qwen3:14b", True, "high", True),
    ("gpt-oss:20b", False, "high", "low"),
    ("gpt-oss:20b", True, "medium", "medium"),
    ("gpt-oss:20b", True, "xhigh", "high"),
])
def test_think_setting(model, think, effort, expected):
    fake = FakeOllama(['{"answer": "x"}'], caps=["completion", "thinking"])
    call(fake.backend(model=model, think=think), effort=effort)
    assert fake.chats[0]["think"] == expected


def test_retries_once_with_the_validation_error():
    fake = FakeOllama(['{"wrong": 1}', '```json\n{"answer": "fixed"}\n```'])
    assert call(fake.backend(model="m")).answer == "fixed"
    retry = fake.chats[1]["messages"]
    assert retry[-2]["role"] == "assistant" and "didn't match the schema" in retry[-1]["content"]


def test_gives_up_after_second_bad_answer():
    fake = FakeOllama(["nope", "still nope"])
    with pytest.raises(LLMError, match="didn't match"):
        call(fake.backend(model="m"))


def test_missing_model_and_server_errors():
    fake = FakeOllama([httpx.Response(404, json={"error": "model not found"})])
    with pytest.raises(LLMError, match="ollama pull m"):
        call(fake.backend(model="m"))

    def down(req):
        raise httpx.ConnectError("refused")

    backend = OllamaBackend(model="m", http=httpx.Client(transport=httpx.MockTransport(down)))
    with pytest.raises(LLMError, match="ollama serve"):
        call(backend)


def test_pdf_documents_become_text(tmp_path):
    pdf = make_pdf(tmp_path / "r.pdf", ["Alex Rivera", "Backend Engineer at Acme", "Built billing API in Go"] * 3)
    block = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf",
                                            "data": base64.standard_b64encode(pdf.read_bytes()).decode()}}
    fake = FakeOllama(['{"answer": "ok"}'])
    call(fake.backend(model="m"), content=[block, {"type": "text", "text": "Convert this."}])
    user = fake.chats[0]["messages"][1]["content"]
    assert "<document>" in user and "Built billing API in Go" in user and user.endswith("Convert this.")


def test_cut_off_answer():
    fake = FakeOllama([])
    fake.replies = [httpx.Response(200, json={"message": {"content": '{"answ'}, "done_reason": "length"})]
    with pytest.raises(LLMError, match="cut off"):
        call(fake.backend(model="m"))


def test_config_selects_ollama(monkeypatch):
    cfg = config.Config(llm_backend="ollama", ollama_model="qwen3:14b", ollama_host="box:11434", ollama_num_ctx=4096)
    config.save_config(cfg)
    b = LLM().backend
    assert isinstance(b, OllamaBackend)
    assert (b.model, b.host, b.num_ctx) == ("qwen3:14b", "http://box:11434", 4096)


def test_host_resolution(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert ollama_host() == "http://localhost:11434"
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:9999")
    assert ollama_host() == "http://0.0.0.0:9999"
    assert ollama_host("https://gpu.lan/") == "https://gpu.lan"
