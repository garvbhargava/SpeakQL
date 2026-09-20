"""Step 9: run it (Backend Plan §10.4, §13.1, §15.1).

Two rules govern this file.

**It receives an engine, never a DSN, and cannot construct one.** There is no
import of `Settings` here and no call to `create_engine`. Whatever privilege
the caller handed over is the ceiling, and this module cannot raise it.

**Resolution happens at execution, not generation.** `sql_generator.py` always
produces SQL against `public`. This module rewrites the schema qualifier to
the caller's overlay for exactly those tables where that member has a pending
correction -- so their own answers include their own pending values, and
nobody else's answers change at all.

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


def rewrite_for_overlay(sql: str, overlay_schema: str,
                        overlaid_tables: set[str]) -> tuple[str, tuple[str, ...]]:
    """Point reads at the member's overlay views, for those tables only.

    The overlay view is a `COALESCE` of the member's pending rows over the
    real table (§10.4). `speakql_ro` holds SELECT on the **views** and nothing
    on the overlay tables beneath them.

    Returns the rewritten SQL and which tables were redirected, so the answer
    can say "includes your pending value" rather than changing silently.
    """
    if not overlaid_tables:
        return sql, ()

    try:
        tree = sqlglot.parse_one(sql, read=DIALECT)
    except Exception:
        return sql, ()

    redirected: set[str] = set()
    for table in tree.find_all(exp.Table):
        name = (table.name or "").lower()
        schema = (table.text("db") or "public").lower()
        if schema != "public":
            continue
        if name in overlaid_tables:
            table.set("db", exp.to_identifier(overlay_schema))
            redirected.add(name)

    return tree.sql(dialect=DIALECT), tuple(sorted(redirected))


def execute(
    engine: Engine,
    sql: str,
    permitted: Permitted,
    *,
    max_rows: int,
    overlay_schema: str | None = None,
    overlaid_tables: set[str] | None = None,
) -> Result:
    """Run a validated SELECT under a read-only engine.

    `permitted` is passed again deliberately. The statement was validated
    before it got here; it is validated once more immediately before execution
    so that nothing which happened in between -- a rewrite, a retry, a bug --
    can put an unchecked statement in front of the database.
    """
    recheck = validate(sql, permitted)
    if not recheck.allowed:
        # If this ever fires, something above the executor rewrote a statement
        # after it was approved. That is a bug worth a loud log line.
        log.error("post-validation refusal, statement was altered: %s", recheck.reason)
        raise ExecutionError(f"refused at execution: {recheck.reason}")

    final_sql = sql
    used_overlay: tuple[str, ...] = ()
    if overlay_schema and overlaid_tables:
        final_sql, used_overlay = rewrite_for_overlay(
            sql, overlay_schema, overlaid_tables
        )

    started = time.monotonic()
    try:
        with engine.connect() as conn:
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
