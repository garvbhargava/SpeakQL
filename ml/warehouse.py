"""The seeded warehouse's shape, read from the registry the API itself uses.

Not typed out again here. If the schema and the training data can disagree,
one day they will -- and the symptom is a model that quietly writes SQL for a
table that no longer looks like that.
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

    return tables, foreign_keys


if __name__ == "__main__":
    tables, foreign_keys = warehouse_schema()
    for name, columns in tables.items():
        print(f"{name}: {', '.join(columns)}")
    for fk in foreign_keys:
        print(f"  {fk['table']}.{fk['column']} -> "
              f"{fk['references_table']}.{fk['references_column']}")
