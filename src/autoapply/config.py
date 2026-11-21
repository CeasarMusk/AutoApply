"""Paths and user configuration.

Everything personal (resume, answers, database, generated documents) lives under
``~/.autoapply`` so it never ends up in the git repo. Override with ``AUTOAPPLY_HOME``.
"""

from __future__ import annotations

import os
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


class Companies(BaseModel):
    """Company board slugs for the public ATS APIs, e.g. greenhouse: [stripe, airbnb]."""

    greenhouse: list[str] = Field(default_factory=list)
    lever: list[str] = Field(default_factory=list)
    ashby: list[str] = Field(default_factory=list)

    def pairs(self) -> list[tuple[str, str]]:
        return [(ats, slug) for ats in ("greenhouse", "lever", "ashby") for slug in getattr(self, ats)]

    def add(self, ats: str, slug: str) -> bool:
        lst = getattr(self, ats)
        if slug in lst:
            return False
        lst.append(slug)
        return True

    def remove(self, slug: str, ats: str | None = None) -> list[str]:
        removed = []
        for name in [ats] if ats else ["greenhouse", "lever", "ashby"]:
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
    llm_backend: Literal["claude-code", "api"] = "claude-code"
    claude_code_model: str = "opus"
    model: str = DEFAULT_MODEL  # API model id, used when llm_backend is "api"
    companies: Companies = Field(default_factory=Companies)
    search: SearchDefaults = Field(default_factory=SearchDefaults)


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
