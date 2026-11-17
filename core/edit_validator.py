"""Layer 4: the edit validator (Backend Plan §10.2).

The other half of the safety surface. `validator.py` guards the read path;
this guards the only path in the system that writes.

Four rules, and a statement must satisfy all of them:

    1. the table is on the editable list
    2. the statement is scoped by primary key
    3. it touches exactly one row
    4. the caller holds owner, or an analyst grant on that database

Rule 2 is the one that carries the others. A change that cannot be pinned to
one row cannot be checked, cannot be logged usefully, and cannot be undone --
so a table without a primary key is never editable, and an UPDATE whose WHERE
clause is anything other than `pk = value` is refused before it is composed.

Note what is NOT here: the statement is never taken from a model. The caller
supplies a table, a key, a column and a value; this module composes the SQL
itself. There is no path by which generated text becomes a write.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import sqlglot
from sqlglot import exp

DIALECT = "postgres"


class EditRefusal(str, Enum):
    NOT_EDITABLE = "that table is not on the editable list"
    NO_PRIMARY_KEY = "that table has no primary key, so a change cannot be pinned to one row"
    WRONG_KEY_COLUMN = "the change is not scoped by the table's primary key"
    PK_NOT_EDITABLE = "a primary key cannot be edited"
    NO_GRANT = "an analyst grant is required to correct a value"
    VIEWER = "a viewer may not correct values"
    UNKNOWN_COLUMN = "that column is not in the schema registry"
    MULTIPLE_ROWS = "the change would touch more than one row"
    NO_ROWS = "no row matches that key"
    TYPE_MISMATCH = "the value does not fit the column's type"


@dataclass(frozen=True)
class EditRequest:
    connection_id: int
    schema_name: str
    table_name: str
    pk_column: str
    pk_value: str
    column_name: str
    after_value: str | None
    note: str | None = None

    @property
    def qualified(self) -> str:
        return f"{self.schema_name.lower()}.{self.table_name.lower()}"


@dataclass
class EditVerdict:
    allowed: bool
    refusal: EditRefusal | None = None
    detail: str = ""

    @property
    def reason(self) -> str:
        if self.allowed:
            return "accepted"
        base = self.refusal.value if self.refusal else "refused"
        return f"{base}{': ' + self.detail if self.detail else ''}"


@dataclass(frozen=True)
class EditContext:
    """Everything the decision needs, resolved before validation runs.

    `editable` maps 'schema.table' -> primary key column.
    `columns` maps 'schema.table' -> {column: data_type}.
    """

    editable: dict[str, str]
    columns: dict[str, dict[str, str]]
    is_owner: bool
    db_role: str | None  # analyst | viewer | None


_NUMERIC = {"integer", "bigint", "smallint", "numeric", "real", "double precision"}
_TEMPORAL = {"date", "timestamp", "timestamptz", "timestamp with time zone"}


def validate_edit(req: EditRequest, ctx: EditContext) -> EditVerdict:
    """Decide whether this correction may be written. Never raises."""

    # --- 4. who is asking ---------------------------------------------------
    # Checked first because it is the cheapest and the most absolute.
    if not ctx.is_owner:
        if ctx.db_role is None:
            return EditVerdict(False, EditRefusal.NO_GRANT)
        if ctx.db_role == "viewer":
            return EditVerdict(False, EditRefusal.VIEWER)
        if ctx.db_role != "analyst":
            return EditVerdict(False, EditRefusal.NO_GRANT, ctx.db_role)

    # --- 1. is the table editable ------------------------------------------
    pk = ctx.editable.get(req.qualified)
    if pk is None:
        return EditVerdict(False, EditRefusal.NOT_EDITABLE, req.qualified)
    if not pk:
        return EditVerdict(False, EditRefusal.NO_PRIMARY_KEY, req.qualified)

    # --- 2. scoped by THE primary key, not merely by something --------------
    if req.pk_column.lower() != pk.lower():
        return EditVerdict(
            False, EditRefusal.WRONG_KEY_COLUMN,
            f"scoped by {req.pk_column}, but the primary key is {pk}",
        )

    if req.column_name.lower() == pk.lower():
        # Editing the key would move the row the log says it changed.
        return EditVerdict(False, EditRefusal.PK_NOT_EDITABLE, pk)

    # --- the column must exist ---------------------------------------------
    table_cols = ctx.columns.get(req.qualified, {})
    declared = {c.lower(): t for c, t in table_cols.items()}
    if req.column_name.lower() not in declared:
        return EditVerdict(False, EditRefusal.UNKNOWN_COLUMN, req.column_name)

    # --- the value must fit -------------------------------------------------
    data_type = declared[req.column_name.lower()].lower()
    problem = _type_problem(req.after_value, data_type)
    if problem:
        return EditVerdict(False, EditRefusal.TYPE_MISMATCH, problem)

    return EditVerdict(True)


def _type_problem(value: str | None, data_type: str) -> str | None:
    """A cheap pre-flight check. The database is still the authority -- this
    turns a constraint violation into a readable message before it happens."""
    if value is None or value == "":
        return None  # clearing a cell to NULL is a legitimate correction
    base = data_type.split("(")[0].strip()
    if base in _NUMERIC:
        try:
            float(value)
        except ValueError:
            return f"{value!r} is not a number, and the column is {data_type}"
    if base in _TEMPORAL:
        import datetime as _dt
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                _dt.datetime.strptime(value, fmt)
                return None
            except ValueError:
                continue
        return f"{value!r} is not a date, and the column is {data_type}"
    if base == "boolean" and value.lower() not in {"true", "false", "t", "f", "1", "0"}:
        return f"{value!r} is not a boolean"
    return None


def compose_update(req: EditRequest) -> tuple[str, dict[str, object]]:
    """Build the one UPDATE this system is capable of issuing.

    Parameterised, single table, single column, scoped by primary key. The
    caller never supplies SQL and a model never sees this path, so injection
    has nowhere to enter -- the identifiers come from the schema registry and
    the values are bound.
    """
    schema = _safe_identifier(req.schema_name)
    table = _safe_identifier(req.table_name)
    column = _safe_identifier(req.column_name)
    key = _safe_identifier(req.pk_column)

    sql = (
        f'UPDATE "{schema}"."{table}" SET "{column}" = :new_value '
        f'WHERE "{key}" = :pk_value'
    )
    return sql, {"new_value": req.after_value, "pk_value": req.pk_value}


def assert_single_row(affected: int) -> EditVerdict:
    """Rule 3, checked against reality rather than intention.

    Called inside the transaction, before commit. More than one row means the
    key was not unique after all, and the whole transaction rolls back -- the
    difference between "we believed it was scoped" and "it was".
    """
    if affected == 0:
        return EditVerdict(False, EditRefusal.NO_ROWS)
    if affected > 1:
        return EditVerdict(False, EditRefusal.MULTIPLE_ROWS, f"{affected} rows")
    return EditVerdict(True)


_IDENT_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$")


def _safe_identifier(name: str) -> str:
    """Identifiers cannot be bound as parameters, so they are whitelisted.

    Every identifier reaching this module has already been matched against the
    schema registry, so this is the second check rather than the first -- but
    an identifier path with only one check is how injection happens.
    """
    if not name or not set(name) <= _IDENT_OK:
        raise ValueError(f"unsafe identifier: {name!r}")
    return name


def is_write_statement(sql: str) -> bool:
    """Used by tests and by the executor's assertion that the read path never
    receives one of these."""
    try:
        tree = sqlglot.parse_one(sql, read=DIALECT)
    except Exception:
        return False
    return isinstance(tree, (exp.Update, exp.Insert, exp.Delete, exp.Merge))
