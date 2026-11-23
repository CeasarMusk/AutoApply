"""Pull projects for the master resume from a public GitHub profile.

Basic scan (default): the profile, its public repos, and each repo's README, from which we take
a one-paragraph summary and any "Features" list. That's 2 requests + 1 per repo.

Deep scan (``--deep``): also each repo's language breakdown and its dependency manifests
(package.json, pyproject.toml, …) to name the frameworks it uses, and, if AI is on, 2-3 resume
bullets written from the README. Those bullets go through the same fact guard as tailoring:
any number or name that isn't in the repo's own text is rejected.

Nothing is invented: every line comes from the README/metadata or is dropped.
"""

from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field

from ..models import Bullet, MasterResume, Project
from ..sources import base

API = "https://api.github.com"
MAX_README_CHARS = 12_000
_USER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_NOT_USERS = {"orgs", "settings", "topics", "explore", "marketplace", "features", "about", "login",
              "sponsors", "notifications", "pulls", "issues", "search", "trending", "collections"}


class GitHubError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# input
# ---------------------------------------------------------------------------


def parse_username(text: str) -> str:
    """`https://github.com/octocat`, `github.com/octocat/repo`, `@octocat` or `octocat` → `octocat`."""
    s = (text or "").strip().rstrip("/")
    if not s:
        raise GitHubError("Paste your GitHub profile link, e.g. https://github.com/yourname")
    if s.startswith("@"):
        s = s[1:]
    if "/" in s or "." in s:
        url = s if "://" in s else f"https://{s}"
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if host not in ("github.com", "www.github.com"):
            raise GitHubError(f"That isn't a github.com link: {text}")
        segs = [p for p in parts.path.split("/") if p]
        if not segs:
            raise GitHubError("That link has no username in it; use https://github.com/yourname")
        s = segs[0]
    if s.lower() in _NOT_USERS or not _USER_RE.match(s):
        raise GitHubError(f"'{s}' isn't a GitHub username.")
    return s


# ---------------------------------------------------------------------------
# README parsing (no AI)
# ---------------------------------------------------------------------------

_FEATURE_HEADING = re.compile(r"features|highlights|what (it|this) does|capabilities|overview of features", re.I)
_STOP_HEADING = re.compile(r"install|setup|getting started|usage|license|contribut|build|requirements|"
                           r"prerequisites|development|running|deploy|faq|credits|acknowledg|table of contents|toc",
                           re.I)
_BOILERPLATE = re.compile(r"^(this (project|repo(sitory)?) (was|is) (bootstrapped|generated|created) with|"
                          r"getting started with create|welcome to your|todo\b|wip\b|work in progress)", re.I)


def clean_inline(text: str) -> str:
    """Markdown inline syntax → plain text."""
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)  # images
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)  # links → their text
    t = re.sub(r"\[([^\]]*)\]\[[^\]]*\]", r"\1", t)  # reference links
    t = re.sub(r"<[^>]+>", "", t)  # inline html
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"(\*\*|__)(.+?)\1", r"\2", t)
    t = re.sub(r"(?<![\w*])[*_](?!\s)(.+?)(?<!\s)[*_](?![\w*])", r"\1", t)
    t = re.sub(r":[a-z0-9_+-]+:", "", t)  # :rocket: emoji shortcodes
    t = re.sub(r"[\U0001F000-\U0001FAFF☀-➿️]", "", t)  # emoji
    return re.sub(r"\s+", " ", t).strip(" -–—:|")


def _strip_blocks(md: str) -> str:
    md = re.sub(r"<!--.*?-->", "", md, flags=re.S)
    md = re.sub(r"^(```|~~~).*?^\1\s*$", "", md, flags=re.S | re.M)  # fenced code
    md = re.sub(r"<(picture|details|table|svg)\b.*?</\1>", "", md, flags=re.S | re.I)
    return md


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    sentences = re.split(r"(?<=[.!?])\s+", text)
    out = ""
    for s_ in sentences:
        if len(out) + len(s_) + 1 > limit:
            break
        out = f"{out} {s_}".strip()
    return out or text[: limit - 1].rsplit(" ", 1)[0] + "…"


@dataclass
class ReadmeInfo:
    title: str | None = None
    summary: str | None = None
    features: list[str] = field(default_factory=list)


