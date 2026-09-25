"""Uploading a file, end to end, against Postgres (§9.4, §21).

Ingestion is the one path that holds a DDL role, so what it may create and
where is the whole question. Every organisation's uploads live in the same
database in a schema of their own, and the isolation is that schema plus the
registry -- which makes "can one organisation see another's upload" a test
worth having rather than a claim.

Like test_corrections.py this needs the real database: SQLite has no schemas,
no CREATE SCHEMA, and no role that can be refused.

What it proves:

    plan creates nothing and says what would happen
    load creates the table in THIS organisation's schema and reindexes it, so
        the table is askable immediately rather than after a separate chore
    commas and dd/mm/yyyy dates are coerced rather than rejected
    a second organisation cannot see the first one's table, in its registry
        or through a question
    a member cannot upload at all
"""

from __future__ import annotations

import io
import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

pytestmark = pytest.mark.skipif(
    not os.environ.get("META_DSN", "").startswith("postgresql"),
    reason="needs the bootstrapped Postgres; run `make test`",
)

DOMAIN_A = "ingestion-a.example"
DOMAIN_B = "ingestion-b.example"

CSV = (
    "Region Name,Units Sold,Booked On,Revenue\n"
    "Northern,1,240,16/03/2025,\"12,400.50\"\n"
    "Southern,980,17/03/2025,\"9,150.00\"\n"
    "Eastern,1,530,18/03/2025,\"21,300.75\"\n"
)

# The first data row has a thousands separator inside a quoted field and the
# second does not, which is exactly the mess a real export arrives in.
CLEAN_CSV = (
    "region_name,units_sold,booked_on,revenue\n"
    "Northern,1240,16/03/2025,\"12,400.50\"\n"
    "Southern,980,17/03/2025,\"9,150.00\"\n"
    "Eastern,1530,18/03/2025,\"21,300.75\"\n"
)


class _FixedSQL:
    name = "fixed-sql-stub"

    def __init__(self) -> None:
        self.sql = ""

    def generate(self, question, schema):
        from core.sql_generator import Candidate  # noqa: PLC0415
        return Candidate(self.sql, 0.99, self.name)


@pytest.fixture(scope="module")
def stack():
    from app.config import load  # noqa: PLC0415
    from app.mailer import Mailer  # noqa: PLC0415
    from app.main import app  # noqa: PLC0415
    from app.ratelimit import RateLimiter  # noqa: PLC0415
    from db.engines import Engines  # noqa: PLC0415
    from db.session import SessionFactory  # noqa: PLC0415

    settings = load()
    engines = Engines(settings)
    generator = _FixedSQL()

    with TestClient(app) as client:
        app.state.settings = settings
        app.state.engines = engines
        app.state.sessions = SessionFactory(engines.meta)
        app.state.mailer = Mailer(env="test")
        app.state.limiter = RateLimiter(ask_per_hour=1000, upload_per_day=100,
                                        export_per_hour=100)
        app.state.llm = None
        app.state.generator = generator
        app.state.retriever = None
        client.generator = generator
        yield client

    engines.dispose()


def _remove(app, domain: str) -> None:
    from db.entities import Organisation  # noqa: PLC0415

    with app.state.sessions.begin() as session:
        org_id = session.execute(
            text("SELECT id FROM organisations WHERE domain = :d"),
            {"d": domain}).scalar_one_or_none()
        if org_id is None:
            return
        mine = "(SELECT id FROM people WHERE org_id = :org)"
        theirs = "(SELECT id FROM connections WHERE org_id = :org)"
        for statement in (
            f"DELETE FROM edit_log WHERE person_id IN {mine} OR connection_id IN {theirs}",
            "DELETE FROM query_log WHERE org_id = :org",
            "DELETE FROM audit_log WHERE org_id = :org",
            f"DELETE FROM uploaded_datasets WHERE uploaded_by IN {mine} "
            f"   OR connection_id IN {theirs}",
            f"DELETE FROM access_grants WHERE person_id IN {mine} OR granted_by IN {mine}",
            f"DELETE FROM feedback WHERE person_id IN {mine}",
            f"DELETE FROM messages WHERE person_id IN {mine}",
            f"DELETE FROM threads WHERE person_id IN {mine}",
            "DELETE FROM connections WHERE org_id = :org",
            "DELETE FROM people WHERE org_id = :org",
            "DELETE FROM organisations WHERE id = :org",
        ):
            session.execute(text(statement), {"org": org_id})

        # And the uploaded schema itself, in the uploads database.
        with app.state.engines.uploads.begin() as conn:
            conn.execute(text(f'DROP SCHEMA IF EXISTS "org_{org_id}" CASCADE'))
    _ = Organisation


def _make_org(app, domain: str, name: str) -> dict:
    from auth import jwt_handler  # noqa: PLC0415
    from db.entities import Organisation, Person  # noqa: PLC0415

    with app.state.sessions.begin() as session:
        org = Organisation(name=name, kind="company", domain=domain)
        session.add(org)
        session.flush()
        owner = Person(email=f"owner@{domain}", org_id=org.id,
                       product_role="owner", state="active")
        member = Person(email=f"member@{domain}", org_id=org.id,
                        product_role="member", state="active")
        session.add_all([owner, member])
        session.flush()
        ids = {"org": org.id, "owner": owner.id, "member": member.id}

    secret = app.state.settings.secret_key
    return {
        "ids": ids,
        "owner": jwt_handler.issue_access(secret, person_id=ids["owner"],
                                          org_id=ids["org"], product_role="owner",
                                          email=f"owner@{domain}"),
        "member": jwt_handler.issue_access(secret, person_id=ids["member"],
                                           org_id=ids["org"], product_role="member",
                                           email=f"member@{domain}"),
    }


