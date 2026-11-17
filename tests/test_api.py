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


class _OneTenant:
    """Stand-in for db.tenant_engine.TenantEngines: every connection resolves
    to the one SQLite database. That each connection really gets its OWN
    engine is asserted in tests/test_isolation.py, without a server; here the
    point is only that routes go through `tenants` at all -- the old shared
    `engines.read` no longer exists, so a route that reached for it would
    fail these tests with an AttributeError."""

    def __init__(self, engine):
        self._engine = engine

    def read(self, connection):
        return self._engine

    def write(self, connection):
        from db.tenant_engine import WriteNotSupported  # noqa: PLC0415
        if connection.kind != "internal":
            raise WriteNotSupported("internal warehouses only")
        return self._engine

    edits = write

    def expected_database(self, connection):
        return None  # SQLite has no current_database(); see test_isolation.py

    def forget(self, connection_id):
        pass

    def dispose(self):
        pass


class _OneEngine:
    """Stand-in for db.engines.Engines. Role separation is enforced by
    Postgres and is asserted in tests/test_privileges.py -- these tests are
    about what the *application* refuses, which is the part no grant can save
    you from."""

    def __init__(self, engine):
        self.meta = self.uploads = engine
        self.tenants = _OneTenant(engine)

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
        # Every test signs in from the same TestClient address, so the
        # per-network cap is raised here; the tests that are ABOUT the caps
        # install their own limiter.
        app.state.limiter = RateLimiter(ask_per_hour=100, upload_per_day=10,
                                        export_per_hour=10,
                                        codes_per_ip_hour=10_000)
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


def test_a_returning_person_is_told_they_are_signing_in(client):
    """Not the signup hint for their domain. A returning owner was told
    "you will join as a member"."""
    sign_in(client, "returning@returning-co.io", as_owner=True)
    from auth import routes as auth_routes  # noqa: PLC0415
    auth_routes._PENDING.pop("returning@returning-co.io", None)  # skip the cooldown

    again = client.post("/api/auth/start", json={"email": "returning@returning-co.io"})
    assert again.json()["path"] == "sign_in"
    assert "member" not in again.json()["reason"]


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


# ======================================================== sign-in abuse ======

def test_the_lockout_actually_fires(client):
    """Five wrong codes lock the address -- and the count survives the refusal.

    The first version recorded each failure inside the request's transaction
    and then refused, which rolled the transaction back: every wrong code said
    "4 attempts left" forever, and a six-digit code could be guessed at
    leisure. This test is the one that would have caught it.
    """
    email = "lockme@gmail.com"
    client.post("/api/auth/start", json={"email": email})

    seen = []
    for _ in range(5):
        response = client.post("/api/auth/verify",
                               json={"email": email, "code": "000000"})
        assert response.status_code == 400
        seen.append(response.json()["detail"])

    assert seen[0].endswith("4 attempts left")
    assert seen[3].endswith("1 attempt left")
    assert "too many" in seen[4]

    # Locked: even the right code would now be refused, and asking for a new
    # one does not reset anything -- it is a 429 with a retry window, not a 500.
    locked = client.post("/api/auth/start", json={"email": email})
    assert locked.status_code == 429
    assert int(locked.headers["Retry-After"]) > 0


def test_a_second_code_within_a_minute_is_refused(client):
    email = "twice@gmail.com"
    assert client.post("/api/auth/start", json={"email": email}).status_code == 200
    again = client.post("/api/auth/start", json={"email": email})
    assert again.status_code == 429
    assert "less than a minute" in again.json()["detail"]
    # The error handler used to drop headers, so this never reached a client.
    assert 0 < int(again.headers["Retry-After"]) <= 60


def test_codes_per_address_are_capped(client):
    """The cooldown bounds the rate; this bounds the hour."""
    from app.main import app  # noqa: PLC0415
    from app.ratelimit import RateLimiter  # noqa: PLC0415
    from auth import routes as auth_routes  # noqa: PLC0415

    saved = app.state.limiter
    app.state.limiter = RateLimiter(ask_per_hour=1, upload_per_day=1,
                                    export_per_hour=1, codes_per_address_hour=2,
                                    codes_per_ip_hour=10_000)
    try:
        email = "flood@gmail.com"
        for _ in range(2):
            auth_routes._PENDING.pop(email, None)  # as if the minute had passed
            assert client.post("/api/auth/start", json={"email": email}).status_code == 200
        auth_routes._PENDING.pop(email, None)
        capped = client.post("/api/auth/start", json={"email": email})
        assert capped.status_code == 429
        assert "Retry-After" in capped.headers
    finally:
        app.state.limiter = saved


# ========================================================== invitations ======

def _invite(client, owner_token: str, email: str) -> str:
    """Invite `email` and return the raw token from the invitation email."""
    import re  # noqa: PLC0415

    from app.main import app  # noqa: PLC0415

    response = client.post("/api/org/invite",
                           headers={"Authorization": f"Bearer {owner_token}"},
                           json={"email": email, "grants": []})
    assert response.status_code == 201, response.text
    for message in reversed(app.state.mailer.outbox):
        if message.to == email and "token=" in message.body:
            return re.search(r"token=([A-Za-z0-9_\-]+)", message.body).group(1)
    raise AssertionError(f"no invitation was sent to {email}")


@pytest.fixture(scope="module")
def company_owner(client) -> str:
    return sign_in(client, "owner@acme-invites.io", as_owner=True)


