"""Paths and user configuration.

Everything personal (resume, answers, database, generated documents) lives under
``~/.autoapply`` so it never ends up in the git repo. Override with ``AUTOAPPLY_HOME``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

DEFAULT_MODEL = "claude-opus-5-5"


def home() -> Path:
    return Path(os.environ.get("AUTOAPPLY_HOME", Path.home() / ".autoapply")).expanduser()


def config_path() -> Path:
    return home() / "config.yaml"


def master_resume_path() -> Path:
    return home() / "master_resume.yaml"


def answers_path() -> Path:
    return home() / "answers.yaml"


def db_path() -> Path:
    return home() / "autoapply.db"


def output_dir() -> Path:
    return home() / "out"


BOARD_TYPES = ("greenhouse", "lever", "ashby", "smartrecruiters", "workable", "bamboohr", "workday", "oracle")


class Companies(BaseModel):
    """Company board slugs for the public ATS APIs, e.g. greenhouse: [stripe, airbnb].

    Workday boards are ``host/tenant/site`` and Oracle boards ``host/site``; `companies add <url>`
    works them out from a careers-page link.
    """

    greenhouse: list[str] = Field(default_factory=list)
    lever: list[str] = Field(default_factory=list)
    ashby: list[str] = Field(default_factory=list)
    smartrecruiters: list[str] = Field(default_factory=list)
    workable: list[str] = Field(default_factory=list)
    bamboohr: list[str] = Field(default_factory=list)
    workday: list[str] = Field(default_factory=list)
    oracle: list[str] = Field(default_factory=list)

    def pairs(self) -> list[tuple[str, str]]:
        return [(ats, slug) for ats in BOARD_TYPES for slug in getattr(self, ats)]

    def add(self, ats: str, slug: str) -> bool:
        lst = getattr(self, ats)
        if slug in lst:
            return False
        lst.append(slug)
        return True

    def remove(self, slug: str, ats: str | None = None) -> list[str]:
        removed = []
        for name in [ats] if ats else BOARD_TYPES:
            lst = getattr(self, name)
            if slug in lst:
                lst.remove(slug)
                removed.append(name)
        return removed


class SearchDefaults(BaseModel):
    keywords: list[str] = Field(default_factory=list)  # e.g. ["software engineer", "backend"]
    exclude: list[str] = Field(default_factory=list)  # e.g. ["senior", "staff", "manager"]
    locations: list[str] = Field(default_factory=list)  # e.g. ["Austin", "Remote"]
    remote_only: bool = False
    max_age_days: int | None = None


class Config(BaseModel):
    # "claude-code": use your Claude subscription via the `claude` CLI (no API tokens).
    # "api": use the Anthropic API with ANTHROPIC_API_KEY (pay per token).
    # "ollama": a model running locally in Ollama (free, private, slower and less capable).
    llm_backend: Literal["claude-code", "api", "ollama"] = "claude-code"
    claude_code_model: str = "opus"
    model: str = DEFAULT_MODEL  # API model id, used when llm_backend is "api"
    ollama_model: str = ""  # e.g. "qwen3:14b"; empty = the first model `ollama list` shows
    ollama_host: str = ""  # empty = $OLLAMA_HOST or http://localhost:11434
    ollama_num_ctx: int = 16384  # context window; Ollama's default (2-4k) truncates resumes + postings
    ollama_think: bool = False  # let thinking models (qwen3, deepseek-r1, gpt-oss…) reason first: better, slower
    companies: Companies = Field(default_factory=Companies)
    use_starter_companies: bool = True  # also search the built-in list of ~60 tech companies
    use_remoteok: bool = True  # also search Remote OK's remote-jobs feed
    use_keryx: bool = True  # also search Keryx's offline list of US internships / new-grad roles
    keryx_sync_hours: float = 12  # re-download the Keryx list when a search finds it older (0: only `keryx sync`)
    keryx_live: bool = True  # also ask the boards behind Keryx matches directly, for up-to-the-minute listings
    keryx_live_max_boards: int = 40  # at most this many of those boards per search
    board_cache_minutes: float = 15  # reuse a board's listing fetched this recently (0: always refetch)
    search: SearchDefaults = Field(default_factory=SearchDefaults)
    last_search: dict | None = None  # the last preset picked in the search menu
    resume_file: str | None = None  # the resume file uploaded to applications (copied here on import)
    transcript_file: str | None = None  # uploaded when a form asks for a transcript (copied here on import)
    tailor_resume: bool = True  # upload a resume tailored to each job (falls back to resume_file)
    ai_essays: bool = True  # draft answers to required open-ended questions with AI
    ask_optional: bool = True  # ask about optional questions it can't answer (False: leave them blank)
    queue_order: Literal["match", "recent"] = "match"  # apply queue: best match or most recently posted first


def keep_document(src: Path, stem: str) -> Path:
    """Copy a document into the AutoApply home as ``<stem><ext>`` (replacing older copies) and return it."""
    src = src.expanduser().resolve()
    dest = home() / f"{stem}{src.suffix.lower()}"
    home().mkdir(parents=True, exist_ok=True)
    if src != dest.resolve():
        for old in home().glob(f"{stem}.*"):
            if old.is_file() and old.suffix != ".bak":
                old.unlink()
        shutil.copy2(src, dest)
    return dest


def load_config() -> Config:
    path = config_path()
    if not path.exists():
        return Config()
    return Config.model_validate(yaml.safe_load(path.read_text()) or {})


def save_config(cfg: Config) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg.model_dump(), sort_keys=False))


DEFAULT_ANSWERS = """\
# Standard answers reused on every application. Anything left blank is flagged
# for you to fill in at apply time; the program never guesses legal/EEO answers.
work_authorization:
  authorized_us: null          # true / false
  requires_sponsorship: null   # true / false
salary_expectation: null       # e.g. "140000-160000 USD"
willing_to_relocate: null
start_date: null               # e.g. "2 weeks notice"
links:
  linkedin: null
  github: null
  portfolio: null
eeo:                           # leave null to answer "decline to self-identify"
  gender: null
  race: null
  veteran: null
  disability: null
custom: {}                     # question text -> your saved answer
"""
