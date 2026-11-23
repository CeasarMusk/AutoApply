import json

import httpx
import pytest
from typer.testing import CliRunner

from autoapply.cli import app
from autoapply.resume import github as gh
from autoapply.resume import store

from .conftest import FakeLLM, sample_resume

README = """<p align="center"><img src="logo.png"></p>

# Trail Mapper

[![CI](https://img.shields.io/badge/ci-passing-green)](https://x) ![stars](https://img.shields.io/x)

Trail Mapper is a **web app** that plans hiking routes from [OpenStreetMap](https://osm.org) data and
shows elevation profiles for each segment.

## Features
- Route planning with `A*` search over trail graphs
- Elevation charts :chart_with_upwards_trend:
- [x] Offline tile caching
- ok

## Installation
```bash
pip install trail-mapper
```
This paragraph is about installing and should never be the summary at all.
"""


def test_parse_username_variants():
    assert gh.parse_username("https://github.com/octocat") == "octocat"
    assert gh.parse_username("github.com/octocat/hello-world/") == "octocat"
    assert gh.parse_username("@octo-cat") == "octo-cat"
    assert gh.parse_username("https://github.com/octocat?tab=repositories") == "octocat"
    for bad in ["https://gitlab.com/octocat", "https://github.com/", "https://github.com/settings", "not a user!"]:
        with pytest.raises(gh.GitHubError):
            gh.parse_username(bad)


def test_parse_readme_summary_and_features():
    info = gh.parse_readme(README)
    assert info.title == "Trail Mapper"
    assert info.summary == ("Trail Mapper is a web app that plans hiking routes from OpenStreetMap data and "
                            "shows elevation profiles for each segment.")
    assert info.features == ["Route planning with A* search over trail graphs", "Elevation charts",
                             "Offline tile caching"]


def test_parse_readme_skips_boilerplate_and_handles_setext():
    md = "My App\n======\n\nThis project was bootstrapped with Create React App and more words.\n\n" \
         "A small budgeting tool for splitting rent between roommates.\n"
    info = gh.parse_readme(md)
    assert info.title == "My App"
    assert info.summary == "A small budgeting tool for splitting rent between roommates."


def test_manifest_deps_and_tech():
    pkg = json.dumps({"dependencies": {"react": "^18", "express": "4"}, "devDependencies": {"vitest": "1"}})
    py = '[project]\ndependencies = ["fastapi>=0.1", "SQLAlchemy[asyncio]~=2.0"]\n'
    go = "module x\n\nrequire (\n\tgithub.com/gin-gonic/gin v1.9.1\n)\n"
    deps = gh.manifest_deps("package.json", pkg) | gh.manifest_deps("pyproject.toml", py) | gh.manifest_deps("go.mod", go)
    assert gh.tech_from_deps(deps) == ["Express", "FastAPI", "Gin", "React", "SQLAlchemy", "Vitest"]
    assert gh.manifest_deps("package.json", "{not json") == set()
    assert gh.top_languages({"Python": 900, "HTML": 500, "Shell": 10, "Rust": 100}) == ["Python", "Rust"]


def _api(readme=README, deep_calls=None):
    repos = [
        {"name": "trail-mapper", "full_name": "octo/trail-mapper", "html_url": "https://github.com/octo/trail-mapper",
         "description": "Hiking routes", "language": "Python", "stargazers_count": 5, "fork": False},
        {"name": "dotfiles", "full_name": "octo/dotfiles", "html_url": "https://github.com/octo/dotfiles",
         "description": None, "language": "Shell", "stargazers_count": 0, "fork": False},
        {"name": "linux", "full_name": "octo/linux", "html_url": "https://github.com/octo/linux",
         "stargazers_count": 99, "fork": True},
    ]
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(path)
        if path == "/users/octo":
            return httpx.Response(200, json={"login": "octo", "name": "Octo Cat", "html_url": "https://github.com/octo",
                                             "public_repos": 3})
        if path == "/users/octo/repos":
            return httpx.Response(200, json=repos)
        if path == "/repos/octo/trail-mapper/readme":
            assert "raw" in request.headers["accept"]
            return httpx.Response(200, text=readme)
        if path == "/repos/octo/trail-mapper/languages":
            return httpx.Response(200, json={"Python": 1000, "TypeScript": 400})
        if path == "/repos/octo/trail-mapper/contents/":
            return httpx.Response(200, json=[{"name": "pyproject.toml", "type": "file"}, {"name": "src", "type": "dir"}])
        if path == "/repos/octo/trail-mapper/contents/pyproject.toml":
            return httpx.Response(200, text='[project]\ndependencies = ["fastapi"]\n')
        if path.startswith("/repos/octo/dotfiles/"):
            return httpx.Response(404, json={})
        return httpx.Response(500)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return gh.GitHub(http=client, token=""), seen


