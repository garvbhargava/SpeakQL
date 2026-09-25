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
from collections.abc import Iterable
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
    # "schema.table.column" this column points at, for a single-column foreign
    # key. The join path, taken from the catalogue instead of guessed.
    references_to: str | None = None
    is_nullable: bool = True

    @property
    def qualified_table(self) -> str:
        return f"{self.schema_name}.{self.table_name}"

    @property
    def references_table(self) -> str | None:
        if not self.references_to:
            return None
        return self.references_to.rsplit(".", 1)[0]


@dataclass
class Scored:
    table: str
    score: float
    columns: list[Column]
    # True when retrieval did not choose this table: a join has to pass
    # through it to connect two tables that retrieval did choose.
    bridge: bool = False

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

def table_document(table: str, columns: Iterable[str],
                   description: str | None = None) -> str:
    """What a table looks like to Model A.

    **This is a contract with ml/**: the index is built from this text and the
    model was trained on it. Change it and both have to be rebuilt, or
    retrieval quietly gets worse with nothing to show why.
    """
    text = f"table {table} with columns {', '.join(columns)}"
    if description:
        text += f". {description}"
    return text


class EmbeddingRetriever(Retriever):
    """MiniLM bi-encoder + FAISS (Model A).

    A bi-encoder embeds the question and each table separately, so tables are
    embedded once and a question is one embedding plus a vector search -- not
    a model call per table. The index is per schema and cached: the same
    connection asks the same six tables a thousand times.

    Falls back to nothing: if the checkpoint or the libraries are missing it
    raises NotImplementedError, and the pipeline uses LexicalRetriever, which
    is the study's off-the-shelf baseline rather than a placeholder.
    """

    name = "minilm-faiss"

    # Everything more than this far below the best match is not sent to the
    # generator. On a six-table warehouse nothing is pruned; on a registered
    # database with two hundred tables, almost everything is.
    MARGIN = 0.22

    def __init__(self, checkpoint: str | None = None,
                 model_name: str = "sentence-transformers/all-MiniLM-L6-v2") -> None:
        self.checkpoint = checkpoint
        self.model_name = model_name
        self._model = None
        self._cache: dict[tuple, tuple[list[str], object]] = {}

    def available(self) -> bool:
        try:
            self._load()
            return True
        except NotImplementedError:
            return False

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer  # noqa: PLC0415
        except ImportError as exc:
            raise NotImplementedError(
                "EmbeddingRetriever needs sentence-transformers and faiss-cpu. "
                "Until they are installed the pipeline uses LexicalRetriever, "
                "which is the study's off-the-shelf baseline."
            ) from exc

        source = self.checkpoint or self.model_name
        try:
            self._model = SentenceTransformer(source)
        except Exception as exc:  # noqa: BLE001 - a missing checkpoint is normal
            raise NotImplementedError(
                f"no retriever checkpoint at {source}: {exc}"
            ) from exc
        log.info("retriever loaded from %s", source)
        return self._model

    def _index_for(self, by_table: dict[str, list[Column]]):
        """Embed each table once, and keep it. Keyed by the schema itself, so
        a reindex that changes a column invalidates the entry."""
        key = tuple(sorted(
            (table, tuple(c.column_name for c in columns))
            for table, columns in by_table.items()
        ))
        if key in self._cache:
            return self._cache[key]

        model = self._load()
        names = sorted(by_table)
        documents = [
            table_document(
                name.split(".")[-1],
                [c.column_name for c in by_table[name]],
                next((c.description for c in by_table[name] if c.description), None),
            )
            for name in names
        ]
        matrix = model.encode(documents, normalize_embeddings=True,
                              show_progress_bar=False)

        index = None
        try:
            import faiss  # noqa: PLC0415
            index = faiss.IndexFlatIP(matrix.shape[1])
            index.add(matrix)
        except ImportError:
            # Cosine against a few hundred rows is a matrix multiply; FAISS is
            # what keeps it flat when a registered database has thousands.
            log.info("faiss not installed; scoring with numpy instead")

        self._cache[key] = (names, index if index is not None else matrix)
        return self._cache[key]

    def search(self, question: str, columns: list[Column],
               k: int = DEFAULT_K) -> list[Scored]:
        model = self._load()

        by_table: dict[str, list[Column]] = {}
        for column in columns:
            by_table.setdefault(column.qualified_table, []).append(column)
        if not by_table:
            return []

        names, index = self._index_for(by_table)
        query = model.encode([question], normalize_embeddings=True,
                             show_progress_bar=False)

        if hasattr(index, "search"):                    # faiss
            similarity, positions = index.search(query, min(k, len(names)))
            ranked = [(names[p], float(s))
                      for s, p in zip(similarity[0], positions[0]) if p >= 0]
        else:                                           # numpy fallback
            scores = (index @ query[0])
            order = scores.argsort()[::-1][:k]
            ranked = [(names[p], float(scores[p])) for p in order]

        if not ranked:
            return []

        best = ranked[0][1]
        out: list[Scored] = []
        for table, similarity in ranked:
            # Cosine is -1..1; the interface and the cutoff are 0..1.
            score = round(max(0.0, (similarity + 1) / 2), 3)
            if similarity < best - self.MARGIN:
                score = 0.0                              # below CUTOFF: not sent
            out.append(Scored(table, score, by_table[table]))
        return out


