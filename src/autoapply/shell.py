"""Interactive console (msfconsole-style).

Every regular CLI command works here without the `autoapply` prefix. On top of that:
  use <id> / back        select a job; the prompt shows it and job commands default to it
  set / unset / show options   persistent search & AI settings
  show jobs|companies|resume|usage|options
  help, banner, clear, history, exit
"""

from __future__ import annotations

import random
import shlex
from importlib.metadata import PackageNotFoundError, version

import typer
from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import NestedCompleter, WordCompleter
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.markup import escape as esc
from rich.table import Table
from sqlmodel import func, select

from . import config, db
from .llm.client import ClaudeCodeBackend
from .models import Job
from .resume import store

console = Console()

try:
    VERSION = version("autoapply")
except PackageNotFoundError:  # pragma: no cover
    VERSION = "dev"

BANNER = r"""
     _         _            _                _
    / \  _   _| |_ ___     / \   _ __  _ __ | |_   _
   / _ \| | | | __/ _ \   / _ \ | '_ \| '_ \| | | | |
  / ___ \ |_| | || (_) | / ___ \| |_) | |_) | | |_| |
 /_/   \_\__,_|\__\___/ /_/   \_\ .__/| .__/|_|\__, |
                                |_|   |_|      |___/
"""

TIPS = [
    "Type `search` and pick ★ Jobs like my resume.",
    "`use 12` then `apply`: the form gets filled, you review and click Submit.",
    "`queue build` then `queue run` applies to your search results one after another, best match or newest first.",
    "`answers setup` answers the usual work-authorization and EEO questions once, up front.",
    "`search --last` repeats your last search without the menu.",
    "`use 12` selects job 12, then `info`, `open`, `save` act on it.",
    "`jobs --status saved` shows your shortlist.",
    "`profile` shows what AutoApply thinks you're looking for, based on your resume.",
    "Press Tab to complete commands, and ↑ to search your command history.",
    "Internships and new-grad roles from Keryx are searched offline; `keryx sync` grabs the latest list.",
    "`ai` shows the AI providers (Claude subscription, Claude API key, local Ollama) and switches between them.",
    "`companies add <careers link>` works for Workday, Oracle, SmartRecruiters, Workable and BambooHR boards.",
]

# name -> (config path, kind, description)
OPTIONS: dict[str, tuple[str, str, str]] = {
    "STARTER": ("use_starter_companies", "bool", "Search the built-in list of ~60 tech companies"),
    "REMOTEOK": ("use_remoteok", "bool", "Also search Remote OK's remote-jobs feed"),
    "GITHUB": ("use_github_lists", "bool", "Also search SimplifyJobs' GitHub internship/new-grad lists"),
    "KERYX": ("use_keryx", "bool", "Search Keryx's offline list of US internships / new-grad roles"),
    "KERYX_LIVE": ("keryx_live", "bool", "Also check the boards behind Keryx matches live (fresher, more requests)"),
    "KERYX_MAX": ("keryx_live_max_boards", "int", "Most boards checked live per search"),
    "KERYX_SYNC": ("keryx_sync_hours", "float", "Re-download the Keryx list when older than this many hours (0: manual)"),
    "CACHE": ("board_cache_minutes", "float", "Reuse a board's listing fetched within this many minutes"),
    "BACKEND": ("llm_backend", "choice:claude-code,api,ollama", "AI provider: claude-code (Claude subscription), api (Claude API key), ollama (local). See `ai`"),
    "MODEL": ("claude_code_model", "str", "Claude Code model alias (opus, sonnet, haiku)"),
    "OLLAMA_MODEL": ("ollama_model", "str", "Ollama model, e.g. qwen3:14b (empty: first installed)"),
    "OLLAMA_HOST": ("ollama_host", "str", "Ollama server URL (empty: $OLLAMA_HOST or localhost:11434)"),
    "OLLAMA_CTX": ("ollama_num_ctx", "int", "Ollama context window in tokens"),
    "OLLAMA_THINK": ("ollama_think", "bool", "Let thinking models reason before answering (better, slower)"),
    "TAILOR": ("tailor_resume", "bool", "Tailor your resume to each job when applying"),
    "OPTIONAL": ("ask_optional", "bool", "Ask about optional questions too (off: leave unknown ones blank)"),
}

# Commands that act on a job id; with a job selected via `use`, the id can be omitted.
JOB_SHORTCUTS = {
    "info": ["job", "show"],
    "open": ["job", "open"],
    "save": ["job", "save"],
    "ignore": ["job", "ignore"],
    "reset": ["job", "reset"],
    "apply": ["apply"],
    "tailor": ["tailor"],
}

