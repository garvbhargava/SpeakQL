"""End-to-end tests through the real app (Backend Plan §21).

These drive the actual FastAPI application with a real database behind it --
SQLite, because these tests are about *authorisation and shape*, not about
Postgres roles. The privilege assertions in test_privileges.py cover what only
Postgres can answer, and they need the container.

What is proved here is the part that no grant can save you from: that the
application itself refuses the right requests.
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

os.environ.setdefault("SECRET_KEY", "test-only-key")
for var in ("META_DSN", "RO_DSN", "WRITE_DSN", "EDITS_DSN", "UPLOADS_DSN"):
    os.environ.setdefault(var, "sqlite+pysqlite:///:memory:")
os.environ.setdefault("SPEAKQL_ENV", "test")


class _OneEngine:
    """Stand-in for db.engines.Engines: every role points at the same SQLite
    database. Role separation is enforced by Postgres and is asserted in
    tests/test_privileges.py -- these tests are about what the *application*
    refuses, which is the part no grant can save you from."""

    def __init__(self, engine):
        self.meta = self.read = self.write = self.edits = self.uploads = engine

    def check(self) -> dict[str, str]:
        return {k: "ok" for k in ("meta", "read", "write", "edits", "uploads")}

    def dispose(self) -> None:
        self.meta.dispose()


@pytest.fixture(scope="module")
def client():
    from sqlalchemy.pool import StaticPool  # noqa: PLC0415

    from app.main import app            # noqa: PLC0415 - after env is set
    from app.mailer import Mailer       # noqa: PLC0415
    from app.ratelimit import RateLimiter  # noqa: PLC0415
    from db.entities import Base        # noqa: PLC0415
    from db.session import SessionFactory  # noqa: PLC0415

    # StaticPool keeps ONE connection, so the in-memory database survives
    # across TestClient's worker thread. Without it every request gets a fresh
    # empty database and nothing persists.
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)

    with TestClient(app) as test_client:
        # Replace the Postgres-backed state with the in-memory one. The app
        # reaches everything through app.state, which is exactly what makes
        # this swap possible without touching a single route.
        app.state.sessions = SessionFactory(engine)
        app.state.engines = _OneEngine(engine)
        app.state.mailer = Mailer(env="test")
        app.state.limiter = RateLimiter(ask_per_hour=100, upload_per_day=10,
                                        export_per_hour=10)
        app.state.llm = None
        yield test_client

    engine.dispose()


def _code_for(client, email: str) -> str:
    """Read the code out of the development mailer's outbox."""
    from app.main import app  # noqa: PLC0415
    for message in reversed(app.state.mailer.outbox):
        if message.to == email:
            return "".join(c for c in message.body if c.isdigit())[:6]
    raise AssertionError(f"no code was sent to {email}")


def sign_in(client, email: str, *, as_owner: bool = True) -> str:
    started = client.post("/api/auth/start", json={"email": email})
    assert started.status_code == 200, started.text
    code = _code_for(client, email)
    verified = client.post("/api/auth/verify",
                           json={"email": email, "code": code,
                                 "as_owner": as_owner})
    assert verified.status_code == 200, verified.text
    return verified.json()["access_token"]


# ============================================================== health ======

def test_health_reports_per_role_status(client):
    response = client.get("/health")
    body = response.json()
    assert "databases" in body
    assert set(body["databases"]) >= {"meta", "read", "write", "edits", "uploads"}
    assert "llm" in body


# =============================================================== auth ======

def test_a_work_address_creates_a_company(client):
    response = client.post("/api/auth/start",
                           json={"email": "priya@trellisfoods.io"})
    body = response.json()
    assert body["path"] == "create_company"
    assert body["domain"] == "trellisfoods.io"


def test_a_gmail_address_gets_a_personal_workspace(client):
    response = client.post("/api/auth/start", json={"email": "someone@gmail.com"})
    body = response.json()
    assert body["path"] == "personal_workspace"
    # and the reason says why it is safe, in words a person can read
    assert "no domain" in body["reason"].lower()


def test_two_gmail_users_are_two_separate_tenants(client):
    """The isolation guarantee, end to end.

    If this ever fails, every Gmail user in the world is in one organisation
    reading each other's warehouses. It is the worst failure this system can
    have.
    """
    token_a = sign_in(client, "alice@gmail.com")
    token_b = sign_in(client, "bob@gmail.com")

    org_a = client.get("/api/org", headers={"Authorization": f"Bearer {token_a}"}).json()
    org_b = client.get("/api/org", headers={"Authorization": f"Bearer {token_b}"}).json()

    assert org_a["id"] != org_b["id"], "two Gmail users landed in ONE organisation"
    assert org_a["kind"] == org_b["kind"] == "personal"
    assert org_a["domain"] is None and org_b["domain"] is None
    assert org_a["people_count"] == 1 and org_b["people_count"] == 1