# ---------------------------------------------------------- join paths ----

def _fk_graph(columns: list[Column]) -> dict[str, set[str]]:
    """Which tables are joinable to which, from the foreign keys alone."""
    graph: dict[str, set[str]] = {}
    for column in columns:
        target = column.references_table
        if not target:
            continue
        graph.setdefault(column.qualified_table, set()).add(target)
        graph.setdefault(target, set()).add(column.qualified_table)
    return graph


def _path(graph: dict[str, set[str]], start: str, goal: str,
          limit: int = 4) -> list[str]:
    """Shortest join path between two tables, breadth first. [] if none."""
    if start == goal:
        return [start]
    seen = {start}
    queue: list[list[str]] = [[start]]
    while queue:
        path = queue.pop(0)
        if len(path) > limit:
            return []
        for neighbour in sorted(graph.get(path[-1], ())):
            if neighbour == goal:
                return path + [neighbour]
            if neighbour not in seen:
                seen.add(neighbour)
                queue.append(path + [neighbour])
    return []


def complete_join_paths(scored: list[Scored], columns: list[Column], *,
                        limit: int = 3) -> list[Scored]:
    """Add the tables a join has to pass through.

    Retrieval scores each table against the question, so it picks `orders` and
    `regions` for "sales by region" and misses `customers`, which the question
    never mentions and which is the only way to get from one to the other. A
    generator handed those two tables and nothing else invents a join key --
    `orders.region_id`, which does not exist. Every model did it; the fix is
    not a better model, it is giving it the path.
    """
    chosen = [s for s in scored if s.above_cutoff]
    if len(chosen) < 2:
        return scored

    graph = _fk_graph(columns)
    by_table: dict[str, list[Column]] = {}
    for column in columns:
        by_table.setdefault(column.qualified_table, []).append(column)

    present = {s.table for s in chosen}
    added: list[Scored] = []
    anchor = chosen[0].table

    for other in (s.table for s in chosen[1:]):
        for step in _path(graph, anchor, other):
            if step in present or len(added) >= limit:
                continue
            if step not in by_table:
                continue
            present.add(step)
            added.append(Scored(step, CUTOFF, by_table[step], bridge=True))

    return scored + added


# ------------------------------------------------------------ formatting ----

def _visible(item: Scored, public_only: bool) -> list[Column]:
    return [c for c in item.columns if (c.is_public or not public_only)]


