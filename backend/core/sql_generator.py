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
    def generate(self, question: str, schema) -> Candidate: ...


def _ddl(schema) -> str:
    """Accept a SchemaContext or a plain string, and take the DDL form."""
    return getattr(schema, "ddl", schema) or ""


def _compact(schema) -> str:
    """The same, for the form CodeT5 was trained on."""
    return getattr(schema, "compact", schema) or ""


def model_input(question: str, schema_compact: str) -> str:
    """The exact string CodeT5 is fed, at training time and at request time.

    **A contract with ml/**: ml/serialize.py imports this. Serialising one way
    in training and another way here would cost accuracy with nothing in any
    log to explain it.
    """
    return f"question: {question.strip()} | schema: {schema_compact}"


# ------------------------------------------------------------------ Gemma ----

# The worked example is not decoration. Without it this model wrote
# SUM(t2.orders.amount) -- a column of a table it had not joined -- on a
# schema that carried every table it needed. With it, four of the five demo
# questions came back correct instead of two.
_SQL_INSTRUCTION = """\
You write one PostgreSQL SELECT statement and nothing else.

Rules:
- Output SQL only. No explanation, no markdown fence, no commentary.
- Exactly one statement. Never more than one.
- SELECT only. Never INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE or CREATE.
- Every column you name must appear in the schema below, under the table you
  read it from. A column of a table you have not joined is not available:
  join that table first, along the join path given under the schema.
- Write joins as: FROM a JOIN b ON b.key = a.key. Never write t.other_table.col.
- A value that was never recorded is NULL, not 0. "no X", "missing X" and
  "X not recorded" mean X IS NULL.
- If the question cannot be answered from this schema, output exactly:
  CANNOT_ANSWER

Worked example, for a different schema:
  schema:   CREATE TABLE staff ( staff_id integer NOT NULL, office_id integer NOT NULL REFERENCES offices (office_id), );
            CREATE TABLE offices ( office_id integer NOT NULL, city text NOT NULL, );
            CREATE TABLE hours ( staff_id integer NOT NULL REFERENCES staff (staff_id), logged numeric NULL, );
  question: Which city logged the most hours?
  answer:   SELECT o.city, SUM(h.logged) AS logged FROM hours h JOIN staff s ON s.staff_id = h.staff_id JOIN offices o ON o.office_id = s.office_id GROUP BY o.city ORDER BY logged DESC LIMIT 1
"""


class GemmaGenerator(Generator):
    """The LLM path. Used below the confidence threshold, and -- until Model B
    is trained -- for every question."""

    def __init__(self, client: LLMClient) -> None:
        self.client = client
        self.name = client.model

    def generate(self, question: str, schema) -> Candidate:
        schema_text = _ddl(schema)
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
    """The fine-tuned in-house generator (Model B).

    60M parameters, trained in two stages by ml/generator_train.py: Spider
    first, this warehouse second. It answers in well under a second on CPU,
    which is what makes it the primary rather than a curiosity -- Gemma takes
    tens of seconds on the same machine.

    Its confidence is a **real decoding score**: the length-normalised
    log-probability of the beam that was returned, exponentiated, so it is the
    per-token geometric mean probability. That is the number the router routes
    on and the number the week-9 threshold sweep will calibrate. Gemma has no
    such score and gets a structural estimate instead -- the two are not the
    same measurement and the answer says which one it is.

    If the checkpoint is missing it raises NotImplementedError, and the router
    falls through to Gemma rather than the request failing.
    """

    name = "codet5-small"
    NUM_BEAMS = 4
    MAX_NEW_TOKENS = 160
    MAX_INPUT = 384

    def __init__(self, checkpoint_path: str | None = None) -> None:
        self.checkpoint_path = checkpoint_path
        self._model = None
        self._tokenizer = None

    # -- loading -----------------------------------------------------------

    def available(self) -> bool:
        try:
            self._load()
            return True
        except NotImplementedError:
            return False

    def _load(self):
        if self._model is not None:
            return self._model, self._tokenizer
        if not self.checkpoint_path:
            raise NotImplementedError("no CodeT5 checkpoint configured")
        try:
            import torch  # noqa: PLC0415, F401
            from transformers import (  # noqa: PLC0415
                AutoTokenizer, T5ForConditionalGeneration,
            )
        except ImportError as exc:
            raise NotImplementedError(
                "CodeT5 needs torch and transformers; the pipeline falls back "
                "to Gemma without them"
            ) from exc

        try:
            self._tokenizer = AutoTokenizer.from_pretrained(self.checkpoint_path)
            self._model = T5ForConditionalGeneration.from_pretrained(
                self.checkpoint_path)
            self._model.eval()
        except Exception as exc:  # noqa: BLE001 - an absent checkpoint is normal
            raise NotImplementedError(
                f"no usable CodeT5 checkpoint at {self.checkpoint_path}: {exc}"
            ) from exc
        log.info("CodeT5 loaded from %s", self.checkpoint_path)
        return self._model, self._tokenizer

    def warm(self) -> bool:
        """Load and run once, so the first question is not the slow one."""
        try:
            self.generate("warm up", "orders : order_id , amount")
            return True
        except NotImplementedError:
            return False
        except Exception:  # noqa: BLE001 - warming never breaks a startup
            return True

    # -- generation --------------------------------------------------------

    def generate(self, question: str, schema) -> Candidate:
        import time  # noqa: PLC0415

        import torch  # noqa: PLC0415

        model, tokenizer = self._load()
        text = model_input(question, _compact(schema))

        started = time.monotonic()
        inputs = tokenizer([text], max_length=self.MAX_INPUT, truncation=True,
                           return_tensors="pt")
        with torch.no_grad():
            output = model.generate(
                **inputs,
                max_new_tokens=self.MAX_NEW_TOKENS,
                num_beams=self.NUM_BEAMS,
                early_stopping=True,
                length_penalty=1.0,          # sequences_scores stays comparable
                output_scores=True,
                return_dict_in_generate=True,
            )
        elapsed = int((time.monotonic() - started) * 1000)

        sql = tokenizer.decode(output.sequences[0], skip_special_tokens=True).strip()

        # sequences_scores is sum(log p) / length^length_penalty. With the
        # penalty at 1.0, exp() of it is the per-token geometric mean
        # probability -- comparable across statements of different lengths,
        # which is the whole point of normalising.
        score = getattr(output, "sequences_scores", None)
        confidence = float(torch.exp(score[0])) if score is not None else 0.5

        return Candidate(
            sql=_strip_to_sql(sql),
            confidence=round(min(1.0, max(0.0, confidence)), 3),
            generator=self.name,
            elapsed_ms=elapsed,
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
