"""The correction workflow, end to end, against Postgres (§10, §21).

SQLite cannot host this test. A member's pending value lives in a table in the
`member_edits` SCHEMA, and the executor lays it over the real table with a
derived join -- neither of which SQLite has. The unit tests in
test_security.py cover what the edit validator refuses; this covers what
actually happens to the data, on the database the product runs on.

What it proves:

    an owner's correction lands in the real table immediately, with an
        edit_log row
    a member's correction does NOT touch the real table
    the member's OWN answers include their pending value, and nobody else's do
    approving a merge writes the real table
    a merge whose row moved underneath is refused as stale, not applied --
        last-write-wins here would silently destroy somebody's edit
    a viewer cannot correct anything

It runs against harbor_dw, never the Northwind demo warehouse, and restores
the row it touched -- a test suite that leaves the demo data changed is a
test suite that breaks the demo.

    make test                      (inside the api container)
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
SHIPMENT = 84112          # a row with a real unit count in the seed
DOMAIN = "corrections-test.example"


class _FixedSQL:
    """A generator that returns one statement, so /api/ask can be driven with
    no model loaded. What is under test here is the pipeline, not the model."""

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
        client.generator = generator          # the test sets the SQL per call
        yield client

    engines.dispose()


def _remove_test_org(app) -> None:
    """Delete everything this module created, in dependency order.

    The logs reference people and connections WITHOUT a cascade -- deliberately,
    because an audit trail that disappears when a row is deleted is not an
    audit trail. So they are cleared explicitly here, and a previous run that
    died half way cannot block the next one.
    """
    from db.entities import Connection, MemberEdit  # noqa: PLC0415

    with app.state.sessions.begin() as session:
        org_id = session.execute(
            text("SELECT id FROM organisations WHERE domain = :d"),
            {"d": DOMAIN}).scalar_one_or_none()
        if org_id is None:
            return

        # A member's overlay tables live in the warehouse, not in the registry.
        edits = session.execute(text(
            "SELECT DISTINCT person_id, connection_id, table_name "
            "FROM member_edits WHERE connection_id IN "
            "(SELECT id FROM connections WHERE org_id = :org)"), {"org": org_id}).all()
        for person_id, connection_id, table_name in edits:
            connection = session.get(Connection, connection_id)
            if connection is None:
                continue
            from db.edits_engine import overlay_table_for  # noqa: PLC0415
            engine = app.state.engines.tenants.edits(connection)
            with engine.begin() as conn:
                conn.execute(text(
                    f"DROP TABLE IF EXISTS "
                    f"{overlay_table_for(person_id, table_name)}"))

        # In dependency order. Several columns point at people and connections
        # WITHOUT a cascade -- granted_by, decided_by, the log rows -- and that
        # is deliberate: "who granted this" and "who approved that" must not
        # vanish when somebody leaves. It does mean a test has to clean up in
        # the right order rather than relying on one DELETE.
        mine = "(SELECT id FROM people WHERE org_id = :org)"
        theirs = "(SELECT id FROM connections WHERE org_id = :org)"
        for statement in (
            f"DELETE FROM edit_log WHERE person_id IN {mine} "
            f"   OR connection_id IN {theirs}",
            "DELETE FROM query_log WHERE org_id = :org",
            "DELETE FROM audit_log WHERE org_id = :org",
            f"DELETE FROM merge_requests WHERE decided_by IN {mine} "
            f"   OR member_edit_id IN (SELECT id FROM member_edits "
            f"                         WHERE person_id IN {mine})",
            f"DELETE FROM member_edits WHERE person_id IN {mine}",
            f"DELETE FROM access_grants WHERE person_id IN {mine} "
            f"   OR granted_by IN {mine}",
            f"DELETE FROM uploaded_datasets WHERE uploaded_by IN {mine} "
            f"   OR connection_id IN {theirs}",
            f"DELETE FROM feedback WHERE person_id IN {mine}",
            f"DELETE FROM messages WHERE person_id IN {mine}",
            f"DELETE FROM threads WHERE person_id IN {mine}",
            "DELETE FROM invitations WHERE org_id = :org",
            "DELETE FROM connections WHERE org_id = :org",
            "DELETE FROM people WHERE org_id = :org",
            "DELETE FROM organisations WHERE id = :org",
        ):
            session.execute(text(statement), {"org": org_id})
    _ = MemberEdit  # cascades from people; named so the intent is visible


@pytest.fixture(scope="module")
def people(stack):
    """An organisation of its own, on harbor_dw, cleaned up afterwards."""
    from app.main import app  # noqa: PLC0415
    from auth import jwt_handler  # noqa: PLC0415
    from db.entities import (  # noqa: PLC0415
        AccessGrant, Connection, Organisation, Person,
    )

    settings = app.state.settings
    sessions = app.state.sessions
    _remove_test_org(app)          # a previous run that died half way

    with sessions.begin() as session:
        org = Organisation(name="Corrections Test", kind="company", domain=DOMAIN)
        session.add(org)
        session.flush()

        owner = Person(email=f"owner@{DOMAIN}", org_id=org.id,
                       product_role="owner", state="active")
        member = Person(email=f"member@{DOMAIN}", org_id=org.id,
                        product_role="member", state="active")
        viewer = Person(email=f"viewer@{DOMAIN}", org_id=org.id,
                        product_role="member", state="active")
        connection = Connection(org_id=org.id, name="harbor_test",
                                kind="internal", database_name=WAREHOUSE)
        session.add_all([owner, member, viewer, connection])
        session.flush()

        session.add_all([
            AccessGrant(person_id=member.id, connection_id=connection.id,
                        db_role="analyst", granted_by=owner.id),
            AccessGrant(person_id=viewer.id, connection_id=connection.id,
                        db_role="viewer", granted_by=owner.id),
        ])
        session.flush()

        from db.introspect import introspect  # noqa: PLC0415
        introspect(session, app.state.engines.tenants.read(connection),
                   connection.id, only_schemas=("public",))

        ids = {
            "org": org.id, "connection": connection.id,
            "owner": owner.id, "member": member.id, "viewer": viewer.id,
        }
        tokens = {
            role: jwt_handler.issue_access(
                settings.secret_key, person_id=ids[role], org_id=org.id,
                product_role="owner" if role == "owner" else "member",
                email=f"{role}@{DOMAIN}")
            for role in ("owner", "member", "viewer")
        }

    before = _units(app, ids["connection"])

    yield {"ids": ids, "tokens": tokens, "before": before}

    # Put the warehouse back, then remove the test organisation. A suite that
    # leaves the demo data changed is a suite that breaks the demo.
    _set_units(app, ids["connection"], before)
    _remove_test_org(app)


def _connection(app, connection_id: int):
    from db.entities import Connection  # noqa: PLC0415
    with app.state.sessions() as session:
        return session.get(Connection, connection_id)


def _units(app, connection_id: int):
    engine = app.state.engines.tenants.read(_connection(app, connection_id))
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT units FROM public.shipments WHERE shipment_id = :id"),
            {"id": SHIPMENT}).scalar_one()


def _set_units(app, connection_id: int, value) -> None:
    engine = app.state.engines.tenants.write(_connection(app, connection_id))
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE public.shipments SET units = :v WHERE shipment_id = :id"),
            {"v": value, "id": SHIPMENT})


def _auth(people, role: str) -> dict:
    return {"Authorization": f"Bearer {people['tokens'][role]}"}


def _correct(stack, people, role: str, value: str) -> dict:
    return stack.post("/api/rows/edit", headers=_auth(people, role), json={
        "connection_id": people["ids"]["connection"],
        "table_name": "shipments", "pk_column": "shipment_id",
        "pk_value": str(SHIPMENT), "column_name": "units",
        "after_value": value, "note": f"set by the {role} test",
    })


# ================================================================ owner =====

def test_an_owners_correction_lands_in_the_real_table(stack, people):
    from app.main import app  # noqa: PLC0415

    response = _correct(stack, people, "owner", "4242")
    body = response.json()

    assert response.status_code == 200, body
    assert body["landed"] == "real_table"
    assert body["edit_log_id"] is not None
    assert _units(app, people["ids"]["connection"]) == 4242

    with app.state.sessions() as session:
        logged = session.execute(text(
            "SELECT after_value FROM edit_log WHERE id = :id"
        ), {"id": body["edit_log_id"]}).scalar_one()
    assert logged == "4242"


def test_a_viewer_cannot_correct_anything(stack, people):
    response = _correct(stack, people, "viewer", "9999")
    assert response.status_code == 403


# =============================================================== member =====

def test_a_members_correction_does_not_touch_the_real_table(stack, people):
    from app.main import app  # noqa: PLC0415

    before = _units(app, people["ids"]["connection"])
    response = _correct(stack, people, "member", "777")
    body = response.json()

    assert response.status_code == 200, body
    assert body["landed"] == "overlay"
    assert body["merge_request_id"] is not None
    assert _units(app, people["ids"]["connection"]) == before, \
        "a member's correction reached the real table"


def test_the_member_sees_their_own_pending_value_and_nobody_else_does(stack, people):
    """The overlay, through the actual /api/ask path.

    The same statement, asked by two people, must return different numbers --
    the member's pending value for them, the real one for the owner. If this
    ever returns the same number for both, either the overlay is dead or it is
    leaking into everyone's answers, and both are serious.
    """
    stack.generator.sql = (
        f"SELECT units FROM public.shipments WHERE shipment_id = {SHIPMENT}")
    body = {"question": f"How many units were on shipment {SHIPMENT}?",
            "connection_id": people["ids"]["connection"]}

    member = stack.post("/api/ask", headers=_auth(people, "member"), json=body).json()
    owner = stack.post("/api/ask", headers=_auth(people, "owner"), json=body).json()

    assert member.get("rows") == [[777]], member
    assert owner.get("rows") == [[4242]], owner
    assert member["includes_pending_edit"] is True
    assert owner["includes_pending_edit"] is False


def test_approving_the_merge_writes_the_real_table(stack, people):
    from app.main import app  # noqa: PLC0415

    merges = stack.get("/api/merges", headers=_auth(people, "owner")).json()
    open_merges = [m for m in merges if m["state"] == "open"]
    assert open_merges, "the member's correction raised no merge request"

    decided = stack.post(f"/api/merges/{open_merges[0]['id']}",
                         headers=_auth(people, "owner"),
                         json={"decision": "approve"})
    assert decided.status_code == 200, decided.text
    assert decided.json()["state"] == "merged"
    assert _units(app, people["ids"]["connection"]) == 777


# ============================================================ staleness =====

def test_a_merge_whose_row_moved_is_refused_not_applied(stack, people):
    """Last-write-wins here would silently destroy the owner's change. The
    merge is refused, marked stale, and the owner is shown all three values."""
    from app.main import app  # noqa: PLC0415

    _correct(stack, people, "member", "555")          # member proposes 555
    _correct(stack, people, "owner", "888")           # owner changes it first

    merges = stack.get("/api/merges", headers=_auth(people, "owner")).json()
    open_merges = [m for m in merges if m["state"] == "open"]
    assert open_merges

    decided = stack.post(f"/api/merges/{open_merges[0]['id']}",
                         headers=_auth(people, "owner"),
                         json={"decision": "approve"})
    body = decided.json()

    assert body["state"] == "stale", body
    assert _units(app, people["ids"]["connection"]) == 888, \
        "a stale merge overwrote the owner's value"