def to_schema_text(scored: list[Scored], *, public_only: bool = False) -> str:
    """Render the retrieved subset for the generator's prompt.

    CREATE TABLE form, because that is what the model saw during
    pre-training -- it reads a schema far better as DDL than as prose. Foreign
    keys are included: they are the join path, and a model that has to guess
    one guesses wrong.
    """
    blocks: list[str] = []
    for item in scored:
        if not item.above_cutoff:
            continue
        cols = _visible(item, public_only)
        if not cols:
            continue
        header = f"CREATE TABLE {item.table} ("
        if item.bridge:
            header += "   -- joins the tables above"
        lines = [header]
        for column in cols:
            reference = (f" REFERENCES {column.references_to.rsplit('.', 1)[0]} "
                         f"({column.references_to.rsplit('.', 1)[1]})"
                         if column.references_to else "")
            # NULL is stated, not left out: "no units recorded" is IS NULL,
            # and a model that cannot see which columns are nullable writes
            # units = 0 instead.
            null = " NULL" if column.is_nullable else " NOT NULL"
            comment = f"  -- {column.description}" if column.description else ""
            lines.append(
                f"    {column.column_name} {column.data_type}{null}{reference},{comment}"
            )
        lines.append(");")
        blocks.append("\n".join(lines))

    if not blocks:
        return ""

    # The joins spelled out, as equalities. The same facts are in the
    # REFERENCES clauses above, but a 4B model reads the list and guesses at
    # the clauses -- it wrote SUM(t2.orders.amount) against a schema that
    # carried both tables until this was added.
    included = {s.table for s in scored if s.above_cutoff}
    joins = sorted({
        f"{c.qualified_table}.{c.column_name} = {c.references_to}"
        for s in scored if s.above_cutoff
        for c in _visible(s, public_only)
        if c.references_to and c.references_table in included
    })
    if joins:
        blocks.append("-- join path:\n" + "\n".join(f"--   {j}" for j in joins))

    return "\n\n".join(blocks)


def to_compact_schema(scored: list[Scored], *, public_only: bool = False) -> str:
    """One line per table, for CodeT5.

    A 60M-parameter model pays for every token, and its input window is 512:
    DDL spends most of it on punctuation. **This function is the contract
    between training and inference** -- ml/ serialises the training pairs with
    it, so a change here without retraining changes what the model is asked
    and quietly costs accuracy.
    """
    parts: list[str] = []
    for item in scored:
        if not item.above_cutoff:
            continue
        cols = _visible(item, public_only)
        if not cols:
            continue
        rendered = []
        for column in cols:
            mark = "?" if column.is_nullable else ""
            if column.references_to:
                target = column.references_to.split(".")
                rendered.append(
                    f"{column.column_name}{mark} -> {target[-2]}.{target[-1]}")
            else:
                rendered.append(f"{column.column_name}{mark}")
        name = item.table.split(".")[-1] if item.table.startswith("public.") else item.table
        parts.append(f"{name} : {' , '.join(rendered)}")
    return " | ".join(parts)


@dataclass(frozen=True)
class SchemaContext:
    """The retrieved schema, in both forms a generator might want.

    Built once per question. Gemma reads DDL; CodeT5 was trained on the
    compact line. Giving each the form it was built for is worth more than
    picking one and making the other cope.
    """

    ddl: str
    compact: str
    tables: tuple[str, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.ddl)


def context_for(scored: list[Scored], *, public_only: bool = False) -> SchemaContext:
    return SchemaContext(
        ddl=to_schema_text(scored, public_only=public_only),
        compact=to_compact_schema(scored, public_only=public_only),
        tables=tuple(s.table for s in scored if s.above_cutoff),
    )


def permitted_from(scored: list[Scored]) -> frozenset[str]:
    """The tables actually sent to the generator.

    Not the permission set -- rbac.permitted_tables produces that, and the
    validator checks against it. This is only what the prompt contained.
    """
    return frozenset(s.table.lower() for s in scored if s.above_cutoff)
