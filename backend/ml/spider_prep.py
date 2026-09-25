"""Spider 1.0, prepared for both models (Backend Plan §23, week 1).

Spider is 10,181 questions over 200 databases, written by 11 students, with
the databases split so that **no database in the development set appears in
training**. That split is the point: it measures whether a model generalises
to a schema it has never seen, which is exactly what SpeakQL has to do when an
owner registers their own warehouse.

    questions and gold SQL   huggingface: xlangai/spider   (CC BY-SA 4.0)
    schemas                  tables.json from the Spider repository

What this produces, one JSON object per line:

    db_id, question, sql (rewritten for Postgres), tables (the gold tables),
    schema (every table in that database, with columns and foreign keys)

Two things are done to the SQL, and both matter:

    Double-quoted values become single-quoted. Spider is SQLite, where
    "Boston" is a string; in Postgres it is an identifier, and training a
    model on it would teach it to write a column name where a value belongs.

    Identifiers are lowercased and the statement is re-printed by sqlglot, so
    every target in training has one shape. A model that spends capacity
    learning three ways to write the same query has less left for the query.

    python -m ml.spider_prep
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers

from ml.paths import SPIDER, write_jsonl

TABLES_URL = (
    "https://raw.githubusercontent.com/taoyds/spider/master/"
    "evaluation_examples/examples/tables.json"
)

_DOUBLE_QUOTED = re.compile(r'"([^"]*)"')


def fetch_tables() -> list[dict]:
    """The schema of every Spider database. Cached after the first run."""
    local = SPIDER / "tables.json"
    if not local.exists():
        print(f"downloading {TABLES_URL}")
        with urllib.request.urlopen(TABLES_URL, timeout=120) as response:
            local.write_bytes(response.read())
    return json.loads(local.read_text(encoding="utf-8"))


def schema_of(entry: dict) -> dict:
    """tables.json for one database -> {table: [columns], foreign_keys: [...]}.

    tables.json stores columns as (table_index, column_name) pairs and foreign
    keys as pairs of column indices, so both need resolving before they are
    useful to anything else.
    """
    tables = [name.lower() for name in entry["table_names_original"]]
    columns: dict[str, list[str]] = {name: [] for name in tables}
    owner: list[str | None] = []

    for table_index, column_name in entry["column_names_original"]:
        if table_index < 0:          # the synthetic "*" column
            owner.append(None)
            continue
        table = tables[table_index]
        columns[table].append(column_name.lower())
        owner.append(table)

    foreign_keys = []
    for source, target in entry.get("foreign_keys", []):
        source_table, target_table = owner[source], owner[target]
        if not source_table or not target_table:
            continue
        foreign_keys.append({
            "table": source_table,
            "column": entry["column_names_original"][source][1].lower(),
            "references_table": target_table,
            "references_column": entry["column_names_original"][target][1].lower(),
        })

    return {"tables": columns, "foreign_keys": foreign_keys}


def to_postgres(sql: str) -> str | None:
    """Spider's SQLite SQL, rewritten as one canonical Postgres statement."""
    sql = _DOUBLE_QUOTED.sub(lambda m: "'" + m.group(1).replace("'", "''") + "'", sql)
    try:
        tree = sqlglot.parse_one(sql, read="sqlite")
        tree = normalize_identifiers(tree, dialect="postgres")
        return tree.sql(dialect="postgres")
    except Exception:       # noqa: BLE001 - a handful of Spider rows do not parse
        return None


def tables_in(sql: str) -> list[str]:
    """The gold tables, from the SQL itself -- which is what the retriever is
    trained and measured against."""
    try:
        tree = sqlglot.parse_one(sql, read="postgres")
    except Exception:       # noqa: BLE001
        return []
    names = {t.name.lower() for t in tree.find_all(exp.Table) if t.name}
    return sorted(names)


def prepare() -> dict[str, int]:
    from datasets import load_dataset  # noqa: PLC0415 - heavy, and only here

    schemas = {entry["db_id"]: schema_of(entry) for entry in fetch_tables()}
    print(f"{len(schemas)} database schemas")

    dataset = load_dataset("xlangai/spider")
    counts: dict[str, int] = {}

    for split, name in (("train", "train"), ("validation", "dev")):
        rows = []
        skipped = 0
        for item in dataset[split]:
            schema = schemas.get(item["db_id"])
            sql = to_postgres(item["query"])
            if schema is None or sql is None:
                skipped += 1
                continue
            gold = [t for t in tables_in(sql) if t in schema["tables"]]
            if not gold:
                skipped += 1
                continue
            rows.append({
                "db_id": item["db_id"],
                "question": item["question"].strip(),
                "sql": sql,
                "tables": gold,
                "schema": schema,
            })
        counts[name] = write_jsonl(SPIDER / f"{name}.jsonl", rows)
        print(f"{name}: {counts[name]} rows, {skipped} skipped")

    return counts


if __name__ == "__main__":
    prepare()
    sys.exit(0)
