"""Member overlays: where a pending correction lives (Backend Plan §10.4).

Revision 4 promised that a member's pending value "appears in their own
answers" and never said how. This is the storage half; the read half is
`core/executor.rewrite_for_overlay`.

**One small table per member per corrected table**, inside the warehouse's
`member_edits` schema:

    member_edits.p42_shipments      (pk_value, column_name, after_value)

It holds only that member's pending cells -- three pending corrections, three
rows, whatever the size of the table underneath. It is not a copy of the
warehouse, which is what made the old staging mirror expensive.

**Why rows here and COALESCE in the executor, rather than a view.** The design
first called for a `COALESCE` view per member. A Postgres view reads its base
tables with its *owner's* privileges, and the only role that can create objects
here is `speakql_edits_rw` -- which, by the assertion that protects the
warehouse, holds **nothing** on `public`. A view it owned could not read the
table it overlays, and granting it SELECT on `public` would break the one
guarantee the overlay design exists to keep. So the overlay is applied at
query time, by the executor, under `speakql_ro` in a read-only transaction:

    speakql_edits_rw   owns and writes the overlay rows. Holds NOTHING on public.
    speakql_ro         SELECT on the overlay rows, read-only, like everything else.

A member can never reach another member's overlay: the executor applies only
the caller's own, after validation, and the validator refuses any statement
that names `member_edits` directly.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import text
from sqlalchemy.engine import Engine

log = logging.getLogger("speakql.overlay")

OVERLAY_SCHEMA = "member_edits"
READ_ROLE = "speakql_ro"

_SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _ident(name: str) -> str:
    if not _SAFE.match(name or ""):
        raise ValueError(f"unsafe identifier: {name!r}")
    return name


def overlay_table_name(person_id: int, table_name: str) -> str:
    """Named from an integer we control and a registry identifier -- never from
    anything a user typed."""
    return f"p{int(person_id)}_{_ident(table_name).lower()}"


def overlay_table_for(person_id: int, table_name: str) -> str:
    """Fully qualified, quoted, ready for a statement."""
    return f'"{OVERLAY_SCHEMA}"."{overlay_table_name(person_id, table_name)}"'


def sync_rows(
    engine: Engine,
    person_id: int,
    table_name: str,
    rows: list[tuple[str, str, str | None]],
) -> None:
    """Make the overlay table hold exactly these pending cells.

    `rows` is (pk_value, column_name, after_value), taken from the open merge
    requests in `speakql_meta` -- the source of truth. Replacing the contents
    rather than patching them means the overlay can never drift from what the
    merge queue says is pending: an approved, rejected or stale request simply
    stops appearing here on the next sync.
    """
    table = overlay_table_for(person_id, table_name)

    with engine.begin() as conn:
        conn.execute(text(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            " pk_value TEXT NOT NULL,"
            " column_name TEXT NOT NULL,"
            " after_value TEXT,"
            " PRIMARY KEY (pk_value, column_name))"
        ))
        # The table's owner may grant read on it. This is the only privilege
        # the read role gains from an overlay, and it is read-only.
        conn.execute(text(f"GRANT SELECT ON {table} TO {READ_ROLE}"))
        conn.execute(text(f"DELETE FROM {table}"))
        for pk_value, column_name, after_value in rows:
            conn.execute(
                text(f"INSERT INTO {table} (pk_value, column_name, after_value) "
                     "VALUES (:pk, :col, :val)"),
                {"pk": str(pk_value), "col": _ident(column_name), "val": after_value},
            )

    log.info("overlay %s now holds %d pending cell%s", table, len(rows),
             "" if len(rows) == 1 else "s")


_ALLOWED_CASTS = {
    "integer", "bigint", "smallint", "numeric", "real", "double precision",
    "text", "character varying", "boolean", "date", "timestamp",
    "timestamp with time zone", "timestamp without time zone",
}


def safe_type(data_type: str) -> str:
    """Only a known type may be interpolated into the rewritten statement.

    A type is part of the SQL grammar, not a value, so it cannot be bound as a
    parameter -- it is whitelisted instead. It comes from the schema registry,
    which came from the catalogue, but this is the one place a type reaches a
    statement as text, and a second check costs nothing.
    """
    base = (data_type or "").split("(")[0].strip().lower()
    return base if base in _ALLOWED_CASTS else "text"
