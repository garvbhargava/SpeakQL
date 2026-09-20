"""The edit log — one row per accepted change (Backend Plan §10, §20).

**Exactly one writer, and it is this module.**

Append-only, and that is a design decision rather than an implementation
detail: an undo writes a *second* row reversing the first, never a deletion.
A trail that can be edited is not a trail, and "who changed this number and
what was it before" is the question this table exists to answer.

Every row is written **inside the same transaction as the change itself**.
A change committed without its log row, or a log row for a change that rolled
back, are both worse than either failing outright.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.entities import EditLog

log = logging.getLogger("speakql.edit_log")


@dataclass
class Change:
    person_id: int
    connection_id: int
    schema_name: str
    table_name: str
    pk_column: str
    pk_value: str
    column_name: str
    before_value: str | None
    after_value: str | None
    note: str | None = None
    via_merge_id: int | None = None


def write(session: Session, change: Change) -> EditLog:
    """Record an accepted change.

    Called inside the write transaction, before commit -- never after. If the
    caller's transaction rolls back, this row goes with it, which is correct.
    """
    row = EditLog(
        person_id=change.person_id,
        connection_id=change.connection_id,
        schema_name=change.schema_name,
        table_name=change.table_name,
        pk_column=change.pk_column,
        pk_value=str(change.pk_value),
        column_name=change.column_name,
        before_value=_as_text(change.before_value),
        after_value=_as_text(change.after_value),
        note=change.note,
        via_merge_id=change.via_merge_id,
    )
    session.add(row)
    session.flush()
    return row


def write_reversal(session: Session, original: EditLog, *, person_id: int,
                   note: str = "reverted") -> EditLog:
    """Undo, as a second row rather than a deletion.

    The trail is append-only. Both the change and its reversal stay visible,
    which is what lets somebody later see that a value moved and came back
    rather than never having moved.
    """
    return write(session, Change(
        person_id=person_id,
        connection_id=original.connection_id,
        schema_name=original.schema_name,
        table_name=original.table_name,
        pk_column=original.pk_column,
        pk_value=original.pk_value,
        column_name=original.column_name,
        before_value=original.after_value,   # deliberately swapped
        after_value=original.before_value,
        note=note,
    ))


def history_for_cell(session: Session, connection_id: int, schema_name: str,
                     table_name: str, pk_value: str,
                     column_name: str) -> list[EditLog]:
    """Every change to one cell, oldest first."""
    return list(session.scalars(
        select(EditLog)
        .where(
            EditLog.connection_id == connection_id,
            EditLog.schema_name == schema_name,
            EditLog.table_name == table_name,
            EditLog.pk_value == str(pk_value),
            EditLog.column_name == column_name,
        )
        .order_by(EditLog.created_at.asc())
    ))


def _as_text(value) -> str | None:
    if value is None:
        return None
    return str(value)
