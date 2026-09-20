"""Six-digit codes (Backend Plan §7.3).

There is no password anywhere in this product, which removes a whole class of
failure -- reuse, stuffing, leaked hashes, reset flows. What replaces it has
to be done carefully, because a six-digit code is only a million
possibilities.

The four things that make it safe:

    1. The code is generated with `secrets`, never `random`.
    2. Only its hash is stored, so a database read does not yield a code.
    3. Comparison is constant-time, so timing does not leak a prefix.
    4. Five failures locks the address for fifteen minutes, which turns a
       million guesses into roughly six years of them.

Rate limiting alone is what makes a six-digit code acceptable. Without rule 4
this design would be indefensible, so `record_failure` is not optional
book-keeping -- it is the control.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from db.entities import OtpAttempt

CODE_LENGTH = 6
CODE_TTL = dt.timedelta(minutes=10)
RESEND_AFTER = dt.timedelta(seconds=60)
MAX_FAILURES = 5
LOCKOUT = dt.timedelta(minutes=15)


class OtpError(RuntimeError):
    pass


class LockedOut(OtpError):
    def __init__(self, until: dt.datetime) -> None:
        self.until = until
        super().__init__("too many incorrect codes; try again later")


@dataclass(frozen=True)
class IssuedCode:
    code: str            # returned once, to be mailed. Never stored.
    code_hash: str
    expires_at: dt.datetime


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def generate(secret_key: str, email: str) -> IssuedCode:
    """A cryptographically random code, and the hash we keep instead of it."""
    # secrets, not random: random is seeded predictably and is not for this.
    code = f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"
    return IssuedCode(
        code=code,
        code_hash=hash_code(secret_key, email, code),
        expires_at=_now() + CODE_TTL,
    )


def hash_code(secret_key: str, email: str, code: str) -> str:
    """Keyed hash, salted with the address.

    The address in the message means a code stolen for one account cannot be
    replayed against another, and the key means a database dump alone is not
    enough to forge one.
    """
    message = f"{email.strip().lower()}:{code}".encode()
    return hmac.new(secret_key.encode(), message, hashlib.sha256).hexdigest()


def verify(secret_key: str, email: str, code: str, stored_hash: str,
           expires_at: dt.datetime) -> bool:
    """Constant-time comparison, and expiry checked first."""
    if _now() > expires_at:
        return False
    candidate = hash_code(secret_key, email, code)
    # compare_digest, not ==: an early-exit comparison leaks how many leading
    # characters were right, one request at a time.
    return hmac.compare_digest(candidate, stored_hash)


# --------------------------------------------------------------- lockout ----

def check_not_locked(session: Session, email: str) -> None:
    row = _attempt_row(session, email)
    if row is None or row.locked_until is None:
        return
    if row.locked_until > _now():
        raise LockedOut(row.locked_until)
    # window has passed; reset
    row.failures = 0
    row.locked_until = None
    session.flush()


def record_failure(session: Session, email: str) -> int:
    """Count a wrong code, and lock the address at the threshold.

    Returns how many attempts remain, so the interface can warn before the
    last one rather than after it.
    """
    row = _attempt_row(session, email)
    if row is None:
        row = OtpAttempt(email=email.strip().lower(), failures=0)
        session.add(row)
        session.flush()

    row.failures += 1
    row.updated_at = _now()
    if row.failures >= MAX_FAILURES:
        row.locked_until = _now() + LOCKOUT
    session.flush()
    return max(0, MAX_FAILURES - row.failures)


def record_success(session: Session, email: str) -> None:
    row = _attempt_row(session, email)
    if row is not None:
        row.failures = 0
        row.locked_until = None
        row.updated_at = _now()
        session.flush()


def _attempt_row(session: Session, email: str) -> OtpAttempt | None:
    return session.scalar(
        select(OtpAttempt).where(OtpAttempt.email == email.strip().lower())
    )
