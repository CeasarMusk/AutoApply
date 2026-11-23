"""One entry point (`LLM.parse`) for every AI step, with three interchangeable backends.

- ``claude-code`` (default): runs Claude Code headless (``claude -p``), so calls use your
  Claude Pro/Max subscription instead of paid API tokens. For personal use on your own
  machine; tools shared with other people should use their own API keys.
- ``api``: the Anthropic API via the official SDK (pay per token). Adds refusal fallbacks
  and prompt caching.
- ``ollama``: a model running locally in Ollama (free and private; weaker, so its output goes
  through the same validation and fact guards).

All return a validated pydantic object and log token usage to sqlite.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Protocol, TypeVar

import anthropic
import httpx
from pydantic import BaseModel, ValidationError

from .. import config

T = TypeVar("T", bound=BaseModel)

FALLBACK_BETA = "server-side-fallback-2026-07-01"
CLAUDE_CODE_TIMEOUT_S = 600


class LLMError(RuntimeError):
    pass


class LLMRefusal(LLMError):
    pass


def _log_usage(task: str, model: str, input_tokens: int, output_tokens: int,
               cache_read: int, cache_write: int) -> None:
    try:
        from .. import db
        from ..models import LLMUsage

        with db.session() as s:
            s.add(LLMUsage(task=task, model=model, input_tokens=input_tokens,
                           output_tokens=output_tokens, cache_read_tokens=cache_read,
                           cache_write_tokens=cache_write))
            s.commit()
    except Exception:
        # Usage logging must never break the actual task.
        pass


class Backend(Protocol):
    def parse(self, *, task: str, system: str, content: str | list[dict[str, Any]],
              output_format: type[T], effort: str, max_tokens: int) -> T: ...


# ---------------------------------------------------------------------------
# Claude Code (subscription)
# ---------------------------------------------------------------------------


class ClaudeCodeBackend:
    def __init__(self, model: str = "opus", runner: Any = subprocess.run):
        self.model = model
        self._run = runner

    @staticmethod
    def available() -> bool:
        return shutil.which("claude") is not None

    def parse(self, *, task, system, content, output_format, effort="high", max_tokens=16000):
        if not self.available():
            raise LLMError(
                "Claude Code isn't installed. Install it (https://claude.com/claude-code) and run "
                "`claude` once to log in with your subscription, or set llm_backend: api in "
                f"{config.config_path()}."
            )
        with tempfile.TemporaryDirectory(prefix="autoapply-") as tmp:
            prompt, has_files = self._prompt(content, Path(tmp))
            cmd = [
                "claude", "-p",
                "--output-format", "json",
                "--json-schema", json.dumps(output_format.model_json_schema()),
                "--system-prompt", system,
                "--model", self.model,
                "--effort", effort,
                # Only the Read tool, and only when there's a file to read.
                "--tools", "Read" if has_files else "",
                *(["--allowedTools", "Read"] if has_files else []),
                # Don't pick up the user's settings, hooks, MCP servers or session history.
                "--setting-sources", "",
                "--strict-mcp-config",
                "--no-session-persistence",
            ]
            # NB: never pass --bare: it ignores the subscription login.
            try:
                proc = self._run(cmd, input=prompt, capture_output=True, text=True,
                                 cwd=tmp, timeout=CLAUDE_CODE_TIMEOUT_S)
            except subprocess.TimeoutExpired as e:
                raise LLMError(f"Claude Code took longer than {CLAUDE_CODE_TIMEOUT_S}s on {task}.") from e

        result = self._result_json(proc.stdout)
        if result is None:
            detail = (proc.stderr or proc.stdout or "").strip()[-500:]
            raise LLMError(f"Claude Code failed on {task} (exit {proc.returncode}): {detail}")

        self._log(task, result)

        if result.get("is_error") or result.get("subtype") != "success":
            msg = str(result.get("result") or result.get("subtype") or "unknown error")
            if result.get("api_error_status") == 429 or "limit" in msg.lower():
                raise LLMError(
                    "Your Claude subscription usage limit was reached. Wait for it to reset, "
                    f"then try again. ({msg})"
                )
            if "log in" in msg.lower() or "login" in msg.lower() or result.get("api_error_status") == 401:
                raise LLMError("Claude Code isn't logged in. Run `claude` and log in, then retry.")
            raise LLMError(f"Claude Code error on {task}: {msg}")
        if result.get("stop_reason") == "refusal":
            raise LLMRefusal(f"The model declined the {task} request.")

        data = result.get("structured_output")
        if data is None:
            raise LLMError(f"The {task} response had no structured output.")
        try:
            return output_format.model_validate(data)
        except ValidationError as e:
            raise LLMError(f"The {task} response didn't match the expected format: {e}") from e

    @staticmethod
    def _prompt(content: str | list[dict[str, Any]], tmp: Path) -> tuple[str, bool]:
        if isinstance(content, str):
            return content, False
        parts: list[str] = []
        has_files = False
        for i, block in enumerate(content):
            if block.get("type") == "text":
                parts.append(block["text"])
            elif block.get("type") == "document" and block["source"].get("type") == "base64":
                path = tmp / f"document_{i}.pdf"
                path.write_bytes(base64.standard_b64decode(block["source"]["data"]))
                parts.append(f"The document is at {path}. Read the whole file with the Read tool.")
                has_files = True
            else:
                raise LLMError(f"Unsupported content block for Claude Code: {block.get('type')}")
        return "\n\n".join(parts), has_files

    @staticmethod
    def _result_json(stdout: str) -> dict | None:
        for line in reversed((stdout or "").strip().splitlines()):
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and obj.get("type") == "result":
                return obj
        return None

    def _log(self, task: str, result: dict) -> None:
        u = result.get("usage") or {}
        model = next(iter(result.get("modelUsage") or {}), self.model)
        _log_usage(task, model, u.get("input_tokens", 0), u.get("output_tokens", 0),
                   u.get("cache_read_input_tokens", 0), u.get("cache_creation_input_tokens", 0))


# ---------------------------------------------------------------------------
# Anthropic API (pay per token)
# ---------------------------------------------------------------------------


class ApiBackend:
    def __init__(self, model: str, client: Any | None = None):
        self.model = model
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = anthropic.Anthropic()
        return self._client

    def parse(self, *, task, system, content, output_format, effort="high", max_tokens=16000):
        no_creds = LLMError("No Anthropic API credentials found. Set ANTHROPIC_API_KEY.")
        try:
            # Streaming avoids HTTP timeouts on long outputs (the SDK requires it for
            # large max_tokens); we just wait for the final, parsed message.
            with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=max_tokens,
                betas=[FALLBACK_BETA],
                fallbacks="default",
                cache_control={"type": "ephemeral"},
                output_config={"effort": effort},
                system=system,
                messages=[{"role": "user", "content": content}],
                output_format=output_format,
            ) as stream:
                resp = stream.get_final_message()
        except TypeError as e:
            # Raised by the SDK at request time when no credentials can be found.
            if "authentication" not in str(e).lower():
                raise
            raise no_creds from e
        except anthropic.AuthenticationError as e:
            raise LLMError("Anthropic API rejected the credentials.") from e
        except anthropic.RateLimitError as e:
            raise LLMError("Rate limited by the Anthropic API; try again shortly.") from e
        except anthropic.APIStatusError as e:
            raise LLMError(f"Anthropic API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise LLMError("Could not reach the Anthropic API.") from e

        usage = getattr(resp, "usage", None)
        if usage is not None:
            _log_usage(task, getattr(resp, "model", self.model), usage.input_tokens or 0,
                       usage.output_tokens or 0, getattr(usage, "cache_read_input_tokens", 0) or 0,
                       getattr(usage, "cache_creation_input_tokens", 0) or 0)

        if resp.stop_reason == "refusal":
            raise LLMRefusal(f"The model declined the {task} request.")
        if resp.stop_reason == "max_tokens":
            raise LLMError(f"The {task} response was cut off (max_tokens={max_tokens}).")
        if resp.parsed_output is None:
            raise LLMError(f"The {task} response could not be parsed.")
        return resp.parsed_output


# ---------------------------------------------------------------------------
# Ollama (local models)
# ---------------------------------------------------------------------------

OLLAMA_DEFAULT_HOST = "http://localhost:11434"
OLLAMA_TIMEOUT_S = 900  # local models on modest hardware can take minutes on a long resume


def ollama_host(configured: str = "") -> str:
    host = (configured or os.environ.get("OLLAMA_HOST") or OLLAMA_DEFAULT_HOST).strip().rstrip("/")
    if "://" not in host:
        host = f"http://{host}"
    return host


def ollama_models(host: str = "", http: Any | None = None) -> list[str]:
    """Names of the models installed in Ollama (raises LLMError if it isn't running)."""
    url = f"{ollama_host(host)}/api/tags"
    try:
        r = (http or httpx).get(url, timeout=5)
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise LLMError(f"Ollama isn't reachable at {ollama_host(host)}. Start it with `ollama serve`. ({e})") from e
    return [m.get("name") or m.get("model") for m in r.json().get("models") or []]


class OllamaBackend:
    """A model served by a local Ollama. Uses Ollama's JSON-schema structured outputs.

    Local models are weaker than Claude, so the schema is also spelled out in the system prompt,
    and an answer that doesn't validate gets one retry with the error. Every guard that keeps
    tailoring and essays honest (tailor.check_rewrite, id-based picking) runs after this, the same
    as with Claude.
    """

    def __init__(self, model: str = "", host: str = "", num_ctx: int = 16384, think: bool = False,
                 http: Any | None = None):
        self.model = model
        self.host = ollama_host(host)
        self.num_ctx = num_ctx
        self.think = think
        self._http = http
        self._caps: dict[str, list[str]] = {}

    @property
    def http(self) -> Any:
        if self._http is None:
            self._http = httpx.Client(timeout=httpx.Timeout(OLLAMA_TIMEOUT_S, connect=5.0))
        return self._http

    def _model(self) -> str:
        if not self.model:
            models = ollama_models(self.host, self.http)
            if not models:
                raise LLMError("Ollama has no models installed. Run e.g. `ollama pull qwen3:14b`.")
            self.model = models[0]
        return self.model

    def _capabilities(self, model: str) -> list[str]:
        if model not in self._caps:
            try:
                r = self.http.post(f"{self.host}/api/show", json={"model": model})
                self._caps[model] = (r.json().get("capabilities") or []) if r.status_code == 200 else []
            except (httpx.HTTPError, ValueError):
                self._caps[model] = []
        return self._caps[model]

    def _think_value(self, model: str, effort: str) -> bool | str | None:
        if "thinking" not in self._capabilities(model):
            return None  # sending `think` to a model without it is an error
        if model.startswith("gpt-oss"):
            # gpt-oss always reasons; it takes a level instead of on/off.
            if not self.think:
                return "low"
            return {"low": "low", "medium": "medium"}.get(effort, "high")
        return self.think

    @staticmethod
    def _prompt(content: str | list[dict[str, Any]]) -> str:
        if isinstance(content, str):
            return content
        parts: list[str] = []
        for block in content:
            if block.get("type") == "text":
                parts.append(block["text"])
            elif block.get("type") == "document" and block["source"].get("type") == "base64":
                parts.append(f"<document>\n{_pdf_text(block['source']['data'])}\n</document>")
            else:
                raise LLMError(f"Unsupported content block for Ollama: {block.get('type')}")
        return "\n\n".join(parts)

    def parse(self, *, task, system, content, output_format, effort="high", max_tokens=16000):
        model = self._model()
        schema = output_format.model_json_schema()
        sys_prompt = (
            f"{system}\n\nReply with a single JSON object that matches this JSON schema, and nothing else:\n"
            f"{json.dumps(schema)}"
        )
        messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": self._prompt(content)}]
        body: dict[str, Any] = {
            "model": model, "messages": messages, "stream": False, "format": schema,
            "options": {"num_ctx": self.num_ctx, "num_predict": max_tokens, "temperature": 0.2},
        }
        think = self._think_value(model, effort)
        if think is not None:
            body["think"] = think

        last_error = ""
        for attempt in range(2):
            data = self._chat(task, body)
            text = ((data.get("message") or {}).get("content") or "").strip()
            _log_usage(task, f"ollama:{model}", data.get("prompt_eval_count") or 0, data.get("eval_count") or 0, 0, 0)
            if data.get("done_reason") == "length":
                raise LLMError(f"The {task} response was cut off (num_predict={max_tokens}); "
                               f"try a larger OLLAMA_CTX or a shorter input.")
            try:
                return output_format.model_validate_json(_strip_fences(text))
            except ValidationError as e:
                last_error = str(e)
                if attempt == 0:
                    body["messages"] = messages + [
                        {"role": "assistant", "content": text[:4000]},
                        {"role": "user", "content": "That JSON didn't match the schema:\n"
                                                    f"{last_error[:2000]}\nReply again with corrected JSON only."},
                    ]
        raise LLMError(f"The local model's {task} answer didn't match the expected format: {last_error[:500]}")

    def _chat(self, task: str, body: dict) -> dict:
        url = f"{self.host}/api/chat"
        try:
            r = self.http.post(url, json=body)
        except httpx.TimeoutException as e:
            raise LLMError(f"Ollama took longer than {OLLAMA_TIMEOUT_S}s on {task}.") from e
        except httpx.HTTPError as e:
            raise LLMError(f"Ollama isn't reachable at {self.host}. Start it with `ollama serve`. ({e})") from e
        if r.status_code == 404:
            raise LLMError(f"Ollama doesn't have the model '{body['model']}'. Run `ollama pull {body['model']}`, "
                           "or `set OLLAMA_MODEL` to one from `ollama list`.")
        if r.status_code >= 400:
            try:
                msg = r.json().get("error") or r.text
            except ValueError:
                msg = r.text
            raise LLMError(f"Ollama error on {task}: {msg[:300]}")
        try:
            return r.json()
        except ValueError as e:
            raise LLMError(f"Ollama returned something that isn't JSON on {task}.") from e


def _strip_fences(text: str) -> str:
    """Some models wrap JSON in ```json fences even in format mode."""
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


def _pdf_text(b64: str) -> str:
    import io

    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(base64.standard_b64decode(b64)))
        text = "\n\n".join((p.extract_text() or "") for p in reader.pages).strip()
    except Exception as e:  # pypdf raises many types on odd files
        raise LLMError(f"Couldn't read the PDF's text for the local model: {e}") from e
    if len(text) < 50:
        raise LLMError("This PDF has no extractable text (scanned?). Local models can't read images; "
                       "export a text PDF or use the Claude backend.")
    return text


