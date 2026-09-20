"""Step 6: question + retrieved schema -> candidate SQL (Backend Plan §14).

The generator receives the question plus **only the retrieved schema subset**,
never the whole 18-table schema. Two reasons: shorter input decodes faster and
more accurately, and restricting the input is exactly what makes retrieval
matter. Feeding the full schema is kept as an ablation condition to prove it.

Two backends, one interface:

    CodeT5Generator   the fine-tuned model (arrives in build phase 5)
    GemmaGenerator    the LLM, via llm_client

Whichever produced the SQL, **the same validator runs on the result**. There
is no softer path for the fallback: a model with more parameters is given no
more trust.
"""

from __future__ import annotations

import logging
import math
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

from core.llm_client import LLMClient, LLMUnavailable, build_prompt

log = logging.getLogger("speakql.generator")


@dataclass(frozen=True)
class Candidate:
    sql: str
    confidence: float     # 0-1, length-normalised
    generator: str        # codet5 | gemma3:4b | ...
    elapsed_ms: int = 0


class Generator(ABC):
    name: str = "abstract"

    @abstractmethod
    def generate(self, question: str, schema_text: str) -> Candidate: ...


# ------------------------------------------------------------------ Gemma ----

_SQL_INSTRUCTION = """\
You write one PostgreSQL SELECT statement and nothing else.

Rules:
- Output SQL only. No explanation, no markdown fence, no commentary.
- Exactly one statement. Never more than one.
- SELECT only. Never INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE or CREATE.
- Use only the tables and columns given in the schema below.
- If the question cannot be answered from this schema, output exactly:
  CANNOT_ANSWER
"""


class GemmaGenerator(Generator):
    """The LLM path. Used below the confidence threshold, and -- until Model B
    is trained -- for every question."""

    def __init__(self, client: LLMClient) -> None:
        self.client = client
        self.name = client.model

    def generate(self, question: str, schema_text: str) -> Candidate:
        prompt = build_prompt(
            _SQL_INSTRUCTION + f"\nQuestion: {question.strip()}",
            schema=schema_text,
        )
        completion = self.client.complete(prompt, temperature=0.0, max_tokens=400)
        sql = _strip_to_sql(completion.text)

        if sql.strip().upper().startswith("CANNOT_ANSWER"):
            # An honest refusal from the model. It is not an error, and it is
            # not an answer -- the pipeline turns it into the refusal mode.
            return Candidate("", 0.0, self.name, completion.elapsed_ms)

        # An API model returns no beam score, so confidence here is a
        # structural estimate rather than a decoding score. It is labelled as
        # such wherever it is displayed, because claiming otherwise would be
        # claiming a measurement we did not make.
        return Candidate(
            sql=sql,
            confidence=estimate_confidence(sql, schema_text),
            generator=self.name,
            elapsed_ms=completion.elapsed_ms,
        )


# ----------------------------------------------------------------- CodeT5 ----

class CodeT5Generator(Generator):
    """The fine-tuned in-house generator. Build phase 5.

    Left unimplemented deliberately rather than stubbed with a fake number:
    a generator that silently returned a plausible confidence would corrupt
    the router and the ablation table at once.
    """

    name = "codet5-small"

    def __init__(self, checkpoint_path: str | None = None) -> None:
        self.checkpoint_path = checkpoint_path

    def generate(self, question: str, schema_text: str) -> Candidate:
        raise NotImplementedError(
            "CodeT5 is trained in build phase 5. Until then the router has one "
            "arm and Gemma generates every query -- see README, Roadmap."
        )


# ------------------------------------------------------------------ utils ----

_FENCE_RE = re.compile(r"^\s*```(?:sql)?\s*|\s*```\s*$", re.IGNORECASE | re.MULTILINE)
_LEAD_RE = re.compile(r"^\s*(here(?:'s| is)[^\n:]*:|sql:)\s*", re.IGNORECASE)


def _strip_to_sql(text: str) -> str:
    """Models wrap SQL in fences and preambles however firmly you ask them not
    to. Strip the wrapper; never rewrite the statement itself."""
    cleaned = _FENCE_RE.sub("", text or "").strip()
    cleaned = _LEAD_RE.sub("", cleaned).strip()
    # Keep everything up to the first semicolon that ends a statement -- but
    # do NOT use this to make a multi-statement string safe. The validator
    # refuses those, and it must keep seeing them to do so.
    return cleaned


def estimate_confidence(sql: str, schema_text: str) -> float:
    """A structural score in 0-1 for generators that return no beam score.

    It reads the shape of the statement, not its meaning: does it reference
    tables that were actually retrieved, is it complete, does it join without
    a condition. Correlates with correctness well enough to route on, and is
    never presented as a probability that the SQL is right.

    CodeT5 replaces this with the real length-normalised beam score in phase 5.
    """
    if not sql.strip():
        return 0.0

    score = 0.5
    lowered = sql.lower()

    known = {t.lower() for t in re.findall(r"\b([a-z_][a-z0-9_]*)\s*\(", schema_text.lower())}
    known |= {t.lower() for t in re.findall(r"^\s*([a-z_][a-z0-9_]*)\b", schema_text.lower(), re.M)}

    referenced = set(re.findall(r"\bfrom\s+([a-z_][a-z0-9_.]*)|\bjoin\s+([a-z_][a-z0-9_.]*)",
                                lowered))
    flat = {a or b for a, b in referenced}
    flat = {t.split(".")[-1] for t in flat if t}

    if flat:
        hits = len(flat & known) / len(flat)
        score += 0.25 * hits
    else:
        score -= 0.2

    if " join " in lowered and " on " not in lowered:
        score -= 0.25                      # a cartesian product is usually a bug
    if lowered.count("select") > 1:
        score -= 0.05                      # subqueries are fine, just less certain
    if len(sql) < 25:
        score -= 0.15                      # suspiciously short
    if "group by" in lowered and "select" in lowered:
        score += 0.05

    # length normalisation, in the same spirit as the beam score it stands in
    # for: longer statements should not be penalised merely for being longer
    score += 0.05 * math.tanh(len(sql) / 300)

    return round(max(0.0, min(1.0, score)), 3)
