"""FastAPI dependencies: how a request acquires identity and resources.

Questions 1 and 2 of the five (§6) are answered here, on every request, before
a route body runs. Questions 3, 4 and 5 are answered in rbac.py and
object_access.py, because they need to know what is being asked for.

The invariant this file protects: **the organisation comes from the session
token and nothing else.** No route reads an `org_id` from a request body. If
you ever need one, you want `principal.org_id`.
"""

from __future__ import annotations

from typing import Annotated, Iterator

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from app.config import Settings
from app.ratelimit import RateLimiter
from app.rbac import Principal, Unauthenticated, require_active
from auth import jwt_handler
from db.engines import Engines
from db.entities import Person
from db.session import SessionFactory


# ------------------------------------------------------------- resources ----

def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_engines(request: Request) -> Engines:
    return request.app.state.engines


def get_limiter(request: Request) -> RateLimiter:
    return request.app.state.limiter


def get_mailer(request: Request):
    return request.app.state.mailer


def get_llm(request: Request):
    """The LLM client, or None when it is unreachable.

    None is a legitimate state, not an error: the explainer falls back to its
    deterministic sentence and the pipeline still answers. A backend that
    refused to work without a model would make the demo hostage to a 3 GB
    download.
    """
    return getattr(request.app.state, "llm", None)


def get_session(request: Request) -> Iterator[Session]:
    factory: SessionFactory = request.app.state.sessions
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


SettingsDep = Annotated[Settings, Depends(get_settings)]
EnginesDep = Annotated[Engines, Depends(get_engines)]
SessionDep = Annotated[Session, Depends(get_session)]
LimiterDep = Annotated[RateLimiter, Depends(get_limiter)]
MailerDep = Annotated[object, Depends(get_mailer)]
LLMDep = Annotated[object, Depends(get_llm)]


# -------------------------------------------------------------- identity ----

def _bearer(authorization: str | None) -> str:
    if not authorization:
        raise Unauthenticated()
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise Unauthenticated("expected a bearer token")
    return token.strip()


def current_principal(
    session: SessionDep,
    settings: SettingsDep,
    authorization: Annotated[str | None, Header()] = None,
    x_speakql_persona: Annotated[str | None, Header()] = None,
) -> Principal:
    """Questions 1 and 2, on every authenticated request.

    The token carries who you are. It does **not** carry your database grants
    — those are read live in rbac.resolve_connection, because a token issued
    an hour ago would still claim a grant the owner revoked ten minutes ago.

    The person row is re-read here too, so a suspended account stops working
    immediately rather than when its token expires.
    """
    token = _bearer(authorization)

    try:
        claims = jwt_handler.decode(settings.secret_key, token, expect="access")
    except jwt_handler.TokenExpired as exc:
        raise Unauthenticated("this session has expired") from exc
    except jwt_handler.TokenError as exc:
        raise Unauthenticated(str(exc)) from exc

    person = session.get(Person, claims.person_id)
    if person is None:
        raise Unauthenticated("this account no longer exists")

    # The token says which organisation. The row has to agree, or the token
    # was issued before a change that moved them.
    if person.org_id != claims.org_id:
        raise Unauthenticated("this session is out of date; sign in again")

    # Persona is a display choice and arrives per request. It is NOT a
    # permission: rbac.require(SEE_GENERATED_SQL) checks the grant as well,
    # and a viewer never receives SQL whatever this header says.
    persona = (x_speakql_persona or "workbench").strip().lower()
    if persona not in {"workbench", "readout"}:
        persona = "workbench"

    principal = Principal(
        person_id=person.id,
        org_id=person.org_id,
        email=person.email,
        product_role=person.product_role,
        state=person.state,
        persona=persona,
    )
    return require_active(principal)


PrincipalDep = Annotated[Principal, Depends(current_principal)]


def owner_only(principal: PrincipalDep) -> Principal:
    """For routes an owner alone may reach. The finer-grained checks still
    run inside; this is the cheap gate at the door."""
    from app.rbac import Action, require  # noqa: PLC0415 - avoids a cycle
    require(Action.ADMINISTER, principal)
    return principal


OwnerDep = Annotated[Principal, Depends(owner_only)]