# ---------------------------------------------------------------------------
# Facade
# ---------------------------------------------------------------------------


def backend_label() -> str:
    """Short name of the configured AI, for status messages ("Tailoring … with <label>")."""
    cfg = config.load_config()
    if cfg.llm_backend == "ollama":
        return f"{cfg.ollama_model or 'a local model'} (Ollama)"
    return "Claude"


class LLM:
    def __init__(self, model: str | None = None, client: Any | None = None,
                 backend: Backend | None = None):
        cfg = config.load_config()
        if backend is not None:
            self.backend = backend
        elif client is None and cfg.llm_backend == "ollama":
            self.backend = OllamaBackend(model or cfg.ollama_model, cfg.ollama_host,
                                         num_ctx=cfg.ollama_num_ctx, think=cfg.ollama_think)
        elif client is not None or cfg.llm_backend == "api":
            self.backend = ApiBackend(model or cfg.model, client)
        else:
            self.backend = ClaudeCodeBackend(model or cfg.claude_code_model)

    def parse(self, *, task: str, system: str, content: str | list[dict[str, Any]],
              output_format: type[T], effort: str = "high", max_tokens: int = 16000) -> T:
        return self.backend.parse(task=task, system=system, content=content,
                                  output_format=output_format, effort=effort,
                                  max_tokens=max_tokens)
