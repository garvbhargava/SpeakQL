"""Sign-in (Backend Plan §7).

There is no password anywhere in this product. A six-digit code, valid for ten
minutes, used once.

The flow has one branch, and the interface asks about it exactly once:

    an invitation                      -> the token fixes the organisation,
                                          the role and the grants
    the domain is already registered   -> join as a member, never asked
    nobody from this domain is here    -> "are you setting this up, or joining?"
    a free-mail address                -> a personal workspace of one

The invitation is checked first, before any domain lookup, which is what lets
it carry a free-mail address into a company without the domain rule ever
being consulted.

The question is asked once because the answer is stored on the organisation.
Everyone who signs up from that domain afterwards joins as a member.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import func, select

from api import schemas
from app.deps import LimiterDep, MailerDep, PrincipalDep, SessionDep, SettingsDep
from app.ratelimit import Limited, RateLimiter
from app.rbac import Unauthenticated
from auth import invitations, jwt_handler, otp
from auth.domain_resolver import (
    FreeEmailMode, SignupPath, assert_personal_is_unreachable, domain_of,
    normalise, resolve,
)
from db.entities import Connection, Organisation, Person
from logs import audit_log

log = logging.getLogger("speakql.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])

INVITATION = "invitation"
SIGN_IN = "sign_in"


@dataclass(frozen=True)
class _Pending:
    code_hash: str
    expires_at: dt.datetime
    path: str
    invitation_id: int | None = None

    @property
    def issued_at(self) -> dt.datetime:
        return self.expires_at - otp.CODE_TTL


# The issued code lives here between request and verification. In a
# multi-container deployment this moves to Postgres; the shape does not
# change, and no other module reads it.
_PENDING: dict[str, _Pending] = {}


@router.post("/start", response_model=schemas.SignupStarted)
def start(body: schemas.SignupStart, request: Request, session: SessionDep,
          settings: SettingsDep, mailer: MailerDep,
          limiter: LimiterDep) -> schemas.SignupStarted:
    """Tell them what is about to happen, then send a code.

    The hint is returned *before* the code is sent, because the interface
    shows it while they are still typing -- so somebody learns that a personal
    address gets a workspace of its own before they commit to it.
    """
    email = normalise(body.email)
    invitation_id: int | None = None

    if body.invite_token:
        try:
            invitation = invitations.live_invitation(session, body.invite_token, email)
        except invitations.InvitationError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        org = session.get(Organisation, invitation.org_id)
        invitation_id = invitation.id
        path = INVITATION
        started = schemas.SignupStarted(
            path=INVITATION,
            reason=(f"You were invited to {org.name}. Enter the code sent to "
                    f"{email} to accept."),
            organisation_name=org.name,
            domain=org.domain,
        )
    elif (existing := session.scalar(
            select(Person).where(func.lower(Person.email) == email))) is not None \
            and existing.state != "pending":
        # Somebody signing in again. Without this branch a returning owner was
        # told "you will join as a member" -- the signup hint for their domain.
        # A PENDING person is still joining, so they fall through to that hint,
        # which is true for them.
        org = session.get(Organisation, existing.org_id)
        path = SIGN_IN
        started = schemas.SignupStarted(
            path=SIGN_IN,
            reason=f"Welcome back. Enter the code sent to {email}.",
            organisation_name=org.name if org else None,
            domain=org.domain if org else None,
        )
    else:
        mode = FreeEmailMode(settings.free_email_mode)
        resolution = resolve(session, email, mode)

        if resolution.path is SignupPath.REFUSED:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, resolution.reason)

        if resolution.path is SignupPath.INVITE_REQUIRED:
            return schemas.SignupStarted(
                path="invite_required", reason=resolution.reason,
                domain=resolution.domain,
            )

        path = resolution.path.value
        started = schemas.SignupStarted(
            path=path,  # type: ignore[arg-type]
            reason=resolution.reason,
            organisation_name=resolution.organisation.name if resolution.organisation else None,
            domain=resolution.domain,
        )

    otp.check_not_locked(session, email)
    _throttle(limiter, email, request)

    issued = otp.generate(settings.secret_key, email)
    _PENDING[email] = _Pending(issued.code_hash, issued.expires_at, path, invitation_id)

    mailer.send_code(email, issued.code, purpose=path)

    return started.model_copy(update={
        "resend_after_seconds": int(otp.RESEND_AFTER.total_seconds()),
    })


@router.post("/verify", response_model=schemas.Session)
def verify(body: schemas.VerifyCode, session: SessionDep,
           settings: SettingsDep) -> schemas.Session:
    """Check the code, then create, join or redeem whatever /start decided."""
    email = normalise(body.email)

    otp.check_not_locked(session, email)

    pending = _PENDING.get(email)
    if pending is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "ask for a code first, or the code has expired")

    if not otp.verify(settings.secret_key, email, body.code,
                      pending.code_hash, pending.expires_at):
        remaining = otp.record_failure(session, email)
        if remaining == 0:
            audit_log.write(session, action=audit_log.Action.OTP_LOCKOUT,
                            target=email)
            _PENDING.pop(email, None)  # the code dies with the lockout
        # Commit the count BEFORE refusing. Raising rolls this request's
        # transaction back, and the first version lost the count with it --
        # so the five-attempt lockout, the one control that makes a six-digit
        # code defensible, could never fire.
        session.commit()
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"that code is not right. {remaining} attempt"
            f"{'s' if remaining != 1 else ''} left" if remaining
            else "too many incorrect codes; try again later",
        )

    # Single use, whatever happens next.
    _PENDING.pop(email, None)
    otp.record_success(session, email)

    if pending.path == INVITATION and pending.invitation_id is not None:
        try:
            person = invitations.redeem(session, pending.invitation_id, email)
        except invitations.InvitationError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
        audit_log.write(session, action=audit_log.Action.INVITATION_REDEEMED,
                        person_id=person.id, org_id=person.org_id, target=email)
    else:
        person = _land(session, email, pending.path, as_owner=body.as_owner)
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

def _throttle(limiter: RateLimiter, email: str, request: Request) -> None:
    """§7.3: a sixty-second resend cooldown, then hourly caps per address and
    per network. Without these, /start is a free service for mailing codes to
    strangers -- the code is useless to the sender, the inbox flood is not."""
    previous = _PENDING.get(email)
    if previous is not None:
        wait = (previous.issued_at + otp.RESEND_AFTER
                - dt.datetime.now(dt.timezone.utc)).total_seconds()
        if wait > 0:
            seconds = math.ceil(wait)
            raise HTTPException(
                status.HTTP_429_TOO_MANY_REQUESTS,
                f"a code was sent less than a minute ago. Wait {seconds} "
                "seconds before asking for another.",
                headers={"Retry-After": str(seconds)},
            )

    # The socket's address, never X-Forwarded-For: that header is whatever the
    # caller typed. Behind a trusted proxy, uvicorn's --proxy-headers is the
    # place to change this, not here.
    network = request.client.host if request.client else "unknown"
    for key, what in ((network, Limited.CODE_PER_IP),
                      (email, Limited.CODE_PER_ADDRESS)):
        decision = limiter.check(key, what)
        if not decision.allowed:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                                decision.message,
                                headers={"Retry-After": str(decision.retry_after_seconds)})


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
