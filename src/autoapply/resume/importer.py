"""Turn an existing resume file (PDF / DOCX / TXT / MD) into a MasterResume.

PDFs are sent to Claude as a native document so it sees the actual layout (columns,
tables, scanned pages). DOCX/TXT are converted to text first. Claude is told to copy
content verbatim; ids are assigned locally afterwards.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path

from ..llm import LLM
from ..models import MasterResume, ResumeExtraction
from . import store

# The API accepts requests up to 32 MB; base64 inflates by ~4/3, so stay well under.
MAX_PDF_BYTES = 20 * 1024 * 1024
SUPPORTED = {".pdf", ".docx", ".txt", ".md"}

SYSTEM_PROMPT = """\
You convert a person's resume into structured data for a job-application tool.

Rules:
- Copy text verbatim. Do not reword, summarize, improve, or add anything. Fix only obvious
  extraction artifacts (broken hyphenation across lines, stray bullet glyphs, doubled spaces).
- Every bullet point becomes its own bullet. Keep bullets attached to the job or project they
  appear under. If one company lists several roles, make one experience entry per role.
- Dates: use YYYY-MM when a month is given, otherwise YYYY. Use "Present" for current roles.
  Leave a date null if it isn't stated; never guess.
- Fields not present in the resume stay null or empty. Leave every `id` as an empty string.
- Put links (LinkedIn, GitHub, portfolio, personal site) in contact.links.
- Group skills under the categories the resume uses; if it has one flat list, use a single
  "Skills" group.
- Content that fits no section (volunteering, awards, publications, languages): put awards and
  publications under certifications if they are credential-like, otherwise as a project, and
  mention it in warnings.
- Add a warning for anything you were unsure about: unreadable text, ambiguous dates or order,
  possibly merged or split items.
"""


class ImportError_(RuntimeError):
    pass


@dataclass
class ImportResult:
    resume: MasterResume
    warnings: list[str] = field(default_factory=list)
    source_text: str = ""  # plain text of the source, for side-by-side review
    scanned: bool = False  # PDF had little or no extractable text (image-only)


def pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    return "\n\n".join((page.extract_text() or "") for page in reader.pages).strip()


def docx_text(path: Path) -> str:
    import docx

    d = docx.Document(str(path))
    parts: list[str] = []
    for para in d.paragraphs:
        if para.text.strip():
            prefix = "- " if (para.style is not None and "List" in (para.style.name or "")) else ""
            parts.append(prefix + para.text.strip())
    for table in d.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(dict.fromkeys(cells)))
    # Hyperlink targets aren't in paragraph text; collect them so links aren't lost.
    links = [
        rel.target_ref
        for rel in d.part.rels.values()
        if "hyperlink" in rel.reltype and rel.is_external
    ]
    if links:
        parts.append("Links: " + ", ".join(dict.fromkeys(links)))
    return "\n".join(parts)


def build_content(path: Path) -> tuple[list[dict], str, bool]:
    """Return (message content blocks, plain source text, scanned?)."""
    suffix = path.suffix.lower()
    instruction = {"type": "text", "text": "Convert this resume into the structured format."}

    if suffix == ".pdf":
        text = pdf_text(path)
        scanned = len(text) < 200
        size = path.stat().st_size
        if size <= MAX_PDF_BYTES:
            data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
            doc = {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": data},
            }
            return [doc, instruction], text, scanned
        if scanned:
            raise ImportError_(
                f"{path.name} is {size // (1024 * 1024)} MB and has no extractable text. "
                "Export a smaller PDF and try again."
            )
        return [{"type": "text", "text": f"<resume>\n{text}\n</resume>"}, instruction], text, False

    if suffix == ".docx":
        text = docx_text(path)
    elif suffix in {".txt", ".md"}:
        text = path.read_text(errors="replace")
    else:
        raise ImportError_(
            f"Unsupported file type '{suffix}'. Use one of: {', '.join(sorted(SUPPORTED))}"
        )
    if not text.strip():
        raise ImportError_(f"{path.name} appears to be empty.")
    return [{"type": "text", "text": f"<resume>\n{text}\n</resume>"}, instruction], text, False


def import_resume(path: Path, llm: LLM | None = None) -> ImportResult:
    path = path.expanduser()
    if not path.exists():
        raise ImportError_(f"File not found: {path}")
    content, source_text, scanned = build_content(path)
    llm = llm or LLM()
    extraction = llm.parse(
        task="resume_import",
        system=SYSTEM_PROMPT,
        content=content,
        output_format=ResumeExtraction,
        effort="medium",
        max_tokens=32000,
    )
    resume = extraction.resume
    # Ids are ours to assign; discard anything the model put there.
    for obj in store._walk(resume):
        obj.id = ""
    store.assign_ids(resume)
    warnings = list(extraction.warnings)
    if scanned:
        warnings.insert(
            0,
            "This PDF has little or no selectable text (likely scanned). The content was read "
            "from the page images; check names, dates, and numbers carefully.",
        )
    return ImportResult(resume=resume, warnings=warnings, source_text=source_text, scanned=scanned)
