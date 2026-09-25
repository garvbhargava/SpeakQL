"""Steps 2 and 5: the intent gatekeeper and the ambiguity check.

**Layer 1 of the four safety layers** (Backend Plan §6). It runs *before*
generation, which is what makes it a different thing from the AST validator
rather than a duplicate of it.

The distinction matters and the interface shows it: a destructive question is
caught *after* generation, with a statement to display struck through. A
credential question is caught *before* generation, so there is no statement to
show at all. The pipeline ladder, not the wording, is what tells a person
which happened.

This layer is on the synopsis deck's cut list -- second, after the chart
classifier. That is correct and worth understanding: **removing it costs
convenience, not safety**, because the AST validator refuses every dangerous
statement anyway. What this buys is a faster, clearer refusal and one fewer
pointless model call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Intent(str, Enum):
    ANSWERABLE = "answerable"
    CREDENTIALS = "credentials"
    DESTRUCTIVE = "destructive"
    SYSTEM_PROBE = "system_probe"
    PROMPT_INJECTION = "prompt_injection"
    NOT_A_QUESTION = "not_a_question"


@dataclass
class Screening:
    intent: Intent
    reason: str = ""
    matched: str = ""

    @property
    def allowed(self) -> bool:
        return self.intent is Intent.ANSWERABLE


# These are phrased as intent, not as a SQL blocklist. The SQL blocklist would
# be the mistake -- that job belongs to the parser, which does it properly.
_CREDENTIAL = re.compile(
    r"\b(password|passwd|credential|secret|api[ _-]?key|token|"
    r"connection string|dsn|private key|env(?:ironment)? variable)\b", re.I,
)
_DESTRUCTIVE = re.compile(
    r"\b(delete|drop|truncate|erase|wipe|remove all|purge|destroy)\b", re.I,
)
_SYSTEM_PROBE = re.compile(
    r"\b(pg_\w+|information_schema|system catalog|list (all )?(users|roles|databases)|"
    r"who (are|is) the (admin|superuser))\b", re.I,
)
# The qualifiers repeat: "ignore ALL PREVIOUS instructions" is the commonest
# phrasing there is, and the first version of this allowed exactly one of them
# -- so it caught "ignore previous instructions" and let the canonical form
# through. Layer 1 is not the only defence (data is fenced, and the validator
# refuses anything that is not a read-only SELECT) but it is the layer that is
# supposed to catch this one.
_INJECTION = re.compile(
    r"("
    r"(?:ignore|disregard|forget|override)\s+"
    r"(?:all\s+|any\s+|the\s+|your\s+|previous\s+|prior\s+|above\s+|earlier\s+)*"
    r"(?:instruction|rule|prompt|direction|guideline)s?|"
    r"disregard\s+(?:the|your|all)\s+above|"
    r"you are now|act as|pretend (?:to be|you are)|system prompt|"
    r"(?:reveal|print|repeat|show|output)\s+(?:me\s+)?"
    r"(?:your|the)\s+(?:system\s+)?(?:prompt|instructions)"
    r")", re.I,
)


def screen(question: str) -> Screening:
    """Layer 1. Runs before anything is generated."""
    text = (question or "").strip()
    if len(text) < 3:
        return Screening(Intent.NOT_A_QUESTION, "there is no question here")

    if (m := _INJECTION.search(text)):
        return Screening(
            Intent.PROMPT_INJECTION,
            "this asks the system to change its own instructions",
            m.group(0),
        )
    if (m := _CREDENTIAL.search(text)):
        return Screening(
            Intent.CREDENTIALS,
            "this asks for credentials. SpeakQL answers questions about data, "
            "and holds no path to its own secrets",
            m.group(0),
        )
    if (m := _DESTRUCTIVE.search(text)):
        return Screening(
            Intent.DESTRUCTIVE,
            "this asks to change or remove data. Every query runs read-only",
            m.group(0),
        )
    if (m := _SYSTEM_PROBE.search(text)):
        return Screening(
            Intent.SYSTEM_PROBE,
            "this asks about the database's own structure rather than its data",
            m.group(0),
        )

    return Screening(Intent.ANSWERABLE)


# ------------------------------------------------------ step 5: ambiguity ----

@dataclass
class Ambiguity:
    ambiguous: bool
    term: str = ""
    options: list[dict] = field(default_factory=list)
    note: str = ""


# Words that are defensible in several ways, where guessing produces a
# confidently wrong answer rather than a slightly wrong one.
_AMBIGUOUS_TERMS: dict[str, list[tuple[str, str]]] = {
    "best": [
        ("Highest revenue", "SUM of amount"),
        ("Fastest growth", "change month over month"),
        ("Best margin", "(amount - cost) / amount"),
    ],
    "top": [
        ("By value", "ordered by the measure"),
        ("By volume", "ordered by count"),
    ],
    "performance": [
        ("Revenue", "SUM of amount"),
        ("Volume", "COUNT of orders"),
        ("Margin", "(amount - cost) / amount"),
    ],
    "recent": [
        ("Last 7 days", "rolling week"),
        ("Last 30 days", "rolling month"),
        ("This calendar month", "month to date"),
    ],
}


def check_ambiguity(question: str) -> Ambiguity:
    """Ask back once, rather than guess and be confidently wrong.

    One round, then it commits: a question still ambiguous after one
    clarification gets the best-guess answer with its assumption stated. One
    clarification is good service; three is a broken interface.
    """
    words = set(re.findall(r"[a-z]+", (question or "").lower()))
    for term, options in _AMBIGUOUS_TERMS.items():
        if term in words:
            return Ambiguity(
                ambiguous=True,
                term=term,
                options=[{"label": label, "definition": how} for label, how in options],
                note=(
                    f'"{term}" has several defensible readings here, and they '
                    "give different answers. Rather than guess and be "
                    "confidently wrong, pick one."
                ),
            )
    return Ambiguity(ambiguous=False)