def parse_readme(md: str) -> ReadmeInfo:
    """Title, first real paragraph, and up to 4 items of a Features-style list."""
    info = ReadmeInfo()
    heading: str | None = None
    in_features = stopped = False
    para: list[str] = []

    def flush_para() -> None:
        if not para:
            return
        text = clean_inline(" ".join(para))
        para.clear()
        if info.summary or stopped or in_features:
            return
        if len(text) >= 30 and len(text.split()) >= 5 and not _BOILERPLATE.match(text):
            info.summary = _shorten(text, 300)

    for raw in _strip_blocks(md).splitlines():
        line = raw.strip()
        h = re.match(r"^(#{1,6})\s+(.*?)\s*#*$", line)
        setext = None
        if not h and para and re.match(r"^(=+|-+)$", line) and len(para) == 1:
            setext = para.pop()
        if h or setext:
            flush_para()
            heading = clean_inline(h.group(2) if h else setext)
            if info.title is None and heading and (not h or len(h.group(1)) == 1):
                info.title = heading
                continue
            in_features = bool(_FEATURE_HEADING.search(heading or ""))
            if _STOP_HEADING.search(heading or "") and not in_features:
                stopped = True
            continue
        item = re.match(r"^(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(.*)", line)
        if item:
            flush_para()
            if in_features and len(info.features) < 4:
                text = clean_inline(item.group(1))
                if len(text) >= 8:
                    info.features.append(_shorten(text, 160))
            continue
        if not line or line.startswith(("|", ">", "[!")) or re.match(r"^(\*{3,}|-{3,}|_{3,})$", line):
            flush_para()
            continue
        para.append(line)
    flush_para()
    return info


# ---------------------------------------------------------------------------
# deep scan: languages + dependency manifests
# ---------------------------------------------------------------------------

# dependency name (lowercase) → how it reads on a resume
KNOWN_TECH = {
    "react": "React", "next": "Next.js", "vue": "Vue", "nuxt": "Nuxt", "svelte": "Svelte", "@sveltejs/kit": "SvelteKit",
    "@angular/core": "Angular", "express": "Express", "fastify": "Fastify", "@nestjs/core": "NestJS",
    "tailwindcss": "Tailwind CSS", "typescript": "TypeScript", "vite": "Vite", "electron": "Electron",
    "react-native": "React Native", "expo": "Expo", "three": "Three.js", "d3": "D3.js", "redux": "Redux",
    "@reduxjs/toolkit": "Redux", "graphql": "GraphQL", "@apollo/client": "Apollo", "prisma": "Prisma",
    "@prisma/client": "Prisma", "mongoose": "MongoDB", "mongodb": "MongoDB", "pg": "PostgreSQL",
    "socket.io": "Socket.IO", "jest": "Jest", "vitest": "Vitest", "playwright": "Playwright",
    "@playwright/test": "Playwright", "cypress": "Cypress", "firebase": "Firebase", "@supabase/supabase-js": "Supabase",
    "django": "Django", "flask": "Flask", "fastapi": "FastAPI", "sqlalchemy": "SQLAlchemy", "sqlmodel": "SQLModel",
    "pydantic": "Pydantic", "numpy": "NumPy", "pandas": "pandas", "scipy": "SciPy", "scikit-learn": "scikit-learn",
    "sklearn": "scikit-learn", "torch": "PyTorch", "tensorflow": "TensorFlow", "keras": "Keras", "jax": "JAX",
    "transformers": "Hugging Face Transformers", "langchain": "LangChain", "openai": "OpenAI API",
    "anthropic": "Claude API", "matplotlib": "Matplotlib", "streamlit": "Streamlit", "celery": "Celery",
    "redis": "Redis", "psycopg2": "PostgreSQL", "psycopg": "PostgreSQL", "psycopg2-binary": "PostgreSQL",
    "boto3": "AWS", "pytest": "pytest", "typer": "Typer", "click": "Click", "opencv-python": "OpenCV",
    "selenium": "Selenium", "beautifulsoup4": "Beautiful Soup", "scrapy": "Scrapy", "polars": "Polars",
    "tokio": "Tokio", "actix-web": "Actix", "axum": "Axum", "serde": "Serde", "rocket": "Rocket", "bevy": "Bevy",
    "github.com/gin-gonic/gin": "Gin", "github.com/gofiber/fiber": "Fiber", "github.com/labstack/echo": "Echo",
    "gorm.io/gorm": "GORM", "github.com/spf13/cobra": "Cobra", "rails": "Ruby on Rails", "sinatra": "Sinatra",
    "spring-boot-starter-web": "Spring Boot", "spring-boot-starter": "Spring Boot", "laravel/framework": "Laravel",
    "flutter": "Flutter",
}
MANIFESTS = ("package.json", "pyproject.toml", "requirements.txt", "Cargo.toml", "go.mod", "Gemfile",
             "pom.xml", "build.gradle", "build.gradle.kts", "composer.json", "pubspec.yaml", "Dockerfile")