HELP: list[tuple[str, list[tuple[str, str]]]] = [
    ("Search", [
        ("search", "Pick what to look for from a menu (★ jobs like my resume, a field, or keywords)"),
        ("search --last", "Repeat the last search"),
        ("search <keywords> [--level entry] [-l Austin] [--remote]", "Search without the menu"),
        ("jobs [-s saved] [-g text] [--remote]", "List saved jobs, best match first"),
        ("profile [--refresh]", "What your resume says you should search for"),
    ]),
    ("Selected job", [
        ("use <id> / back", "Select / deselect a job"),
        ("apply [id]", "Fill the application in a browser; you review and submit"),
        ("apply [id] --dry-run", "Preview what would be filled, invisibly"),
        ("tailor [id] [--preview] [--redo]", "Make (and open) a resume tailored to the job"),
        ("info [id]", "Details and description"),
        ("open [id]", "Open the posting in your browser"),
        ("save | ignore | reset [id]", "Shortlist / hide / clear"),
    ]),
    ("Apply queue", [
        ("queue build [--from last|saved|all] [--order match|recent]", "Queue the matching jobs to apply to one after another"),
        ("queue / queue show", "See what's queued, in order"),
        ("queue run", "Apply to each in turn: you review and submit every form"),
        ("queue order match|recent", "Best match first, or most recently posted first"),
        ("queue add | remove [id…] / queue clear", "Change what's queued"),
    ]),
    ("Answers", [
        ("answers setup", "Answer the common questions up front (work auth, EEO…)"),
        ("answers list | set <key> <value> | forget <key|#>", "See / change saved answers"),
    ]),
    ("Resume", [
        ("resume import <file.pdf>", "Turn your resume PDF into the master resume"),
        ("resume show", "View the master resume"),
        ("resume github <profile link> [--deep]", "Add projects from your GitHub repos (READMEs; --deep: dependencies + AI bullets)"),
        ("resume transcript <file.pdf>", "Save your transcript for forms that ask for one"),
    ]),
    ("More", [
        ("companies add <slug|board link…> | list | remove", "Extra companies beyond the built-in list"),
        ("keryx status | sync", "Offline internship / new-grad list: freshness, update now"),
        ("ai / ai use claude-code|api|ollama [-m model]", "Pick the AI provider: Claude subscription, Claude API key, or local Ollama"),
        ("set <OPTION> <value> | unset | show options", "Settings: GITHUB, KERYX, KERYX_LIVE, BACKEND, OLLAMA_MODEL, TAILOR…"),
        ("usage", "AI token usage"),
        ("banner, history, clear, help <command>, exit", ""),
    ]),
]