def test_basic_scan_reads_readmes_only():
    api, seen = _api()
    result = gh.scan("octo", gh=api)
    assert [r.name for r in result.repos] == ["trail-mapper", "dotfiles"]
    assert result.skipped_forks == 1
    tm = result.repos[0]
    assert tm.display_name == "Trail Mapper" and tm.summary.startswith("Trail Mapper is a web app")
    assert not any("languages" in p or "contents" in p for p in seen)  # basic = no deep requests
    assert result.repos[1].summary is None


def test_deep_scan_reads_languages_and_manifests():
    api, seen = _api()
    result = gh.scan("octo", gh=api, deep=True)
    tm = result.repos[0]
    assert tm.languages == ["Python", "TypeScript"] and tm.tech == ["FastAPI"]
    assert "README:" in gh.source_text(tm) and "OpenStreetMap" in gh.source_text(tm)


def test_rate_limit_message():
    def handler(request):
        return httpx.Response(403, headers={"X-RateLimit-Remaining": "0"})

    api = gh.GitHub(http=httpx.Client(transport=httpx.MockTransport(handler)), token="")
    with pytest.raises(gh.GitHubError, match="GITHUB_TOKEN"):
        gh.scan("octo", gh=api)


def test_ai_bullets_are_fact_checked():
    api, _ = _api()
    repo = gh.scan("octo", gh=api, deep=True).repos[0]
    llm = FakeLLM(gh.RepoBullets(bullets=[
        "Built a web app that plans hiking routes from OpenStreetMap data with FastAPI.",
        "Served 10,000 users with Kubernetes.",
    ]))
    kept, rejected = gh.ai_bullets(repo, llm)
    assert kept == ["Built a web app that plans hiking routes from OpenStreetMap data with FastAPI."]
    assert rejected and "10000" in rejected[0][1]
    assert llm.calls[0]["task"] == "github_project"


def test_merge_helpers():
    r = sample_resume()
    api, _ = _api()
    repos = gh.scan("octo", gh=api, deep=True).repos
    extra = gh.new_skills(r, repos[:1])
    assert extra == {"Languages": ["TypeScript"], "Frameworks & Tools": ["FastAPI"]}  # Python already listed
    gh.add_skills(r, extra)
    assert "TypeScript" in r.skills[0].items and r.skills[-1].category == "Frameworks & Tools"
    assert gh.add_github_link(r, "https://github.com/octo") and not gh.add_github_link(r, "https://github.com/octo")
    r.projects.append(gh.to_project(repos[0]))
    assert gh.already_in(r, repos[0]) and not gh.already_in(r, repos[1])
    assert [b.text for b in r.projects[0].bullets][0].startswith("Route planning")


def test_cli_basic_import_saves_projects(monkeypatch):
    store.save(sample_resume())
    api, _ = _api()
    monkeypatch.setattr(gh, "GitHub", lambda: api)
    res = CliRunner().invoke(app, ["resume", "github", "https://github.com/octo", "--yes"])
    assert res.exit_code == 0, res.output
    r = store.load()
    assert [p.name for p in r.projects] == ["Trail Mapper"]
    assert r.projects[0].id and r.projects[0].url == "https://github.com/octo/trail-mapper"
    assert any(l.url == "https://github.com/octo" for l in r.contact.links)
    # Running it again finds nothing new.
    res = CliRunner().invoke(app, ["resume", "github", "octo", "--yes"])
    assert "already in your resume" in res.output
