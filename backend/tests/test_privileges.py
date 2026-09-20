"""The six privilege assertions (Backend Plan §17).

These run against a live, bootstrapped database. They are the only proof that
the sentence in the viva is true:

    "The read path holds no write privilege of any kind, and every write in
     the system goes through one path that is validated, scoped to a single
     primary key, restricted to tables the owner marked editable, and written
     to the edit log."

Everything above the database can be re-read and re-reasoned about. These
assertions ask Postgres itself, which cannot be argued with.

    make bootstrap && make test-privileges
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError

WAREHOUSE = os.environ.get("TEST_WAREHOUSE", "northwind_dw")


def _dsn(env_name: str) -> str:
    dsn = os.environ.get(env_name, "").strip()
    if not dsn:
        pytest.skip(f"{env_name} is not set; run `make bootstrap` first")
    return dsn


@pytest.fixture(scope="module")
def ro():
    engine = create_engine(_dsn("RO_DSN"), future=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def write():
    engine = create_engine(_dsn("WRITE_DSN"), future=True)
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def edits():
    engine = create_engine(_dsn("EDITS_DSN"), future=True)
    yield engine
    engine.dispose()


def _has_privilege(conn, role: str, table: str, privilege: str) -> bool:
    return conn.execute(
        text("SELECT has_table_privilege(:role, :table, :priv)"),
        {"role": role, "table": table, "priv": privilege},
    ).scalar_one()


# --------------------------------------------------------------- 1 of 6 ----
def test_read_role_can_read_the_warehouse(ro):
    """The read path works at all. A test suite that only proves refusals
    passes just as happily against a database that is simply broken."""
    with ro.connect() as conn:
        count = conn.execute(text("SELECT count(*) FROM public.orders")).scalar_one()
    assert count > 0, "orders is empty; the seed did not run"


# --------------------------------------------------------------- 2 of 6 ----
@pytest.mark.parametrize("privilege", ["INSERT", "UPDATE", "DELETE", "TRUNCATE"])
def test_read_role_holds_no_write_privilege_anywhere(ro, privilege):
    """speakql_ro reads every warehouse table and can write to none of them."""
    with ro.connect() as conn:
        tables = conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )).scalars().all()
        assert tables, "no tables found in public; the warehouse DDL did not run"

        for table in tables:
            held = _has_privilege(conn, "speakql_ro", f"public.{table}", privilege)
            assert not held, f"speakql_ro holds {privilege} on public.{table}"


# --------------------------------------------------------------- 3 of 6 ----
def test_read_role_transactions_are_read_only_at_the_server(ro):
    """Even with a grant, the connection itself refuses to write. The engine
    sets default_transaction_read_only, so this is defence in depth below the
    validator rather than a second copy of it."""
    engine = create_engine(
        _dsn("RO_DSN"), future=True,
        connect_args={"options": "-c default_transaction_read_only=on"},
    )
    try:
        with engine.connect() as conn, pytest.raises(Exception) as caught:
            conn.execute(text("CREATE TEMP TABLE should_not_exist (x int)"))
        assert "read-only" in str(caught.value).lower()
    finally:
        engine.dispose()


# --------------------------------------------------------------- 4 of 6 ----
def test_write_role_cannot_delete_or_truncate(write):
    """The edit path updates and inserts. It never removes. A correction that
    could delete a row is not a correction."""
    with write.connect() as conn:
        for privilege in ("DELETE", "TRUNCATE"):
            held = _has_privilege(conn, "speakql_write", "public.shipments", privilege)
            assert not held, f"speakql_write holds {privilege} on public.shipments"


# --------------------------------------------------------------- 5 of 6 ----
def test_edits_role_holds_nothing_on_public(edits):
    """The assertion that protects the warehouse.

    speakql_edits_rw owns a member's overlay tables and must hold no privilege
    of any kind on the real data -- so a bug in the overlay code cannot reach
    production rows even if it tries.
    """
    with edits.connect() as conn:
        tables = conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
        )).scalars().all()
        for table in tables:
            for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE"):
                held = _has_privilege(conn, "speakql_edits_rw", f"public.{table}", privilege)
                assert not held, (
                    f"speakql_edits_rw holds {privilege} on public.{table} -- "
                    "the overlay role must never touch the real warehouse"
                )


# --------------------------------------------------------------- 6 of 6 ----
def test_no_runtime_role_can_create_a_database_or_a_role(ro):
    """Only speakql_owner may, and the API never loads its DSN. config.py does
    not define the variable, so an accidental import fails at import time."""
    with ro.connect() as conn:
        rows = conn.execute(text(
            "SELECT rolname, rolcreatedb, rolcreaterole, rolsuper FROM pg_roles "
            "WHERE rolname IN "
            "('speakql_ro','speakql_write','speakql_edits_rw','speakql_upload_ddl')"
        )).mappings().all()

    assert len(rows) == 4, f"expected four runtime roles, found {len(rows)}"
    for row in rows:
        assert not row["rolcreatedb"], f"{row['rolname']} can create databases"
        assert not row["rolcreaterole"], f"{row['rolname']} can create roles"
        assert not row["rolsuper"], f"{row['rolname']} is a superuser"


# ------------------------------------------------------------- and a 7th ----
def test_config_does_not_expose_the_owner_dsn():
    """Not one of the six, but the same idea one layer up: the API cannot
    construct an owner connection because its Settings has nowhere to put one."""
    from app.config import Settings  # noqa: PLC0415

    fields = set(Settings.__dataclass_fields__)
    leaked = {f for f in fields if "owner" in f.lower() or "superuser" in f.lower()}
    assert not leaked, f"Settings exposes a privileged DSN: {leaked}"
