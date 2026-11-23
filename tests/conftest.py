from __future__ import annotations

from pathlib import Path

import pytest

from autoapply.models import Bullet, Contact, Experience, MasterResume, ResumeExtraction, SkillGroup


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    from autoapply.sources import keryx

    monkeypatch.setenv("AUTOAPPLY_HOME", str(tmp_path / "home"))
    keryx.clear_cache()  # snapshot caches are per process; each test has its own home
    yield tmp_path / "home"
    keryx.clear_cache()


def sample_resume() -> MasterResume:
    return MasterResume(
        contact=Contact(name="Alex Rivera", email="alex@example.com", phone="555-0100", location="Austin, TX"),
        experience=[
            Experience(
                company="Acme",
                title="Backend Engineer",
                start="2022-03",
                end="Present",
                bullets=[Bullet(text="Built billing API in Go"), Bullet(text="Cut p99 latency 40%")],
            ),
            Experience(company="Initech", title="Intern", start="2021", end="2021",
                       bullets=[Bullet(text="Wrote ETL jobs")]),
        ],
        skills=[SkillGroup(category="Languages", items=["Go", "Python"])],
    )


class FakeLLM:
    """Stands in for autoapply.llm.LLM; records the call and returns a canned extraction."""

    def __init__(self, result: ResumeExtraction | None = None):
        self.result = result or ResumeExtraction(resume=sample_resume(), warnings=["date unclear"])
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def make_pdf(path: Path, lines: list[str]) -> Path:
    """Write a minimal single-page PDF with real (extractable) text."""
    stream = "BT /F1 11 Tf 50 750 Td 14 TL " + " ".join(
        f"({l.replace('(', '[').replace(')', ']')}) '" for l in lines
    ) + " ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = "%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n"
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    path.write_bytes(out.encode("latin-1"))
    return path


@pytest.fixture(autouse=True)
def no_request_spacing(monkeypatch):
    """Tests use mock transports; don't wait between requests."""
    from autoapply.sources import base

    monkeypatch.setattr(base, "MIN_INTERVAL_S", 0.0)
