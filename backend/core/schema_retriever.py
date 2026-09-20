"""Step 4: which tables is this question actually about? (Backend Plan §14)

The generator receives only the retrieved subset, never the whole schema.
Restricting the input is what makes retrieval matter -- and feeding the full
schema is kept as an ablation condition to prove exactly that.

Two implementations behind one interface:

    LexicalRetriever    token overlap. No model, no index, works on day one.
    EmbeddingRetriever  MiniLM + FAISS. Build phase 3, and the measured one.

The lexical retriever is not a placeholder to be deleted. It is the
**off-the-shelf baseline** in the comparative study: slide 11's first row is
"off-the-shelf retrieval + LLM", and this is what produces it. Keeping it
means the ablation has something real to compare against.
"""

from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass

log = logging.getLogger("speakql.retriever")

DEFAULT_K = 8
CUTOFF = 0.15   # below this a table is not sent to the generator at all


@dataclass(frozen=True)
class Column:
    schema_name: str
    table_name: str
    column_name: str
    data_type: str
    description: str | None = None
    is_public: bool = False

    @property
    def qualified_table(self) -> str:
        return f"{self.schema_name}.{self.table_name}"


@dataclass
class Scored:
    table: str
    score: float
    columns: list[Column]

    @property
    def above_cutoff(self) -> bool:
        return self.score >= CUTOFF


class Retriever(ABC):
    name = "abstract"

    @abstractmethod
    def search(self, question: str, columns: list[Column], k: int = DEFAULT_K) -> list[Scored]: ...


# --------------------------------------------------------------- lexical ----

_WORD = re.compile(r"[a-z0-9]+")
_STOP = {
    "the", "a", "an", "of", "for", "in", "on", "by", "to", "and", "or", "is",
    "was", "were", "what", "which", "how", "many", "much", "show", "me", "our",
    "we", "us", "give", "list", "all", "from", "did", "do", "does", "per",
    "last", "this", "that", "with", "at", "be", "have", "has",
}


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD.findall((text or "").lower()) if w not in _STOP and len(w) > 2}


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("ses") or word.endswith("xes"):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _expand(words: set[str]) -> set[str]:
    out = set(words)
    for w in words:
        out.add(_singular(w))
        out.update(p for p in w.split("_") if len(p) > 2)
    return out


class LexicalRetriever(Retriever):
    """Token overlap between the question and each table's identifiers.

    Deliberately simple and deliberately explainable: every score can be
    traced to the words that produced it, which is useful while the pipeline
    is being built and honest as a baseline afterwards.
    """

    name = "lexical (off-the-shelf baseline)"

    def search(self, question: str, columns: list[Column],
               k: int = DEFAULT_K) -> list[Scored]:
        asked = _expand(_tokens(question))
        if not asked:
            return []

        by_table: dict[str, list[Column]] = {}
        for column in columns:
            by_table.setdefault(column.qualified_table, []).append(column)

        scored: list[Scored] = []
        for table, cols in by_table.items():
            bare = table.split(".")[-1]
            table_words = _expand(_tokens(bare))
            column_words: set[str] = set()
            for c in cols:
                column_words |= _expand(_tokens(c.column_name))
                if c.description:
                    column_words |= _expand(_tokens(c.description))

            # The table name matching is worth more than a column matching:
            # "shipments" in the question is a much stronger signal than
            # "date", which appears in every table.
            table_hits = len(asked & table_words)
            column_hits = len(asked & column_words)

            score = (table_hits * 0.6 + column_hits * 0.12) / max(1, len(asked) ** 0.5)
            scored.append(Scored(table, round(min(1.0, score), 3), cols))

        scored.sort(key=lambda s: s.score, reverse=True)
        return scored[:k]


# ------------------------------------------------------------- embedding ----

class EmbeddingRetriever(Retriever):
    """MiniLM bi-encoder + FAISS. Build phase 3.

    A bi-encoder embeds the question and every column separately, so columns
    are embedded once in advance and a query is a vector search rather than a
    model call per column. Metric: recall@10 of the gold tables.
    """

    name = "minilm-faiss"

    def __init__(self, index_path: str | None = None,
                 model_name: str = "sentence-transformers/all-MiniLM-L6-v2") -> None:
        self.index_path = index_path
        self.model_name = model_name
        self._model = None
        self._index = None

    def _load(self) -> None:
        if self._model is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer  # noqa: PLC0415
        except ImportError as exc:
            raise NotImplementedError(
                "EmbeddingRetriever needs sentence-transformers and faiss-cpu, "
                "which arrive in build phase 3. Until then the pipeline uses "
                "LexicalRetriever, which is the study's off-the-shelf baseline."
            ) from exc
        self._model = SentenceTransformer(self.model_name)

    def search(self, question: str, columns: list[Column],
               k: int = DEFAULT_K) -> list[Scored]:
        self._load()
        raise NotImplementedError("FAISS index is built in phase 3")


# ------------------------------------------------------------ formatting ----

def to_schema_text(scored: list[Scored], *, public_only: bool = False) -> str:
    """Render the retrieved subset for the generator's prompt.

    CREATE TABLE form, because that is what the model saw during
    pre-training -- it reads a schema far better as DDL than as prose.
    """
    blocks: list[str] = []
    for item in scored:
        if not item.above_cutoff:
            continue
        cols = [c for c in item.columns if (c.is_public or not public_only)]
        if not cols:
            continue
        lines = [f"CREATE TABLE {item.table} ("]
        for column in cols:
            comment = f"  -- {column.description}" if column.description else ""
            lines.append(f"    {column.column_name} {column.data_type},{comment}")
        lines.append(");")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def permitted_from(scored: list[Scored]) -> frozenset[str]:
    """The tables actually sent to the generator.

    Not the permission set -- rbac.permitted_tables produces that, and the
    validator checks against it. This is only what the prompt contained.
    """
    return frozenset(s.table.lower() for s in scored if s.above_cutoff)
