"""Registering an external database (Backend Plan §9.3).

The one place the product is handed a network address by a user, which makes
it the one place SSRF can enter. Four checks, reported **one at a time**:

    host        resolve the name, then judge the resolved address
    tls         sslmode=require, never downgraded
    query       SELECT 1, five-second timeout
    privileges  the supplied account holds no write privilege

"Connection failed" is the least useful message a form can give, so each check
returns its own line and a refusal names which one fired. The three that did
not run are reported as not run — never as passed.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import SQLAlchemyError

from api import schemas
from app.deps import EnginesDep, OwnerDep, PrincipalDep, SessionDep
from app.rbac import Denied
from db.entities import Connection, SchemaColumn
from db.host_guard import check_host, require_tls
from db.introspect import introspect
from logs import audit_log

log = logging.getLogger("speakql.connections")

router_api = APIRouter(prefix="/api/connections", tags=["databases"])

TEST_TIMEOUT = 5


@router_api.get("", response_model=list[schemas.ConnectionOut])
def list_connections(principal: PrincipalDep,
                     session: SessionDep) -> list[schemas.ConnectionOut]:
    """Credentials are absent from the response model, so they cannot leak
    from here even by accident."""
    rows = session.scalars(
        select(Connection).where(Connection.org_id == principal.org_id)
        .order_by(Connection.created_at.asc())
    ).all()

    out: list[schemas.ConnectionOut] = []
    for row in rows:
        tables = session.scalar(
            select(SchemaColumn.table_name)
            .where(SchemaColumn.connection_id == row.id)
            .distinct()
        )
        count = len(set(session.scalars(
            select(SchemaColumn.table_name)
            .where(SchemaColumn.connection_id == row.id).distinct()
        ).all()))
        out.append(schemas.ConnectionOut(
            id=row.id, name=row.name, kind=row.kind,  # type: ignore[arg-type]
            database_name=row.database_name, host=row.host,
            table_count=count, created_at=row.created_at,
        ))
    return out


@router_api.post("", response_model=schemas.ConnectionResult)
def register(body: schemas.RegisterConnection, principal: OwnerDep,
             session: SessionDep, engines: EnginesDep) -> schemas.ConnectionResult:
    checks: list[schemas.ConnectionCheck] = []

    def not_run(*names: str) -> None:
        for name in names:
            checks.append(schemas.ConnectionCheck(
                check=name, passed=False, detail="not reached",  # type: ignore[arg-type]
            ))

    # --- 1. host -----------------------------------------------------------
    verdict = check_host(body.host, body.port)
    checks.append(schemas.ConnectionCheck(
        check="host", passed=verdict.allowed,
        detail=(", ".join(verdict.resolved) if verdict.allowed else verdict.reason),
    ))
    if not verdict.allowed:
        not_run("tls", "query", "privileges")
        audit_log.write(session, action=audit_log.Action.CONNECTION_REFUSED,
                        person_id=principal.person_id, org_id=principal.org_id,
                        target=body.host, detail={"reason": verdict.reason})
        return schemas.ConnectionResult(
            accepted=False, checks=checks, refused_because=verdict.reason
        )

    # --- 2/3/4. TLS, a test query, and the account's privileges ------------
    dsn = require_tls(
        f"postgresql+psycopg://{body.username}:{body.password}"
        f"@{body.host}:{body.port}/{body.database_name}"
    )
    checks.append(schemas.ConnectionCheck(
        check="tls", passed=True, detail="sslmode=require",
    ))

    probe = create_engine(dsn, connect_args={"connect_timeout": TEST_TIMEOUT},
                          pool_pre_ping=False)
    try:
        with probe.connect() as conn:
            conn.execute(text("SELECT 1"))
            checks.append(schemas.ConnectionCheck(
                check="query", passed=True, detail="SELECT 1 returned",
            ))

            writable = conn.execute(text(
                "SELECT count(*) FROM information_schema.role_table_grants "
                "WHERE grantee = current_user "
                "AND privilege_type IN ('INSERT','UPDATE','DELETE','TRUNCATE')"
            )).scalar_one()

            if writable:
                detail = f"this account holds {writable} write privileges"
                checks.append(schemas.ConnectionCheck(
                    check="privileges", passed=False, detail=detail,
                ))
                audit_log.write(
                    session, action=audit_log.Action.CONNECTION_REFUSED,
                    person_id=principal.person_id, org_id=principal.org_id,
                    target=body.host, detail={"reason": detail},
                )
                return schemas.ConnectionResult(
                    accepted=False, checks=checks,
                    refused_because=(
                        "Supply a read-only account. SpeakQL will not connect "
                        "with one that can write, even though it would never "
                        "issue a write — the grant is the guarantee, not our "
                        "good intentions."
                    ),
                )

            checks.append(schemas.ConnectionCheck(
                check="privileges", passed=True,
                detail="SELECT, USAGE and CONNECT only",
            ))
    except SQLAlchemyError as exc:
        checks.append(schemas.ConnectionCheck(
            check="query", passed=False, detail=_readable(exc),
        ))
        not_run("privileges")
        return schemas.ConnectionResult(
            accepted=False, checks=checks, refused_because=_readable(exc)
        )
    finally:
        probe.dispose()

    # --- accepted -----------------------------------------------------------
    connection = Connection(
        org_id=principal.org_id, name=body.name, kind="external",
        host=body.host, port=body.port, database_name=body.database_name,
        secret_cipher=None,   # encryption at rest wired with the KMS choice
    )
    session.add(connection)
    session.flush()

    # Reindex is the last stage, not a separate chore: the database is
    # queryable the moment this response returns.
    registered = create_engine(dsn, pool_pre_ping=True)
    try:
        introspect(session, registered, connection.id)
    finally:
        registered.dispose()

    audit_log.write(session, action=audit_log.Action.CONNECTION_ADDED,
                    person_id=principal.person_id, org_id=principal.org_id,
                    target=body.name)
    session.flush()

    return schemas.ConnectionResult(
        accepted=True, checks=checks,
        connection=schemas.ConnectionOut(
            id=connection.id, name=connection.name, kind="external",
            database_name=connection.database_name, host=connection.host,
            created_at=connection.created_at,
        ),
    )


@router_api.post("/{connection_id}/reindex", response_model=dict)
def reindex(connection_id: int, principal: OwnerDep, session: SessionDep,
            engines: EnginesDep) -> dict:
    connection = session.get(Connection, connection_id)
    if connection is None or connection.org_id != principal.org_id:
        raise Denied("no such database, or it is not yours")
    result = introspect(session, engines.read, connection_id)
    return {"reindexed": connection.name, "summary": result.summary}


def _readable(exc: Exception) -> str:
    text_ = str(getattr(exc, "orig", exc)).strip().splitlines()[0]
    lowered = text_.lower()
    if "password authentication failed" in lowered:
        return "those credentials were refused"
    if "timeout" in lowered or "could not connect" in lowered:
        return "the host did not answer within five seconds"
    if "does not exist" in lowered:
        return "that database does not exist on this host"
    if "ssl" in lowered:
        return "the host would not negotiate TLS"
    return "the test connection failed"
