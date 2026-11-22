from __future__ import annotations

import io

import pytest

from autoapply.models import Bullet, Experience, Job, Project
from autoapply.resume import render, store, tailor
from autoapply.resume.tailor import BulletPick, ItemPick, SkillPick, TailorPlan, check_rewrite

from .conftest import sample_resume


def _master():
    r = sample_resume()
    r.projects = [Project(name="Ledger", bullets=[Bullet(text="Double-entry ledger in Python")])]
    store.assign_ids(r)
    return r


def _job():
    return Job(id=7, source="greenhouse", external_id="1", company="Acme Pay", title="Backend Engineer",
               url="x", dedupe_key="k", description="We use Go and Python to build payments APIs.")


def test_check_rewrite():
    vocab = tailor._vocab(_master())
    orig = "Cut p99 latency 40%"
    assert check_rewrite(orig, "Reduced p99 API latency by 40%", vocab) is None
    assert "number" in check_rewrite(orig, "Cut p99 latency 60%", vocab)
    assert "Kubernetes" in check_rewrite(orig, "Cut p99 latency 40% on Kubernetes", vocab)
    assert check_rewrite("Built billing API in Go", "Built a billing API in Go and Python", vocab) is None


def test_apply_plan_only_uses_master_items():
    m = _master()
    plan = TailorPlan(
        experience=[ItemPick(id="exp1", bullets=[
            BulletPick(id="exp1.b2", text="Reduced p99 latency 40%"),
            BulletPick(id="exp1.b1", text="Built billing API in Rust"),  # new tool: rejected
            BulletPick(id="exp9.b1", text="Invented"),  # unknown id: ignored
        ])],
        projects=[ItemPick(id="proj1", bullets=[])],
        skills=[SkillPick(id="skill1", items=["python", "Go", "Haskell"])],
        certifications=["cert5"],
        summary="Backend engineer who builds billing APIs in Go.",
    )
    m.summary = "Engineer. Go, Python, billing APIs."
    t = tailor.apply_plan(m, plan)
    r = t.resume
    assert [e.id for e in r.experience] == ["exp1"]
    assert [b.text for b in r.experience[0].bullets] == ["Reduced p99 latency 40%", "Built billing API in Go"]
    assert len(t.reworded) == 1 and len(t.rejected) == 1
    assert r.projects[0].bullets[0].text == "Double-entry ledger in Python"  # empty pick keeps bullets
    assert r.skills[0].items == ["Python", "Go"]
    assert t.summary_changed and r.summary.startswith("Backend engineer")
    assert r.certifications == [] and r.contact.name == "Alex Rivera" and r.education == m.education


def test_tailor_calls_llm_and_roundtrips():
    class LLM:
        def parse(self, **kw):
            self.kw = kw
            return TailorPlan(experience=[ItemPick(id="exp2", bullets=[BulletPick(id="exp2.b1")]),
                                          ItemPick(id="exp1", bullets=[BulletPick(id="exp1.b1")])],
                              notes="Go focus")
    llm = LLM()
    t = tailor.tailor(_master(), _job(), llm)
    assert llm.kw["task"] == "tailor" and "payments APIs" in llm.kw["content"]
    assert [e.id for e in t.resume.experience] == ["exp1", "exp2"]  # kept chronological
    back = tailor.Tailored.from_yaml(t.to_yaml())
    assert back.resume == t.resume and back.notes == "Go focus"


def test_fmt_date():
    assert render.date_range("2022-03", "Present") == "Mar 2022 – Present"
    assert render.date_range("2021", None) == "2021"


def test_render_fits_one_page(tmp_path):
    pw = pytest.importorskip("playwright.sync_api")
    try:
        with pw.sync_playwright() as p:
            p.chromium.launch().close()
    except Exception as e:
        pytest.skip(f"chromium unavailable: {e}")
    from pypdf import PdfReader

    m = _master()
    m.experience = [Experience(company=f"Co{i}", title="Engineer", start="2020", end="2021",
                               bullets=[Bullet(text=f"Did meaningful thing number {j} " * 4) for j in range(8)])
                    for i in range(6)]
    store.assign_ids(m)
    t = tailor.Tailored(resume=m)
    out = render.render_pdf(t, tmp_path / "r.pdf")
    reader = PdfReader(io.BytesIO(out.read_bytes()))
    assert len(reader.pages) == 1 and t.dropped_for_space > 0
    assert "Alex Rivera" in reader.pages[0].extract_text()
