"""Step 9: run it (Backend Plan §10.4, §13.1, §15.1).

Two rules govern this file.

**It receives an engine, never a DSN, and cannot construct one.** There is no
import of `Settings` here and no call to `create_engine`. Whatever privilege
the caller handed over is the ceiling, and this module cannot raise it.

**Resolution happens at execution, not generation.** `sql_generator.py` always
produces SQL against `public`. For exactly those tables where the calling
member has a pending correction, this module substitutes a derived table that
`COALESCE`s their pending cells over the real ones -- so their own answers
include their own pending values, and nobody else's answers change at all.
It happens here, after validation and under the read-only role, which is why
the overlay needs no view and no privilege on `public` (see db/edits_engine.py).

The second check of the organisation identity also lives here, immediately
before the query runs (§6). One check is one point of failure.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from core.validator import Permitted, validate

log = logging.getLogger("speakql.executor")

DIALECT = "postgres"


class ExecutionError(RuntimeError):
    """A failure, not a refusal. The interface shows a retry strip and no seal
    band, because there is no answer to seal (§13)."""


class StatementTimeout(ExecutionError):
    pass


@dataclass
class Result:
    columns: list[str]
    rows: list[tuple]
    row_count: int
    elapsed_ms: int
    truncated: bool = False
    overlay_tables: tuple[str, ...] = ()
    notes: list[str] = field(default_factory=list)

    def as_dicts(self) -> list[dict]:
        return [dict(zip(self.columns, row)) for row in self.rows]


@dataclass(frozen=True)
class OverlaySpec:
    """Everything needed to lay one member's pending cells over one table.

    `columns` is every column of the real table in order, with its registry
    type. `corrected` is the subset that has at least one pending cell. All of
    it comes from the schema registry and the merge queue -- none of it from
    the request.
    """

    overlay_table: str                     # quoted, e.g. "member_edits"."p42_shipments"
    pk_column: str
    columns: tuple[tuple[str, str], ...]   # (name, data_type)
    corrected: frozenset[str]


def _overlay_select(table: str, spec: OverlaySpec) -> str:
    from db.edits_engine import _ident, safe_type  # noqa: PLC0415

    key = _ident(spec.pk_column)
    projected: list[str] = []
    joins: list[str] = []

    for name, data_type in spec.columns:
        column = _ident(name)
        if column in spec.corrected and column != key:
            alias = f"o_{column}"
            projected.append(
                f'COALESCE(CAST(NULLIF({alias}.after_value, \'\') AS '
                f'{safe_type(data_type)}), s."{column}") AS "{column}"'
            )
            joins.append(
                f"LEFT JOIN {spec.overlay_table} AS {alias} "
                f'ON {alias}.pk_value = CAST(s."{key}" AS TEXT) '
                f"AND {alias}.column_name = '{column}'"
            )
        else:
            projected.append(f's."{column}"')

    return (
        f"SELECT {', '.join(projected)} "
        f'FROM "public"."{_ident(table)}" AS s ' + " ".join(joins)
    )


def rewrite_for_overlay(
    sql: str, overlays: dict[str, OverlaySpec]
) -> tuple[str, tuple[str, ...]]:
    """Substitute the member's overlay for the tables they have corrected.

    Each reference to `public.<table>` becomes a derived table with the same
    alias, so every column reference in the original query still resolves --
    only the values of the corrected cells differ. Returns the rewritten SQL
    and which tables were overlaid, so the answer can say "includes your
    pending value" rather than changing silently.
    """
    if not overlays:
        return sql, ()

    try:
        tree = sqlglot.parse_one(sql, read=DIALECT)
    except Exception:
        return sql, ()

    targets = []
    for table in tree.find_all(exp.Table):
        name = (table.name or "").lower()
        schema = (table.text("db") or "public").lower()
        if schema == "public" and name in overlays:
            targets.append(table)

    overlaid: set[str] = set()
    # Replace after collecting: mutating a tree while walking it skips nodes.
    for table in targets:
        name = table.name.lower()
        alias = table.alias or table.name
        derived = sqlglot.parse_one(_overlay_select(name, overlays[name]),
                                    read=DIALECT)
        table.replace(exp.Subquery(
            this=derived,
            alias=exp.TableAlias(this=exp.to_identifier(alias)),
        ))
        overlaid.add(name)

    return tree.sql(dialect=DIALECT), tuple(sorted(overlaid))


def execute(
    engine: Engine,
    sql: str,
    permitted: Permitted,
    *,
    max_rows: int,
    expected_database: str | None = None,
    overlays: dict[str, OverlaySpec] | None = None,
) -> Result:
    """Run a validated SELECT under a read-only engine.

    `permitted` is passed again deliberately. The statement was validated
    before it got here; it is validated once more immediately before execution
    so that nothing which happened in between -- a rewrite, a retry, a bug --
    can put an unchecked statement in front of the database.

    `expected_database` is the database the question was about. Before the
    query runs, the server is asked which database this connection is actually
    on, and a mismatch refuses. It is the check that would have caught the
    first version of this backend, which ran every question on one warehouse.
    """
    recheck = validate(sql, permitted)
    if not recheck.allowed:
        # If this ever fires, something above the executor rewrote a statement
        # after it was approved. That is a bug worth a loud log line.
        log.error("post-validation refusal, statement was altered: %s", recheck.reason)
        raise ExecutionError(f"refused at execution: {recheck.reason}")

    # The overlay is applied AFTER validation, by this module, and only ever
    # the caller's own -- so a statement naming member_edits directly is
    # refused above, while the executor's own substitution is not a statement
    # anybody supplied.
    final_sql = sql
    used_overlay: tuple[str, ...] = ()
    if overlays:
        final_sql, used_overlay = rewrite_for_overlay(sql, overlays)

    started = time.monotonic()
    try:
        with engine.connect() as conn:
            if expected_database and engine.dialect.name == "postgresql":
                actual = conn.execute(text("SELECT current_database()")).scalar_one()
                if actual != expected_database:
                    log.error(
                        "database identity mismatch: expected %s, connected to %s",
                        expected_database, actual,
                    )
                    raise ExecutionError(
                        "refused: this query was routed to the wrong database"
                    )
            cursor = conn.execute(text(final_sql))
            columns = list(cursor.keys())
            # fetchmany(max_rows + 1): the extra row is how we know the result
            # was truncated without counting the whole table.
            fetched = cursor.fetchmany(max_rows + 1)
    except DBAPIError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        message = str(getattr(exc, "orig", exc))
        if "statement timeout" in message.lower() or "canceling statement" in message.lower():
            raise StatementTimeout(
                f"the query was stopped after {elapsed} ms"
            ) from exc
        raise ExecutionError(_readable(message)) from exc
    except SQLAlchemyError as exc:
        raise ExecutionError(_readable(str(exc))) from exc

    elapsed = int((time.monotonic() - started) * 1000)
    truncated = len(fetched) > max_rows
    rows = [tuple(r) for r in fetched[:max_rows]]

    notes: list[str] = list(recheck.notes)
    if truncated:
        notes.append(f"showing the first {max_rows:,} rows")
    if used_overlay:
        notes.append(
            "includes your pending correction to " + ", ".join(used_overlay)
        )

    return Result(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        elapsed_ms=elapsed,
        truncated=truncated,
        overlay_tables=used_overlay,
        notes=notes,
    )


def _readable(message: str) -> str:
    """Database errors leak schema detail and stack context. Return something
    a person can act on and log the rest."""
    first = message.strip().splitlines()[0]
    lowered = first.lower()
    if "does not exist" in lowered:
        return "the query names something that is not in this database"
    if "permission denied" in lowered:
        # Should be unreachable -- the validator refuses first -- so if a user
        # ever sees this, a grant and the registry disagree.
        return "the database refused that read"
    if "division by zero" in lowered:
        return "the query divides by zero"
    if "syntax error" in lowered:
        return "the generated query was not valid SQL"
    return "the query could not be completed"


def assert_same_organisation(session_org_id: int, connection_org_id: int) -> None:
    """The second check of §6's invariant, run immediately before execution.

    The organisation comes from the session, never from a request body. This
    asserts that whatever chain of calls reached the executor did not swap it
    on the way.
    """
    if session_org_id != connection_org_id:
        log.error(
            "organisation mismatch at execution: session=%s connection=%s",
            session_org_id, connection_org_id,
        )
        raise ExecutionError("this database does not belong to your organisation")
