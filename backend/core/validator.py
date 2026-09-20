"""Layer 2: the AST validator (Backend Plan §6, §8).

**This file and edit_validator.py are the only places safety rules live.**
A check anywhere else is a bug, not a second layer.

The rule the whole product rests on:

    Nothing that is not a single read-only SELECT over tables this person is
    allowed to read ever reaches the database.

It is decided by parsing, never by matching text. A blocklist of words is
defeated by `SELECT ... ; DROP`, by comments, by casing, by unicode
lookalikes, and by a hundred things nobody thought of. A parser is defeated by
none of them, because it does not look at the words -- it looks at what the
statement *is*.

The model is never consulted here. The same decision is reached whether the
SQL came from CodeT5, from Gemma, or from a person typing it by hand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import sqlglot
from sqlglot import exp

DIALECT = "postgres"


class Refusal(str, Enum):
    """Why a statement was refused. The interface names the rule that fired,
    so these strings are user-facing and must stay readable."""

    UNPARSEABLE = "the statement could not be parsed"
    NOT_SINGLE = "more than one statement was supplied"
    NOT_SELECT = "the statement is not a SELECT"
    WRITE_OPERATION = "the statement writes, and this path only reads"
    DDL = "the statement changes the schema"
    UNKNOWN_TABLE = "the statement names a table that is not in this organisation"
    FORBIDDEN_TABLE = "the statement names a table this person may not read"
    FORBIDDEN_COLUMN = "the statement names a column this person may not read"
    SYSTEM_CATALOGUE = "the statement reads a system catalogue"
    NO_TABLES = "the statement names no table at all"
    SUBQUERY_DEPTH = "the statement nests deeper than the limit"
    CARTESIAN = "the statement joins without a condition"
    FUNCTION_NOT_ALLOWED = "the statement calls a function that is not permitted"


# Statement types that write. sqlglot gives us the node type directly, so this
# is a structural check and not a word list.
_WRITE_NODES = (
    exp.Insert, exp.Update, exp.Delete, exp.Merge,
)
_DDL_NODES = (
    exp.Create, exp.Drop, exp.Alter, exp.TruncateTable,
)

# Schemas that are never readable, whatever the grant says.
_SYSTEM_SCHEMAS = {"pg_catalog", "information_schema", "pg_toast"}

# Functions that read the filesystem, run commands, or leak the server's
# identity. Aggregates and ordinary scalar functions are allowed; these are
# the ones with side effects or privilege implications.
_FORBIDDEN_FUNCTIONS = {
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "lo_import", "lo_export", "dblink", "dblink_exec", "pg_sleep",
    "pg_terminate_backend", "pg_cancel_backend", "pg_reload_conf",
    "current_setting", "set_config", "query_to_xml", "pg_logdir_ls",
}

MAX_SUBQUERY_DEPTH = 5


@dataclass
class Verdict:
    """The outcome. `allowed` is the only thing callers should branch on."""

    allowed: bool
    refusal: Refusal | None = None
    detail: str = ""
    tables: tuple[str, ...] = ()
    normalised_sql: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        if self.allowed:
            return "accepted"
        base = self.refusal.value if self.refusal else "refused"
        return f"{base}{': ' + self.detail if self.detail else ''}"


@dataclass(frozen=True)
class Permitted:
    """What this caller may read, resolved before validation ever runs.

    `tables` is fully qualified, lowercased `schema.table`.
    `public_only` is True for a viewer grant: they may read only the columns
    marked is_public in the schema registry.
    """

    tables: frozenset[str]
    public_columns: frozenset[str] = frozenset()
    public_only: bool = False


def _qualified(table: exp.Table) -> str:
    schema = (table.text("db") or "public").lower()
    name = (table.name or "").lower()
    return f"{schema}.{name}"


def validate(sql: str, permitted: Permitted) -> Verdict:
    """Decide whether this statement may run. Never raises."""
    if not sql or not sql.strip():
        return Verdict(False, Refusal.UNPARSEABLE, "the statement is empty")

    # --- one statement, and one only ---------------------------------------
    # Parsing the whole string is what catches `SELECT 1; DROP TABLE x`.
    # A validator that parsed only the first statement would pass it.
    try:
        statements = sqlglot.parse(sql, read=DIALECT)
    except Exception as exc:  # sqlglot raises several types
        return Verdict(False, Refusal.UNPARSEABLE, str(exc).splitlines()[0][:200])

    statements = [s for s in statements if s is not None]
    if not statements:
        return Verdict(False, Refusal.UNPARSEABLE, "nothing to run")
    if len(statements) > 1:
        return Verdict(
            False, Refusal.NOT_SINGLE,
            f"{len(statements)} statements were supplied; only one may run",
        )

    tree = statements[0]

    # --- what KIND of statement is this ------------------------------------
    if isinstance(tree, _DDL_NODES):
        return Verdict(False, Refusal.DDL, type(tree).__name__.upper())
    if isinstance(tree, _WRITE_NODES):
        return Verdict(False, Refusal.WRITE_OPERATION, type(tree).__name__.upper())

    # A CTE wrapping a write (`WITH x AS (DELETE ... RETURNING *) SELECT ...`)
    # is a SELECT at the top and a write underneath. Look inside.
    for node in tree.find_all(*_WRITE_NODES):
        return Verdict(
            False, Refusal.WRITE_OPERATION,
            f"a {type(node).__name__.upper()} is nested inside the statement",
        )
    for node in tree.find_all(*_DDL_NODES):
        return Verdict(
            False, Refusal.DDL,
            f"a {type(node).__name__.upper()} is nested inside the statement",
        )

    if not isinstance(tree, (exp.Select, exp.Union, exp.Subquery)):
        return Verdict(False, Refusal.NOT_SELECT, type(tree).__name__.upper())

    # SELECT ... INTO creates a table. It parses as a Select.
    if tree.args.get("into"):
        return Verdict(False, Refusal.DDL, "SELECT ... INTO creates a table")
    # FOR UPDATE takes row locks, which is a write intention.
    if tree.args.get("locks"):
        return Verdict(False, Refusal.WRITE_OPERATION, "row locking is not permitted")

    # --- functions ---------------------------------------------------------
    for fn in tree.find_all(exp.Anonymous):
        name = (fn.this or "").lower() if isinstance(fn.this, str) else ""
        if name in _FORBIDDEN_FUNCTIONS:
            return Verdict(False, Refusal.FUNCTION_NOT_ALLOWED, name)
    for fn in tree.find_all(exp.Func):
        name = (fn.sql_name() or "").lower()
        if name in _FORBIDDEN_FUNCTIONS:
            return Verdict(False, Refusal.FUNCTION_NOT_ALLOWED, name)

    # --- which tables ------------------------------------------------------
    # CTE names are not real tables; collect them so they are not demanded of
    # the registry.
    cte_names = {
        (cte.alias_or_name or "").lower()
        for cte in tree.find_all(exp.CTE)
    }

    referenced: set[str] = set()
    for table in tree.find_all(exp.Table):
        bare = (table.name or "").lower()
        if not bare or bare in cte_names:
            continue
        schema = (table.text("db") or "").lower()
        if schema in _SYSTEM_SCHEMAS:
            return Verdict(False, Refusal.SYSTEM_CATALOGUE, f"{schema}.{bare}")
        if bare.startswith("pg_"):
            return Verdict(False, Refusal.SYSTEM_CATALOGUE, bare)
        referenced.add(_qualified(table))

    if not referenced:
        return Verdict(False, Refusal.NO_TABLES, "no table is named")

    # This is the check that stops one company reading another's data. It is
    # the same refusal whether the table belongs to another tenant or does not
    # exist at all, so existence cannot be discovered by iterating (§6.1).
    unknown = sorted(referenced - permitted.tables)
    if unknown:
        return Verdict(
            False, Refusal.UNKNOWN_TABLE, ", ".join(unknown), tuple(sorted(referenced))
        )

    # --- viewer: public columns only ---------------------------------------
    if permitted.public_only:
        star = any(isinstance(n, exp.Star) for n in tree.find_all(exp.Star))
        if star:
            return Verdict(
                False, Refusal.FORBIDDEN_COLUMN,
                "SELECT * is not available on a viewer grant; name the columns",
                tuple(sorted(referenced)),
            )
        for col in tree.find_all(exp.Column):
            name = (col.name or "").lower()
            if not name:
                continue
            if name not in permitted.public_columns:
                return Verdict(
                    False, Refusal.FORBIDDEN_COLUMN, name, tuple(sorted(referenced))
                )

    # --- shape -------------------------------------------------------------
    depth = _max_depth(tree)
    if depth > MAX_SUBQUERY_DEPTH:
        return Verdict(
            False, Refusal.SUBQUERY_DEPTH,
            f"nested {depth} deep, limit is {MAX_SUBQUERY_DEPTH}",
            tuple(sorted(referenced)),
        )

    notes: list[str] = []
    if _has_cartesian_join(tree):
        # Not refused -- a deliberate cross join is legitimate -- but the
        # answer carries a warning, because it is usually a generation bug.
        notes.append("joins without a condition; the row count may be a product")

    return Verdict(
        allowed=True,
        tables=tuple(sorted(referenced)),
        normalised_sql=tree.sql(dialect=DIALECT),
        notes=notes,
    )


def _max_depth(tree: exp.Expression, depth: int = 0) -> int:
    best = depth
    for sub in tree.find_all(exp.Subquery, exp.Select):
        if sub is tree:
            continue
        best = max(best, _max_depth(sub, depth + 1))
        if best > MAX_SUBQUERY_DEPTH * 2:  # stop early; it is already refused
            return best
    return best


def _has_cartesian_join(tree: exp.Expression) -> bool:
    for join in tree.find_all(exp.Join):
        if join.args.get("on") or join.args.get("using"):
            continue
        if (join.side or "").upper() or (join.kind or "").upper() == "CROSS":
            continue
        return True
    return False


def enforce_limit(sql: str, max_rows: int) -> str:
    """Add or tighten a LIMIT. Runs after validation, never instead of it.

    The row cap is a resource guard, not a safety layer: an unbounded SELECT
    on a large table is a slow answer and a large response, not a dangerous
    one. Kept here because it is a statement rewrite and the validator is the
    only module allowed to rewrite statements.
    """
    try:
        tree = sqlglot.parse_one(sql, read=DIALECT)
    except Exception:
        return sql

    existing = tree.args.get("limit")
    if existing is not None:
        try:
            current = int(existing.expression.name)
            if current <= max_rows:
                return tree.sql(dialect=DIALECT)
        except (AttributeError, ValueError):
            pass

    return tree.limit(max_rows).sql(dialect=DIALECT)
