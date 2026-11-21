"""Local "how much is this job like my resume" score, 0-100. No AI calls, so it's free and instant.

score = 55% title similarity (job title vs. the profile's target titles)
      + 45% skill coverage (resume skills mentioned in the job title/description)
      + up to 10 points when the title's seniority wording fits your level
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache

from .profile import SearchProfile
from .search import stem_words


@dataclass
class Match:
    score: int
    skills: list[str]


@lru_cache(maxsize=2048)
def _skill_re(skill: str) -> re.Pattern:
    s = skill.strip()
    body = re.escape(s)
    flags = 0 if len(s) <= 2 else re.IGNORECASE  # "Go", "R", "C": only exact case
    return re.compile(rf"(?<![A-Za-z0-9+#]){body}(?![A-Za-z0-9+#])", flags)


def _title_similarity(job_title: str, targets: list[str]) -> float:
    """Mostly 'does the title contain the target title', slightly penalizing long, specialized titles."""
    jw = stem_words(job_title)
    best = 0.0
    for t in targets:
        tw = stem_words(t)
        if tw and jw:
            common = len(tw & jw)
            best = max(best, 0.75 * common / len(tw) + 0.25 * common / len(jw))
    return best


_LEVEL_HINTS = {
    "intern": r"\b(intern|internship|co-?op)\b",
    "entry": r"\b(new grad|graduate|entry|junior|jr|early career|associate|university|engineer i)\b",
    "mid": r"\b(ii|2|mid)\b",
    "senior": r"\b(senior|sr|staff|principal|lead)\b",
}


def _level_bonus(title: str, level: str) -> int:
    pattern = _LEVEL_HINTS.get(level)
    return 10 if pattern and re.search(pattern, title, re.IGNORECASE) else 0


def score(title: str, description: str, prof: SearchProfile) -> Match:
    text = f"{title}\n{description}"
    hits = [s for s in prof.skills if s.strip() and _skill_re(s).search(text)]
    denom = max(5, min(len(prof.skills), 12))
    skill_part = min(1.0, len(hits) / denom)
    title_part = _title_similarity(title, prof.titles)
    total = 100 * (0.55 * title_part + 0.45 * skill_part) + _level_bonus(title, prof.level)
    return Match(score=min(100, round(total)), skills=hits[:6])
