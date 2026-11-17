"""Session tokens (Backend Plan §7.3).

Short-lived access token, longer-lived refresh token, both signed with
HS256 and the application's SECRET_KEY.

What the token carries matters as much as how it is signed. It carries the
person id, the organisation id and the product role -- and **nothing about
database grants**. Grants change, tokens do not, and a token that claimed
"analyst on connection 4" would still claim it an hour after the owner revoked
it. So grants are read from the database on every request, and the token only
says who you are.

That is the rule behind §6's invariant: the organisation identity comes from
the session, and the grant comes from a live query.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import jwt

ALGORITHM = "HS256"
ACCESS_TTL = dt.timedelta(minutes=30)
REFRESH_TTL = dt.timedelta(days=14)

ISSUER = "speakql"


class TokenError(RuntimeError):
    pass


class TokenExpired(TokenError):
    pass


@dataclass(frozen=True)
class Claims:
    person_id: int
    org_id: int
    product_role: str
    email: str
    kind: str  # access | refresh


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def issue_access(secret: str, *, person_id: int, org_id: int,
                 product_role: str, email: str) -> str:
    return _encode(secret, person_id, org_id, product_role, email,
                   kind="access", ttl=ACCESS_TTL)


def issue_refresh(secret: str, *, person_id: int, org_id: int,
                  product_role: str, email: str) -> str:
    return _encode(secret, person_id, org_id, product_role, email,
                   kind="refresh", ttl=REFRESH_TTL)


def _encode(secret: str, person_id: int, org_id: int, product_role: str,
            email: str, *, kind: str, ttl: dt.timedelta) -> str:
    issued = _now()
    payload = {
        "sub": str(person_id),
        "org": org_id,
        "role": product_role,
        "email": email,
        "kind": kind,
        "iss": ISSUER,
        "iat": issued,
        "exp": issued + ttl,
        # nbf guards against a clock-skewed token being accepted early
        "nbf": issued - dt.timedelta(seconds=5),
    }
    return jwt.encode(payload, secret, algorithm=ALGORITHM)


def decode(secret: str, token: str, *, expect: str = "access") -> Claims:
    """Verify and unpack. Raises TokenError on anything unexpected."""
    try:
        payload = jwt.decode(
            token,
            secret,
            # A list of exactly one algorithm. Accepting a list the caller
            # can influence is how the `alg: none` family of bugs happens.
            algorithms=[ALGORITHM],
            issuer=ISSUER,
            options={"require": ["exp", "iat", "sub", "iss"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpired("this session has expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("invalid session token") from exc

    kind = payload.get("kind")
    if kind != expect:
        # A refresh token must never be accepted as an access token: it lives
        # far longer and is meant to be exchanged, not presented.
        raise TokenError(f"expected a {expect} token, got {kind}")

    try:
        return Claims(
            person_id=int(payload["sub"]),
            org_id=int(payload["org"]),
            product_role=str(payload["role"]),
            email=str(payload["email"]),
            kind=kind,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenError("session token is missing a required claim") from exc
