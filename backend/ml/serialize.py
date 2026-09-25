"""How a schema is written down for the models. Training and inference share it.

The single most expensive mistake available here is serialising the schema one
way during training and another way at request time. The model would still
answer, fluently, and be quietly worse at it -- with nothing in any log to say
why. So both sides call into `core.schema_retriever`, and this module is the
only bridge: it turns the shapes the training data comes in (Spider's
tables.json, the warehouse registry) into the shapes those functions take.
"""

from __future__ import annotations

import random

from core.schema_retriever import Column, Scored, SchemaContext, context_for
from core.sql_generator import model_input

MAX_TABLES = 6

# Re-exported so ml/ has one import for the input format, and core/ owns it.
to_input = model_input


def columns_for(tables: dict[str, list[str]], foreign_keys: list[dict],
                schema_name: str = "public") -> list[Column]:
    """{table: [column, ...]} plus foreign keys -> the registry's own shape."""
    references: dict[tuple[str, str], str] = {
        (fk["table"], fk["column"]):
            f"{schema_name}.{fk['references_table']}.{fk['references_column']}"
        for fk in foreign_keys
    }
    return [
        Column(
            schema_name=schema_name, table_name=table, column_name=column,
            data_type="text", description=None, is_public=True,
            references_to=references.get((table, column)),
            # Spider does not record nullability, and the compact form only
            # marks it -- claiming NOT NULL here would be inventing a fact.
            is_nullable=True,
        )
        for table, columns in tables.items()
        for column in columns
    ]


def scored_for(columns: list[Column], chosen: list[str],
               schema_name: str = "public") -> list[Scored]:
    by_table: dict[str, list[Column]] = {}
    for column in columns:
        by_table.setdefault(column.table_name, []).append(column)
    return [Scored(f"{schema_name}.{name}", 1.0, by_table[name])
            for name in chosen if name in by_table]


def context_object(columns: list[Column], chosen: list[str]) -> SchemaContext:
    """Both forms at once -- what the route hands a generator."""
    return context_for(scored_for(columns, chosen))


def choose_tables(columns: list[Column], gold: list[str], rng: random.Random,
                  *, max_tables: int = MAX_TABLES) -> list[str]:
    """The tables retrieval would have returned: gold plus a few distractors.

    Training on gold tables alone would teach the model that every table it is
    given belongs in the query -- and then retrieval, which returns a few
    extra by design, would be the thing that broke it.
    """
    available = sorted({c.table_name for c in columns})
    distractors = [t for t in available if t not in gold]
    rng.shuffle(distractors)
    chosen = list(gold) + distractors[: max(0, max_tables - len(gold))]
    rng.shuffle(chosen)
    return chosen


def training_context(columns: list[Column], gold: list[str],
                     rng: random.Random, *, max_tables: int = MAX_TABLES) -> str:
    """The compact serialisation CodeT5 is trained on."""
    chosen = choose_tables(columns, gold, rng, max_tables=max_tables)
    return context_object(columns, chosen).compact
