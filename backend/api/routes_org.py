"""Organisation, people and access (Backend Plan §7.5, §8).

Owner-only, all of it. The finer checks still run inside each route; the
dependency is the cheap gate at the door.
"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from api import schemas
from app.deps import MailerDep, OwnerDep, PrincipalDep, SessionDep, SettingsDep
from app.rbac import Denied
from auth import invitations
from db.entities import AccessGrant, Connection, Invitation, Organisation, Person
from logs import audit_log

log = logging.getLogger("speakql.org")

router_api = APIRouter(prefix="/api/org", tags=["organisation"])


@router_api.get("", response_model=schemas.OrganisationOut)
def get_org(principal: PrincipalDep, session: SessionDep) -> schemas.OrganisationOut:
    org = session.get(Organisation, principal.org_id)
    if org is None:
        raise Denied("no such organisation")
    return _org_out(session, org)


@router_api.get("/people", response_model=list[schemas.PersonOut])
def list_people(principal: OwnerDep, session: SessionDep) -> list[schemas.PersonOut]:
    """Scoped by the query, not filtered afterwards.

    A WHERE clause cannot leak a row a serialiser forgot to drop.
    """
    people = session.scalars(
        select(Person).where(Person.org_id == principal.org_id)
        .order_by(Person.created_at.asc())
    ).all()

    connections = {
        c.id: c.name
        for c in session.scalars(
            select(Connection).where(Connection.org_id == principal.org_id)
        )
    }

    out: list[schemas.PersonOut] = []
    for person in people:
        grants = session.scalars(
            select(AccessGrant).where(
                AccessGrant.person_id == person.id,
                AccessGrant.revoked_at.is_(None),
            )
        ).all()
        out.append(schemas.PersonOut(
            id=person.id, email=person.email,
            product_role=person.product_role,  # type: ignore[arg-type]
            state=person.state,
            grants=[
                schemas.GrantOut(
                    connection_id=g.connection_id,
                    connection_name=connections.get(g.connection_id, "?"),
                    db_role=g.db_role,  # type: ignore[arg-type]
                )
                for g in grants if g.connection_id in connections
            ],
        ))
    return out


@router_api.post("/invite", response_model=schemas.PersonOut,
                 status_code=status.HTTP_201_CREATED)
def invite(body: schemas.InvitePerson, principal: OwnerDep,
           session: SessionDep, settings: SettingsDep,
           mailer: MailerDep) -> schemas.PersonOut:
    """Single-use, expiring, and it fixes the role and the grants.

    All three matter. A token that could be redeemed twice makes two accounts;
    one that never expires is a credential sitting in an inbox forever; and
    one that did not fix the role would let a redeemer choose to be an owner,
    which is how you get a second owner nobody approved. Redemption is in
    auth/invitations.py.
    """
    org = session.get(Organisation, principal.org_id)
    if org is None:
        raise Denied("no such organisation")

    email = body.email.strip().lower()
    if session.scalar(select(Person).where(func.lower(Person.email) == email)):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "that address already has an account")

    # Verify every connection in the grant list belongs to this organisation
    # before writing any of them -- a grant to another tenant's database would
    # be the worst possible thing to create here.
    for grant in body.grants:
        connection = session.get(Connection, grant.connection_id)
        if connection is None or connection.org_id != principal.org_id:
            raise Denied("no such database, or it is not yours")

    raw_token, token_hash = invitations.new_token()
    invitation = Invitation(
        org_id=org.id,
        email=email,
        token_hash=token_hash,
        product_role=body.product_role,
        grants_json=[g.model_dump() for g in body.grants],
        expires_at=(dt.datetime.now(dt.timezone.utc)
                    + dt.timedelta(hours=settings.invite_ttl_hours)),
    )
    session.add(invitation)

    person = Person(email=email, org_id=org.id,
                    product_role=body.product_role, state="pending")
    session.add(person)
    session.flush()

    for grant in body.grants:
        session.add(AccessGrant(
            person_id=person.id, connection_id=grant.connection_id,
            db_role=grant.db_role, granted_by=principal.person_id,
        ))

    audit_log.write(session, action=audit_log.Action.PERSON_INVITED,
                    person_id=principal.person_id, org_id=org.id, target=email)
    session.flush()

    mailer.send_invitation(email, org.name, raw_token)

    return schemas.PersonOut(
        id=person.id, email=person.email,
        product_role=person.product_role,  # type: ignore[arg-type]
        state=person.state, grants=[],
    )


@router_api.post("/people/{person_id}/approve", response_model=schemas.PersonOut)
def approve(person_id: int, principal: OwnerDep,
            session: SessionDep) -> schemas.PersonOut:
    """The owner decides who is in their company.

    The domain decides which company somebody may join; it does not decide
    that they may. Those are different questions and this is the second one.
    """
    person = session.get(Person, person_id)
    if person is None or person.org_id != principal.org_id:
        raise Denied("no such person, or they are not in your organisation")
    if person.state == "active":
        return _person_out(person)

    person.state = "active"
    audit_log.write(session, action=audit_log.Action.PERSON_APPROVED,
                    person_id=principal.person_id, org_id=principal.org_id,
                    target=person.email)
    session.flush()
    return _person_out(person)


@router_api.post("/people/{person_id}/grants", response_model=schemas.PersonOut)
def set_grant(person_id: int, body: schemas.GrantIn, principal: OwnerDep,
              session: SessionDep) -> schemas.PersonOut:
    person = session.get(Person, person_id)
    if person is None or person.org_id != principal.org_id:
        raise Denied("no such person, or they are not in your organisation")

    connection = session.get(Connection, body.connection_id)
    if connection is None or connection.org_id != principal.org_id:
        raise Denied("no such database, or it is not yours")

    existing = session.scalar(select(AccessGrant).where(
        AccessGrant.person_id == person_id,
        AccessGrant.connection_id == body.connection_id,
    ))
    if existing is not None:
        existing.db_role = body.db_role
        existing.revoked_at = None
        existing.granted_by = principal.person_id
    else:
        session.add(AccessGrant(
            person_id=person_id, connection_id=body.connection_id,
            db_role=body.db_role, granted_by=principal.person_id,
        ))

    audit_log.write(session, action=audit_log.Action.GRANT_ADDED,
                    person_id=principal.person_id, org_id=principal.org_id,
                    target=f"{person.email}:{connection.name}:{body.db_role}")
    session.flush()
    return _person_out(person)


@router_api.delete("/people/{person_id}/grants/{connection_id}",
                   status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def revoke_grant(person_id: int, connection_id: int, principal: OwnerDep,
                 session: SessionDep) -> None:
    """Revoked, not deleted.

    `revoked_at` keeps the history: "who could see this, and until when" is a
    question somebody will eventually have to answer.
    """
    person = session.get(Person, person_id)
    if person is None or person.org_id != principal.org_id:
        raise Denied("no such person, or they are not in your organisation")

    grant = session.scalar(select(AccessGrant).where(
        AccessGrant.person_id == person_id,
        AccessGrant.connection_id == connection_id,
        AccessGrant.revoked_at.is_(None),
    ))
    if grant is None:
        return None

    grant.revoked_at = dt.datetime.now(dt.timezone.utc)
    audit_log.write(session, action=audit_log.Action.GRANT_REVOKED,
                    person_id=principal.person_id, org_id=principal.org_id,
                    target=f"{person.email}:{connection_id}")
    session.flush()
    return None


# ------------------------------------------------------------- internals ----

def _person_out(person: Person) -> schemas.PersonOut:
    return schemas.PersonOut(
        id=person.id, email=person.email,
        product_role=person.product_role,  # type: ignore[arg-type]
        state=person.state, grants=[],
    )


def _org_out(session, org: Organisation) -> schemas.OrganisationOut:
    people = session.scalar(
        select(func.count()).select_from(Person).where(Person.org_id == org.id)
    ) or 0
    databases = session.scalar(
        select(func.count()).select_from(Connection).where(Connection.org_id == org.id)
    ) or 0
    return schemas.OrganisationOut(
        id=org.id, name=org.name, kind=org.kind, domain=org.domain,
        people_count=people, database_count=databases,
    )
