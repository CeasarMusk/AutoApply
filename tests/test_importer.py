import base64

import docx
import pytest

from autoapply.models import ResumeExtraction
from autoapply.resume import importer

from .conftest import FakeLLM, make_pdf


def test_pdf_sent_as_native_document(tmp_path):
    pdf = make_pdf(tmp_path / "r.pdf", ["Alex Rivera", "Backend Engineer at Acme"] * 10)
    llm = FakeLLM()
    result = importer.import_resume(pdf, llm)

    content = llm.calls[0]["content"]
    assert content[0]["type"] == "document"
    assert content[0]["source"]["media_type"] == "application/pdf"
    assert base64.b64decode(content[0]["source"]["data"]) == pdf.read_bytes()
    assert llm.calls[0]["output_format"] is ResumeExtraction
    assert "Alex Rivera" in result.source_text
    assert not result.scanned


def test_ids_from_model_are_discarded(tmp_path):
    fake = FakeLLM()
    fake.result.resume.experience[0].id = "made-up"
    f = tmp_path / "r.txt"
    f.write_text("Alex Rivera\nBackend Engineer")
    result = importer.import_resume(f, fake)
    assert result.resume.experience[0].id == "exp1"
    assert result.warnings == ["date unclear"]


def test_scanned_pdf_warns(tmp_path):
    pdf = make_pdf(tmp_path / "scan.pdf", ["x"])
    result = importer.import_resume(pdf, FakeLLM())
    assert result.scanned
    assert "scanned" in result.warnings[0]


def test_large_pdf_falls_back_to_text(tmp_path, monkeypatch):
    pdf = make_pdf(tmp_path / "big.pdf", ["Alex Rivera has lots of experience here"] * 10)
    monkeypatch.setattr(importer, "MAX_PDF_BYTES", 10)
    llm = FakeLLM()
    importer.import_resume(pdf, llm)
    block = llm.calls[0]["content"][0]
    assert block["type"] == "text" and "Alex Rivera" in block["text"]


def test_docx_text_includes_tables_and_links(tmp_path):
    d = docx.Document()
    d.add_paragraph("Alex Rivera")
    d.add_paragraph("Built billing API", style="List Bullet")
    t = d.add_table(rows=1, cols=2)
    t.cell(0, 0).text = "Skills"
    t.cell(0, 1).text = "Go, Python"
    d.part.relate_to("https://github.com/alex", docx.opc.constants.RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    path = tmp_path / "r.docx"
    d.save(path)

    text = importer.docx_text(path)
    assert "Alex Rivera" in text
    assert "- Built billing API" in text
    assert "Skills | Go, Python" in text
    assert "https://github.com/alex" in text


def test_unsupported_and_missing(tmp_path):
    (tmp_path / "r.pages").write_text("x")
    with pytest.raises(importer.ImportError_, match="Unsupported"):
        importer.import_resume(tmp_path / "r.pages", FakeLLM())
    with pytest.raises(importer.ImportError_, match="not found"):
        importer.import_resume(tmp_path / "nope.pdf", FakeLLM())


def test_extraction_schema_is_accepted_by_sdk():
    """The SDK must be able to turn our pydantic model into a structured-output schema."""
    from anthropic.lib._parse._transform import to_json_output_format as conv

    fmt = conv(ResumeExtraction)
    assert fmt["type"] == "json_schema"
