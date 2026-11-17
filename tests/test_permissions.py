"""The two axes of permission, through the API (§6, §19).

Product role (owner / member) and database role (analyst / viewer) are
different questions, and the matrix of the two is where a permission system
usually leaks. The parts that only Postgres can answer are in
test_privileges.py; this is what the APPLICATION allows and refuses.

The rule that this file exists to hold down: **a viewer never receives the
SQL, and neither does anyone in Readout.** Not hidden by the interface --
absent from the response, so there is nothing in the payload to reveal. The
same for a column an owner has not marked public: the validator refuses the
statement rather than the interface dropping a column.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

pytestmark = pytest.mark.skipif(
    not os.environ.get("META_DSN", "").startswith("postgresql"),
    reason="needs the bootstrapped Postgres; run `make test`",
)

WAREHOUSE = "harbor_dw"
DOMAIN = "permissions-test.example"
PUBLIC_COLUMNS = {"region_id", "region_name"}


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


def _remove(app) -> None:
    with app.state.sessions.begin() as session:
        org_id = session.execute(
            text("SELECT id FROM organisations WHERE domain = :d"),
            {"d": DOMAIN}).scalar_one_or_none()
        if org_id is None:
            return
        mine = "(SELECT id FROM people WHERE org_id = :org)"
        theirs = "(SELECT id FROM connections WHERE org_id = :org)"
        for statement in (
            f"DELETE FROM edit_log WHERE person_id IN {mine} OR connection_id IN {theirs}",
            "DELETE FROM query_log WHERE org_id = :org",
            "DELETE FROM audit_log WHERE org_id = :org",
            f"DELETE FROM merge_requests WHERE decided_by IN {mine} "
            f"   OR member_edit_id IN (SELECT id FROM member_edits WHERE person_id IN {mine})",
            f"DELETE FROM member_edits WHERE person_id IN {mine}",
            f"DELETE FROM access_grants WHERE person_id IN {mine} OR granted_by IN {mine}",
            f"DELETE FROM feedback WHERE person_id IN {mine}",
            f"DELETE FROM messages WHERE person_id IN {mine}",
            f"DELETE FROM threads WHERE person_id IN {mine}",
            "DELETE FROM invitations WHERE org_id = :org",
            "DELETE FROM connections WHERE org_id = :org",
            "DELETE FROM people WHERE org_id = :org",
            "DELETE FROM organisations WHERE id = :org",
        ):
            session.execute(text(statement), {"org": org_id})


@pytest.fixture(scope="module")
def people(stack):
    from app.main import app  # noqa: PLC0415
    from auth import jwt_handler  # noqa: PLC0415
    from db.entities import (  # noqa: PLC0415
        AccessGrant, Connection, Organisation, Person, SchemaColumn,
    )
    from db.introspect import introspect  # noqa: PLC0415

    _remove(app)
    settings = app.state.settings

    with app.state.sessions.begin() as session:
        org = Organisation(name="Permissions Test", kind="company", domain=DOMAIN)
        session.add(org)
        session.flush()

        owner = Person(email=f"owner@{DOMAIN}", org_id=org.id,
                       product_role="owner", state="active")
        analyst = Person(email=f"analyst@{DOMAIN}", org_id=org.id,
                         product_role="member", state="active")
        viewer = Person(email=f"viewer@{DOMAIN}", org_id=org.id,
                        product_role="member", state="active")
        connection = Connection(org_id=org.id, name="harbor_permissions",
                                kind="internal", database_name=WAREHOUSE)
        session.add_all([owner, analyst, viewer, connection])
        session.flush()
        session.add_all([
            AccessGrant(person_id=analyst.id, connection_id=connection.id,
                        db_role="analyst", granted_by=owner.id),
            AccessGrant(person_id=viewer.id, connection_id=connection.id,
                        db_role="viewer", granted_by=owner.id),
        ])
        introspect(session, app.state.engines.tenants.read(connection),
                   connection.id, only_schemas=("public",))

        # An owner decides what a viewer may read. Nothing is public until
        # they say so, so the fixture says so for exactly two columns.
        for column in session.scalars(text(  # noqa: S608 - fixed identifiers
            "SELECT id FROM schema_registry WHERE connection_id = :c "
            "AND table_name = 'regions'").bindparams(c=connection.id)):
            row = session.get(SchemaColumn, column)
            row.is_public = row.column_name in PUBLIC_COLUMNS

        ids = {"org": org.id, "connection": connection.id,
               "owner": owner.id, "analyst": analyst.id, "viewer": viewer.id}
        tokens = {
            role: jwt_handler.issue_access(
                settings.secret_key, person_id=ids[role], org_id=org.id,
                product_role="owner" if role == "owner" else "member",
                email=f"{role}@{DOMAIN}")
            for role in ("owner", "analyst", "viewer")
        }

    yield {"ids": ids, "tokens": tokens}
    _remove(app)


def _headers(people, role: str, persona: str | None = None) -> dict:
    headers = {"Authorization": f"Bearer {people['tokens'][role]}"}
    if persona:
        headers["X-SpeakQL-Persona"] = persona
    return headers


def _ask(stack, people, role: str, sql: str, persona: str | None = None) -> dict:
    stack.generator.sql = sql
    return stack.post("/api/ask", headers=_headers(people, role, persona),
                      json={"question": "which regions are there",
                            "connection_id": people["ids"]["connection"]}).json()


PUBLIC_QUERY = "SELECT region_name FROM public.regions ORDER BY region_name"
PRIVATE_QUERY = "SELECT amount FROM public.orders LIMIT 5"


# ============================================================== reading =====

def test_a_viewer_may_read_a_public_column(stack, people):
    answer = _ask(stack, people, "viewer", PUBLIC_QUERY)
    assert answer.get("rows"), answer
    assert answer["columns"] == ["region_name"]


def test_a_viewer_never_receives_the_sql(stack, people):
    """Absent from the payload, not hidden by the interface: a viewer looking
    at the network tab finds nothing, because nothing was sent."""
    answer = _ask(stack, people, "viewer", PUBLIC_QUERY)
    assert answer["sql"] is None


def test_a_viewer_cannot_read_a_column_the_owner_did_not_publish(stack, people):
    answer = _ask(stack, people, "viewer", PRIVATE_QUERY)
    assert answer.get("mode") == "blocked", answer
    assert answer["layer"] == 2
    assert answer.get("rows") is None


def test_an_analyst_reads_any_table_and_sees_the_sql(stack, people):
    answer = _ask(stack, people, "analyst", PRIVATE_QUERY)
    assert answer.get("rows"), answer
    assert answer["sql"] == PRIVATE_QUERY


def test_readout_withholds_the_sql_from_an_analyst_too(stack, people):
    """The persona is a display choice and the grant is a permission. In
    Readout the server removes the statement; it is not the interface
    declining to draw it."""
    answer = _ask(stack, people, "analyst", PRIVATE_QUERY, persona="readout")
    assert answer.get("rows"), answer
    assert answer["sql"] is None


def test_the_persona_header_cannot_grant_anything(stack, people):
    """Workbench does not turn a viewer into an analyst."""
    answer = _ask(stack, people, "viewer", PRIVATE_QUERY, persona="workbench")
    assert answer.get("mode") == "blocked"
    assert answer.get("sql") is None


# ========================================================= administering ====

def test_only_an_owner_may_invite(stack, people):
    body = {"email": f"new@{DOMAIN}", "grants": []}
    for role in ("analyst", "viewer"):
        assert stack.post("/api/org/invite", headers=_headers(people, role),
                          json=body).status_code == 403

    created = stack.post("/api/org/invite", headers=_headers(people, "owner"),
                         json=body)
    assert created.status_code == 201, created.text


def test_only_an_owner_sees_the_people_list(stack, people):
    assert stack.get("/api/org/people",
                     headers=_headers(people, "analyst")).status_code == 403
    assert stack.get("/api/org/people",
                     headers=_headers(people, "owner")).status_code == 200


def test_a_member_cannot_grant_themselves_access(stack, people):
    response = stack.post(
        f"/api/org/people/{people['ids']['viewer']}/grants",
        headers=_headers(people, "viewer"),
        json={"connection_id": people["ids"]["connection"], "db_role": "analyst"},
    )
    assert response.status_code == 403
