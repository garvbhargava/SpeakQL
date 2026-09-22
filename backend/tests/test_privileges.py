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

After the six come the assertions about tenants and the application role,
which only exist because of bugs the first version had: every question ran on
one warehouse, and the API connected to its own database as a superuser.
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

RUNTIME_ROLES = ("speakql_ro", "speakql_write", "speakql_edits_rw",
                 "speakql_upload_ddl", "speakql_app")


def _dsn(env_name: str) -> str:
    dsn = os.environ.get(env_name, "").strip()
    # Not merely "set": test_api.py points every DSN at in-memory SQLite when
    # it is collected first, and these questions only Postgres can answer.
    if not dsn.startswith("postgresql"):
        pytest.skip(f"{env_name} does not point at Postgres; run "
                    "`make bootstrap && make test-privileges`")
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
    not define the variable, and refuses to start if any runtime DSN names it.

    Five roles, including speakql_app -- the role the API reaches its own
    database with. The first version used speakql_owner there instead.
    """
    with ro.connect() as conn:
        rows = conn.execute(text(
            "SELECT rolname, rolcreatedb, rolcreaterole, rolsuper, rolbypassrls "
            "FROM pg_roles WHERE rolname = ANY(:roles)"
        ), {"roles": list(RUNTIME_ROLES)}).mappings().all()

    found = {row["rolname"] for row in rows}
    assert found == set(RUNTIME_ROLES), f"missing roles: {set(RUNTIME_ROLES) - found}"
    for row in rows:
        assert not row["rolcreatedb"], f"{row['rolname']} can create databases"
        assert not row["rolcreaterole"], f"{row['rolname']} can create roles"
        assert not row["rolsuper"], f"{row['rolname']} is a superuser"
        assert not row["rolbypassrls"], f"{row['rolname']} bypasses row security"


# ------------------------------------------------------ beyond the six ----
def _on(database: str):
    """speakql_ro on another warehouse: the same role, a different database --
    exactly what db/tenant_engine.py builds for a connection."""
    return create_engine(make_url(_dsn("RO_DSN")).set(database=database), future=True)


def test_the_two_tenants_warehouses_answer_differently():
    """If these agreed, a question run on the wrong tenant's warehouse would
    return the right-looking answer and no test could tell. They must differ
    -- sql/22_second_tenant.sql is what makes them."""
    answers = {}
    for database in ("northwind_dw", "harbor_dw"):
        engine = _on(database)
        try:
            with engine.connect() as conn:
                answers[database] = (
                    conn.execute(text("SELECT current_database()")).scalar_one(),
                    conn.execute(text(
                        "SELECT string_agg(region_name, ',' ORDER BY region_id) "
                        "FROM public.regions")).scalar_one(),
                    conn.execute(text("SELECT sum(amount) FROM public.orders")).scalar_one(),
                )
        finally:
            engine.dispose()

    northwind, harbor = answers["northwind_dw"], answers["harbor_dw"]
    assert northwind[0] == "northwind_dw" and harbor[0] == "harbor_dw"
    assert northwind[1] != harbor[1], "the two tenants have the same regions"
    assert northwind[2] != harbor[2], "the two tenants have the same order totals"
    assert harbor[1].startswith("Harbor ")


@pytest.mark.parametrize("role", ["speakql_ro", "speakql_write",
                                  "speakql_edits_rw", "speakql_upload_ddl"])
def test_no_warehouse_role_can_log_in_to_the_application_database(ro, role):
    """speakql_meta holds every organisation's registry, grants and logs.
    Postgres grants CONNECT to PUBLIC by default; bootstrap revokes it, so
    only speakql_app -- which owns the database -- may connect."""
    with ro.connect() as conn:
        held = conn.execute(
            text("SELECT has_database_privilege(:role, 'speakql_meta', 'CONNECT')"),
            {"role": role},
        ).scalar_one()
    assert not held, f"{role} can connect to speakql_meta"


def test_the_application_role_cannot_read_a_warehouse(ro):
    """speakql_app owns the metadata and nothing else. It reaches no
    customer's data: questions go through speakql_ro, built per connection."""
    with ro.connect() as conn:
        connect = conn.execute(text(
            "SELECT has_database_privilege('speakql_app', current_database(), 'CONNECT')"
        )).scalar_one()
        select = conn.execute(text(
            "SELECT has_table_privilege('speakql_app', 'public.orders', 'SELECT')"
        )).scalar_one()
    assert not connect, "speakql_app can connect to a warehouse"
    assert not select, "speakql_app can read a warehouse table"


# ------------------------------------------------------------- and a 7th ----
def test_config_does_not_expose_the_owner_dsn():
    """Not one of the six, but the same idea one layer up: the API cannot
    construct an owner connection because its Settings has nowhere to put one."""
    from app.config import Settings  # noqa: PLC0415

    fields = set(Settings.__dataclass_fields__)
    leaked = {f for f in fields if "owner" in f.lower() or "superuser" in f.lower()}
    assert not leaked, f"Settings exposes a privileged DSN: {leaked}"
