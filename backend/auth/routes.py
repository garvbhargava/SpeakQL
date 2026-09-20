"""Sign-in (Backend Plan §7).

There is no password anywhere in this product. A six-digit code, valid for ten
minutes, used once.

The flow has one branch, and the interface asks about it exactly once:

    the domain is already registered   -> join as a member, never asked
    nobody from this domain is here    -> "are you setting this up, or joining?"
    a free-mail address                -> a personal workspace of one
    an invitation                      -> the token fixes the role and grants

The question is asked once because the answer is stored on the organisation.
Everyone who signs up from that domain afterwards joins as a member.
"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import func, select

from api import schemas
from app.deps import MailerDep, PrincipalDep, SessionDep, SettingsDep
from app.rbac import Unauthenticated
from auth import jwt_handler, otp
from auth.domain_resolver import (
    FreeEmailMode, SignupPath, assert_personal_is_unreachable, domain_of,
    normalise, resolve,
)
from db.entities import Connection, Organisation, Person
from logs import audit_log

log = logging.getLogger("speakql.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])

# The issued code lives here between request and verification. In a
# multi-container deployment this moves to Postgres; the shape does not
# change, and no other module reads it.
_PENDING: dict[str, tuple[str, dt.datetime, str]] = {}   # email -> (hash, expiry, path)


@router.post("/start", response_model=schemas.SignupStarted)
def start(body: schemas.SignupStart, session: SessionDep,
          settings: SettingsDep, mailer: MailerDep) -> schemas.SignupStarted:
    """Tell them what is about to happen, then send a code.

    The hint is returned *before* the code is sent, because the interface
    shows it while they are still typing -- so somebody learns that a personal
    address gets a workspace of its own before they commit to it.
    """
    email = normalise(body.email)
    mode = FreeEmailMode(settings.free_email_mode)
    resolution = resolve(session, email, mode)

    if resolution.path is SignupPath.REFUSED:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, resolution.reason)

    if resolution.path is SignupPath.INVITE_REQUIRED:
        return schemas.SignupStarted(
            path="invite_required", reason=resolution.reason,
            domain=resolution.domain,
        )

    otp.check_not_locked(session, email)

    issued = otp.generate(settings.secret_key, email)
    _PENDING[email] = (issued.code_hash, issued.expires_at, resolution.path.value)

    mailer.send_code(email, issued.code, purpose=resolution.path.value)

    return schemas.SignupStarted(
        path=resolution.path.value,  # type: ignore[arg-type]
        reason=resolution.reason,
        organisation_name=resolution.organisation.name if resolution.organisation else None,
        domain=resolution.domain,
        resend_after_seconds=int(otp.RESEND_AFTER.total_seconds()),
    )


@router.post("/verify", response_model=schemas.Session)
def verify(body: schemas.VerifyCode, session: SessionDep,
           settings: SettingsDep) -> schemas.Session:
    """Check the code, then create or join whatever the path decided."""
    email = normalise(body.email)

    otp.check_not_locked(session, email)

    pending = _PENDING.get(email)
    if pending is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "ask for a code first, or the code has expired")

    code_hash, expires_at, path = pending

    if not otp.verify(settings.secret_key, email, body.code, code_hash, expires_at):
        remaining = otp.record_failure(session, email)
        if remaining == 0:
            audit_log.write(session, action=audit_log.Action.OTP_LOCKOUT,
                            target=email)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"that code is not right. {remaining} attempt"
            f"{'s' if remaining != 1 else ''} left" if remaining
            else "too many incorrect codes; try again later",
        )

    # Single use, whatever happens next.
    _PENDING.pop(email, None)
    otp.record_success(session, email)

    person = _land(session, email, path, as_owner=body.as_owner)
    session.flush()

    return _issue_session(settings.secret_key, person)


@router.post("/refresh", response_model=schemas.Session)
def refresh(body: schemas.Refresh, session: SessionDep,
            settings: SettingsDep) -> schemas.Session:
    try:
        claims = jwt_handler.decode(settings.secret_key, body.refresh_token,
                                    expect="refresh")
    except jwt_handler.TokenError as exc:
        raise Unauthenticated(str(exc)) from exc

    person = session.get(Person, claims.person_id)
    if person is None or person.state != "active":
        raise Unauthenticated("this account is no longer active")

    return _issue_session(settings.secret_key, person)


@router.get("/me", response_model=schemas.Me)
def me(principal: PrincipalDep, session: SessionDep) -> schemas.Me:
    org = session.get(Organisation, principal.org_id)
    if org is None:
        raise Unauthenticated("this organisation no longer exists")
    return schemas.Me(
        person_id=principal.person_id,
        email=principal.email,
        product_role=principal.product_role,  # type: ignore[arg-type]
        state=principal.state,                # type: ignore[arg-type]
        organisation=_org_out(session, org),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_model=None)
def logout() -> None:
    """Tokens are stateless and short-lived; the client discards them.

    Said plainly rather than pretending to revoke something: a 30-minute
    access token cannot be un-issued without a revocation list, and this
    product does not have one. The refresh token is what the client actually
    drops.
    """
    return None


# ------------------------------------------------------------- internals ----

def _land(session: SessionDep, email: str, path: str, *, as_owner: bool) -> Person:
    """Create or join, according to the path decided at /start."""
    existing = session.scalar(select(Person).where(func.lower(Person.email) == email))
    if existing is not None:
        return existing

    if path == SignupPath.PERSONAL_WORKSPACE.value:
        # domain stays NULL. That null is the isolation guarantee.
        org = Organisation(name=email, kind="personal", domain=None)
        session.add(org)
        session.flush()
        assert_personal_is_unreachable(org)
        person = Person(email=email, org_id=org.id, product_role="owner",
                        state="active")
        session.add(person)
        session.flush()
        audit_log.write(session, action=audit_log.Action.ORG_CREATED,
                        person_id=person.id, org_id=org.id,
                        target="personal workspace")
        return person

    domain = domain_of(email)

    if path == SignupPath.CREATE_COMPANY.value and as_owner:
        org = Organisation(name=_company_name(domain), kind="company", domain=domain)
        session.add(org)
        session.flush()
        person = Person(email=email, org_id=org.id, product_role="owner",
                        state="active")
        session.add(person)
        session.flush()
        audit_log.write(session, action=audit_log.Action.ORG_CREATED,
                        person_id=person.id, org_id=org.id, target=domain)
        return person

    org = session.scalar(
        select(Organisation).where(
            func.lower(Organisation.domain) == domain, Organisation.kind == "company"
        )
    )
    if org is None:
        # "I was invited" on a domain nobody has registered. An invitation is
        # issued BY an organisation, and none exists -- so this is a dead end
        # by construction rather than a greyed-out button.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "There is no organisation to join. An invitation is issued by an "
            "organisation, and nobody from this domain has registered one.",
        )

    # A member joins pending, and the owner approves. The owner decides who is
    # in their company; the domain only decides which company.
    person = Person(email=email, org_id=org.id, product_role="member",
                    state="pending")
    session.add(person)
    session.flush()
    return person


def _issue_session(secret: str, person: Person) -> schemas.Session:
    common = dict(person_id=person.id, org_id=person.org_id,
                  product_role=person.product_role, email=person.email)
    return schemas.Session(
        access_token=jwt_handler.issue_access(secret, **common),
        refresh_token=jwt_handler.issue_refresh(secret, **common),
        expires_in=int(jwt_handler.ACCESS_TTL.total_seconds()),
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


def _company_name(domain: str) -> str:
    stem = domain.split(".")[0].replace("-", " ").replace("_", " ")
    return stem.title()