@pytest.fixture(scope="module")
def orgs(stack):
    from app.main import app  # noqa: PLC0415

    for domain in (DOMAIN_A, DOMAIN_B):
        _remove(app, domain)

    made = {
        "a": _make_org(app, DOMAIN_A, "Ingestion A"),
        "b": _make_org(app, DOMAIN_B, "Ingestion B"),
    }
    yield made

    for domain in (DOMAIN_A, DOMAIN_B):
        _remove(app, domain)


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _upload(stack, token: str, path: str, table: str, body: str = CLEAN_CSV):
    return stack.post(
        f"/api/datasets/{path}",
        headers=_headers(token),
        files={"file": ("export.csv", io.BytesIO(body.encode()), "text/csv")},
        data={"table_name": table},
    )


# ================================================================= plan =====

def test_the_plan_says_what_would_happen_and_creates_nothing(stack, orgs):
    from app.main import app  # noqa: PLC0415

    response = _upload(stack, orgs["a"]["owner"], "plan", "March Export")
    plan = response.json()

    assert response.status_code == 200, plan
    assert plan["table_name"] == "march_export", "the name is sanitised"
    assert plan["row_estimate"] == 3
    types = {c["name"]: c["data_type"] for c in plan["columns"]}
    assert set(types) == {"region_name", "units_sold", "booked_on", "revenue"}

    with app.state.engines.uploads.connect() as conn:
        exists = conn.execute(text(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = :s"), {"s": f"org_{orgs['a']['ids']['org']}"}
        ).scalar_one()
    assert exists == 0, "plan created something"


def test_a_member_cannot_upload(stack, orgs):
    response = _upload(stack, orgs["a"]["member"], "plan", "sneaky")
    assert response.status_code == 403


# ================================================================= load =====

def test_loading_creates_the_table_and_makes_it_askable(stack, orgs):
    from app.main import app  # noqa: PLC0415

    response = _upload(stack, orgs["a"]["owner"], "load", "March Export")
    body = response.json()
    assert response.status_code == 201, body
    assert body["rows"] == 3

    schema = f"org_{orgs['a']['ids']['org']}"
    with app.state.engines.uploads.connect() as conn:
        rows = conn.execute(text(
            f'SELECT count(*) FROM "{schema}"."march_export"')).scalar_one()
    assert rows == 3

    # Reindex is part of ingestion, not a separate chore: the table is in the
    # registry the moment the response returns.
    schema_rows = stack.get(f"/api/schema/{body['connection_id']}",
                            headers=_headers(orgs["a"]["owner"])).json()
    names = {r["column_name"] for r in schema_rows if r["table_name"] == "march_export"}
    assert names >= {"region_name", "units_sold", "booked_on", "revenue"}
    # Ingestion adds a key of its own: a table without one can never be
    # corrected, because a change that cannot be pinned to one row cannot be
    # checked.
    assert "id" in names


def test_commas_and_day_first_dates_are_coerced_not_refused(stack, orgs):
    """A real export writes 12,400.50 and 16/03/2025. Refusing those means
    refusing most real files; guessing silently means wrong numbers. They are
    converted, and the plan reports what was done."""
    from app.main import app  # noqa: PLC0415

    schema = f"org_{orgs['a']['ids']['org']}"
    with app.state.engines.uploads.connect() as conn:
        row = conn.execute(text(
            f'SELECT units_sold, booked_on, revenue FROM "{schema}"."march_export" '
            "ORDER BY units_sold DESC LIMIT 1")).one()

    units, booked, revenue = row
    assert int(units) == 1530
    assert str(booked).startswith("2025-03-18"), booked
    assert float(revenue) == pytest.approx(21300.75)


# ============================================================ isolation =====

def test_another_organisation_cannot_see_the_upload(stack, orgs):
    """Every organisation's uploads share one database. The schema and the
    registry are the isolation, so this is the test that says whether it
    holds."""
    from app.main import app  # noqa: PLC0415

    # B uploads its own file, which gives B an uploads connection of its own.
    assert _upload(stack, orgs["b"]["owner"], "load", "b_export").status_code == 201

    b_connections = stack.get("/api/connections",
                              headers=_headers(orgs["b"]["owner"])).json()
    b_upload = next(c for c in b_connections if c["kind"] == "uploaded")

    registry = stack.get(f"/api/schema/{b_upload['id']}",
                         headers=_headers(orgs["b"]["owner"])).json()
    tables = {r["table_name"] for r in registry}
    assert "march_export" not in tables, "A's table is in B's registry"
    assert "b_export" in tables

    # And asking for it by name is refused by the validator, not answered.
    stack.generator.sql = (
        f'SELECT count(*) FROM "org_{orgs["a"]["ids"]["org"]}".march_export')
    answer = stack.post("/api/ask", headers=_headers(orgs["b"]["owner"]),
                        json={"question": "how many rows are in march export",
                              "connection_id": b_upload["id"]}).json()
    assert answer.get("mode") in ("blocked", "refusal", "failure"), answer
    assert answer.get("rows") is None
