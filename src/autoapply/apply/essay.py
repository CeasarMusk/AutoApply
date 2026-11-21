"""Draft answers to open-ended questions ("Why do you want to work here?") from the resume + job."""

from __future__ import annotations

import yaml
from pydantic import BaseModel, Field

from ..llm import LLM
from ..models import Job, MasterResume


class EssayAnswer(BaseModel):
    answer: str = Field(description="The answer text, ready to paste into the form")


SYSTEM = """\
You draft answers to job-application questions for the applicant, in their voice (first person).
Rules:
- Use only facts from the resume. Never invent experience, numbers, employers, or skills.
- Be specific to this company and role, using the job description; no generic filler or flattery.
- Plain text, no markdown, no greeting or sign-off. Keep it to 60-150 words unless the question
  clearly asks for more or less. If the question is factual (e.g. a link or a number), answer briefly.
- If the resume has nothing relevant, write a short honest answer rather than making something up.
"""


def draft(question: str, job: Job, resume: MasterResume, llm: LLM | None = None) -> str:
    llm = llm or LLM()
    resume_text = yaml.safe_dump(resume.model_dump(mode="json", exclude_none=True), sort_keys=False, allow_unicode=True)
    content = (
        f"<resume>\n{resume_text}\n</resume>\n\n"
        f"<job company=\"{job.company}\" title=\"{job.title}\">\n{(job.description or '')[:8000]}\n</job>\n\n"
        f"Question: {question}"
    )
    return llm.parse(task="essay", system=SYSTEM, content=content, output_format=EssayAnswer,
                     effort="medium", max_tokens=4000).answer.strip()
