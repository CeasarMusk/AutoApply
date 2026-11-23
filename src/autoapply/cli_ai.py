"""`ai`: see which AI provider AutoApply uses, check each one is ready, and switch between them."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

import questionary
import typer
from rich.console import Console
from rich.markup import escape as esc
from rich.console import Group

from . import config
from .llm import LLMError, ollama_models
from .llm.client import ClaudeCodeBackend

console = Console()
ai_app = typer.Typer(
    help="Choose the AI provider: Claude via your subscription (Claude Code), Claude via an API key, "
    "or a local Ollama model. Run `ai` alone to see them and switch.",
    invoke_without_command=True,
)

PROVIDERS = {
    "claude-code": ("Claude — your Claude subscription, via Claude Code",
                    "No per-token cost; best quality. Needs the `claude` CLI, logged in."),
    "api": ("Claude — Anthropic API key",
            "Pay per token. Needs ANTHROPIC_API_KEY set in your environment."),
    "ollama": ("Ollama — a model on your own machine",
               "Free and private, but slower and less capable. Needs Ollama running with a model pulled."),
}
CLAUDE_CODE_MODELS = ["opus", "sonnet", "haiku"]


@dataclass
class Status:
    ready: bool
    detail: str
    models: list[str] | None = None  # Ollama's installed models


def provider_status(key: str, cfg: config.Config | None = None) -> Status:
    cfg = cfg or config.load_config()
    if key == "claude-code":
        if ClaudeCodeBackend.available():
            return Status(True, f"`claude` found · model {cfg.claude_code_model}")
        return Status(False, "`claude` not found: install Claude Code, then run `claude` once to log in")
    if key == "api":
        if os.environ.get("ANTHROPIC_API_KEY"):
            return Status(True, f"ANTHROPIC_API_KEY is set · model {cfg.model}")
        return Status(False, "ANTHROPIC_API_KEY isn't set: `export ANTHROPIC_API_KEY=sk-ant-…` in your shell profile")
    try:
        models = ollama_models(cfg.ollama_host)
    except LLMError:
        return Status(False, "Ollama isn't running: install it from ollama.com and start it (`ollama serve`)", None)
    if not models:
        return Status(False, "Ollama is running but has no models: e.g. `ollama pull qwen3:14b`", [])
    using = cfg.ollama_model or models[0]
    missing = cfg.ollama_model and cfg.ollama_model not in models
    detail = f"{len(models)} model(s) installed · using {using}" + (" [red](not installed)[/red]" if missing else "")
    return Status(not missing, detail, models)


def status_table(cfg: config.Config) -> Group:
    """One block per provider: name (and whether it's in use), its `ai use` key, and whether it's set up."""
    lines = ["[bold]AI providers[/bold]"]
    for key, (label, _) in PROVIDERS.items():
        st = provider_status(key, cfg)
        in_use = key == cfg.llm_backend
        mark = "[bold green]▶[/bold green]" if in_use else " "
        lines.append(f"{mark} [bold]{esc(label)}[/bold]" + ("  [bold green](in use)[/bold green]" if in_use else ""))
        state = "[green]ready[/green]" if st.ready else "[yellow]not ready[/yellow]"
        lines.append(f"    [cyan]ai use {key}[/cyan] · {state} · {st.detail}")
    return Group(*lines)


def pick_provider(cfg: config.Config, *, heading: bool = True) -> bool:
    """Ask which provider to use (and its model), and update cfg. Returns False if cancelled."""
    statuses = {k: provider_status(k, cfg) for k in PROVIDERS}
    if heading:
        console.print("[bold]AutoApply uses AI to read your resume, tailor it to each job, and draft essay answers.[/bold]\n"
                      "Pick the provider. You can switch any time with the `ai` command.")
    default = cfg.llm_backend if config.config_path().exists() else next(
        (k for k in PROVIDERS if statuses[k].ready), "claude-code")
    choice = questionary.select(
        "Which AI provider should AutoApply use?",
        choices=[questionary.Choice(
            title=f"{label}  ({'ready' if statuses[k].ready else 'not set up yet'})",
            value=k, description=desc) for k, (label, desc) in PROVIDERS.items()],
        default=default,
        instruction="(↑/↓ then Enter)",
    ).ask()
    if choice is None:
        return False
    cfg.llm_backend = choice
    st = statuses[choice]
    if choice == "claude-code":
        cfg.claude_code_model = questionary.select(
            "Which Claude model? (opus is best; sonnet/haiku use less of your plan)",
            choices=CLAUDE_CODE_MODELS, default=cfg.claude_code_model if cfg.claude_code_model in CLAUDE_CODE_MODELS else "opus",
        ).ask() or cfg.claude_code_model
    elif choice == "api":
        model = questionary.text("API model id:", default=cfg.model).ask()
        cfg.model = (model or cfg.model).strip()
    elif choice == "ollama" and st.models:
        cfg.ollama_model = questionary.select(
            "Which Ollama model?", choices=st.models,
            default=cfg.ollama_model if cfg.ollama_model in st.models else st.models[0],
        ).ask() or cfg.ollama_model or st.models[0]
    if not st.ready:
        console.print(f"[yellow]Not ready yet: {st.detail}.[/yellow]")
        if choice == "ollama":
            console.print("[yellow]Then pick the model with `ai use ollama --model <name>`.[/yellow]")
    return True


@ai_app.callback()
def _ai_root(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return
    cfg = config.load_config()
    console.print(status_table(cfg))
    if not sys.stdin.isatty():
        console.print("Switch with `ai use claude-code|api|ollama [--model NAME]`.")
        return
    if not questionary.confirm("Switch provider or model?", default=False).ask():
        return
    if pick_provider(cfg, heading=False):
        config.save_config(cfg)
        console.print(f"[green]Now using[/green] {esc(PROVIDERS[cfg.llm_backend][0])}.")


@ai_app.command("show")
def ai_show() -> None:
    """Show every provider, whether it's set up, and which one is in use."""
    console.print(status_table(config.load_config()))


@ai_app.command("use")
def ai_use(
    provider: str = typer.Argument(..., help="claude-code (subscription), api (API key), or ollama (local)"),
    model: str = typer.Option(None, "--model", "-m", help="Model for that provider: opus/sonnet/haiku, an API "
                              "model id, or an Ollama model name."),
) -> None:
    """Switch to a provider without the menu, e.g. `ai use ollama --model qwen3:14b`."""
    aliases = {"claude": "claude-code", "subscription": "claude-code", "anthropic": "api", "local": "ollama"}
    key = aliases.get(provider.lower(), provider.lower())
    if key not in PROVIDERS:
        console.print(f"[red]Unknown provider '{esc(provider)}'. Use one of: {', '.join(PROVIDERS)}[/red]")
        raise typer.Exit(1)
    cfg = config.load_config()
    cfg.llm_backend = key
    if model:
        if key == "claude-code":
            cfg.claude_code_model = model
        elif key == "api":
            cfg.model = model
        else:
            cfg.ollama_model = model
    config.save_config(cfg)
    st = provider_status(key, cfg)
    console.print(f"[green]Now using[/green] {esc(PROVIDERS[key][0])}.")
    if not st.ready:
        console.print(f"[yellow]Not ready yet: {st.detail}.[/yellow]")


def register(app: typer.Typer) -> None:
    app.add_typer(ai_app, name="ai")
