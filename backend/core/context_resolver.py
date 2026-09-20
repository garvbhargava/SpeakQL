"""Step 3: make a follow-up self-contained (Backend Plan §11.1).

    "now break that down by region"

means nothing on its own. This rewrites it into a question that does, using
the thread's history -- so every later step, including the validator, sees one
complete question and never has to reason about conversational state.

**Bounded on purpose.** Only the last few turns are considered, and only the
question text. A resolver with unlimited memory produces answers nobody can
trace back to what they asked, and a long history is a long prompt.

When there is no antecedent, step 3 is **skipped** rather than guessed at, and
the pipeline ladder says so: "no antecedent". A resolver that invented context
would be the worst kind of wrong -- plausible.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_HISTORY = 3


@dataclass
class Resolved:
    question: str
    rewritten: bool
    antecedent: str | None = None
    note: str = ""


# Openings that cannot stand alone. A question starting with one of these is
# continuing something.
_CONTINUATION = re.compile(
    r"^\s*(now|then|and|also|what about|how about|ok(?:ay)?[, ]|"
    r"break (that|it) down|same (for|but)|instead|just|only)\b", re.I,
)

# Pronouns with no referent in the sentence itself.
_DANGLING = re.compile(r"\b(that|those|it|them|these|this one)\b", re.I)

# Just the conjunctions, for _combine -- see the comment there.
_CONJUNCTION = re.compile(r"^\s*(now|then|and|also|ok(?:ay)?)\b[, ]*", re.I)


def resolve(question: str, history: list[str]) -> Resolved:
    """Rewrite a follow-up into a self-contained question.

    `history` is the person's own recent questions in this thread, newest
    last. It is never another person's -- threads are scoped by person in
    object_access.py.
    """
    text = (question or "").strip()
    if not text:
        return Resolved(text, False, note="empty question")

    needs_context = bool(_CONTINUATION.match(text)) or bool(_DANGLING.search(text))
    if not needs_context:
        return Resolved(text, False, note="the question stands alone")

    recent = [h.strip() for h in history[-MAX_HISTORY:] if h and h.strip()]
    if not recent:
        # Nothing to resolve against. Skip the step and say so, rather than
        # inventing an antecedent.
        return Resolved(text, False, note="no antecedent")

    antecedent = recent[-1]
    rewritten = _combine(antecedent, text)

    return Resolved(
        question=rewritten,
        rewritten=True,
        antecedent=antecedent,
        note=f'resolved against "{_shorten(antecedent)}"',
    )


def _combine(antecedent: str, follow_up: str) -> str:
    """Produce one sentence that carries the subject of the first question.

    Deliberately literal. A model could phrase this better, but this step runs
    before generation on every follow-up, and a rewrite nobody can predict is
    a rewrite nobody can debug -- the answer would be traceable to a question
    the person never asked.
    """
    subject = _strip_lead(antecedent).rstrip("?.")
    # Strip only the leading conjunction here, not the whole continuation
    # phrase: "what about" and "break that down by" are the patterns matched
    # below, so removing them first would leave nothing to match.
    tail = _CONJUNCTION.sub("", follow_up or "").strip(" ,").rstrip("?.")

    if (m := re.match(r"^\s*break\s+(?:that|it)\s+down\s+by\s+(.+)$", tail, re.I)):
        return f"{subject}, broken down by {m.group(1).strip()}"

    if (m := re.match(r"^\s*(?:what|how)\s+about\s+(.+)$", tail, re.I)):
        return f"{subject}, for {m.group(1).strip()}"

    if (m := re.match(r"^\s*(?:same|but)\s+(?:for|in)\s+(.+)$", tail, re.I)):
        return f"{subject}, for {m.group(1).strip()}"

    # General case: state the original subject, then the modifier.
    cleaned = _DANGLING.sub("", tail).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return f"{subject}, {cleaned}" if cleaned else subject


def _strip_lead(text: str) -> str:
    return _CONTINUATION.sub("", text or "").strip(" ,")


def _shorten(text: str, limit: int = 48) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
