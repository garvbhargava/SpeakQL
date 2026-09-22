"""Read a warehouse's shape into schema_registry (Backend Plan §9.2).

Runs under the read-only role, like everything else on the read path. It asks
the catalogue what tables and columns exist and records one row per column --
which is what retrieval searches, what the validator checks against, and where
the explanation's column names come from.

**Reindex is the last stage of ingestion, not a separate chore.** Until
Revision 5 an uploaded table was not retrievable until somebody remembered to
run a command, which meant the demo had a step that looked like a bug.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import bindparam, delete, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from db.entities import EditableTable, SchemaColumn

log = logging.getLogger("speakql.introspect")

# Never introspected. They are not customer data and the validator refuses
# them anyway; recording them would only put their names in the retriever.
_SKIP_SCHEMAS = ("pg_catalog", "information_schema", "pg_toast", "member_edits")

_COLUMNS_SQL = """
SELECT c.table_schema, c.table_name, c.column_name, c.data_type
FROM information_schema.columns c
JOIN information_schema.tables t
  ON t.table_schema = c.table_schema AND t.table_name = c.table_name
WHERE t.table_type = 'BASE TABLE'
  AND c.table_schema NOT IN :skip
  {only}
ORDER BY c.table_schema, c.table_name, c.ordinal_position
"""

# The default editable list: every table with a SINGLE-column primary key.
# A composite key is excluded -- the edit path scopes by one column, and
# pretending otherwise would fail at the worst moment.
#
# Read from pg_catalog, NOT information_schema. The information_schema
# constraint views show a constraint only to a role that owns the table or
# holds some privilege on it OTHER than SELECT -- and this runs as speakql_ro,
# which holds SELECT and nothing else. The first version used them, and on a
# real bootstrap found "0 editable" tables in a warehouse where every table
# has a primary key: the correction workflow had nothing to correct.
_PRIMARY_KEYS_SQL = """
SELECT n.nspname AS table_schema, c.relname AS table_name,
       a.attname AS column_name
FROM pg_constraint con
JOIN pg_class c      ON c.oid = con.conrelid
JOIN pg_namespace n  ON n.oid = c.relnamespace
JOIN pg_attribute a  ON a.attrelid = c.oid AND a.attnum = con.conkey[1]
WHERE con.contype = 'p'
  AND cardinality(con.conkey) = 1
  AND c.relkind = 'r'
  AND n.nspname NOT IN :skip
  {only}
"""


def _statement(template: str, column: str, only_schemas):
    """Bind the schema lists as EXPANDING parameters.

    `IN :skip` with a plain tuple binds the tuple as one value, which psycopg 3
    will not expand into a list -- another bug the first version would have hit
    on its first real run.
    """
    only_clause = f"AND {column} IN :only" if only_schemas else ""
    stmt = text(template.format(only=only_clause))
    params = [bindparam("skip", expanding=True)]
    if only_schemas:
        params.append(bindparam("only", expanding=True))
    return stmt.bindparams(*params)


@dataclass
class IntrospectionResult:
    tables: int
    columns: int
    editable: int

    @property
    def summary(self) -> str:
        return (
            f"{self.tables} tables, {self.columns} columns, "
            f"{self.editable} editable"
        )


def introspect(meta: Session, engine: Engine, connection_id: int, *,
               only_schemas: tuple[str, ...] | None = None) -> IntrospectionResult:
    """Replace the registry for one connection, in one transaction.

    Replace rather than merge: a column that was dropped upstream must vanish
    from the registry, or the retriever will keep offering it to the generator
    and the generator will keep writing SQL that fails.

    `only_schemas` restricts what is read. It matters for uploaded datasets,
    which live in per-organisation schemas inside a database every
    organisation's uploads share: introspecting the whole database would put
    another organisation's table names into this organisation's registry.
    """
    params: dict = {"skip": list(_SKIP_SCHEMAS)}
    if only_schemas:
        params["only"] = list(only_schemas)

    with engine.connect() as conn:
        columns = conn.execute(
            _statement(_COLUMNS_SQL, "c.table_schema", only_schemas), params
        ).all()
        primary_keys = conn.execute(
            _statement(_PRIMARY_KEYS_SQL, "n.nspname", only_schemas), params
        ).all()

    meta.execute(delete(SchemaColumn).where(SchemaColumn.connection_id == connection_id))
    meta.execute(delete(EditableTable).where(EditableTable.connection_id == connection_id))

    tables: set[tuple[str, str]] = set()
    for schema_name, table_name, column_name, data_type in columns:
        tables.add((schema_name, table_name))
        meta.add(SchemaColumn(
            connection_id=connection_id,
            schema_name=schema_name,
            table_name=table_name,
            column_name=column_name,
            data_type=data_type,
            description=_describe(table_name, column_name),
            # Nothing is public until an owner says so. Defaulting to public
            # would mean a viewer could read a column the day it was added.
            is_public=False,
        ))

    editable = 0
    for schema_name, table_name, pk_column in primary_keys:
        meta.add(EditableTable(
            connection_id=connection_id,
            schema_name=schema_name,
            table_name=table_name,
            pk_column=pk_column,
        ))
        editable += 1

    meta.flush()
    result = IntrospectionResult(len(tables), len(columns), editable)
    log.info("introspected connection %s: %s", connection_id, result.summary)
    return result


def _describe(table_name: str, column_name: str) -> str | None:
    """A first-pass description, so retrieval has more than an identifier to
    match against. An owner can replace it, and a real comment on the column
    beats both -- but an empty description helps nobody."""
    words = column_name.replace("_", " ")
    if column_name.endswith("_id"):
        return f"identifier linking {table_name} to {column_name[:-3]}"
    if column_name.endswith(("_at", "_on", "_date")):
        return f"when the {table_name.rstrip('s')} {words.rsplit(' ', 1)[0]}"
    return f"{words} of a {table_name.rstrip('s')}"