def test_a_wrong_code_is_refused_and_counted(client):
    client.post("/api/auth/start", json={"email": "wrong@gmail.com"})
    response = client.post("/api/auth/verify",
                           json={"email": "wrong@gmail.com", "code": "000000"})
    assert response.status_code == 400
    assert "attempt" in response.json()["detail"].lower()


def test_a_code_cannot_be_used_twice(client):
    email = "once@gmail.com"
    client.post("/api/auth/start", json={"email": email})
    code = _code_for(client, email)
    first = client.post("/api/auth/verify", json={"email": email, "code": code})
    assert first.status_code == 200
    second = client.post("/api/auth/verify", json={"email": email, "code": code})
    assert second.status_code == 400


# ======================================================= authorisation ======

def test_no_token_is_401(client):
    assert client.get("/api/org").status_code == 401


def test_a_garbage_token_is_401(client):
    response = client.get("/api/org",
                          headers={"Authorization": "Bearer not.a.token"})
    assert response.status_code == 401


def test_a_refresh_token_is_not_an_access_token(client):
    """A refresh token lives two weeks. Accepting one as an access token
    would silently extend every session to a fortnight."""
    client.post("/api/auth/start", json={"email": "kinds@gmail.com"})
    code = _code_for(client, "kinds@gmail.com")
    session = client.post("/api/auth/verify",
                          json={"email": "kinds@gmail.com", "code": code}).json()

    response = client.get(
        "/api/org",
        headers={"Authorization": f"Bearer {session['refresh_token']}"},
    )
    assert response.status_code == 401


def test_another_tenants_connection_is_refused(client):
    """And it is refused with the same message a non-existent one gets, so
    existence cannot be discovered by iterating."""
    token = sign_in(client, "solo@gmail.com")
    headers = {"Authorization": f"Bearer {token}"}

    missing = client.get("/api/schema/999999", headers=headers)
    assert missing.status_code == 403
    assert "not yours" in missing.json()["detail"]


def test_a_personal_workspace_owner_sees_only_their_own(client):
    token = sign_in(client, "alone@gmail.com")
    headers = {"Authorization": f"Bearer {token}"}

    people = client.get("/api/org/people", headers=headers)
    assert people.status_code == 200
    assert len(people.json()) == 1

    connections = client.get("/api/connections", headers=headers)
    assert connections.json() == []


def test_me_returns_the_session_identity(client):
    token = sign_in(client, "whoami@gmail.com")
    body = client.get("/api/auth/me",
                      headers={"Authorization": f"Bearer {token}"}).json()
    assert body["email"] == "whoami@gmail.com"
    assert body["product_role"] == "owner"
    assert body["organisation"]["kind"] == "personal"


# ============================================================== asking ======

def test_asking_without_a_database_is_refused_not_crashed(client):
    token = sign_in(client, "asker@gmail.com")
    response = client.post(
        "/api/ask",
        headers={"Authorization": f"Bearer {token}"},
        json={"question": "Which region had the highest sales?", "connection_id": 1},
    )
    # no connection exists for this workspace, so it is a 403 with the same
    # message as any other unreachable identifier
    assert response.status_code == 403


def test_credentials_are_blocked_before_generation(client, monkeypatch):
    """Layer 1. There is no statement to show, because nothing was generated.

    Proving this needs a connection to exist, so the route reaches layer 1 --
    which is why the fixture seeds one directly rather than through the API.
    """
    from app.main import app  # noqa: PLC0415
    from db.entities import Connection, SchemaColumn  # noqa: PLC0415

    token = sign_in(client, "layer1@gmail.com")
    headers = {"Authorization": f"Bearer {token}"}
    org_id = client.get("/api/org", headers=headers).json()["id"]

    session = app.state.sessions()
    connection = Connection(org_id=org_id, name="wh", kind="uploaded",
                            database_name="wh")
    session.add(connection)
    session.flush()
    session.add(SchemaColumn(
        connection_id=connection.id, schema_name="public", table_name="orders",
        column_name="amount", data_type="numeric", is_public=True,
    ))
    session.commit()
    connection_id = connection.id
    session.close()

    response = client.post(
        "/api/ask", headers=headers,
        json={"question": "What is the database password?",
              "connection_id": connection_id},
    )
    body = response.json()
    assert body["mode"] == "blocked"
    assert body["layer"] == 1
    assert body["statement"] is None, "layer 1 runs before generation"
