"""Member overlays and their read-back views (Backend Plan §10.4).

Revision 4 promised that a member's pending value "appears in their own
answers" and never said how. This is how.

For each member with pending corrections on a table, one view:

    CREATE VIEW member_edits.p42_shipments AS
    SELECT s.shipment_id,
           COALESCE(e.units::integer, s.units) AS units,
           ...
    FROM public.shipments s
    LEFT JOIN member_edits.p42_shipments_rows e
           ON e.pk_value = s.shipment_id::text

The executor points that member's reads at the view (see
`core/executor.rewrite_for_overlay`), so *their* answers include *their*
pending values and nobody else's answers change at all.

The privilege shape is the important part:

    speakql_edits_rw   owns the overlay tables. Holds NOTHING on public.
    speakql_ro         SELECT on the VIEWS only -- never on the tables beneath.

A read privilege on a read-only object, granted in the opposite direction to
the assertion that protects the warehouse. Both are asserted in
tests/test_privileges.py.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import text
from sqlalchemy.engine import Engine

log = logging.getLogger("speakql.overlay")

OVERLAY_SCHEMA = "member_edits"

_SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _ident(name: str) -> str:
    if not _SAFE.match(name or ""):
        raise ValueError(f"unsafe identifier: {name!r}")
    return name


def overlay_schema_for(person_id: int) -> str:
    """One schema per member. Named from an integer we control, never from
    anything a user supplied."""
    return f"{OVERLAY_SCHEMA}_p{int(person_id)}"


def ensure_schema(engine: Engine, person_id: int) -> str:
    schema = overlay_schema_for(person_id)
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
    return schema


def ensure_overlay_table(engine: Engine, person_id: int, table_name: str) -> str:
    """The thin table holding only this member's pending cells.

    Not a copy of the warehouse -- one row per corrected cell. A member with
    three pending corrections has three rows here, whatever the size of the
    table underneath.
    """
    schema = overlay_schema_for(person_id)
    table = _ident(table_name)
    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"'))
        conn.execute(text(f'''
            CREATE TABLE IF NOT EXISTS "{schema}"."{table}_rows" (
                pk_value    TEXT NOT NULL,
                column_name TEXT NOT NULL,
                after_value TEXT,
                PRIMARY KEY (pk_value, column_name)
            )
        '''))
    return f'"{schema}"."{table}_rows"'


def rebuild_view(
    engine: Engine,
    person_id: int,
    *,
    schema_name: str,
    table_name: str,
    pk_column: str,
    columns: dict[str, str],
    corrected_columns: set[str],
    ro_role: str = "speakql_ro",
) -> str:
    """Create or replace the COALESCE view for one table.

    `columns` maps column name -> data type, and the cast matters: the overlay
    stores every value as text, so `COALESCE(e.after_value, s.units)` would be
    a type error. The cast is built from the registry's declared type, never
    from anything a user typed.
    """
    overlay = overlay_schema_for(person_id)
    source_schema = _ident(schema_name)
    table = _ident(table_name)
    key = _ident(pk_column)

    projected: list[str] = []
    for column, data_type in columns.items():
        safe_column = _ident(column)
        if safe_column in corrected_columns and safe_column != key:
            cast = _safe_type(data_type)
            projected.append(
                f'COALESCE(NULLIF(e_{safe_column}.after_value, \'\')::{cast}, '
                f's."{safe_column}") AS "{safe_column}"'
            )
        else:
            projected.append(f's."{safe_column}"')

    joins: list[str] = []
    for column in sorted(corrected_columns):
        safe_column = _ident(column)
        if safe_column == key:
            continue
        joins.append(
            f'LEFT JOIN "{overlay}"."{table}_rows" e_{safe_column} '
            f'ON e_{safe_column}.pk_value = s."{key}"::text '
            f"AND e_{safe_column}.column_name = '{safe_column}'"
        )

    statement = (
        f'CREATE OR REPLACE VIEW "{overlay}"."{table}" AS\n'
        f'SELECT {", ".join(projected)}\n'
        f'FROM "{source_schema}"."{table}" s\n'
        + "\n".join(joins)
    )

    with engine.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{overlay}"'))
        conn.execute(text(statement))
        # The read role gets SELECT on the VIEW and nothing on the table
        # underneath it. This is the grant §5.1 added in Revision 5.
        conn.execute(text(f'GRANT USAGE ON SCHEMA "{overlay}" TO {_ident(ro_role)}'))
        conn.execute(text(
            f'GRANT SELECT ON "{overlay}"."{table}" TO {_ident(ro_role)}'
        ))

    log.info("rebuilt overlay view %s.%s for person %s", overlay, table, person_id)
    return f"{overlay}.{table}"


def drop_view(engine: Engine, person_id: int, table_name: str) -> None:
    """Called when a member's last pending correction on a table is merged or
    rejected. A view that COALESCEs nothing is just a slower table."""
    overlay = overlay_schema_for(person_id)
    table = _ident(table_name)
    with engine.begin() as conn:
        conn.execute(text(f'DROP VIEW IF EXISTS "{overlay}"."{table}"'))


_ALLOWED_CASTS = {
    "integer", "bigint", "smallint", "numeric", "real", "double precision",
    "text", "character varying", "boolean", "date", "timestamp",
    "timestamp with time zone", "timestamp without time zone",
}


def _safe_type(data_type: str) -> str:
    """Only a known type may be interpolated into the view definition.

    This string is not parameterisable -- it is part of the SQL grammar, not a
    value -- so it is whitelisted instead. The type comes from the schema
    registry, which came from the catalogue, but a second check costs nothing
    and this is the one place a type reaches a statement as text.
    """
    base = (data_type or "").split("(")[0].strip().lower()
    if base not in _ALLOWED_CASTS:
        return "text"
    return base
