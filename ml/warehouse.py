"""The seeded warehouse's shape, read from the registry the API itself uses.

Not typed out again here. If the schema and the training data can disagree,
one day they will -- and the symptom is a model that quietly writes SQL for a
table that no longer looks like that.

The registry is also checked against the warehouse it describes before it is
returned. It is written by the bootstrap, so a registry left from before the
warehouse changed -- six tables, where §11.4 now has 18 -- would train both
models on a warehouse that no longer exists. Training stops instead.
"""

from __future__ import annotations

import os

from ml.paths import BACKEND

CONNECTION = "northwind_sales"


def _dsn(name: str) -> str:
    dsn = os.environ.get(name, "")
    if not dsn:
        env = BACKEND / ".env"
        if env.exists():
            for line in env.read_text(encoding="utf-8").splitlines():
                if line.startswith(f"{name}="):
                    dsn = line.split("=", 1)[1].strip()
    if not dsn:
        raise SystemExit(f"set {name} (the stack must be bootstrapped)")
    return dsn


def warehouse_schema(connection: str = CONNECTION
                     ) -> tuple[dict[str, list[str]], list[dict]]:
    """({table: [column, ...]}, [foreign key, ...]) for one connection."""
    from sqlalchemy import create_engine, text  # noqa: PLC0415

    engine = create_engine(_dsn("META_DSN"), future=True)
    try:
        with engine.connect() as conn:
            database = conn.execute(text(
                "SELECT database_name FROM connections WHERE name = :name"
            ), {"name": connection}).scalar()
            rows = conn.execute(text(
                "SELECT r.table_name, r.column_name, r.references_to "
                "FROM schema_registry r JOIN connections c ON c.id = r.connection_id "
                "WHERE c.name = :name AND r.schema_name = 'public' ORDER BY r.id"
            ), {"name": connection}).all()
    finally:
        engine.dispose()

    if not rows:
        raise SystemExit(
            f"no registry rows for {connection!r}; run the bootstrap first")

    tables: dict[str, list[str]] = {}
    foreign_keys: list[dict] = []
    for table_name, column_name, references_to in rows:
        tables.setdefault(table_name, []).append(column_name)
        if references_to:
            target = references_to.split(".")
            foreign_keys.append({
                "table": table_name, "column": column_name,
                "references_table": target[-2], "references_column": target[-1],
            })

    _matches_the_warehouse(connection, database, tables)
    return tables, foreign_keys


def _matches_the_warehouse(connection: str, database: str | None,
                           tables: dict[str, list[str]]) -> None:
    """Refuse a registry whose tables and columns differ from the warehouse's.

    Read as speakql_ro, the role every read in the product uses, from the
    catalogue rather than from anything the seed or the registry wrote.
    """
    from sqlalchemy import create_engine, text  # noqa: PLC0415
    from sqlalchemy.engine import make_url  # noqa: PLC0415

    if not database:
        raise SystemExit(f"{connection!r} names no hosted database to check against")

    engine = create_engine(make_url(_dsn("RO_DSN")).set(database=database),
                           future=True)
    try:
        with engine.connect() as conn:
            live = {(table, column) for table, column in conn.execute(text(
                "SELECT c.table_name, c.column_name "
                "FROM information_schema.columns c "
                "JOIN information_schema.tables t "
                "  ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
                "WHERE c.table_schema = 'public' AND t.table_type = 'BASE TABLE'"))}
    finally:
        engine.dispose()

    registry = {(table, column) for table, columns in tables.items()
                for column in columns}
    if registry != live:
        missing = sorted(f"{t}.{c}" for t, c in live - registry)
        stale = sorted(f"{t}.{c}" for t, c in registry - live)
        raise SystemExit(
            f"the registry for {connection!r} does not match {database}: "
            f"missing {missing or 'nothing'}, no longer there {stale or 'nothing'}. "
            "Run the bootstrap again so training sees the warehouse as it is.")


if __name__ == "__main__":
    tables, foreign_keys = warehouse_schema()
    for name, columns in tables.items():
        print(f"{name}: {', '.join(columns)}")
    for fk in foreign_keys:
        print(f"  {fk['table']}.{fk['column']} -> "
              f"{fk['references_table']}.{fk['references_column']}")
    print(f"{len(tables)} tables, {len(foreign_keys)} foreign keys; "
          "the registry matches the warehouse")