def test_an_invitation_carries_a_gmail_address_into_a_company(client, company_owner):
    """§7.2: the token fixes the organisation, so no domain lookup happens --
    and the free-mail rule is not weakened, because nothing was resolved."""
    email = "contractor@gmail.com"
    token = _invite(client, company_owner, email)

    started = client.post("/api/auth/start",
                          json={"email": email, "invite_token": token})
    assert started.status_code == 200, started.text
    assert started.json()["path"] == "invitation"
    assert started.json()["organisation_name"] == "Acme Invites"

    code = _code_for(client, email)
    session = client.post("/api/auth/verify", json={"email": email, "code": code})
    assert session.status_code == 200, session.text

    me = client.get("/api/auth/me", headers={
        "Authorization": f"Bearer {session.json()['access_token']}"}).json()
    assert me["organisation"]["name"] == "Acme Invites"
    assert me["organisation"]["kind"] == "company"
    assert me["product_role"] == "member", "an invitation never makes an owner"
    assert me["state"] == "active", "the invitation is the owner's approval"


def test_an_invitation_cannot_be_redeemed_twice(client, company_owner):
    email = "once-only@gmail.com"
    token = _invite(client, company_owner, email)
    client.post("/api/auth/start", json={"email": email, "invite_token": token})
    client.post("/api/auth/verify",
                json={"email": email, "code": _code_for(client, email)})

    again = client.post("/api/auth/start",
                        json={"email": email, "invite_token": token})
    assert again.status_code == 400
    assert again.json()["detail"].startswith("this invitation is not valid")


def test_an_invitation_is_bound_to_the_address_it_was_sent_to(client, company_owner):
    """A leaked link is not enough: it must be redeemed from its own inbox."""
    token = _invite(client, company_owner, "intended@gmail.com")
    stolen = client.post("/api/auth/start",
                         json={"email": "thief@gmail.com", "invite_token": token})
    garbage = client.post("/api/auth/start",
                          json={"email": "thief@gmail.com",
                                "invite_token": "x" * 43})
    assert stolen.status_code == garbage.status_code == 400
    # Indistinguishable from a token that never existed (§7.5).
    assert stolen.json()["detail"] == garbage.json()["detail"]


def test_an_expired_invitation_is_refused_like_a_missing_one(client, company_owner):
    import datetime as dt  # noqa: PLC0415

    from sqlalchemy import update  # noqa: PLC0415

    from app.main import app  # noqa: PLC0415
    from auth.invitations import REFUSAL, hash_token  # noqa: PLC0415
    from db.entities import Invitation  # noqa: PLC0415

    email = "late@gmail.com"
    token = _invite(client, company_owner, email)
    with app.state.sessions.begin() as session:
        session.execute(update(Invitation)
                        .where(Invitation.token_hash == hash_token(token))
                        .values(expires_at=dt.datetime(2000, 1, 1,
                                                       tzinfo=dt.timezone.utc)))

    response = client.post("/api/auth/start",
                           json={"email": email, "invite_token": token})
    assert response.status_code == 400
    assert response.json()["detail"] == REFUSAL


def test_a_racing_second_redemption_gets_nothing(client, company_owner):
    """The claim is a conditional UPDATE, so two redemptions that both passed
    the read cannot both succeed. Driven directly, because two HTTP requests
    in a test client do not race."""
    from app.main import app  # noqa: PLC0415
    from auth import invitations  # noqa: PLC0415
    from db.entities import Invitation  # noqa: PLC0415

    email = "racer@gmail.com"
    token = _invite(client, company_owner, email)
    with app.state.sessions() as session:
        invitation_id = session.query(Invitation.id).filter(
            Invitation.token_hash == invitations.hash_token(token)).scalar()

    with app.state.sessions.begin() as first:
        invitations.redeem(first, invitation_id, email)

    with app.state.sessions() as second:
        with pytest.raises(invitations.InvitationError):
            invitations.redeem(second, invitation_id, email)


def test_invite_only_mode_still_honours_an_invitation(client, company_owner):
    """invite_only: a free-mail address may redeem an invitation but not
    create anything (§7.2)."""
    import dataclasses  # noqa: PLC0415

    from app.main import app  # noqa: PLC0415

    saved = app.state.settings
    app.state.settings = dataclasses.replace(saved, free_email_mode="invite_only")
    try:
        email = "invited-only@gmail.com"
        token = _invite(client, company_owner, email)

        uninvited = client.post("/api/auth/start", json={"email": email})
        assert uninvited.json()["path"] == "invite_required"

        invited = client.post("/api/auth/start",
                              json={"email": email, "invite_token": token})
        assert invited.status_code == 200
        assert invited.json()["path"] == "invitation"
    finally:
        app.state.settings = saved


def test_listing_the_seeded_kind_of_connection_does_not_crash(client):
    """`internal` -- the kind every seeded demo warehouse has -- was missing
    from the response model, so listing them was a 500."""
    from app.main import app  # noqa: PLC0415
    from db.entities import Connection  # noqa: PLC0415

    token = sign_in(client, "lister@gmail.com")
    headers = {"Authorization": f"Bearer {token}"}
    org_id = client.get("/api/org", headers=headers).json()["id"]
    with app.state.sessions.begin() as session:
        session.add(Connection(org_id=org_id, name="demo", kind="internal",
                               database_name="northwind_dw"))

    listed = client.get("/api/connections", headers=headers)
    assert listed.status_code == 200, listed.text
    assert listed.json()[0]["kind"] == "internal"
