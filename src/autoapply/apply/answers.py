"""Answer memory: answer a question once, never again.

Two kinds of saved answers, both in ~/.autoapply/answers.yaml:
- standard: common questions recognized by meaning, however a company words them
  ("Are you legally authorized to work in the US?" / "Are you currently eligible to work
  legally in the United States?" -> `authorized`). Contact details default to your resume.
- learned: any other question, remembered by its (normalized) wording.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

import yaml

from .. import config
from ..models import MasterResume

# (key, description shown when asking, pattern on the normalized question). First match wins.
STANDARD: list[tuple[str, str, str]] = [
    ("preferred_name", "Preferred first name", r"preferred (first )?name"),
    ("first_name", "First name", r"^(legal )?first name|given name"),
    ("last_name", "Last name", r"^(legal )?last name|family name|surname"),
    ("full_name", "Full name", r"^(full |legal )?name$|^your name$|^full name"),
    ("email", "Email", r"\be ?mail\b"),
    ("phone", "Phone", r"\bphone\b|mobile number"),
    ("linkedin", "LinkedIn URL", r"linkedin"),
    ("github", "GitHub URL", r"github"),
    ("portfolio", "Website / portfolio URL", r"portfolio|personal (web)?site|^website|other website"),
    # Inverted wording ("authorized ... WITHOUT sponsorship?" -> Yes means no sponsorship needed):
    # kept as its own answer so it can never be confused with the plain sponsorship question.
    ("authorized_no_sponsorship", "Are you authorized to work in the US without needing sponsorship, now or in the future?",
     r"without (any )?(the )?(need (for|of) |needing |requiring )?(current or )?(future )?(employer |visa )?sponsor"),
    ("sponsorship", "Will you now or in the future require visa sponsorship?", r"sponsor|\bvisa\b|immigration support"),
    ("authorized", "Are you legally authorized to work in the US?", r"authori[sz]ed to work|eligible to work|legally (able|permitted|allowed|eligible)|right to work|work authori[sz]ation"),
    ("previously_employed", "Have you previously worked for the company you're applying to?",
     r"(previously|ever|formerly) (been )?(employed|worked|work) (by|for|at)|former employee|worked (for|at) [a-z0-9 ]+ (before|previously)"),
    ("age_18", "Are you at least 18 years old?", r"(at least|over|older than) (the age of )?18|18 years"),
    ("how_heard", "How did you hear about this job?", r"how did you (hear|find|learn)|where did you (hear|find|learn)|referral source|how were you referred"),
    ("salary", "Salary expectation", r"salary|compensation (expectation|requirement)|expected (pay|compensation)|desired (pay|compensation)"),
    ("start_date", "When can you start?", r"start date|when (can|could) you start|earliest (start|availability)|notice period|available to start"),
    ("relocate", "Are you willing to relocate?", r"relocat"),
    ("current_company", "Current company", r"current (company|employer)|most recent (company|employer)|^company$"),
    ("current_title", "Current job title", r"current (job |role )?title|most recent (job )?title"),
    ("school", "School", r"\bschool\b|university|college|institution"),
    ("degree", "Degree", r"^degree|highest (level of )?(education|degree)|degree (type|level)"),
    ("discipline", "Major / field of study", r"discipline|\bmajor\b|field of study|area of study"),
    ("grad_date", "Graduation date", r"graduat(ion|e) (date|year)|when (did|will) you graduate|expected graduation"),
    ("transgender", "Do you identify as transgender?", r"transgender"),
    ("sexual_orientation", "Sexual orientation", r"sexual orientation"),
    ("gender", "Gender", r"gender"),
    ("hispanic", "Are you Hispanic or Latino?", r"hispanic|latin[oax]"),
    ("race", "Race / ethnicity", r"\brace\b|ethnicit"),
    ("veteran", "Veteran status", r"veteran|military|armed forces"),
    ("disability", "Disability status", r"disabilit"),
    ("pronouns", "Pronouns", r"pronoun"),
    ("country", "Country you live in", r"^country\b|country of residence|which country|what country"),
    ("city", "City", r"^city$|^current city"),
    ("state", "State / province", r"^state$|^state ?/ ?province|^province"),
    ("zip", "ZIP / postal code", r"\bzip\b|postal code"),
    ("location", "Current location (City, State)", r"location|where are you (currently )?(based|located|living)|city and state|where do you (currently )?live"),
]

# Never filled from a guess: if there's no saved answer, ask.
SENSITIVE = {"authorized", "authorized_no_sponsorship", "sponsorship", "age_18", "transgender", "sexual_orientation", "gender", "hispanic",
             "race", "veteran", "disability", "salary"}

DECLINE_RE = re.compile(r"decline|prefer not|not wish|don.?t wish|choose not|rather not|not to (say|disclose|answer|identify)|i don.?t want", re.I)
YES = {"yes", "y", "true", "i am", "i do"}
NO = {"no", "n", "false", "i am not", "i do not"}
SYNONYMS = {"us": "united states", "usa": "united states", "u s": "united states", "u s a": "united states",
            "united states of america": "united states", "uk": "united kingdom"}


def normalize(text: str) -> str:
    t = text.lower().replace("✱", " ").replace("*", " ")
    t = re.sub(r"\((required|optional)\)", " ", t)
    t = re.sub(r"[^a-z0-9+#]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


# Keys answered with a plain fact (name, city, school…) only match short, form-style labels;
# a long question that merely mentions "location" or "company" is something else.
FACT_KEYS = {"preferred_name", "first_name", "last_name", "full_name", "email", "phone", "linkedin", "github",
             "portfolio", "current_company", "current_title", "school", "degree", "discipline", "grad_date",
             "country", "city", "state", "zip", "location"}


def standard_key(question: str) -> str | None:
    q = normalize(question)
    words = len(q.split())
    for key, _, pattern in STANDARD:
        if re.search(pattern, q):
            if key in FACT_KEYS and words > 9:
                continue
            return key
    return None


def describe(key: str) -> str:
    return next((d for k, d, _ in STANDARD if k == key), key)


def _tokens(s: str) -> set[str]:
    return set(normalize(s).split())


def similarity(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


# ---------------------------------------------------------------------------
# matching an answer to a field's options
# ---------------------------------------------------------------------------


def _canon(s: str) -> str:
    n = normalize(s)
    return SYNONYMS.get(n, n)


def match_option(answer: str, options: list[str]) -> int | None:
    """Index of the option that best matches a saved answer, or None."""
    if not options or answer is None:
        return None
    a = _canon(str(answer))
    canon = [_canon(o) for o in options]
    if a in canon:
        return canon.index(a)
    if a in YES or a in NO:
        want = "yes" if a in YES else "no"
        for i, o in enumerate(canon):
            if o == want or o.startswith(want + " "):
                return i
    if DECLINE_RE.search(str(answer)):
        for i, o in enumerate(options):
            if DECLINE_RE.search(o):
                return i
    for i, o in enumerate(canon):
        if o and (o.startswith(a) or a.startswith(o)) and min(len(a), len(o)) >= 2:
            return i
    for i, o in enumerate(canon):
        if len(a) >= 4 and a in o:
            return i
    best, best_i = 0.0, None
    for i, o in enumerate(options):
        s = similarity(str(answer), o)
        if s > best:
            best, best_i = s, i
    return best_i if best >= 0.5 else None


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------


class AnswerStore:
    """~/.autoapply/answers.yaml: `standard: {key: answer}` and `learned: {normalized q: {question, answer}}`."""

    def __init__(self, data: dict | None = None):
        data = data or {}
        self.standard: dict[str, object] = dict(data.get("standard") or {})
        self.learned: dict[str, dict] = dict(data.get("learned") or {})

    @classmethod
    def load(cls) -> "AnswerStore":
        path = config.answers_path()
        if not path.exists():
            return cls()
        raw = yaml.safe_load(path.read_text()) or {}
        if "standard" not in raw and "learned" not in raw:
            raw = {}  # old placeholder format from `init`
        return cls(raw)

    def save(self) -> None:
        path = config.answers_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        header = ("# AutoApply saved answers. `standard` answers are reused for any wording of the same\n"
                  "# question; `learned` ones for the same question text. Edit or delete freely.\n")
        body = {"standard": self.standard, "learned": self.learned}
        path.write_text(header + yaml.safe_dump(body, sort_keys=False, allow_unicode=True, width=100))

    # lookups

    def lookup(self, question: str) -> tuple[object, str, str | None] | None:
        """(answer, source, key) for a question, or None. source: 'saved' | 'similar'."""
        norm = normalize(question)
        if norm in self.learned:
            return self.learned[norm]["answer"], "saved", norm
        key = standard_key(question)
        if key and key in self.standard and self.standard[key] not in (None, ""):
            return self.standard[key], "saved", key
        best, best_key = 0.0, None
        for k, v in self.learned.items():
            s = similarity(question, v.get("question", k))
            if s > best:
                best, best_key = s, k
        if best_key and best >= 0.8:
            return self.learned[best_key]["answer"], "similar", best_key
        return None

    def remember(self, question: str, answer, prefer_standard: bool = True) -> str:
        key = standard_key(question) if prefer_standard else None
        if key:
            self.standard[key] = answer
            return key
        norm = normalize(question)
        self.learned[norm] = {"question": question.strip(), "answer": answer,
                              "saved": datetime.now(timezone.utc).strftime("%Y-%m-%d")}
        return norm

    def forget(self, key: str) -> bool:
        if key in self.standard:
            del self.standard[key]
            return True
        if key in self.learned:
            del self.learned[key]
            return True
        return False


# ---------------------------------------------------------------------------
# defaults from the master resume
# ---------------------------------------------------------------------------


@dataclass
class ResumeFacts:
    values: dict[str, str]

    @classmethod
    def from_resume(cls, r: MasterResume | None) -> "ResumeFacts":
        v: dict[str, str] = {}
        if r is None:
            return cls(v)
        c = r.contact
        if c.name:
            parts = c.name.strip().split()
            name = " ".join(p.capitalize() if p.isupper() else p for p in parts)
            v["full_name"] = name
            v["first_name"] = name.split()[0]
            v["preferred_name"] = name.split()[0]
            if len(parts) > 1:
                v["last_name"] = " ".join(name.split()[1:])
        if c.email:
            v["email"] = c.email
        if c.phone:
            v["phone"] = c.phone
        if c.location:
            v["location"] = c.location
            bits = [b.strip() for b in c.location.split(",")]
            v["city"] = bits[0]
            if len(bits) > 1:
                v["state"] = bits[1]
        for link in c.links:
            url = link.url if link.url.startswith("http") else f"https://{link.url}"
            low = (link.label + " " + link.url).lower()
            if "linkedin" in low:
                v["linkedin"] = url
            elif "github" in low:
                v["github"] = url
            else:
                v.setdefault("portfolio", url)
        current = next((e for e in r.experience if (e.end or "").lower() in ("present", "current", "")), None)
        if current:
            v["current_company"] = current.company
            v["current_title"] = current.title
        if r.education:
            ed = r.education[0]
            v["school"] = ed.school
            if ed.degree:
                v["degree"] = ed.degree
            if ed.field:
                v["discipline"] = ed.field
            if ed.end:
                v["grad_date"] = ed.end
        return cls(v)
