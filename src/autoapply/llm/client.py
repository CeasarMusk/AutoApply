"""One entry point (`LLM.parse`) for every AI step, with two interchangeable backends.

- ``claude-code`` (default): runs Claude Code headless (``claude -p``), so calls use your
  Claude Pro/Max subscription instead of paid API tokens. For personal use on your own
  machine; tools shared with other people should use their own API keys.
- ``api``: the Anthropic API via the official SDK (pay per token). Adds refusal fallbacks
  and prompt caching.

Both return a validated pydantic object and log token usage to sqlite.
"""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Protocol, TypeVar

import anthropic
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
# Facade
# ---------------------------------------------------------------------------


class LLM:
    def __init__(self, model: str | None = None, client: Any | None = None,
                 backend: Backend | None = None):
        cfg = config.load_config()
        if backend is not None:
            self.backend = backend
        elif client is not None or cfg.llm_backend == "api":
            self.backend = ApiBackend(model or cfg.model, client)
        else:
            self.backend = ClaudeCodeBackend(model or cfg.claude_code_model)

    def parse(self, *, task: str, system: str, content: str | list[dict[str, Any]],
              output_format: type[T], effort: str = "high", max_tokens: int = 16000) -> T:
        return self.backend.parse(task=task, system=system, content=content,
                                  output_format=output_format, effort=effort,
                                  max_tokens=max_tokens)
