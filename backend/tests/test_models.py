"""The seams between the two trained models and the pipeline.

Not the models themselves -- their accuracy is measured by ml/*_eval.py
against a real warehouse, and a checkpoint is too large to keep in a test
suite. What is tested here is everything around them that can break without
either model noticing:

    the input format, which is a CONTRACT between training and inference
    retrieval's ranking, cutoff and join-path completion
    the confidence the router routes on
    that a missing checkpoint degrades to Gemma instead of failing

Every one of these can be wrong while the model is perfectly good, and none of
them would show up as an error -- only as a worse answer.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from core.router import Route, route
from core.schema_retriever import (
    Column, EmbeddingRetriever, LexicalRetriever, complete_join_paths,
    context_for, table_document, to_compact_schema, to_schema_text,
)
from core.sql_generator import Candidate, CodeT5Generator, Generator, model_input
from core.validator import Permitted

WAREHOUSE = {
    "regions": [("region_id", None), ("region_name", None)],
    "customers": [("customer_id", None), ("name", None),
                  ("region_id", "public.regions.region_id"), ("created_at", None)],
    "orders": [("order_id", None), ("customer_id", "public.customers.customer_id"),
               ("order_date", None), ("amount", None)],
    "shipments": [("shipment_id", None), ("order_id", "public.orders.order_id"),
                  ("shipped_on", None), ("carrier", None), ("units", None)],
}

COLUMNS = [
    Column("public", table, name, "text", None, True, reference,
           is_nullable=(name == "units"))
    for table, columns in WAREHOUSE.items() for name, reference in columns
]


def _scored(*tables):
    from core.schema_retriever import Scored  # noqa: PLC0415
    by_table: dict[str, list[Column]] = {}
    for column in COLUMNS:
        by_table.setdefault(column.table_name, []).append(column)
    return [Scored(f"public.{t}", 0.9, by_table[t]) for t in tables]


# ====================================================== the input format ====

def test_the_model_input_format_is_pinned():
    """This exact string is what ml/generator_train.py trains on.

    If it changes without retraining, the model is being asked a question in a
    language it was not taught -- and it will answer anyway, slightly worse,
    with nothing in any log to say why. Changing this line means retraining,
    and this test is here to make that a decision rather than an accident.
    """
    assert model_input("Which region sold most?", "orders : order_id , amount") == (
        "question: Which region sold most? | schema: orders : order_id , amount"
    )


def test_the_table_document_format_is_pinned():
    """Likewise for Model A: the index is built from this text."""
    assert table_document("orders", ["order_id", "amount"]) == (
        "table orders with columns order_id, amount"
    )


def test_both_schema_forms_carry_the_join_path():
    context = context_for(_scored("orders", "customers"))
    assert "REFERENCES public.customers (customer_id)" in context.ddl
    assert "customer_id -> customers.customer_id" in context.compact
    assert context.tables == ("public.orders", "public.customers")
    assert bool(context) is True


def test_the_compact_form_marks_what_can_be_missing():
    """CodeT5 sees "units?" and writes IS NULL; without it, units = 0."""
    compact = to_compact_schema(_scored("shipments"))
    assert "units?" in compact
    assert "carrier ," in compact or compact.endswith("carrier")


def test_a_viewer_never_sees_a_private_column_in_either_form():
    private = [Column("public", "orders", "amount", "numeric", None, False)]
    from core.schema_retriever import Scored  # noqa: PLC0415
    scored = [Scored("public.orders", 0.9, private)]
    assert to_schema_text(scored, public_only=True) == ""
    assert to_compact_schema(scored, public_only=True) == ""


# ========================================================= join completion ==

def test_the_bridge_table_is_added():
    """"Sales by region" scores orders and regions. customers is the only way
    between them and the question never mentions it."""
    retrieved = LexicalRetriever().search(
        "Which region had the highest total order amount?", COLUMNS)
    completed = complete_join_paths(retrieved, COLUMNS)

    tables = {s.table for s in completed if s.above_cutoff}
    assert "public.customers" in tables
    assert any(s.bridge for s in completed if s.table == "public.customers")


def test_a_single_table_question_gets_no_bridges():
    retrieved = LexicalRetriever().search(
        "How many shipments have no units recorded?", COLUMNS)
    completed = complete_join_paths(retrieved, COLUMNS)
    assert not [s for s in completed if s.bridge]


def test_the_join_path_is_stated_for_the_tables_that_are_sent():
    ddl = to_schema_text(_scored("orders", "customers", "regions"))
    assert "-- join path:" in ddl
    assert "public.orders.customer_id = public.customers.customer_id" in ddl


# ======================================================= Model A ranking ====

class _FakeEncoder:
    """Cosine by hand: "region" questions point at regions, and so on."""

    def __init__(self, table_scores: dict[str, float]):
        self.table_scores = table_scores

    def encode(self, texts, **kwargs):
        import numpy as np  # noqa: PLC0415

        vectors = []
        for text in texts:
            if text.startswith("table "):
                name = text.split(" ")[1]
                angle = self.table_scores.get(name, 0.0)
            else:
                angle = 1.0
            vectors.append([angle, (1 - angle ** 2) ** 0.5])
        return np.array(vectors, dtype="float32")


def _retriever(scores) -> EmbeddingRetriever:
    retriever = EmbeddingRetriever(checkpoint="ignored")
    retriever._model = _FakeEncoder(scores)      # noqa: SLF001 - that is the seam
    return retriever


def test_the_embedding_retriever_ranks_by_similarity():
    retriever = _retriever({"orders": 0.95, "customers": 0.80,
                            "regions": 0.75, "shipments": 0.05})
    results = retriever.search("what did we sell", COLUMNS, k=4)

    assert [s.table for s in results][:2] == ["public.orders", "public.customers"]
    assert results[0].score > results[-1].score


def test_an_unrelated_table_falls_below_the_cutoff():
    """On a six-table warehouse nothing is pruned; on a registered database
    with two hundred tables, almost everything is. The margin is what does it."""
    retriever = _retriever({"orders": 0.99, "customers": 0.98,
                            "regions": 0.97, "shipments": 0.10})
    results = retriever.search("what did we sell", COLUMNS, k=4)

    dropped = [s for s in results if not s.above_cutoff]
    assert [s.table for s in dropped] == ["public.shipments"]


def test_the_index_is_built_once_per_schema():
    retriever = _retriever({"orders": 0.9})
    retriever.search("first", COLUMNS)
    cached = len(retriever._cache)              # noqa: SLF001
    retriever.search("second", COLUMNS)
    assert len(retriever._cache) == cached == 1  # noqa: SLF001


# ====================================================== Model B wrapper =====

class _FakeTokenizer:
    pad_token_id = 0

    def __call__(self, texts, **kwargs):
        import torch  # noqa: PLC0415
        self.seen = texts
        return {"input_ids": torch.tensor([[1, 2, 3]]),
                "attention_mask": torch.tensor([[1, 1, 1]])}

    def decode(self, sequence, **kwargs):
        return "SELECT region_name FROM public.regions"


class _FakeModel:
    def __init__(self, score: float):
        self.score = score

    def generate(self, **kwargs):
        import torch  # noqa: PLC0415
        return SimpleNamespace(
            sequences=torch.tensor([[1, 2, 3]]),
            sequences_scores=torch.tensor([self.score]),
        )


def _codet5(score: float) -> CodeT5Generator:
    pytest.importorskip("torch")
    generator = CodeT5Generator("ignored")
    generator._model = _FakeModel(score)        # noqa: SLF001
    generator._tokenizer = _FakeTokenizer()     # noqa: SLF001
    return generator


def test_confidence_is_the_length_normalised_beam_score():
    """exp(sequences_scores) with length_penalty 1.0 is the per-token
    geometric mean probability -- comparable across lengths, which is the
    whole reason to normalise. The router routes on this number."""
    import math  # noqa: PLC0415

    assert _codet5(math.log(0.9)).generate("q", "s").confidence == 0.9
    assert _codet5(math.log(0.3)).generate("q", "s").confidence == 0.3


def test_the_generator_is_asked_in_the_trained_format():
    generator = _codet5(-0.1)
    context = context_for(_scored("regions"))
    generator.generate("Which region?", context)
    assert generator._tokenizer.seen == [  # noqa: SLF001
        model_input("Which region?", context.compact)
    ]


def test_a_missing_checkpoint_is_not_an_error():
    assert CodeT5Generator("nowhere/at/all").available() is False
    assert CodeT5Generator(None).available() is False


# ============================================================== routing =====

class _Refuses(Generator):
    name = "absent"

    def generate(self, question, schema):
        raise NotImplementedError("no checkpoint")


class _Writes(Generator):
    def __init__(self, sql: str, confidence: float, name: str = "stub"):
        self.sql, self.confidence, self.name = sql, confidence, name

    def generate(self, question, schema):
        return Candidate(self.sql, self.confidence, self.name)


PERMITTED = Permitted(frozenset({"public.regions"}))


def test_without_a_checkpoint_the_fallback_answers():
    """An untrained checkout still answers, through Gemma. That is the
    off-the-shelf baseline, not a broken state."""
    result = route("q", "schema", PERMITTED,
                   primary=_Refuses(),
                   fallback=_Writes("SELECT region_name FROM public.regions", 0.8),
                   threshold=0.55)
    assert result.route is Route.ESCALATED
    assert result.ok


def test_low_confidence_escalates_and_the_fallback_is_validated_too():
    result = route("q", "schema", PERMITTED,
                   primary=_Writes("SELECT region_name FROM public.regions", 0.2,
                                   "codet5-small"),
                   fallback=_Writes("SELECT * FROM other_tenant.payroll", 0.9),
                   threshold=0.55)
    # The fallback's answer is refused by the SAME validator. A bigger model
    # is given no more trust.
    assert result.route is Route.REFUSED


def test_a_confident_local_answer_never_calls_the_fallback():
    called = []

    class _Counts(_Writes):
        def generate(self, question, schema):
            called.append(1)
            return super().generate(question, schema)

    result = route("q", "schema", PERMITTED,
                   primary=_Writes("SELECT region_name FROM public.regions", 0.91,
                                   "codet5-small"),
                   fallback=_Counts("SELECT 1", 0.99),
                   threshold=0.55)
    assert result.route is Route.LOCAL
    assert result.generator == "codet5-small"
    assert not called, "the fallback ran for an answer that was already good"