class Shell:
    def __init__(self, app: typer.Typer):
        self.cli = typer.main.get_command(app)
        self.current_job: Job | None = None
        config.home().mkdir(parents=True, exist_ok=True)
        self.session: PromptSession = PromptSession(
            history=FileHistory(str(config.home() / "history")),
            auto_suggest=AutoSuggestFromHistory(),
            completer=self._completer(),
            complete_while_typing=False,
        )

    # -- prompt ------------------------------------------------------------

    def _prompt(self) -> FormattedText:
        parts = [("underline", "autoapply")]
        if self.current_job is not None:
            j = self.current_job
            label = f"{j.id}:{j.company}"
            parts += [("", " job("), ("ansired bold", label), ("", ")")]
        parts.append(("", " > "))
        return FormattedText(parts)

    def _completer(self) -> NestedCompleter:
        def leaf(cmd):
            flags = [
                o
                for p in cmd.params
                if getattr(p, "param_type_name", "") == "option"
                for o in [*p.opts, *getattr(p, "secondary_opts", [])]
            ]
            return WordCompleter(flags + ["--help"]) if flags else None

        tree: dict = {}
        for name, cmd in self.cli.commands.items():
            if hasattr(cmd, "commands"):
                tree[name] = {sub: leaf(c) for sub, c in cmd.commands.items()}
            else:
                tree[name] = leaf(cmd)
        opts = {k: None for k in OPTIONS}
        tree.update({
            "set": opts,
            "unset": opts,
            "show": {k: None for k in ["options", "jobs", "companies", "resume", "usage", "profile"]},
            "use": None, "back": None, "help": None, "?": None, "banner": None,
            "history": None, "clear": None, "exit": None, "quit": None,
            **{k: None for k in JOB_SHORTCUTS},
        })
        return NestedCompleter.from_nested_dict(tree)

    # -- loop --------------------------------------------------------------

    def run(self) -> None:
        self.banner()
        while True:
            try:
                line = self.session.prompt(self._prompt())
            except KeyboardInterrupt:
                continue
            except EOFError:
                break
            if not self.handle(line):
                break
        console.print("Bye.")

    def handle(self, line: str) -> bool:
        """Run one line. Returns False when the shell should exit."""
        line = line.strip()
        if not line or line.startswith("#"):
            return True
        try:
            argv = shlex.split(line)
        except ValueError as e:
            console.print(f"[red]Can't parse that: {esc(str(e))}[/red]")
            return True
        cmd, args = argv[0], argv[1:]

        builtin = {
            "exit": None, "quit": None,
            "help": self.help, "?": self.help,
            "banner": lambda *_: self.banner(),
            "clear": lambda *_: console.clear(), "cls": lambda *_: console.clear(),
            "history": self.history,
            "use": self.use, "back": self.back,
            "set": self.set_option, "unset": self.unset_option,
            "show": self.show,
        }
        if cmd in ("exit", "quit"):
            return False
        if cmd in builtin:
            builtin[cmd](args)
            return True
        if cmd in JOB_SHORTCUTS:
            argv = JOB_SHORTCUTS[cmd] + self._with_job_id(args)
        elif cmd == "job" and args and args[0] in ("show", "open", "save", "ignore", "reset"):
            argv = ["job", args[0]] + self._with_job_id(args[1:])
        elif cmd == "queue" and args and args[0] in ("add", "remove"):
            argv = ["queue", args[0]] + self._with_job_id(args[1:])
        self.dispatch(argv)
        if self.current_job is not None:
            self._refresh_job()
        return True

    def _with_job_id(self, args: list[str]) -> list[str]:
        if any(a.isdigit() for a in args) or "--help" in args:
            return args
        if self.current_job is None:
            return args  # the command will report the missing id
        return [str(self.current_job.id), *args]

    def dispatch(self, argv: list[str]) -> None:
        if argv[0] in ("shell", "console"):
            return
        try:
            self.cli.main(args=argv, prog_name="", standalone_mode=False)
        except typer.TyperException as e:  # usage errors: bad option, missing argument, …
            console.print(f"[red]{esc(e.format_message())}[/red]")
            ctx = getattr(e, "ctx", None)
            path = ctx.command_path.strip() if ctx is not None else ""
            console.print(f"[dim]Try: {esc(path)} --help[/dim]" if path else "[dim]Type `help` for commands.[/dim]")
        except typer.Exit:
            pass
        except (typer.Abort, KeyboardInterrupt):
            console.print("\n[yellow]Interrupted.[/yellow]")
        except Exception as e:  # keep the console alive on unexpected errors
            console.print(f"[red]Error: {esc(type(e).__name__)}: {esc(str(e))}[/red]")

    # -- builtins ----------------------------------------------------------

    def banner(self) -> None:
        console.print(f"[bold cyan]{esc(BANNER)}[/bold cyan]", highlight=False)
        cfg = config.load_config()
        try:
            r = store.load()
            bullets = sum(len(e.bullets) for e in r.experience)
            resume = f"{r.contact.name or '(no name)'} — {len(r.experience)} jobs, {bullets} bullets"
        except FileNotFoundError:
            resume = "[yellow]none yet — run `resume import <file>` or `resume build`[/yellow]"
        except Exception:
            resume = "[red]unreadable — run `resume validate`[/red]"
        if cfg.transcript_file:
            resume += " · transcript saved"
        with db.session() as s:
            total = s.exec(select(func.count()).select_from(Job).where(Job.status.not_in(["ignored", "closed"]))).one()
            new = s.exec(select(func.count()).select_from(Job).where(Job.status == "new")).one()
            saved = s.exec(select(func.count()).select_from(Job).where(Job.status == "saved")).one()
        if cfg.llm_backend == "claude-code":
            ai = f"Claude subscription via Claude Code ({cfg.claude_code_model})"
            if not ClaudeCodeBackend.available():
                ai += " [red]— `claude` not found[/red]"
        elif cfg.llm_backend == "ollama":
            ai = f"local Ollama ({cfg.ollama_model or 'first installed model'})"
        else:
            ai = f"Anthropic API ({cfg.model})"
        from . import sources

        n_companies = len(sources.search_targets(cfg))
        lines = [
            f"       =[ [bold]autoapply v{VERSION}[/bold]",
            f"+ -- --=[ resume: {resume}",
            f"+ -- --=[ {n_companies} job boards{' + Keryx offline list' if cfg.use_keryx else ''} · {total} jobs saved · [green]{new} new[/green] · [yellow]{saved} shortlisted[/yellow]",
            f"+ -- --=[ AI: {ai} [dim]· `ai` to switch provider[/dim]",
        ]
        for l in lines:
            console.print(l, highlight=False)
        console.print(f"\n[dim]Tip: {esc(random.choice(TIPS))}  Type `help` for commands.[/dim]\n")

    def help(self, args: list[str]) -> None:
        if args:
            self.dispatch([*args, "--help"])
            return
        for section, rows in HELP:
            t = Table(title=section, title_justify="left", title_style="bold", show_header=False, box=None, padding=(0, 2))
            t.add_column(style="cyan", no_wrap=True)
            t.add_column()
            for c, d in rows:
                t.add_row(c, d)
            console.print(t)
            console.print()

    def history(self, args: list[str]) -> None:
        items = list(self.session.history.get_strings())[-20:]
        for i, h in enumerate(items, 1):
            console.print(f"[dim]{i:>3}[/dim]  {esc(h)}")

    def use(self, args: list[str]) -> None:
        if not args or not args[0].isdigit():
            console.print("Usage: use <job id>   (see `jobs`)")
            return
        with db.session() as s:
            job = s.get(Job, int(args[0]))
        if job is None:
            console.print(f"[red]No job with id {esc(args[0])}.[/red]")
            return
        self.current_job = job
        console.print(f"Selected [cyan]{job.id}[/cyan]: {esc(job.title)} @ {esc(job.company)}. "
                      "[dim]Try `info`, `open`, `save`; `back` to deselect.[/dim]")

    def back(self, args: list[str]) -> None:
        self.current_job = None

    def _refresh_job(self) -> None:
        with db.session() as s:
            self.current_job = s.get(Job, self.current_job.id) if self.current_job else None

    # options

    @staticmethod
    def _get_path(cfg, path: str):
        obj = cfg
        for part in path.split("."):
            obj = getattr(obj, part)
        return obj

    @staticmethod
    def _set_path(cfg, path: str, value) -> None:
        *parents, last = path.split(".")
        obj = cfg
        for part in parents:
            obj = getattr(obj, part)
        setattr(obj, last, value)

    def set_option(self, args: list[str]) -> None:
        if not args:
            self.show(["options"])
            return
        name = args[0].upper()
        if name not in OPTIONS:
            console.print(f"[red]Unknown option {esc(name)}.[/red] Options: {', '.join(OPTIONS)}")
            return
        if len(args) < 2:
            console.print(f"Usage: set {name} <value>")
            return
        path, kind, _ = OPTIONS[name]
        raw = " ".join(args[1:])
        try:
            value = self._convert(kind, raw)
        except ValueError as e:
            console.print(f"[red]{esc(str(e))}[/red]")
            return
        cfg = config.load_config()
        self._set_path(cfg, path, value)
        config.save_config(cfg)
        console.print(f"{name} => {esc(self._fmt(value))}")

    def unset_option(self, args: list[str]) -> None:
        if not args or args[0].upper() not in OPTIONS:
            console.print(f"Usage: unset <OPTION>  ({', '.join(OPTIONS)})")
            return
        name = args[0].upper()
        path, kind, _ = OPTIONS[name]
        cfg = config.load_config()
        default = self._get_path(config.Config(), path)
        self._set_path(cfg, path, default)
        config.save_config(cfg)
        console.print(f"Unset {name} (now {esc(self._fmt(default)) or 'empty'})")

    @staticmethod
    def _convert(kind: str, raw: str):
        if kind == "list":
            return [x.strip() for x in raw.split(",") if x.strip()]
        if kind == "bool":
            low = raw.strip().lower()
            if low in ("true", "yes", "y", "1", "on"):
                return True
            if low in ("false", "no", "n", "0", "off"):
                return False
            raise ValueError("Use true or false.")
        if kind == "int":
            if not raw.strip().isdigit():
                raise ValueError("Use a whole number.")
            return int(raw)
        if kind == "float":
            try:
                value = float(raw)
            except ValueError:
                raise ValueError("Use a number.") from None
            if value < 0:
                raise ValueError("Use a number ≥ 0.")
            return value
        if kind.startswith("choice:"):
            choices = kind.split(":", 1)[1].split(",")
            if raw.strip() not in choices:
                raise ValueError(f"Use one of: {', '.join(choices)}")
            return raw.strip()
        return raw.strip()

    @staticmethod
    def _fmt(value) -> str:
        if isinstance(value, list):
            return ", ".join(value) if value else ""
        if value is None:
            return ""
        return str(value)

    def show(self, args: list[str]) -> None:
        what = args[0].lower() if args else ""
        if what == "options":
            cfg = config.load_config()
            t = Table(title="Options", title_justify="left")
            t.add_column("Name", style="cyan")
            t.add_column("Current Setting")
            t.add_column("Description", style="dim")
            for name, (path, _, desc) in OPTIONS.items():
                t.add_row(name, esc(self._fmt(self._get_path(cfg, path))), desc)
            console.print(t)
        elif what == "jobs":
            self.dispatch(["jobs", *args[1:]])
        elif what == "companies":
            self.dispatch(["companies", "list"])
        elif what == "resume":
            self.dispatch(["resume", "show"])
        elif what in ("usage", "profile"):
            self.dispatch([what])
        else:
            console.print("Usage: show options|jobs|companies|resume|profile|usage")


def run_shell(app: typer.Typer) -> None:
    Shell(app).run()