def _dep_name(spec: str) -> str:
    return re.split(r"[\s<>=!~;\[(@,]", spec.strip(), maxsplit=1)[0].lower()


def manifest_deps(name: str, text: str) -> set[str]:
    """Dependency names declared in one manifest file (best effort; bad files give nothing)."""
    deps: set[str] = set()
    try:
        if name in ("package.json", "composer.json"):
            data = json.loads(text)
            for key in ("dependencies", "devDependencies", "peerDependencies", "require", "require-dev"):
                deps |= {k.lower() for k in (data.get(key) or {})}
        elif name == "pyproject.toml":
            data = tomllib.loads(text)
            proj = data.get("project") or {}
            specs = list(proj.get("dependencies") or [])
            for group in (proj.get("optional-dependencies") or {}).values():
                specs += group
            for group in (data.get("dependency-groups") or {}).values():
                specs += [g for g in group if isinstance(g, str)]
            poetry = (data.get("tool") or {}).get("poetry") or {}
            specs += list(poetry.get("dependencies") or {})
            deps |= {_dep_name(s) for s in specs if isinstance(s, str)}
        elif name == "requirements.txt":
            deps |= {_dep_name(l) for l in text.splitlines() if l.strip() and not l.lstrip().startswith(("#", "-"))}
        elif name == "Cargo.toml":
            data = tomllib.loads(text)
            for key in ("dependencies", "dev-dependencies"):
                deps |= {k.lower() for k in (data.get(key) or {})}
        elif name == "go.mod":
            deps |= {m.group(1).lower() for m in re.finditer(r"^\s*(?:require\s+)?([\w.\-]+\.[\w]+/[\w./\-]+)\s+v",
                                                             text, re.M)}
        elif name == "Gemfile":
            deps |= {m.group(1).lower() for m in re.finditer(r"""^\s*gem\s+['"]([^'"]+)""", text, re.M)}
        elif name in ("pom.xml",):
            deps |= {m.group(1).lower() for m in re.finditer(r"<artifactId>([^<]+)</artifactId>", text)}
        elif name.startswith("build.gradle"):
            deps |= {m.group(1).lower() for m in re.finditer(r"""['"][\w.\-]+:([\w.\-]+)(?::[^'"]*)?['"]""", text)}
        elif name == "pubspec.yaml":
            if re.search(r"^\s*flutter:", text, re.M):
                deps.add("flutter")
        elif name == "Dockerfile":
            deps.add("docker")
    except (ValueError, tomllib.TOMLDecodeError, AttributeError, TypeError):
        return set()
    return deps


def tech_from_deps(deps: set[str]) -> list[str]:
    found = [KNOWN_TECH[d] for d in sorted(deps) if d in KNOWN_TECH]
    if "docker" in deps:
        found.append("Docker")
    return list(dict.fromkeys(found))


def top_languages(byte_counts: dict[str, int], min_share: float = 0.05, keep: int = 4) -> list[str]:
    total = sum(byte_counts.values()) or 1
    skip = {"html", "css", "scss", "makefile", "dockerfile", "shell", "batchfile", "procfile", "jupyter notebook"}
    ranked = sorted(byte_counts.items(), key=lambda kv: kv[1], reverse=True)
    return [lang for lang, n in ranked if n / total >= min_share and lang.lower() not in skip][:keep]


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@dataclass
class Repo:
    name: str
    full_name: str
    url: str
    description: str | None = None
    homepage: str | None = None
    language: str | None = None
    topics: list[str] = field(default_factory=list)
    stars: int = 0
    fork: bool = False
    archived: bool = False
    pushed_at: str | None = None
    readme: ReadmeInfo | None = None
    readme_text: str = ""  # kept for deep scans (AI bullets)
    languages: list[str] = field(default_factory=list)  # deep
    tech: list[str] = field(default_factory=list)  # deep

    @property
    def summary(self) -> str | None:
        return (self.readme.summary if self.readme else None) or self.description

    @property
    def display_name(self) -> str:
        title = self.readme.title if self.readme else None
        # Use the README's title when it's a nicer spelling of the repo name ("my-app" → "My App").
        if title and len(title) <= 40 and re.sub(r"[^a-z0-9]", "", title.lower()) == re.sub(r"[^a-z0-9]", "", self.name.lower()):
            return title
        return self.name


@dataclass
class Scan:
    username: str
    name: str | None
    profile_url: str
    blog: str | None
    repos: list[Repo]
    total_public: int
    skipped_forks: int
    deep: bool


def _token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None


class GitHub:
    def __init__(self, http: httpx.Client | None = None, token: str | None = None):
        self.http = http or base.client()
        self.token = token if token is not None else _token()
        self.calls = 0

    def get(self, path: str, *, raw: bool = False, params: dict | None = None, missing_ok: bool = False) -> Any:
        url = f"{API}{path}"
        headers = {"Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        self.calls += 1
        try:
            with base.polite(url):
                r = self.http.get(url, headers=headers, params=params)
        except httpx.HTTPError as e:
            raise GitHubError(f"Couldn't reach GitHub: {e}") from e
        if r.status_code == 404:
            if missing_ok:
                return None
            raise GitHubError("not found")
        if r.status_code in (403, 429) and (r.headers.get("X-RateLimit-Remaining") == "0" or r.status_code == 429):
            hint = "" if self.token else (" Set GITHUB_TOKEN (e.g. `export GITHUB_TOKEN=$(gh auth token)`) "
                                          "for a higher limit, or")
            raise GitHubError(f"GitHub's rate limit for anonymous requests was reached.{hint} try again in an hour.")
        if r.status_code >= 400:
            raise GitHubError(f"GitHub returned HTTP {r.status_code} for {path}")
        if raw:
            return r.text
        try:
            return r.json()
        except ValueError as e:
            raise GitHubError(f"GitHub sent something that isn't JSON for {path}") from e


def _repo_from_api(d: dict) -> Repo:
    return Repo(name=d.get("name") or "", full_name=d.get("full_name") or "", url=d.get("html_url") or "",
                description=(d.get("description") or "").strip() or None, homepage=(d.get("homepage") or "").strip() or None,
                language=d.get("language"), topics=list(d.get("topics") or []), stars=int(d.get("stargazers_count") or 0),
                fork=bool(d.get("fork")), archived=bool(d.get("archived")), pushed_at=d.get("pushed_at"))


def scan(username: str, *, deep: bool = False, max_repos: int = 12, include_forks: bool = False,
         gh: GitHub | None = None, progress: Callable[[str], None] | None = None) -> Scan:
    gh = gh or GitHub()
    say = progress or (lambda _m: None)
    say(f"Reading github.com/{username}…")
    try:
        user = gh.get(f"/users/{username}")
    except GitHubError as e:
        if str(e) == "not found":
            raise GitHubError(f"There's no GitHub user '{username}'.") from e
        raise
    data = gh.get(f"/users/{username}/repos", params={"per_page": 100, "sort": "pushed", "type": "owner"}) or []
    repos = [_repo_from_api(d) for d in data if isinstance(d, dict)]
    forks = [r for r in repos if r.fork]
    if not include_forks:
        repos = [r for r in repos if not r.fork]
    # Most-starred first, then most recently worked on (the API already returns newest-pushed first).
    order = {r.full_name: i for i, r in enumerate(repos)}
    repos = sorted(repos, key=lambda r: (-r.stars, r.archived, order[r.full_name]))[:max_repos]

    for i, r in enumerate(repos, 1):
        say(f"Reading {r.name} ({i}/{len(repos)})…")
        try:
            text = gh.get(f"/repos/{r.full_name}/readme", raw=True, missing_ok=True) or ""
        except GitHubError as e:
            if "rate limit" in str(e):
                raise
            text = ""
        text = text[:MAX_README_CHARS]
        r.readme = parse_readme(text) if text else ReadmeInfo()
        if deep:
            r.readme_text = text
            _deep(gh, r)

    return Scan(username=user.get("login") or username, name=user.get("name"),
                profile_url=user.get("html_url") or f"https://github.com/{username}",
                blog=(user.get("blog") or "").strip() or None, repos=repos,
                total_public=int(user.get("public_repos") or len(data)), skipped_forks=0 if include_forks else len(forks),
                deep=deep)


def _deep(gh: GitHub, r: Repo) -> None:
    try:
        langs = gh.get(f"/repos/{r.full_name}/languages") or {}
        r.languages = top_languages({k: int(v) for k, v in langs.items()})
        listing = gh.get(f"/repos/{r.full_name}/contents/", missing_ok=True) or []
        present = [f["name"] for f in listing if isinstance(f, dict) and f.get("type") == "file" and f.get("name") in MANIFESTS]
        deps: set[str] = set()
        for name in present[:5]:
            text = gh.get(f"/repos/{r.full_name}/contents/{name}", raw=True, missing_ok=True) or ""
            deps |= manifest_deps(name, text[:200_000])
        r.tech = tech_from_deps(deps)
    except GitHubError as e:
        if "rate limit" in str(e):
            raise


# ---------------------------------------------------------------------------
# AI bullets (deep scans) + fact guard
# ---------------------------------------------------------------------------


class RepoBullets(BaseModel):
    bullets: list[str] = Field(description="2-3 resume bullets, each one sentence, past tense, under 25 words.")


BULLETS_SYSTEM = """You write resume bullets for a software project, using ONLY facts stated in the repository \
text you're given (README, description, languages, dependencies).
- 2-3 bullets, one sentence each, starting with a past-tense verb (Built, Implemented, Designed…).
- Never invent numbers, users, performance results, employers or technologies. If the README gives no \
numbers, use none. Only name technologies that appear in the text.
- Describe what the project does and how it was built. Plain, specific, no buzzwords.
- If the text says too little to write an honest bullet, return fewer bullets (even none)."""


def source_text(r: Repo) -> str:
    parts = [f"Repository: {r.name}", f"Description: {r.description or ''}",
             f"Languages: {', '.join(r.languages or ([r.language] if r.language else []))}",
             f"Libraries and frameworks: {', '.join(r.tech)}", f"Topics: {', '.join(r.topics)}",
             "README:", r.readme_text or ""]
    return "\n".join(parts)


def check_bullet(bullet: str, source: str) -> str | None:
    """Why a generated bullet isn't backed by the repo's text, or None if it is."""
    from .tailor import _WORD, check_rewrite

    vocab = {w.lower() for w in _WORD.findall(source)}
    # The source is long, so check_rewrite's length rule never trips; numbers and names are what matter.
    return check_rewrite(source, bullet, vocab)


def ai_bullets(r: Repo, llm: Any) -> tuple[list[str], list[tuple[str, str]]]:
    """(kept bullets, [(rejected bullet, why)])."""
    src = source_text(r)
    out = llm.parse(task="github_project", system=BULLETS_SYSTEM, content=src[:MAX_README_CHARS + 2000],
                    output_format=RepoBullets, effort="low", max_tokens=2000)
    kept, rejected = [], []
    for b in out.bullets[:3]:
        b = clean_inline(b)
        if not b:
            continue
        why = check_bullet(b, src)
        (rejected.append((b, why)) if why else kept.append(b))
    return kept, rejected


# ---------------------------------------------------------------------------
# merging into the master resume
# ---------------------------------------------------------------------------


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def already_in(resume: MasterResume, r: Repo) -> bool:
    urls = {(p.url or "").rstrip("/").lower() for p in resume.projects}
    names = {_norm(p.name) for p in resume.projects}
    return r.url.rstrip("/").lower() in urls or _norm(r.name) in names or _norm(r.display_name) in names


def to_project(r: Repo, bullets: list[str] | None = None) -> Project:
    lines = bullets if bullets is not None else (r.readme.features if r.readme else [])
    return Project(name=r.display_name, url=r.url, description=r.summary,
                   bullets=[Bullet(text=b) for b in lines])


def new_skills(resume: MasterResume, repos: list[Repo]) -> dict[str, list[str]]:
    """Languages / frameworks the chosen repos use that the resume's skills don't list yet."""
    have = {_norm(i) for g in resume.skills for i in g.items}
    langs, tools = [], []
    for r in repos:
        for lang in r.languages or ([r.language] if r.language else []):
            if _norm(lang) not in have and lang not in langs:
                langs.append(lang)
        for t in r.tech:
            if _norm(t) not in have and t not in tools and t not in langs:
                tools.append(t)
    out = {}
    if langs:
        out["Languages"] = langs
    if tools:
        out["Frameworks & Tools"] = tools
    return out


def add_skills(resume: MasterResume, extra: dict[str, list[str]]) -> None:
    from ..models import SkillGroup

    for category, items in extra.items():
        group = next((g for g in resume.skills if _norm(g.category) == _norm(category)), None)
        if group is None and category == "Frameworks & Tools":
            group = next((g for g in resume.skills if re.search(r"framework|tool|librar", g.category, re.I)), None)
        if group is None:
            resume.skills.append(SkillGroup(category=category, items=list(items)))
        else:
            group.items += [i for i in items if i not in group.items]


def add_github_link(resume: MasterResume, profile_url: str) -> bool:
    from ..models import Link

    if any("github.com" in (l.url or "").lower() for l in resume.contact.links):
        return False
    resume.contact.links.append(Link(label="GitHub", url=profile_url))
    return True
